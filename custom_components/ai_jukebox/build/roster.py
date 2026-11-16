"""Produce a playlist's artist roster: seeds plus discovered artists.

One model call, not two stages. Seeds are *returned by the model*, not
concatenated afterwards, because a bare seed name has no reference album --
and the reference album is the only thing that can recover an artist whose
name does not resolve in Music Assistant (see catalog.resolve_artist).

The roster is regenerated on every rebuild and never cached. Reusing it
would mean matching artist names across runs, which is precisely the
operation that has failed repeatedly. Seeds are the stability guarantee; the
discovered artists are allowed to drift.
"""
from __future__ import annotations

import logging

from ..const import BUILD_ROSTER_TARGET
from .llm import BuildModel, rows, text_field

_LOGGER = logging.getLogger(__name__)

STRUCTURE = {
    "artists": rows(
        "one entry per artist",
        {
            "artist": text_field("the artist's name"),
            "album": text_field("one album where this artist is the primary credited artist"),
        },
    )
}


def _instructions(prompt: str, seeds: list[str], count: int) -> str:
    seed_list = ", ".join(seeds) if seeds else "(none yet)"
    return (
        f"Playlist description: {prompt}\n\n"
        f"These artists already define the playlist: {seed_list}.\n\n"
        f"Return TWO groups of artists in one list:\n"
        f"1. Every artist named above, repeated back exactly as written.\n"
        f"2. {count} additional artists who belong alongside them and are not "
        f"already listed.\n\n"
        f"Every artist must be real and well known. For each artist in BOTH groups, "
        f"give one well-known album by that exact artist, used to identify them "
        f"unambiguously. The album must be one where the artist is the PRIMARY "
        f"credited artist -- never a collaboration, guest appearance, or "
        f"various-artists compilation. Spell each artist's name exactly as it was "
        f"given to you above; do not correct or restyle it."
    )


async def build_roster(
    model: BuildModel, prompt: str, seeds: list[str], count: int = BUILD_ROSTER_TARGET
) -> list[dict]:
    """Returns [{artist, album}], seeds first, deduped on lowercased name."""
    data = await model.ask(
        _instructions(prompt, seeds, count), STRUCTURE, "ai_jukebox_roster"
    )
    returned = (data or {}).get("artists") or []

    merged: list[dict] = []
    seen: set[str] = set()
    for row in returned:
        name = (row.get("artist") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append({"artist": name, "album": (row.get("album") or "").strip() or None})

    # Safety net, not the main path: if the model dropped a seed despite being
    # asked to echo it, keep the artist rather than silently losing curation.
    # It arrives without a reference album, so album-first cannot rescue it.
    for seed in seeds:
        if seed.strip().lower() not in seen:
            _LOGGER.warning("Roster: model omitted seed artist %r; kept without a "
                            "reference album", seed)
            merged.append({"artist": seed, "album": None})

    return merged
