"""The AI Jukebox integration."""
import asyncio
from datetime import timedelta
import json
import logging
import os
from types import MappingProxyType

from homeassistant.components.media_player import MediaPlayerState
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_time_change, async_track_time_interval
from homeassistant.util import dt as dt_util
import voluptuous as vol

from homeassistant.components import websocket_api

from .const import (
    CONF_AUTO_REBUILD,
    CONF_BUILD_AI_ENTITIES,
    CONF_MANAGED_SPEAKERS,
    CONF_PLAYLIST_ARTISTS,
    CONF_PLAYLIST_BATCH_SIZE,
    CONF_PLAYLIST_EXCLUDE_ARTISTS,
    CONF_PLAYLIST_EXCLUDE_GENRES,
    CONF_PLAYLIST_GENRES,
    CONF_PLAYLIST_PROMPT,
    CONF_PLAYLIST_REFILL_THRESHOLD,
    CONF_PLAYLIST_TAGS,
    CONF_REBUILD_END_HOUR,
    CONF_REBUILD_START_HOUR,
    DEFAULT_AUTO_REBUILD,
    DEFAULT_BATCH_SIZE,
    DEFAULT_REBUILD_END_HOUR,
    DEFAULT_REBUILD_START_HOUR,
    DOMAIN,
    NO_AUTO_REBUILD_TAG,
    PLATFORMS,
    RESUME_STALLED_BUILD_MINUTES,
    SUBENTRY_PLAYLIST,
)
from .media_player import get_or_create_player, get_player, in_rebuild_window
from .store import PlaylistStore

_LOGGER = logging.getLogger(__name__)

SERVICE_START = "start"
SERVICE_STOP = "stop"
SERVICE_ATTACH_SPEAKER = "attach_speaker"
SERVICE_DETACH_SPEAKER = "detach_speaker"
SERVICE_RESET = "reset"
SERVICE_SAVE_PLAYLIST = "save_playlist"
SERVICE_DELETE_PLAYLIST = "delete_playlist"
SERVICE_LIST_PLAYLISTS = "list_playlists"
SERVICE_CLEAR_HISTORY = "clear_history"
SERVICE_REBUILD_POOL = "rebuild_pool"


# These are the first playlist keys whose bad values cause *writes to Music
# Assistant*, and there is no voluptuous schema anywhere in this integration
# — so they're coerced and checked explicitly here rather than trusted.
def _coerce_bool(value, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "yes", "no", "on", "off"):
        return value.strip().lower() in ("true", "yes", "on")
    raise HomeAssistantError(f"{key} must be a boolean (got {value!r})")


def _coerce_str_list(value, key: str) -> list[str]:
    """A list of non-empty strings, or raise.

    A bare string must not be accepted: these values are iterated when the
    build composes its instructions and when exclude_artists is enforced, so
    "Nightwish" would be read as eight single-character entries.
    """
    if isinstance(value, str):
        raise HomeAssistantError(
            f"{key} must be a list of names, not a single string (got {value!r})")
    if not isinstance(value, (list, tuple)):
        raise HomeAssistantError(f"{key} must be a list (got {value!r})")
    return [str(v).strip() for v in value if str(v).strip()]


