// LTVPlayer: the quick menu over the player (hold OK; port of Sources/QuickPanel.swift, rows as on the Apple TV): Close
// menu, ♥ Love, Shuffle, Queue ("n of N", opens the queue), Lyrics / subtitles (◀▶ File
// default / English / Croatian / Off), Lyrics screen, Audio output (TV / Headphones), Volume (headphones only), Equalizer (a list under the row),
// Loudness, Boost (◀▶) and Version (a list). The sound rows exist only with the headphones: the TV's own sound cannot be
// changed by an app. Keys: ▲▼ rows, OK = the row's action (opens / confirms a list), ◀▶ = previous / next value, Back
// closes an open list, then the menu; Blue (lyrics screen), Green / Red (play / pause, stop) still work (app.js).

import { icon } from "../icons.js";
import { esc } from "../api.js";
import { settings } from "../settings.js";

const SUBS = [["File default", true, ""], ["English", true, "eng"], ["Croatian", true, "hrv"], ["Off", false, ""]];

export class QuickMenu {
  /** ctx: {engine, sock, audio, state() -> session state, openQueue(), player, log(text)} */
  constructor(root, ctx) {
    this.root = root;
    this.ctx = ctx;
    this.isOpen = false;
    this.sel = 0;
    this.list = null;          // {row id, index}
    this.volEdit = null;       // {percent, muted, at}: shown until the sink's status caught up
    settings.on(() => { if (this.isOpen) this.render(); });
  }

  log(t) { this.ctx.log(`quick menu: ${t}`); }

  open() {
    this.isOpen = true;
    this.sel = 0;
    this.selId = "close";   // the focus follows the row, not its index, when rows come and go
    this.list = null;
    this.root.classList.add("on");
    this.render();
    this.log("opened");
  }

  close() {
    this.isOpen = false;
    this.list = null;
    this.root.classList.remove("on");
  }

  refresh() { if (this.isOpen) this.render(); }

