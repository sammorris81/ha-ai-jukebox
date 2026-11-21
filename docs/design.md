# AI Jukebox — Design

AI Jukebox is one Home Assistant integration with two subsystems that share no
code: a **builder** that decides what belongs on a playlist, and a **player**
that decides what to play next. They meet at one place — a pool file on disk.

---

## 1. The core idea

Ask a language model only questions it can be held to.

The original design asked a model to name tracks for a playlist, then resolved
that text against Music Assistant. Measured over many runs, that failed:
20–50% of proposed tracks did not exist, only 23% of the intended "anchor"
artists appeared, and 93% of the repeats came from a handful of signature
songs. The model was doing recall, and there was nothing to check its recall
against.

Every question the builder asks now has a ground-truth check behind it:

| Question asked | Checked against |
|---|---|
| Which artists suit this playlist? | Does Music Assistant resolve the name? |
| Which of *this artist's* albums suit it? | A list of their real albums |
| Which tracks on *this album* suit it? | The album's real track listing |

The model never has to remember a fact. It is shown real data and asked to
judge it. That single change is why the pipeline works, and it is the rule to
apply to any new stage.

---

## 2. Data model

### Playlist record (a `playlist` config subentry)

One config subentry per playlist (unique_id = the slug), each with its own
device. Edited from the device's **Configure** button, or through the
`save_playlist` action, which writes the same subentry. Before 1.2 these lived
in `.storage/ai_jukebox.playlists`; the 1.2 migration imports from that file
and leaves it in place, untouched, so an older build can still be rolled back
to. The fields that matter to the builder:

- `name` — what the playlist is
- `prompt` — **a description of what it should sound like, and nothing else.**
  Not a place for rules; those have fields.
- `artists` — **seed artists**. This is the durable curation surface: the
  artists you have explicitly decided belong. Everything else about a pool is
  derived and disposable.
- `exclude_artists` — artists that must never appear
- `genres` — optional allow-list of genres to stay within
- `exclude_genres` — genres that must never appear. Listing christmas, xmas or
  holiday(s) here also turns on the holiday title filter (§3.6); it is the one
  part of this field that is enforced, not just advised.
- `exclude_live`, `exclude_remix` — whether live and remix albums and tracks
  are dropped, at both the album and track stage (§3.6)

