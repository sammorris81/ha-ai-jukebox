"""Constants for the AI Jukebox integration."""
from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "ai_jukebox"

# Defaults
DEFAULT_BATCH_SIZE: Final = 10
DEFAULT_REFILL_THRESHOLD: Final = 2

# Two comment blocks were removed here on 2026-09-30, both describing
# constants that no longer exist: a COLD_START_SEED_SIZE (a small first batch
# ahead of a full-size follow-up) and a prompt-drift checker with
# CONF_DRIFT_CHECK_* options. Cold start now enqueues a full batch_size and
# samples it again for the follow-up (async_session_start), and _check_drift()
# went with LLM track generation.

# Activity values — a custom attribute, deliberately separate from the entity's
# native `state` (which stays a plain MediaPlayerState so stock media-control
# cards work). A background refill mid-playback needs state=playing (so
# pause/skip keep working) but activity=refilling so it's visible.
ACTIVITY_IDLE: Final = "idle"
ACTIVITY_ENQUEUING: Final = "enqueuing"
ACTIVITY_SPEAKER_JOIN: Final = "speaker_join"
ACTIVITY_PLAYING: Final = "playing"
ACTIVITY_PAUSED: Final = "paused"
ACTIVITY_REFILLING: Final = "refilling"
ACTIVITY_ERROR: Final = "error"

# --- Music Assistant playlist pools ---
#
# A pool is a real, durable MA playlist built by build/pipeline.py, and it is
# the ONLY source of playback tracks -- _sample_from_pool() draws every track
# from it. Pools are not optional and there is no per-playlist opt-out: the
# _pool_enabled property returns a hardcoded True. The stored `pool_enabled`
# key was opt-in scaffolding for the 2026-09-09 migration, was ignored from
# the moment that migration completed, and was removed on 2026-09-27.

# (A POOL_TARGET/POOL_HANDOFF_MIN pair lived here: a pool size ceiling and the
# count that gated handing playback over to the pool. Both went when pools
# became the only source of tracks -- there is nothing to hand over from. The
# real per-playlist keys are further down, at CONF_PLAYLIST_*.)

# --- Sampling (see docs/design.md section 4) ---
#
# Two weighting effects multiplied together, plus a seed boost. Neither
# weighting excludes anything: every pool track keeps a non-zero weight, just
# at wildly different odds. That is what removes the need for a starvation
# fallback -- decay is monotonic in time since last play, so when everything is
# recent the least-recently-played track automatically carries the highest
# weight. One hard exclusion does exist and is not a weighting: a track already
# in the live queue or now playing is skipped outright (_sample_from_pool).
#
#   recency = 10 ** (-6 * (1 - min(dt, RECENCY_FULL) / RECENCY_FULL))
#   repeat  = plays_artist + ALBUM_REPEAT_K * plays_album   (within REPEAT_WINDOW)
#   weight  = recency * (1 / (1 + repeat)) ** WEIGHT_ALPHA

# A just-played track is 1e-6 as likely as one not played in 12 hours.
RECENCY_FLOOR_EXP: Final = 6
# How long until a track is fully recovered. 12h covers a day's listening,
# which is what actually matters: on a ~1,200-track pool with ~60 plays a
# day, uniform sampling already gives a ~20-day expected repeat interval, so
# pool size is what prevents repeats across a week. The decay exists to
# prevent repeats *within* a session, and shorter recovery also means less
# distortion on a thin pool.
RECENCY_FULL_HOURS: Final = 12

# Artist/album repetition is counted over a shorter window than track
# recency: hearing the same artist again after 8 hours is unremarkable,
# hearing the same *track* is still mildly suppressed for another 4.
REPEAT_WINDOW_HOURS: Final = 8

# Album plays are a subset of artist plays, so summing them deliberately
# double-counts the album: a track from a heavily-played album is penalised
# once as its artist and again as its album. K tunes how much album
# repetition matters independently of the artist -- raising alpha alone
# would penalise both equally. Starting value, only listening will tell.
ALBUM_REPEAT_K: Final = 2

