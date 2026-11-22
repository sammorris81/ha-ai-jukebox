"""Persistence for AI Jukebox.

Playlist *definitions* (name, prompt, batch_size, ...) are config and live in
an HA Store. Each playlist's live, unplayed-track queue is durable too, but
for a different reason: it's the session's queue-authority (there is no
coordinator object; see media_player.py's module docstring), so it's
write-through — updated on every enqueue/advance/refill, not
just on an explicit stop — collapsing what the old ai_playlist integration
kept as two separate concepts ("cache", written only on stop, and an
in-memory-only live queue) into one.

The same file also carries a `session` record (speakers/now_playing/
played_count, or None when idle) — distinct from the in-memory-only
`hass.data[DOMAIN]["_suspended"]` snapshot used for a same-process
integration reload. `session` is what survives an actual HA restart, read
back by AiJukeboxPlayer.async_added_to_hass() to decide whether to attempt
restart-resurrection (see media_player.py's _resume_from_restart()).

A third tier, the *pool* (`.storage/ai_jukebox/pool/<slug>.json`), is the
playlist's durable track set and the only source of playback tracks. It is
written by the builder (build/pipeline.py) and read by sampling; nothing else
writes it, so no locking is needed for its contents. A build **replaces** it
wholesale — begin_pool_build() empties `tracks` — so it is not an accumulation
of everything ever collected. It keeps full track dicts, uri included.

It is not a mirror of anything. An earlier design published each pool as a real
Music Assistant playlist and reconciled the two in an audit pass; that was
removed (docs/design.md §6) along with the publisher, the reconciliation and
MA's 500-row read cap. There is no audit pass, and no MA write anywhere in this
module.
"""
import asyncio
import json
import logging
import os
import re
from types import MappingProxyType
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_REFILL_THRESHOLD,
    POOL_STORAGE_VERSION,
    STORAGE_KEY_PLAYLISTS,
    STORAGE_VERSION,
    SUBENTRY_PLAYLIST,
)
from .track_processing import normalize_track, song_key

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry, ConfigSubentry

_LOGGER = logging.getLogger(__name__)


def _pool_key(artist: str, title: str) -> str:
    """Dedup key for pool membership.

    Artist+title, not uri: the same recording legitimately carries
    different uris depending on which provider resolved it and whether it
    has been pulled into MA's library (see ma_playlist.normalize_uri), so
    keying on uri would let the same track into the pool more than once.
    """
    return normalize_track(f"{artist} - {title}")


