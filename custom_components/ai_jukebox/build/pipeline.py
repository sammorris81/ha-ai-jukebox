"""Per-playlist pool construction, batch by batch.

Appends each batch to the pool as it completes rather than assembling
everything and saving at the end, so playback works within seconds of a
rebuild starting rather than after several minutes.

A batch's fetched track listings are persisted to disk before the pick call
and deleted once its tracks land. Nothing reads them back: the resume state is
the pool record's `batch_plan`. A file left behind preserves the exact model
input for inspection when a pick looks wrong, which is the only reason it
survives.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import timedelta
from typing import Final

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..const import (
    BUILD_BATCH_ATTEMPTS,
    BUILD_BATCH_BACKOFF_SECONDS,
    BUILD_MAX_BATCH_ATTEMPTS,
    PLAYS_RETENTION_HOURS,
)
from . import curate
from .catalog import catalogue_for, excludes_holiday
from .llm import BuildModel
from .roster import build_roster

_LOGGER = logging.getLogger(__name__)

BATCH_DIRNAME = "building"


def _batch_dir(hass: HomeAssistant, slug: str) -> str:
    return os.path.join(hass.config.path(".storage"), "ai_jukebox", BATCH_DIRNAME, slug)


def _batch_path(hass: HomeAssistant, slug: str, index: int) -> str:
    return os.path.join(_batch_dir(hass, slug), f"batch_{index:03d}.json")


def _save_batch(hass: HomeAssistant, slug: str, index: int, payload: list) -> None:
    os.makedirs(_batch_dir(hass, slug), exist_ok=True)
    path = _batch_path(hass, slug, index)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def _drop_batch(hass: HomeAssistant, slug: str, index: int) -> None:
    try:
        os.remove(_batch_path(hass, slug, index))
    except FileNotFoundError:
        pass


def _clear_batches(hass: HomeAssistant, slug: str) -> None:
    d = _batch_dir(hass, slug)
    if not os.path.isdir(d):
        return
    for name in os.listdir(d):
        try:
            os.remove(os.path.join(d, name))
        except OSError:
            pass


def _model_prompt(text: str | None) -> str:
    """The playlist prompt as the model should see it.

    Now just a normaliser, and kept as one deliberately: it is the single place
    that answers "what description did the model actually get", which is worth
    having even when the answer is "all of it".

    It used to strip a `{artists}` placeholder that nothing substituted, two
    ways -- cutting the text at the literal string "Be sure to consider", and
    dropping any remaining line containing the placeholder. Both are gone as of
    2026-09-28, with the placeholder itself removed from all 28 prompts and seed
    artists reaching the model as structured data (roster.py) instead.

    The magic-string cut was the dangerous half: anything written after that
    phrase was silently discarded, so appending a rule to a prompt -- the
    natural place to put one -- deleted it. Playlists wording the sentence
    differently hit the line-drop instead and lost a whole instruction, which is
    how Powerhouse Vocal Precision lost "every pick must belong in their
    company". See docs/design.md §2.
    """
    return (text or "").strip()


# Structured policy fields, rendered into the instruction text the model sees.
# Deliberately NOT a metadata filter: docs/design.md §6 rejected hard year
# filtering because MA's own metadata is wrong (it reports reissue years), and
# its genre tags are no better. The model judges, as everywhere else in §1 --
# these fields just stop that judgement being expressed as prose buried in the
# description. `artists` is absent on purpose: roster.py states the seed list
# itself, and giving seed names to the album and track stages too would be a
# behaviour change worth measuring separately.
POLICY_FIELDS: Final = (
    ("exclude_artists", "Never include these artists: {}."),
    ("genres", "Stay within these genres: {}."),
    ("exclude_genres", "Never include these genres: {}."),
)


def _policy_lines(config: dict) -> list[str]:
    """Render the playlist's structured policy fields, skipping empty ones."""
    lines: list[str] = []
    for key, template in POLICY_FIELDS:
        values = [str(v).strip() for v in (config.get(key) or []) if str(v).strip()]
        if values:
            lines.append(template.format(", ".join(values)))
    return lines


