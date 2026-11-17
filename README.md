# AI Jukebox

Endless, curated playlists for Home Assistant and Music Assistant. You describe
a playlist in a sentence or two and name a few artists that define it. AI
Jukebox uses a language model to build a pool of a few hundred real tracks
that fit, then plays from that pool on your speakers indefinitely, keeping
repeats rare and the seed artists prominent.

Each playlist shows up as its own media player (`media_player.ai_jukebox_<name>`)
with its own device, so you can start, stop, pause and group it like any other
player.

## How it works

The model only answers questions that can be checked against real data:

1. **Artists.** Given your description and seed artists, it suggests about 55
   more that belong alongside them. Each one is looked up in Music Assistant,
   and names that don't resolve are dropped.
2. **Albums.** For each artist, it's shown their real discography and picks
   the albums that fit.
3. **Tracks.** For each album, it's shown the real track listing and picks
   the tracks that fit.

The result is the playlist's **pool**. Playback samples from the pool locally,
with no model call between tracks. Recently played tracks, artists and albums
are weighted down, and the seed artists are weighted up. Pools are refreshed
overnight, one playlist per hour inside a window you choose.

[`docs/design.md`](docs/design.md) has the full design.

## Requirements

- **Home Assistant 2026.10** or newer.
- **[Music Assistant](https://music-assistant.io/)** with its Home Assistant
  integration set up, and the speakers you want to use exposed through it.
  Either way of running it works, as a Home Assistant app or as a separate
  server: AI Jukebox talks to it through the connection the integration
  already has.
- **Apple Music** as a Music Assistant provider. The builder only keeps tracks
  it can play from Apple Music. Other providers are not supported yet.
- At least one **AI Task** entity (`ai_task.*`), for example from the Google
  Gemini, OpenAI or Ollama integrations. One pool build makes about 30 model
  requests, so a model with a generous daily free tier works well.

## Installation (HACS)

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=sammorris81&repository=ha-ai-jukebox&category=integration)

The button opens this repository in HACS on your own Home Assistant, ready to
download. Then restart Home Assistant and add the integration:

[![Open your Home Assistant instance and start setting up AI Jukebox.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ai_jukebox)

Or by hand:

1. In HACS, open the menu (⋮) and choose **Custom repositories**.
2. Add `https://github.com/sammorris81/ha-ai-jukebox` with the type
   **Integration**.
3. Find **AI Jukebox** in HACS and download it.
4. Restart Home Assistant.
5. Go to **Settings → Devices & services → Add integration** and choose
   **AI Jukebox**.
6. Pick your AI Task entities. They're tried in order, moving on to the next
   when one fails, so put the ones with the largest daily allowance first.

## Adding and editing playlists

On the AI Jukebox integration page, choose **Add playlist**:

| Field | What it's for |
|---|---|
| Name | Names the media player. Set once, because it can't be changed later. |
| Description | What the playlist should sound like, and nothing else. Judgements a list can't hold go here ("a jazz standard sung straight is welcome, a bebop vocal-as-instrument performance is not"). |
| Seed artists | The artists that define the playlist. The most important field. |
| Never include these artists | Hard exclusions. |
| Stay within these genres / Never include these genres | Optional genre guidance for the model. |
| Tags | Free-form labels. A playlist tagged `test` is left out of the nightly rebuild. |
| Leave out live recordings / remixes | On by default. |
| Tracks per refill, Refill threshold | How deep the speaker's queue is kept. |
| Build the track pool now | A new playlist can't play until its pool is built. Takes a few minutes. |

To edit a playlist later, open its device and choose **Configure**. Edits
take effect at the next refill. They don't change the pool until it's rebuilt,
either overnight or by ticking **Build the track pool now**.

The `ai_jukebox.save_playlist` action does the same from automations or
scripts. It only changes the fields you pass.

## Settings

Choose **Configure** on the integration itself:

- **AI Task entities**, in the order they're tried.
- **Speakers that may join automatically.** When one of these is grouped with
  a playing jukebox speaker from outside the jukebox (the Sonos app, or Music
  Assistant directly), it joins the session. Other speakers are ignored.
- **Rebuild pools overnight**, and the window it runs in (02:00 to 05:00 by
  default). The rebuild also resumes builds that were cut off by a restart.

## Dashboard card

The integration ships its own card, which loads automatically. There's
nothing to add under Resources. Add it from the card picker ("AI Jukebox"), or
in YAML:

```yaml
type: custom:ai-jukebox-card
title: AI Jukebox   # optional; set to "" to hide
```

Pick a speaker and a playlist, then play, stop or **surprise** (a random
playlist on the chosen speaker). The card shows the playlist's description and
seed artists, and for each active session its status, what's playing, what's
next, and why each track was picked. Any playlist with an error gets a banner.

The speaker list only offers the speakers allowed on the integration's
**Configure** page, in the order you arrange them there. If that list is
empty, the card points you there.

### Player card

A now playing card for running sessions: artwork, track, why it was picked,
progress, play/pause, next/previous, stop, and a volume slider for the whole
session. Under that are the speakers in the session, each with its own
volume. The speaker button opens a list of speakers with a switch to add or
remove each one: the managed speakers in Configure order, plus any speaker
already in the session that isn't on it.

```yaml
type: custom:ai-jukebox-player-card
entity: media_player.ai_jukebox_my_playlist   # optional
```

Without `entity`, the card follows every playlist that has speakers
attached. With more than one, it shows one at a time: switch with the arrows,
the dots, or a swipe. It says "Nothing playing" when none are active. With
`entity`, it follows that one playlist only.

## Actions

| Action | Does |
|---|---|
| `ai_jukebox.start` | Start a playlist on one or more speakers. |
| `ai_jukebox.stop` | Stop it, or detach some speakers only. |
| `ai_jukebox.attach_speaker` / `detach_speaker` | Add or remove speakers from a running session. `media_player.join` on the playlist entity works too. |
| `ai_jukebox.rebuild_pool` | Rebuild a playlist's pool. Outside the rebuild window, this needs `force: true`. |
| `ai_jukebox.save_playlist` / `delete_playlist` / `list_playlists` | Manage playlists from automations. |
| `ai_jukebox.clear_history` | Forget what a playlist has played, which resets repeat avoidance. |
| `ai_jukebox.reset` | Force a stuck session back to idle. |

Example:

```yaml
action: ai_jukebox.start
target:
  entity_id: media_player.ai_jukebox_sunday_morning
data:
  group_members:
    - media_player.kitchen
    - media_player.living_room
```

## Good to know

- A playlist's media player reports `state` like any other player. The richer
  status (`enqueuing`, `refilling`, `error` and so on) is in its `activity`
  attribute. Pool size, last build time and build summary are attributes too.
- After a Home Assistant restart, a session that was playing picks up where it
  left off, as long as the speakers are still playing its queue.
- Each playlist's pool, queue and play history live under
  `.storage/ai_jukebox/` in your config directory.

## Development

```bash
python3.14 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest
```

## License

MIT
