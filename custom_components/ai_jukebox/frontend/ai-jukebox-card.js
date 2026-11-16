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

  _onStop() {
    const playlist = this._selectedPlaylist(this._playlists());
    if (!playlist) return;
    this._run("Stop", () =>
      this._hass.callService("ai_jukebox", "stop", {}, { entity_id: playlist }));
  }

  _onSurprise() {
    const playlists = this._playlists();
    const current = this._selectedPlaylist(playlists);
    const pool = playlists.filter((p) => p.entity_id !== current);
    const pick = (pool.length ? pool : playlists)[Math.floor(Math.random() * (pool.length || playlists.length))];
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
    const description = sel
      ? (sel.attributes.build_instructions || sel.attributes.prompt || "")
      : "";
    const seeds = sel?.attributes.artists || [];
    const poolNote = sel
      ? (sel.attributes.pool_building
        ? "Pool is building…"
        : (sel.attributes.pool_size ? "" : "No track pool yet. It builds overnight, or tick \"Build the track pool now\" in the playlist's Configure form."))
      : "";

    this.shadowRoot.innerHTML = `
      <style>${AiJukeboxCard.styles}</style>
      <ha-card><div class="content">
        ${this._config.title ? `<div class="title">${esc(this._config.title)}</div>` : ""}
        <div class="controls">
          <div class="pickers">${speakerControl}${playlistControl}</div>
          <div class="buttons">
            <button id="play" class="primary" title="Play" aria-label="Play" ${canPlay ? "" : "disabled"}>${svg(ICONS.play)}</button>
            <button id="stop" title="Stop" aria-label="Stop" ${!this._pending && sel && ["playing", "paused"].includes(sel.state) ? "" : "disabled"}>${svg(ICONS.stop)}</button>
            <button id="surprise" title="Surprise me" aria-label="Surprise me" ${canPlay && playlists.length > 1 ? "" : "disabled"}>${svg(ICONS.dice)}</button>
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
