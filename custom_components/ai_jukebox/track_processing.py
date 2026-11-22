"""Pure track-parsing/normalization helpers. No Home Assistant dependencies.

Deliberately does NOT include the old ai_playlist integration's
tracks_match()/artist_enqueued() fuzzy-matching helpers — those existed to
support a reactive "did the user secretly change the track" heuristic that
this redesign drops in favor of trusting Music Assistant's own queue state.
An artists_match() helper was ported back in 2026-08-31 as a title-agnostic
fallback for queue-position advancement, and was removed again: matching in
_reconcile_now_playing is now uri-first with an exact title+artist fallback,
with no fuzzy step anywhere. Nothing here is fuzzy.
"""
import logging
import re
import unicodedata

_LOGGER = logging.getLogger(__name__)


def normalize_track(track: str | None) -> str:
    """Normalize a track string for dedup comparison against history."""
    if not track:
        return ""

    if "|" in track:
        track = track.split("|", 1)[0].strip()

    normalized = track.lower().replace(" ", " ")
    # Fold accented characters to their base form (e.g. "Céline" -> "celine")
    # -- an AI proposal routinely drops diacritics that Music Assistant's
    # canonical artist credit keeps, which otherwise fails every match check
    # below even though it's clearly the same artist.
    normalized = "".join(
        c for c in unicodedata.normalize("NFKD", normalized) if not unicodedata.combining(c)
    )
    normalized = re.sub(r"[\"'‘’“”]", "", normalized)
    normalized = re.sub(r"[‐‑‒–—−]", "-", normalized)
    normalized = re.sub(r"\s*-\s*", " - ", normalized)

    suffix_pattern = (
        r"\s*[\(\[](?:[^\)\]]*(?:version|acoustic|piano|rock|mix|remix|edit|"
        r"live|remaster|remastered|deluxe|single|original|overture|mono|stereo|"
        r"recording|session|performance|instrumental|vocal|acapella)[^\)\]]*|"
        r"(?:\d{4}|\+).*?)[\)\]]\s*$"
    )
    normalized = re.sub(suffix_pattern, "", normalized, flags=re.IGNORECASE)
    normalized = normalized.replace("&", "and")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


_TRAILING_GROUP = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")
_TRAILING_VERSION = re.compile(
    r"\s+-\s+[^-]*\b(?:remaster(?:ed)?|version|mono|stereo|single|edit|"
    r"recording|mix|from)\b[^-]*$",
    re.IGNORECASE,
)


def song_key(title: str | None) -> str:
    """The song a title names, independent of who recorded it or where.

    'White Christmas [From "Holiday Inn" Soundtrack]', 'White Christmas -
    Remastered 2004' and 'White Christmas' are one song; so are 'Santa Claus
    Is Comin' to Town' and '...Coming to Town'. Used to keep one recording
    per song per artist in a pool (store.extend_pool) and to stop the same
    song being drawn twice into one refill (media_player._sample_from_pool).
    Every trailing (...)/[...] group is stripped, not just one: these are
    edition notes, never part of a song's name. Dropped-g spellings are
    folded by turning every word ending "in" into "ing" on both sides, which
    is only ever compared against itself, so 'begin' -> 'beging' is harmless.
    """
    t = title or ""
    for _ in range(3):
        stripped = _TRAILING_GROUP.sub("", t)
        if stripped == t or not stripped.strip():
            break
        t = stripped
    t = _TRAILING_VERSION.sub("", t) or t
    t = normalize_track(t)
    t = re.sub(r"\b(\w+)in\b", r"\1ing", t)
    return re.sub(r"[^a-z0-9]", "", t)


def strip_album(track: str) -> tuple[str, str]:
    """Split a 'Artist - Title | Album' string into (track, album)."""
    if not track:
        return ("", "")
    if "|" in track:
        parts = track.split("|", 1)
        return (parts[0].strip(), parts[1].strip())
    return (track.strip(), "")


def split_track(track: str) -> tuple[str, str]:
    """Split a track string into (artist, title). Strips album first."""
    if not track:
        return ("", "")
    track_only, _ = strip_album(track)
    normalized = re.sub(r"[‐‑‒–—−]", "-", track_only)
    if re.search(r"\s-\s", normalized):
        parts = re.split(r"\s*-\s*", normalized, maxsplit=1)
        return (parts[0].strip(), parts[1].strip() if len(parts) > 1 else "")
    return (track_only.strip(), "")