  rows() {
    const { engine, audio, sock } = this.ctx, st = this.ctx.state() || {}, it = engine.item;
    const cur = st.current && it && st.current.id === it.id ? st.current : it;
    const out = [{ id: "close", ic: "close", t: "Close menu", v: "", act: () => this.close() }];
    const hp = audio.on;
    const o = audio.sink && audio.sink.output;
    const loved = !!(cur && cur.loved);
    out.push({ id: "love", ic: loved ? "heart" : "heartOutline", t: "Love", v: loved ? "On" : "Off", act: () => {
      if (!it) return;
      sock.cmd("love", { id: it.id, on: !loved });
      if (cur) cur.loved = !loved;   // shown at once; the server's next state confirms it
      this.log(`love ${it.id} -> ${loved ? "off" : "on"}`);
    } });
    out.push({ id: "shuffle", ic: "shuffle", t: "Shuffle", v: st.shuffle ? "On" : "Off",
               act: () => { sock.cmd("shuffle", { on: !st.shuffle }); this.log(`shuffle -> ${st.shuffle ? "off" : "on"}`); } });
    const n = (st.queue || []).length, i = st.index ?? -1;
    out.push({ id: "queue", ic: "queue", t: "Queue", v: n ? (i >= 0 && i < n ? `${i + 1} of ${n}` : String(n)) : "—",
               act: () => { this.close(); this.log("queue opened"); this.ctx.openQueue(); } });
    const si = !settings.get("subsOn") ? 3 : Math.max(0, SUBS.findIndex((s) => s[1] && s[2] === settings.get("subLang")));
    out.push({ id: "subs", ic: "captions", t: "Lyrics / subtitles", v: SUBS[si][0], steps: [si > 0, si < SUBS.length - 1],
               step: (d) => { const j = si + d; if (j < 0 || j >= SUBS.length) return;
                 settings.set("subsOn", SUBS[j][1]); settings.set("subLang", SUBS[j][2]); this.log(`lyrics -> ${SUBS[j][0]}`); },
               act: () => {} });
    out.push({ id: "lyrscreen", ic: "lyrics", t: "Lyrics screen", v: settings.get("lyricsScreen") ? "On" : "Off",
               act: () => this.ctx.player.toggleLyricsScreen("quick menu") });
    out.push({ id: "output", ic: hp ? "headphones" : "tv", t: "Audio output", v: hp ? `Headphones${o && o.ready ? "" : " (not connected)"}` : "TV",
               act: () => { if (!hp && !(audio.sink && audio.sink.connected)) { this.ctx.notice("atv-audio is not connected", 3000); this.log("audio output -> headphones refused: not connected"); return; }
                 audio.setOn(!hp, "quick menu"); } });
    if (hp) {   // Volume below Audio output (0.7.1, his wish): switching the output no longer moves the rows above it
      const vol = this.volume();
      out.push({ id: "volume", ic: vol && vol.muted ? "speakerOff" : "speaker", t: "Volume", v: vol ? `${vol.percent} %` : "—",
                 bar: vol ? vol.percent / 100 : null, muted: vol && vol.muted,
                 step: (d) => { if (!vol) return; const p = d > 0 ? Math.min(100, (Math.floor(vol.percent / 5) + 1) * 5)
                   : Math.max(0, (Math.ceil(vol.percent / 5) - 1) * 5); this.setVolume(p, vol.muted); },
                 act: () => { if (vol) this.setVolume(vol.percent, !vol.muted); } });
    }
    if (hp) {
      const snd = st.sound || {}, presets = st.sound_presets || [];
      const pi = Math.max(0, presets.findIndex(([v]) => v === snd.preset));
      out.push({ id: "eq", ic: "sliders", t: "Equalizer", v: (presets[pi] || [])[1] || "Off", opens: true,
                 list: presets.map(([v, l]) => [l, v === snd.preset]),
                 pick: (j) => { const v = presets[j][0]; if (v === snd.preset) return; sock.cmd("sound", { preset: v }); this.log(`equalizer -> ${v}`); },
                 act: () => { this.list = { id: "eq", index: pi }; } });
      const loud = snd.loudness && snd.loudness !== "off";
      out.push({ id: "loud", ic: "wave", t: "Loudness", v: loud ? "On" : "Off",
                 act: () => { sock.cmd("sound", { loudness: loud ? "off" : "raise" }); this.log(`loudness -> ${loud ? "off" : "on"}`); } });
      const b = snd.boost || 0;
      out.push({ id: "boost", ic: "boost", t: "Boost", v: `+${b} dB`, bar: b / 8, steps: [b > 0, b < 8],
                 step: (d) => { const nb = Math.max(0, Math.min(8, b + d)); if (nb === b) return; sock.cmd("sound", { boost: nb }); this.log(`boost -> +${nb} dB`); },
                 act: () => {} });
    }
    const versions = (it && it.versions) || [];
    const vi = Math.max(0, versions.findIndex((x) => x.id === (it && it.id)));
    out.push({ id: "version", ic: "layers", t: "Version", v: versions.length ? (versions[vi] ? versions[vi].label || "—" : "—") : "—",
               opens: versions.length > 1, list: versions.map((x) => [`${x.label || "Version"} · ${x.kind === "video" ? "Video" : "Song"}`, x.id === it.id]),
               pick: (j) => { const x = versions[j]; if (!x || x.id === it.id) return;
                 sock.cmd("switch_version", { id: x.id, position: Math.round(engine.position() * 100) / 100 }); this.log(`version ${it.id} -> ${x.id} (${x.label})`); },
               act: () => { if (versions.length > 1) this.list = { id: "version", index: vi }; } });
    return out;
  }

