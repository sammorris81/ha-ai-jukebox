"""AI Jukebox button platform: one "Rebuild pool" button per playlist.

The button sits on the playlist's own device, next to its media player, so a
rebuild is one press from the device page or any dashboard instead of a
rebuild_pool action call with `force: true`. A press is always forced: someone
pressing it wants the rebuild now, not at 02:00.

The player is looked up by slug at press time rather than held, because the
two platforms are set up independently and a playlist's player can be replaced.
"""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SUBENTRY_PLAYLIST
from .media_player import playlist_device_info
from .store import PlaylistStore


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hass.data[DOMAIN]["button_add_entities"] = async_add_entities
    hass.data[DOMAIN].setdefault("buttons", {})
    for subentry in entry.get_subentries_of_type(SUBENTRY_PLAYLIST):
        ensure_rebuild_button(hass, subentry.data.get("name", ""), subentry.subentry_id)


def ensure_rebuild_button(hass: HomeAssistant, name: str, subentry_id: str) -> None:
    """Add the playlist's button unless it already has one."""
    if not name:
        return
    slug = PlaylistStore.playlist_slug(name)
    buttons = hass.data[DOMAIN].setdefault("buttons", {})
    add_entities = hass.data[DOMAIN].get("button_add_entities")
    if slug in buttons or add_entities is None:
        return
    button = RebuildPoolButton(slug, name)
    buttons[slug] = button
    add_entities([button], config_subentry_id=subentry_id)


class RebuildPoolButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "rebuild_pool"
    _attr_icon = "mdi:playlist-refresh"

    def __init__(self, slug: str, name: str) -> None:
        self._slug = slug
        self._attr_unique_id = f"ai_jukebox_{slug}_rebuild_pool"
        # Same device as the player's -- see playlist_device_info().
        self._attr_device_info = playlist_device_info(slug, name)

    async def async_press(self) -> None:
        player = self.hass.data[DOMAIN].get("players", {}).get(self._slug)
        if player is None:
            raise HomeAssistantError("This playlist's media player isn't loaded")
        if player.pool_build_running:
            raise HomeAssistantError("A rebuild is already running for this playlist")
        await player.async_rebuild_pool(force=True)
