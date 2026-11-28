// LTVPlayer: Settings (port of Sources/SettingsScreen.swift + SettingsView.swift / SoundSettingsView.swift /
// MixesSettings.swift, as far as a TV app can do it). The first page lists one row per page, "Name …… value ›", like
// the tvOS Settings app: Theme, Mixes, Sound, Playback, Lyrics, About. Every page is a row screen of the list stack
// (Back = one page up). A choice opens its own page with ✓ on the current value; OK picks it and goes back.
// Stored here: look, list text size, jump steps, keep music, lyrics style, sound output, lip sync (settings.js). Stored
// on the server: equalizer / Boost / Loudness of this queue, Prefer videos, the mixes.

import { RowScreen, KINDS, header, text } from "./lists.js";
import { rowInner } from "./tiles.js";
import { settings, CHOICES, label } from "../settings.js";
import { codecName } from "../audio.js";
import { getJSON, esc, plural } from "../api.js";

const EQ_LABELS = ["31 Hz", "63 Hz", "125 Hz", "250 Hz", "500 Hz", "1 kHz", "2 kHz", "4 kHz", "8 kHz", "16 kHz"];
const db = (v) => (v > 0 ? `+${v} dB` : v < 0 ? `\u2212${-v} dB` : "0 dB");
const chev = (v) => `<span class="set-v">${esc(v)}</span><span class="set-chev">›</span>`;

/** Row builders: {key, type: "row", focusable, html(), act()} for VList. */
function nav(key, ic, title, value, act) {
  return { key, type: "row", focusable: true, cls: "set", act, html: () => rowInner({ symbol: ic, title, right: chev(value) }) };
}
function toggle(key, ic, title, on, set) {
  return { key, type: "row", focusable: true, cls: "set", act: () => set(!on),
           html: () => rowInner({ symbol: ic, title, right: `<span class="set-sw${on ? " on" : ""}"><i></i></span>` }) };
}
function info(key, title, value) {
  return { key, type: "row", focusable: true, cls: "set info", act: () => {},
           html: () => rowInner({ symbol: "info", title, right: `<span class="set-v">${esc(value)}</span>` }) };
}
function action(key, ic, title, sub, act) {
  return { key, type: "row", focusable: true, cls: "set", act, html: () => rowInner({ symbol: ic, title, sub, right: "" }) };
}

class SettingsPage extends RowScreen {
  constructor(lists, kind, params, title) {
    super(lists, kind, params, title);
    this.el.classList.add("settings");
  }
  get st() { return this.ctx.state() || {}; }
  entries() { return []; }
  mount() { this.refresh(); }
  refresh() { this.list.setEntries(this.entries()); }
  shown() { this.refresh(); }
  setState() { if (this.lists.top() === this && this.usesState) this.refresh(); }
  /** A choice: its own page with the options. */
  choice(key, ic, title, options, cur, set) {
    const name = (options.find(([v]) => v === cur) || [null, String(cur)])[1];
    return nav(key, ic, title, name, () => this.lists.open("set-choice", { title, options, cur, set }));
  }
  /** A choice of settings.js: its options and labels from CHOICES. */
  pref(key, ic, title) {
    return this.choice(`p:${key}`, ic, title, CHOICES[key], settings.get(key), (v) => settings.set(key, v));
  }
  cmd(name, args) {
    if (!this.ctx.sock.open) { this.ctx.notice("Not connected to the server", 3000); return false; }
    this.ctx.sock.cmd(name, args);
    return true;
  }
}

class ChoicePage extends SettingsPage {
  constructor(lists, p) { super(lists, "set-choice", p, p.title); }
  entries() {
    return this.params.options.map(([v, l]) => ({
      key: `c:${v}`, type: "row", focusable: true, cls: "set",
      html: () => rowInner({ symbol: v === this.params.cur ? "check" : "none", title: l, right: "" }),
      act: () => { this.params.set(v); this.ctx.sock.log(`settings: ${this.params.title} -> ${l}`); this.lists.back(); },
    }));
  }
  mount() { this.list.setEntries(this.entries(), { focusKey: `c:${this.params.cur}` }); }
  shown() {}
}

