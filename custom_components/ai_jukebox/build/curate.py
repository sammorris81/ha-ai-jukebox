"""The two model judgements: which albums, then which tracks.

Kept as two passes on purpose. They ask different questions -- one about
coherent bodies of work, one about individual songs -- and merging them makes
the model go straight to tracks without ever forming a view about the album,
producing a thin scatter across many albums instead of deep cuts from the
right ones. Both are grouped by artist, so call count scales with roster size
rather than catalogue size, and merging would not have saved calls anyway.
"""
from __future__ import annotations

import collections
import logging
import re

from homeassistant.core import HomeAssistant

from ..const import (
    BUILD_ARTISTS_PER_CALL,
    BUILD_MAX_ALBUMS,
    BUILD_MIN_ALBUMS,
)
from ..ma_playlist import MAPlaylistError, async_ma_call
from ..track_processing import is_live, is_remix, normalize_track
from .catalog import (
    APPLE_DOMAIN,
    BAD_ALBUM_ALWAYS,
    BAD_ALBUM_HOLIDAY,
    BAD_ALBUM_LIVE,
    BAD_ALBUM_REMIX,
)
from .llm import BuildModel, as_ints, number_field, row_field, rows, text_field

_LOGGER = logging.getLogger(__name__)

_QUALIFIER = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")

# Matched against BOTH the track name and MA's `version` field. Presence of a
# version is NOT enough on its own: every track on Mellon Collie carries
# 'Remastered 2012', and treating that as variant-ness deleted the album.
_VARIANT = re.compile(
    r"\b(mono|instrumental|demos?|rehearsal|snippet|outtake|a\s?cappella|"
    r"alternate|alternative\s+(?:take|version|vocal)|early\s+version|"
    r"reimagin\w*|session|take\s*\d+|\d{4}\s*mix|rough\s*mix|work\s*tape|"
    r"digital\s+transfer|radio\s+edit|single\s+version)\b",
    re.I,
)

SELECT_STRUCTURE = {
    "kept": rows(
        "one entry per artist",
        {
            "artist": text_field("the artist's name, exactly as given"),
            "keep": text_field("the numbers of the albums to keep", multiple=True),
        },
    )
}

PICK_STRUCTURE = {
    "picks": rows(
        "one entry per album",
        {
            "album_id": number_field("the album's bracketed id"),
            "tracks": row_field(
                {
                    "n": text_field("the track's number in the listing"),
                    "why": text_field("why this track belongs, at most 10 words"),
                }
            ),
        },
    )
}

# A `why` longer than this is the model ignoring the instruction, not a
# better justification. Capped on ingest because the pool file is re-read
# from disk on every refill (see the player's _sample_from_pool), so an
# unbounded string is re-parsed for the life of the pool.
WHY_MAX_CHARS = 120


def _base_title(name: str) -> str:
    """Title with one trailing (qualifier) removed, normalised.

    'To Sheila (Mono)' and 'To Sheila' collapse together. Only the trailing
    group is stripped, so 'Death On Two Legs (Dedicated To...)' stays distinct.
    """
    return normalize_track(_QUALIFIER.sub("", name or "") or (name or ""))


