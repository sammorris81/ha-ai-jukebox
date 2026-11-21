/*
 * AI Jukebox card -- bundled with the ai_jukebox integration, which serves
 * this file and loads it on every dashboard (see _async_register_card).
 *
 *   type: custom:ai-jukebox-card
 *   title: AI Jukebox        # optional
 *
 * Plain custom element, no dependencies: it must work on an install that has
 * none of the community cards the author's own dashboard was built from.
 *
 * The speaker picker offers only the integration's managed speakers (its
 * Configure page), fetched over the ai_jukebox/settings websocket command, so
 * the card and auto-attach agree on which speakers the jukebox drives.
 */

const PREFIX = "media_player.ai_jukebox_";
const NAME_PREFIX = "AI Jukebox ";
const TEST_TAG = "test";
const BUSY = ["enqueuing", "refilling", "speaker_join", "generating"];
const SETTINGS_TTL_MS = 60_000;
const STORE_KEY = "ai-jukebox-card";

const ICONS = {
  play: "M8,5.14V19.14L19,12.14L8,5.14Z",
  stop: "M18,18H6V6H18V18Z",
  build: "M17.65,6.35C16.2,4.9 14.21,4 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20C15.73,20 18.84,17.45 19.73,14H17.65C16.83,16.33 14.61,18 12,18A6,6 0 0,1 6,12A6,6 0 0,1 12,6C13.66,6 15.14,6.69 16.22,7.78L13,11H20V4L17.65,6.35Z",
  dice:
    "M19,5V19H5V5H19M19,3H5A2,2 0 0,0 3,5V19A2,2 0 0,0 5,21H19A2,2 0 0,0 21,19V5A2,2 0 0,0 19,3M7.5,6A1.5,1.5 0 0,0 6,7.5A1.5,1.5 0 0,0 7.5,9A1.5,1.5 0 0,0 9,7.5A1.5,1.5 0 0,0 7.5,6M16.5,15A1.5,1.5 0 0,0 15,16.5A1.5,1.5 0 0,0 16.5,18A1.5,1.5 0 0,0 18,16.5A1.5,1.5 0 0,0 16.5,15M16.5,6A1.5,1.5 0 0,0 15,7.5A1.5,1.5 0 0,0 16.5,9A1.5,1.5 0 0,0 18,7.5A1.5,1.5 0 0,0 16.5,6M7.5,15A1.5,1.5 0 0,0 6,16.5A1.5,1.5 0 0,0 7.5,18A1.5,1.5 0 0,0 9,16.5A1.5,1.5 0 0,0 7.5,15M12,10.5A1.5,1.5 0 0,0 10.5,12A1.5,1.5 0 0,0 12,13.5A1.5,1.5 0 0,0 13.5,12A1.5,1.5 0 0,0 12,10.5Z",
  music:
    "M15,6H3V8H15V6M15,10H3V12H15V10M3,16H11V14H3V16M17,6V14.18C16.69,14.07 16.35,14 16,14A3,3 0 0,0 13,17A3,3 0 0,0 16,20A3,3 0 0,0 19,17V8H22V6H17Z",
  alert:
    "M13,13H11V7H13M13,17H11V15H13M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2Z",
};

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);

const svg = (path) =>
  `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${path}"></path></svg>`;

const playlistName = (state) => {
  const name = state.attributes.friendly_name || state.entity_id;
  return name.startsWith(NAME_PREFIX) ? name.slice(NAME_PREFIX.length) : name;
};

const loadPrefs = () => {
  try {
    return JSON.parse(localStorage.getItem(STORE_KEY)) || {};
  } catch (err) {
    return {};
  }
};

const savePrefs = (prefs) => {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(prefs));
  } catch (err) {
    /* private mode or blocked storage: the card still works, it just forgets */
  }
};

class AiJukeboxCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._prefs = loadPrefs();
    this._settings = null;
    this._settingsAt = 0;
    this._signature = "";
    this._pending = false;
  }

  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = { title: "AI Jukebox", ...(config || {}) };
    this._signature = "";
    if (this._hass) this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (Date.now() - this._settingsAt > SETTINGS_TTL_MS) this._fetchSettings();
    this._render();
  }

  getCardSize() {
    return 6;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: "auto" };
  }

  async _fetchSettings() {
    this._settingsAt = Date.now();
    try {
      this._settings = await this._hass.callWS({ type: "ai_jukebox/settings" });
    } catch (err) {
      this._settings = { managed_speakers: [], configured: false, error: String(err.message || err) };
    }
    this._signature = "";
    this._render();
  }

  // --- model -------------------------------------------------------------

  _playlists() {
    return Object.values(this._hass.states)
      .filter((s) => s.entity_id.startsWith(PREFIX))
      .filter((s) => s.state !== "unavailable")
      .filter((s) => !(s.attributes.tags || []).includes(TEST_TAG))
      .sort((a, b) => playlistName(a).localeCompare(playlistName(b)));
  }

  _speakers() {
    return (this._settings?.managed_speakers || []).map((id) => {
      const st = this._hass.states[id];
      return {
        id,
        name: st?.attributes.friendly_name || id,
        available: !!st && st.state !== "unavailable",
      };
    });
  }

  _active(playlists) {
    return playlists.filter((s) => ["playing", "paused"].includes(s.state));
  }

  _selectedPlaylist(playlists) {
    const wanted = this._prefs.playlist;
    if (wanted && playlists.some((p) => p.entity_id === wanted)) return wanted;
    const active = this._active(playlists);
    return (active[0] || playlists[0])?.entity_id || "";
  }

  _selectedSpeaker(speakers) {
    const wanted = this._prefs.speaker;
    if (wanted && speakers.some((s) => s.id === wanted && s.available)) return wanted;
    return speakers.find((s) => s.available)?.id || "";
  }

  // --- actions -----------------------------------------------------------

  _toast(message) {
    this.dispatchEvent(new CustomEvent("hass-notification", {
      detail: { message }, bubbles: true, composed: true,
    }));
  }

  async _run(label, fn) {
    if (this._pending) return;
    this._pending = true;
    this._signature = "";
    this._render();
    try {
      await fn();
    } catch (err) {
      this._toast(`${label} failed: ${err.message || err}`);
    } finally {
      this._pending = false;
      this._signature = "";
      this._render();
    }
  }

  async _start(playlist, speaker) {
    if (!playlist || !speaker) {
      this._toast("Pick a playlist and a speaker first");
      return;
    }
    // A speaker still grouped elsewhere would drag its group along with it.
    try {
      await this._hass.callService("media_player", "unjoin", {}, { entity_id: speaker });
    } catch (err) {
      /* not grouped, or doesn't support grouping: nothing to undo */
    }
    await this._hass.callService(
      "ai_jukebox", "start", { group_members: [speaker] }, { entity_id: playlist },
    );
  }

  _onPlay() {
    const playlists = this._playlists();
    const speakers = this._speakers();
    this._run("Play", () =>
      this._start(this._selectedPlaylist(playlists), this._selectedSpeaker(speakers)));
  }

  _onBuild() {
    const playlist = this._selectedPlaylist(this._playlists());
    if (!playlist) return;
    this._run("Build", () =>
      this._hass.callService("ai_jukebox", "rebuild_pool", { force: true }, { entity_id: playlist }));
  }

  _onStop() {
    const playlist = this._selectedPlaylist(this._playlists());
    if (!playlist) return;
    this._run("Stop", () =>
      this._hass.callService("ai_jukebox", "stop", {}, { entity_id: playlist }));
  }

  _onSurprise() {
    const playlists = this._playlists();
    // Only playlists with tracks to play: a playlist with no pool yet, or
    // one whose rebuild has just emptied it, would only fail to start.
    const playable = playlists.filter((p) => p.attributes.pool_size);
    const current = this._selectedPlaylist(playlists);
    const others = playable.filter((p) => p.entity_id !== current);
    const choices = others.length ? others : playable;
    const pick = choices[Math.floor(Math.random() * choices.length)];
    if (!pick) return;
    this._prefs.playlist = pick.entity_id;
    savePrefs(this._prefs);
    this._run("Surprise", () => this._start(pick.entity_id, this._selectedSpeaker(this._speakers())));
  }

  _onSelect(kind, value) {
    this._prefs[kind] = value;
    savePrefs(this._prefs);
    this._signature = "";
    this._render();
  }

  // --- view --------------------------------------------------------------

  _render() {
    if (!this._hass || !this._config) return;
    const playlists = this._playlists();
    const speakers = this._speakers();
    const selected = this._selectedPlaylist(playlists);
    const speaker = this._selectedSpeaker(speakers);

    // Only redraw when something shown actually changed: a redraw replaces
    // the <select>s, which would close one that is open.
    const signature = JSON.stringify([
      this._config, this._settings, this._pending, selected, speaker,
      playlists.map((p) => [p.entity_id, p.state, p.attributes.friendly_name,
        p.attributes.activity, p.attributes.queue_length, p.attributes.speakers,
        p.attributes.now_playing, p.attributes.up_next, p.attributes.last_error,
        p.attributes.prompt, p.attributes.build_instructions, p.attributes.artists,
        p.attributes.pool_size, p.attributes.pool_building]),
      speakers,
    ]);
    if (signature === this._signature) return;
    this._signature = signature;

    const sel = this._hass.states[selected];
    const active = this._active(playlists);
    const errors = playlists.filter((p) => p.attributes.last_error);
    const disabled = this._pending ? "disabled" : "";

    let speakerControl;
    if (!this._settings) {
      speakerControl = `<div class="hint">Loading speakers…</div>`;
    } else if (!this._settings.configured) {
      speakerControl = `<div class="hint">AI Jukebox isn't set up yet.</div>`;
    } else if (!speakers.length) {
      speakerControl = `<div class="hint">No speakers allowed yet. Choose them in
        Settings → Devices &amp; services → AI Jukebox → Configure.</div>`;
    } else {
      speakerControl = `<label class="field"><span>Speaker</span>
        <select id="speaker" ${disabled}>${speakers.map((s) =>
          `<option value="${esc(s.id)}" ${s.id === speaker ? "selected" : ""}
            ${s.available ? "" : "disabled"}>${esc(s.name)}${s.available ? "" : " (unavailable)"}</option>`).join("")}
        </select></label>`;
    }

    const playlistControl = playlists.length
      ? `<label class="field"><span>Playlist</span>
          <select id="playlist" ${disabled}>${playlists.map((p) =>
            `<option value="${esc(p.entity_id)}" ${p.entity_id === selected ? "selected" : ""}>${esc(playlistName(p))}</option>`).join("")}
          </select></label>`
      : `<div class="hint">No playlists yet. Add one from the AI Jukebox integration page.</div>`;

    const canPlay = !this._pending && selected && speaker;
    // A playlist with no tracks can't play, so the play button builds it
    // instead, and waits while a build that has emptied the pool refills it.
    // It turns back into play as soon as the first batch lands.
    const needsPool = sel && !sel.attributes.pool_size;
    const playButton = !needsPool
      ? `<button id="play" class="primary" title="Play" aria-label="Play" ${canPlay ? "" : "disabled"}>${svg(ICONS.play)}</button>`
      : sel.attributes.pool_building
        ? `<button class="primary pill" title="Building the track pool" disabled>${svg(ICONS.build)}<span>Building…</span></button>`
        : `<button id="build" class="primary pill" title="Build this playlist's track pool" ${this._pending ? "disabled" : ""}>${svg(ICONS.build)}<span>Build</span></button>`;
    const description = sel
      ? (sel.attributes.build_instructions || sel.attributes.prompt || "")
      : "";
    const seeds = sel?.attributes.artists || [];
    const poolNote = sel
      ? (sel.attributes.pool_building
        ? "Pool is building…"
        : (sel.attributes.pool_size ? "" : "No track pool yet. Press Build to make one, which takes a few minutes. Play is available as soon as the first tracks are in."))
      : "";

    this.shadowRoot.innerHTML = `
      <style>${AiJukeboxCard.styles}</style>
      <ha-card><div class="content">
        ${this._config.title ? `<div class="title">${esc(this._config.title)}</div>` : ""}
        <div class="controls">
          <div class="pickers">${speakerControl}${playlistControl}</div>
          <div class="buttons">
            ${playButton}
            <button id="stop" title="Stop" aria-label="Stop" ${!this._pending && sel && ["playing", "paused"].includes(sel.state) ? "" : "disabled"}>${svg(ICONS.stop)}</button>
            <button id="surprise" class="pill" title="Play a random playlist on the chosen speaker" ${canPlay && playlists.length > 1 ? "" : "disabled"}>${svg(ICONS.dice)}<span>Feeling lucky</span></button>
          </div>
        </div>
        ${sel ? `
          <div class="about">
            <div class="name">${esc(playlistName(sel))}</div>
            ${description ? `<div class="desc">${esc(description)}</div>` : ""}
            ${seeds.length ? `<div class="seeds"><span>Seeds</span> ${esc(seeds.join(", "))}</div>` : ""}
            ${poolNote ? `<div class="hint">${esc(poolNote)}</div>` : ""}
          </div>` : ""}
        <div class="sessions">
          ${active.length ? active.map((p) => this._session(p)).join("") : `
            <div class="session idle">
              <div class="icon">${svg(ICONS.music)}</div>
              <div class="body"><div class="name">No active session</div></div>
            </div>`}
        </div>
        ${errors.map((p) => `
          <div class="error" role="alert">
            ${svg(ICONS.alert)}
            <div><strong>${esc(playlistName(p))}</strong> ${esc(p.attributes.last_error)}</div>
          </div>`).join("")}
      </div></ha-card>`;

    const root = this.shadowRoot;
    root.getElementById("speaker")?.addEventListener("change", (e) => this._onSelect("speaker", e.target.value));
    root.getElementById("playlist")?.addEventListener("change", (e) => this._onSelect("playlist", e.target.value));
    root.getElementById("play")?.addEventListener("click", () => this._onPlay());
    root.getElementById("stop")?.addEventListener("click", () => this._onStop());
    root.getElementById("surprise")?.addEventListener("click", () => this._onSurprise());
    root.getElementById("build")?.addEventListener("click", () => this._onBuild());
  }

  _session(p) {
    const a = p.attributes;
    const activity = a.activity || p.state;
    const tone = a.last_error ? "bad" : BUSY.includes(activity) ? "busy" : "ok";
    const np = a.now_playing || {};
    const next = a.up_next || {};
    const speakers = (a.speakers || []).map((id) =>
      this._hass.states[id]?.attributes.friendly_name || id);
    const track = (t, label) => t.title ? `
      <div class="track"><span class="label">${label}</span>
        ${esc(t.artist)} – ${esc(t.title)}
        ${t.why ? `<div class="why">${esc(t.why)}</div>` : ""}
      </div>` : "";
    return `
      <div class="session">
        <div class="icon ${tone}">${svg(ICONS.music)}</div>
        <div class="body">
          <div class="name">${esc(playlistName(p))}</div>
          <div class="meta">${esc(activity)} · ${esc(a.queue_length ?? 0)} queued · ${esc(speakers.join(", ") || "no speakers")}</div>
          ${track(np, "Now")}
          ${track(next, "Next")}
        </div>
      </div>`;
  }
}