  volume() {
    const o = this.ctx.audio.usable ? this.ctx.audio.sink.output : null;
    if (!o || o.volume === undefined || o.volume === null) return null;
    if (this.volEdit && performance.now() - this.volEdit.at < 1500) return this.volEdit;
    return { percent: o.volume, muted: !!o.muted };
  }

  setVolume(percent, muted) {
    this.volEdit = { percent, muted, at: performance.now() };
    this.ctx.audio.volume(percent, muted);
    this.log(`volume -> ${percent} %${muted ? " (muted)" : ""}`);
    this.render();
  }

  render() {
    const rows = this.rows();
    const at = rows.findIndex((r) => r.id === this.selId);
    this.sel = at >= 0 ? at : Math.min(this.sel, rows.length - 1);
    this.selId = rows[this.sel] && rows[this.sel].id;
    const listRow = this.list ? rows.find((r) => r.id === this.list.id) : null;
    if (this.list && !listRow) this.list = null;
    this.root.innerHTML = `<div class="qm-panel"><div class="qm-title">QUICK MENU</div>${rows.map((r, i) => {
      const bar = r.bar !== undefined && r.bar !== null ? `<span class="qm-bar${r.muted ? " muted" : ""}"><i style="transform:scaleX(${Math.max(0, Math.min(1, r.bar))})"></i></span>` : "";
      const arrows = r.step ? `<span class="qm-arr${r.steps && !r.steps[0] ? " off" : ""}">‹</span>` : "";
      const arrowsR = r.step ? `<span class="qm-arr${r.steps && !r.steps[1] ? " off" : ""}">›</span>` : r.opens ? `<span class="qm-arr">⌄</span>` : "";
      const row = `<div class="qm-row${i === this.sel && !this.list ? " focused" : ""}${this.list && this.list.id === r.id ? " open" : ""}" data-qm="${r.id}">
        ${icon(r.ic, 34)}<span class="qm-t">${esc(r.t)}</span>${bar}${arrows}<span class="qm-v">${esc(r.v)}</span>${arrowsR}</div>`;
      if (!(this.list && this.list.id === r.id)) return row;
      return row + `<div class="qm-list">${r.list.map(([l, on], j) => `<div class="qm-entry${j === this.list.index ? " focused" : ""}">
        <span>${esc(l)}</span>${on ? icon("check", 28) : ""}</div>`).join("")}</div>`;
    }).join("")}<div class="qm-hint">${this.list ? "▲ ▼ picks  ·  OK confirms  ·  Back closes the list"
      : "OK changes  ·  ◀ ▶ adjust  ·  Back closes  ·  Green pauses"}</div></div>`;
    const le = this.root.querySelector(".qm-entry.focused");
    if (le) le.scrollIntoView({ block: "nearest" });
  }

  /** Keys while the menu is open; true = handled. */
  key(k) {
    const rows = this.rows();
    if (this.list) {
      const r = rows.find((x) => x.id === this.list.id);
      const n = r ? r.list.length : 0;
      if (k === 38) this.list.index = Math.max(0, this.list.index - 1);
      else if (k === 40) this.list.index = Math.min(n - 1, this.list.index + 1);
      else if (k === 13) { r?.pick(this.list.index); this.list = null; }
      else if (k === 461 || k === 8 || k === 27) this.list = null;
      else return k === 37 || k === 39;   // ◀▶ do nothing in a list (Blue goes on to app.js: the lyrics screen)
      this.render();
      return true;
    }
    if (k === 38) { this.sel = Math.max(0, this.sel - 1); this.selId = rows[this.sel].id; }
    else if (k === 40) { this.sel = Math.min(rows.length - 1, this.sel + 1); this.selId = rows[this.sel].id; }
    else if (k === 37 || k === 39) rows[this.sel]?.step?.(k === 39 ? 1 : -1);
    else if (k === 13) rows[this.sel]?.act();
    else if (k === 461 || k === 8 || k === 27) { this.close(); return true; }
    else return false;
    if (this.isOpen) this.render();
    return true;
  }
}