def dedupe_album_tracks(tracks: list[dict], exclude_live: bool = True,
                        exclude_remix: bool = True,
                        exclude_holiday: bool = True) -> list[dict]:
    """Drop non-canonical takes from one album's listing.

    `exclude_live`/`exclude_remix` come from the playlist's own config and
    were dead keys until 2026-09-27 -- stored on every playlist, read by
    nothing, while this filter ran unconditionally. Defaults stay True so
    behaviour is unchanged for a playlist that never set them. The box-set
    and variant collapsing below is never optional: it exists to stop the
    same recording appearing four times, not to express a taste.

    Deluxe and box editions repeat the same song many times: Smashing
    Pumpkins' 'Adore' returns 90 tracks over 6 discs with 'To Sheila'
    appearing four times under four ids. Nothing downstream caught it, since
    the dedupe key is artist+title and the suffixed names read as distinct.

    Disc 1 is the original album on a box set, so lowest (disc, track) wins.
    A rule like "more than two discs means box set" would instead mangle
    genuine double albums -- Mellon Collie is exactly that.
    """
    out: dict[str, tuple] = {}
    for t in tracks:
        name = t.get("name") or ""
        version = (t.get("version") or "").strip()
        if version and (_VARIANT.search(version)
                        or (exclude_live and is_live(version))
                        or (exclude_remix and is_remix(version))):
            continue
        if (BAD_ALBUM_ALWAYS.search(name) or _VARIANT.search(name)
                or (exclude_holiday and BAD_ALBUM_HOLIDAY.search(name))
                or (exclude_live and (BAD_ALBUM_LIVE.search(name) or is_live(name)))
                or (exclude_remix and (BAD_ALBUM_REMIX.search(name) or is_remix(name)))):
            continue
        key = _base_title(name)
        if not key:
            continue
        rank = (t.get("disc_number") or 99, t.get("track_number") or 999)
        prev = out.get(key)
        if prev is None or rank < prev[0]:
            out[key] = (rank, t)
    return [t for _, t in sorted(out.values(), key=lambda rt: rt[0])]


def batches(roster_albums: dict[str, list[dict]], seeds: set[str]) -> list[list[str]]:
    """Group artists into calls, spreading seeds so early batches are a mix.

    A pool is playable seconds into a rebuild, so the first batches decide
    what the first minutes sound like. Sorting seeds to the front entirely
    would make that all-seeds; interleaving gives each batch at least one.
    """
    seeded = [a for a in roster_albums if a.lower() in seeds]
    rest = [a for a in roster_albums if a.lower() not in seeds]
    order: list[str] = []
    while seeded or rest:
        if seeded:
            order.append(seeded.pop(0))
        for _ in range(max(1, BUILD_ARTISTS_PER_CALL - 1)):
            if rest:
                order.append(rest.pop(0))
    return [order[i:i + BUILD_ARTISTS_PER_CALL]
            for i in range(0, len(order), BUILD_ARTISTS_PER_CALL)]


async def select_albums(
    model: BuildModel, prompt: str, group: list[tuple[str, list[dict]]], *,
    exclude_live: bool = True, exclude_remix: bool = True,
) -> dict[str, list[dict]] | None:
    """Which of each artist's albums belong. None if the call failed."""
    lines: list[str] = []
    numbered: dict[int, dict] = {}
    idx = 1
    for artist, albums in group:
        lines.append(f"\n## {artist}")
        for album in albums:
            lines.append(f"{idx}. {album['title']}")
            numbered[idx] = album
            idx += 1

    # The skip list follows the playlist's toggles: telling the model to skip
    # live albums on a playlist that allows them undid exclude_live in prose.
    skip = ["greatest-hits and 'best of' compilations"]
    if exclude_live:
        skip.append("live albums")
    skip.append("remix and demo collections" if exclude_remix else "demo collections")
    skip += ["soundtracks", "anniversary re-releases", "and EPs or singles"]

    instructions = (
        f"Playlist description: {prompt}\n\n"
        "Below are candidate albums grouped by artist. Each of these artists was "
        "chosen as a good fit for this playlist. For EACH artist, decide which of "
        "their albums best represent them on THIS playlist -- the ones whose music "
        "most matches the description and mood above. Rank that artist's own albums "
        "against each other; you are choosing the best of this artist, not judging "
        "them against an absolute bar.\n\n"
        "Rules per artist:\n"
        f"- Keep their strongest studio albums for this playlist: at least "
        f"{BUILD_MIN_ALBUMS}, and up to {BUILD_MAX_ALBUMS} if that many genuinely fit.\n"
        f"- Skip {', '.join(skip)}.\n"
        f"- If an artist has fewer than {BUILD_MIN_ALBUMS} eligible studio albums, keep "
        "whatever studio albums they do have.\n\n"
        "Return one entry per artist, giving the album NUMBERS you kept, not their "
        "titles.\n" + "\n".join(lines)
    )

    data = await model.ask(instructions, SELECT_STRUCTURE, "ai_jukebox_select_albums")
    if data is None:
        return None

    # Same accommodation as pick_tracks: the list has to be declared as text
    # (HA's number selector rejects `multiple`), which invites the model to
    # answer with album titles instead of numbers -- it does so roughly half
    # the time. Accept either: a number indexes the listing, a title is matched
    # back against the very strings we just sent, so the match is exact.
    by_title = {normalize_track(a["title"]): a for a in numbered.values()}
    kept: dict[str, list[dict]] = collections.defaultdict(list)
    for row in data.get("kept") or []:
        for token in row.get("keep") or []:
            album = None
            ints = as_ints([token])
            if ints:
                album = numbered.get(ints[0])
            if album is None:
                album = by_title.get(normalize_track(str(token)))
            if album is not None:
                kept[album["artist"]].append(album)
    for artist, _ in group:
        if not kept.get(artist):
            _LOGGER.info("Album selection kept nothing for %r", artist)
    return dict(kept)