// -------------------------------------------------------------------- the first page
class RootPage extends SettingsPage {
  constructor(lists) { super(lists, "settings", {}, "Settings"); this.usesState = true; }
  entries() {
    const st = this.st, audio = this.ctx.audio;
    const snd = st.sound || {}, presets = st.sound_presets || [];
    const eq = (presets.find(([v]) => v === snd.preset) || [null, "Off"])[1];
    const subs = !settings.get("subsOn") ? "Off" : label("subLang", settings.get("subLang")).replace("Default (as set in the file)", "On");
    const out = [nav("theme", "palette", "Theme", label("look", settings.get("look")), () => this.lists.open("set-theme"))];
    if (st.mixes) out.push(nav("mixes", "shuffle", "Mixes", st.mixes.map((m) => m.name).join(", "), () => this.lists.open("set-mixes")));
    out.push(nav("sound", audio.on ? "headphones" : "speaker", "Sound", audio.on ? `Headphones · ${eq}` : "TV",
                 () => this.lists.open("set-sound")));
    out.push(nav("playback", "playCircle", "Playback", `${settings.get("seekBack")} s / ${settings.get("seekForward")} s`,
                 () => this.lists.open("set-playback")));
    out.push(nav("lyrics", "captions", "Lyrics", subs, () => this.lists.open("set-lyrics")));
    out.push(nav("about", "info", "About", this.ctx.version, () => this.lists.open("set-about")));
    return out;
  }
}

class ThemePage extends SettingsPage {
  constructor(lists) { super(lists, "set-theme", {}, "Theme"); }
  entries() {
    return [
      this.pref("look", "palette", "Look"),
      toggle("effects", "layers", "Theme effects", settings.get("effects"), (v) => settings.set("effects", v)),
      this.pref("textSize", "playlists", "Text size in music & folder lists"),
    ];
  }
}

// -------------------------------------------------------------------- mixes (stored on the server)
async function mixes() { return (await getJSON("/api/mixes")).mixes || []; }
const canon = (src) => (src.type === "playlist" ? { type: "playlist", id: src.id }
  : { type: "folder", root: src.root, folder: src.folder || "", recursive: src.recursive !== false });