AiJukeboxCard.styles = `
  :host { display: block; }
  /* Layout lives on an inner element: ha-card's own styles own its box. */
  .content { padding: 16px; display: flex; flex-direction: column; gap: 14px; }
  .title { font-size: 1.25em; font-weight: 500; color: var(--primary-text-color); }
  .controls { display: flex; gap: 12px; align-items: flex-end; flex-wrap: wrap; }
  .pickers { flex: 1 1 220px; display: flex; flex-direction: column; gap: 10px; min-width: 0; }
  .field { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .field span { font-size: 0.8em; color: var(--secondary-text-color); }
  select {
    width: 100%; min-width: 0; padding: 10px 12px; border-radius: 8px;
    border: 1px solid var(--divider-color);
    background: var(--secondary-background-color, var(--card-background-color));
    color: var(--primary-text-color); font: inherit;
  }
  .buttons { display: flex; gap: 8px; }
  button {
    width: 48px; height: 48px; border-radius: 50%; border: none; cursor: pointer;
    display: grid; place-items: center;
    background: var(--secondary-background-color, rgba(127,127,127,0.15));
    color: var(--primary-text-color);
  }
  button.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  /* The dice alone didn't say what it does, so it carries a label. */
  button.pill { width: auto; border-radius: 24px; padding: 0 16px 0 12px; display: flex; gap: 6px; align-items: center; font: inherit; font-weight: 500; white-space: nowrap; }
  button:disabled { opacity: 0.4; cursor: default; }
  button:not(:disabled):hover { filter: brightness(1.1); }
  button:focus-visible, select:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
  svg { width: 24px; height: 24px; fill: currentColor; flex: none; }
  .about { display: flex; flex-direction: column; gap: 6px; }
  .name { font-weight: 500; color: var(--primary-text-color); }
  .desc { color: var(--secondary-text-color); line-height: 1.4; white-space: pre-line; }
  .seeds { color: var(--secondary-text-color); font-size: 0.9em; }
  .seeds span, .label {
    font-size: 0.75em; text-transform: uppercase; letter-spacing: 0.04em;
    color: var(--secondary-text-color); margin-right: 4px;
  }
  .hint { color: var(--secondary-text-color); font-size: 0.9em; line-height: 1.4; }
  .sessions { display: flex; flex-direction: column; gap: 10px; }
  .session {
    display: flex; gap: 12px; padding: 12px; border-radius: 12px;
    background: var(--secondary-background-color, rgba(127,127,127,0.08));
  }
  .icon {
    width: 40px; height: 40px; border-radius: 50%; display: grid; place-items: center; flex: none;
    color: var(--disabled-text-color, #888); background: rgba(127,127,127,0.15);
  }
  .icon.ok { color: var(--success-color, #43a047); background: rgba(67,160,71,0.15); }
  .icon.busy { color: var(--warning-color, #ffa000); background: rgba(255,160,0,0.15); }
  .icon.bad { color: var(--error-color, #db4437); background: rgba(219,68,55,0.15); }
  .body { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .meta { color: var(--secondary-text-color); font-size: 0.9em; }
  .track { color: var(--primary-text-color); overflow-wrap: anywhere; }
  .why { color: var(--secondary-text-color); font-size: 0.85em; font-style: italic; }
  .error {
    display: flex; gap: 10px; align-items: flex-start; padding: 10px 12px; border-radius: 10px;
    color: var(--error-color, #db4437); background: rgba(219,68,55,0.12); overflow-wrap: anywhere;
  }
  .error div { color: var(--primary-text-color); }
`;