# Alpha 1.0 is pure inverse and over-corrects (90/10 listening flips to
# 10/90); 0.4 lands nearer 30/70, a nudge rather than a reversal.
WEIGHT_ALPHA: Final = 0.4

# Plays older than this are dropped when a pool is rebuilt. Equal to the
# longest span sampling reads, so the file needs no background pruning.
PLAYS_RETENTION_HOURS: Final = 12

# Tracks drawn per refill. Sampling is local and instant, so there is no
# reason to refill in small increments -- except while a pool is still
# building, when a smaller draw means each refill sees a materially larger
# pool as batches land.
POOL_SAMPLE_MIN: Final = 15
POOL_SAMPLE_MAX: Final = 20
POOL_SAMPLE_WHILE_BUILDING: Final = 5

# --- Builder (see docs/design.md section 3) ---

# Artists per model call, for both album selection and track selection.
# Call count therefore scales with roster size, not catalogue size.
BUILD_ARTISTS_PER_CALL: Final = 4

# Albums kept per artist by the selection pass. The floor is what makes the
# judgement relative ("the best of this artist") rather than an absolute bar
# -- an absolute bar returned zero albums for 11 of 56 artists in testing.
BUILD_MIN_ALBUMS: Final = 2
BUILD_MAX_ALBUMS: Final = 8

# Discovered artists requested per playlist, on top of the seed artists.
BUILD_ROSTER_TARGET: Final = 55

# The ai_task entities the builder rotates through are chosen at setup and
# changed from the integration's options (CONF_BUILD_AI_ENTITIES). There is
# deliberately no built-in default: entity ids are specific to each install.
# Order them by daily request budget rather than quality -- on a free tier the
# lite models usually carry far more requests a day, and leading with the
# scarce ones exhausts the chain after a few playlists.

# One model call's ceiling. Generous: track selection sends a few hundred
# track titles and the reply is proportionally large.
BUILD_CALL_TIMEOUT_SECONDS: Final = 180

# A batch carries BUILD_ARTISTS_PER_CALL artists, and curate.batches() puts
# exactly one seed in each, so a batch discarded without retrying costs a seed
# artist outright -- which is how Jackson Browne and Cat Stevens vanished from
# Warm 70s Harmony. Retry it in place, per design.md section 5.
BUILD_BATCH_ATTEMPTS: Final = 3
# Delay before each retry. Was (10, 30) on the reasoning that failures here are
# a Music Assistant hiccup or a call timing out, both clearing in seconds, with
# daily-quota exhaustion left to the resume budget instead of a longer wait.
# Widened 2026-09-30: The Orchestral Arena lost 5 of 8 batches on 2026-09-28,
# and (10, 30) put all three attempts inside a ~40s window -- too tight to
# outlast a per-minute limit, whatever the cause. (30, 90) straddles a minute
# boundary, at the cost of ~2 minutes per doomed batch instead of ~40s.
# Whether that is the right shape is still open: see the retry/backoff review
# in the project backlog, and read a failing build's `build_log` first, which
# now records each entity's own error string rather than just "RuntimeError".
BUILD_BATCH_BACKOFF_SECONDS: Final = (30, 90)
# How many times one batch may be attempted across resumes, counted per batch
# on the pool record's `batch_plan` rather than once for the whole build. Each
# attempt is itself BUILD_BATCH_ATTEMPTS tries in-run, so a batch gets 3 x 3
# before it is abandoned and its artists recorded in `failed_artists`.
#
# Was a pool-level BUILD_MAX_RESUME_ATTEMPTS = 1 until 2026-09-30: one resume
# for the entire build, which regenerated the roster and so re-attempted lost
# artists only if the new roster happened to name them again. The Orchestral
# Arena lost 18 artists that way on 2026-09-28.
BUILD_MAX_BATCH_ATTEMPTS: Final = 3
# Build progress lines kept on the pool record. The builder already narrates
# itself via _LOGGER.info, but this instance writes no home-assistant.log --
# only .log.fault -- so that narration is unreachable, which is how three lost
# seed artists went unnoticed for days. Persisted where it can actually be read.
BUILD_LOG_MAX_LINES: Final = 80

