"""Config flow for AI Jukebox.

Three surfaces, each owning one kind of setting:

- **Setup** picks the ai_task entities the pool builder uses.
- **Configure** on the integration (the options flow) holds every
  integration-wide setting: the ai_task list, the speakers auto-attach may
  adopt, and the nightly rebuild schedule.
- **Playlists** are config subentries: *Add playlist* on the integration page
  creates one, and *Configure* on a playlist's device edits it.
"""
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigSubentryFlow, SubentryFlowResult
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_AUTO_REBUILD,
    CONF_BUILD_AI_ENTITIES,
    CONF_MANAGED_SPEAKERS,
    CONF_PLAYLIST_ARTISTS,
    CONF_PLAYLIST_BATCH_SIZE,
    CONF_PLAYLIST_EXCLUDE_ARTISTS,
    CONF_PLAYLIST_EXCLUDE_GENRES,
    CONF_PLAYLIST_EXCLUDE_LIVE,
    CONF_PLAYLIST_EXCLUDE_REMIX,
    CONF_PLAYLIST_GENRES,
    CONF_PLAYLIST_NAME,
    CONF_PLAYLIST_PROMPT,
    CONF_PLAYLIST_REFILL_THRESHOLD,
    CONF_PLAYLIST_TAGS,
    CONF_REBUILD_END_HOUR,
    CONF_REBUILD_START_HOUR,
    DEFAULT_AUTO_REBUILD,
    DEFAULT_BATCH_SIZE,
    DEFAULT_REBUILD_END_HOUR,
    DEFAULT_REBUILD_START_HOUR,
    DEFAULT_REFILL_THRESHOLD,
    DOMAIN,
    SUBENTRY_PLAYLIST,
)
from .media_player import get_player
from .store import PlaylistStore

_LOGGER = logging.getLogger(__name__)

# Form-only field: start a pool build as soon as the playlist is saved.
CONF_BUILD_NOW = "build_now"

_LIST_FIELDS = (
    CONF_PLAYLIST_ARTISTS,
    CONF_PLAYLIST_EXCLUDE_ARTISTS,
    CONF_PLAYLIST_GENRES,
    CONF_PLAYLIST_EXCLUDE_GENRES,
    CONF_PLAYLIST_TAGS,
)


def _ai_entities_selector() -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain="ai_task", multiple=True)
    )


def _hour_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0, max=23, step=1, mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="h",
        )
    )


def _request_build(hass, slug: str) -> None:
    """Queue a pool build for `slug`, started by _async_sync_players once the
    subentry change has landed and the playlist's player exists."""
    hass.data.setdefault(DOMAIN, {}).setdefault("_build_on_save", set()).add(slug)


class AiJukeboxConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AI Jukebox."""

    VERSION = 1
    # 1.2: settings live in options only; playlists are subentries.
    MINOR_VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return AiJukeboxOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: config_entries.ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_PLAYLIST: PlaylistSubentryFlow}

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors = {}

        if user_input is not None:
            entities = user_input.get(CONF_BUILD_AI_ENTITIES) or []
            if not entities:
                errors[CONF_BUILD_AI_ENTITIES] = "no_ai_entity"
            else:
                return self.async_create_entry(
                    title="AI Jukebox",
                    data={},
                    options={
                        CONF_BUILD_AI_ENTITIES: list(entities),
                        CONF_MANAGED_SPEAKERS: [],
                        CONF_AUTO_REBUILD: DEFAULT_AUTO_REBUILD,
                        CONF_REBUILD_START_HOUR: DEFAULT_REBUILD_START_HOUR,
                        CONF_REBUILD_END_HOUR: DEFAULT_REBUILD_END_HOUR,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_BUILD_AI_ENTITIES): _ai_entities_selector()}
            ),
            errors=errors,
        )


class AiJukeboxOptionsFlow(config_entries.OptionsFlow):
    """Integration-wide settings.

    The ai_task order matters and is by *daily request budget*, not quality:
    on a free tier the lite models allow far more requests a day, so leading
    with the scarce ones exhausts the chain after a couple of playlists.
    Rotation starts where the previous call left off rather than at the head,
    so one entity's quota is not drained before the fallback is ever tried.
    """

    async def async_step_init(self, user_input=None):
        errors = {}
        options = self.config_entry.options

        if user_input is not None:
            if not user_input.get(CONF_BUILD_AI_ENTITIES):
                errors[CONF_BUILD_AI_ENTITIES] = "no_ai_entity"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_BUILD_AI_ENTITIES: list(user_input[CONF_BUILD_AI_ENTITIES]),
                        CONF_MANAGED_SPEAKERS: list(user_input.get(CONF_MANAGED_SPEAKERS) or []),
                        CONF_AUTO_REBUILD: bool(user_input.get(CONF_AUTO_REBUILD)),
                        CONF_REBUILD_START_HOUR: int(user_input[CONF_REBUILD_START_HOUR]),
                        CONF_REBUILD_END_HOUR: int(user_input[CONF_REBUILD_END_HOUR]),
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_BUILD_AI_ENTITIES): _ai_entities_selector(),
                vol.Optional(CONF_MANAGED_SPEAKERS): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="media_player", multiple=True)
                ),
                vol.Required(CONF_AUTO_REBUILD): selector.BooleanSelector(),
                vol.Required(CONF_REBUILD_START_HOUR): _hour_selector(),
                vol.Required(CONF_REBUILD_END_HOUR): _hour_selector(),
            }
        )
        suggested = {
            CONF_BUILD_AI_ENTITIES: list(options.get(CONF_BUILD_AI_ENTITIES) or []),
            CONF_MANAGED_SPEAKERS: list(options.get(CONF_MANAGED_SPEAKERS) or []),
            CONF_AUTO_REBUILD: options.get(CONF_AUTO_REBUILD, DEFAULT_AUTO_REBUILD),
            CONF_REBUILD_START_HOUR: options.get(CONF_REBUILD_START_HOUR, DEFAULT_REBUILD_START_HOUR),
            CONF_REBUILD_END_HOUR: options.get(CONF_REBUILD_END_HOUR, DEFAULT_REBUILD_END_HOUR),
        }
        if user_input is not None:
            suggested.update(user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )


def _playlist_schema(include_name: bool) -> vol.Schema:
    """One playlist's form. The name is set once, at creation.

    Renaming would change the slug, and with it the entity_id and the
    queue/pool/plays files, all of which are keyed on it -- so the edit form
    leaves the name out rather than orphaning them.
    """
    text_list = selector.TextSelector(selector.TextSelectorConfig(multiple=True))
    fields: dict[Any, Any] = {}
    if include_name:
        fields[vol.Required(CONF_PLAYLIST_NAME)] = selector.TextSelector()
    fields.update({
        vol.Required(CONF_PLAYLIST_PROMPT): selector.TextSelector(
            selector.TextSelectorConfig(multiline=True)
        ),
        vol.Optional(CONF_PLAYLIST_ARTISTS): text_list,
        vol.Optional(CONF_PLAYLIST_EXCLUDE_ARTISTS): text_list,
        vol.Optional(CONF_PLAYLIST_GENRES): text_list,
        vol.Optional(CONF_PLAYLIST_EXCLUDE_GENRES): text_list,
        vol.Optional(CONF_PLAYLIST_TAGS): text_list,
        vol.Required(CONF_PLAYLIST_EXCLUDE_LIVE): selector.BooleanSelector(),
        vol.Required(CONF_PLAYLIST_EXCLUDE_REMIX): selector.BooleanSelector(),
        vol.Required(CONF_PLAYLIST_BATCH_SIZE): selector.NumberSelector(
            selector.NumberSelectorConfig(min=3, max=50, step=1,
                                          mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Required(CONF_PLAYLIST_REFILL_THRESHOLD): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=20, step=1,
                                          mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Required(CONF_BUILD_NOW): selector.BooleanSelector(),
    })
    return vol.Schema(fields)


def _clean_playlist(user_input: dict) -> dict:
    """Form values -> the stored playlist record's shape."""
    config = {
        CONF_PLAYLIST_PROMPT: (user_input.get(CONF_PLAYLIST_PROMPT) or "").strip(),
        CONF_PLAYLIST_EXCLUDE_LIVE: bool(user_input.get(CONF_PLAYLIST_EXCLUDE_LIVE, True)),
        CONF_PLAYLIST_EXCLUDE_REMIX: bool(user_input.get(CONF_PLAYLIST_EXCLUDE_REMIX, True)),
        CONF_PLAYLIST_BATCH_SIZE: int(user_input.get(CONF_PLAYLIST_BATCH_SIZE, DEFAULT_BATCH_SIZE)),
        CONF_PLAYLIST_REFILL_THRESHOLD: int(
            user_input.get(CONF_PLAYLIST_REFILL_THRESHOLD, DEFAULT_REFILL_THRESHOLD)),
    }
    for key in _LIST_FIELDS:
        # Ordered dedupe, not set(): order means something to whoever wrote it.
        cleaned: list[str] = []
        for value in user_input.get(key) or []:
            value = str(value).strip()
            if value and value not in cleaned:
                cleaned.append(value)
        config[key] = cleaned
    return config