/*
 * AI Jukebox player card -- the now playing view of a running session.
 *
 *   type: custom:ai-jukebox-player-card
 *   entity: media_player.ai_jukebox_x   # optional; omit to follow every active session
 *
 * Artwork, track, progress, transport and group volume for the playlist, then
 * the speakers in the session with their own volume. The speaker button opens
 * a join list: the managed speakers from the Configure page, in that order,
 * plus any speaker already in the session that isn't on it, so it can always
 * be taken back out. Without `entity`, several active sessions are shown one
 * at a time with arrows, dots and swipe to move between them.
 */

const PLAYER_ICONS = {
  prev: "M6,18V6H8V18H6M9.5,12L18,6V18L9.5,12Z",
  next: "M16,18H18V6H16M6,18L14.5,12L6,6V18Z",
  play: ICONS.play,
  pause: "M14,19H18V5H14M6,19H10V5H6V19Z",
  stop: ICONS.stop,
  left: "M15.41,16.58L10.83,12L15.41,7.41L14,6L8,12L14,18L15.41,16.58Z",
  right: "M8.59,16.58L13.17,12L8.59,7.41L10,6L16,12L10,18L8.59,16.58Z",
  speakers:
    "M18,2H6A2,2 0 0,0 4,4V20A2,2 0 0,0 6,22H18A2,2 0 0,0 20,20V4A2,2 0 0,0 18,2M12,4A2,2 0 0,1 14,6A2,2 0 0,1 12,8A2,2 0 0,1 10,6A2,2 0 0,1 12,4M12,20A5,5 0 0,1 7,15A5,5 0 0,1 12,10A5,5 0 0,1 17,15A5,5 0 0,1 12,20M12,12A3,3 0 0,0 9,15A3,3 0 0,0 12,18A3,3 0 0,0 15,15A3,3 0 0,0 12,12Z",
  volume: "M14,3.23V5.29C16.89,6.15 19,8.83 19,12C19,15.17 16.89,17.84 14,18.7V20.77C18,19.86 21,16.28 21,12C21,7.72 18,4.14 14,3.23M16.5,12C16.5,10.23 15.5,8.71 14,7.97V16C15.5,15.29 16.5,13.76 16.5,12M3,9V15H7L12,20V4L7,9H3Z",
  muted: "M12,4L9.91,6.09L12,8.18M4.27,3L3,4.27L7.73,9H3V15H7L12,20V13.27L16.25,17.53C15.58,18.04 14.83,18.46 14,18.7V20.77C15.38,20.45 16.63,19.82 17.68,18.96L19.73,21L21,19.73L12,10.73M19,12C19,12.94 18.8,13.82 18.46,14.64L19.97,16.15C20.62,14.91 21,13.5 21,12C21,7.72 18,4.14 14,3.23V5.29C16.89,6.15 19,8.83 19,12M16.5,12C16.5,10.23 15.5,8.71 14,7.97V10.18L16.45,12.63C16.5,12.43 16.5,12.21 16.5,12Z",
};

const ACTIVITY_LABEL = {
  enqueuing: "Queuing", refilling: "Refilling", speaker_join: "Joining speaker",
  generating: "Generating", error: "Error",
};

const fmtTime = (secs) => {
  if (!Number.isFinite(secs) || secs < 0) return "";
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
};

class AiJukeboxPlayerCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._settings = null;
    this._settingsAt = 0;
    this._signature = "";
    this._pending = new Set();
    this._open = new Set();
    this._current = null; // carousel: entity_id of the session on screen
    // Where a speaker switch was flipped to, held until the call returns, so
    // it doesn't snap back while the speaker takes seconds to join.
    this._want = new Map();
    // Volume just set from a slider, per entity: shown until HA reports it
    // (or a few seconds pass), so the slider doesn't jump back on release.
    this._volHold = new Map();
    this._dragging = false;
    this._ticker = null;
  }

  static getStubConfig(hass) {
    const first = Object.keys(hass?.states || {}).find((id) => id.startsWith(PREFIX));
    return first ? { entity: first } : {};
  }

  setConfig(config) {
    if (config?.entity && !String(config.entity).startsWith(PREFIX)) {
      throw new Error(`entity must be an AI Jukebox playlist (${PREFIX}…)`);
    }
    this._config = { ...(config || {}) };
    this._signature = "";
    if (this._hass) this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (Date.now() - this._settingsAt > SETTINGS_TTL_MS) this._fetchSettings();
    this._render();
  }

  connectedCallback() {
    this._ticker = setInterval(() => this._tick(), 1000);
  }

  disconnectedCallback() {
    clearInterval(this._ticker);
    this._ticker = null;
  }

  getCardSize() {
    return 4;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: "auto" };
  }

  async _fetchSettings() {
    this._settingsAt = Date.now();
    try {
      this._settings = await this._hass.callWS({ type: "ai_jukebox/settings" });
    } catch (err) {
      this._settings = { managed_speakers: [], configured: false };
    }
    this._signature = "";
    this._render();
  }

  // --- model -------------------------------------------------------------

  _sessions() {
    if (this._config.entity) {
      const st = this._hass.states[this._config.entity];
      return st ? [st] : [];
    }
    return Object.values(this._hass.states)
      .filter((s) => s.entity_id.startsWith(PREFIX))
      .filter((s) => !["unavailable", "unknown"].includes(s.state))
      .filter((s) => (s.attributes.speakers || []).length)
      .sort((a, b) => playlistName(a).localeCompare(playlistName(b)));
  }

  /** Which session, other than `self`, holds each speaker. */
  _owners(self) {
    const owners = {};
    for (const s of Object.values(this._hass.states)) {
      if (!s.entity_id.startsWith(PREFIX) || s.entity_id === self) continue;
      for (const sp of s.attributes.speakers || []) owners[sp] = playlistName(s);
    }
    return owners;
  }

  _speakerRows(p) {
    const attached = p.attributes.speakers || [];
    const managed = this._settings?.managed_speakers || [];
    const ids = [...managed, ...attached.filter((id) => !managed.includes(id))];
    const owners = this._owners(p.entity_id);
    const status = p.attributes.speaker_status || {};
    return ids.map((id) => {
      const st = this._hass.states[id];
      return {
        id,
        name: st?.attributes.friendly_name || id,
        available: !!st && st.state !== "unavailable",
        joined: attached.includes(id),
        volume: st?.attributes.volume_level,
        muted: !!st?.attributes.is_volume_muted,
        owner: owners[id],
        failed: status[id] === "join_failed",
      };
    });
  }

  _position(st) {
    const a = st.attributes;
    let pos = Number(a.media_position);
    const dur = Number(a.media_duration);
    if (!Number.isFinite(pos) || !Number.isFinite(dur) || dur <= 0) return null;
    if (st.state === "playing" && a.media_position_updated_at) {
      pos += (Date.now() - new Date(a.media_position_updated_at).getTime()) / 1000;
    }
    return { pos: Math.min(Math.max(pos, 0), dur), dur };
  }

  // --- actions -----------------------------------------------------------

  _toast(message) {
    this.dispatchEvent(new CustomEvent("hass-notification", {
      detail: { message }, bubbles: true, composed: true,
    }));
  }

  async _run(key, label, fn) {
    if (this._pending.has(key)) return;
    this._pending.add(key);
    this._signature = "";
    this._render();
    try {
      await fn();
    } catch (err) {
      this._toast(`${label} failed: ${err.message || err}`);
    } finally {
      this._pending.delete(key);
      this._signature = "";
      this._render();
    }
  }

  _call(domain, service, data, entityId) {
    return this._hass.callService(domain, service, data, { entity_id: entityId });
  }

  _onAction(entity, action, arg) {
    const p = this._hass.states[entity];
    if (!p) return;
    switch (action) {
      case "carousel": {
        const ids = this._sessions().map((x) => x.entity_id);
        const i = ids.indexOf(entity);
        this._current = arg === "prev" || arg === "next"
          ? ids[(i + (arg === "next" ? 1 : -1) + ids.length) % ids.length]
          : arg;
        this._signature = "";
        this._render();
        return;
      }
      case "toggle-speakers":
        if (this._open.has(entity)) this._open.delete(entity);
        else this._open.add(entity);
        this._signature = "";
        this._render();
        return;
      case "play_pause":
        this._run(entity, "Play/pause", () =>
          this._call("media_player", p.state === "playing" ? "media_pause" : "media_play", {}, entity));
        return;
      case "prev":
        this._run(entity, "Previous", () => this._call("media_player", "media_previous_track", {}, entity));
        return;
      case "next":
        this._run(entity, "Next", () => this._call("media_player", "media_next_track", {}, entity));
        return;
      case "stop":
        this._run(entity, "Stop", () => this._call("ai_jukebox", "stop", {}, entity));
        return;
      case "mute":
        this._run(entity, "Mute", () =>
          this._call("media_player", "volume_mute", { is_volume_muted: !p.attributes.is_volume_muted }, entity));
        return;
      case "volume":
        this._volHold.set(entity, { value: arg, until: Date.now() + 5000 });
        this._run(entity, "Volume", () =>
          this._call("media_player", "volume_set", { volume_level: arg / 100 }, entity));
        return;
      case "join":
      case "leave": {
        const key = `${entity}|${arg}`;
        const join = action === "join";
        this._want.set(key, join);
        this._run(key, join ? "Adding speaker" : "Removing speaker", () =>
          this._call("ai_jukebox", join ? "attach_speaker" : "detach_speaker",
            { group_members: [arg] }, entity))
          .finally(() => { this._want.delete(key); this._signature = ""; this._render(); });
        return;
      }
      case "speaker-volume":
        this._volHold.set(arg.id, { value: arg.value, until: Date.now() + 5000 });
        this._run(`${entity}|${arg.id}`, "Volume", () =>
          this._call("media_player", "volume_set", { volume_level: arg.value / 100 }, arg.id));
        return;
      default:
    }
  }

  // --- view --------------------------------------------------------------

  /** Moves the progress bars between state updates, without a redraw. */
  _tick() {
    if (!this._hass) return;
    for (const bar of this.shadowRoot.querySelectorAll(".progress[data-entity]")) {
      const st = this._hass.states[bar.dataset.entity];
      const pr = st && this._position(st);
      if (!pr) continue;
      bar.querySelector(".fill").style.width = `${(pr.pos / pr.dur) * 100}%`;
      const t = bar.parentElement.querySelector(".elapsed");
      if (t) t.textContent = fmtTime(pr.pos);
    }
  }

  _render() {
    if (!this._hass || !this._config) return;
    // A redraw replaces the sliders, which would drop one being dragged.
    if (this._dragging) return;
    const sessions = this._sessions();
    const relevant = new Set(sessions.flatMap((p) => [p.entity_id, ...this._speakerRows(p).map((r) => r.id)]));
    const signature = JSON.stringify([
      this._config, this._settings, [...this._pending], [...this._open],
      sessions.map((p) => [p.entity_id, p.state, p.attributes]),
      [...relevant].map((id) => {
        const st = this._hass.states[id];
        return st ? [id, st.state, st.attributes.friendly_name, st.attributes.volume_level,
          st.attributes.is_volume_muted, st.attributes.media_position_updated_at] : [id];
      }),
    ]);
    if (signature === this._signature) return;
    this._signature = signature;

    let body;
    if (this._config.entity && !sessions.length) {
      body = `<div class="empty">${esc(this._config.entity)} not found</div>`;
    } else if (!sessions.length) {
      body = `<div class="empty">${svg(ICONS.music)}<span>Nothing playing</span></div>`;
    } else {
      // Several sessions: one at a time, staying on the one picked even as
      // others start or stop around it.
      const ids = sessions.map((x) => x.entity_id);
      if (!ids.includes(this._current)) this._current = ids[0];
      const cur = sessions[ids.indexOf(this._current)];
      body = this._panel(cur);
      if (sessions.length > 1) {
        const id = esc(cur.entity_id);
        body = `
          <div class="carousel">
            <button class="icon small" data-entity="${id}" data-action="carousel" data-arg="prev" aria-label="Previous playlist">${svg(PLAYER_ICONS.left)}</button>
            <div class="dots">${sessions.map((x) => `
              <button class="dot ${x.entity_id === cur.entity_id ? "on" : ""}" data-entity="${id}" data-action="carousel"
                data-arg="${esc(x.entity_id)}" aria-label="${esc(playlistName(x))}" title="${esc(playlistName(x))}"></button>`).join("")}
            </div>
            <button class="icon small" data-entity="${id}" data-action="carousel" data-arg="next" aria-label="Next playlist">${svg(PLAYER_ICONS.right)}</button>
          </div>${body}`;
      }
    }

    this.shadowRoot.innerHTML = `
      <style>${AiJukeboxPlayerCard.styles}</style>
      <ha-card>${body}</ha-card>`;

    const root = this.shadowRoot;
    const panel = root.querySelector(".panel");
    if (panel && sessions.length > 1 && !this._config.entity) {
      let x0 = null;
      panel.addEventListener("pointerdown", (e) => {
        x0 = e.target.closest("input, button") ? null : e.clientX;
      });
      panel.addEventListener("pointerup", (e) => {
        if (x0 === null) return;
        const dx = e.clientX - x0;
        x0 = null;
        if (Math.abs(dx) > 50) this._onAction(this._current, "carousel", dx < 0 ? "next" : "prev");
      });
    }
    root.querySelectorAll("[data-action]").forEach((el) => {
      const { entity, action, arg } = el.dataset;
      if (el.tagName === "INPUT" && el.type === "range") {
        el.addEventListener("pointerdown", () => { this._dragging = true; });
        // Keep the percentage beside the slider in step while it's dragged.
        el.addEventListener("input", () => {
          const pct = el.parentElement.querySelector(".pct");
          if (pct) pct.textContent = `${el.value}%`;
        });
        el.addEventListener("change", () => {
          this._dragging = false;
          const value = Number(el.value);
          this._onAction(entity, action, arg ? { id: arg, value } : value);
        });
        el.addEventListener("pointerup", () => { this._dragging = false; });
      } else if (el.tagName === "INPUT" && el.type === "checkbox") {
        el.addEventListener("change", () => this._onAction(entity, el.checked ? "join" : "leave", arg));
      } else {
        el.addEventListener("click", () => this._onAction(entity, action, arg));
      }
    });
  }

  _panel(p) {
    const a = p.attributes;
    const id = esc(p.entity_id);
    const busy = this._pending.has(p.entity_id);
    const dis = busy ? "disabled" : "";
    const pic = a.entity_picture;
    const activity = a.last_error ? "error" : a.activity;
    const badge = ACTIVITY_LABEL[activity]
      ? `<span class="badge ${activity === "error" ? "bad" : ""}">${esc(ACTIVITY_LABEL[activity])}</span>` : "";
    const pr = this._position(p);
    const vol = this._volume(p.entity_id, a.volume_level);
    const rows = this._speakerRows(p);
    const joined = rows.filter((r) => r.joined);
    const open = this._open.has(p.entity_id);
    const why = a.now_playing?.why;

    return `
      <div class="panel">
        <div class="top">
          <div class="art">${pic ? `<img src="${esc(pic)}" alt="">` : svg(ICONS.music)}</div>
          <div class="info">
            <div class="playlist">${esc(playlistName(p))}${badge}</div>
            <div class="title">${esc(a.media_title || (p.state === "idle" ? "Idle" : "—"))}</div>
            <div class="artist">${esc(a.media_artist || "")}</div>
            ${why ? `<div class="why">${esc(why)}</div>` : ""}
          </div>
        </div>
        ${pr ? `
          <div class="time">
            <span class="elapsed">${fmtTime(pr.pos)}</span>
            <div class="progress" data-entity="${id}"><div class="fill" style="width:${(pr.pos / pr.dur) * 100}%"></div></div>
            <span>${fmtTime(pr.dur)}</span>
          </div>` : ""}
        <div class="controls">
          <button class="icon" data-entity="${id}" data-action="stop" title="Stop session" aria-label="Stop session" ${dis}>${svg(PLAYER_ICONS.stop)}</button>
          <button class="icon" data-entity="${id}" data-action="prev" title="Previous" aria-label="Previous" ${dis}>${svg(PLAYER_ICONS.prev)}</button>
          <button class="icon main" data-entity="${id}" data-action="play_pause" aria-label="${p.state === "playing" ? "Pause" : "Play"}" ${dis}>${svg(p.state === "playing" ? PLAYER_ICONS.pause : PLAYER_ICONS.play)}</button>
          <button class="icon" data-entity="${id}" data-action="next" title="Next" aria-label="Next" ${dis}>${svg(PLAYER_ICONS.next)}</button>
          <button class="icon ${open ? "on" : ""}" data-entity="${id}" data-action="toggle-speakers" title="Speakers" aria-label="Speakers" aria-expanded="${open}">${svg(PLAYER_ICONS.speakers)}<span class="count">${joined.length}</span></button>
        </div>
        <div class="volume">
          <button class="icon small" data-entity="${id}" data-action="mute" aria-label="${a.is_volume_muted ? "Unmute" : "Mute"}" ${dis}>${svg(a.is_volume_muted ? PLAYER_ICONS.muted : PLAYER_ICONS.volume)}</button>
          <input type="range" min="0" max="100" step="5" value="${vol}" data-entity="${id}" data-action="volume" aria-label="Volume of every speaker in the session" ${dis}>
          <span class="pct">${vol}%</span>
        </div>
        ${this._activeSpeakers(p, joined)}
        ${open ? this._joinList(p, rows) : ""}
      </div>`;
  }

  /** Volume as a whole percent, preferring one just set over a stale state. */
  _volume(entityId, level) {
    const actual = Math.round((level ?? 0) * 100);
    const hold = this._volHold.get(entityId);
    if (!hold) return actual;
    if (hold.value === actual || Date.now() > hold.until) {
      this._volHold.delete(entityId);
      return actual;
    }
    return hold.value;
  }

  /** The speakers in the session, each with its own volume. */
  _activeSpeakers(p, joined) {
    if (!joined.length) return "";
    const id = esc(p.entity_id);
    return `
      <div class="active">
        ${joined.map((r) => {
          const dis = this._pending.has(`${p.entity_id}|${r.id}`) || !r.available ? "disabled" : "";
          const v = this._volume(r.id, r.volume);
          const note = !r.available ? "unavailable" : r.failed ? "didn't join the group" : "";
          return `
            <div class="aspk">
              <span class="sname">${esc(r.name)}${note ? `<span class="note">${esc(note)}</span>` : ""}</span>
              <input type="range" min="0" max="100" step="5" value="${v}" data-entity="${id}" data-action="speaker-volume"
                data-arg="${esc(r.id)}" aria-label="${esc(r.name)} volume" ${dis}>
              <span class="pct">${v}%</span>
            </div>`;
        }).join("")}
      </div>`;
  }

  /** Every speaker the session can take, with a switch to add or remove it. */
  _joinList(p, rows) {
    const id = esc(p.entity_id);
    if (!this._settings) return `<div class="speakers"><div class="hint">Loading speakers…</div></div>`;
    if (!rows.length) {
      return `<div class="speakers"><div class="hint">No speakers allowed yet. Choose them in
        Settings → Devices &amp; services → AI Jukebox → Configure.</div></div>`;
    }
    return `
      <div class="speakers">
        <div class="ltitle">Group speakers</div>
        ${rows.map((r) => {
          const key = `${p.entity_id}|${r.id}`;
          const on = this._want.has(key) ? this._want.get(key) : r.joined;
          const dis = this._pending.has(key) || !r.available ? "disabled" : "";
          const note = !r.available ? "unavailable" : (!r.joined && r.owner) ? `playing ${r.owner}` : "";
          return `
            <label class="row">
              <input type="checkbox" class="switch" data-entity="${id}" data-action="member" data-arg="${esc(r.id)}" ${on ? "checked" : ""} ${dis}>
              <span class="sname">${esc(r.name)}${note ? `<span class="note">${esc(note)}</span>` : ""}</span>
            </label>`;
        }).join("")}
      </div>`;
  }
}