# Share of played tracks that should come from the playlist's seed artists.
# Seeds are the curation surface -- the artists explicitly chosen -- but they
# are only ~14% of a typical pool by track count, and sampling is otherwise
# proportional, so they came through as just another artist among sixty. This
# is a floor, not a quota: a playlist already above it is left alone, and the
# repeat term pushes the realised share below it as seeds actually play.
SEED_TARGET_SHARE: Final = 1 / 3

# Solving the weights for SEED_TARGET_SHARE does not produce it: the repeat
# term penalises seeds precisely as they start winning, and with only 5-9 seed
# artists that bites hard. Simulated against real pools, the realised share
# lands near two thirds of whatever the weights aim at -- stable across pools
# from 6% to 16% seed content -- so the solve aims high and the feedback loop
# brings it back. Measured 2026-09-15: aiming at 50% realises 31%.
SEED_BOOST_CORRECTION: Final = 1.5

# Storage keys
STORAGE_KEY_PLAYLISTS: Final = "ai_jukebox.playlists"
STORAGE_VERSION: Final = 1
# The pool file carries its own version, independent of STORAGE_VERSION —
# a playlist with no pool file simply has no pool, which is a valid state,
# so adding pools needs no migration of the playlists store.
POOL_STORAGE_VERSION: Final = 1

# Config flow / config entry keys

# Playlist config keys
CONF_PLAYLIST_NAME: Final = "name"
CONF_PLAYLIST_PROMPT: Final = "prompt"
CONF_PLAYLIST_BATCH_SIZE: Final = "batch_size"
CONF_PLAYLIST_REFILL_THRESHOLD: Final = "refill_threshold"
CONF_PLAYLIST_TAGS: Final = "tags"
# Structured, LLM-prompt-fed artist list — kept separate from the free-text
# `prompt` so it can be edited independently of prompt text.
CONF_PLAYLIST_ARTISTS: Final = "artists"
# Policy fields, kept out of the free-text prompt so the description says what
# the playlist should sound like and nothing else. Composed into the model's
# instructions by build/pipeline.py (build_instructions), not matched against Music
# Assistant metadata -- see docs/design.md §6 on why year filtering was
# rejected; genre tags are no more trustworthy.
CONF_PLAYLIST_EXCLUDE_ARTISTS: Final = "exclude_artists"
CONF_PLAYLIST_GENRES: Final = "genres"
CONF_PLAYLIST_EXCLUDE_GENRES: Final = "exclude_genres"

# There was a GENRE_DENYLIST_BASELINE/ADDITIONS pair here, matched against MA's
# raw per-track genre tags by genre_denied(), guarding _resolve_track()'s
# similar-artist substitution. All of it went with LLM track generation (see
# docs/design.md §6): pools are the only source of tracks now, nothing
# substitutes, and none of those three symbols exists. Removed 2026-09-28 --
# 28 lines of comments had outlived the code they described, which is why the
# ai-jukebox-drift-check skill still documented the mechanism as live.
# Per-playlist genre policy is now `exclude_genres` above, stated to the model
# rather than matched against metadata.

# Speakers eligible for auto-attach when they join an already-attached
# speaker's group from outside ai_jukebox (e.g. grouped natively in the Sonos
# app or in Music Assistant directly) — see the group_members growth check in
# _async_handle_speaker_update(). Deliberately an allowlist rather than
# "anything that shows up in group_members", so a guest's or one-off speaker
# is never pulled into a session. Chosen in the integration's options.
CONF_MANAGED_SPEAKERS: Final = "managed_speakers"

PLATFORMS: Final = [Platform.MEDIA_PLAYER, Platform.BUTTON]

# (A per-entity backoff constant lived here, for retreating from an ai_task
# entity after repeated generation failures. It went with LLM track generation;
# its runtime counterpart _consecutive_failures is still declared in
# media_player.py but never incremented.)

