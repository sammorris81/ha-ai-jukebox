"""Resolve an artist to their Apple Music identity, then fetch their albums.

Resolution is two steps, cheapest first:

  1. Search by name and take a single exact match.
  2. Otherwise *album-first*: search "<artist> <album>" restricted to Apple,
     match the reference album, then read the artist's real identity off the
     full album record.

Album-first exists because artist-name lookup fails for a whole class of
mismatches that no string normalisation can fix -- stylisation (Pink ->
P!nk), band renames (Mandolin Orange -> Watchhouse), legal name changes
(Terence Trent D'Arby -> Sananda Maitreya), plus ordinary accent and
'&'/'and' variants. Before it existed, ~40 lookups per build resolved to
nothing and were silently dropped.

Two details that look arbitrary and are not:

  * The album search must include the artist name. Searching the album title
    alone matched Wayne Shorter's 'Juju' for Siouxsie and the Banshees and
    GRiZ's 'Sunshine' for S Club 7, injecting entire wrong catalogues.
  * music/albums/get is required after the search. The artist stub on a
    search hit is degraded: its item_id is the *name* ("Beyonce") with no
    provider_mappings. Only the full album record carries the numeric id.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time

from homeassistant.core import HomeAssistant

from ..ma_playlist import MAPlaylistError, async_ma_call
from ..track_processing import is_live, is_remix, normalize_track

_LOGGER = logging.getLogger(__name__)

APPLE_DOMAIN = "apple_music"
KEEP_TYPES = {"album", "compilation", "soundtrack"}

# Album titles that are never worth carrying into a pool, whatever the
# playlist. Live/remix are handled separately by track_processing.
# Split into parts on 2026-09-27 so a playlist's exclude_live/exclude_remix
# can gate the live and remix halves -- but until 2026-11-21 _filter_albums
# still applied the union, so the two flags only ever reached track names.
# The holiday half was split out the same day: it was unconditional, which
# emptied the one playlist that wants Christmas music of nearly every
# Christmas album and song. It is now per playlist, on when exclude_genres
# names christmas/xmas/holiday(s) -- see excludes_holiday().
#
# What the catalogue cache stores is filtered by _BAD_ALWAYS only; the three
# per-playlist halves run on the way out (filter_for_playlist), so one cached
# catalogue serves every playlist's settings.
_BAD_LIVE = r"\b(live|unplugged|in concert|setlist|on tour|world tour|itunes festival|bbc|mtv)\b"
_BAD_REMIX = r"\b(remix(?:e[sd])?|mixes)\b"
_BAD_HOLIDAY = r"\b(christmas|xmas|no[eë]l|holidays?|yule|santa|merry|winter wonderland)\b"
# Never optional: a karaoke, tribute, instrumental, demo or commentary
# release is not a taste any flag expresses -- it is not the recording
# anyone asked for.
_BAD_ALWAYS = r"\b(karaoke|tribute|instrumentals?|demos?|commentary)\b"

BAD_ALBUM_LIVE = re.compile(_BAD_LIVE, re.I)
BAD_ALBUM_REMIX = re.compile(_BAD_REMIX, re.I)
BAD_ALBUM_HOLIDAY = re.compile(_BAD_HOLIDAY, re.I)
BAD_ALBUM_ALWAYS = re.compile(_BAD_ALWAYS, re.I)

# An exclude_genres entry that turns the holiday title filter on.
_HOLIDAY_GENRE = re.compile(r"\b(christmas|xmas|holidays?)\b", re.I)


def excludes_holiday(exclude_genres) -> bool:
    """Does this playlist's exclude_genres keep holiday music out?

    Option C of the 2026-11-21 review: no separate toggle. A playlist that
    lists christmas/xmas/holiday(s) in exclude_genres gets the title filter;
    one that does not is allowed holiday albums and songs. The add-playlist
    form pre-fills "christmas, holiday" so a new playlist keeps the old
    behaviour unless that entry is deleted.
    """
    return any(_HOLIDAY_GENRE.search(str(g)) for g in (exclude_genres or []))


def filter_for_playlist(albums: list[dict], *, exclude_live: bool = True,
                        exclude_remix: bool = True,
                        exclude_holiday: bool = True) -> list[dict]:
    """A cached (base-filtered) catalogue -> what one playlist may see."""
    out = []
    for a in albums:
        title = a.get("title") or ""
        if exclude_holiday and BAD_ALBUM_HOLIDAY.search(title):
            continue
        if exclude_live and (BAD_ALBUM_LIVE.search(title) or is_live(title)):
            continue
        if exclude_remix and (BAD_ALBUM_REMIX.search(title) or is_remix(title)):
            continue
        out.append(a)
    return out


# Catalogue cache. Keyed by Apple artist id, never by name: the key is then
# immune to the spelling problem (Pink and P!nk share one entry) and the TTL
# tracks something real -- whether that artist has released anything new.
CACHE_DIRNAME = "catalog"
# Schema 2 (2026-11-21): albums filtered by _BAD_ALWAYS only. An entry with
# no schema predates that and has the live, remix and holiday albums already
# stripped, so it is only good for a playlist that excludes all three.
CACHE_SCHEMA = 2
ACTIVE_TTL_DAYS = 22
ACTIVE_JITTER_DAYS = 7
ACTIVE_WINDOW_YEARS = 10
LEGACY_TTL_DAYS = 90
LEGACY_JITTER_DAYS = 30


def _cache_dir(hass: HomeAssistant) -> str:
    return os.path.join(hass.config.path(".storage"), "ai_jukebox", CACHE_DIRNAME)


def _cache_path(hass: HomeAssistant, artist_id: str) -> str:
    safe = re.sub(r"[^\w.-]", "_", str(artist_id))[:80]
    return os.path.join(_cache_dir(hass), f"{safe}.json")


def _expiry_for(albums: list[dict]) -> float:
    """Recently-active artists expire sooner than legacy ones.

    Artists with no year data at all are treated as legacy. That is a guess:
    missing metadata usually means an older catalogue, but thin tagging on a
    modern act would hide a new release for months.
    """
    years = [a.get("year") for a in albums if a.get("year")]
    now_year = time.gmtime().tm_year
    active = bool(years) and (now_year - max(years)) <= ACTIVE_WINDOW_YEARS
    base, jitter = (
        (ACTIVE_TTL_DAYS, ACTIVE_JITTER_DAYS) if active else (LEGACY_TTL_DAYS, LEGACY_JITTER_DAYS)
    )
    # Jittered so a whole build's entries do not all expire on the same day.
    days = base + random.uniform(-jitter, jitter)
    return time.time() + days * 86400


def _cache_read(hass: HomeAssistant, artist_id: str,
                strict: bool = False) -> list[dict] | None:
    """Cached albums, or None on a miss. `strict` rejects a pre-schema-2
    entry, whose albums are missing whatever the old filters removed."""
    path = _cache_path(hass, artist_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            entry = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    albums = entry.get("albums")
    if not isinstance(albums, list):
        return None
    if entry.get("expires_at", 0) < time.time():
        return None
    if strict and entry.get("schema") != CACHE_SCHEMA:
        return None
    return albums


def _cache_write(hass: HomeAssistant, artist_id: str, albums: list[dict]) -> None:
    os.makedirs(_cache_dir(hass), exist_ok=True)
    path = _cache_path(hass, artist_id)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"artist_id": artist_id, "schema": CACHE_SCHEMA,
                   "expires_at": _expiry_for(albums),
                   "albums": albums}, f, ensure_ascii=False)
    os.replace(tmp, path)


def _apple_mapping(item: dict) -> dict | None:
    for m in item.get("provider_mappings") or []:
        if m.get("provider_domain") == APPLE_DOMAIN:
            return m
    return None


async def _search(hass: HomeAssistant, query: str, media_type: str, limit: int = 10) -> list[dict]:
    try:
        res = await async_ma_call(
            hass, "music/search",
            {"search_query": query, "media_types": [media_type], "limit": limit},
        )
    except MAPlaylistError as err:
        _LOGGER.debug("MA search failed for %r: %s", query, err)
        return []
    if isinstance(res, dict):
        return res.get(f"{media_type}s") or []
    return res or []


async def _resolve_via_album(hass: HomeAssistant, name: str, ref_album: str) -> dict | None:
    """Find the artist by searching for one of their albums."""
    albums = await _search(hass, f"{name} {ref_album}", "album", limit=5)
    apple = [a for a in albums if str(a.get("provider", "")).startswith(APPLE_DOMAIN)]
    if not apple:
        return None

    want = normalize_track(ref_album)
    exact = [a for a in apple if normalize_track(a.get("name") or "") == want]
    partial = [
        a for a in apple
        if want in normalize_track(a.get("name") or "")
        or normalize_track(a.get("name") or "") in want
    ]
    # Exact title match is too strict to rely on: Apple titles these
    # 'M!ssundaztood' and 'Introducing the Hardline According to Terence
    # Trent D'Arby'. The query already carried the artist name, which is what
    # makes falling back to the top hit safe.
    candidates = exact or partial or apple[:1]

    try:
        full = await async_ma_call(
            hass, "music/albums/get",
            {"item_id": candidates[0]["item_id"],
             "provider_instance_id_or_domain": candidates[0]["provider"]},
        )
    except MAPlaylistError:
        return None

    artists = (full or {}).get("artists") or []
    if not artists:
        return None
    mapping = _apple_mapping(artists[0])
    if not mapping:
        return None

    canon = artists[0].get("name") or ""
    how = "exact" if exact else ("partial" if partial else "top-hit")
    if not (set(normalize_track(name).split()) & set(normalize_track(canon).split())):
        # Not a rejection: Watchhouse and Sananda Maitreya share no tokens
        # with the name searched and are both correct. Surfaced for review.
        _LOGGER.info("album-first [%s, VERIFY - no name overlap]: %r via %r -> %r",
                     how, name, ref_album, canon)
    else:
        _LOGGER.debug("album-first [%s]: %r via %r -> %r", how, name, ref_album, canon)
    return mapping


def _has_album(albums: list[dict] | None, ref_album: str | None) -> bool:
    """Does this catalogue contain the roster's reference album?

    Tolerant in both directions: Music Assistant titles carry edition
    suffixes ("(Deluxe Edition)") while the model names the short form, so a
    containment match counts -- but only for titles long enough that
    containment means something, or "1" would match everything. Accepts both
    album shapes, since a raw MA hit keys the title as `name` and a
    filtered/cached entry as `title`.
    """
    want = normalize_track(ref_album or "")
    if not want:
        return False
    for a in albums or []:
        got = normalize_track(a.get("title") or a.get("name") or "")
        if not got:
            continue
        if got == want:
            return True
        if len(want) >= 5 and len(got) >= 5 and (want in got or got in want):
            return True
    return False


async def _fetch_albums(hass: HomeAssistant, mapping: dict) -> list[dict]:
    try:
        res = await async_ma_call(
            hass, "music/artists/artist_albums",
            {"item_id": mapping["item_id"],
             "provider_instance_id_or_domain": mapping["provider_instance"]},
        )
    except MAPlaylistError:
        return []
    return res or []


def _filter_albums(raw: list[dict], artist_name: str, artist_id: str) -> list[dict]:
    out: dict[str, dict] = {}
    # normalize_track() strips "(Live)"-style suffixes, so "X" and "X (Live)"
    # share a key. Before the per-playlist filters moved out of here the live
    # one was already gone; now it is still present, and whichever came first
    # would win the key -- and then be dropped by filter_for_playlist, losing
    # the studio album too. So the clean titles go first.
    def _flagged(a: dict) -> bool:
        t = a.get("name") or ""
        return bool(BAD_ALBUM_LIVE.search(t) or is_live(t) or BAD_ALBUM_REMIX.search(t)
                    or is_remix(t) or BAD_ALBUM_HOLIDAY.search(t))
    for a in sorted(raw, key=_flagged):
        if (a.get("album_type") or "").lower() not in KEEP_TYPES:
            continue
        title = a.get("name") or ""
        if BAD_ALBUM_ALWAYS.search(title):
            continue
        key = normalize_track(title)
        if not key or key in out:
            continue  # same album reissued or duplicated
        out[key] = {
            "artist": artist_name,
            "artist_id": artist_id,
            "title": title,
            "item_id": a.get("item_id"),
            "provider": a.get("provider"),
            "year": a.get("year"),
            "album_type": a.get("album_type"),
        }
    return list(out.values())


async def resolve_artist(hass: HomeAssistant, name: str, ref_album: str | None) -> dict | None:
    """Name -> the Apple provider mapping for that artist, or None."""
    mapping = None
    exact = [
        a for a in await _search(hass, name, "artist")
        if (a.get("name") or "").strip().lower() == name.strip().lower()
    ]
    for a in exact:
        mapping = _apple_mapping(a)
        if mapping:
            break
    if mapping is not None:
        return mapping
    if ref_album:
        return await _resolve_via_album(hass, name, ref_album)
    _LOGGER.warning("Unresolved artist %r (no reference album to fall back on)", name)
    return None


async def catalogue_for(hass: HomeAssistant, name: str, ref_album: str | None, *,
                        exclude_live: bool = True, exclude_remix: bool = True,
                        exclude_holiday: bool = True) -> list[dict]:
    """Name -> that artist's album catalogue, filtered for one playlist's
    exclude_live / exclude_remix / exclude_holiday. [] if unresolvable.

    Resolution runs every time (1-2 cheap local searches); only the album
    catalogue is cached, keyed by the resolved artist id. The cache cannot be
    consulted earlier than this because the id is what resolution produces --
    and keying it on the name instead is exactly the fragility this design
    removes elsewhere.
    """
    mapping = await resolve_artist(hass, name, ref_album)
    if mapping is None:
        _LOGGER.warning("Unresolved artist %r", name)
        return []

    # A pre-schema-2 cache entry already lacks live, remix and holiday
    # albums, so it only serves a playlist that excludes all three. Any other
    # playlist refetches, and its write upgrades the entry for everyone.
    strict = not (exclude_live and exclude_remix and exclude_holiday)

    def _view(albums: list[dict]) -> list[dict]:
        return filter_for_playlist(albums, exclude_live=exclude_live,
                                   exclude_remix=exclude_remix,
                                   exclude_holiday=exclude_holiday)

    async def _albums_for(m: dict) -> tuple[str, list[dict], bool]:
        """(artist_id, base-filtered albums, came_from_cache) for one mapping."""
        aid = str(m["item_id"])
        hit = await hass.async_add_executor_job(_cache_read, hass, aid, strict)
        if hit is not None:
            # Re-stamp with the name this playlist used, so the pool shows the
            # roster's spelling rather than whichever playlist filled the cache.
            return aid, [dict(a, artist=name) for a in hit], True
        return aid, _filter_albums(await _fetch_albums(hass, m), name, aid), False

    artist_id, base, cached = await _albums_for(mapping)
    albums = _view(base)

    # Finding a mapping is not proof it was the right one, and it is wrong in
    # two different ways. A search hit's provider_mapping sometimes carries
    # the NAME as item_id ("Halsey") and artist_albums returns nothing at all.
    # Worse, an exact name match can land on a real but *wrong* Apple entity:
    # "Florence Welch" is a covers act, not Florence + The Machine, and it
    # returns a small, entirely plausible catalogue of somebody else's work,
    # which then supplies tracks under the right artist's name.
    #
    # Both are the same question -- did the artist we landed on actually
    # release the reference album the roster named to identify them? Only the
    # empty case used to be asked, so the wrong-entity case sailed through.
    # The check runs against the cache too: a wrong id, once cached, would
    # otherwise be trusted forever.
    if ref_album and not _has_album(albums, ref_album):
        retry = await _resolve_via_album(hass, name, ref_album)
        if retry and str(retry["item_id"]) != artist_id:
            alt_id, alt_base, alt_cached = await _albums_for(retry)
            alt_albums = _view(alt_base)
            # Switch only if the alternative is genuinely better. A reference
            # album can be legitimately missing from artist_albums (regional
            # gaps, compilation-only releases, or the filters above dropping
            # it), and that must never downgrade a correct resolution.
            if _has_album(alt_albums, ref_album) or not albums:
                _LOGGER.info(
                    "Re-resolved %r from item_id=%s to %s via reference album %r",
                    name, artist_id, alt_id, ref_album)
                artist_id, base, albums, cached = alt_id, alt_base, alt_albums, alt_cached
            else:
                _LOGGER.debug(
                    "Kept %r at item_id=%s; reference album %r found in neither "
                    "catalogue", name, artist_id, ref_album)

    # Cache the base catalogue even when this playlist's view of it is
    # empty: another playlist's settings may see something in it.
    if not cached and base:
        await hass.async_add_executor_job(_cache_write, hass, artist_id, base)

    if not albums:
        _LOGGER.warning("Artist %r resolved to item_id=%s but has no albums",
                        name, artist_id)
        return []
    return albums
