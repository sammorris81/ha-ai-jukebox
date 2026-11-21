"""AI Jukebox media_player platform.

Each configured playlist gets one virtual media_player entity
(`media_player.ai_jukebox_<slug>`) that IS the session — there's no separate
coordinator object bridging to a sensor the way the old ai_playlist
integration split things. The entity owns session lifecycle (start/stop/
attach/detach speakers, generate/enqueue/refill) directly, and reuses stock
`media_player.*` services (play/pause/next/previous/stop) for navigation so
ordinary media-control dashboard cards work with no custom UI.

Speaker state, not our own command bookkeeping, is queue-authority: a
`homeassistant.helpers.event.async_track_state_change_event` listener on
every attached speaker watches for real changes. What a play/pause/stop
seen on a speaker *means* depends on where it came from and how many
speakers are attached:
  - Commands issued *to ai_jukebox itself* (async_media_play/pause/stop)
    already propagate to every attached speaker directly — unaffected by
    anything below.
  - A single-speaker session: that one speaker's own play/pause/stop (from
    any source — ai_jukebox, a card, or directly on the device) directly
    controls our state. A stop ends the session; a pause is mirrored (you
    can still resume it later).
  - A speaker that's one of several attached: a pause or stop on just that
    speaker is assumed to mean *that speaker* wants out, not "pause/stop
    the whole session" — it's detached (unjoined) automatically and the
    rest keep going. No auto-rejoin; getting it back is a manual
    `ai_jukebox.attach_speaker` call.
  - `media_title`/`media_artist` changes on the *primary* speaker
    (self._speakers[0]) drive now_playing/history/refill — whether the
    change came from an explicit next/previous command, the track ending
    naturally, or Music Assistant resolving to something we didn't
    literally ask for. Matching is on `uri` first;
    if the speaker reports no uri, an exact title-and-artist compare is the
    fallback. (An `artists_match()` fuzzy fallback is described in older
    comments — it no longer exists.) If nothing in the tracked
    queue matches at all (autoplay/radio, a hijack, or a search resolving
    to a completely different artist), now_playing is still updated to
    reflect reality — trusting the speaker over our own bookkeeping — but
    the queue/history is left untouched rather than guessed at. This used
    to also feed a per-playlist matched/mismatched counter and flag (see
    project notes from 2026-08-30/2026-08-31); that was removed on
    2026-09-01 after it produced false positives once queue-position
    tracking fell behind reality over a long, many-refill session — there is no
    aggregate quality signal in its place: a background prompt-drift checker
    (_check_drift()) filled that role and went with LLM track generation. Each
    pool track's `why` from build/curate.py is what remains, and it is recorded
    for inspection only.
  - `unavailable`/`unknown` are deliberately never treated as a stop —
    more likely a transient connectivity blip than real intent.

Speaker grouping (2026-08-31): when self._speakers[0] and at least one other
attached speaker both support HA's GROUPING feature, they're physically
joined via the standard `media_player.join` service (translated by whichever
platform backs each entity — Music Assistant, for everything this
integration deals with) and enqueued to just the leader (speakers[0]) —
genuinely synced playback instead of independent replays. speakers[0] stays
the group leader always; this deliberately never elects a different leader
just because it's "more groupable," so "group leader" and "content-authority
primary" are always the same speaker. Any non-groupable attached speaker (or
a groupable one whose join failed) still gets its own independent enqueue —
except a failed join is flagged via speaker_status/last_error rather than
silently falling back to independent play. See _partition_speakers()/
_form_group()/_enqueue_tracks(). ai_jukebox itself also exposes GROUPING
natively (async_join_players/async_unjoin_player, wrapping the same
attach/detach logic) so the standard dashboard media-control card's group UI
works — but ai_jukebox never becomes part of any real MA/speaker group
itself; physical grouping only ever happens speaker-to-speaker.
  - Detaching the group leader is a special case, and its handling changed
    on 2026-09-13: Music Assistant (2.10.2) promotes a surviving member and
    carries the queue across rather than dissolving the group, so
    async_detach_speakers() unjoins the old leader first (before touching
    playback on survivors) and then adopts MA's chosen heir as the new
    speakers[0], re-forming a group and re-enqueueing itself only when MA
    really did dissolve. The earlier finding that unjoining a leader
    dissolves the WHOLE group was verified live on 2026-08-31 against an
    older MA and no longer holds.
  - A speaker leaving a group is never sent a stop (2026-10-18). MA's unjoin
    already silences it, and a playback command aimed at a player MA still
    considers synced is redirected to its sync leader, so the stop silenced
    the whole group instead — waiting for HA's group_members to clear first
    was not enough once the Sonos moved to AirPlay. Only a speaker that was
    playing independently is stopped on detach. See async_detach_speakers()
    for the live sessions this ended.
  - A speaker leaving a group *by its own means* (e.g. that speaker's own
    dashboard card's "leave group" button, which HA routes to its own
    integration — never through ai_jukebox) is detected reactively:
    _async_handle_speaker_update() also watches for a tracked group's
    group_members shrinking, not just a stopped playback state — but only
    for members HA has actually reported inside the group, since a freshly
    formed one reads as empty for a second or so and would otherwise look
    like everybody leaving at once (see _wait_until_grouped()). Which
    speaker left is decided after a short settle, not from the first
    update: when the *leader* leaves, MA bounces its heir through idle and
    that update can arrive first, which read as the heir leaving and ended
    a session on 2026-10-18 (see _detach_after_group_settles()).

Generation is never on the playback critical path (2026-09-08). Both the
cold-start top-up and the steady-state refill run as background tasks
(_cold_start_topup_task / _generation_task, cancelled together by
_cancel_pending_generation() on stop/reset/re-start), so the speaker-state
listener above never waits on an ai_task call. This matters because the
local model is CPU-bound at a few tokens/sec: awaiting a refill inline used
to hold the listener task open for the entire generation, which is how a
hung backend once stalled track-advance reconciliation for over two hours.
_maybe_refill() will not spawn a second batch while one is still in flight,
since state changes arrive as one unserialized task each.
  - The direct cost of that design is a window where MA holds only a seed
    batch, which is why _reset_playback_modes() now also disables MA's
    "Don't stop the music" per queue (2026-09-09). Left on, MA fills that
    window with recommendations of its own, which play as part of the
    playlist and land in history as `untracked` — see
    _disable_dont_stop_the_music(). It is on by default for every player on
    the server, so this has to be turned off explicitly, after every
    enqueue, on every speaker this integration touches.

Restart-resurrection: a session's speakers/now_playing/played_count are
write-through persisted to disk (PlaylistStore.save_session(), distinct
from the in-memory-only `_suspended` snapshot used for a same-process
integration reload — see async_suspend()/_resume_from_suspend()) any time
they change. On a real HA restart, async_added_to_hass() finds no in-memory
suspend snapshot, so it instead checks that disk record and, if present,
calls _resume_from_restart() once HA has finished starting (so other
integrations' entities, e.g. the speaker itself, are actually available).
That method never blindly trusts the disk record or issues a play/pause
command — a restart can take long enough that reality has moved on — it
only reattaches if the primary speaker is *currently* found in a playing/
paused state, then reconciles our bookkeeping against whatever it's
actually playing (same machinery _reconcile_now_playing() uses live). If
the speaker's gone, off, or idle, the session is treated as lost and the
disk record is cleared rather than resurrected.

Music Assistant pools. Taking generation off the critical path bounded the
damage a slow model can do, but playback still depended on it. A pool removes
that dependency entirely: the pool is now the *only* source of playback
tracks. `build/pipeline.py` constructs it (roster -> albums -> per-track
selection with a `why` for each pick), it is persisted to
.storage/ai_jukebox/pool/<slug>.json, and _sample_from_pool() draws every
track from it at playback time. No AI call sits between a track ending and
the next one starting.

Rebuilds are explicit, via the ai_jukebox.rebuild_pool service — there is no
background growth. Editing a playlist's prompt therefore changes nothing
until its pool is rebuilt.

Corrected 2026-09-27: this paragraph previously described a `pool_enabled`
opt-in defaulting to off, playback running off self._queue with the pool only
"accumulating", and an async_grow_pool()/_mirror_to_pool()/_pool_add_tracks()
growth subsystem fed by an ai_jukebox.grow_pool service. None of that exists
— the service was removed (docs/implementation-notes.md section E4), those
functions are gone, and _pool_enabled is a hardcoded True. The stale text
caused two wrong diagnoses before it was caught.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
from datetime import timedelta

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from . import ma_playlist
from .const import (
    ACTIVITY_ENQUEUING,
    ACTIVITY_ERROR,
    ACTIVITY_IDLE,
    ACTIVITY_PAUSED,
    ACTIVITY_PLAYING,
    ACTIVITY_REFILLING,
    ACTIVITY_SPEAKER_JOIN,
    ALBUM_REPEAT_K,
    MA_OUTAGE_RESUME_SECONDS,
    MA_RECOVERY_SPEAKER_WAIT,
    BUILD_LOG_MAX_LINES,
    CONF_MANAGED_SPEAKERS,
    CONF_REBUILD_END_HOUR,
    CONF_REBUILD_START_HOUR,
    DEFAULT_REBUILD_END_HOUR,
    DEFAULT_REBUILD_START_HOUR,
    SUBENTRY_PLAYLIST,
    DEFAULT_BATCH_SIZE,
    DEFAULT_REFILL_THRESHOLD,
    DETACH_POLL_INTERVAL,
    DETACH_TRANSFER_TIMEOUT,
    DOMAIN,
    GROUP_SETTLE_TIMEOUT,
    GROUP_SHRINK_SETTLE,
    POOL_SAMPLE_MAX,
    POOL_SAMPLE_MIN,
    POOL_SAMPLE_WHILE_BUILDING,
    RECENCY_FLOOR_EXP,
    RECENCY_FULL_HOURS,
    SEED_BOOST_CORRECTION,
    SEED_TARGET_SHARE,
    REPEAT_WINDOW_HOURS,
    WEIGHT_ALPHA,
)
from .store import PlaylistStore
from .track_processing import track_dict_to_string

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one virtual media_player per playlist subentry."""
    hass.data[DOMAIN]["media_player_add_entities"] = async_add_entities
    hass.data[DOMAIN].setdefault("players", {})

    for subentry in entry.get_subentries_of_type(SUBENTRY_PLAYLIST):
        get_or_create_player(hass, entry, dict(subentry.data), subentry.subentry_id)


def get_or_create_player(
    hass: HomeAssistant,
    entry: ConfigEntry,
    playlist_config: dict,
    subentry_id: str | None = None,
) -> "AiJukeboxPlayer | None":
    """Get the existing entity for a playlist, or create it if new.

    `subentry_id` attaches the entity (and its device) to the playlist's
    config subentry, which is what gives the device page its Configure button.
    """
    slug = PlaylistStore.playlist_slug(playlist_config["name"])
    players = hass.data[DOMAIN].setdefault("players", {})
    if slug in players:
        # An existing entity has to *adopt* the edited definition, not just
        # be handed back. Without this, save_playlist updated the store
        # while the live entity kept whatever config it was constructed
        # with, so an edited prompt/batch_size/artists list
        # appeared to save but had no effect until the next
        # restart, with the entity's own attributes still reporting the old
        # values.
        player = players[slug]
        player.async_adopt_config(playlist_config)
        return player

    add_entities = hass.data[DOMAIN].get("media_player_add_entities")
    if add_entities is None:
        _LOGGER.warning("media_player platform not ready, cannot create entity for %s", slug)
        return None

    store: PlaylistStore = hass.data[DOMAIN]["store"]
    player = AiJukeboxPlayer(hass, store, playlist_config)
    players[slug] = player
    add_entities([player], config_subentry_id=subentry_id)
    return player


def get_player(hass: HomeAssistant, slug: str) -> "AiJukeboxPlayer | None":
    """Look up an existing playlist entity by slug (no create)."""
    return hass.data.get(DOMAIN, {}).get("players", {}).get(slug)


def in_rebuild_window(hour: int, start: int, end: int) -> bool:
    """Whether `hour` falls in [start, end), wrapping past midnight."""
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


