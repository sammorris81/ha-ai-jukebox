# AI Jukebox — implementation notes

This was the plan for the pool-first rebuild, kept because the *reasoning*
behind several decisions is not recoverable from the code: which Home
Assistant APIs refused to do what, and why particular shapes were chosen over
the obvious ones. The work itself is done and merged.

Work required to reach the design in `design.md`. Everything lives
in `custom_components/ai_jukebox/` (its own git repo — commit there, not in the
main config repo).

Prototype code to port from lives in `~/jukebox-study/`. That tree is a
scratchpad, not a dependency: nothing should import from it afterwards.

**2026-09-20:** that port is long done, and `~/jukebox-study` (plus the
`~/jukebox-dev`/`~/jukebox-dev2` shards) has been deleted. Its measurement
record — model comparisons, wording experiments, depth runs — is preserved in
`~/dev/jukebox/archive/study/`. The builder now lives in `build/`, and
`~/dev/jukebox/harness/` is how it gets run outside HA.

---

## A. New — the builder

New package `build/` inside the integration. All Music Assistant access goes
through `async_ma_call` (the HA MA integration), **not** a standalone websocket —
that removes the reconnect and keepalive machinery entirely.

### A1. `build/llm.py`
Model calls with fallback. Replaces direct Gemini HTTP plus the hardcoded
`MODELS` list.

**HA's `structure` is not JSON Schema**, and three of its limits were only
found by probing the live service. All are load-bearing:

- An array of typed objects needs an `object` selector with `multiple` **and**
  nested `fields`. A bare `object` selector collapses every row to
  `{"json": "<free text>"}`; raw JSON Schema is rejected outright with a 400.
- A **nested** field may carry only `selector`. Give it a `description` and
  the whole call 400s, though the top-level entry may have one.
- The `number` selector rejects `multiple`, so a list of integers must be a
  `text` selector with `multiple`, parsed back with `int()`.
- An `object` selector with `multiple` **does** nest inside another one
  (probed 2026-09-14). The rows come back as real objects, not the `{"json":
  ...}` collapse a bare `object` produces. This is what lets track picking
  return `{n, why}` per track while keeping the per-album grouping — and with
  it, priority order as structure and the empty-list rejection case. The
  nested list must use `row_field()`, not `rows()`: a nested field carrying
  `description` or `required` 400s the call, per the limit above.

That last one has a behavioural consequence worth knowing: declaring the list
as text invites the model to answer with **titles instead of numbers**, which
it does roughly half the time. Both selection and track-picking therefore
accept either -- a number indexes the listing, a title is matched back against
the exact strings just sent. Handling it in only one of the two places cost a
whole silent batch (0 tracks, no error).

- Rotate across the configured `ai_task` entities in order (see E3), moving to
  the next on failure rather than always starting from the first — a full
  refresh would otherwise drain one entity's daily quota before failing over.
- Backoff on rate-limit responses; surface a typed error when all entities fail.
- Structured-output schema per call site.

### A2. `build/roster.py`
Replaces `gen_artists.py` **and** `enrich_refs.py` — one call, not two stages.

- Input: prompt + seed artists. Output: `[{artist, album}]` covering **both**
  seeds (echoed verbatim) and discovered artists.
- Prompt must state: primary-credit albums only, no collaborations; echo seed
  names exactly, do not correct spelling.
- Dedupe on lowercased name, preferring the entry that has an album; append any
  seed the model dropped as a bare name (safety net, not the main path).
- Not cached between runs.

### A3. `build/resolve.py`
Port `albums_for` + `_mapping_via_album` from `fetch_albums_v3.py`.

- **Delete** the per-candidate reference-album disambiguation loop (it fetched a
  catalogue per same-name candidate and never changed the answer in 21 tests).
- Keep: exact-name match → album-first fallback → empty-catalogue retry.
- Album-first: combined `"<artist> <album>"` query, `providers=[apple]`, title
  match (exact → containment → top hit), then `music/albums/get` to read the
  artist's real `provider_mappings`.
- Log, do not reject, when the resolved name shares no tokens with the input —
  `Watchhouse` and `Sananda Maitreya` are both correct.
- Return the artist's Apple `item_id`; it is the cache key and goes on every
  track as `artist_id`.

### A4. `build/catalogue.py`
Port `album_cache.py`, **rekeyed from artist name to `artist_id`**.

- Keep the TTL banding, jitter, lazy refresh, per-run refresh budget,
  stale-if-error.