def track_dict_to_string(track: dict) -> str:
    """Convert a track dict to 'Artist - Title | Album' string format."""
    artist = (track.get("artist") or "").strip()
    title = (track.get("title") or "").strip()
    album = (track.get("album") or "").strip()
    base = f"{artist} - {title}"
    return f"{base} | {album}" if album else base


# Matches "live" anywhere inside a "(...)" qualifier, not just as the
# parenthetical's first word — e.g. "(FIP Session Live in Paris at the
# Pantheon)" is a live recording just as much as "(Live)" is, but the old
# `\(\s*live\b` anchor only caught the latter.
_LIVE_PATTERN = re.compile(r"\([^)]*\blive\b[^)]*\)", re.IGNORECASE)

# Matches a "(...)" qualifier naming an alternate mix/edit — e.g. "(Radio
# Edit)", "(Club Mix)", or a remixer credit like "(Bermudez & Griffin
# Radio)". Deliberately doesn't touch covers (a different performing artist
# entirely, not a version tag) or arrangement tags like "(Acoustic)"/"(Piano
# Version)" that aren't remixes.
#
# The inflected forms matter: a bare `\bmix\b` misses "(Mixed)" outright,
# since the word boundary lands straight after "mix" and "ed" follows — which
# is how a DJ mix reached the queue with exclude_remix already switched on.
_REMIX_PATTERN = re.compile(
    r"\([^)]*\b(?:"
    r"(?:re)?mix(?:ed|es)?|rmx|edit(?:s|ed)?|radio|club|extended|bootleg|rework|mashup|dub"
    # Qualifiers that only ever precede "version" on a non-standard cut. Bare
    # "version" stays unmatched, so arrangement tags like "(Piano Version)"
    # and the standard "(Album Version)"/"(Original Version)" still pass.
    r"|(?:long|short|single|alternate|alternative|highlights|epic|dance|instrumental)\s+version"
    r")\b[^)]*\)"
    # A *quoted* qualifier before "version" names an alternate edition (e.g.
    # Shania Twain's Red/Green/Blue cuts of Up!). Unquoted arrangement tags
    # are untouched, since those are never quoted.
    r"|\([^)]*[\"'][^\"']+[\"']\s+version\b[^)]*\)",
    re.IGNORECASE,
)


def _wrap(text: str | None) -> str | None:
    """Wrap a bare version-metadata string (e.g. Music Assistant's
    candidate["version"], which has no parens of its own) in "(...)" so the
    same paren-qualifier regexes apply to it as to a full track title.
    """
    if not text or not text.strip():
        return None
    return text if text.strip().startswith("(") else f"({text})"


def is_live(text: str | None, *, wrap: bool = True) -> bool:
    """True if `text` — a title, or a bare version-metadata string — marks
    a live recording, e.g. "(Live)" or "(FIP Session Live in Paris)".
    Exclusion is playlist-specific (the `exclude_live` config flag); this
    only answers the yes/no question, callers decide whether it matters.

    Pass `wrap=False` for free-form prose like an album title, where only a
    real "(...)" qualifier should count — see the note in `filter_tracks`.
    """
    candidate = _wrap(text) if wrap else text
    return bool(candidate and _LIVE_PATTERN.search(candidate))


def is_remix(text: str | None, *, wrap: bool = True) -> bool:
    """True if `text` — a title, or a bare version-metadata string — marks
    an alternate mix/remix/edit, e.g. "(Radio Edit)" or a remixer credit
    like "(Bermudez & Griffin Radio)". Doesn't flag covers (a different
    performing artist, not a version tag) or arrangement tags like
    "(Acoustic)"/"(Piano Version)". Exclusion is playlist-specific (the
    `exclude_remix` config flag); this only answers the yes/no question.

    Pass `wrap=False` for free-form prose like an album title, where only a
    real "(...)" qualifier should count — see the note in `filter_tracks`.
    """
    candidate = _wrap(text) if wrap else text
    return bool(candidate and _REMIX_PATTERN.search(candidate))