**Why policy is in fields rather than prose (2026-09-28).** The prompt used to
carry everything: description, genre exclusions, an artist-calibration sentence,
and a `{artists}` placeholder that nothing ever substituted. Three problems came
from that. The placeholder had to be stripped before the model saw it, by
splitting the text on the literal string `"Be sure to consider"` — so anything
written after that phrase was silently discarded, and a playlist wording it
differently lost a whole line instead (Powerhouse Vocal Precision lost "every
pick must belong in their company"). Genre rules reached only the stages that
happened to receive the prompt. And there was no way to tell, from the stored
record, what the model was actually told.

`build/pipeline.py: _instructions()` now composes the description plus one line
per non-empty policy field, and that single string is passed to the roster,
album and track calls alike — so a rule stated once applies at every stage
instead of by accident of placement.

**These fields are not metadata filters.** They are rendered into the
instructions and judged by the model, exactly as §1 requires. `exclude_artists`
is the one exception: it is *also* enforced after the roster returns, because
artist identity has a ground-truth check behind it (the name resolves to an
Apple id). `exclude_genres` gets no such check — see §6.

### Pool record (`.storage/ai_jukebox/pool/<slug>.json`)

Written by the builder, read by the player. Nothing else writes it, so no
locking is required.

```json
{
  "building": false,
  "built_at": "2026-09-12T03:14:00Z",
  "tracks": [
    {"uri": "apple_music://track/1443185025",
     "artist_id": "1419227",
     "album_id": "1443184951",
     "artist": "Pat Benatar",
     "title": "Fire and Ice",
     "album": "Precious Time",
     "duration": 218,
     "rank": 2,
     "why": "Soaring dual-vocal hook over a hard rock riff."}
  ]
}
```

`uri`, `artist_id` and `album_id` are identity; `artist`, `title`, `album` are
for display. The pool is an **unordered set** — every ordering decision belongs
to playback.

`why` is the model's one-line reason for picking the track, from §3.5. Like
`rank` it is **recorded but not used by playback** — nothing in sampling,
matching or refill may read it, and it is display and debugging only. It is the
one builder output with no ground-truth check behind it (§1), so it must never
become an input to a decision. Absent on pools built before it existed, and on
anything the player reports as `untracked`; both render as no reason rather
than an error.

`rank` is the track's position in its album's pick list from §3.5, where the
model returned tracks best-fit-first. It is **recorded but not used by
playback**: ranks are per album, so rank 1 on a twelve-track album and rank 1 on
a three-track album are not comparable, and the model's calibration drifts
between calls. Weighting by it would also front-load the best material and let
the playlist decay. It is kept because it is free and makes "do the picks
predict what gets skipped" answerable later.

There is deliberately no per-track `added_at`: a rebuild replaces the pool
wholesale, so every track shares the pool's `built_at`.

### Play history (`.storage/ai_jukebox/plays/<slug>.json`)

Owned by the player. Timestamps of every play, keyed by track URI:

```json
{"apple_music://track/1443185025": ["2026-09-11T19:04:22Z", "2026-09-12T08:31:10Z"]}
```

**Why URIs and not `"Artist - Title"` strings.** The old history was text, so
every comparison against the pool needed normalising — the exact string
matching this design removes everywhere else. Playback plays a URI and the
speaker reports that same URI back, so identity is exact.

**Why a separate file rather than timestamps on the pool record.** Three
reasons. The pool stays build-written and play-read, so the two subsystems never
contend for the same file — which matters precisely during a rebuild, when
playback is drawing from a pool that is being appended to. A rebuild can nuke
and replace the pool without destroying listening history, including for tracks
that drop out of the new pool. And a play mutates one timestamp rather than
forcing a rewrite of a thousand-track file every few minutes.

**Why identity is an ID and not a normalised string.** Playback plays a URI and
the speaker reports that same URI back (verified across Sonos, HomePod and Nest
— all expose `apple_music://track/<id>` in `media_content_id`). So "did this
track play" is an exact comparison. Artist play-counts key on `artist_id` for
the same reason. String normalisation exists only inside the builder, where the
question is different — see §3.5.

---

## 3. The builder

> Testing: `~/dev/jukebox/harness/` runs this builder outside Home Assistant
> against a sandbox `.storage`, so a change can be exercised without an HA
> Restart and without touching live data. See `~/dev/jukebox/README.md`.


Runs inside HA as a background task, started by the `ai_jukebox.rebuild_pool`
service against a target entity.

### 3.1 Roster

One model call produces the artist list: the seed artists echoed back, plus
newly discovered ones, each with **one reference album** where that artist is
the primary credited artist.

**Why seeds are returned rather than merged in afterwards.** They used to be
concatenated as bare strings after generation. That created two bugs. Seeds had
no reference album, so they were the only artists that could not use the
album-first lookup in §3.2. And because the roster was only generated once,
seeds added *later* never reached it — Sarah McLachlan sat in Ethereal
Storyteller's seed list for weeks and never appeared in the pool.

**Why the roster is regenerated every refresh, not cached.** Reusing it means
matching artist names across runs, which is exactly the operation that has
failed repeatedly. Seeds are the stability guarantee; the discovered artists are
allowed to drift, and some drift is desirable.

That drift is between **successive rebuilds**, not within one. A resume is not a
refresh: it finishes the build it interrupted, replaying that build's own batch
plan rather than deriving new batches from a new roster (§5). It does still
regenerate the roster — that call's output is simply unused when a plan exists. Read the other way
round -- as this was, until 2026-09-30 -- it means a resume can silently
substitute different artists for the ones it was supposed to recover.

**Why the reference album must be primary-credit.** A collaboration resolves to
the wrong artist — asking for João Gilberto and getting *Getz/Gilberto* resolved
him to Stan Getz.

### 3.2 Artist resolution

Name → Apple Music artist ID, in two steps:

1. Search by name. One exact match wins.
2. Otherwise **album-first**: search `"<artist> <album>"` filtered to the Apple
   provider, match the album, then `music/albums/get` to read the artist's real
   `provider_mappings`.

**Why album-first replaced same-name disambiguation.** The old path fetched each
same-name candidate's catalogue and kept whoever released the reference album.
It cost one catalogue fetch per candidate, and across 21 same-name artists —
including 4- and 5-way ties — it never once changed the answer that
first-exact-match would have given. It also could not help when the name itself
is wrong, which is the case that actually loses artists.

Album-first is a fixed two calls and handles all of it, because it never depends
on the artist string: `Pink`→`P!nk`, `Mandolin Orange`→`Watchhouse` (a band
rename), `Terence Trent D'Arby`→`Sananda Maitreya` (a legal name change),
`Seals and Crofts`→`Seals & Crofts`, `Beyonce`→`Beyoncé`. Before it existed,
40 lookups per build resolved to nothing and were silently dropped.

**Why the album search includes the artist name.** Searching the album title
alone matched Wayne Shorter's *Juju* for Siouxsie and the Banshees and GRiZ's
*Sunshine* for S Club 7 — injecting entire wrong catalogues. Querying both
signals together fixed every such collision.

**Why `music/albums/get` is needed.** The artist stub on a search hit is
degraded: its `item_id` is the *name* (`"Beyoncé"`) with no `provider_mappings`.
Only the full album record carries the real numeric ID.

**A mapping being found is not proof it was the right one.** The resolved
artist's catalogue is checked against the reference album §3.1 collected, and
if it is absent the lookup retries via album-first. Two distinct failures need
this. A search hit's `provider_mapping` sometimes carries the *name* as
`item_id` ("Halsey") and returns nothing — the original case. And an exact name
match can land on a real but **wrong** Apple entity: "Florence Welch" is a
covers act with three releases, not Florence + The Machine, so the roster
naming the singer rather than the band put *Shake It Out* and a Lady Gaga cover
into a pool under her name. That one returns a small, entirely plausible
catalogue, so an emptiness test never caught it.

The check also runs against the cached catalogue, not just a fresh fetch: a
wrong id, once cached, would otherwise be trusted until the TTL expired. The
retry is only accepted if the alternative actually contains the reference album
(or the original had nothing at all) — a reference album can be legitimately
missing from `artist_albums`, and that must never downgrade a correct
resolution.

### 3.3 Catalogue

`music/artists/artist_albums` via the Apple mapping, filtered to
album/compilation/soundtrack, deduped.

**Why not `music/search`.** Search returns a relevance slice, not a catalogue:
Queen came back with 12 albums where `artist_albums` returns 155, with *A Night
at the Opera* absent.

**Cached on disk, keyed by `artist_id`, with a TTL.** Keying by ID rather than
name means the cache is immune to the spelling problem — `Pink` and `P!nk` share
one entry — and the TTL then means something real: "has this artist released
anything new." Accepted consequence: a new release stays invisible until the
entry expires.

### 3.4 Album selection

Per artist, grouped four artists to a call: *which of this artist's albums
belong on this playlist?* Keep 2–8.

**Why per artist and not per playlist.** Asking one call to judge all ~1,050
candidates makes the model apply an absolute bar, and it applies it
inconsistently: two runs agreed on only 44% of picks and returned *zero* albums
for 11 and 20 artists respectively — artists the model had itself just named.
Chunking made coverage worse, so it was never about list length; the question
was wrong. Framed per artist it becomes relative — "the best of *this* artist" —
and coverage went to 55 of 56.

### 3.5 Track selection

For each selected album, the model is shown the **real track listing** and asked
which tracks belong, **in priority order**.

**Why this replaced a depth rating.** The previous stage rated each album
`full`/`most`/`some`/`few` and the fill took that fraction *at random*. So the
model would correctly identify a 40% album and then dice-roll which 40%. Direct
track selection is better calibrated as well as more precise: asked to rate
*Mellon Collie*, the depth pass said `most` (≈17 of 28 tracks); asked to pick
tracks, it took 4 — *Tonight Tonight*, *Porcelina*, *Galapogos*, *Thru the Eyes
of Ruby* — precisely the orchestral ones on an album that is otherwise grunge.

**Why this is not the failure mode of §1.** The model is not recalling track
names, it is selecting from a supplied list. Verified: picks arrived in album
order for only 1 of 47 albums, where the signature of a model echoing its input
was 93%. It put *Bohemian Rhapsody* first from position 11, and rejected Muse's
most famous singles (*Hysteria*, *Time Is Running Out*) in favour of the
orchestral tracks — popularity-matching would have done the opposite.

**Why each pick carries a reason.** The model is asked for `why` alongside each
track number — the judgement it was already making, said out loud. Measured on
the two batch listings a dead build left on disk (50 albums, 559 tracks,
2026-09-14): asking for it roughly **halves** the picks, from ~3.0–4.2 tracks
per album to ~1.6–1.9. That is not a different selection — **96–100% of what it
keeps was also picked by a run without the reason**, so it prunes the same
ranking down to what the model can defend. Echo-proneness and mean picked track
position are unchanged across both arms, so it costs neither of the failure
modes above. Smaller, higher-conviction pools were judged the better trade;
§3.7's "pool size is an output" argument applies unchanged.

A caution on the 1-of-47 figure above: it does **not** reproduce on the current
`ai_task` entities. Re-measured 2026-09-14, baseline runs put 19–58% of albums
in ascending order raw — though corrected for pick count (two picks are
ascending half the time by luck) both arms sit at 1.2–3.6x the random rate,
nowhere near an echo signature. Treat 1-of-47 as historical.

**Why two passes rather than one.** Album selection and track selection are
different judgements. A merged call goes straight to tracks and never forms a
view about the album, producing a thin scatter across many albums instead of
deep cuts from the right ones. The album pass is also the only stage with a
demonstrated ability to reject outright.

### 3.6 Track listings and variants

Album listings need cleaning before the model sees them. Two rules:

1. **Variant keywords** in either the track name or MA's `version` field —
   mono, instrumental, demo, take *n*, alternate, session, reimagined, radio
   edit, and so on.
2. **Collapse repeats of the same base title**, keeping the lowest
   `(disc_number, track_number)`.

**Why keywords and not "`version` is non-empty."** Every track on *Mellon
Collie* carries `Remastered 2012`. Treating presence as variant-ness deleted the
entire album — silently, since an empty album just makes a smaller pool.

**Why base-title collapse and not a disc filter.** Smashing Pumpkins' *Adore*
returns 90 tracks over 6 discs with "To Sheila" appearing four times under four
IDs. Disc 1 is the original album, so lowest-disc wins — but a rule like "more
than two discs means box set" would mangle genuine double albums, and *Mellon
Collie* is exactly that. With both rules: *Adore* 90→24, *Mellon Collie* 28→28,
Queen's *Innuendo* 17→12 (it is a 12-track album), and ordinary albums untouched.

**Per-playlist title filters (2026-11-21).** On top of those two rules, album
titles (in `build/catalog.py`) and track names (in `dedupe_album_tracks`) are
filtered by keyword in four groups. Karaoke, tribute, instrumental, demo and
commentary releases are always dropped. Live, remix and holiday titles are
dropped per playlist: live and remix follow `exclude_live`/`exclude_remix`, and
holiday (christmas, xmas, noël, holiday, yule, santa, merry, winter wonderland)
is on when `exclude_genres` names christmas, xmas or holiday(s). The add-playlist
form pre-fills `exclude_genres` with "christmas, holiday", so that stays the
default.

Before this, the album stage applied all four groups unconditionally. The two
toggles reached only track names, and the holiday group was on for every
playlist, which left the one Christmas playlist with almost no Christmas music:
every album titled *Merry Christmas* or *Christmas Portrait* and every song with
"Christmas" or "Santa" in the name was gone, so the pool fell back on carols
whose titles happen to avoid the keywords ("O Holy Night" eight times) and on
artists' regular albums.

The catalogue cache (`catalog/<artist_id>.json`) is shared by every playlist, so
it stores albums filtered by the always-on group only (`"schema": 2`), and each
playlist's three filters run when an entry is read. An entry with no `schema`
predates that and is missing the live, remix and holiday albums. It still
serves a playlist that excludes all three, which is every playlist by default.
A playlist that allows any of them treats it as a miss and refetches, which
upgrades the entry for everyone. That avoids refetching thousands of artists at
once.

This is also where `normalize_track` lives, and the only place it is needed: the
builder must recognise that the same song appearing on two of an artist's albums
is one song. Playback never asks that question.

### 3.7 Assembly

Every track the model picked goes into the pool. No quota, no scaling, no cap,
no sorting.

**Why no target size.** An hour target was arbitrary, and trimming would discard
tracks the model explicitly endorsed. Pool size is an *output*. If a playlist
comes out implausibly small that is a signal worth investigating, not something
to paper over.

**Why no ordering.** Ordering depends on listening history, which only playback
knows. The builder decides membership; the player decides order.

---

## 4. The player

Draws from the pool and manages sessions, speakers and the queue. It never
generates, resolves or writes tracks.

**Sampling** is entirely weight-based. There are no exclusions and no
fallbacks — every track in the pool is always a candidate, just at wildly
different odds.

```
recency = 10 ** (-6 * (1 - min(dt, T) / T))     dt = time since this track
                                                 last played, T = 12 hours
repeat  = plays_artist + k * plays_album         (plays within the last 8 hours)
seed    = B if this track's artist is a seed, else 1

weight  = recency * (1 / (1 + repeat)) ** alpha * seed      (alpha 0.4)
```

The recency term runs from 1e-6 for a track just played to 1.0 at twelve hours:

| Since last play | Multiplier |
|---|---|
| 20 min | 1.5e-6 |
| 2 hours | 1.0e-5 |
| 6 hours | 1.0e-3 |
| 8 hours | 1.0e-2 |
| 12 hours | 1.0 |

**Why seeds are boosted.** `artists` is the curation surface — the artists you
explicitly decided belong — but sampling is otherwise proportional to track
count, and seeds are only ~14% of a typical pool (6% to 35% across playlists).
They came through as one artist among sixty. `B` is solved per pool rather than
fixed, because a flat multiplier means something different at 6% than at 35%:

```
B = max(1, (aim / (1 - aim)) * ((1 - s) / s))    s = seed share of the pool
                                                  aim = target * 1.5
```

Only pools *below* target are lifted; one already carrying its seeds is left
alone rather than pushed further. The 1.5 is not arbitrary — solving directly
for the target undershoots badly, because the repeat term penalises seeds
exactly as they begin to win, and with 5-9 seed artists that bites hard.
Simulated against real pools, realised share lands near two thirds of whatever
the weights aim at, stably across pools from 6% to 16% seed content. Measured:
Soulful Rock & Gravitas 4% → 31%, Yacht & Harmony Groove 9% → 31%, Ethereal
Storyteller untouched at 41%.

**Why not by `artist_id`.** Seeds are names in config and tracks carry IDs, and
§2 is otherwise emphatic that identity is an ID. The exception holds because
the roster echoes seed names verbatim (§3.1), so the pool carries the seed's own
spelling — measured at 96%. The remainder are seeds absent from the pool
altogether, which no weighting can rescue.

**Why this is not the `rank` weighting §2 rejects.** `rank` was rejected as a
playback input because ranks are per album and not comparable, and because
weighting by it front-loads the best material. Seed membership is binary,
comparable everywhere, and authored by a person rather than inferred by a model.

**Why decay rather than a hard "no repeat within N" rule.** Hard exclusion has a
cliff — it is either irrelevant (plenty of candidates) or catastrophic (nothing
is eligible and a relax rule has to be invented). Decay is monotonic in time
since last play, so when everything is recent the least-recently-played track
automatically carries the highest weight. A twenty-track starter pool rotates
correctly with no starvation branch at all. The same argument rules out an album
cooldown: if ten tracks are eligible and eight are needed, excluding by album
achieves nothing, whereas weighting still expresses a preference.

**Why 12 hours.** It covers a day's listening, which is what actually matters —
on a healthy pool the decay barely binds anyway. With ~1,200 tracks and ~60
plays a day, uniform sampling already gives an expected repeat interval near 20
days; pool size is what prevents repeats across a week. The decay exists to
prevent repeats *within a session*, and shorter recovery also means less
distortion on a thin pool.

**Why the repeat terms are additive.** Album plays are a subset of artist plays,
so summing them deliberately double-counts the album: a track from a
heavily-played album is penalised once as its artist and again as its album.
`k` tunes how much repetition of an *album* matters independently of the
artist — raising alpha alone would penalise both equally. Start at `k = 2`.

**Why alpha 0.4.** Pure inverse weighting (1.0) over-corrects: 90/10 listening
flips straight to 10/90. Alpha 0.4 lands nearer 30/70.

**Why an 8-hour repeat window inside a 12-hour recency curve.** Artist and album
effects should fade before track repeats do — hearing the same artist again
after eight hours is unremarkable, hearing the same *track* is still mildly
suppressed for another four.

### Numerical stability

Selection is Efraimidis–Spirakis sampling without replacement, which normally
assigns each candidate the key `random() ** (1 / w)` and takes the largest.
**That form must not be used here.** With `w = 1e-6` the exponent is 1,000,000,
and `random() ** 1000000` underflows to exactly `0.0` for any draw below about
0.9999 — so every recently-played track ties at zero and the ordering among
them is lost. That ordering is the entire point of the decay: it is what makes
a thin pool rotate by least-recently-played instead of at random.

Compute the key in log space instead:

```
key = log(random()) / w          # log(random()) is negative
```

and take the **maximum**. This is monotonically equivalent to `random() ** (1/w)`
— dividing a negative number by a smaller `w` pushes it further from zero, so a
larger weight still wins — but it stays in a range doubles represent comfortably.
For `w = 1e-6` keys land around -1e6 rather than underflowing; for an unplayed
track near `w = 0.64` they land near -1.

Guard `random()` returning exactly `0.0`, which would make `log` undefined.

**Reconciliation.** What actually played is matched on `media_content_id`, the
exact MA URI. No fuzzy artist matching — that existed only to forgive
hallucinated titles from the old generation path.

**No handoff threshold.** With one source of tracks there is no branch to make.

**While `building` is true**, the queue refills ~5 tracks at a time instead of
15–20, so each refill draws from a materially larger pool as batches land.

---

## 5. Lifecycle

**Creating a playlist.** `save_playlist` stores the record. `save_playlist` with
generation also starts a full build. A batch of four artists takes about four
seconds end to end, so a typical seed list yields playable tracks in 10–30
seconds.

**Playing during a build.** The builder appends each batch to the pool as it
completes, and the player re-reads the pool on every refill, so playback works
within seconds of a rebuild starting. Early playback is narrower by definition —
the smaller refill size in §4 is what stops it locking in.

**Scheduled refresh.** Rebuilds run between 02:00 and 05:00 local, chosen by
least-recently-built. The trigger is hourly and the window is three hours, and
each firing rebuilds exactly one playlist — so **three per night**, and a full
cycle of ~27 playlists takes about **nine nights**.

There is no age threshold anywhere: nothing asks whether a pool is older than
some deadline. The cycle length is purely emergent from (playlists ÷ rebuilds
per night), so adding playlists lengthens every playlist's interval.

Nine nights is not a quota decision. Three rebuilds is ~90 model requests
against a free-tier ceiling near 1,000/day, so the window could be widened
considerably before budget bound it; staggering at all is what stops a single
all-at-once rebuild consuming most of a day, and the current rate is simply
what an hourly trigger in a three-hour window produces.

**Failure.** A failed batch retries in place with backoff
(`BUILD_BATCH_ATTEMPTS`, `BUILD_BATCH_BACKOFF_SECONDS`). A dead build leaves
`building: true`, which is resumed on HA restart and by a periodic sweep — it
should not wait for the next 02:00 window. That batch's fetched track listings
are persisted to disk and deleted on success, so a crash leaves the exact model
input available for inspection.

**The batch plan (2026-09-30).** When the batches are first computed they are
written to the pool record as `batch_plan`: for each batch, its index, its
artists **with their reference albums**, an attempt count and a done flag. A
resume replays the unfinished entries of that plan directly, reconstructing each
batch exactly as proposed — the reference album is what
`catalog.resolve_artist()` needs for its album-first lookup, so a planned artist
resolves whether or not this run's roster names it.

What a resume still does, and arguably should not: it calls `build_roster()`
unconditionally (`pipeline.py:175`) and resolves catalogues for the whole roster
before the plan is read. That work is discarded when a plan exists. Skipping it
would mean branching before the roster call, which was judged riskier than the
wasted request; the plan's independence from the roster is what makes the replay
correct either way.

The retry budget is **per batch**, `BUILD_MAX_BATCH_ATTEMPTS = 3` attempts each
across resumes (each itself `BUILD_BATCH_ATTEMPTS` tries in-run). A build defers
while any batch still has attempts left, and abandons only batches that have
spent all of theirs — those artists, and only those, are recorded in
`failed_artists`.

**What this replaced, and why.** The budget used to be one resume for the whole
build (`BUILD_MAX_RESUME_ATTEMPTS = 1`), and batches were re-derived from the
freshly regenerated roster after skipping artists already in the pool. So a lost
artist was re-attempted only if the new roster happened to name it again, and after one failed resume the build
completed as finished — leaving those artists absent until the next full
rebuild, roughly nine nights later. The Orchestral Arena lost 18 artists that
way on 2026-09-28: 5 of 8 batches failed, the single resume failed the same
batches again, and the build closed itself out.

---

## 6. Deliberately rejected

- **A Music Assistant playlist per pool.** `playlist_tracks` caps at 500 rows
  and ignores `offset`, so pools larger than that cannot be reliably read back.
  More importantly, AI Jukebox does its own sampling, so the MA playlist was a
  write-only mirror. Removing it deleted the publisher, the reconciliation
  problem and the cap in one move.
- **A shared utility library.** Once playback identifies tracks by URI, the two
  subsystems share no logic at all — only the pool schema. A shared library
  would have been structure without substance.
- **Hard year filtering.** MA reports the *reissue* year: Beyoncé's *B'DAY*
  shows 2026. The model knows it is a 2006 album and judges era correctly when
  asked; a metadata filter would have deleted it.
- **LLM track generation, pool growth, and drift auditing.** All downstream of
  the recall problem in §1. Roughly 800 lines removed.
- **Naming the seed artists in the album and track calls.** Seeds reach only the
  roster stage (§3.1). The idea was to make a playlist's bar concrete at the
  stages where picks are actually made -- Powerhouse Vocal Precision's prompt
  states its bar abstractly ("a woman widely recognized as a great singer,
  celebrated specifically for her range, power, and vocal control") and had a
  sentence naming the seeds as the required caliber, which the old placeholder
  handling silently deleted.
  Measured 2026-09-30, four wordings over six saved batches, 26 model calls: a
  control, "these artists set the standard for the range/power/control a singer
  must demonstrate", "for calibration only, these artists exemplify the
  description -- judge against the description", and the original "every pick
  must belong in their company". **No wording could be distinguished from
  noise.** A duplicated control arm (identical prompt, two runs) differed by 1
  track kept and 1 borderline-artist track; every wording effect sat at or below
  that. On Powerhouse the borderline counts were 9, 10, 10, 9, 7 against a
  control spread of 9-10.
  The premise did not survive either. Sia was kept 6-8 times by *every* arm,
  including the one that spells out range, power and control. The model is not
  failing to enforce the stated bar; it disagrees about which singers clear it,
  which is a matter of taste. For specific artists the mechanism is
  `exclude_artists`, which is exact and hard-enforced -- three names in a field
  beat a calibration sentence measurement cannot detect.
  A methodological caution from the same work: a single A/B pair on one batch
  suggested the "company" wording loosened selection substantially (+7 tracks of
  183). Across four batches it was +4 of 549. One pair of runs against a
  nondeterministic model is not a measurement.
- **Genre as a metadata filter.** `exclude_genres` is advice to the model, not a
  match against Music Assistant's genre tags, for the same reason hard year
  filtering was rejected above: MA's metadata is not trustworthy enough to
  delete tracks on. Its per-track genre tags are free-form and inconsistent —
  Nightwish is not reliably tagged "symphonic metal" — so a tag filter would
  miss the case it was added for while silently dropping correctly-tagged
  material. The prompt clause demonstrably works: naming a genre in the
  description removed Björk's cohort from Prestige Soul & Jazz, taking art-pop
  from 20% of the pool to 13%. The filter approach was tried: a
  `GENRE_DENYLIST_BASELINE`/`ADDITIONS` pair matched against MA's genre tags,
  guarding the similar-artist substitution in `_resolve_track()`. It only ever
  ran on the no-exact-artist-match fallback, so a correctly resolved artist
  never reached it, and it was removed along with LLM track generation. Its
  comments outlived it in `const.py` until 2026-09-28.

---

## 7. Known gaps

- The builder's variant filtering is absent from playback's reconciliation.
  Harmless while pools are the only source, but worth remembering.
- Compilations are eligible (`KEEP_TYPES`), so if one is ever selected it can
  claim an artist's hits and leave the studio albums contributing only deep cuts.
- Track-stage rejection is no longer unobserved: once picks had to carry a
  reason (§3.5), empty lists appeared in every run — 2–5 albums per batch. The
  path works; it was the unjustified prompt that never exercised it.
- Pool size is unbounded by design. If a playlist builds to only a few hours,
  that is a signal the roster or the prompt is too narrow.
