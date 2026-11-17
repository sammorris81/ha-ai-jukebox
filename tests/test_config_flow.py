"""Offline tests for the subentry/options/scheduler rework (HA test harness)."""
import json
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.ai_jukebox import const
from custom_components.ai_jukebox.media_player import AiJukeboxPlayer, in_rebuild_window

DOMAIN = const.DOMAIN

PLAYLISTS = {
    "the_orchestral_arena": {
        "name": "The Orchestral Arena", "prompt": "Theatrical rock.",
        "batch_size": 10, "refill_threshold": 5, "exclude_live": True,
        "exclude_remix": True, "tags": ["rock"], "artists": ["Muse", "Queen"],
        "exclude_artists": ["Nightwish"], "exclude_genres": ["death metal"],
    },
    "country-pop_storytellers": {
        "name": "Country-Pop Storytellers", "prompt": "Story songs.",
        "batch_size": 10, "refill_threshold": 2, "tags": [], "artists": [],
    },
    "fixture": {
        "name": "Fixture", "prompt": "x", "batch_size": 10,
        "refill_threshold": 2, "tags": ["test"], "artists": [],
    },
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
async def core_setup(hass):
    from homeassistant.setup import async_setup_component
    assert await async_setup_component(hass, "homeassistant", {})
    yield


@pytest.fixture(autouse=True)
def no_pool_io():
    """Players read pool stats on add; nothing to read in a test."""
    with patch.object(AiJukeboxPlayer, "_refresh_pool_stats", AsyncMock()):
        yield


@pytest.fixture(autouse=True)
def legacy_store(hass_storage):
    hass_storage["ai_jukebox.playlists"] = {
        "version": 1, "key": "ai_jukebox.playlists",
        "data": {"playlists": json.loads(json.dumps(PLAYLISTS))},
    }
    return hass_storage


async def _setup_legacy(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, version=1, minor_version=1, unique_id=DOMAIN,
        data={"build_ai_entities": ["ai_task.old_data"]},
        options={"build_ai_entities": ["ai_task.a", "ai_task.b"]},
    )
    entry.add_to_hass(hass)
    # A pre-existing registry entry, as on the live install.
    er.async_get(hass).async_get_or_create(
        "media_player", DOMAIN, "ai_jukebox_the_orchestral_arena",
        config_entry=entry, suggested_object_id="ai_jukebox_the_orchestral_arena",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_migration(hass: HomeAssistant, legacy_store) -> None:
    entry = await _setup_legacy(hass)
    assert (entry.version, entry.minor_version) == (1, 2)
    assert dict(entry.data) == {}
    assert entry.options["build_ai_entities"] == ["ai_task.a", "ai_task.b"]
    assert entry.options["managed_speakers"] == []
    subs = entry.get_subentries_of_type("playlist")
    assert {s.unique_id for s in subs} == set(PLAYLISTS)
    arena = next(s for s in subs if s.unique_id == "the_orchestral_arena")
    assert dict(arena.data) == PLAYLISTS["the_orchestral_arena"]

    ent_reg, dev_reg = er.async_get(hass), dr.async_get(hass)
    reg = ent_reg.async_get("media_player.ai_jukebox_the_orchestral_arena")
    assert reg is not None and reg.config_subentry_id == arena.subentry_id
    device = dev_reg.async_get(reg.device_id)
    assert device.name == "AI Jukebox The Orchestral Arena"
    state = hass.states.get("media_player.ai_jukebox_the_orchestral_arena")
    assert state.attributes["friendly_name"] == "AI Jukebox The Orchestral Arena"
    assert (DOMAIN, "the_orchestral_arena") in device.identifiers
    # Hyphenated slug still maps to the underscore entity_id.
    assert hass.states.get("media_player.ai_jukebox_country_pop_storytellers")
    # Legacy store untouched (rollback safety).
    assert legacy_store["ai_jukebox.playlists"]["data"]["playlists"] == PLAYLISTS
    assert hass.data[DOMAIN]["build_ai_entities"] == ("ai_task.a", "ai_task.b")


async def test_reconfigure_adopts_without_reload(hass: HomeAssistant) -> None:
    entry = await _setup_legacy(hass)
    players = dict(hass.data[DOMAIN]["players"])
    arena = next(s for s in entry.get_subentries_of_type("playlist")
                 if s.unique_id == "the_orchestral_arena")

    result = await entry.start_subentry_reconfigure_flow(hass, arena.subentry_id)
    assert result["type"] is FlowResultType.FORM
    with patch.object(AiJukeboxPlayer, "async_rebuild_pool", AsyncMock()) as rebuild:
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {"prompt": "New prompt", "artists": ["Muse", " Queen ", "Muse"],
             "exclude_live": False, "exclude_remix": True, "batch_size": 12,
             "refill_threshold": 3, "build_now": True},
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert rebuild.await_count == 1
    data = entry.subentries[arena.subentry_id].data
    assert data["name"] == "The Orchestral Arena"
    assert data["artists"] == ["Muse", "Queen"]
    assert data["batch_size"] == 12 and data["exclude_live"] is False
    # Same player objects: no reload happened.
    assert hass.data[DOMAIN]["players"] == players
    state = hass.states.get("media_player.ai_jukebox_the_orchestral_arena")
    assert state.attributes["prompt"] == "New prompt"


async def test_add_and_delete_playlist(hass: HomeAssistant) -> None:
    entry = await _setup_legacy(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "playlist"), context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    form = {"prompt": "Quiet piano.", "exclude_live": True, "exclude_remix": True,
            "batch_size": 10, "refill_threshold": 2, "build_now": True}
    bad = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**form, "name": "the orchestral ARENA"})
    assert bad["errors"] == {"name": "name_taken"}
    with patch.object(AiJukeboxPlayer, "async_rebuild_pool", AsyncMock()) as rebuild:
        done = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {**form, "name": "Night Piano"})
        await hass.async_block_till_done()
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert rebuild.await_count == 1
    assert hass.states.get("media_player.ai_jukebox_night_piano")

    sub = next(s for s in entry.get_subentries_of_type("playlist")
               if s.unique_id == "night_piano")
    hass.config_entries.async_remove_subentry(entry, sub.subentry_id)
    await hass.async_block_till_done()
    assert hass.states.get("media_player.ai_jukebox_night_piano") is None
    assert "night_piano" not in hass.data[DOMAIN]["players"]