# Rebuilds run overnight: nobody is listening, so a pool being replaced under
# a live session is not a real risk. The window is an option; it is enforced
# on the rebuild_pool action as well as by the scheduler, so a stray call
# cannot start a multi-minute build mid-evening.
CONF_AUTO_REBUILD: Final = "auto_rebuild"
CONF_REBUILD_START_HOUR: Final = "rebuild_start_hour"
CONF_REBUILD_END_HOUR: Final = "rebuild_end_hour"
DEFAULT_AUTO_REBUILD: Final = True
DEFAULT_REBUILD_START_HOUR: Final = 2
DEFAULT_REBUILD_END_HOUR: Final = 5
# How often the scheduler looks for a build that died mid-flight (an HA
# restart or an unhandled failure leaves `building: true` with nothing
# driving it) and resumes it. Resuming keeps the tracks already collected.
RESUME_STALLED_BUILD_MINUTES: Final = 15
# Playlists carrying this tag are left out of the nightly rotation: build
# fixtures do not need refreshing, and each rebuild costs ~30 model requests.
NO_AUTO_REBUILD_TAG: Final = "test"

# Playlists are config subentries of this type, one per playlist, each with
# its own device. The subentry's unique_id is the playlist slug.
SUBENTRY_PLAYLIST: Final = "playlist"
CONF_PLAYLIST_EXCLUDE_LIVE: Final = "exclude_live"
CONF_PLAYLIST_EXCLUDE_REMIX: Final = "exclude_remix"

# Ordered list of ai_task entities the builder rotates through.
CONF_BUILD_AI_ENTITIES: Final = "build_ai_entities"

# A Music Assistant outage takes every MA-provided entity unavailable at once,
# then they return as `idle` a few seconds later. That idle is indistinguishable
# from someone stopping the speaker, so without this the session ends on
# recovery rather than on the outage. Observed 2026-09-12: all 34 MA entities
# unavailable at 22:17:21, back by 22:17:33, session dead.
#
# Only an ALL-players outage counts. One speaker going unavailable is a speaker
# problem and keeps the existing per-speaker handling.
MA_OUTAGE_RESUME_SECONDS: Final = 300

# The outage ends on the first MA player to come back, but a session's own
# speakers can trail it by a long way: on 2026-11-08 sonos_d was
# back 22s after an MA restart and sonos_a (the session's leader)
# took another 2h20m, because MA's Sonos listener failed on startup. Resuming
# straight away aimed the whole queue at the unavailable leader, which HA
# skips with only a warning, so the "resume" played nothing. How long the
# resume waits for every attached speaker before going ahead on the ones
# that are back.
MA_RECOVERY_SPEAKER_WAIT: Final = 60

# How long to wait for Music Assistant's leadership transfer (move the queue,
# regroup the survivors, resume at the saved position) after a group leader
# leaves. Raised from 5 on 2026-10-18: with the Sonos on AirPlay the heir
# resumed 10.5s after sonos_c was unjoined at 09:08:36.8, though
# ai_jukebox's own mistaken unjoin of that heir may have lengthened it.
DETACH_TRANSFER_TIMEOUT: Final = 12
DETACH_POLL_INTERVAL: Final = 0.25
# A group shrinking is not acted on until every speaker's state has caught
# up. When a leader leaves, MA bounces its heir through idle first, and that
# heir's update can arrive before the leader's own (0.3s ahead, 2026-10-18
# 09:08:37) — deciding on the first update reads the heir as the speaker
# leaving. How long to hold the regrouping guard before deciding.
GROUP_SHRINK_SETTLE: Final = 2
# media_player.join returns as soon as MA has run the command, but HA's
# group_members attribute lags well behind it — 1.3s measured live on
# 2026-09-13. Until it catches up the leader still reports an empty group,
# which the group-shrink rule reads as every member having just left. How long
# to hold the speaker_join guard waiting for the group to become visible.
GROUP_SETTLE_TIMEOUT: Final = 5