_FORM_DEFAULTS = {
    CONF_PLAYLIST_EXCLUDE_LIVE: True,
    CONF_PLAYLIST_EXCLUDE_REMIX: True,
    CONF_PLAYLIST_BATCH_SIZE: DEFAULT_BATCH_SIZE,
    CONF_PLAYLIST_REFILL_THRESHOLD: DEFAULT_REFILL_THRESHOLD,
}


class PlaylistSubentryFlow(ConfigSubentryFlow):
    """Add a playlist, or edit one from its device's Configure button."""

    async def async_step_user(self, user_input=None) -> SubentryFlowResult:
        errors = {}
        if user_input is not None:
            name = (user_input.get(CONF_PLAYLIST_NAME) or "").strip()
            slug = PlaylistStore.playlist_slug(name)
            taken = {
                sub.unique_id
                for sub in self._get_entry().get_subentries_of_type(SUBENTRY_PLAYLIST)
            }
            if not name:
                errors[CONF_PLAYLIST_NAME] = "name_required"
            elif slug in taken:
                errors[CONF_PLAYLIST_NAME] = "name_taken"
            elif not (user_input.get(CONF_PLAYLIST_PROMPT) or "").strip():
                errors[CONF_PLAYLIST_PROMPT] = "prompt_required"
            else:
                if user_input.get(CONF_BUILD_NOW):
                    _request_build(self.hass, slug)
                return self.async_create_entry(
                    title=name,
                    data={CONF_PLAYLIST_NAME: name, **_clean_playlist(user_input)},
                    unique_id=slug,
                )

        # A new playlist has an empty pool and cannot play until it is built,
        # so building straight away is the default here and not on edit.
        suggested = {**_FORM_DEFAULTS, CONF_BUILD_NOW: True, **(user_input or {})}
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _playlist_schema(include_name=True), suggested),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input=None) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        errors = {}
        if user_input is not None:
            if not (user_input.get(CONF_PLAYLIST_PROMPT) or "").strip():
                errors[CONF_PLAYLIST_PROMPT] = "prompt_required"
            else:
                # data_updates, not data: keeps keys the form does not show,
                # name included.
                result = self.async_update_and_abort(
                    self._get_entry(), subentry,
                    data_updates=_clean_playlist(user_input),
                )
                # The player already exists, and the builder reads the
                # playlist through the store, which already sees the update.
                # Started here rather than via _request_build because an
                # unchanged form fires no update listener at all.
                player = get_player(self.hass, subentry.unique_id)
                if user_input.get(CONF_BUILD_NOW) and player is not None:
                    self.hass.async_create_task(player.async_rebuild_pool(force=True))
                return result

        suggested = {
            **_FORM_DEFAULTS, **dict(subentry.data), CONF_BUILD_NOW: False,
            **(user_input or {}),
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _playlist_schema(include_name=False), suggested),
            errors=errors,
            description_placeholders={"name": subentry.title},
        )
