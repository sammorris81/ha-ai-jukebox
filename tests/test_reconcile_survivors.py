"""_reconcile_stopped_survivors() after Music Assistant hands a group to a new leader.

Replays the 2026-11-25 incident: the leader is already playing and lists
both members in its group, but the members' own states still read idle
for a moment. They must not be detached for that.
"""

from unittest.mock import AsyncMock

from homeassistant.components.media_player import MediaPlayerEntityFeature

from custom_components.ai_jukebox import media_player as mp

LEADER = "media_player.main_bedroom"
FLOOR = "media_player.main_floor"
BATH = "media_player.main_bathroom"
OTHER = "media_player.game_room"
GROUPING = int(MediaPlayerEntityFeature.GROUPING)


def _player(hass, speakers):
    player = object.__new__(mp.AiJukeboxPlayer)
    player.hass = hass
    player._speakers = list(speakers)
    player.async_detach_speakers = AsyncMock()
    return player


def _set(hass, entity_id, state, members=()):
    hass.states.async_set(
        entity_id, state,
        {"supported_features": GROUPING, "group_members": list(members)},
    )


async def test_members_still_grouped_are_not_detached(hass, monkeypatch):
    monkeypatch.setattr(mp, "DETACH_POLL_INTERVAL", 0.01)
    _set(hass, LEADER, "playing", [LEADER, FLOOR, BATH])
    _set(hass, FLOOR, "idle")
    _set(hass, BATH, "idle")
    player = _player(hass, [LEADER, FLOOR, BATH])

    async def members_catch_up():
        _set(hass, FLOOR, "playing", [LEADER, FLOOR, BATH])
        _set(hass, BATH, "playing", [LEADER, FLOOR, BATH])

    hass.loop.call_later(0.05, lambda: hass.async_create_task(members_catch_up()))
    await player._reconcile_stopped_survivors()

    player.async_detach_speakers.assert_not_called()


async def test_member_outside_leader_group_is_detached(hass):
    _set(hass, LEADER, "playing", [LEADER, FLOOR])
    _set(hass, FLOOR, "playing", [LEADER, FLOOR])
    _set(hass, OTHER, "idle")
    player = _player(hass, [LEADER, FLOOR, OTHER])

    await player._reconcile_stopped_survivors()

    player.async_detach_speakers.assert_awaited_once_with([OTHER])


async def test_grouped_member_still_stopped_at_deadline_is_detached(hass, monkeypatch):
    monkeypatch.setattr(mp, "DETACH_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(mp, "DETACH_TRANSFER_TIMEOUT", 0.05)
    _set(hass, LEADER, "playing", [LEADER, FLOOR])
    _set(hass, FLOOR, "idle")
    player = _player(hass, [LEADER, FLOOR])

    await player._reconcile_stopped_survivors()

    player.async_detach_speakers.assert_awaited_once_with([FLOOR])