AiJukeboxPlayerCard.styles = `
  :host { display: block; }
  .panel { padding: 16px; display: flex; flex-direction: column; gap: 12px; }
  .panel { touch-action: pan-y; }
  .top { display: flex; gap: 14px; align-items: center; min-width: 0; }
  .art {
    width: 88px; height: 88px; flex: none; border-radius: 10px; overflow: hidden;
    display: grid; place-items: center; color: var(--secondary-text-color);
    background: var(--secondary-background-color, rgba(127,127,127,0.15));
  }
  .art img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .art svg { width: 40px; height: 40px; }
  .info { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
  .playlist {
    font-size: 0.8em; color: var(--secondary-text-color); text-transform: uppercase;
    letter-spacing: 0.04em; display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
  }
  .badge {
    font-size: 0.85em; font-weight: 600; padding: 1px 8px; border-radius: 10px; letter-spacing: 0.04em;
    color: var(--warning-color, #ffa000); background: rgba(255,160,0,0.15);
  }
  .badge.bad { color: var(--error-color, #db4437); background: rgba(219,68,55,0.15); }
  .title {
    font-size: 1.15em; font-weight: 500; color: var(--primary-text-color);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .artist { color: var(--secondary-text-color); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .why { color: var(--secondary-text-color); font-size: 0.85em; font-style: italic; line-height: 1.35; }
  .time { display: flex; align-items: center; gap: 8px; font-size: 0.8em; color: var(--secondary-text-color); font-variant-numeric: tabular-nums; }
  .progress { flex: 1; height: 4px; border-radius: 2px; background: var(--divider-color); overflow: hidden; }
  .fill { height: 100%; background: var(--primary-color); }
  .controls { display: flex; justify-content: center; align-items: center; gap: 6px; }
  button.icon {
    position: relative; width: 44px; height: 44px; border-radius: 50%; border: none; cursor: pointer;
    display: grid; place-items: center; background: none; color: var(--primary-text-color);
  }
  button.icon.main { width: 56px; height: 56px; background: var(--primary-color); color: var(--text-primary-color, #fff); }
  button.icon.on { color: var(--primary-color); background: rgba(3,169,244,0.12); }
  button.icon.small { width: 36px; height: 36px; }
  button.icon:disabled { opacity: 0.4; cursor: default; }
  button.icon:not(:disabled):hover { background: rgba(127,127,127,0.15); }
  button.icon.main:not(:disabled):hover { background: var(--primary-color); filter: brightness(1.1); }
  button:focus-visible, input:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
  .count {
    position: absolute; top: 2px; right: 0; min-width: 16px; height: 16px; border-radius: 8px;
    font-size: 10px; font-weight: 600; line-height: 16px; text-align: center;
    background: var(--primary-color); color: var(--text-primary-color, #fff);
  }
  svg { width: 24px; height: 24px; fill: currentColor; flex: none; }
  .volume, .svol { display: flex; align-items: center; gap: 8px; }
  input[type=range] { flex: 1; min-width: 0; accent-color: var(--primary-color); }
  .pct { width: 3.2em; text-align: right; font-size: 0.85em; color: var(--secondary-text-color); font-variant-numeric: tabular-nums; }
  .active { display: flex; flex-direction: column; gap: 4px; }
  .aspk { display: grid; grid-template-columns: minmax(0, 11em) 1fr auto; align-items: center; gap: 10px; }
  .aspk .sname { font-size: 0.9em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: block; }
  .aspk .note { display: block; }
  .speakers {
    display: flex; flex-direction: column; padding: 4px 12px 8px; border-radius: 12px;
    background: var(--secondary-background-color, rgba(127,127,127,0.08));
  }
  .ltitle { font-size: 0.8em; color: var(--secondary-text-color); padding: 6px 0 2px; }
  .row { display: flex; align-items: center; gap: 12px; cursor: pointer; min-height: 40px; }
  .row + .row { border-top: 1px solid var(--divider-color); }
  .carousel { display: flex; align-items: center; justify-content: space-between; padding: 8px 8px 0; }
  .dots { display: flex; gap: 6px; flex-wrap: wrap; justify-content: center; }
  .dot {
    width: 8px; height: 8px; padding: 0; border: none; border-radius: 50%; cursor: pointer;
    background: var(--divider-color);
  }
  .dot.on { background: var(--primary-color); }
  .carousel + .panel { padding-top: 4px; }
  .sname { color: var(--primary-text-color); display: flex; flex-direction: column; min-width: 0; }
  .note { font-size: 0.8em; color: var(--secondary-text-color); }
  .switch {
    appearance: none; -webkit-appearance: none; flex: none; margin: 0; cursor: pointer;
    width: 40px; height: 22px; border-radius: 11px; position: relative;
    background: var(--switch-unchecked-track-color, rgba(127,127,127,0.45)); transition: background .15s;
  }
  .switch::after {
    content: ""; position: absolute; top: 3px; left: 3px; width: 16px; height: 16px; border-radius: 50%;
    background: #fff; transition: transform .15s;
  }
  .switch:checked { background: var(--primary-color); }
  .switch:checked::after { transform: translateX(18px); }
  .switch:disabled { opacity: 0.4; cursor: default; }
  .hint { color: var(--secondary-text-color); font-size: 0.9em; line-height: 1.4; padding: 6px 0; }
  .empty { padding: 16px; display: flex; gap: 10px; align-items: center; color: var(--secondary-text-color); }
`;

if (!customElements.get("ai-jukebox-card")) {
  customElements.define("ai-jukebox-card", AiJukeboxCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "ai-jukebox-card",
    name: "AI Jukebox",
    description: "Pick a playlist and a speaker, start, stop or surprise, and see what's playing and why.",
    preview: false,
  });
}

if (!customElements.get("ai-jukebox-player-card")) {
  customElements.define("ai-jukebox-player-card", AiJukeboxPlayerCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "ai-jukebox-player-card",
    name: "AI Jukebox player",
    description: "What's playing on an AI Jukebox session, with transport, volume and the session's speakers.",
    preview: false,
  });
}