class PlaylistStore:
    """Playlist configs plus each playlist's write-through unplayed queue."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY_PLAYLISTS)
        self._playlists: dict[str, dict] = {}
        self._queue_dir = os.path.join(hass.config.path(".storage"), "ai_jukebox", "queue")
        self._queue_locks: dict[str, asyncio.Lock] = {}
        self._pool_dir = os.path.join(hass.config.path(".storage"), "ai_jukebox", "pool")
        self._pool_locks: dict[str, asyncio.Lock] = {}
        self._plays_dir = os.path.join(hass.config.path(".storage"), "ai_jukebox", "plays")
        self._plays_locks: dict[str, asyncio.Lock] = {}
        # Inside Home Assistant, playlist definitions are the config entry's
        # playlist subentries -- the same records the UI's Configure form
        # edits -- so there is one writer, not two. Without an entry (the dev
        # harness) they come from the legacy `ai_jukebox.playlists` store,
        # which is also what the one-time migration imports from.
        self._entry: "ConfigEntry | None" = None

    def attach_entry(self, entry: "ConfigEntry") -> None:
        """Serve and save playlist definitions through `entry`'s subentries."""
        self._entry = entry

    def _definitions(self) -> dict[str, dict]:
        if self._entry is None:
            return self._playlists
        return {
            sub.unique_id or self.playlist_slug(sub.data.get("name", sub.title)): dict(sub.data)
            for sub in self._entry.get_subentries_of_type(SUBENTRY_PLAYLIST)
        }

    def _subentry_for(self, slug: str) -> "ConfigSubentry | None":
        if self._entry is None:
            return None
        for sub in self._entry.get_subentries_of_type(SUBENTRY_PLAYLIST):
            if sub.unique_id == slug:
                return sub
        return None

    def legacy_playlists(self) -> dict[str, dict]:
        """The pre-subentry store's playlists, for the one-time migration."""
        return dict(self._playlists)

    def _queue_lock(self, slug: str) -> asyncio.Lock:
        lock = self._queue_locks.get(slug)
        if lock is None:
            lock = asyncio.Lock()
            self._queue_locks[slug] = lock
        return lock

    def _pool_lock(self, slug: str) -> asyncio.Lock:
        """Separate from _queue_lock on purpose — background pool growth
        must not block a live enqueue's write-through queue save.
        """
        lock = self._pool_locks.get(slug)
        if lock is None:
            lock = asyncio.Lock()
            self._pool_locks[slug] = lock
        return lock

    def _plays_lock(self, slug: str) -> asyncio.Lock:
        lock = self._plays_locks.get(slug)
        if lock is None:
            lock = asyncio.Lock()
            self._plays_locks[slug] = lock
        return lock

    async def async_load(self) -> None:
        """Load playlist configs from HA storage."""
        data = await self._store.async_load()
        self._playlists = data.get("playlists", {}) if data else {}

    async def _async_save(self) -> None:
        await self._store.async_save({"playlists": self._playlists})

    # --- Playlist config CRUD ---

    def get_playlist(self, name: str) -> dict | None:
        return self._definitions().get(self.playlist_slug(name))

    def get_all_playlists(self) -> dict[str, dict]:
        return dict(self._definitions())

    def get_playlists_by_tags(self, tags: list[str]) -> list[dict]:
        playlists = self._definitions()
        if not tags:
            return list(playlists.values())
        result = []
        for config in playlists.values():
            playlist_tags = [t.lower() for t in config.get("tags", [])]
            if all(t.lower() in playlist_tags for t in tags):
                result.append(config)
        return result

    async def async_save_playlist(self, name: str, config: dict) -> dict:
        """Create or update a playlist config. Returns the stored config.

        Raises ValueError if `name` slugifies to a slug already used by a
        different playlist.
        """
        slug = self.playlist_slug(name)
        existing = self._definitions().get(slug)
        if existing and existing.get("name") != name:
            raise ValueError(
                f"Playlist name '{name}' collides with existing playlist "
                f"'{existing.get('name')}' (both produce slug '{slug}'). "
                f"Pick a more distinct name."
            )
        # Merge over the prior record rather than rebuilding it. A playlist
        # carries keys this method never writes -- history_depth,
        # exclude_live/exclude_remix, pool_enabled, target_pool_size,
        # pool_handoff_min, pool_removal_mode -- and rebuilding from `config`
        # alone silently reset every one of them to a default.
        #
        # Updated 2026-09-30. Five of those keys (history_depth, pool_enabled,
        # target_pool_size, pool_handoff_min, pool_removal_mode) were read by
        # nothing and were deleted from every record on 2026-09-27. The other
        # two are live: exclude_live/exclude_remix have been read since
        # 2026-09-28 (build/pipeline.py passes them to curate.fetch_tracks),
        # so this merge now protects real configuration rather than inert
        # keys -- a partial save that reset them would change which tracks a
        # build keeps.
        # `config` now contains only the keys the caller actually supplied
        # (see async_handle_save_playlist), so absent keys fall through.
        prior = existing or {}
        stored = {**prior, "name": name, **config}
        stored.setdefault("prompt", "")
        stored.setdefault("batch_size", DEFAULT_BATCH_SIZE)
        stored.setdefault("refill_threshold", DEFAULT_REFILL_THRESHOLD)
        stored.setdefault("tags", [])
        stored.setdefault("artists", [])
        if self._entry is not None:
            # Imported here, not at module level: the dev harness imports
            # this module against a stubbed homeassistant package that has no
            # config_entries, and never reaches this branch.
            from homeassistant.config_entries import ConfigSubentry

            entries = self._hass.config_entries
            sub = self._subentry_for(slug)
            if sub is None:
                entries.async_add_subentry(self._entry, ConfigSubentry(
                    data=MappingProxyType(stored),
                    subentry_type=SUBENTRY_PLAYLIST,
                    title=name,
                    unique_id=slug,
                ))
            else:
                entries.async_update_subentry(self._entry, sub, data=stored, title=name)
            return stored
        self._playlists[slug] = stored
        await self._async_save()
        return stored

    async def async_delete_playlist(self, name: str) -> None:
        slug = self.playlist_slug(name)
        if self._entry is not None:
            sub = self._subentry_for(slug)
            if sub is not None:
                self._hass.config_entries.async_remove_subentry(
                    self._entry, sub.subentry_id)
            return
        self._playlists.pop(slug, None)
        await self._async_save()

    # --- Live queue + history (write-through, one file per playlist) ---

    def _queue_path(self, playlist_name: str) -> str:
        slug = self.playlist_slug(playlist_name)
        return os.path.join(self._queue_dir, f"{slug}.json")

    def _default_queue_data(self) -> dict:
        return {"unplayed": [], "session": None}

    def _prune_legacy_queue_keys(self, data: dict) -> dict:
        """Drop `history`/`history_sources` left in files written before plays
        moved to their own store. Nothing reads them; they are removed on the
        next write so the file stops carrying dead weight."""
        data.pop("history", None)
        data.pop("history_sources", None)
        return data

    def _load_queue_data(self, playlist_name: str) -> dict:
        path = self._queue_path(playlist_name)
        if not os.path.isfile(path):
            return self._default_queue_data()
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("unplayed", [])
            data.setdefault("history", [])
            data.setdefault("history_sources", [])
            data.setdefault("session", None)
            return self._prune_legacy_queue_keys(data)
        except (json.JSONDecodeError, OSError):
            return self._default_queue_data()

    def _save_queue_data(self, playlist_name: str, data: dict) -> None:
        os.makedirs(self._queue_dir, exist_ok=True)
        path = self._queue_path(playlist_name)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)

    def get_unplayed(self, playlist_name: str) -> list[str]:
        return self._load_queue_data(playlist_name)["unplayed"]

    def save_unplayed(self, playlist_name: str, tracks: list[str]) -> None:
        """Write-through save of the current remaining queue for a playlist."""
        data = self._load_queue_data(playlist_name)
        data["unplayed"] = tracks
        self._save_queue_data(playlist_name, data)

    def get_session(self, playlist_name: str) -> dict | None:
        return self._load_queue_data(playlist_name)["session"]

    def save_session(self, playlist_name: str, session: dict | None) -> None:
        """Write-through record of whether this playlist was actively
        playing (and on what speakers) when last touched — read back on HA
        startup to decide whether to attempt restart-resurrection. `None`
        means "not active", written on stop/reset so a restart never
        resurrects a session the user deliberately ended.
        """
        data = self._load_queue_data(playlist_name)
        data["session"] = session
        self._save_queue_data(playlist_name, data)

    # --- Pool (one file per playlist; this file IS the pool) ---
    #
    # No authority split and no mirror: the pool is local truth, written by the
    # builder and read by sampling. The MA-playlist publisher and its audit pass
    # were removed (docs/design.md §6).
    #
    # Entries are full track dicts keeping their `uri`, which is what playback
    # enqueues and what identity comparisons use everywhere. The queue tier is
    # the same shape -- it was flattened to "Artist - Title" strings once, and
    # _restore_queue() now discards anything that is not a dict.

    def _pool_path(self, playlist_name: str) -> str:
        slug = self.playlist_slug(playlist_name)
        return os.path.join(self._pool_dir, f"{slug}.json")

    def _default_pool_data(self) -> dict:
        return {
            "version": POOL_STORAGE_VERSION,
            # True while a rebuild is in flight. Playback reads it to draw
            # smaller refills (so each one sees a larger pool as batches
            # land), and the resume paths read it to find dead builds.
            "building": False,
            "built_at": None,
            "tracks": [],
        }

    def _load_pool_data(self, playlist_name: str) -> dict:
        path = self._pool_path(playlist_name)
        if not os.path.isfile(path):
            return self._default_pool_data()
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            _LOGGER.warning(
                "Pool file for '%s' is unreadable; treating as empty", playlist_name
            )
            return self._default_pool_data()
        defaults = self._default_pool_data()
        for key, value in defaults.items():
            data.setdefault(key, value)
        return data

    def _save_pool_data(self, playlist_name: str, data: dict) -> None:
        os.makedirs(self._pool_dir, exist_ok=True)
        path = self._pool_path(playlist_name)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)

    def get_pool(self, playlist_name: str) -> dict:
        return self._load_pool_data(playlist_name)

    def save_pool(self, playlist_name: str, data: dict) -> None:
        self._save_pool_data(playlist_name, data)

    def begin_pool_build(self, playlist_name: str) -> None:
        """Empty the pool and flag it building. A rebuild fully replaces."""
        data = self._load_pool_data(playlist_name)
        data["tracks"] = []
        data["building"] = True
        data["built_at"] = None
        # A fresh build starts the resume budget over. A resume deliberately
        # does not come through here, so its counter survives across attempts.
        data["resume_attempts"] = 0
        data["failed_artists"] = []
        data["batch_plan"] = []
        self._save_pool_data(playlist_name, data)

    def save_batch_plan(self, playlist_name: str, plan: list[dict]) -> None:
        """Persist the batch plan: which artists were grouped into which batch.

        Written once, when the batches are first computed, so a resume replays
        the build it interrupted rather than re-deriving one. Each entry carries
        the artist name AND the roster's reference album, which is what
        catalog.resolve_artist() needs for its album-first lookup -- so a resume
        needs no roster call at all, and cannot substitute different artists for
        the ones that were lost.
        """
        data = self._load_pool_data(playlist_name)
        data["batch_plan"] = list(plan)
        self._save_pool_data(playlist_name, data)

    def record_batch_outcome(self, playlist_name: str, index: int,
                             done: bool) -> None:
        """Mark one planned batch finished, or count another failed attempt."""
        data = self._load_pool_data(playlist_name)
        for entry in data.get("batch_plan") or []:
            if int(entry.get("index", -1)) != int(index):
                continue
            if done:
                entry["done"] = True
            else:
                entry["attempts"] = int(entry.get("attempts") or 0) + 1
            break
        self._save_pool_data(playlist_name, data)

    def record_build_log(self, playlist_name: str, lines: list[str],
                         summary: str) -> None:
        """Persist the last run's progress narration onto the pool record."""
        data = self._load_pool_data(playlist_name)
        data["build_log"] = list(lines)
        data["build_summary"] = summary
        self._save_pool_data(playlist_name, data)

    def defer_pool_build(self, playlist_name: str, attempts: int,
                         failed: list[str]) -> None:
        """Leave `building` set so the resume sweep picks this build back up.

        Used when batches were lost: resume skips artists already in the pool,
        so it re-attempts exactly what went missing rather than rebuilding
        everything. `attempts` is the spent resume budget, carried on the pool
        record because each resume is a separate builder run with no memory of
        the last.
        """
        data = self._load_pool_data(playlist_name)
        data["building"] = True
        data["built_at"] = None
        data["resume_attempts"] = attempts
        data["failed_artists"] = failed
        self._save_pool_data(playlist_name, data)

    def finish_pool_build(self, playlist_name: str, at: str,
                          failed: list[str] | None = None) -> None:
        data = self._load_pool_data(playlist_name)
        data["building"] = False
        data["built_at"] = at
        if failed is not None:
            # Recorded even when empty, so a clean rebuild clears the previous
            # run's losses instead of leaving them to look current.
            data["failed_artists"] = failed
        self._save_pool_data(playlist_name, data)

    def extend_pool(self, playlist_name: str, tracks: list[dict]) -> int:
        """Append one batch. Returns how many were new.

        Deduped on uri, and on (artist, song): one recording of each song per
        artist. The builder only collapses repeats within one album listing,
        and artists with many Christmas albums, reissues and compilations
        brought the same song in two to four times (81 of 608 tracks on the
        2026-11-21 holidays build -- the Beach Boys' Little Saint Nick from
        three albums). Within a batch the model's best-ranked pick of a song
        is the one kept; across batches the first to land wins, which is
        rarely tested since an artist sits in exactly one batch.
        """
        if not tracks:
            return 0
        data = self._load_pool_data(playlist_name)
        have = {t.get("uri") for t in data["tracks"]}
        songs = {self._artist_song(t) for t in data["tracks"]}
        added = 0
        # sorted() is stable, so equal ranks keep the model's album order.
        for t in sorted(tracks, key=lambda t: t.get("rank") or 999):
            uri = t.get("uri")
            if not uri or uri in have:
                continue
            key = self._artist_song(t)
            if key[1] and key in songs:
                continue
            have.add(uri)
            songs.add(key)
            data["tracks"].append(t)
            added += 1
        self._save_pool_data(playlist_name, data)
        return added

    # --- Plays (`.storage/ai_jukebox/plays/<slug>.json`) ---
    #
    # What played and when, the sole input to sampling's recency decay and
    # artist/album weighting. Identity is the track uri plus the artist and
    # album ids the builder resolved; there are deliberately no display
    # fields, because nothing reads this for display.
    #
    # Not pruned in the background. A rebuild truncates it to the last
    # PLAYS_RETENTION_HOURS, which is the only span sampling reads, so the
    # file stays small without any scheduled maintenance. Entries whose uri
    # is absent from the new pool simply never match.

    def _plays_path(self, playlist_name: str) -> str:
        slug = self.playlist_slug(playlist_name)
        return os.path.join(self._plays_dir, f"{slug}.json")

    def _load_plays(self, playlist_name: str) -> list[dict]:
        path = self._plays_path(playlist_name)
        if not os.path.isfile(path):
            return []
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            _LOGGER.warning(
                "Plays file for '%s' is unreadable; treating as empty", playlist_name
            )
            return []
        return data if isinstance(data, list) else []

    def _save_plays(self, playlist_name: str, data: list[dict]) -> None:
        os.makedirs(self._plays_dir, exist_ok=True)
        path = self._plays_path(playlist_name)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp_path, path)

    def get_plays(self, playlist_name: str) -> list[dict]:
        return self._load_plays(playlist_name)

    def record_play(self, playlist_name: str, track: dict, at: str) -> None:
        """Append one play. `track` is a pool record (or anything carrying
        uri/artist_id/album_id); a track with no uri is not recorded, since
        sampling could never match it back."""
        uri = track.get("uri")
        if not uri:
            return
        data = self._load_plays(playlist_name)
        data.append({
            "uri": uri,
            "artist_id": track.get("artist_id"),
            "album_id": track.get("album_id"),
            "at": at,
        })
        self._save_plays(playlist_name, data)

    def prune_plays(self, playlist_name: str, cutoff_iso: str) -> int:
        """Drop plays older than `cutoff_iso`. Returns how many remain."""
        data = [p for p in self._load_plays(playlist_name)
                if (p.get("at") or "") >= cutoff_iso]
        self._save_plays(playlist_name, data)
        return len(data)

    def clear_plays(self, playlist_name: str) -> None:
        self._save_plays(playlist_name, [])

    # --- Async wrappers (lock-protected, executor-dispatched) ---
    #
    # One file holds both `unplayed` and `history`; without serialization,
    # concurrent history-add / unplayed-save calls could interleave and lose
    # data. Use these from the coordinator/entity; the sync methods above
    # exist mainly for tests.

    async def async_get_unplayed(self, playlist_name: str) -> list[str]:
        async with self._queue_lock(self.playlist_slug(playlist_name)):
            return await self._hass.async_add_executor_job(
                self.get_unplayed, playlist_name
            )

    async def async_save_unplayed(self, playlist_name: str, tracks: list[str]) -> None:
        async with self._queue_lock(self.playlist_slug(playlist_name)):
            await self._hass.async_add_executor_job(
                self.save_unplayed, playlist_name, tracks
            )

    async def async_get_session(self, playlist_name: str) -> dict | None:
        async with self._queue_lock(self.playlist_slug(playlist_name)):
            return await self._hass.async_add_executor_job(
                self.get_session, playlist_name
            )

    async def async_save_session(self, playlist_name: str, session: dict | None) -> None:
        async with self._queue_lock(self.playlist_slug(playlist_name)):
            await self._hass.async_add_executor_job(
                self.save_session, playlist_name, session
            )

    # --- Helpers ---

    async def async_get_pool(self, playlist_name: str) -> dict:
        async with self._pool_lock(self.playlist_slug(playlist_name)):
            return await self._hass.async_add_executor_job(self.get_pool, playlist_name)

    async def async_begin_pool_build(self, playlist_name: str) -> None:
        async with self._pool_lock(self.playlist_slug(playlist_name)):
            await self._hass.async_add_executor_job(self.begin_pool_build, playlist_name)

    async def async_finish_pool_build(self, playlist_name: str, at: str | None = None,
                                      failed: list[str] | None = None) -> None:
        stamp = at or dt_util.utcnow().isoformat()
        async with self._pool_lock(self.playlist_slug(playlist_name)):
            await self._hass.async_add_executor_job(
                self.finish_pool_build, playlist_name, stamp, failed
            )

    async def async_save_batch_plan(self, playlist_name: str,
                                    plan: list[dict]) -> None:
        async with self._pool_lock(self.playlist_slug(playlist_name)):
            await self._hass.async_add_executor_job(
                self.save_batch_plan, playlist_name, plan
            )

    async def async_record_batch_outcome(self, playlist_name: str, index: int,
                                        done: bool) -> None:
        async with self._pool_lock(self.playlist_slug(playlist_name)):
            await self._hass.async_add_executor_job(
                self.record_batch_outcome, playlist_name, index, done
            )

    async def async_record_build_log(self, playlist_name: str, lines: list[str],
                                     summary: str) -> None:
        async with self._pool_lock(self.playlist_slug(playlist_name)):
            await self._hass.async_add_executor_job(
                self.record_build_log, playlist_name, lines, summary
            )

    async def async_defer_pool_build(self, playlist_name: str, attempts: int,
                                     failed: list[str]) -> None:
        async with self._pool_lock(self.playlist_slug(playlist_name)):
            await self._hass.async_add_executor_job(
                self.defer_pool_build, playlist_name, attempts, failed
            )

    async def async_extend_pool(self, playlist_name: str, tracks: list[dict]) -> int:
        async with self._pool_lock(self.playlist_slug(playlist_name)):
            return await self._hass.async_add_executor_job(
                self.extend_pool, playlist_name, tracks
            )

    async def async_get_plays(self, playlist_name: str) -> list[dict]:
        async with self._plays_lock(self.playlist_slug(playlist_name)):
            return await self._hass.async_add_executor_job(
                self.get_plays, playlist_name
            )

    async def async_record_play(self, playlist_name: str, track: dict, at: str) -> None:
        async with self._plays_lock(self.playlist_slug(playlist_name)):
            await self._hass.async_add_executor_job(
                self.record_play, playlist_name, track, at
            )

    async def async_prune_plays(self, playlist_name: str, cutoff_iso: str) -> int:
        async with self._plays_lock(self.playlist_slug(playlist_name)):
            return await self._hass.async_add_executor_job(
                self.prune_plays, playlist_name, cutoff_iso
            )

    async def async_clear_plays(self, playlist_name: str) -> None:
        async with self._plays_lock(self.playlist_slug(playlist_name)):
            await self._hass.async_add_executor_job(
                self.clear_plays, playlist_name
            )

    @staticmethod
    def _artist_song(track: dict) -> tuple[str, str]:
        artist = str(track.get("artist_id") or (track.get("artist") or "").lower())
        return artist, song_key(track.get("title"))

    @staticmethod
    def playlist_slug(name: str) -> str:
        """Convert a playlist name to a filename/entity_id-safe slug."""
        if not name:
            return ""
        slug = name.lower()
        slug = re.sub(r"[^\w\s-]", "", slug)
        slug = re.sub(r"\s+", "_", slug)
        slug = slug.strip("_")
        return slug if slug else "_unnamed"