def build_instructions(config: dict) -> str:
    """Description plus policy: what every build stage should be told.

    Public because media_player publishes it as an entity attribute -- the one
    place to read what a playlist actually tells the model, which the stored
    `prompt` alone no longer answers now that policy lives in fields.

    One composition, passed as the existing `prompt` argument to the roster,
    album and track calls -- so a rule stated once applies at all three, which
    prose in the description could only do by accident of placement.
    """
    parts = [_model_prompt(config.get("prompt")), *_policy_lines(config)]
    return "\n\n".join(part for part in parts if part)


class PoolBuilder:
    """Builds one playlist's pool. One instance per run."""

    def __init__(self, hass: HomeAssistant, store, playlist_name: str,
                 entities: tuple[str, ...] | None = None) -> None:
        self._hass = hass
        self._store = store
        self._name = playlist_name
        self._slug = store.playlist_slug(playlist_name)
        self._model = BuildModel(hass, entities)
        self.stats = {"artists": 0, "resolved": 0, "albums": 0, "tracks": 0,
                      "batches": 0, "failed_batches": 0, "failed_artists": []}

    @property
    def calls(self) -> int:
        return self._model.calls

    async def run(self, progress=None, resume: bool = False) -> dict:
        """Build this playlist's pool.

        `resume=False` replaces the pool outright. `resume=True` keeps what is
        already there and skips artists already represented -- used when a
        build died mid-flight (HA restart, unhandled failure) and left
        `building: true`. The interrupted batch is redone from the start,
        since a batch is only removed from the pending set once its tracks
        have landed.
        """
        config = self._store.get_playlist(self._name) or {}
        prompt = build_instructions(config)
        seeds = [s for s in (config.get("artists") or []) if s]

        def say(msg: str) -> None:
            _LOGGER.info("[%s] %s", self._name, msg)
            if progress:
                progress(msg)

        say(f"roster: {len(seeds)} seed artist(s)")
        roster = await build_roster(self._model, prompt, seeds)
        if not roster:
            raise RuntimeError(f"Roster generation returned nothing for '{self._name}'")

        # exclude_artists is enforced here as well as stated in the prompt.
        # Artist identity has a ground-truth check behind it (§1: the name
        # resolves to an Apple id), so a hard drop is legitimate -- unlike
        # exclude_genres, which has nothing to check against and stays advice.
        denied = {a.strip().lower() for a in (config.get("exclude_artists") or []) if str(a).strip()}
        if denied:
            kept_roster = [r for r in roster
                           if (r.get("artist") or "").strip().lower() not in denied]
            dropped = len(roster) - len(kept_roster)
            if dropped:
                say(f"roster: dropped {dropped} excluded artist(s)")
            roster = kept_roster
            if not roster:
                raise RuntimeError(
                    f"Every rostered artist for '{self._name}' is in exclude_artists")

        self.stats["artists"] = len(roster)
        say(f"roster: {len(roster)} artists")

        done_artists: set[str] = set()
        if resume:
            existing = await self._store.async_get_pool(self._name)
            done_artists = {t.get("artist") for t in existing.get("tracks") or []}
            say(f"resuming: {len(existing.get('tracks') or [])} tracks already held "
                f"across {len(done_artists)} artists")
        else:
            # Full replace. Playback keeps working throughout: the pool is
            # marked building, emptied, then refilled a batch at a time.
            await self._store.async_begin_pool_build(self._name)
            await self._hass.async_add_executor_job(_clear_batches, self._hass, self._slug)
            cutoff = (dt_util.utcnow() - timedelta(hours=PLAYS_RETENTION_HOURS)).isoformat()
            await self._store.async_prune_plays(self._name, cutoff)

        # The playlist's own title filters. Holiday has no toggle of its own:
        # it follows exclude_genres (catalog.excludes_holiday).
        filters = {
            "exclude_live": config.get("exclude_live", True),
            "exclude_remix": config.get("exclude_remix", True),
            "exclude_holiday": excludes_holiday(config.get("exclude_genres")),
        }
        catalogues: dict[str, list[dict]] = {}
        for entry in roster:
            albums = await catalogue_for(self._hass, entry["artist"], entry.get("album"),
                                         **filters)
            if albums:
                catalogues[entry["artist"]] = albums
                self.stats["resolved"] += 1
        say(f"catalogues: {self.stats['resolved']}/{len(roster)} artists resolved, "
            f"{sum(len(v) for v in catalogues.values())} candidate albums")
        if not catalogues:
            raise RuntimeError(f"No artist resolved for '{self._name}'")

        if done_artists:
            skipped = [a for a in catalogues if a in done_artists]
            for a in skipped:
                catalogues.pop(a, None)
            say(f"skipping {len(skipped)} artist(s) already in the pool")
            if not catalogues:
                await self._store.async_finish_pool_build(self._name)
                return self.stats

        # A resume replays the plan the interrupted build wrote rather than
        # re-deriving batches from a fresh roster. The plan carries each
        # artist's reference album, so a batch is reconstructed exactly as it
        # was proposed. Before this, a resume regenerated the roster and
        # re-attempted a lost artist only if the new roster happened to name it
        # again -- how The Orchestral Arena lost 18 artists on 2026-09-28.
        # Roster drift (§3.1) is wanted across successive rebuilds, not between
        # an interrupted build and its own resume.
        planned: list[dict] = []
        if resume:
            pool_now = await self._store.async_get_pool(self._name)
            planned = [b for b in (pool_now.get("batch_plan") or [])
                       if not b.get("done")
                       and int(b.get("attempts") or 0) < BUILD_MAX_BATCH_ATTEMPTS]
        if planned:
            # The planned artists may not be in this run's roster at all, so
            # their catalogues are fetched from the plan's own reference albums.
            for entry in planned:
                for member in entry.get("artists") or []:
                    name = member.get("artist")
                    if not name or name in catalogues:
                        continue
                    albums = await catalogue_for(self._hass, name, member.get("album"),
                                                 **filters)
                    if albums:
                        catalogues[name] = albums
                        self.stats["resolved"] += 1
            say(f"replaying {len(planned)} unfinished batch(es) from the saved plan")
        else:
            seed_keys = {s.lower() for s in seeds}
            reference = {e["artist"]: e.get("album") for e in roster}
            planned = [
                {"index": i,
                 "artists": [{"artist": n, "album": reference.get(n)} for n in names],
                 "attempts": 0, "done": False}
                for i, names in enumerate(curate.batches(catalogues, seed_keys), 1)
            ]
            await self._store.async_save_batch_plan(self._name, planned)
            say(f"{len(planned)} batches")

        for entry in planned:
            index = int(entry.get("index") or 0)
            names = [m.get("artist") for m in (entry.get("artists") or [])
                     if m.get("artist")]
            # Retry in place, per design.md section 5. Re-running a batch is
            # safe: extend_pool dedupes on uri, _save_batch overwrites, and the
            # model calls carry no state. Without this a single transient
            # failure silently costs four artists -- and since batches() puts
            # one seed in every batch, exactly one of them is a seed.
            added = None
            for attempt in range(1, BUILD_BATCH_ATTEMPTS + 1):
                try:
                    added = await self._run_batch(index, names, catalogues, prompt)
                    break
                except Exception as err:  # noqa: BLE001 - one batch must not kill the build
                    if attempt >= BUILD_BATCH_ATTEMPTS:
                        self.stats["failed_batches"] += 1
                        self.stats["failed_artists"].extend(names)
                        await self._store.async_record_batch_outcome(
                            self._name, index, done=False)
                        _LOGGER.exception("[%s] batch %d failed after %d attempts: %s",
                                          self._name, index, attempt, err)
                        say(f"batch {index}/{len(planned)} FAILED after {attempt} "
                            f"attempts ({type(err).__name__}: {err}): "
                            f"{', '.join(names)}")
                        # The per-entity reasons, which the exception type alone
                        # does not carry: "timeout" reads very differently from
                        # a quota rejection when deciding whether the backoff
                        # (BUILD_BATCH_BACKOFF_SECONDS) is the problem.
                        for line in getattr(self._model, "last_errors", [])[:8]:
                            say(f"  model: {line}")
                        break
                    delay = BUILD_BATCH_BACKOFF_SECONDS[attempt - 1]
                    say(f"batch {index}/{len(planned)} attempt {attempt} failed "
                        f"({type(err).__name__}); retrying in {delay}s")
                    await asyncio.sleep(delay)
            if added is None:
                continue
            await self._store.async_record_batch_outcome(self._name, index, done=True)
            self.stats["batches"] += 1
            self.stats["tracks"] += added
            say(f"batch {index}/{len(planned)} ({', '.join(names)}): "
                f"+{added} tracks, {self.stats['tracks']} total")

        # Budget is per batch now, carried on the plan. Defer while ANY batch
        # still has attempts left; abandon only the ones that have spent all
        # BUILD_MAX_BATCH_ATTEMPTS, and record just those as lost.
        pool_after = await self._store.async_get_pool(self._name)
        plan_after = pool_after.get("batch_plan") or []
        pending = [b for b in plan_after
                   if not b.get("done")
                   and int(b.get("attempts") or 0) < BUILD_MAX_BATCH_ATTEMPTS]
        abandoned = [m.get("artist") for b in plan_after
                     if not b.get("done")
                     and int(b.get("attempts") or 0) >= BUILD_MAX_BATCH_ATTEMPTS
                     for m in (b.get("artists") or []) if m.get("artist")]
        failed = abandoned
        if pending:
            worst = max((int(b.get("attempts") or 0) for b in pending), default=0)
            await self._store.async_defer_pool_build(self._name, worst, abandoned)
            say(f"{len(pending)} batch(es) still have attempts left "
                f"(max {BUILD_MAX_BATCH_ATTEMPTS}); left open for the resume sweep")
            return self.stats
        if abandoned:
            say(f"{len(abandoned)} artist(s) lost after "
                f"{BUILD_MAX_BATCH_ATTEMPTS} attempts each: {', '.join(abandoned)}")

        if not failed:
            # A clean finish means no batch is still owed, so the persisted
            # listings can go. Only a *fresh* build cleared them before, so a
            # successful resume left the previous run's failures on disk
            # forever, making the on-disk set misleading to anyone inspecting
            # it. Kept when something was lost: the exact model input is what
            # you want when a batch keeps failing. (Which batch failed is read
            # from `batch_plan`, not from these files.)
            await self._hass.async_add_executor_job(
                _clear_batches, self._hass, self._slug
            )
        await self._store.async_finish_pool_build(self._name, failed=failed)
        return self.stats

    async def _run_batch(self, index: int, names: list[str],
                         catalogues: dict[str, list[dict]], prompt: str) -> int:
        group = [(n, catalogues[n]) for n in names if catalogues.get(n)]
        if not group:
            return 0

        # Read per-batch rather than relying on run() having stashed it:
        # _run_batch is also reachable on the resume path.
        batch_config = self._store.get_playlist(self._name) or {}
        exclude_live = batch_config.get("exclude_live", True)
        exclude_remix = batch_config.get("exclude_remix", True)
        exclude_holiday = excludes_holiday(batch_config.get("exclude_genres"))

        kept = await curate.select_albums(self._model, prompt, group,
                                          exclude_live=exclude_live,
                                          exclude_remix=exclude_remix)
        if kept is None:
            raise RuntimeError("album selection failed after all model entities")
        self.stats["albums"] += sum(len(v) for v in kept.values())

        with_tracks: list[tuple[dict, list[dict]]] = []
        for albums in kept.values():
            for album in albums:
                tracks = await curate.fetch_tracks(
                    self._hass, album, exclude_live, exclude_remix, exclude_holiday)
                if tracks:
                    with_tracks.append((album, tracks))
        if not with_tracks:
            return 0

        # Persisted before the pick call, removed after the tracks land.
        await self._hass.async_add_executor_job(
            _save_batch, self._hass, self._slug, index,
            [{"album": a, "tracks": [t.get("name") for t in ts]} for a, ts in with_tracks],
        )

        picked = await curate.pick_tracks(self._model, prompt, with_tracks)
        if picked is None:
            raise RuntimeError("track selection failed after all model entities")

        added = await self._store.async_extend_pool(self._name, picked)
        await self._hass.async_add_executor_job(
            _drop_batch, self._hass, self._slug, index
        )
        return added
