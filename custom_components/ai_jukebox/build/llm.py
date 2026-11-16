"""Model calls for the builder, via HA's ai_task with entity rotation.

Everything here asks the model to *judge supplied data*, never to recall
facts -- see the design doc's section 1. That is why structured output
matters: each call returns rows keyed back to data we already hold.

ai_task's `structure` is HA's selector format, not raw JSON Schema. Two
limits discovered by probing the live service:

  * An array of typed objects needs an `object` selector with `multiple`
    plus nested `fields`. A bare `object` selector collapses every row to
    {"json": "<free text>"}.
  * The `number` selector rejects `multiple`, so a list of integers has to
    be a `text` selector with `multiple` and be parsed back with int().
"""
from __future__ import annotations

import asyncio
import logging

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..const import BUILD_CALL_TIMEOUT_SECONDS

_LOGGER = logging.getLogger(__name__)


class BuildModelError(HomeAssistantError):
    """Every configured ai_task entity failed for one call."""


def text_field(description: str, multiple: bool = False) -> dict:
    """A nested field inside rows().

    `description` is accepted and discarded: HA rejects the whole structure
    with a 400 if a *nested* field carries one, though the top-level entry
    may. It is kept in the signature so call sites stay self-documenting --
    the model is told what each field means in the instructions text instead.
    """
    return {"selector": {"text": {"multiple": multiple}}}


def number_field(description: str) -> dict:
    """A nested numeric field. See text_field on why description is dropped."""
    return {"selector": {"number": {"mode": "box"}}}


def row_field(fields: dict) -> dict:
    """A nested list of objects, one level below rows().

    Probed 2026-09-14: an `object` selector with `multiple` nests inside
    another one and survives -- the rows come back as real objects, not the
    {"json": "<free text>"} collapse a bare `object` produces. Unlike rows()
    this carries no `description`/`required`, because a nested field that
    has either 400s the whole call.
    """
    return {"selector": {"object": {"multiple": True, "fields": fields}}}


def rows(description: str, fields: dict) -> dict:
    """A list of objects with typed fields -- the only shape that survives."""
    return {
        "description": description,
        "required": True,
        "selector": {"object": {"multiple": True, "fields": fields}},
    }


def as_ints(values) -> list[int]:
    """Parse a text-selector list back to ints, dropping anything unparseable.

    The model returns ["1", "3"] rather than [1, 3]; it also occasionally
    emits "12." or " 7". Silently skipping junk is right here -- a stray
    token should cost one album, not the whole call.
    """
    out: list[int] = []
    for v in values or []:
        try:
            out.append(int(str(v).strip().rstrip(".")))
        except (TypeError, ValueError):
            continue
    return out


class BuildModel:
    """Rotating ai_task caller.

    Entities are tried in configured order, which is by daily request budget
    rather than quality. Rotation is per call and starts where the last one
    left off: always restarting at the head would drain the first entity's
    daily quota and only then discover the fallback, which is how an earlier
    full rebuild died partway through.
    """

    def __init__(self, hass: HomeAssistant, entities: tuple[str, ...] | None = None) -> None:
        self._hass = hass
        # No fallback list: entity ids are specific to each install, and are
        # chosen in the integration's options.
        configured = list(entities or ())
        if not configured:
            raise RuntimeError(
                "No ai_task entities configured: choose them in the AI Jukebox "
                "integration's options")
        self._entities = [e for e in configured if hass.states.get(e) is not None]
        if not self._entities:
            self._entities = configured
        self._cursor = 0
        self.calls = 0
        self.last_errors: list[str] = []

    @property
    def entities(self) -> list[str]:
        return list(self._entities)

    async def ask(self, instructions: str, structure: dict, task_name: str) -> dict | None:
        """Returns the `data` dict, or None if every entity failed."""
        order = self._entities[self._cursor:] + self._entities[: self._cursor]
        self._cursor = (self._cursor + 1) % len(self._entities)
        errors: list[str] = []

        for entity_id in order:
            try:
                response = await asyncio.wait_for(
                    self._hass.services.async_call(
                        "ai_task",
                        "generate_data",
                        {
                            "task_name": task_name,
                            "instructions": instructions,
                            "entity_id": entity_id,
                            "structure": structure,
                        },
                        blocking=True,
                        return_response=True,
                    ),
                    timeout=BUILD_CALL_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                errors.append(f"{entity_id}: timeout")
                continue
            except Exception as err:  # noqa: BLE001 - any backend failure means "try the next one"
                errors.append(f"{entity_id}: {type(err).__name__}: {err}")
                continue

            self.calls += 1
            data = response.get("data") if isinstance(response, dict) else None
            if isinstance(data, dict):
                return data
            errors.append(f"{entity_id}: no structured data in response")

        # Kept on the instance as well as logged: this install writes no
        # home-assistant.log, so a warning here is unreadable. The pipeline
        # folds these into the build narration, which IS persisted onto the
        # pool record (store.record_build_log) -- otherwise a failed batch is
        # only ever "RuntimeError" and the actual cause needs a replay to see.
        self.last_errors = list(errors)
        _LOGGER.warning("All ai_task entities failed for %s: %s", task_name, "; ".join(errors))
        return None