class AiJukeboxPlayer(MediaPlayerEntity, RestoreEntity):
    """One AI-generated playlist session, modeled as a virtual media_player."""

    # The entity is its device's main feature, so it takes the device's name
    # outright (name None). With has_entity_name False, HA 2026.11 still
    # prefixed the device name, and every friendly_name came out doubled:
    # "The Orchestral Arena AI Jukebox The Orchestral Arena".
    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_media_content_type = MediaType.MUSIC

    def __init__(
        self,
        hass: HomeAssistant,
        store: PlaylistStore,
        playlist_config: dict,
    ) -> None:
        self.hass = hass
        self._store = store
        self._playlist_config = playlist_config
        self._playlist_name = playlist_config["name"]

        slug = PlaylistStore.playlist_slug(self._playlist_name)
        self._slug = slug
        self._attr_unique_id = f"ai_jukebox_{slug}"
        # A slug may keep a hyphen ("country-pop_storytellers"); an entity_id
        # may not, and HA warned on every start while rewriting it to the
        # underscore form the registry already holds. Same normalisation as
        # _get_player_or_raise() in __init__.py.
        self.entity_id = f"media_player.ai_jukebox_{slug.replace('-', '_')}"
        # One device per playlist, keyed on the slug rather than the subentry
        # id: entity_id, unique_id and the queue/pool files are all keyed on
        # the slug, so the device is too. The device name is what the entity's
        # friendly_name becomes, and it keeps the "AI Jukebox " prefix that
        # friendly names have always had -- dashboards and scripts match on it.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, slug)},
            name=f"AI Jukebox {self._playlist_name}",
            manufacturer="AI Jukebox",
            model="Playlist",
            entry_type=DeviceEntryType.SERVICE,
        )

        self._state = MediaPlayerState.IDLE
        self._activity = ACTIVITY_IDLE
        self._speakers: list[str] = []
        self._queue: list[dict] = []
        self._now_playing: dict | None = None
        self._last_prompt: str | None = None
        self._played_count = 0
        self._last_error: str | None = None
        self._last_activity_at: str | None = None
        self._consecutive_failures = 0
        self._registered = False
        self._session_lock = asyncio.Lock()
        # The backgrounded cold-start top-up (see async_session_start) —
        # tracked so async_session_stop can cancel it before it lands, in
        # case a stop follows close behind a start while it's still
        # in-flight. Otherwise it could still complete after a subsequent
        # session has already started and mutate that new session's
        # self._queue out from under it.
        self._cold_start_topup_task: asyncio.Task | None = None
        # The backgrounded steady-state refill (see _maybe_refill) — the same
        # deal as the cold-start top-up above, just triggered by the queue
        # draining to refill_threshold rather than by a fresh start. Both are
        # cancelled together by _cancel_pending_generation().
        self._generation_task: asyncio.Task | None = None
        # Handle for a pool *build* (ai_jukebox.rebuild_pool). Kept apart
        # from the two generation handles above on purpose: those belong to a
        # session and are cancelled with it, while a build is maintenance
        # that outlives any session and is only cancelled on teardown.
        self._pool_build_task: asyncio.Task | None = None
        # Set when every Music Assistant player goes unavailable at once, so
        # the `idle` they recover into is read as recovery rather than a stop.
        self._ma_outage_at: float | None = None
        self._ma_resume_task: asyncio.Task | None = None
        # Held from the first player returning until the resume has run, so
        # the half-recovered speaker states in between (one back, the rest
        # still unavailable) are not read as speakers leaving. See
        # _resume_after_ma_outage().
        self._ma_recovering = False
        # Cached mirror of the pool file's summary fields. extra_state_attributes
        # is a sync property and can't read the store, so pool stats are
        # refreshed alongside every write instead of read on demand.
        self._pool_stats: dict = {}
        self._unsub_speaker_listener: CALLBACK_TYPE | None = None
        self._group_join_failed: list[str] = []
        # Speakers HA has actually *reported* inside the primary's group, as
        # opposed to ones we asked Music Assistant to join. Only a speaker in
        # here can be seen to leave the group: see _wait_until_grouped() for
        # the lag that makes an unconfirmed group read meaningless.
        self._grouped_seen: set[str] = set()
        # Detach-only, deliberately invisible counterpart to
        # ACTIVITY_SPEAKER_JOIN — see _busy_reason().
        self._regrouping = False

    # --- Entity plumbing ---

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # An *integration reload* (same process, seconds not minutes) is
        # handled explicitly: __init__.py stashes a suspend snapshot in
        # hass.data across the reload, and if one exists for us here, restore
        # it before going live. A real HA restart wipes hass.data entirely,
        # so it falls through to the disk-persisted session record instead
        # (see module docstring / _resume_from_restart()).
        await self._refresh_pool_stats()
        suspended = self.hass.data.get(DOMAIN, {}).get("_suspended", {})
        snapshot = suspended.pop(self._slug, None)
        if snapshot:
            await self._resume_from_suspend(snapshot)
        else:
            session = await self._store.async_get_session(self._playlist_name)
            if session:
                if self.hass.is_running:
                    await self._resume_from_restart(session)
                else:
                    self.hass.bus.async_listen_once(
                        EVENT_HOMEASSISTANT_STARTED, self._on_ha_started_resume(session)
                    )
        self._registered = True
        self._resubscribe_speakers()
        self._safe_write_state()

    def _on_ha_started_resume(self, session: dict):
        async def _listener(_event: Event) -> None:
            await self._resume_from_restart(session)
            self._resubscribe_speakers()
            self._safe_write_state()

        return _listener

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_speaker_listener:
            self._unsub_speaker_listener()
            self._unsub_speaker_listener = None
        # Refills run as background tasks now, so one can easily still be
        # in flight when this entity goes away (an integration reload, most
        # likely). Left alone it would keep generating against a dead entity
        # and write to the queue store behind the replacement's back.
        self._registered = False
        await self._cancel_pending_generation()
        # A pool *build* deliberately survives stop/reset, but not the entity
        # itself going away — same reasoning as the refills above: it would
        # otherwise keep writing to the pool store behind the replacement
        # entity's back. (There is no background "pool growth"; builds are
        # explicit, via ai_jukebox.rebuild_pool.)
        grow, self._pool_build_task = self._pool_build_task, None
        if grow and not grow.done():
            grow.cancel()
            try:
                await grow
            except asyncio.CancelledError:
                pass

    def _safe_write_state(self) -> None:
        if self._registered:
            self.async_write_ha_state()

    def _touch(self) -> None:
        self._last_activity_at = dt_util.utcnow().isoformat()

    def _resubscribe_speakers(self) -> None:
        """(Re)point the state-change listener at all attached speakers.

        Called any time self._speakers changes — start/stop/attach/detach/
        reset/resume. Every attached speaker is watched for a genuine stop
        (see _STOPPED_STATES); only the primary (self._speakers[0]) drives
        track-advance/desync reconciliation — see the module docstring for
        why multi-speaker content drift isn't handled yet.
        """
        if self._unsub_speaker_listener:
            self._unsub_speaker_listener()
            self._unsub_speaker_listener = None
        if self._speakers:
            self._unsub_speaker_listener = async_track_state_change_event(
                self.hass, self._speakers, self._on_speaker_state_change
            )

    @callback
    def _on_speaker_state_change(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        self.hass.async_create_task(self._async_handle_speaker_update(new_state))

    # "standby" as a plain string: MediaPlayerState.STANDBY is deprecated
    # (HA warns on every import), but a speaker platform may still report it.
    _STOPPED_STATES = (MediaPlayerState.IDLE, MediaPlayerState.OFF, "standby")
    _ACTIVE_STATES = (MediaPlayerState.PLAYING, MediaPlayerState.PAUSED, MediaPlayerState.BUFFERING)
    # Generating/enqueuing are the two windows where a queue is actively
    # being built against Music Assistant — a manual play/pause issued to
    # the raw speaker mid-build would race the replace/add calls in
    # _enqueue_tracks() rather than being queued behind anything, since
    # async_media_play/pause() don't take _session_lock. Guarded explicitly
    # rather than silently racing.
    _BUSY_ACTIVITIES = (ACTIVITY_REFILLING, ACTIVITY_ENQUEUING, ACTIVITY_SPEAKER_JOIN)

    def _all_ma_players_unavailable(self) -> bool:
        """True when every media_player from the music_assistant integration
        is unavailable — the signature of MA itself dropping rather than one
        speaker misbehaving. Verified against a real outage: all 34 MA
        entities went unavailable together while androidtv_remote players and
        every other domain were untouched.
        """
        registry = er.async_get(self.hass)
        ma_players = [
            entry.entity_id for entry in registry.entities.values()
            if entry.domain == "media_player" and entry.platform == "music_assistant"
        ]
        if not ma_players:
            return False
        for entity_id in ma_players:
            state = self.hass.states.get(entity_id)
            if state is not None and state.state != STATE_UNAVAILABLE:
                return False
        return True

    async def _resume_after_ma_outage(self) -> None:
        """Re-enqueue and resume what was playing before MA dropped.

        MA loses its queues across a restart, so simply ignoring the outage
        would leave the session claiming to play with nothing actually
        playing. The queue is ours and survives, so it is re-enqueued whole.

        Runs with _ma_recovering set (by the listener, before this task is
        created) so speaker updates are ignored until it finishes. Without
        that, on 2026-11-08 sonos_d's first update after an MA
        restart showed it out of sonos_a's group, and the
        group-shrink rule detached the still-unavailable leader 2s later,
        racing this resume.

        Waits for the session's own speakers rather than trusting that the
        outage ending means they are back (see MA_RECOVERY_SPEAKER_WAIT).
        Any still unavailable after the wait are dropped from the session
        without unjoin/stop calls, which HA would skip for an unavailable
        entity anyway; they can be re-attached once they return.
        """
        try:
            if not self._speakers or not self._queue:
                return
            back = await self._wait_for_speakers_available(MA_RECOVERY_SPEAKER_WAIT)
            if not self._speakers or not self._queue:
                return  # stopped or reset during the wait
            if not back:
                _LOGGER.warning(
                    "Music Assistant recovered but none of %s came back within "
                    "%ss — ending '%s'",
                    self._speakers, MA_RECOVERY_SPEAKER_WAIT, self._playlist_name,
                )
                await self.async_session_stop()
                self._last_error = (
                    "Music Assistant recovered without any of this session's speakers"
                )
                self._safe_write_state()
                return
            gone = [s for s in self._speakers if s not in back]
            if gone:
                _LOGGER.warning(
                    "%s still unavailable %ss after Music Assistant recovered — "
                    "resuming '%s' without them",
                    gone, MA_RECOVERY_SPEAKER_WAIT, self._playlist_name,
                )
                self._speakers = back
                self._grouped_seen -= set(gone)
                self._resubscribe_speakers()
                await self._persist_session()
            _LOGGER.info(
                "Music Assistant recovered — re-enqueuing %d track(s) for '%s' on %s",
                len(self._queue), self._playlist_name, self._speakers,
            )
            ok = await self._enqueue_tracks(self._queue, clear_first=True)
            # After the enqueue, not before: a "replace" carries the previous
            # queue's modes over (see _reset_playback_modes()).
            await self._reset_playback_modes(self._speakers)
            if ok:
                self._state = MediaPlayerState.PLAYING
                self._activity = ACTIVITY_PLAYING
                self._last_error = None
                await self._reconcile_with_primary()
        except Exception as err:  # noqa: BLE001 - never let recovery raise into the listener
            self._last_error = f"Resume after Music Assistant outage failed: {err}"
            _LOGGER.error("Resume after MA outage failed for '%s': %s",
                          self._playlist_name, err)
        finally:
            self._ma_recovering = False
        self._touch()
        self._safe_write_state()

    def _is_available(self, entity_id: str) -> bool:
        state = self.hass.states.get(entity_id)
        return state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)

    async def _wait_for_speakers_available(self, timeout: float) -> list[str]:
        """The attached speakers that are available, once all of them are or
        `timeout` seconds have passed, whichever comes first."""
        deadline = self.hass.loop.time() + timeout
        while True:
            back = [s for s in self._speakers if self._is_available(s)]
            if len(back) == len(self._speakers) or self.hass.loop.time() >= deadline:
                return back
            await asyncio.sleep(DETACH_POLL_INTERVAL)

    async def _async_handle_speaker_update(self, new_state) -> None:
        """React to an attached speaker's own state. Three distinct rules,
        by (source of the command) x (how many speakers are attached):

          1. Commands issued *to ai_jukebox itself* (async_media_play/pause/
             stop) already propagate to every attached speaker — that's
             unaffected by anything here, see those methods directly.
          2. A single-speaker session: that one speaker's own play/pause/
             stop (from any source — ai_jukebox, a card, or directly on the
             device) directly controls our state. Mirrored, not detached —
             you can resume it later.
          3. A speaker that's one of several attached: a pause or stop on
             just that speaker (from any source) is assumed to mean that
             speaker wants out, not "pause/stop the whole session" — it's
             detached (unjoined) automatically, the rest continue
             unaffected. No auto-rejoin; re-attaching is a manual
             ai_jukebox.attach_speaker call.
          4. The primary's group growing from outside ai_jukebox (grouped
             natively in the Sonos app, or in Music Assistant directly) is
             the mirror image of 3: any managed speaker (the managed_speakers option) that shows
             up in the primary's group_members without having been attached
             through us gets auto-attached, same as a manual attach_speaker
             call. Unmanaged speakers are left alone.

        `unavailable`/`unknown` are deliberately not treated as a stop —
        more likely a transient connectivity blip than real intent.
        """
        if new_state.state == STATE_UNAVAILABLE:
            # Record an MA-wide outage so the `idle` on the way back is not
            # mistaken for intent. A single speaker going unavailable is left
            # to the existing handling, which ignores it as transient.
            if self._ma_outage_at is None and self._all_ma_players_unavailable():
                self._ma_outage_at = self.hass.loop.time()
                _LOGGER.warning(
                    "All Music Assistant players are unavailable — holding "
                    "'%s' open to resume when they return", self._playlist_name,
                )
            return

        if self._ma_outage_at is not None:
            age = self.hass.loop.time() - self._ma_outage_at
            if age > MA_OUTAGE_RESUME_SECONDS:
                self._ma_outage_at = None          # too old to attribute
            elif not self._all_ma_players_unavailable():
                # Players are back. Swallow this state change (it is the idle
                # they recover into) and resume once, not once per speaker.
                self._ma_outage_at = None
                if self._ma_resume_task is None or self._ma_resume_task.done():
                    self._ma_recovering = True
                    self._ma_resume_task = self.hass.async_create_task(
                        self._resume_after_ma_outage()
                    )
                return

        if self._busy_reason:
            # Don't react to our own in-flight enqueue calls — but note this
            # means the state change caused by the *first* track of a fresh
            # enqueue (which happens mid-loop, while still "enqueuing") is
            # deliberately swallowed here. async_session_start() reads the
            # speaker's state directly right after enqueuing finishes to
            # cover exactly that case — see _reconcile_now_playing().
            return
        if not self._speakers:
            return

        primary = self._speakers[0]
        if new_state.entity_id == primary and self._supports_grouping(primary):
            # The primary's own group_members growing means something joined
            # its Sonos/MA group from outside ai_jukebox entirely (the Sonos
            # app, or grouping directly in Music Assistant) — pull it into
            # the session the same way an explicit attach would. Restricted
            # to the speakers chosen in the integration's options: an
            # unrelated/guest speaker joining the primary's group elsewhere
            # shouldn't get silently absorbed into a jukebox session.
            managed = self.hass.data[DOMAIN].get(CONF_MANAGED_SPEAKERS) or frozenset()
            live_members = set(new_state.attributes.get("group_members", []) or [])
            new_members = (live_members - set(self._speakers)) & managed
            if new_members:
                await self.async_attach_speakers(sorted(new_members))
                return

        leader, members, _ = self._partition_speakers(self._speakers)
        if leader and new_state.entity_id in (leader, *members):
            # A grouped speaker's own group_members can shrink without its
            # playback state ever going through _STOPPED_STATES below —
            # e.g. someone hits "leave group" on that speaker's OWN card,
            # which HA routes straight to its own integration
            # (music_assistant), never through ai_jukebox at all. Speaker
            # state is queue-authority here just like everywhere else in
            # this listener, so detect the departure from the resulting
            # group_members change instead of requiring unjoin to go
            # through us.
            #
            # Only a speaker HA has already reported *inside* this group can
            # be seen to leave it. Without that requirement the lag between
            # media_player.join returning and the group appearing in
            # group_members reads as "every member left at once", and the
            # detach that follows takes the group's audio down with it — how
            # joining sonos_d killed a playing sonos_b session on
            # 2026-09-13 at 15:26:52. See _wait_until_grouped().
            #
            # Which speaker left is decided once the group has settled, not
            # from this one update — see _detach_after_group_settles().
            live_members = set(new_state.attributes.get("group_members", []) or [])
            if new_state.entity_id == leader:
                self._grouped_seen |= live_members & set(members)
                shrunk = bool((set(members) & self._grouped_seen) - live_members)
            else:
                shrunk = leader not in live_members and new_state.entity_id in self._grouped_seen
            if shrunk:
                # Set before the first await, so updates arriving during the
                # settle are swallowed by _busy_reason rather than each
                # starting a decision of their own.
                self._regrouping = True
                await self._detach_after_group_settles()
                return

        if new_state.state in self._STOPPED_STATES:
            # Always detach just this speaker. If it's the last one,
            # async_detach_speakers() already cascades into a full session
            # stop — single- and multi-speaker sessions fall out of the
            # same one rule.
            await self.async_detach_speakers([new_state.entity_id])
            return

        if new_state.state not in self._ACTIVE_STATES:
            return  # unavailable/unknown — not a stop signal

        if new_state.state == MediaPlayerState.PAUSED and len(self._speakers) > 1:
            await self.async_detach_speakers([new_state.entity_id])
            return

        if len(self._speakers) == 1:
            target = (
                MediaPlayerState.PAUSED if new_state.state == MediaPlayerState.PAUSED
                else MediaPlayerState.PLAYING
            )
            if self._state != target:
                self._state = target
                if self._activity in (ACTIVITY_PLAYING, ACTIVITY_PAUSED):
                    self._activity = (
                        ACTIVITY_PAUSED if target == MediaPlayerState.PAUSED else ACTIVITY_PLAYING
                    )
                self._touch()
                self._safe_write_state()

        if new_state.entity_id != self._speakers[0]:
            return  # only the primary drives track-advance/desync reconciliation

        new_title = new_state.attributes.get("media_title")
        new_artist = new_state.attributes.get("media_artist")
        new_uri = new_state.attributes.get("media_content_id")
        if not new_title or not new_artist:
            return
        new_album = new_state.attributes.get("media_album_name")

        changed = await self._reconcile_now_playing(new_title, new_artist, new_album, uri=new_uri)
        if changed:
            self._touch()
            self._safe_write_state()

    async def _reconcile_with_primary(self, skip_refill: bool = False) -> bool:
        """Read back what the primary actually started after one of our own
        enqueues. Returns False when it reports no title/artist to go on.

        Every enqueue of ours runs under a busy guard (ENQUEUING, or
        _regrouping on a detach), so the state change it causes is swallowed
        by _async_handle_speaker_update — this direct read is the only place
        that catches a first-track mismatch.

        The uri goes with it. Without it this matched on title alone, which
        section 4 explicitly does not rely on, and Music Assistant decorates
        plenty of titles: the pool holds "Wishing You Were Here" while the
        speaker reports "Wishing You Were Here (Remastered)". The first track
        of a session was landing as `untracked` on that mismatch -- no reason
        shown, and with no uri to exclude itself by, sampling then drew the
        same track straight back into the queue it was already playing from.
        """
        state = self._primary_state
        title = state.attributes.get("media_title") if state else None
        artist = state.attributes.get("media_artist") if state else None
        if not (title and artist):
            return False
        await self._reconcile_now_playing(
            title, artist, state.attributes.get("media_album_name"),
            skip_refill=skip_refill,
            uri=state.attributes.get("media_content_id"),
        )
        return True

    async def _reconcile_now_playing(
        self, new_title: str, new_artist: str, new_album: str | None,
        skip_refill: bool = False, uri: str | None = None
    ) -> bool:
        """Update now_playing/queue/history from what a speaker is actually
        playing. Single source of truth for "what's really playing," used
        both by the live state-change listener and right after our own
        initial enqueue (see async_session_start). Returns False if this was
        a no-op (already reflected).

        `skip_refill=True` suppresses the normal threshold-triggered
        _maybe_refill() call below — used only by a fresh cold start, which
        schedules its own unconditional follow-up batch right after this
        returns (see async_session_start) and would otherwise risk both
        running concurrently against the same self._queue.
        """
        previous = self._now_playing
        if previous and new_title == previous.get("title") and new_artist == previous.get("artist"):
            return False  # no real change

        # Match on the Music Assistant item uri. Pool tracks carry the exact
        # uri that was enqueued and every speaker platform here reports it
        # back in media_content_id (verified on Sonos, HomePod and Nest), so
        # this is an identity check rather than a guess. Title/artist remain
        # as a fallback for a speaker that omits the uri.
        match_index = None
        if uri:
            match_index = next(
                (i for i, t in enumerate(self._queue) if t.get("uri") == uri), None
            )
        if match_index is None:
            match_index = next(
                (
                    i for i, track in enumerate(self._queue)
                    if track.get("title") == new_title and track.get("artist") == new_artist
                ),
                None,
            )

        if match_index is not None:
            # Expected advance — possibly skipping ahead past more than one
            # queued track. Move everything up to and including the match
            # out of the live queue and into history.
            matched_track = self._queue[match_index]
            skipped = self._queue[:match_index]
            # Display fields come from what the speaker actually resolved to;
            # the identity fields come from the matched pool entry, because
            # those are what sampling weights on and the speaker does not
            # report them.
            self._now_playing = {
                "artist": new_artist, "title": new_title, "album": new_album,
                "source": matched_track.get("source"),
                "uri": matched_track.get("uri") or uri,
                "artist_id": matched_track.get("artist_id"),
                "album_id": matched_track.get("album_id"),
                # The builder's one-line reason this track is on the playlist.
                # Display only -- absent on pools built before it existed, and
                # on anything the untracked branch below reports.
                "why": matched_track.get("why"),
            }
            self._queue = self._queue[match_index + 1:]
            # Recorded when a track *starts*, which is what the recency decay
            # measures from. Skipped entries are recorded too: they were
            # served, and re-offering them immediately would feel like a bug.
            stamp = dt_util.utcnow().isoformat()
            for track in [matched_track, *skipped]:
                await self._store.async_record_play(self._playlist_name, track, stamp)
            self._played_count += 1 + len(skipped)
            await self._persist_queue()
            await self._persist_session()
            self._activity = ACTIVITY_PLAYING if self._state == MediaPlayerState.PLAYING else self._activity
            if not skip_refill:
                await self._maybe_refill()
        else:
            # Nothing in our tracked queue matches at all — autoplay/radio,
            # a hijack, or a search resolving to a completely different
            # artist than anything we queued. Trust reality for now_playing
            # rather than guessing, and don't try to auto-heal the queue,
            # but still record what was actually playing before this, so the
            # plays log stays an accurate record of everything heard and
            # sampling's recency weighting accounts for it. Marked
            # `untracked`: content this playlist never queued (a hijack, or an
            # external group-join).
            if previous:
                await self._store.async_record_play(
                    self._playlist_name, previous, dt_util.utcnow().isoformat()
                )
                self._played_count += 1
            self._now_playing = {
                "artist": new_artist, "title": new_title, "album": new_album, "source": "untracked",
            }
            await self._persist_session()
        return True

    # --- Speaker grouping ---
    #
    # The group leader is always self._speakers[0] — the same speaker
    # already used everywhere else as this session's content-authority
    # primary (track-advance/desync reconciliation above). A physical
    # group only forms if it AND at least one other attached speaker both
    # support grouping; if speakers[0] itself doesn't, no group forms this
    # session, full stop — deliberately simple rather than trying to elect
    # a different leader, so "group leader" and "content-authority
    # primary" never diverge. See media_player.py module docstring for the
    # full design writeup.

    def _supports_grouping(self, entity_id: str) -> bool:
        state = self.hass.states.get(entity_id)
        if not state:
            return False
        features = state.attributes.get("supported_features", 0)
        return bool(features & MediaPlayerEntityFeature.GROUPING)

    def _live_group_members(self, entity_id: str) -> list[str]:
        state = self.hass.states.get(entity_id)
        if not state:
            return []
        return list(state.attributes.get("group_members", []) or [])

    def _partition_speakers(self, speakers: list[str]) -> tuple[str | None, list[str], list[str]]:
        """Split `speakers` into (group_leader, group_members, independent).

        group_leader is speakers[0] if it supports grouping and at least
        one other attached speaker does too, else None (and everything
        falls into `independent`, unchanged from before grouping existed).
        """
        if len(speakers) < 2 or not self._supports_grouping(speakers[0]):
            return None, [], list(speakers)
        members = [s for s in speakers[1:] if self._supports_grouping(s)]
        if not members:
            return None, [], list(speakers)
        independent = [s for s in speakers[1:] if s not in members]
        return speakers[0], members, independent

    async def _form_group(self, leader: str, members: list[str]) -> bool:
        """Join `members` to `leader` via the standard media_player.join
        service — translated by whichever platform backs each entity (MA,
        for everything this integration deals with). Returns False on
        failure; callers must not fall back to an independent enqueue for
        `members` on failure — explicit design decision, since silently
        giving a speaker its own copy of the queue after a failed group
        join risks confusing double-audio rather than a clean, visible
        failure. Tracks failed members in self._group_join_failed so
        speaker_status can surface it, clearing them first so a
        successful retry un-flags them.
        """
        self._group_join_failed = [m for m in self._group_join_failed if m not in members]
        try:
            await self.hass.services.async_call(
                "media_player", "join", {"group_members": members},
                target={"entity_id": leader}, blocking=True,
            )
            return True
        except Exception as err:
            _LOGGER.warning(
                "Failed to group %s under %s for '%s': %s",
                members, leader, self._playlist_name, err,
            )
            self._group_join_failed.extend(members)
            return False

    def _speaker_status(self) -> dict[str, str]:
        leader, members, independent = self._partition_speakers(self._speakers)
        status: dict[str, str] = {}
        if leader:
            status[leader] = "synced"
            for m in members:
                status[m] = "join_failed" if m in self._group_join_failed else "synced"
        for s in independent:
            status[s] = "independent"
        return status

    @property
    def state(self) -> MediaPlayerState:
        return self._state

    @property
    def playlist_name(self) -> str:
        return self._playlist_name

    @property
    def icon(self) -> str:
        return "mdi:record-player"

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        return (
            MediaPlayerEntityFeature.PLAY
            | MediaPlayerEntityFeature.PAUSE
            | MediaPlayerEntityFeature.STOP
            | MediaPlayerEntityFeature.NEXT_TRACK
            | MediaPlayerEntityFeature.PREVIOUS_TRACK
            | MediaPlayerEntityFeature.GROUPING
            | MediaPlayerEntityFeature.VOLUME_SET
            | MediaPlayerEntityFeature.VOLUME_MUTE
        )

    @property
    def _busy_reason(self) -> str | None:
        """What, if anything, makes it unsafe right now to act on speaker
        state or to take a transport command.

        `activity` carries the busy states worth showing on a dashboard.
        _regrouping is the same guard without the display: a detach spends a
        few seconds in blocking unjoin/stop calls and group-settle waits, and
        borrowing ACTIVITY_SPEAKER_JOIN to cover it meant unjoining a speaker
        read as "Speaker Join" on the card for three seconds (reported
        2026-09-13). Attaching still says so out loud, because there the
        label is the truth.
        """
        if self._regrouping:
            return "regrouping speakers"
        if self._ma_recovering:
            return "recovering from a Music Assistant outage"
        if self._activity in self._BUSY_ACTIVITIES:
            return self._activity
        return None

    @property
    def _primary_state(self):
        """Live state object of the primary attached speaker, or None. Read
        through at access time rather than cached/mirrored, so these never
        drift from what the real device is actually reporting.
        """
        if not self._speakers:
            return None
        return self.hass.states.get(self._speakers[0])

    @property
    def group_members(self) -> list[str]:
        """This is a purely cosmetic, HA-attribute-level list (e.g. for a
        dashboard media-control card's group UI) — ai_jukebox itself never
        becomes part of any real Music Assistant/speaker group. Physical
        grouping only ever happens speaker-to-speaker, see
        _partition_speakers()/_form_group().
        """
        return [self.entity_id, *self._speakers] if self._speakers else []

    @property
    def media_title(self) -> str | None:
        return (self._now_playing or {}).get("title")

    @property
    def media_artist(self) -> str | None:
        return (self._now_playing or {}).get("artist")

    @property
    def media_album_name(self) -> str | None:
        return (self._now_playing or {}).get("album") or None

    @property
    def entity_picture(self) -> str | None:
        state = self._primary_state
        return state.attributes.get("entity_picture") if state else None

    @property
    def media_duration(self) -> int | None:
        state = self._primary_state
        return state.attributes.get("media_duration") if state else None

    @property
    def media_position(self) -> float | None:
        state = self._primary_state
        return state.attributes.get("media_position") if state else None

    @property
    def media_position_updated_at(self):
        state = self._primary_state
        return state.attributes.get("media_position_updated_at") if state else None

    @property
    def volume_level(self) -> float | None:
        state = self._primary_state
        return state.attributes.get("volume_level") if state else None

    @property
    def is_volume_muted(self) -> bool | None:
        state = self._primary_state
        return state.attributes.get("is_volume_muted") if state else None

    def _build_instructions(self) -> str:
        """The composed instruction the builder would send for this playlist.

        Imported locally, like PoolBuilder in async_rebuild_pool: `build/` is
        only needed when a build runs or its input is being reported, and this
        module deliberately does not depend on it at import time.
        """
        try:
            from .build.pipeline import build_instructions
            return build_instructions(self._playlist_config)
        except Exception:  # never let an attribute read break the entity
            _LOGGER.debug("Could not compose build instructions", exc_info=True)
            return ""

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "prompt": (self._playlist_config.get("prompt") or ""),
            "last_prompt": self._last_prompt,
            "tags": self._playlist_config.get("tags", []),
            "artists": self._playlist_config.get("artists", []),
            "exclude_artists": self._playlist_config.get("exclude_artists", []),
            "genres": self._playlist_config.get("genres", []),
            "exclude_genres": self._playlist_config.get("exclude_genres", []),
            # What the builder actually tells the model: the description plus one
            # line per policy field. `prompt` alone stopped answering that once
            # policy moved into fields, and nothing else exposes the composition.
            "build_instructions": self._build_instructions(),
            "activity": self._activity,
            "last_error": self._last_error,
            "last_activity_at": self._last_activity_at,
            "batch_size": self._playlist_config.get("batch_size", DEFAULT_BATCH_SIZE),
            "refill_threshold": self._playlist_config.get(
                "refill_threshold", DEFAULT_REFILL_THRESHOLD
            ),
            "speakers": list(self._speakers),
            "primary_speaker": self._speakers[0] if self._speakers else None,
            "speaker_status": self._speaker_status(),
            "now_playing": self._now_playing,
            "up_next": self._up_next(),
            "queue_length": len(self._queue),
            "played_count": self._played_count,
            "pool_enabled": self._pool_enabled,
            "pool_size": self._pool_stats.get("size", 0),
            # A stale pool has no other user-visible symptom, so the build
            # timestamp is surfaced to alert against. `pool_building` is what
            # tells you a rebuild is in flight and the pool is still filling.
            "pool_built_at": self._pool_stats.get("built_at"),
            "pool_building": self._pool_stats.get("building", False),
            "pool_missing_artists": self._pool_stats.get("failed_artists", []),
            "pool_last_build": self._pool_stats.get("build_summary"),
        }

    def _up_next(self) -> dict | None:
        if not self._queue:
            return None
        track = self._queue[0]
        return {
            "artist": track.get("artist"),
            "title": track.get("title"),
            "source": track.get("source"),
            "why": track.get("why"),
        }

    # --- Standard media_player controls ---

    async def async_media_play(self) -> None:
        if not self._speakers:
            raise HomeAssistantError(
                f"'{self._playlist_name}' has no speakers attached — call "
                f"ai_jukebox.start first"
            )
        if busy := self._busy_reason:
            raise HomeAssistantError(
                f"'{self._playlist_name}' is still {busy} — try again in a moment"
            )
        await self.hass.services.async_call(
            "media_player", "media_play", {}, target={"entity_id": self._speakers}, blocking=True
        )
        self._state = MediaPlayerState.PLAYING
        self._activity = ACTIVITY_PLAYING
        self._touch()
        self._safe_write_state()

    async def async_media_pause(self) -> None:
        if not self._speakers:
            return
        if busy := self._busy_reason:
            raise HomeAssistantError(
                f"'{self._playlist_name}' is still {busy} — try again in a moment"
            )
        await self.hass.services.async_call(
            "media_player", "media_pause", {}, target={"entity_id": self._speakers}, blocking=True
        )
        self._state = MediaPlayerState.PAUSED
        self._activity = ACTIVITY_PAUSED
        self._touch()
        self._safe_write_state()

    async def async_media_stop(self) -> None:
        await self.async_session_stop()

    async def async_set_volume_level(self, volume: float) -> None:
        if not self._speakers:
            return
        await self.hass.services.async_call(
            "media_player",
            "volume_set",
            {"volume_level": volume},
            target={"entity_id": self._speakers},
            blocking=True,
        )

    async def async_mute_volume(self, mute: bool) -> None:
        if not self._speakers:
            return
        await self.hass.services.async_call(
            "media_player",
            "volume_mute",
            {"is_volume_muted": mute},
            target={"entity_id": self._speakers},
            blocking=True,
        )

    async def async_media_next_track(self) -> None:
        await self._advance(1)

    async def async_media_previous_track(self) -> None:
        await self._advance(-1)

    async def _advance(self, direction: int) -> None:
        """Tell Music Assistant to skip. Queue/now_playing bookkeeping is NOT
        done here — the resulting media_title/media_artist change on the
        primary speaker fires _async_handle_speaker_update, which is now the
        single place advancement is applied (so an explicit skip and a track
        ending naturally go through the same, one, code path).
        """
        if not self._speakers:
            return
        service = "media_next_track" if direction == 1 else "media_previous_track"
        await self.hass.services.async_call(
            "media_player", service, {}, target={"entity_id": self._speakers}, blocking=True
        )

    async def async_join_players(self, group_members: list[str]) -> None:
        """Standard HA grouping entry point — media_player.join targeting
        this entity, e.g. from a dashboard media-control card's own group
        UI. Delegates to the same logic as ai_jukebox.attach_speaker so
        both surfaces stay in sync; requires an active session, same as
        that service.
        """
        await self.async_attach_speakers(group_members)

    async def async_unjoin_player(self) -> None:
        """Standard HA grouping entry point — media_player.unjoin
        targeting this entity. Ai_jukebox is the group's cosmetic anchor,
        not a member that can leave while the rest continue (see
        group_members docstring), so this means the whole session is
        done — same as ai_jukebox.stop with no speakers specified.
        """
        await self.async_session_stop()

    # --- Session lifecycle (called from services in __init__.py) ---

    async def _rehydrate_from_pool(self, cached: list[dict]) -> list[dict]:
        """Re-read a restored queue against the pool as it stands now.

        The queue outlives the pool it was drawn from. Stop a session, let the
        playlist rebuild overnight, start it again, and the restored queue is
        pre-rebuild material: Yacht & Harmony Groove came back holding Laura
        Branigan, an artist that rebuild had dropped from the pool outright,
        and carrying none of the fields the rebuild added. Every improvement
        to a pool was invisible for the first ~15 tracks of the next session.

        Matched on uri, the identity the rest of the design turns on. Entries
        the pool no longer holds are dropped -- they are not part of this
        playlist any more -- and the rest are replaced by the pool's current
        record, so a queued track gains whatever the rebuild gave it.

        An empty pool returns the cache untouched: better to play stale tracks
        than nothing at all.
        """
        pool = await self._store.async_get_pool(self._playlist_name)
        by_uri = {t["uri"]: t for t in (pool.get("tracks") or []) if t.get("uri")}
        if not by_uri:
            return cached
        fresh = [
            dict(by_uri[t["uri"]], source=t.get("source") or "pool")
            for t in cached
            if t.get("uri") in by_uri
        ]
        if len(fresh) != len(cached):
            _LOGGER.info(
                "'%s': dropped %d queued track(s) the pool no longer holds",
                self._playlist_name, len(cached) - len(fresh),
            )
        return fresh

    async def _release_speakers_from_other_sessions(self, speakers: list[str]) -> None:
        """Take these speakers off any other playlist still claiming them.

        Two sessions holding one speaker is a real state, not a theoretical
        one. The dashboard's swap path starts the newly selected playlist
        without stopping the running one, and nothing downstream noticed: the
        old session stayed `playing`, kept the speaker in its own list, and
        watched the new playlist's tracks arrive as content it could not match
        -- recording them through the untracked branch as if its speaker had
        been hijacked. Which it had, by us.

        Handing the overlap to async_session_stop() gets the right behaviour
        either way: a session losing only some of its speakers keeps playing on
        the rest, and one losing all of them stops.
        """
        wanted = set(speakers)
        players = self.hass.data.get(DOMAIN, {}).get("players", {})
        for other in list(players.values()):
            if other is self:
                continue
            held = [sp for sp in (getattr(other, "_speakers", None) or []) if sp in wanted]
            if not held:
                continue
            _LOGGER.info(
                "'%s' is taking %s from '%s'",
                self._playlist_name, ", ".join(held), other._playlist_name,
            )
            try:
                await other.async_session_stop(speakers=held)
            except Exception:  # noqa: BLE001 - never let this block the new session
                _LOGGER.exception(
                    "Could not release %s from '%s'", held, other._playlist_name
                )

    async def async_session_start(
        self,
        speakers: list[str],
        batch_size: int | None = None,
    ) -> None:
        """Start (or restart) this playlist session on the given speakers.

        Always clears whatever queue existed before — no leftover material
        from a previous session mixes with the new one.
        """
        # Before the lock, deliberately: this reaches into another entity and
        # takes *its* session lock, and doing that while holding ours is how a
        # deadlock gets built if two playlists ever start on each other's
        # speakers at once.
        await self._release_speakers_from_other_sessions(speakers)

        async with self._session_lock:
            # A start called again while a prior session's background refill
            # is still running (no intervening stop) would otherwise let that
            # stale task land against this new session's queue once it
            # finishes — same race async_session_stop guards against.
            await self._cancel_pending_generation()

            # Dedupe, order-preserving: a caller passing the same speaker
            # twice (a script building the list, a repeated entity in a
            # service target) otherwise lands a duplicate in _speakers, and
            # every rule keyed on speakers[0] or len(self._speakers) then
            # reads a session one speaker larger than it is -- including the
            # "last speaker leaving stops the session" check in
            # async_detach_speakers(). Observed live 2026-09-26 with
            # sonos_e listed twice.
            self._speakers = list(dict.fromkeys(speakers))
            self._resubscribe_speakers()
            effective_batch = batch_size or self._playlist_config.get(
                "batch_size", DEFAULT_BATCH_SIZE
            )

            self._activity = ACTIVITY_REFILLING
            self._state = MediaPlayerState.BUFFERING
            self._last_error = None
            self._touch()
            self._safe_write_state()

            cached = _restore_queue(
                await self._store.async_get_unplayed(self._playlist_name)
            )
            if cached:
                cached = await self._rehydrate_from_pool(cached)
            fresh_start = False
            if cached:
                _LOGGER.info(
                    "Restoring %d cached tracks for '%s'", len(cached), self._playlist_name
                )
                tracks = cached
            else:
                fresh_start = True
                tracks = await self._sample_from_pool(effective_batch) or []
                if not tracks:
                    # Nothing in the pool yet. A playlist becomes playable
                    # seconds into its first build, so this means no build has
                    # run at all -- there is no generation path to fall back to.
                    self._state = MediaPlayerState.IDLE
                    self._last_error = (
                        f"'{self._playlist_name}' has no pool yet — run "
                        f"ai_jukebox.rebuild_pool for it first"
                    )
                    self._touch()
                    self._safe_write_state()
                    raise HomeAssistantError(self._last_error)

            self._queue = tracks
            enqueued_ok = await self._enqueue_tracks(self._queue, clear_first=True)

            if enqueued_ok:
                await self._reset_playback_modes(self._speakers)
                self._played_count = 0
                self._now_playing = None  # fresh session — nothing to compare against
                if not await self._reconcile_with_primary(skip_refill=fresh_start):
                    self._now_playing = self._queue.pop(0) if self._queue else None
                await self._persist_queue()
                self._state = MediaPlayerState.PLAYING
                self._activity = ACTIVITY_PLAYING
            else:
                # Nothing actually started playing — don't claim otherwise.
                # _enqueue_tracks already set last_error/activity=error.
                self._now_playing = None
                self._state = MediaPlayerState.IDLE
                await self._persist_queue()
            await self._persist_session()
            self._touch()
            self._safe_write_state()

            if fresh_start and enqueued_ok:
                # Seed batch is already playing — top the queue back up to a
                # full batch right away rather than waiting for it to drain
                # down to refill_threshold. Backgrounded (not awaited) so
                # this service call returns as soon as the seed is playing,
                # not after the follow-up draw completes too. Handle kept
                # on self._cold_start_topup_task so a stop that follows
                # close behind can cancel it — see async_session_stop().
                self._cold_start_topup_task = self.hass.async_create_task(
                    self._generate_and_enqueue_more(effective_batch)
                )

    @property
    def _generation_busy(self) -> bool:
        """True while either background generation task is still running."""
        return any(
            task is not None and not task.done()
            for task in (self._cold_start_topup_task, self._generation_task)
        )

    async def _cancel_pending_generation(self) -> None:
        """Cancel any in-flight background generation — the cold-start top-up
        (see async_session_start) and the steady-state refill (_maybe_refill)
        — and wait for them to actually unwind before returning. Without
        this, a stop (or a second start) that lands while one is still
        generating could let it complete afterward and mutate self._queue out
        from under whatever session is running by then.

        Safe to call while holding _session_lock: neither task takes it.
        """
        topup, self._cold_start_topup_task = self._cold_start_topup_task, None
        refill, self._generation_task = self._generation_task, None
        for task in (topup, refill):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def async_session_stop(self, speakers: list[str] | None = None) -> None:
        """Stop the session (or just the given speakers — see attach/detach)."""
        if speakers and set(speakers) != set(self._speakers):
            await self.async_detach_speakers(speakers)
            return

        async with self._session_lock:
            await self._cancel_pending_generation()
            await self._persist_queue()
            # The track playing at the moment of stop needs no special
            # handling: plays are recorded when a track *starts*, so it is
            # already in the plays file.
            if self._speakers:
                leader, _members, _independent = self._partition_speakers(self._speakers)
                if leader:
                    # Unjoin before stopping — leaves speakers ungrouped and
                    # clean for whatever uses them next, same precedent as
                    # play_surprise_ai_jukebox's "unjoin before playing"
                    # step. Unlike async_detach_speakers() there are no
                    # survivors to protect here, since everything is being
                    # stopped a few lines down anyway.
                    #
                    # Known wart as of MA 2.10.2 (see async_detach_speakers):
                    # this unjoin makes MA transfer the queue to a survivor
                    # and resume it, for the second or so until the stop
                    # below lands, and leaves the survivors grouped under
                    # that new leader rather than ungrouped. Stopping first
                    # and unjoining every speaker afterwards would avoid
                    # both, but is a change to the most-used path and is
                    # deliberately not bundled with the detach fix.
                    await self._unjoin(leader)
                try:
                    await self.hass.services.async_call(
                        "media_player",
                        "media_stop",
                        {},
                        target={"entity_id": self._speakers},
                        blocking=True,
                    )
                except Exception as err:  # best-effort — stop must always succeed locally
                    _LOGGER.warning(
                        "Error stopping speakers for '%s': %s", self._playlist_name, err
                    )

            self._speakers = []
            self._grouped_seen.clear()
            self._resubscribe_speakers()
            self._now_playing = None
            self._queue = []
            self._activity = ACTIVITY_IDLE
            self._state = MediaPlayerState.IDLE
            await self._persist_session()
            self._touch()
            self._safe_write_state()

    async def async_attach_speakers(self, speakers: list[str]) -> None:
        if not self._speakers:
            raise HomeAssistantError(
                f"'{self._playlist_name}' is not running — call ai_jukebox.start first"
            )
        new = [s for s in speakers if s not in self._speakers]
        if not new:
            return

        async with self._session_lock:
            # Guard against the reactive listener (_async_handle_speaker_
            # update) treating the primary's own transient idle blip mid-
            # join as a real stop — _form_group()'s media_player.join can
            # briefly bounce the primary through idle while it renegotiates,
            # especially bridging speakers with no shared native protocol
            # (e.g. Sonos + HomePod, see the "Sendspin" note). Without this,
            # that blip reads as "the primary wants out" and, in a single-
            # speaker session, cascades into a full session stop — racing
            # and tearing down the very attach in progress. Confirmed live
            # 2026-09-01 attaching a HomePod to an already-playing
            # single-speaker Sonos session.
            previous_activity = self._activity
            self._activity = ACTIVITY_SPEAKER_JOIN
            self._safe_write_state()
            error = False

            # New speaker joins mid-stream with whatever's left of the queue
            # — it starts from "now playing" forward, not from track 1.
            remaining = ([self._now_playing] if self._now_playing else []) + self._queue
            primary = self._speakers[0]

            if self._supports_grouping(primary):
                # speakers[0] can anchor a group (whether or not one already
                # exists yet — this attach may be what forms it for the
                # first time). Join the groupable subset of `new` straight
                # to it instead of a separate independent enqueue; a failed
                # join is flagged, not fallen back to independent (there are
                # clearly other speakers in the session already).
                joinable_new = [s for s in new if self._supports_grouping(s)]
                independent_new = [s for s in new if s not in joinable_new]
                if joinable_new:
                    joined_ok = await self._form_group(primary, joinable_new)
                    if not joined_ok:
                        self._last_error = f"Failed to join {joinable_new} to the group under {primary}"
                        error = True
                    else:
                        # Stay in ACTIVITY_SPEAKER_JOIN until HA can see the
                        # group, so no listener rule gets to act on the
                        # half-propagated state in between.
                        await self._wait_until_grouped(primary, joinable_new)
                if independent_new and remaining:
                    await self._enqueue_tracks(remaining, clear_first=True, speakers=independent_new)
                    await self._reset_playback_modes(independent_new)
            elif remaining:
                # speakers[0] doesn't support grouping — today's original
                # behavior, independent enqueue for whatever's newly attached.
                await self._enqueue_tracks(remaining, clear_first=True, speakers=new)
                await self._reset_playback_modes(new)

            self._speakers.extend(new)
            # Watch the speakers that just joined. Missing until 2026-09-13,
            # against this method's own contract in _resubscribe_speakers():
            # a speaker attached mid-session was never subscribed, so its own
            # stop, or its leaving the group by its own means, stayed
            # invisible to _async_handle_speaker_update() until some other
            # path happened to resubscribe. Safe to widen the listener's
            # inputs now that the group-shrink rule only acts on membership
            # HA has actually confirmed (see _wait_until_grouped()).
            self._resubscribe_speakers()
            await self._persist_session()
            self._activity = ACTIVITY_ERROR if error else previous_activity
            self._touch()
            self._safe_write_state()

    async def async_detach_speakers(self, speakers: list[str]) -> None:
        """Remove speakers from a running session, keeping the rest playing.

        Two Music Assistant behaviours shape this, both read out of MA
        2.10.2's own source on 2026-09-13 (this server's version) rather
        than inferred from observed playback:

        - A playback command aimed at a player MA still considers synced is
          silently redirected to that player's sync leader
          (PlayerController._get_player_with_redirect). So the media_stop
          meant to silence a departing speaker stops the entire group
          instead, unless MA has already cleared its `synced_to`. That ended
          two live sessions on 2026-09-13: sonos_b was unjoined at
          15:00:26.067, sonos_a (the leader) went idle at .243, and
          the cascade below had the session dead by .8 — while sonos_b's
          own group_members did not clear until .317. Waiting for HA to show
          the speaker ungrouped before stopping it was the first fix, and it
          stopped being enough once the Sonos moved to AirPlay (2026-09-24):
          on 2026-10-18 sonos_c's group_members cleared at
          08:13:20.2, the stop went out at 08:13:23.27, and sonos_a
          (the leader) went idle 0.26s later, ending the session. MA's own
          unjoin had already silenced sonos_c at 08:13:20.2, as it did
          every grouped speaker in both incidents that day. So a speaker that
          was in the group is never stopped here at all — only one that was
          playing independently, which MA has no sync leader to redirect to.
        - Unjoining the *leader* of an ad-hoc group no longer dissolves it:
          PlayerController._handle_set_members promotes a remaining
          playback-capable member, moves the queue across (position
          preserved via resume_pos), regroups the others under it and
          resumes — _transfer_ad_hoc_leadership(). It only dissolves and
          stops when no survivor can render audio or the queue is already
          idle. So the leader case now adopts MA's heir as the new
          speakers[0] instead of building its own group and re-enqueueing:
          that fought MA, restarted the current track from zero, and could
          settle on a different heir than MA picked (it chooses by protocol
          reachability, _select_ad_hoc_leader). The old
          re-form-and-re-enqueue survives as the fallback for a real
          dissolve.
        """
        remove = [s for s in speakers if s in self._speakers]
        if not remove:
            return

        survivors = [s for s in self._speakers if s not in remove]
        if not survivors:
            # Last speakers out end the session — a playlist playing on zero
            # speakers isn't a meaningful state to sit in. The full stop
            # unjoins and stops them itself, and must: skipping the stop for
            # grouped speakers (below) would leave MA's heir playing here.
            await self.async_session_stop()
            return

        old_leader, old_members, _ = self._partition_speakers(self._speakers)
        leader_removed = bool(old_leader) and old_leader in remove
        grouped = {old_leader, *old_members} if old_leader else set()

        # Same guard, and for the same reason, as async_attach_speakers():
        # MA's leadership transfer stops the old leader and bounces the
        # survivors through idle while it regroups them, and our own unjoin
        # calls do the same to the speakers leaving. Unguarded, the listener
        # reads those blips as further speakers wanting out and cascades
        # into a full session stop — exactly the 2026-09-13 failure above.
        # Unlike attach this leaves `activity` alone, so a detach no longer
        # shows as "Speaker Join" for the few seconds it takes — see
        # _busy_reason().
        self._regrouping = True
        try:
            if leader_removed:
                # Must come before the stop below and before self._speakers
                # is updated: while this speaker still leads, anything aimed
                # at it lands on the survivors' audio too.
                await self._unjoin(old_leader)
            else:
                member_removed = [s for s in remove if s in old_members]
                if member_removed:
                    await self._unjoin(member_removed)

            # Only speakers that were never in the group get a stop. The
            # unjoin above already silences a grouped one, and a stop can
            # only hurt: MA may still hold it synced after HA's group_members
            # has cleared, and redirects the stop to the sync leader.
            stoppable = [s for s in remove if s not in grouped]
            if stoppable:
                try:
                    await self.hass.services.async_call(
                        "media_player", "media_stop", {},
                        target={"entity_id": stoppable}, blocking=True,
                    )
                except Exception as err:
                    _LOGGER.warning(
                        "Error stopping detached speakers for '%s': %s",
                        self._playlist_name, err,
                    )

            self._speakers = survivors
            self._grouped_seen -= set(remove)

            if leader_removed:
                await self._settle_new_leader(old_leader)

            self._resubscribe_speakers()  # primary may have changed
            await self._persist_session()
        finally:
            self._regrouping = False
        self._touch()
        self._safe_write_state()
        await self._reconcile_stopped_survivors()

    async def _detach_after_group_settles(self) -> None:
        """Work out who left a shrinking group once its state has settled.

        Called by _async_handle_speaker_update() with _regrouping already set.
        A member leaving and the leader leaving look alike for the first
        second: when the leader goes, MA bounces its heir through idle while
        it moves the queue across, and the heir's update can land first.
        Acting on that first update is how unjoining sonos_c (the
        leader) from its own card on 2026-10-18 09:08:36.8 ended the session:
        sonos_a read as the speaker leaving, and was detached and
        stopped right after MA had resumed the queue on it. The guard that
        covers a leader transfer only existed for detaches ai_jukebox itself
        started.

        So wait, then decide from the leader: still playing means the
        members HA no longer shows in its group left; anything else means the
        leader is the one that went, and async_detach_speakers()'s leader
        path adopts MA's heir.
        """
        try:
            await asyncio.sleep(GROUP_SHRINK_SETTLE)
        finally:
            self._regrouping = False
        leader, members, _ = self._partition_speakers(self._speakers)
        if leader:
            departed = (set(members) & self._grouped_seen) - set(self._live_group_members(leader))
            if departed:
                state = self.hass.states.get(leader)
                if state is not None and state.state in (
                    MediaPlayerState.PLAYING, MediaPlayerState.BUFFERING
                ):
                    await self.async_detach_speakers(sorted(departed))
                else:
                    await self.async_detach_speakers([leader])
                return
        # The group healed, or the shrink was a stale read. Stops swallowed
        # during the settle still need catching up on.
        await self._reconcile_stopped_survivors()

    async def _reconcile_stopped_survivors(self) -> None:
        """Catch up on speaker stops that landed while a detach was busy.

        _async_handle_speaker_update() ignores speaker state entirely while
        activity is ACTIVITY_SPEAKER_JOIN, which is what stops a detach (and
        MA's leadership transfer inside it) from cascading into a full
        session stop. The cost is that a *genuine* stop arriving in that
        window — everything being stopped at once from the Sonos app, say —
        is dropped, and no further event will arrive to re-trigger it,
        leaving the session claiming to play on idle speakers. So re-read the
        survivors once the guard is off.

        The grace period is the point of the loop rather than an arbitrary
        settle: MA's leadership transfer resumes playback with a short gap,
        so every survivor can legitimately read as stopped for a moment
        right after one. A mix of stopped and playing survivors needs no
        grace — the playing ones prove the group is alive, so the stopped
        ones really are leaving.
        """
        deadline = self.hass.loop.time() + DETACH_TRANSFER_TIMEOUT
        while self._speakers:
            stopped = [
                s
                for s in self._speakers
                if (state := self.hass.states.get(s)) is not None
                and state.state in self._STOPPED_STATES
            ]
            if not stopped:
                return
            if len(stopped) < len(self._speakers) or self.hass.loop.time() >= deadline:
                # Terminates: each round drops at least one speaker, and the
                # last one out ends the session.
                await self.async_detach_speakers(stopped)
                return
            await asyncio.sleep(DETACH_POLL_INTERVAL)

    async def _unjoin(self, entity_id: str | list[str]) -> None:
        """Best-effort media_player.unjoin, by this module's rule that a
        session-teardown path never lets one unreachable speaker block the
        rest of the cleanup.
        """
        try:
            await self.hass.services.async_call(
                "media_player", "unjoin", {}, target={"entity_id": entity_id}, blocking=True
            )
        except Exception as err:
            _LOGGER.warning(
                "Error unjoining %s for '%s': %s", entity_id, self._playlist_name, err
            )

    async def _wait_until_grouped(self, leader: str, members: list[str]) -> list[str]:
        """Wait for HA to report `members` inside `leader`'s group, returning
        any that never showed up.

        media_player.join returns as soon as MA has run the command, but HA's
        group_members attribute lags behind it — 1.3s, measured live on
        2026-09-13. Inside that window the leader still reports an *empty*
        group, which the group-shrink rule in _async_handle_speaker_update()
        reads as every member having just left; the detach that followed
        stopped the group and ended the session, which is how joining
        sonos_d killed a playing sonos_b session at 15:26:52 that
        day. Callers hold ACTIVITY_SPEAKER_JOIN across this so the listener
        never sees the half-propagated state at all, and what HA does confirm
        is recorded in _grouped_seen as the evidence the shrink rule needs.
        """
        pending = list(members)
        deadline = self.hass.loop.time() + GROUP_SETTLE_TIMEOUT
        while pending:
            live = set(self._live_group_members(leader))
            self._grouped_seen |= live & set(members)
            pending = [m for m in pending if m not in live]
            if not pending or self.hass.loop.time() >= deadline:
                break
            await asyncio.sleep(DETACH_POLL_INTERVAL)
        if pending:
            # Not fatal on its own: MA may still have the group, and the
            # shrink rule will not act on speakers it never saw grouped.
            _LOGGER.warning(
                "Home Assistant never reported %s inside %s's group for '%s' — "
                "grouping may not have taken effect",
                pending, leader, self._playlist_name,
            )
        return pending

    async def _settle_new_leader(self, old_leader: str) -> None:
        """Agree with MA about who leads now that `old_leader` has left.

        The expected outcome is MA's own leadership transfer, which has
        already moved the queue, regrouped the survivors and resumed at the
        saved position — all that is left is to make its heir our
        speakers[0], since that entity is also this integration's
        content-authority primary and so has to be the one the listener
        watches for track advances. Falls back to building the group here
        when MA dissolved instead (no playback-capable survivor, or a queue
        that was already idle).
        """
        heir = await self._wait_for_new_leader(old_leader)
        if heir:
            self._speakers.remove(heir)
            self._speakers.insert(0, heir)
            return

        new_leader, new_members, _ = self._partition_speakers(self._speakers)
        if new_leader and new_members:
            if await self._form_group(new_leader, new_members):
                await self._wait_until_grouped(new_leader, new_members)
            else:
                self._last_error = (
                    f"Failed to re-form group under {new_leader} after {old_leader} left"
                )
                self._activity = ACTIVITY_ERROR
        # The queue lived on the departed leader and MA did not carry it
        # across, so re-enqueue onto whoever leads now — including the
        # single-survivor case, which used to fall through here silently
        # because _partition_speakers() returns no leader for one speaker.
        # Unlike the transfer path this restarts the current track from the
        # beginning, which is the best available outcome once the group is
        # gone.
        #
        # _enqueue_tracks() leaves activity at ENQUEUING on success; every
        # other caller puts it back, and this one did not until 2026-11-09.
        # ENQUEUING is a busy state, so the listener then ignored the survivor
        # for good: no track advances, no refills. A Music Assistant restart
        # on 2026-11-08 took this path at 23:17:11 UTC and the session sat
        # "enqueuing" for 14.5 hours while sonos_d ran its 12
        # tracks dry and went silent.
        target = new_leader or self._speakers[0]
        remaining = ([self._now_playing] if self._now_playing else []) + self._queue
        if remaining:
            regroup_failed = self._activity == ACTIVITY_ERROR
            enqueued = await self._enqueue_tracks(remaining, clear_first=True, speakers=[target])
            await self._reset_playback_modes([target])
            if enqueued:
                if regroup_failed:
                    self._activity = ACTIVITY_ERROR
                else:
                    self._activity = (
                        ACTIVITY_PAUSED if self._state == MediaPlayerState.PAUSED
                        else ACTIVITY_PLAYING
                    )
                await self._reconcile_with_primary()

    async def _wait_for_new_leader(self, old_leader: str) -> str | None:
        """The survivor MA promoted to lead the group, or None if MA
        dissolved the group instead.

        MA regroups the survivors under its heir, so the heir is the speaker
        reporting a group whose first member is itself (group_members[0] is
        the leader, as every grouped Sonos here reports it). A speaker whose
        group still lists `old_leader` is ignored as a stale read. With a
        single survivor there is no group left to inspect, so an active
        playback state is the signal that MA moved the queue across rather
        than stopping.
        """
        deadline = self.hass.loop.time() + DETACH_TRANSFER_TIMEOUT
        while True:
            for speaker in self._speakers:
                live = self._live_group_members(speaker)
                if old_leader in live:
                    continue
                members = [m for m in live if m in self._speakers]
                if len(members) > 1 and members[0] == speaker:
                    return speaker
                state = self.hass.states.get(speaker)
                if (
                    len(self._speakers) == 1
                    and state is not None
                    and state.state in self._ACTIVE_STATES
                ):
                    return speaker
            if self.hass.loop.time() >= deadline:
                return None
            await asyncio.sleep(DETACH_POLL_INTERVAL)

    async def async_session_reset(self) -> None:
        """Force this session back to clean idle without an HA restart.

        Unlike async_session_stop(), this never blocks on anything succeeding
        — it's the escape hatch for when stop itself is stuck. Does not touch
        the persisted unplayed-queue/history store, only the state machine —
        except for the persisted `session` record itself, which must be
        cleared here too, otherwise a stuck session force-cleared by this
        would still look "active" on disk and get wrongly resurrected after
        the next HA restart.
        """
        # Fire-and-forget, unlike _cancel_pending_generation()'s awaited
        # cancel: reset is the escape hatch for when something is already
        # stuck, so it must never block on a task unwinding.
        topup, self._cold_start_topup_task = self._cold_start_topup_task, None
        refill, self._generation_task = self._generation_task, None
        resume, self._ma_resume_task = self._ma_resume_task, None
        for task in (topup, refill, resume):
            if task and not task.done():
                task.cancel()
        self._ma_recovering = False
        self._ma_outage_at = None

        if self._speakers:
            try:
                await self.hass.services.async_call(
                    "media_player",
                    "media_stop",
                    {},
                    target={"entity_id": self._speakers},
                    blocking=True,
                )
            except Exception:
                pass

        self._speakers = []
        self._grouped_seen.clear()
        self._resubscribe_speakers()
        self._queue = []
        self._now_playing = None
        self._activity = ACTIVITY_IDLE
        self._state = MediaPlayerState.IDLE
        self._last_error = None
        self._consecutive_failures = 0
        await self._persist_session()
        self._touch()
        self._safe_write_state()

    async def async_suspend(self) -> dict:
        """Pause playback in place ahead of an integration reload/unload.

        Unlike async_session_stop(), this never clears or replaces the queue
        — it just pauses the attached speakers and returns a snapshot for
        _resume_from_suspend() to restore once setup finishes. The physical
        MA queue is left exactly as-is, so this is safe without the
        MA-state verification a real restart-resurrection would need —
        nothing else has a chance to diverge in the few seconds a reload
        takes.
        """
        was_playing = self._state == MediaPlayerState.PLAYING
        if self._speakers:
            try:
                await self.hass.services.async_call(
                    "media_player", "media_pause", {},
                    target={"entity_id": self._speakers}, blocking=True,
                )
            except Exception as err:
                _LOGGER.warning(
                    "Error pausing speakers for '%s' during suspend: %s",
                    self._playlist_name, err,
                )
        await self._persist_queue()
        return {
            "speakers": list(self._speakers),
            "now_playing": self._now_playing,
            "played_count": self._played_count,
            "was_playing": was_playing,
            "last_prompt": self._last_prompt,
        }

    async def _resume_from_suspend(self, snapshot: dict) -> None:
        """Restore a session suspended by async_suspend(), called from
        async_added_to_hass() once this entity exists again post-reload.
        """
        self._speakers = list(dict.fromkeys(snapshot.get("speakers", [])))
        self._now_playing = snapshot.get("now_playing")
        self._last_prompt = snapshot.get("last_prompt")
        self._played_count = snapshot.get("played_count", 0)
        self._queue = _restore_queue(
            await self._store.async_get_unplayed(self._playlist_name)
        )

        if not self._speakers:
            self._activity = ACTIVITY_IDLE
            self._state = MediaPlayerState.IDLE
            return

        if snapshot.get("was_playing"):
            try:
                await self.hass.services.async_call(
                    "media_player", "media_play", {},
                    target={"entity_id": self._speakers}, blocking=True,
                )
                self._state = MediaPlayerState.PLAYING
                self._activity = ACTIVITY_PLAYING
            except Exception as err:
                _LOGGER.warning(
                    "Error resuming speakers for '%s' after reload: %s",
                    self._playlist_name, err,
                )
                self._state = MediaPlayerState.PAUSED
                self._activity = ACTIVITY_PAUSED
        else:
            self._state = MediaPlayerState.PAUSED
            self._activity = ACTIVITY_PAUSED
        self._touch()

    async def _resume_from_restart(self, session: dict) -> None:
        """Attempt restart-resurrection from the disk-persisted `session`
        record, called from async_added_to_hass() (once HA has finished
        starting) when no in-process suspend snapshot exists — i.e. this is
        a real HA restart, not a same-process integration reload.

        Unlike _resume_from_suspend(), this never issues a play/pause
        command and never blindly trusts the record — a restart can take
        long enough, and cross enough of a gap, that a speaker may have
        been stopped, rebooted, renamed, or opted itself out in the
        meantime. Each persisted speaker is filtered the same way
        _async_handle_speaker_update() would treat it live: gone entirely
        (renamed/removed) or OFF/IDLE -> dropped, that speaker
        "wants out" just like it would mid-session; unavailable/unknown ->
        kept, assumed a transient blip; PAUSED -> kept only if it ends up
        the sole survivor (a lone paused speaker is a legitimate
        resume-later pause, but paused amid an otherwise-multi-speaker
        group means opting out, same as live). Only once a primary speaker
        survives that filter AND is currently found actually playing or
        paused is anything reattached, and then only reconciled against
        whatever it's really playing via the same path the live listener
        uses. Otherwise the session is treated as lost and the disk record
        is cleared rather than resurrected.
        """
        persisted = session.get("speakers", [])
        existing = [s for s in persisted if self.hass.states.get(s) is not None]

        survivors = [
            s for s in existing
            if self.hass.states.get(s).state not in self._STOPPED_STATES
        ]
        if len(survivors) > 1:
            survivors = [
                s for s in survivors
                if self.hass.states.get(s).state != MediaPlayerState.PAUSED
            ]

        dropped = [s for s in persisted if s not in survivors]
        if dropped:
            _LOGGER.info(
                "Dropping speaker(s) %s from '%s' on restart — gone, off/idle, or opted out",
                dropped, self._playlist_name,
            )

        if not survivors:
            _LOGGER.info(
                "Not resuming '%s' after restart — no persisted speaker is still active",
                self._playlist_name,
            )
            await self._store.async_save_session(self._playlist_name, None)
            return

        primary_state = self.hass.states.get(survivors[0])
        if primary_state.state not in self._ACTIVE_STATES:
            _LOGGER.info(
                "Not resuming '%s' after restart — primary speaker %s is %s, not playing",
                self._playlist_name, survivors[0], primary_state.state,
            )
            await self._store.async_save_session(self._playlist_name, None)
            return

        self._speakers = survivors
        self._queue = _restore_queue(
            await self._store.async_get_unplayed(self._playlist_name)
        )
        self._played_count = session.get("played_count", 0)
        self._now_playing = session.get("now_playing")
        self._last_prompt = session.get("last_prompt")
        self._state = (
            MediaPlayerState.PAUSED if primary_state.state == MediaPlayerState.PAUSED
            else MediaPlayerState.PLAYING
        )
        self._activity = ACTIVITY_PAUSED if self._state == MediaPlayerState.PAUSED else ACTIVITY_PLAYING

        leader, members, _ = self._partition_speakers(self._speakers)
        if leader and members:
            # Sonos-style groups often survive a restart at the device
            # level, so check before (re)forming rather than blindly
            # re-issuing a join on speakers that are already correctly
            # grouped.
            live_members = set(self._live_group_members(leader)) - {leader}
            if set(members) - live_members:
                joined_ok = await self._form_group(leader, members)
                if not joined_ok:
                    self._last_error = f"Failed to re-form group under {leader} after restart"
                    self._activity = ACTIVITY_ERROR

        title = primary_state.attributes.get("media_title")
        artist = primary_state.attributes.get("media_artist")
        if title and artist:
            album = primary_state.attributes.get("media_album_name")
            self._now_playing = None  # force a fresh comparison
            # With the uri, for the same reason async_session_start passes it:
            # title alone cannot match a track Music Assistant has decorated,
            # and a resurrected session would come back reporting whatever it
            # was playing as `untracked`.
            await self._reconcile_now_playing(
                title, artist, album,
                uri=primary_state.attributes.get("media_content_id"),
            )

        _LOGGER.info(
            "Resumed '%s' after HA restart — primary speaker %s is %s (%s)",
            self._playlist_name, survivors[0], primary_state.state,
            f"{artist} - {title}" if title and artist else "no track info",
        )
        await self._persist_session()
        self._touch()

    # --- Generation / enqueue / refill ---

    async def _sample_from_pool(self, count: int) -> list[dict]:
        """Draw `count` tracks from the pool. See design doc section 4.

        Weighting excludes nothing: every pool track keeps a non-zero weight,
        just at wildly different odds, which is why no starvation fallback is
        needed. One hard exclusion is not a weighting — a track already in the
        live queue or now playing is skipped outright, and if that leaves
        nothing scoreable this returns an empty list. Three effects are
        multiplied: recency, repeat count, and a seed boost.

        Recency decays from 1e-6 for a track just played to 1.0 at
        RECENCY_FULL_HOURS. Because it is monotonic in time since last play,
        a pool where everything is recent still orders correctly by
        least-recently-played -- which is why no starvation branch is needed
        even on a twenty-track starter pool.

        Repetition counts artist and album plays inside a shorter window and
        sums them, deliberately double-counting the album (album plays are a
        subset of artist plays), so a track from a just-played album is
        penalised as its artist and again as its album.

        Selection is Efraimidis-Spirakis, but the key MUST be computed in log
        space. The textbook form random() ** (1/w) underflows to exactly 0.0
        at w=1e-6 -- every suppressed track would tie at zero and the ordering
        the decay exists to create would be lost.
        """
        pool = await self._store.async_get_pool(self._playlist_name)
        tracks = [t for t in (pool.get("tracks") or []) if t.get("uri")]
        if not tracks:
            return []

        # A smaller draw while building means each refill sees a materially
        # larger pool as batches land, instead of locking in the first batch.
        if pool.get("building"):
            count = max(1, min(count, POOL_SAMPLE_WHILE_BUILDING))
        else:
            count = max(count, random.randint(POOL_SAMPLE_MIN, POOL_SAMPLE_MAX))

        plays = await self._store.async_get_plays(self._playlist_name)
        now = dt_util.utcnow()
        repeat_cutoff = now - timedelta(hours=REPEAT_WINDOW_HOURS)
        last_played: dict[str, object] = {}
        artist_plays: dict[str, int] = {}
        album_plays: dict[str, int] = {}
        for entry in plays:
            stamp = dt_util.parse_datetime(entry.get("at") or "")
            if stamp is None:
                continue
            uri = entry.get("uri")
            if uri and (uri not in last_played or stamp > last_played[uri]):
                last_played[uri] = stamp
            if stamp >= repeat_cutoff:
                if entry.get("artist_id"):
                    artist_plays[entry["artist_id"]] = artist_plays.get(entry["artist_id"], 0) + 1
                if entry.get("album_id"):
                    album_plays[entry["album_id"]] = album_plays.get(entry["album_id"], 0) + 1

        live = [self._now_playing] if self._now_playing else []
        queued = {t.get("uri") for t in live + list(self._queue) if t and t.get("uri")}

        # Seed boost. Solved per pool rather than fixed, because seed share
        # varies from 6% to 35% across playlists and a flat multiplier would
        # mean something different on each. max(1.0, ...) makes it a floor, so
        # a pool already above target is not pushed back down.
        #
        # Matched on artist name, not artist_id: the roster echoes seed names
        # verbatim (see the builder's 3.1) so the pool carries the seed's own
        # spelling. Measured at 96% -- the rest are seeds absent from the pool
        # entirely, which no weighting can help.
        #
        # Unlike `rank`, which section 2 rejects as a playback input, seed
        # membership is binary, comparable across albums, and chosen by a
        # person rather than inferred by a model.
        seed_names = {
            a.strip().lower()
            for a in (self._playlist_config.get("artists") or [])
            if isinstance(a, str) and a.strip()
        }

        def _is_seed(track: dict) -> bool:
            return (track.get("artist") or "").strip().lower() in seed_names

        seed_boost = 1.0
        if seed_names:
            seed_count = sum(1 for t in tracks if _is_seed(t))
            share = seed_count / len(tracks)
            # Only lift pools that fall short. A playlist whose seeds already
            # carry it -- Ethereal Storyteller runs 35% -- is left alone rather
            # than pushed further, which a bare solve would do.
            if 0.0 < share < SEED_TARGET_SHARE:
                aim = min(0.9, SEED_TARGET_SHARE * SEED_BOOST_CORRECTION)
                seed_boost = max(1.0, (aim / (1.0 - aim)) * ((1.0 - share) / share))
            _LOGGER.debug(
                "'%s': %d/%d pool tracks are seeds (%.0f%%), boost x%.2f",
                self._playlist_name, seed_count, len(tracks), share * 100, seed_boost,
            )

        full_seconds = RECENCY_FULL_HOURS * 3600
        scored: list[tuple[float, dict]] = []
        for track in tracks:
            uri = track["uri"]
            if uri in queued:
                continue
            stamp = last_played.get(uri)
            elapsed = full_seconds if stamp is None else min(
                (now - stamp).total_seconds(), full_seconds
            )
            recency = 10 ** (-RECENCY_FLOOR_EXP * (1 - elapsed / full_seconds))
            repeat = (
                artist_plays.get(track.get("artist_id"), 0)
                + ALBUM_REPEAT_K * album_plays.get(track.get("album_id"), 0)
            )
            weight = recency * (1.0 / (1 + repeat)) ** WEIGHT_ALPHA
            if seed_boost > 1.0 and _is_seed(track):
                weight *= seed_boost
            # random() can return exactly 0.0, for which log is undefined.
            draw = random.random() or 1e-12
            scored.append((math.log(draw) / weight, track))

        if not scored:
            return []
        scored.sort(key=lambda kv: kv[0], reverse=True)
        picked = [dict(t, source="pool") for _, t in scored[:count]]
        _LOGGER.debug(
            "'%s': drew %d of %d pool tracks (%d eligible, building=%s)",
            self._playlist_name, len(picked), len(tracks), len(scored),
            bool(pool.get("building")),
        )
        return picked
    async def _enqueue_tracks(
        self, tracks: list[dict], clear_first: bool, speakers: list[str] | None = None
    ) -> bool:
        """Enqueue tracks to Music Assistant. Returns True if at least one
        track was actually accepted anywhere — callers must not claim
        "playing" on a False return, since nothing necessarily started.

        Groupable targets (see _partition_speakers()) are joined into one
        physical speaker group and enqueued to just the leader, since
        joined members mirror it automatically — one enqueue instead of N
        identical ones, and genuinely synced playback instead of N
        independent replays. Non-groupable targets (or all of them, if
        fewer than two support grouping) still get their own independent
        enqueue, unchanged from before grouping existed. A speaker whose
        join fails is flagged (see _form_group()) and does NOT fall back
        to an independent enqueue.
        """
        targets = speakers if speakers is not None else self._speakers
        if not targets or not tracks:
            return True
        self._activity = ACTIVITY_ENQUEUING
        self._touch()
        self._safe_write_state()

        any_success = False
        last_err: str | None = None

        # HA skips a service call aimed at an unavailable entity with only a
        # "Referenced entities ... not currently available" warning and no
        # exception, so every call below would "succeed" while nothing
        # played. That is how the 2026-11-08 MA-outage resume reported
        # success after sending its whole queue to an unavailable
        # sonos_a. Count those speakers as failures up front.
        unavailable = [t for t in targets if not self._is_available(t)]
        if unavailable:
            last_err = f"{unavailable} unavailable — nothing enqueued there"
            _LOGGER.warning(
                "Not enqueuing '%s' on unavailable %s", self._playlist_name, unavailable
            )
            targets = [t for t in targets if t not in unavailable]

        leader, members, independent = self._partition_speakers(targets)

        if leader:
            if members:
                joined_ok = await self._form_group(leader, members)
                if not joined_ok:
                    last_err = f"Failed to group {members} under {leader}"
            # Leader always gets the enqueue, whether or not members
            # actually joined it.
            leader_ok, leader_err = await self._enqueue_to_targets([leader], tracks, clear_first)
            any_success = any_success or leader_ok
            last_err = leader_err or last_err

        if independent:
            ind_ok, ind_err = await self._enqueue_to_targets(independent, tracks, clear_first)
            any_success = any_success or ind_ok
            last_err = ind_err or last_err

        if last_err:
            # Visible even on a partial failure — an eventual successful
            # retry (e.g. the next track's "replace") shouldn't erase the
            # fact that an earlier one silently didn't land.
            self._last_error = last_err
            if not any_success:
                self._activity = ACTIVITY_ERROR
            self._touch()
            self._safe_write_state()

        return any_success

    async def _enqueue_to_targets(
        self, targets: list[str], tracks: list[dict], clear_first: bool
    ) -> tuple[bool, str | None]:
        """The actual per-track music_assistant.play_media loop, extracted
        from _enqueue_tracks() so it can be run once for a group leader and
        separately for any independent speakers.
        """
        any_success = False
        last_err: str | None = None
        for target in targets:
            replace_done = not clear_first
            for track in tracks:
                enqueue = "add" if replace_done else "replace"
                # A track resolved via _resolve_track carries the exact URI
                # Music Assistant's search already matched it to — play that
                # directly so what was validated against history is what
                # actually gets queued, instead of letting play_media search
                # again and possibly land on something else. Tracks without
                # a uri (e.g. restored from a cached queue after restart)
                # fall back to the old text-search behavior.
                media_id = track.get("uri") or track["title"]
                media_type = "track"
                extra = {} if track.get("uri") else {
                    "artist": track.get("artist", ""),
                    "album": track.get("album", ""),
                }
                try:
                    await self.hass.services.async_call(
                        "music_assistant",
                        "play_media",
                        {
                            "media_id": media_id,
                            "media_type": media_type,
                            "enqueue": enqueue,
                            **extra,
                        },
                        target={"entity_id": target},
                        blocking=True,
                    )
                    replace_done = True
                    any_success = True
                except Exception as err:
                    last_err = f"{track_dict_to_string(track)} on {target}: {err}"
                    _LOGGER.warning(
                        "Failed to enqueue '%s' on %s for '%s': %s",
                        track_dict_to_string(track), target, self._playlist_name, err,
                    )
        return any_success, last_err

    async def _reset_playback_modes(self, speakers: list[str]) -> None:
        # Must run AFTER the enqueue/replace call above, not before — Music
        # Assistant's "replace" carries over the previous queue's
        # repeat/shuffle state, so resetting first gets silently clobbered
        # the moment the new queue is built (confirmed the hard way in the
        # old ai_playlist integration). Only targets the group leader for
        # any grouped members (they mirror it) plus independent speakers —
        # sending transport-mode commands straight to a joined member
        # isn't necessary and hasn't been verified as meaningful.
        if not speakers:
            return
        leader, _members, independent = self._partition_speakers(speakers)
        targets = independent + ([leader] if leader else [])
        if not targets:
            return
        try:
            await self.hass.services.async_call(
                "media_player", "repeat_set", {"repeat": "off"},
                target={"entity_id": targets}, blocking=True,
            )
            await self.hass.services.async_call(
                "media_player", "shuffle_set", {"shuffle": False},
                target={"entity_id": targets}, blocking=True,
            )
        except Exception as err:
            _LOGGER.warning(
                "Could not reset repeat/shuffle for '%s' on %s: %s",
                self._playlist_name, targets, err,
            )
        for target in targets:
            await self._disable_dont_stop_the_music(target)

    def _ma_queue_id(self, entity_id: str) -> str | None:
        """The Music Assistant queue_id behind an HA media_player entity.

        For entities provided by the `music_assistant` integration the
        registry `unique_id` *is* MA's own player_id, and for a directly
        addressed player the queue_id equals the player_id (verified live
        2026-09-09 against MA 2.10.2: media_player.sonos_e has
        unique_id RINCON_000000000000001400, which is exactly the queue_id
        player_queues/get answers to). Returns None for anything not
        provided by that integration — a speaker reached through some other
        platform has no MA queue to address.
        """
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry is None or entry.platform != ma_playlist.MA_DOMAIN:
            return None
        return entry.unique_id

    async def _disable_dont_stop_the_music(self, entity_id: str) -> None:
        """Turn off MA's "Don't stop the music" for this speaker's queue.

        MA appends its own recommendations when a queue is close to running
        out. That fights this integration directly: generation is
        deliberately off the playback critical path (see
        async_session_start), so a cold start enqueues one draw and the
        background top-up lands seconds later. In that window MA sees a
        thinner queue than it expects and fills
        it with picks of its own, which then play as part of the playlist
        and get logged to history as `untracked` — observed 2026-09-09 on
        sonos_e, where four MA-chosen tracks (including a John
        Williams film score, and on an earlier session a Broadway cast
        recording the playlist prompt explicitly forbids) were inserted
        between the seed batch and the top-up. No prompt wording can prevent
        this, because the model never chose those tracks.

        Not part of the repeat/shuffle reset above: those are stock
        media_player services, while this is MA queue state with no HA
        service wrapping it, so it goes over the same HTTP RPC transport the
        builder uses for its catalogue reads (ma_playlist.async_ma_call). It is
        the only RPC *write* the integration makes. Best-effort by the same rule as the rest of
        _reset_playback_modes — a speaker MA can't reach must never block a
        session from starting — but a failure is logged, since silently
        leaving it on reintroduces exactly the bug this prevents.

        `dont_stop_the_music_enabled` must be a real bool: MA coerces other
        types rather than rejecting them, so a stray truthy value would
        quietly turn the feature back *on*.
        """
        queue_id = self._ma_queue_id(entity_id)
        if queue_id is None:
            return
        try:
            await ma_playlist.async_ma_call(
                self.hass,
                "player_queues/dont_stop_the_music",
                {"queue_id": queue_id, "dont_stop_the_music_enabled": False},
                timeout=5,
            )
        except ma_playlist.MAPlaylistError as err:
            _LOGGER.warning(
                "Could not disable 'don't stop the music' for '%s' on %s "
                "(queue %s) — Music Assistant may inject its own tracks: %s",
                self._playlist_name, entity_id, queue_id, err,
            )

    # --- Music Assistant pool ---
    #
    # A pool is this playlist's durable track set, built by
    # build/pipeline.py and read by _sample_from_pool(). It is the only
    # source of playback tracks; there is no generation on the playback
    # path and no per-playlist opt-out.

    @property
    def _pool_enabled(self) -> bool:
        # Kept as a property rather than inlined: call sites read as
        # intent ("is this playlist pool-driven"), and the stored
        # pool_enabled key it once consulted is gone as of 2026-09-27.
        return True  # pools are the only source of tracks

    @callback
    def async_adopt_config(self, playlist_config: dict) -> None:
        """Take on an edited playlist definition in place.

        Safe mid-session: nothing here touches the queue or the speakers,
        and the values that matter during playback (batch_size,
        refill_threshold, the prompt) are read fresh on each use, so an edit
        simply takes effect from the next refill rather than disturbing what
        is currently playing. The name is not re-read — an edit that changed
        it would slugify to a different entity entirely.
        """
        self._playlist_config = playlist_config
        self._touch()
        self._safe_write_state()
        # Pool stats are only cached for playlists that have a pool; a
        # playlist that just had one switched on needs its first read.
        if self._pool_enabled and not self._pool_stats:
            self.hass.async_create_task(self._refresh_pool_stats())

    async def async_rebuild_pool(self, force: bool = False, resume: bool = False) -> None:
        """Rebuild this playlist's pool from scratch, in the background.

        Spawned rather than awaited: a build runs for minutes and the service
        call must return immediately. Deliberately *not* part of
        _generation_busy -- a rebuild must never make a live session's refill
        think work is already in flight and skip it. Playback keeps drawing
        from the pool throughout, seeing each batch as it lands.

        Outside the rebuild window (an integration option, 02:00-05:00 local
        by default) a rebuild needs `force`. The window is enforced here as
        well as in the scheduler so that a stray call cannot start a
        multi-minute build in the middle of the evening.
        """
        if self.pool_build_running:
            _LOGGER.debug("Pool build already running for '%s'", self._playlist_name)
            return
        start = self.hass.data[DOMAIN].get(CONF_REBUILD_START_HOUR, DEFAULT_REBUILD_START_HOUR)
        end = self.hass.data[DOMAIN].get(CONF_REBUILD_END_HOUR, DEFAULT_REBUILD_END_HOUR)
        if not force and not in_rebuild_window(dt_util.now().hour, start, end):
            raise HomeAssistantError(
                f"Pool rebuilds run between {start:02d}:00 and "
                f"{end:02d}:00 local; pass force: true to override"
            )
        self._pool_build_task = self.hass.async_create_task(self._rebuild_pool_once(resume))

    async def _rebuild_pool_once(self, resume: bool = False) -> None:
        from .build.pipeline import PoolBuilder

        # The build leaves `activity` alone. It used to set "refilling" for
        # the whole build, and since that is one of _BUSY_ACTIVITIES a session
        # playing through a rebuild refused play/pause and ignored its
        # speaker's track changes -- so no refills -- for minutes. Progress
        # shows through pool_building instead, which _on_progress refreshes.
        entities = self.hass.data[DOMAIN].get("build_ai_entities") or None
        builder = None
        build_log: list[str] = []

        def _on_progress(msg: str) -> None:
            # Kept, not just logged: this instance writes no home-assistant.log,
            # so _LOGGER.info goes somewhere unreadable. The tail is bounded
            # because the pool file is re-read on every refill.
            build_log.append(f"{dt_util.utcnow().strftime('%H:%M:%S')} {msg}")
            del build_log[:-BUILD_LOG_MAX_LINES]
            # Refresh after each batch so pool_size and pool_building track the
            # build as it runs. Without this the entity reports the *previous*
            # pool for the whole build, and the nightly automation -- which
            # skips entities already building -- would see this one as idle.
            self.hass.async_create_task(self._refresh_pool_stats())

        summary = "rebuild did not complete"
        try:
            # Constructed inside the try: it raises when no ai_task entity is
            # configured, and that must land on the entity like any other
            # build failure.
            builder = PoolBuilder(self.hass, self._store, self._playlist_name, entities)
            stats = await builder.run(progress=_on_progress, resume=resume)
            summary = (
                f"{stats['tracks']} tracks / {stats['albums']} albums / "
                f"{stats['resolved']}of{stats['artists']} artists / "
                f"{stats['batches']} batches, {stats['failed_batches']} failed / "
                f"{builder.calls} model calls"
            )
            _LOGGER.info(
                "Pool rebuild for '%s' complete: %d tracks from %d albums across "
                "%d/%d artists in %d batches (%d model calls, %d batch failures)",
                self._playlist_name, stats["tracks"], stats["albums"],
                stats["resolved"], stats["artists"], stats["batches"],
                builder.calls, stats["failed_batches"],
            )
            self._last_error = None
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - surfaced on the entity, not raised
            self._last_error = f"Pool rebuild failed: {err}"
            summary = f"FAILED: {err}"
            _LOGGER.exception("Pool rebuild for '%s' failed", self._playlist_name)
        finally:
            # After finish/defer, both of which rewrite the pool record.
            try:
                await self._store.async_record_build_log(
                    self._playlist_name, build_log, summary
                )
            except Exception:  # noqa: BLE001 - diagnostics must never fail a build
                _LOGGER.exception("Could not record build log for '%s'",
                                  self._playlist_name)
            await self._refresh_pool_stats()
            self._touch()
            self._safe_write_state()

    @property
    def pool_build_running(self) -> bool:
        """A build task is live in this process (not just flagged on disk)."""
        return bool(self._pool_build_task and not self._pool_build_task.done())

    @property
    def pool_building(self) -> bool:
        """The pool file says a build is in progress -- possibly a dead one."""
        return bool(self._pool_stats.get("building"))

    @property
    def pool_built_at(self) -> str | None:
        return self._pool_stats.get("built_at")

    @property
    def tags(self) -> list[str]:
        return list(self._playlist_config.get("tags") or [])

    async def _refresh_pool_stats(self) -> None:
        """Re-read the pool file's summary fields into the cached stats."""
        pool = await self._store.async_get_pool(self._playlist_name)
        self._pool_stats = {
            "size": len(pool.get("tracks", [])),
            "built_at": pool.get("built_at"),
            "building": bool(pool.get("building")),
            # Artists a build lost to batches that failed every retry. Surfaced
            # because the only previous signal was a log line, and a lost seed
            # went unnoticed for days as a result.
            "failed_artists": pool.get("failed_artists") or [],
            "build_summary": pool.get("build_summary"),
        }
        self._safe_write_state()
    async def _maybe_refill(self) -> None:
        """Top the queue back up once it drops to refill_threshold.

        The refill is *spawned, never awaited through*. This is reached from
        _reconcile_now_playing on the speaker-state-change listener path.
        Awaiting the work inline held that listener task open for its whole
        duration, so a slow backend stalled track-advance reconciliation for as
        long as it took — the 2026-09-07 2h17m hang, when refills were live
        model calls against a CPU-bound local model. A refill is now a local
        pool read (_sample_from_pool) plus the enqueue, so the window is far
        smaller, but the structure is kept: the listener must never wait on it.

        Still `async def` so its call site reads unchanged, though nothing here
        awaits any more.
        """
        refill_threshold = self._playlist_config.get("refill_threshold", DEFAULT_REFILL_THRESHOLD)
        if len(self._queue) > refill_threshold:
            return
        if self._generation_busy:
            # Generation is already in flight — either a previous refill or
            # a cold start's top-up. Speaker state changes arrive as one
            # unserialized task each (see _async_handle_speaker_update), so
            # without this guard a burst of updates while the queue sits
            # below threshold would each spawn their own generation against
            # the same self._queue.
            return
        batch_size = self._playlist_config.get("batch_size", DEFAULT_BATCH_SIZE)
        self._generation_task = self.hass.async_create_task(
            self._generate_and_enqueue_more(batch_size)
        )

    async def _generate_and_enqueue_more(self, count: int) -> None:
        """Draw `count` more tracks from the pool and append them to the queue.

        Nothing is generated: this samples the existing pool. The name predates
        the removal of LLM track generation.

        Shared by the steady-state refill trigger (_maybe_refill, gated on
        refill_threshold) and the unconditional cold-start follow-up fired from
        async_session_start — that one always runs regardless of threshold, to
        top the queue up right behind the seed draw.

        Both callers now run this as a background task, so it can be
        cancelled at any await point by _cancel_pending_generation() (stop,
        reset, or a second start).
        """
        self._activity = ACTIVITY_REFILLING
        self._touch()
        self._safe_write_state()

        try:
            new_tracks = await self._sample_from_pool(count)
            if not new_tracks:
                self._activity = (
                    ACTIVITY_PLAYING if self._state == MediaPlayerState.PLAYING
                    else ACTIVITY_IDLE
                )
                self._touch()
                self._safe_write_state()
                return

            self._queue.extend(new_tracks)
            enqueued_ok = await self._enqueue_tracks(new_tracks, clear_first=False)
            await self._persist_queue()

            if enqueued_ok:
                self._activity = (
                    ACTIVITY_PLAYING if self._state == MediaPlayerState.PLAYING
                    else self._activity
                )
            # else: leave last_error/activity=error as set by _enqueue_tracks
            self._touch()
            self._safe_write_state()
        except asyncio.CancelledError:
            # Whatever landed before the cancel stays as-is (a partial
            # enqueue is already persisted); just don't leave the entity
            # stranded on "refilling" forever now that this can be killed
            # mid-generation. Only ever synchronous work here — awaiting
            # during cancellation is not reliable.
            if self._activity == ACTIVITY_REFILLING:
                self._activity = (
                    ACTIVITY_PLAYING if self._state == MediaPlayerState.PLAYING
                    else ACTIVITY_PAUSED if self._state == MediaPlayerState.PAUSED
                    else ACTIVITY_IDLE
                )
                self._touch()
                self._safe_write_state()
            raise

    async def _persist_queue(self) -> None:
        # Full records, not flattened "Artist - Title" strings. The queue now
        # holds pool tracks, and uri/artist_id/album_id are what reconciliation
        # matches on and what a play is recorded under -- flattening would
        # restore a session whose tracks could never be matched or recorded.
        await self._store.async_save_unplayed(self._playlist_name, list(self._queue))

    async def _persist_session(self) -> None:
        """Write-through the disk record restart-resurrection reads back —
        see module docstring and _resume_from_restart(). `None` (session
        inactive) whenever we're not actually in a playing/paused state,
        even if self._speakers is still populated (e.g. a failed start
        leaves speakers set but nothing playing).
        """
        session = (
            {
                "speakers": list(self._speakers),
                "now_playing": self._now_playing,
                "played_count": self._played_count,
                "last_prompt": self._last_prompt,
            }
            if self._state in self._ACTIVE_STATES
            else None
        )
        await self._store.async_save_session(self._playlist_name, session)


def _restore_queue(tracks: list) -> list[dict]:
    """Read back a persisted queue, accepting both shapes.

    Queues are now stored as full track records. A queue written before that
    change is a list of "Artist - Title" strings, which can be parsed for
    display but carries no uri -- so those entries cannot be matched on
    playback or recorded as plays. They are dropped rather than restored into
    a session where they would silently misbehave.
    """
    out: list[dict] = []
    dropped = 0
    for t in tracks or []:
        if isinstance(t, dict):
            if t.get("uri"):
                out.append(t)
            else:
                dropped += 1
        else:
            dropped += 1
    if dropped:
        _LOGGER.info("Dropped %d queued track(s) with no uri while restoring", dropped)
    return out