def _coerce_positive_int(value, key: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as err:
        raise HomeAssistantError(f"{key} must be a whole number (got {value!r})") from err
    if number < 1:
        raise HomeAssistantError(f"{key} must be at least 1 (got {number})")
    return number


CARD_FILENAME = "ai-jukebox-card.js"
CARD_URL_BASE = f"/{DOMAIN}/{CARD_FILENAME}"


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    websocket_api.async_register_command(hass, websocket_settings)
    await _async_register_card(hass)
    return True


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve the bundled dashboard card and load it on every frontend.

    Bundled rather than a separate HACS dashboard repo, so installing the
    integration is enough to get its card. The manifest version is appended
    as a query string so browsers fetch a new copy after an update.
    """
    if getattr(hass, "http", None) is None:
        return
    from homeassistant.components.http import StaticPathConfig

    here = os.path.dirname(__file__)

    def _version() -> str:
        with open(os.path.join(here, "manifest.json"), encoding="utf-8") as f:
            return json.load(f).get("version", "0")

    version = await hass.async_add_executor_job(_version)
    await hass.http.async_register_static_paths([
        StaticPathConfig(CARD_URL_BASE, os.path.join(here, "frontend", CARD_FILENAME), True)
    ])
    if "frontend" in hass.config.components:
        from homeassistant.components.frontend import add_extra_js_url

        add_extra_js_url(hass, f"{CARD_URL_BASE}?v={version}")


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/settings"})
@callback
def websocket_settings(hass: HomeAssistant, connection, msg: dict) -> None:
    """What the card needs from the integration's options.

    The card's speaker picker offers only the managed speakers -- the same
    allowlist auto-attach uses -- so the two never disagree about which
    speakers the jukebox is meant to drive.
    """
    data = hass.data.get(DOMAIN, {})
    connection.send_result(msg["id"], {
        "managed_speakers": list(data.get("managed_speakers_ordered") or ()),
        "configured": "entry" in data,
        "entry_id": data["entry"].entry_id if "entry" in data else None,
    })


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """1.1 -> 1.2: settings move to options, playlists become subentries.

    1.1 kept the ai_task list in `data` (setup and Reconfigure) *and*
    `options` (Configure), with options winning; 1.2 keeps it in options only.
    Playlists move from the `ai_jukebox.playlists` store into one subentry
    each. That store is read, never written or deleted, so going back to a
    1.1 build still finds every playlist as it was before the upgrade.
    """
    if entry.version > 1:
        return False
    if entry.version == 1 and entry.minor_version < 2:
        options = {
            CONF_BUILD_AI_ENTITIES: list(
                entry.options.get(CONF_BUILD_AI_ENTITIES)
                or entry.data.get(CONF_BUILD_AI_ENTITIES)
                or []
            ),
            CONF_MANAGED_SPEAKERS: [],
            CONF_AUTO_REBUILD: DEFAULT_AUTO_REBUILD,
            CONF_REBUILD_START_HOUR: DEFAULT_REBUILD_START_HOUR,
            CONF_REBUILD_END_HOUR: DEFAULT_REBUILD_END_HOUR,
        }
        legacy = PlaylistStore(hass)
        await legacy.async_load()
        existing = {sub.unique_id for sub in entry.subentries.values()}
        imported = 0
        for slug, config in legacy.legacy_playlists().items():
            if slug in existing or not config.get("name"):
                continue
            hass.config_entries.async_add_subentry(entry, ConfigSubentry(
                data=MappingProxyType(dict(config)),
                subentry_type=SUBENTRY_PLAYLIST,
                title=config["name"],
                unique_id=slug,
            ))
            imported += 1
        hass.config_entries.async_update_entry(
            entry, data={}, options=options, version=1, minor_version=2)
        _LOGGER.info("Migrated AI Jukebox to 1.2: %d playlist(s) now subentries", imported)
    return True


def _apply_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Publish the entry's options where the players and builder read them."""
    data = hass.data[DOMAIN]
    data["build_ai_entities"] = tuple(entry.options.get(CONF_BUILD_AI_ENTITIES) or ()) or None
    data[CONF_MANAGED_SPEAKERS] = frozenset(entry.options.get(CONF_MANAGED_SPEAKERS) or ())
    # The set answers "is this speaker managed"; the cards list them in the
    # order they were arranged on the Configure page.
    data["managed_speakers_ordered"] = list(entry.options.get(CONF_MANAGED_SPEAKERS) or ())
    data[CONF_REBUILD_START_HOUR] = int(
        entry.options.get(CONF_REBUILD_START_HOUR, DEFAULT_REBUILD_START_HOUR))
    data[CONF_REBUILD_END_HOUR] = int(
        entry.options.get(CONF_REBUILD_END_HOUR, DEFAULT_REBUILD_END_HOUR))


def _playlist_snapshot(entry: ConfigEntry) -> dict[str, dict]:
    return {
        sub.subentry_id: dict(sub.data)
        for sub in entry.get_subentries_of_type(SUBENTRY_PLAYLIST)
    }


@callback
def _async_sync_players(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Bring the players in line with the playlist subentries.

    Replaces what used to be a whole-entry reload on every update. With one
    subentry per playlist, a reload on each edit would push every active
    session through suspend/resume to change one playlist's prompt. Instead:
    a new subentry gets a player, an edited one is adopted in place
    (async_adopt_config), and a removed one's player is dropped -- HA removes
    its entity and device itself when the subentry goes.

    Idempotent, so save_playlist can call it directly as well as the listener.
    """
    domain = hass.data[DOMAIN]
    known: dict[str, dict] = domain.setdefault("_playlist_snapshot", {})
    current = _playlist_snapshot(entry)
    players = domain.setdefault("players", {})
    for subentry_id, config in current.items():
        if known.get(subentry_id) == config:
            continue
        player = get_or_create_player(hass, entry, config, subentry_id)
        slug = PlaylistStore.playlist_slug(config.get("name", ""))
        if player is not None and slug in domain.setdefault("_build_on_save", set()):
            domain["_build_on_save"].discard(slug)
            hass.async_create_task(player.async_rebuild_pool(force=True))
    for subentry_id in set(known) - set(current):
        slug = PlaylistStore.playlist_slug(known[subentry_id].get("name", ""))
        players.pop(slug, None)
    domain["_playlist_snapshot"] = current


def _setup_scheduler(hass: HomeAssistant, entry: ConfigEntry) -> list:
    """Nightly pool refresh plus resumption of builds that died mid-flight.

    Replaces two automations that used to live in the author's own packages,
    so a fresh install keeps its pools fresh with no YAML. One rebuild is
    started per hour inside the window, least-recently-built first, so a
    02:00-05:00 window refreshes three playlists a night: each rebuild is
    ~30 model requests, and free-tier quotas are counted per day.
    """
    players = lambda: list(hass.data[DOMAIN].get("players", {}).values())  # noqa: E731

    async def _hourly(now) -> None:
        start = hass.data[DOMAIN][CONF_REBUILD_START_HOUR]
        end = hass.data[DOMAIN][CONF_REBUILD_END_HOUR]
        if not in_rebuild_window(dt_util.as_local(now).hour, start, end):
            return
        candidates = [
            p for p in players()
            if not p.pool_building and not p.pool_build_running
            and NO_AUTO_REBUILD_TAG not in p.tags
        ]
        if not candidates:
            return
        # Never-built pools first, then the stalest.
        candidates.sort(key=lambda p: (p.pool_built_at is not None, p.pool_built_at or ""))
        player = candidates[0]
        _LOGGER.info("Scheduled pool rebuild: %s", player.playlist_name)
        try:
            await player.async_rebuild_pool(force=False)
        except HomeAssistantError as err:
            _LOGGER.warning("Scheduled pool rebuild of %s refused: %s",
                            player.playlist_name, err)

    async def _resume_stalled(_now=None) -> None:
        for player in players():
            if player.pool_building and not player.pool_build_running:
                _LOGGER.info("Resuming stalled pool build: %s", player.playlist_name)
                await player.async_rebuild_pool(force=True, resume=True)

    return [
        async_track_time_change(hass, _hourly, minute=0, second=0),
        async_track_time_interval(
            hass, _resume_stalled, timedelta(minutes=RESUME_STALLED_BUILD_MINUTES)),
        # Also shortly after setup, so a build cut off by a restart does not
        # wait for the first interval. Delayed rather than immediate: players
        # read their pool stats as they are added, which takes a moment.
        async_call_later(hass, 60, _resume_stalled),
    ]


@callback
def _async_apply_scheduler(hass: HomeAssistant, entry: ConfigEntry, unsubs: list) -> None:
    """(Re)arm the scheduler to match the auto_rebuild option.

    `unsubs` is owned by the entry's setup rather than kept in hass.data:
    async_unload_entry replaces hass.data[DOMAIN] before the entry's unload
    callbacks run, so a list kept there could never be cancelled.
    """
    while unsubs:
        unsubs.pop()()
    if entry.options.get(CONF_AUTO_REBUILD, DEFAULT_AUTO_REBUILD):
        unsubs.extend(_setup_scheduler(hass, entry))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # A suspend snapshot from async_unload_entry (see below) survives here —
    # setup_entry intentionally only resets the rest of the domain dict, not
    # this key, so an integration reload can resume in-place-paused sessions.
    suspended = hass.data.get(DOMAIN, {}).get("_suspended", {})
    hass.data[DOMAIN] = {"_suspended": suspended}

    store = PlaylistStore(hass)
    await store.async_load()
    store.attach_entry(entry)
    hass.data[DOMAIN]["store"] = store
    _apply_options(hass, entry)
    hass.data[DOMAIN]["entry"] = entry
    hass.data[DOMAIN]["_options_snapshot"] = dict(entry.options)


    # Serializes background pool growth across all playlists — they share one
    # CPU-bound Ollama box, so concurrent growth runs would just queue inside
    # Ollama and spend their timeout budget waiting. Deliberately not taken
    # by playback-path generation, which must never wait behind maintenance.
    hass.data[DOMAIN]["_pool_grow_lock"] = asyncio.Lock()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    hass.data[DOMAIN]["_playlist_snapshot"] = _playlist_snapshot(entry)

    async def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
        # Nothing here reloads the entry. Every option is read where it is
        # used (hass.data), so applying one in place is enough -- and a reload
        # suspends every live session, which in practice did not survive it:
        # on 2026-11-17 two Configure saves in a row left a playing Sonos idle
        # and the session ended. Playlist edits are likewise adopted in place,
        # see _async_sync_players.
        if dict(entry.options) != hass.data[DOMAIN].get("_options_snapshot"):
            _apply_options(hass, entry)
            _async_apply_scheduler(hass, entry, scheduler_unsubs)
            hass.data[DOMAIN]["_options_snapshot"] = dict(entry.options)
        _async_sync_players(hass, entry)

    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    scheduler_unsubs: list = []
    _async_apply_scheduler(hass, entry, scheduler_unsubs)

    @callback
    def _cancel_scheduler() -> None:
        while scheduler_unsubs:
            scheduler_unsubs.pop()()

    entry.async_on_unload(_cancel_scheduler)

    def _resolve_target_entity_id(call: ServiceCall) -> str:
        """Pull the single targeted media_player.ai_jukebox_* entity_id off a call.

        Mirrors the defensive entity_id/target handling the old ai_playlist
        integration used — on this HA version, target fields are available
        both merged into call.data and via call.target, depending on caller.
        """
        entity_id = call.data.get("entity_id")
        if not entity_id and getattr(call, "target", None):
            entity_ids = call.target.get("entity_id", [])
            if isinstance(entity_ids, str):
                entity_id = entity_ids
            elif isinstance(entity_ids, list) and entity_ids:
                entity_id = entity_ids[0]
        if isinstance(entity_id, list):
            entity_id = entity_id[0] if entity_id else None
        if not entity_id:
            raise HomeAssistantError("No target entity specified")
        return entity_id

    def _get_player_or_raise(call: ServiceCall):
        entity_id = _resolve_target_entity_id(call)
        slug = entity_id.removeprefix("media_player.ai_jukebox_")
        player = get_player(hass, slug)
        if player:
            return player

        # A slug may legally contain a hyphen ("country-pop_storytellers"),
        # but an entity_id may not -- Home Assistant rewrites it to an
        # underscore. Stripping the prefix therefore does not round-trip back
        # to the slug, and every service call for those playlists failed to
        # find its player. Fall back to comparing on the normalised form.
        wanted = slug.replace("-", "_")
        for candidate_slug, candidate in hass.data[DOMAIN].get("players", {}).items():
            if candidate_slug.replace("-", "_") == wanted:
                return candidate
        raise HomeAssistantError(f"No AI Jukebox playlist entity found for {entity_id}")

    async def async_handle_start(call: ServiceCall) -> None:
        player = _get_player_or_raise(call)
        group_members = call.data["group_members"]
        batch_size = call.data.get("batch_size")
        if batch_size is not None:
            batch_size = int(batch_size)

        await player.async_session_start(
            group_members, batch_size=batch_size
        )

    async def async_handle_stop(call: ServiceCall) -> None:
        player = _get_player_or_raise(call)
        group_members = call.data.get("group_members")
        await player.async_session_stop(speakers=group_members)

    async def async_handle_attach_speaker(call: ServiceCall) -> None:
        player = _get_player_or_raise(call)
        await player.async_attach_speakers(call.data["group_members"])

    async def async_handle_detach_speaker(call: ServiceCall) -> None:
        player = _get_player_or_raise(call)
        await player.async_detach_speakers(call.data["group_members"])

    async def async_handle_reset(call: ServiceCall) -> None:
        player = _get_player_or_raise(call)
        await player.async_session_reset()

    async def async_handle_save_playlist(call: ServiceCall) -> None:
        name = call.data["name"]
        # Only forward what the caller actually sent. Filling absent fields
        # with defaults here is what made a partial save destructive: the
        # store could not tell "omitted" from "explicitly set to the
        # default", so it overwrote the real values every time.
        config = {}
        if "prompt" in call.data:
            config[CONF_PLAYLIST_PROMPT] = call.data["prompt"]
        if "batch_size" in call.data:
            config[CONF_PLAYLIST_BATCH_SIZE] = _coerce_positive_int(
                call.data["batch_size"], "batch_size")
        if "refill_threshold" in call.data:
            config[CONF_PLAYLIST_REFILL_THRESHOLD] = _coerce_positive_int(
                call.data["refill_threshold"], "refill_threshold")
        if "tags" in call.data:
            config[CONF_PLAYLIST_TAGS] = call.data["tags"]
        if "artists" in call.data:
            config[CONF_PLAYLIST_ARTISTS] = call.data["artists"]
        for field, conf_key in (
            ("exclude_artists", CONF_PLAYLIST_EXCLUDE_ARTISTS),
            ("genres", CONF_PLAYLIST_GENRES),
            ("exclude_genres", CONF_PLAYLIST_EXCLUDE_GENRES),
        ):
            if field in call.data:
                config[conf_key] = _coerce_str_list(call.data[field], field)
        generate = _coerce_bool(call.data.get("generate", False), "generate")

        try:
            stored = await store.async_save_playlist(name, config)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        _async_sync_players(hass, entry)
        player = get_player(hass, PlaylistStore.playlist_slug(stored["name"]))
        # Saving alone must not rebuild: editing a prompt or adding one artist
        # would otherwise burn a full build every time.
        if generate and player is not None:
            await player.async_rebuild_pool(force=True)

    async def async_handle_rebuild_pool(call: ServiceCall) -> None:
        player = _get_player_or_raise(call)
        await player.async_rebuild_pool(
            force=bool(call.data.get("force", False)),
            resume=bool(call.data.get("resume", False)),
        )

    async def async_handle_delete_playlist(call: ServiceCall) -> None:
        name = call.data["name"]
        slug = PlaylistStore.playlist_slug(name)
        player = get_player(hass, slug)
        if player and player.state != MediaPlayerState.IDLE:
            raise HomeAssistantError(
                f"'{name}' is currently active — stop it before deleting"
            )
        await store.async_delete_playlist(name)
        _async_sync_players(hass, entry)
        _LOGGER.info("Deleted playlist '%s' with its entity and device", name)

    async def async_handle_list_playlists(call: ServiceCall) -> dict:
        tag = call.data.get("tag")
        tags = call.data.get("tags", [])
        if tag and not tags:
            tags = [tag]
        playlists = (
            store.get_playlists_by_tags(tags) if tags else list(store.get_all_playlists().values())
        )
        return {
            "playlists": [
                {
                    "name": p.get("name", ""),
                    "tags": p.get("tags", []),
                    "batch_size": p.get("batch_size", DEFAULT_BATCH_SIZE),
                    "has_prompt": bool(p.get("prompt")),
                }
                for p in playlists
            ]
        }

    async def async_handle_clear_history(call: ServiceCall) -> None:
        """Clears the plays file -- the only record of what was played."""
        player = _get_player_or_raise(call)
        await store.async_clear_plays(player.playlist_name)

    hass.services.async_register(DOMAIN, SERVICE_START, async_handle_start)
    hass.services.async_register(DOMAIN, SERVICE_STOP, async_handle_stop)
    hass.services.async_register(DOMAIN, SERVICE_ATTACH_SPEAKER, async_handle_attach_speaker)
    hass.services.async_register(DOMAIN, SERVICE_DETACH_SPEAKER, async_handle_detach_speaker)
    hass.services.async_register(DOMAIN, SERVICE_RESET, async_handle_reset)
    hass.services.async_register(DOMAIN, SERVICE_SAVE_PLAYLIST, async_handle_save_playlist)
    hass.services.async_register(DOMAIN, SERVICE_DELETE_PLAYLIST, async_handle_delete_playlist)
    hass.services.async_register(
        DOMAIN, SERVICE_LIST_PLAYLISTS, async_handle_list_playlists,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_HISTORY, async_handle_clear_history)
    hass.services.async_register(DOMAIN, SERVICE_REBUILD_POOL, async_handle_rebuild_pool)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Suspend (pause in place, don't clear) any active session before tearing
    # anything down, so a reload can resume it — see async_added_to_hass() in
    # media_player.py, which consumes this snapshot when entities come back.
    players = hass.data.get(DOMAIN, {}).get("players", {})
    suspended = {
        slug: await player.async_suspend()
        for slug, player in players.items()
        if player.state != MediaPlayerState.IDLE
    }

    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    for service in (
        SERVICE_START, SERVICE_STOP, SERVICE_ATTACH_SPEAKER, SERVICE_DETACH_SPEAKER,
        SERVICE_RESET, SERVICE_SAVE_PLAYLIST, SERVICE_DELETE_PLAYLIST,
        SERVICE_LIST_PLAYLISTS, SERVICE_CLEAR_HISTORY, SERVICE_REBUILD_POOL,
    ):
        hass.services.async_remove(DOMAIN, service)

    hass.data[DOMAIN] = {"_suspended": suspended}
    return True