async function putMix(n, body) {
  const r = await fetch(`/api/mixes/${n}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`mix ${n}: HTTP ${r.status} ${await r.text()}`);
  return (await r.json()).mixes || [];
}

class MixesPage extends SettingsPage {
  constructor(lists) { super(lists, "set-mixes", {}, "Mixes"); this.data = null; }
  async refresh() {
    try { this.data = await mixes(); } catch (e) { this.error(e); return; }
    this.list.setEntries(this.data.map((m) => nav(`m:${m.number}`, "shuffle", m.name,
      m.sources.length ? `${plural(m.sources.length, "source")} · ${plural(m.count, "item")}` : "Not set",
      () => this.lists.open("set-mix", { n: m.number, name: m.name }))));
  }
}

class MixPage extends SettingsPage {
  constructor(lists, p) { super(lists, "set-mix", p, p.name); this.mix = null; }
  async refresh() {
    try { this.mix = (await mixes()).find((m) => m.number === this.params.n); } catch (e) { this.error(e); return; }
    const m = this.mix;
    if (!m) { this.list.setEntries([text("gone", "This mix does not exist.")]); return; }
    this.title(m.name);
    this.countLine(plural(m.count, "item"));
    const e = [action("name", "pencil", "Name", m.name, () => this.rename())];
    e.push(header("src", "A click removes"));
    if (!m.sources.length) e.push(text("none", "Nothing added yet."));
    m.sources.forEach((src, i) => e.push({
      key: `s:${i}`, type: "row", focusable: true, cls: "set",
      html: () => rowInner({ symbol: src.type === "playlist" ? "playlists" : "folder", title: src.name,
                             sub: src.type === "playlist" ? plural(src.count, "item")
                               : `${src.root_name || src.root} · ${plural(src.count, "item")}${src.recursive === false ? " · without subfolders" : ""}`,
                             right: '<span class="set-chev">✕</span>' }),
      act: () => this.save(m.sources.filter((_, j) => j !== i).map(canon), `removed ${src.name}`),
    }));
    e.push(header("add", "Add"));
    e.push(action("addf", "plus", "Add folder", "", () => this.lists.open("set-mix-folder", { n: m.number, root: null, folder: "", name: "Add a folder", pops: 1 })));
    e.push(action("addp", "plus", "Add playlist", "", () => this.lists.open("set-mix-playlist", { n: m.number })));
    this.list.setEntries(e);
  }
  async save(sources, what) {
    try {
      await putMix(this.params.n, { sources });
      this.ctx.sock.log(`settings: mix ${this.params.n} ${what}`);
    } catch (err) { this.ctx.notice(err.message, 4000); }
    this.refresh();
  }
  async rename() {
    const name = await this.ctx.prompt("Name of the mix", this.mix.name);
    if (name === null || !name.trim() || name.trim() === this.mix.name) return;
    try { await putMix(this.params.n, { name: name.trim() }); this.ctx.sock.log(`settings: mix ${this.params.n} renamed`); }
    catch (err) { this.ctx.notice(err.message, 4000); }
    this.refresh();
  }
}

/** The library folders, then their subfolders; "Add this folder" puts it into the mix and goes back to the mix page. */
class MixFolderPage extends SettingsPage {
  constructor(lists, p) { super(lists, "set-mix-folder", p, p.name); this.data = null; }
  async mount() {
    const { root, folder } = this.params;
    try {
      this.data = root ? await getJSON(`/api/browse?root=${encodeURIComponent(root)}&folder=${encodeURIComponent(folder)}`)
                       : await getJSON("/api/browse");
    } catch (e) { this.error(e); return; }
    this.refresh();
  }
  refresh() {
    if (!this.data) return;
    const { root, folder, n, pops } = this.params;
    const rec = settings.get("mixSubfolders");
    const e = [toggle("rec", "folder", "Include subfolders", rec, (v) => settings.set("mixSubfolders", v))];
    if (root) {
      e.push(action("add", "plus", `Add this folder (${this.data.total || 0})`, "", async () => {
        try {
          const m = (await mixes()).find((x) => x.number === n);
          await putMix(n, { sources: [...(m ? m.sources.map(canon) : []), { type: "folder", root, folder, recursive: rec }] });
          this.ctx.sock.log(`settings: mix ${n} + folder ${root}/${folder || "-"}${rec ? "" : " only"}`);
        } catch (err) { this.ctx.notice(err.message, 4000); return; }
        for (let i = 0; i < pops; i += 1) this.lists.back();
      }));
      const subs = this.data.subfolders || [];
      if (subs.length) e.push(header("subs", "Folders"));
      for (const s of subs) {
        e.push(nav(`f:${s.folder}`, "folder", s.name, String(s.count),
          () => this.lists.open("set-mix-folder", { n, root, folder: s.folder, name: s.name, pops: pops + 1 })));
      }
    } else {
      for (const r of this.data.roots || []) {
        e.push(nav(`r:${r.root}`, "drive", r.name, String(r.count),
          () => this.lists.open("set-mix-folder", { n, root: r.root, folder: "", name: r.name, pops: pops + 1 })));
      }
    }
    this.list.setEntries(e);
  }
}

class MixPlaylistPage extends SettingsPage {
  constructor(lists, p) { super(lists, "set-mix-playlist", p, "Add a playlist"); }
  async refresh() {
    let m, pls;
    try {
      m = (await mixes()).find((x) => x.number === this.params.n);
      pls = (await getJSON("/api/playlists")).playlists || [];
    } catch (e) { this.error(e); return; }
    const have = new Set((m ? m.sources : []).filter((s) => s.type === "playlist").map((s) => s.id));
    const left = pls.filter((p) => !have.has(p.id));
    this.list.setEntries(left.length ? left.map((p) => action(`p:${p.id}`, "playlists", p.name, plural(p.count, "item"), async () => {
      try { await putMix(this.params.n, { sources: [...m.sources.map(canon), { type: "playlist", id: p.id }] }); }
      catch (err) { this.ctx.notice(err.message, 4000); return; }
      this.ctx.sock.log(`settings: mix ${this.params.n} + playlist ${p.id}`);
      this.lists.back();
    })) : [text("none", "No other playlists.")]);
  }
  shown() {}
}

// -------------------------------------------------------------------- sound
class SoundPage extends SettingsPage {
  constructor(lists) { super(lists, "set-sound", {}, "Sound"); this.usesState = true; }
  entries() {
    const audio = this.ctx.audio, st = this.st, k = audio.sink;
    const outs = [[false, "TV"], [true, k && k.connected ? "Headphones (atv-audio)" : "Headphones (atv-audio, not connected)"]];
    const e = [this.choice("out", audio.on ? "headphones" : "tv", "Sound output", outs, audio.on, (v) => {
      if (v && !(k && k.connected)) { this.ctx.notice("atv-audio is not connected", 3000); return; }
      audio.setOn(v, "settings");
    })];
    e.push(text("status", audio.on ? `${audio.statusText()}${audio.connecting ? " · connecting…" : ""}` : "The TV plays the sound itself (its own volume keys)."));
    if (!audio.on) return e;
    e.push(this.choice("lip", "wave", "Lip sync", CHOICES.lipSync, settings.get("lipSync"), (v) => audio.setLipSync(v)));
    const codecs = (k && k.codecs) || [];
    const o = k && k.output;
    if (o && o.kind === "bluetooth" && o.ready && codecs.length > 1) {
      e.push(this.choice("codec", "headphones", "Codec", codecs.map((c) => [c.codec, c.label || codecName(c.codec)]), o.codec,
        (v) => this.ctx.switchCodec(v)));
    }
    const snd = st.sound || {}, presets = st.sound_presets || [];
    e.push(this.choice("eq", "sliders", "Equalizer", presets, snd.preset, (v) => this.cmd("sound", { preset: v })));
    e.push(nav("eqedit", "sliders", "Edit custom equalizer", snd.preset === "custom" ? "In use" : "", () => this.lists.open("set-eq")));
    e.push(this.choice("boost", "boost", "Boost", Array.from({ length: 9 }, (_, i) => [i, `+${i} dB`]), snd.boost || 0,
      (v) => this.cmd("sound", { boost: v })));
    const loud = !!snd.loudness && snd.loudness !== "off";
    e.push(toggle("loud", "wave", "Loudness", loud, (v) => this.cmd("sound", { loudness: v ? "raise" : "off" })));
    if (st.sound_label) e.push(text("label", `Now: ${st.sound_label}`));
    return e;
  }
}

/** Custom equalizer: ◀ ▶ change the focused band by 1 dB (-12..+12); Load preset fills the bands from a preset. */
class EqPage extends SettingsPage {
  constructor(lists) {
    super(lists, "set-eq", {}, "Custom equalizer");
    const c = (this.st.sound || {}).custom || { preamp: 0, bands: Array(10).fill(0) };
    this.cur = { preamp: c.preamp, bands: c.bands.slice() };
    this.timer = null;
    this.last = 0;
  }
  entries() {
    const st = this.st;
    const curves = st.sound_curves || {};
    const presets = (st.sound_presets || []).filter(([v]) => v !== "off" && v !== "custom" && curves[v]);
    const band = (key, title, get, setv) => ({
      key, type: "row", focusable: true, cls: "set eq",
      html: () => { const v = get(); return rowInner({ symbol: "none", title, right: `<span class="eq-bar"><i style="transform:translateX(${v * 10}px)"></i></span>` +
        `<span class="set-v eq-v">‹ ${esc(db(v))} ›</span>` }); },
      step: (d) => { const v = Math.max(-12, Math.min(12, get() + d)); if (v === get()) return; setv(v); this.push(); this.list.repaint(); },
      act: () => {},
    });
    const e = [this.choice("load", "sliders", "Load preset", presets, null, (v) => {
      const [pre, bands] = curves[v];
      this.cur = { preamp: Math.max(-12, Math.min(12, Math.round(pre))), bands: bands.map((b) => Math.max(-12, Math.min(12, Math.round(b)))) };
      this.push();
    }), action("flat", "sliders", "Flat", "", () => { this.cur = { preamp: 0, bands: Array(10).fill(0) }; this.push(); this.refresh(); })];
    e.push(header("bands", "◀ ▶ change a band by 1 dB"));
    e.push(band("pre", "Preamp", () => this.cur.preamp, (v) => { this.cur.preamp = v; }));
    EQ_LABELS.forEach((l, i) => e.push(band(`b${i}`, l, () => this.cur.bands[i], (v) => { this.cur.bands[i] = v; })));
    return e;
  }
  mount() {
    this.refresh();
    if ((this.st.sound || {}).preset !== "custom") this.push();   // opening the editor uses the custom curve, as on the web remote
  }
  key(k) {
    if (k === 37 || k === 39) { this.list.focused()?.step?.(k === 39 ? 1 : -1); return true; }
    return super.key(k);
  }
  push() {
    clearTimeout(this.timer);
    const go = () => { this.last = performance.now(); this.cmd("sound", { preset: "custom", custom: { preamp: this.cur.preamp, bands: this.cur.bands.slice() } }); };
    const wait = 150 - (performance.now() - this.last);
    if (wait <= 0) go(); else this.timer = setTimeout(go, wait);
  }
  shown() { this.refresh(); }
}

// -------------------------------------------------------------------- playback, lyrics, about
class PlaybackPage extends SettingsPage {
  constructor(lists) { super(lists, "set-playback", {}, "Playback"); this.usesState = true; }
  entries() {
    return [
      toggle("keep", "note", "Keep music playing after leaving the app", settings.get("keepMusic"), (v) => settings.set("keepMusic", v)),
      this.pref("seekBack", "chevronLeft", "Left button jumps back"),
      this.pref("seekForward", "chevronRight", "Right button jumps forward"),
      toggle("prefer", "videos", "Prefer videos", !!this.st.prefer_videos, (v) => this.cmd("prefer_videos", { on: v })),
    ];
  }
}

class LyricsPage extends SettingsPage {
  constructor(lists) { super(lists, "set-lyrics", {}, "Lyrics"); }
  entries() {
    return [
      toggle("on", "captions", "Show lyrics / subtitles", settings.get("subsOn"), (v) => settings.set("subsOn", v)),
      this.pref("subLang", "captions", "Language"),
      this.pref("subLang2", "captions", "Second language (on top)"),
      toggle("screen", "lyrics", "Lyrics screen (Blue in the player)", settings.get("lyricsScreen"), (v) => settings.set("lyricsScreen", v)),
      header("style", "Style"),
      this.pref("subSize", "captions", "Text size"),
      this.pref("subFont", "captions", "Font"),
      this.pref("subColor", "palette", "Text colour"),
      toggle("bold", "captions", "Bold text", settings.get("subBold"), (v) => settings.set("subBold", v)),
      this.pref("subOutline", "captions", "Text outline"),
      this.pref("subBox", "captions", "Background box"),
      this.pref("subMargin", "captions", "Position on screen"),
    ];
  }
}

class AboutPage extends SettingsPage {
  constructor(lists) { super(lists, "set-about", {}, "About"); this.usesState = true; }
  entries() {
    const st = this.st, sock = this.ctx.sock;
    return [
      header("app", "LTVPlayer"),
      info("version", "Version", this.ctx.version),
      info("plays", "Plays", "Songs and music videos from your own server, original files"),
      header("server", "Server"),
      info("address", "Address", location.host),
      info("status", "Status", sock.open ? `Connected${st.version ? ` · server ${st.version}` : ""}` : "Not connected"),
      info("remote", "Web remote", `${location.host} on a phone or PC`),
      header("keys", "Remote in the player"),
      info("k-ok", "OK", "Play / pause"),
      info("k-hold", "Hold OK", "Quick menu: volume, ♥ love, shuffle, queue, lyrics, audio output, sound, version"),
      info("k-ud", "▲  ▼", "Previous / next"),
      info("k-lr", "◀  ▶", "Jump back / forward (Settings → Playback)"),
      info("k-blue", "Blue", "Lyrics screen on / off"),
      info("k-green", "Green", "Play / pause, also in the menus"),
      info("k-red", "Red", "Stop, also in the menus (Play starts the item again)"),
      info("k-ch", "CH+  CH−", "Next / previous"),
      info("k-back", "Back", "The menu (playback goes on); twice on Home closes the app"),
    ];
  }
}

Object.assign(KINDS, {
  settings: (l) => new RootPage(l), "set-choice": (l, p) => new ChoicePage(l, p), "set-theme": (l) => new ThemePage(l),
  "set-mixes": (l) => new MixesPage(l), "set-mix": (l, p) => new MixPage(l, p), "set-mix-folder": (l, p) => new MixFolderPage(l, p),
  "set-mix-playlist": (l, p) => new MixPlaylistPage(l, p), "set-sound": (l) => new SoundPage(l), "set-eq": (l) => new EqPage(l),
  "set-playback": (l) => new PlaybackPage(l), "set-lyrics": (l) => new LyricsPage(l), "set-about": (l) => new AboutPage(l),
});