def _apple_uri(track: dict) -> str | None:
    """The track's Apple Music uri, or None if it has no Apple route.

    Music Assistant merges library items over provider items: asking an Apple
    album for its tracks returns `library://` entries for anything also in the
    library, and no flag bypasses that (`in_library_only=False` and the exact
    provider instance both still return the library item). Those entries carry
    their real sources in provider_mappings, so the Apple id is recovered from
    there.

    Tracks with no available Apple mapping are dropped. One of them --
    Adele's "I'll Be Waiting", available only via ytmusic -- failed to enqueue
    with "There is nothing to play here." and ended a four-hour session. The
    whole design also assumes Apple throughout: speakers report
    `apple_music://` ids back, which is what reconciliation matches on.
    """
    for m in track.get("provider_mappings") or []:
        if m.get("provider_domain") == APPLE_DOMAIN and m.get("available"):
            return f"{APPLE_DOMAIN}://track/{m['item_id']}"
    uri = track.get("uri") or ""
    return uri if uri.startswith(f"{APPLE_DOMAIN}://") else None


async def fetch_tracks(hass: HomeAssistant, album: dict, exclude_live: bool = True,
                       exclude_remix: bool = True,
                       exclude_holiday: bool = True) -> list[dict]:
    """One album's usable track listing, each rewritten to its Apple uri."""
    try:
        raw = await async_ma_call(
            hass, "music/albums/album_tracks",
            {"item_id": album["item_id"],
             "provider_instance_id_or_domain": album["provider"]},
        )
    except MAPlaylistError as err:
        _LOGGER.debug("album_tracks failed for %r: %s", album.get("title"), err)
        return []

    out = []
    dropped = 0
    for t in dedupe_album_tracks(raw or [], exclude_live, exclude_remix, exclude_holiday):
        uri = _apple_uri(t)
        if uri is None:
            dropped += 1
            continue
        out.append({**t, "uri": uri})
    if dropped:
        _LOGGER.info("%s - %s: dropped %d track(s) with no Apple Music route",
                     album.get("artist"), album.get("title"), dropped)
    return out