async def test_save_playlist_service(hass: HomeAssistant) -> None:
    entry = await _setup_legacy(hass)
    await hass.services.async_call(DOMAIN, "save_playlist", {
        "name": "Service Made", "prompt": "Via the action.", "artists": ["A"],
    }, blocking=True)
    await hass.async_block_till_done()
    sub = next(s for s in entry.get_subentries_of_type("playlist")
               if s.unique_id == "service_made")
    assert sub.data["artists"] == ["A"]
    assert hass.states.get("media_player.ai_jukebox_service_made")
    # Partial update keeps the other fields.
    await hass.services.async_call(DOMAIN, "save_playlist", {
        "name": "The Orchestral Arena", "exclude_genres": ["polka"],
    }, blocking=True)
    arena = next(s for s in entry.get_subentries_of_type("playlist")
                 if s.unique_id == "the_orchestral_arena")
    assert arena.data["exclude_genres"] == ["polka"]
    assert arena.data["artists"] == ["Muse", "Queen"]
    await hass.services.async_call(DOMAIN, "delete_playlist",
                                   {"name": "Service Made"}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("media_player.ai_jukebox_service_made") is None


async def test_options_apply_without_reload(hass: HomeAssistant) -> None:
    entry = await _setup_legacy(hass)
    old = hass.data[DOMAIN]["players"]["the_orchestral_arena"]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "build_ai_entities": ["ai_task.c"],
        "managed_speakers": ["media_player.kitchen"],
        "auto_rebuild": False, "rebuild_start_hour": 23, "rebuild_end_hour": 4,
    })
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert hass.data[DOMAIN]["managed_speakers"] == frozenset({"media_player.kitchen"})
    assert hass.data[DOMAIN]["build_ai_entities"] == ("ai_task.c",)
    # Applied in place: no reload, so live sessions are not suspended.
    assert hass.data[DOMAIN]["players"]["the_orchestral_arena"] is old
    assert hass.data[DOMAIN]["rebuild_start_hour"] == 23


async def test_fresh_setup(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"build_ai_entities": ["ai_task.x"]})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert (entry.version, entry.minor_version) == (1, 2)
    assert entry.options["build_ai_entities"] == ["ai_task.x"]
    assert entry.subentries == {}


def test_rebuild_window() -> None:
    assert in_rebuild_window(2, 2, 5) and in_rebuild_window(4, 2, 5)
    assert not in_rebuild_window(5, 2, 5) and not in_rebuild_window(1, 2, 5)
    assert in_rebuild_window(23, 23, 4) and in_rebuild_window(0, 23, 4)
    assert not in_rebuild_window(4, 23, 4) and not in_rebuild_window(12, 23, 4)
    assert not in_rebuild_window(3, 3, 3)


async def test_scheduler_picks_stalest(hass: HomeAssistant, freezer) -> None:
    from datetime import datetime
    from homeassistant.util import dt as dt_util
    entry = await _setup_legacy(hass)
    players = hass.data[DOMAIN]["players"]
    players["the_orchestral_arena"]._pool_stats = {"built_at": "2026-11-01T00:00:00"}
    players["country-pop_storytellers"]._pool_stats = {"built_at": "2026-10-01T00:00:00"}
    players["fixture"]._pool_stats = {"built_at": None}  # tagged test: skipped
    calls = []

    async def fake_rebuild(self, force=False, resume=False):
        calls.append((self.playlist_name, force, resume))

    with patch.object(AiJukeboxPlayer, "async_rebuild_pool", fake_rebuild):
        local = dt_util.get_default_time_zone()
        freezer.move_to(datetime(2026, 11, 17, 2, 59, 59, tzinfo=local))
        from pytest_homeassistant_custom_component.common import async_fire_time_changed
        freezer.tick(1)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert calls == [("Country-Pop Storytellers", False, False)]
        # Outside the window: nothing.
        calls.clear()
        freezer.move_to(datetime(2026, 11, 17, 11, 59, 59, tzinfo=local))
        freezer.tick(1)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert calls == []
        # A dead build gets resumed.
        players["fixture"]._pool_stats = {"built_at": None, "building": True}
        freezer.tick(15 * 60)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert ("Fixture", True, True) in calls


async def test_card_settings_ws(hass: HomeAssistant, hass_ws_client) -> None:
    entry = await _setup_legacy(hass)
    hass.config_entries.async_update_entry(entry, options={
        **entry.options, "managed_speakers": ["media_player.b", "media_player.a"]})
    await hass.async_block_till_done()
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "ai_jukebox/settings"})
    msg = await client.receive_json()
    assert msg["success"]
    assert msg["result"]["managed_speakers"] == ["media_player.a", "media_player.b"]
    assert msg["result"]["configured"] is True


async def test_card_is_served(hass: HomeAssistant, hass_client) -> None:
    await _setup_legacy(hass)
    client = await hass_client()
    resp = await client.get("/ai_jukebox/ai-jukebox-card.js")
    assert resp.status == 200
    assert "ai-jukebox-card" in await resp.text()
