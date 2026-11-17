"""Music Assistant RPC transport for AI Jukebox.

Sends MA API commands for which no HA service exists. Preferred transport is
the websocket client Home Assistant's own `music_assistant` integration
already holds (`entry.runtime_data.mass.send_command`): it works however MA
is installed, because HA has to reach it anyway. MA as a Home Assistant app
is configured by discovery with an internal-only address, and an MA server
without login stores no token, and the HTTP path cannot rely on either.

Fallback, only when that client is not available (integration not loaded,
or the dev harness, which has no HA): MA's HTTP RPC API (POST {url}/api)
with the config entry's stored url+token. Both return the same JSON result.

This module issues no commands of its own: callers pass the command string,
so every MA command the integration sends lives at its call site. Today
those are four reads from the builder (`music/search`, `music/albums/get`,
`music/albums/album_tracks`, `music/artists/artist_albums`) and one write
from the runtime (`player_queues/dont_stop_the_music`, media_player.py).

Contents: MAPlaylistError, async_ma_call(), normalize_uri().

The name is historical. An earlier design stored pools as real MA playlists,
which is what "playlist RPC" referred to; pools now live in
`.storage/ai_jukebox/pool/<slug>.json` (see store.py) and nothing here
creates or writes an MA playlist.
"""
import asyncio
import logging

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession


_LOGGER = logging.getLogger(__name__)

MA_DOMAIN = "music_assistant"

DEFAULT_TIMEOUT = 30.0


class MAPlaylistError(Exception):
    """A Music Assistant playlist call failed in a way the caller must see.

    Deliberately distinct from _ma_http_call()'s best-effort None: a failed
    similar-artists lookup just degrades a heuristic, but a failed catalogue
    read must never be mistaken for an empty result and recorded as one.
    """


async def async_ma_call(
    hass: HomeAssistant,
    command: str,
    args: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict | list | None:
    """Send one MA command. Raises MAPlaylistError on any failure."""
    entries = hass.config_entries.async_entries(MA_DOMAIN)
    if not entries:
        raise MAPlaylistError("No Music Assistant config entry is set up")

    client = _ha_client(entries)
    if client is not None:
        try:
            return await asyncio.wait_for(
                client.send_command(command, **(args or {})), timeout)
        except asyncio.TimeoutError as err:
            raise MAPlaylistError(f"MA command '{command}' timed out") from err
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - the client raises its own hierarchy
            raise MAPlaylistError(
                f"MA command '{command}' failed: {type(err).__name__}: {err}") from err

    url = entries[0].data.get("url")
    token = entries[0].data.get("token")
    if not url or not token:
        raise MAPlaylistError("Music Assistant config entry has no url/token")

    try:
        session = async_get_clientsession(hass)
        async with session.post(
            f"{url.rstrip('/')}/api",
            headers={"Authorization": f"Bearer {token}"},
            json={"command": command, "args": args or {}},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise MAPlaylistError(
                    f"MA command '{command}' returned HTTP {resp.status}: {body[:200]}"
                )
            if not body.strip():
                return None
            data = await resp.json(content_type=None)
    except MAPlaylistError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
        raise MAPlaylistError(f"MA command '{command}' failed: {err}") from err

    if isinstance(data, dict) and "error_code" in data:
        raise MAPlaylistError(f"MA command '{command}' errored: {data}")
    return data


def _ha_client(entries):
    """The connected MA client of a loaded music_assistant entry, or None."""
    for entry in entries:
        mass = getattr(getattr(entry, "runtime_data", None), "mass", None)
        connection = getattr(mass, "connection", None)
        if mass is not None and getattr(connection, "connected", False):
            return mass
    return None


def normalize_uri(uri: str | None) -> str:
    """Strip the provider *instance* suffix from a MA URI.

    NOTE (2026-09-20): this currently has no live call site — store.py:53
    references it only in a comment. Kept rather than deleted because the
    behaviour below was verified against a real MA server and would be
    expensive to rediscover; re-check before assuming it is wired in.

    URIs do not round-trip. Verified live: a track added as
    `apple_music://track/202272624` is reported back by playlist_tracks as
    `apple_music--mEvwYMn2://track/202272624` — MA substitutes the concrete
    provider instance id for the bare domain. The track id itself survives,
    so folding `<domain>--<instance>` back to `<domain>` makes the two
    comparable.

    This is not universal: a track already in the library can come back as
    `library://track/2142` instead, which shares no id with the added URI.
    That's why callers must fall back to artist+title matching and must
    never treat a URI mismatch alone as "the add failed" — doing so would
    mark every add as failed, retry, and duplicate the whole batch.
    """
    if not uri:
        return ""
    scheme, sep, rest = uri.partition("://")
    if not sep:
        return uri.strip().lower()
    return f"{scheme.split('--', 1)[0]}://{rest}".strip().lower()