async def pick_tracks(
    model: BuildModel, prompt: str, albums: list[tuple[dict, list[dict]]]
) -> list[dict] | None:
    """Choose tracks per album, in priority order. None if the call failed.

    Returns pool-shaped records. This replaces an earlier 'depth' rating that
    scored each album full/most/some/few and then took that fraction *at
    random* -- so the model would correctly identify a 40% album and then dice
    roll which 40%. Asked to rate Mellon Collie the depth pass said 'most'
    (~17 of 28 tracks); asked to pick tracks it took 4, precisely the
    orchestral ones on an album that is otherwise grunge.
    """
    lines: list[str] = []
    numbered: dict[int, tuple[dict, list[dict]]] = {}
    album_id = 1
    for album, tracks in albums:
        if len(tracks) < 2:
            continue
        numbered[album_id] = (album, tracks)
        lines.append(f"\n[album {album_id}] {album['artist']} - {album['title']}")
        for i, t in enumerate(tracks, 1):
            lines.append(f"   {i}. {t.get('name')}")
        album_id += 1
    if not numbered:
        return []

    instructions = (
        f"Playlist description: {prompt}\n\n"
        "Below are albums with their real track listings. For EACH album, choose the "
        "tracks that genuinely belong on THIS playlist, and return them in PRIORITY "
        "ORDER -- best fit for the playlist first, then next best.\n\n"
        "Judge each track on its own sound and mood against the description above. "
        "Some albums deserve nearly all their tracks, others only one or two. Return "
        "an empty list for an album where nothing fits.\n\n"
        "Give each track as its NUMBER from the listing, not its title, and with "
        "each one give `why` it belongs on THIS playlist in at most 10 words.\n"
        + "\n".join(lines)
    )

    data = await model.ask(instructions, PICK_STRUCTURE, "ai_jukebox_pick_tracks")
    if data is None:
        return None

    out: list[dict] = []
    picked_ids: set[int] = set()
    for row in data.get("picks") or []:
        try:
            album_key = int(row.get("album_id"))
        except (TypeError, ValueError):
            continue
        entry = numbered.get(album_key)
        if entry is None:
            continue
        album, tracks = entry
        # The structure has to declare this list as text (HA's number selector
        # rejects `multiple`), and a text list invites the model to answer with
        # titles instead of numbers -- which it does about half the time. Both
        # are accepted: a number indexes the listing, a title is matched back
        # against it exactly. Matching here is safe because the candidates are
        # the very strings we just sent.
        by_title = {normalize_track(t.get("name") or ""): i
                    for i, t in enumerate(tracks)}
        for rank, item in enumerate(row.get("tracks") or [], 1):
            # Nested rows give {"n": ..., "why": ...}; a bare token is still
            # accepted so a model answering in the older flat shape degrades
            # to no justification rather than losing the whole batch.
            if isinstance(item, dict):
                token = item.get("n")
                why = (item.get("why") or "").strip()[:WHY_MAX_CHARS]
            else:
                token, why = item, ""
            index = None
            ints = as_ints([token])
            if ints and 1 <= ints[0] <= len(tracks):
                index = ints[0] - 1
            else:
                index = by_title.get(normalize_track(str(token)))
            if index is None:
                continue
            t = tracks[index]
            if not t.get("uri"):
                continue
            out.append({
                "uri": t["uri"],
                "artist_id": album.get("artist_id"),
                "album_id": str(album.get("item_id")),
                "artist": album["artist"],
                "title": t.get("name"),
                "album": album["title"],
                "duration": t.get("duration") or 240,
                "rank": rank,
                "why": why,
            })
            picked_ids.add(album_key)

    # Rejection is invisible otherwise: an album the model declines returns an
    # empty list, which is indistinguishable from one it never saw. Log what was
    # offered against what survived so the selectivity this stage is supposed to
    # have can be checked against a real build (docs/implementation-notes.md
    # measured 2-5 empty albums per batch once a `why` was required; the
    # unjustified prompt produced 0-1). Album titles, capped, so a rejection can
    # actually be argued with rather than just counted.
    offered_tracks = sum(len(t) for _, t in numbered.values())
    empty = [f"{a['artist']} - {a['title']}"
             for key, (a, _) in numbered.items() if key not in picked_ids]
    _LOGGER.info(
        "pick_tracks: kept %d/%d track(s) across %d album(s); %d album(s) "
        "returned nothing%s",
        len(out), offered_tracks, len(numbered), len(empty),
        (" -- " + "; ".join(empty[:5]) + (" ..." if len(empty) > 5 else "")) if empty else "",
    )
    return out