- Fetch via `music/artists/artist_albums`, filter to album/compilation/
  soundtrack, drop `BAD`-matching titles, dedupe reissues.
- Store `{title, item_id, provider, year, album_type}` per album — track picking
  needs titles, so bare IDs are not enough.

### A5. `build/select.py`
Port the album-selection half of `select_albums.py`. **Drop the depth pass and
its cache entirely** — track picking replaces it.

- Four artists per call, keep 2–8 albums each, relative framing.
- Cache scoped to a single build run only (crash resume), not across refreshes.

### A6. `build/tracks.py`
Port `dedupe_album_tracks` from `build_pool_v3.py`.

- Variant keywords matched against **both** the track `name` and MA's `version`
  field — never "version is non-empty" (`Remastered 2012` on all 28 Mellon
  Collie tracks would delete the album).
- Collapse repeats of the same base title, lowest `(disc_number, track_number)`
  wins.
- This is the only place `normalize_track` is needed.

### A7. `build/pick.py` — new, no prototype beyond a test script
- Input: albums with real track listings, grouped four artists per call.
- Output per album: track indices **in priority order**, empty list if nothing
  fits.
- Prompt uses affirmative instruction ("return them in PRIORITY ORDER, best fit
  first"); do not rely on the negation "do not return in album order".
- Store each pick's position as `rank` on the track record.

### A8. `build/pipeline.py`
Per-playlist orchestration.

1. Set `building: true`, clear the pool (full replace).
2. Roster → resolve all artists → warm catalogues.
3. Assemble batches of four artists, **distributing seed artists across
   batches** so early ones contain a mix.
4. Per batch: select albums → fetch tracks → persist batch listing to disk →
   pick tracks → append to pool → delete the persisted listing.
5. Clear `building`, set `built_at`.

- Append per batch so playback works seconds into a rebuild.
- A persisted batch file left on disk *is* the resume state; it also preserves
  the exact model input for inspection when a pick looks wrong.
- Resume continues from pool contents but restarts the interrupted batch.
- Batch failure: retry in place with backoff. Whole-task failure: leave
  `building: true` for the resume paths in F2.

---

## B. Player changes — `media_player.py`

### B1. Rewrite `_sample_from_pool`
Per design §4:

```
recency = 10 ** (-6 * (1 - min(dt, 12h) / 12h))
repeat  = plays_artist + 2 * plays_album      # plays within last 8h
weight  = recency * (1 / (1 + repeat)) ** 0.4
key     = log(random()) / weight              # take MAX
```

- **Log-space keys are required**, not an optimisation: `random() ** (1/w)`
  underflows to `0.0` at `w=1e-6` and destroys the ordering the decay exists to
  create. Guard `random() == 0.0`.
- Remove the `pool_handoff_min` check — there is no alternative source to hand
  off to.
- Remove the cooldown-relax fallback — decay makes least-recently-played
  emergent.
- `plays_artist` / `plays_album` key on `artist_id` / `album_id`, not strings.

### B2. Record plays
On each confirmed track change, append the timestamp under the track's URI in
the new plays store (C2).

### B3. `_reconcile_now_playing`
- Match on `media_content_id` (verified: Sonos, HomePod and Nest all report
  `apple_music://track/<id>`).
- Remove the `artists_match` near-miss fallback — it existed only to forgive
  hallucinated titles.

### B4. Refill size
~5 tracks per refill while `building` is true, otherwise the normal 15–20, so
each refill during a build draws from a materially larger pool.

### B5. Delete
`_generate_batch`, `_resolved_prompt`, `_fail_generation`, `_resolve_track`,
`_search_apple_music_tracks`, `_similar_artist_names`, `_genres_for_candidate`,
`_check_drift`, `async_grow_pool`, `_grow_pool_once`, `_pool_add_tracks`,
`_mirror_to_pool`. Strip MA-playlist fields from `_refresh_pool_stats`.

---

## C. Storage — `store.py`

### C1. Pool record
Add `album_id`, `rank`, `building`, `built_at`. Remove `ma_playlist_id`,
`ma_playlist_uri`, `rejected`, `last_grown`, `last_audited`,
`empty_growth_cycles`.

### C2. New plays store — `.storage/ai_jukebox/plays/<slug>.json`

```json
[{"uri": "apple_music://track/1443185025",
  "artist_id": "1419227", "album_id": "1443184951",
  "at": "2026-09-12T19:04:22Z"}]
```

Only what sampling needs — no display fields. **Pruned to the last 12 hours on
rebuild**, which is exactly the span the recency curve reads. Entries whose
`uri` is absent from the new pool simply never match, so no reconciliation is
needed.

That keeps a rebuild invisible to the listener. Clearing outright would also
have been acceptable — with a ~1,200-track pool and ~60 plays the previous day
it costs about one repeat per session — but pruning is the same amount of code
and has no discontinuity.

The file is naturally bounded with no background pruning: the nightly cycle
rebuilds each playlist every ~5 days, and each rebuild truncates it.

There is deliberately no long-lived listen log. It was considered and dropped —
no concrete use case, and it would have forced an append-only format, display
fields and a rotation policy to avoid rewriting a growing file every few minutes.

### C3. Existing string history — delete
`history`/`history_sources` in `.storage/ai_jukebox/queue/<slug>.json` have no
remaining reader. Of the four read sites, two are in `_sample_from_pool` (being
rewritten against the plays store), one is in `_generate_batch` (deleted), and
one seeds a cold start in `async_session_start` — which only existed because
generation was slow, and is unnecessary when sampling a pool is instant.

Point `clear_history` at the plays store instead.

---

## D. Deletions

- `metadata_cache.py` — whole file; all six call sites are in deleted methods.
- `ma_playlist.py` — keep `async_ma_call` and `normalize_uri`; delete
  `pool_playlist_name`, `async_find_playlist`, `async_ensure_playlist`,
  `async_get_tracks`, `async_add_tracks`, `async_remove_positions`, `_async_confirm`.
- `track_processing.py` — remove `genre_denied`, `filter_tracks`,
  `parse_ai_response`, `parse_json_tracks`, `parse_drift_response`,
  `artists_match`.
- `const.py` — generation system prompts, `GENRE_DENYLIST_BASELINE`/`ADDITIONS`,
  drift constants, `CONF_POOL_HANDOFF_MIN`, pool-growth constants.

---

## E. Services and config

### E1. `ai_jukebox.rebuild_pool`
Entity-targeted. Spawns a background task in the shape of the old
`async_grow_pool` (guard against concurrent runs, `ACTIVITY_REFILLING`, restore
state in `finally`). Takes `force` so a manual rebuild works outside the 02:00–
05:00 window.

### E2. `save_playlist`
Add a generate option — save alone must not rebuild, or every prompt edit
triggers one.

### E3. Options flow
Currently drift-tuning only, which is being deleted. Repurpose to select the
ordered list of `ai_task` entities the builder rotates through.

### E4. Remove `grow_pool`
Superseded by `rebuild_pool`.

---

## F. Scheduling

### F1. Nightly automation
02:00–05:00 local, chosen by least-recently-built from `built_at`. Hourly
trigger, three-hour window, one playlist per firing — three per night, so a
full 27-playlist cycle takes about nine nights. ~30 model requests per
playlist against a ~1,000/day ceiling, so the rate is set by the window rather
than by budget.

The action must compute its pick in **one** template. It was split across
three `variables` steps, and a script variable cannot carry State objects: HA
renders each template to a string and `literal_eval`s it back, which fails on
`[<template TemplateState(...)>]` and silently leaves a string behind. The
later steps then iterated its characters and the automation rebuilt nothing
while reporting success.

### F2. Resume
Two paths, neither waiting for 02:00: a check on HA startup (an in-flight build
dies with the process and would otherwise leave `building: true` forever), and a
periodic sweep for pools still flagged building.

---

## G. Reset

Delete every existing pool and rebuild from scratch. No migration: the pool
schema changes, play history moves from strings to URIs, and old pools were
built by the superseded pipeline.

Consequence: cooldown is empty on first run, so early playback may repeat until
plays accumulate.

---

## Open questions

1. **`k = 2`** is a starting guess; only listening will tell.
2. Does a rebuild run while that playlist is actively playing? It can (the pool
   is only read at refill), but the pool changes under a live session.
3. ~~Track-stage rejection is unproven~~ — **resolved 2026-09-14.** Requiring a
   `why` per pick made it fire immediately: 2-5 empty albums per batch, where
   the unjustified prompt produced 0-1. The path was always fine; nothing had
   made the model selective enough to use it.

## Working agreement

Do this on a branch in `custom_components/ai_jukebox/` (its own git repo,
separate from the main config repo). Roughly 800 lines are deleted and a new
subsystem is added, so it is not a series of small safe commits on the trunk.
