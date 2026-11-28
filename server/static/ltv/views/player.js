// LTVPlayer: the player screen. Video: the picture full screen with the info overlay at the bottom (appears on every key
// and pointer move, hides 5 s later while playing, stays while paused) and the lyrics drawn on the picture (Settings →
// Lyrics; timed tracks only, a second language on top). Song: its cover in the middle. Yellow = the lyrics screen
// (port of TVLyricsScreen / LyricsPanel: title, progress and up next on the left, the lyrics on the right; time-synced
// lines light up and scroll by themselves, untimed text follows the progress, a file without lyrics shows what is known
// about it). The quick menu (views/quick.js) opens over it with Blue (or hold OK).
// 0.8.0 (P6): the Aurora song player (AuroraPlayer: backdrop, cover left, title column, Up next); OLED care (a still
// screen = song, lyrics screen, paused video dims to 35 % after a minute without a key and moves a few px a minute;
// any key or a new item wakes it); one `fps song <look> #id` journal line per song and look.

import { settings } from "../settings.js";
import { chosen, second, textAt, cueAt } from "../lyrics.js";
import { Backdrop } from "../backdrop.js";
import { bars } from "./tiles.js";
import { esc, formatTime } from "../api.js";

const HIDE_AFTER = 5000;
const SUB_SIZE = { 0: 46, 20: 36, 16: 46, 12: 56, 9: 68 };
const SUB_FONT = { "": '"Nunito", sans-serif', serif: 'Georgia, "Times New Roman", serif', sans: 'Arial, Helvetica, sans-serif',
                   mono: '"Courier New", monospace' };
const ANCHOR = 0.38;   // where the current synced line sits in the lyrics panel (LyricsMetrics.tv)
const DIM_AFTER = 60000;     // OLED care: a still player screen dims after a minute without a key ...
const DRIFT_EVERY = 60000;   // ... and its content moves a few px every minute
const DRIFT = [[0, 0], [6, 0], [6, 6], [0, 6], [-6, 6], [-6, 0], [-6, -6], [0, -6], [6, -6]];
const SONG_FPS_LOGS = 8;
const SONG_FPS_AFTER = 4000;   // ms after the song player came up playing (the cover's spring + the fade are over)
const SONG_FPS_FOR = 2000;

function clock(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

export class PlayerView {
  /** opts: {lyrics: LyricsStore, log(text)} */
  constructor(root, engine, opts = {}) {
    this.root = root;
    this.engine = engine;
    this.opts = opts;
    this.state = null;
    this.item = null;
    this.preview = null;   // a seek target from ◀ / ▶ shown on the bar until the element is there
    this.hideTimer = null;
    this.visible = false;
    root.innerHTML = `
      <div class="p-cover"><img alt=""></div>
      <div class="pn">
        <div class="pn-num"></div>
        <img class="pn-art" alt="">
        <div class="pn-bottom">
          <div class="pn-artist"></div><div class="pn-title"></div><div class="pn-facts"></div>
          <div class="pn-row"><div class="pn-clock"><span class="pn-el"></span><span class="pn-dur"></span></div><div class="pn-next"></div></div>
          <div class="pn-bar"><i class="pn-fill"></i></div>
        </div>
      </div>
      <div class="pv">
        <div class="pv-deck"><div class="pv-disc"><img class="pv-label" alt=""><i class="pv-hole"></i></div><div class="pv-shine"></div>
          <div class="pv-arm"><i></i></div></div>
        <div class="pv-text">
          <div class="pv-side"></div>
          <div class="pv-title"></div><div class="pv-artist"></div><div class="pv-album"></div><div class="pv-chips"></div>
          <div class="pv-prog"><div class="pv-line"><i class="pv-fill"></i><b class="pv-needle"></b></div>
            <div class="pv-times"><span class="pv-el"></span><span class="pv-dur"></span></div></div>
          <div class="pv-next"></div>
        </div>
      </div>
      <div class="pa">
        <div class="pa-bd"></div>
        <div class="pa-row">
          <div class="pa-art"><img alt=""></div>
          <div class="pa-col">
            <div class="hero-eyebrow pa-eyebrow">${bars()}<span class="pa-state"></span></div>
            <div class="pa-title"></div><div class="pa-artist"></div><div class="pa-album"></div><div class="pa-chips"></div>
            <div class="pa-prog"><i class="pa-fill"></i></div>
            <div class="pa-times"><span class="pa-el"></span><span class="pa-rem"></span></div>
            <div class="pa-next"></div>
          </div>
        </div>
      </div>
      <div class="p-subs"><div class="p-sub2"></div><div class="p-sub1"></div></div>
      <div class="p-lyr">
        <div class="p-lyr-bd"></div>
        <div class="p-lyr-left">
          <div class="hero-eyebrow">${bars()}<span class="p-lyr-state"></span></div>
          <div class="p-lyr-title"></div><div class="p-lyr-artist"></div><div class="p-lyr-album"></div>
          <div class="p-lyr-badges"></div>
          <div class="p-lyr-prog"><div class="p-lyr-fill"></div></div>
          <div class="p-lyr-times"><span class="p-lyr-el"></span><span class="p-lyr-rem"></span></div>
          <div class="p-lyr-next"></div>
        </div>
        <div class="p-lyr-panel"><div class="p-lyr-col"></div></div>
      </div>
      <div class="p-overlay">
        <div class="p-scrim"></div>
        <div class="p-info">
          <img class="p-thumb" alt="">
          <div class="p-text">
            <div class="p-title"></div>
            <div class="p-artist"></div>
            <div class="p-badges"></div>
          </div>
          <div class="p-state"></div>
        </div>
        <div class="p-bar"><div class="p-fill"></div><div class="p-knob"></div></div>
        <div class="p-times"><span class="p-el"></span><span class="p-next"></span><span class="p-rem"></span></div>
      </div>
      <div class="p-dim"></div>
      <div class="qm"></div>`;
    this.$ = (sel) => root.querySelector(sel);
    for (const img of root.querySelectorAll("img")) {
      img.addEventListener("error", () => { img.style.visibility = "hidden"; });
      img.addEventListener("load", () => { img.style.visibility = ""; });
    }
    this.backdrop = new Backdrop(this.$(".p-lyr-bd"), 1);
    this.backdrop.still(true);
    this.songBackdrop = new Backdrop(this.$(".pa-bd"), 1);   // AuroraPlayer's AuroraBackground (intensity 1)
    this.songBackdrop.still(true);
    this.lyr = { key: "", track: null, index: -2, lit: null, plain: -2 };
    this.subKey = "";
    this.applySubStyle();
    settings.on((k) => {
      if (k.startsWith("sub")) { this.applySubStyle(); this.lyr.key = ""; this.tickLyrics(); }
      if (k === "lyricsScreen") this.applyLyricsScreen();
      if (k === "effects") this.applyLyricsScreen();
      if (k === "look") this.update();
    });
    this.spin = null;   // the Vinyl record's turn (Web Animations: one compositor transform, see spinTick)
    this.spinRate = 0;
    this.spinTimer = null;
    // OLED care (plan 12.4): a still screen (song player, paused video, lyrics screen) dims after DIM_AFTER without a
    // key and moves a few px every DRIFT_EVERY; a key (app.js wake) or a new item brings it back at once.
    this.keyAt = performance.now();
    this.dimAfter = opts.dimAfter || DIM_AFTER;       // ?oled=<ms> in tests (app.js)
    this.driftEvery = opts.driftEvery || DRIFT_EVERY;
    this.dimmed = false;
    this.driftStep = 0;
    this.driftAt = performance.now();
    this.fpsLeft = SONG_FPS_LOGS;   // song player FPS lines per app start (judges the backdrop / the record on the TV)
    this.fpsKey = "";
    this.fpsDone = new Set();
    this.fpsTimer = null;
    setInterval(() => this.oledTick(), 1000);
    setInterval(() => this.tickLyrics(), 250);
    this.applyLyricsScreen();
  }

  log(t) { this.opts.log?.(t); }

  setItem(it) {
    this.item = it;
    const song = it && it.kind === "audio";
    this.root.classList.toggle("song", !!song);
    this.$(".p-title").textContent = it ? it.title || "" : "";
    this.$(".p-artist").textContent = it ? [it.artist, song ? it.album : null].filter(Boolean).join(" · ") : "";
    const chips = ((it && it.badges) || []).map((b) => `<span class="chip">${esc(b)}</span>`).join("");
    this.$(".p-badges").innerHTML = chips;
    this.$(".p-lyr-badges").innerHTML = chips;
    this.$(".p-lyr-title").textContent = it ? it.title || "" : "";
    this.$(".p-lyr-artist").textContent = it && it.artist ? it.artist : "";
    this.$(".p-lyr-album").textContent = it && song ? [it.album, it.year ? String(it.year).slice(0, 4) : null].filter(Boolean).join(" · ") : "";
    const art = it && it.art ? `/art/${it.id}` : "";
    for (const [img, src] of [[this.$(".p-thumb"), art ? `${art}?s=320` : ""], [this.$(".p-cover img"), art],
                              [this.$(".pn-art"), art ? `${art}?s=640` : ""], [this.$(".pv-label"), art ? `${art}?s=320` : ""],
                              [this.$(".pa-art img"), art ? `${art}?s=640` : ""]]) {
      img.style.visibility = src ? "" : "hidden";
      if (src) img.src = src; else img.removeAttribute("src");
    }
    // Noir / Vinyl song screens (PlayerScreens.swift NoirPlayer / VinylPlayer)
    const year = it && it.year ? String(it.year).slice(0, 4) : null;
    this.$(".pn-num").textContent = it && it.track > 0 ? String(it.track).padStart(2, "0") : "";
    this.$(".pn-artist").textContent = it ? it.artist || "" : "";
    this.$(".pn-title").textContent = it ? it.title || "" : "";
    this.$(".pn-facts").textContent = it ? [it.album, year, ...(it.badges || [])].filter(Boolean).join("   /   ") : "";
    this.$(".pv-title").textContent = it ? it.title || "" : "";
    this.$(".pv-artist").textContent = it ? it.artist || "" : "";
    this.$(".pv-album").textContent = it ? [it.album, year].filter(Boolean).join(", ") : "";
    this.$(".pv-chips").innerHTML = ((it && it.badges) || []).slice(0, 2).map((b) => `<span class="chip">${esc(b)}</span>`).join("");
    // Aurora song screen (AuroraPlayer): cover left, title / artist / album line / fact chips / glow progress / Up next
    this.$(".pa-title").textContent = it ? it.title || "" : "";
    this.$(".pa-artist").textContent = it ? it.artist || "" : "";
    this.$(".pa-album").textContent = it ? [it.album, year].filter(Boolean).join(" · ") : "";
    this.$(".pa-chips").innerHTML = chips;
    this.wake("item");
    this.preview = null;
    this.lyr.key = "";
    if (it) this.opts.lyrics?.load(it).then(() => { this.lyr.key = ""; this.tickLyrics(); });
    this.update();
    this.show();
  }

  setState(st) {
    const before = JSON.stringify((this.state?.upcoming || []).slice(0, 3).map((i) => i.id));
    this.state = st;
    const up = st && st.upcoming && st.upcoming[0];
    if (up) this.opts.lyrics?.load(up);   // the next one ahead
    if (JSON.stringify((st?.upcoming || []).slice(0, 3).map((i) => i.id)) !== before) this.renderNext();
    this.update();
  }

  renderNext() {
    const up = (this.state?.upcoming || []).slice(0, 3);
    const upHtml = up.length ? `<div class="hero-nlabel">UP NEXT</div>${up.map((n, i) => `
      <div class="p-lyr-nrow"><span class="p-lyr-num">${i + 1}</span><div class="hero-ntext"><div class="hero-ntitle">${esc(n.title)}</div>
      <div class="hero-nartist">${esc(n.artist || "")}</div></div></div>`).join("")}` : "";
    this.$(".p-lyr-next").innerHTML = upHtml;
    this.$(".pa-next").innerHTML = upHtml;
    const n = up[0];
    this.$(".pn-next").innerHTML = n ? `<div class="pn-nl">NEXT</div><div class="pn-nt">${esc(n.title)}</div>
      <div class="pn-na">${esc(n.artist || "")}</div>` : "";
    this.$(".pv-next").innerHTML = up.length ? `<div class="pv-nl">Up next</div>${up.map((x, i) => `
      <div class="pv-nrow"><span class="pv-num">${i + 1}</span><span class="pv-nt">${esc(x.title)}</span>
      <span class="pv-na">${esc(x.artist || "")}</span></div>`).join("")}` : "";
  }

  update() {
    const e = this.engine;
    const d = e.duration();
    const t = this.preview !== null ? this.preview : e.position();
    const f = d ? Math.min(1, Math.max(0, t / d)) : 0;
    this.$(".p-fill").style.transform = `scaleX(${f})`;
    this.$(".p-knob").style.transform = `translateX(${f * 1760}px)`;
    this.$(".p-lyr-fill").style.transform = `scaleX(${f})`;
    this.$(".p-el").textContent = e.item ? clock(t) : "";
    this.$(".p-rem").textContent = e.item && d ? "-" + clock(d - t) : "";
    this.$(".p-lyr-el").textContent = e.item ? formatTime(t) : "";
    this.$(".p-lyr-rem").textContent = e.item && d ? "-" + formatTime(d - t) : "";
    this.$(".pn-fill").style.transform = `scaleX(${f})`;
    this.$(".pv-fill").style.transform = `scaleX(${f})`;
    this.$(".pv-needle").style.transform = `translateX(${Math.round(f * 760)}px)`;
    this.$(".pn-el").textContent = e.item ? formatTime(t) : "";
    this.$(".pn-dur").textContent = e.item && d ? "/ " + formatTime(d) : "";
    this.$(".pv-el").textContent = e.item ? formatTime(t) : "";
    this.$(".pv-dur").textContent = e.item && d ? formatTime(d) : "";
    this.$(".pa-fill").style.transform = `scaleX(${f})`;
    this.$(".pa-el").textContent = e.item ? formatTime(t) : "";
    this.$(".pa-rem").textContent = e.item && d ? "-" + formatTime(d - t) : "";
    const up = this.state && this.state.upcoming && this.state.upcoming[0];
    this.$(".p-next").textContent = up ? `Next: ${up.title}` : "";
    const paused = !!e.item && !e.isPlaying();
    this.$(".p-state").textContent = !e.item ? "" : e.stopped ? "■" : paused ? "❚❚" : "▶";
    this.$(".p-lyr-state").textContent = e.stopped ? "STOPPED" : paused ? "PAUSED" : "NOW PLAYING";
    this.$(".pa-state").textContent = e.stopped ? "STOPPED" : paused ? "PAUSED" : "NOW PLAYING";
    const track = this.item && this.item.track > 0 ? this.item.track : 0;
    this.$(".pv-side").textContent = track ? `SIDE A  ·  TRACK ${track}` : paused || e.stopped ? "PAUSED" : "NOW SPINNING";
    this.root.classList.toggle("paused", paused);
    this.root.classList.toggle("playing", !!e.item && !paused);
    if (paused) this.show(true);
    else if (this.wasPaused) this.show();   // playing again: the overlay goes HIDE_AFTER later
    this.wasPaused = paused;
    this.backdrop.still(!this.lyricsShown() || !settings.get("effects"));
    // the song screen's backdrop drifts only while the song plays (plan 12.4: nothing moves on a paused screen)
    this.songBackdrop.still(!this.auroraSong() || !settings.get("effects") || paused);
    this.spinTick();
    this.fpsArm();
  }

  /** The Aurora song player is the visible screen (not Noir / Vinyl, not the lyrics screen, not a video). */
  auroraSong() {
    return this.visible && !this.lyricsShown() && !!this.item && this.item.kind === "audio"
      && !["noir", "vinyl"].includes(settings.get("look"));
  }

  // ---------------------------------------------------------------- OLED care (plan 12.4)
  /** A screen that stands still: the song player (any look), the lyrics screen, a paused or stopped video. A playing
   *  video needs nothing (the overlay is gone after HIDE_AFTER and the picture moves). */
  stillScreen() {
    if (!this.visible || !this.item) return null;
    if (this.lyricsShown()) return "lyrics screen";
    if (this.item.kind === "audio") return "song";
    return this.engine.isPlaying() ? null : "paused video";
  }

  /** Any key on the player (app.js) or a new item: full brightness at once, the minute starts again. */
  wake(why = "key") {
    this.keyAt = performance.now();
    if (!this.dimmed) return;
    this.dimmed = false;
    this.root.classList.remove("dim");
    this.log(`oled: awake (${why})`);
  }

  oledTick() {
    const what = this.stillScreen(), now = performance.now();
    if (!what) {
      if (this.dimmed) this.wake(this.visible ? "moving picture" : "left the player");
      if (this.driftStep) { this.driftStep = 0; this.applyDrift(); }
      this.driftAt = now;
      return;
    }
    if (!this.dimmed && now - this.keyAt >= this.dimAfter) {
      this.dimmed = true;
      this.root.classList.add("dim");
      this.log(`oled: dimmed after ${Math.round((now - this.keyAt) / 1000)} s without a key (${what})`);
    }
    if (now - this.driftAt >= this.driftEvery) {
      this.driftAt = now;
      this.driftStep = (this.driftStep + 1) % DRIFT.length;
      this.applyDrift();
    }
  }

  applyDrift() {
    const [x, y] = DRIFT[this.driftStep];
    this.root.style.setProperty("--dx", `${x}px`);
    this.root.style.setProperty("--dy", `${y}px`);
  }

  // ---------------------------------------------------------------- FPS of the song player (once per song and look)
  // The TV's numbers decide whether the Aurora backdrop / the Vinyl record stay (his rule: drop the spin if heavy).
  fpsArm() {
    const look = ["noir", "vinyl"].includes(settings.get("look")) ? settings.get("look") : "aurora";
    const ok = this.fpsLeft > 0 && this.visible && !this.lyricsShown() && this.item?.kind === "audio" && this.engine.isPlaying();
    const key = ok ? `${this.item.id}|${look}|${settings.get("effects")}` : "";
    if (key === this.fpsKey) return;
    clearTimeout(this.fpsTimer);
    this.fpsTimer = null;
    this.fpsKey = key;
    if (!ok || this.fpsDone.has(key)) return;   // one line per song and look
    this.fpsTimer = setTimeout(() => this.fpsMeasure(key, look), SONG_FPS_AFTER);
  }

  fpsMeasure(key, look) {
    this.fpsTimer = null;
    if (key !== this.fpsKey || this.fpsLeft <= 0) return;
    this.fpsLeft--;
    this.fpsDone.add(key);
    const id = this.item.id;
    let frames = 0, gap = 0, last = 0, t0 = 0;
    const tick = (t) => {
      if (key !== this.fpsKey) return;   // the screen changed meanwhile: no line
      if (!t0) { t0 = last = t; requestAnimationFrame(tick); return; }
      frames++;
      gap = Math.max(gap, t - last);
      last = t;
      if (t - t0 < SONG_FPS_FOR) { requestAnimationFrame(tick); return; }
      const moving = look === "vinyl" ? `record ${this.spinRate > 0 ? "spinning" : "still"}`
        : look === "aurora" ? `backdrop ${settings.get("effects") ? "drifting" : "still"}` : "nothing moving";
      this.log(`fps song ${look} #${id}: ${(frames / ((t - t0) / 1000)).toFixed(1)} fps, max frame gap ${Math.round(gap)} ms, ${moving}`);
    };
    requestAnimationFrame(tick);
  }

  // ---------------------------------------------------------------- Vinyl: the record turns
  // Only on the visible song player with Vinyl + Theme effects on, never under a menu (menu frame rate, his rule
  // 2026-11-28). 33⅓ rpm = 1.8 s a turn; spin-up / spin-down like SpinState (speed += (target - speed) * dt * 2.2):
  // the rate is stepped every 100 ms while it changes, then the timer stops.
  spinWanted() {
    return settings.get("look") === "vinyl" && settings.get("effects") && this.visible && !this.lyricsShown()
      && !!this.item && this.item.kind === "audio" && this.engine.isPlaying();
  }

  spinTick() {
    const target = this.spinWanted() ? 1 : 0;
    if (!this.spin) {
      if (!target) return;
      this.spin = this.$(".pv-disc").animate([{ transform: "rotate(0deg)" }, { transform: "rotate(360deg)" }],
                                             { duration: 1800, iterations: Infinity });
      this.spin.pause();
    }
    if (!target && (!settings.get("effects") || !this.visible || settings.get("look") !== "vinyl")) {
      clearTimeout(this.spinTimer); this.spinTimer = null;   // effects off / screen gone: stop at once
      this.spinRate = 0; this.spin.pause();
      return;
    }
    if (this.spinTimer) return;
    const step = () => {
      const want = this.spinWanted() ? 1 : 0;
      this.spinRate += (want - this.spinRate) * 0.22;
      if (Math.abs(want - this.spinRate) < 0.01) this.spinRate = want;
      if (this.spinRate === 0) this.spin.pause();
      else { this.spin.playbackRate = this.spinRate; if (this.spin.playState !== "running") this.spin.play(); }
      this.spinTimer = this.spinRate === want ? null : setTimeout(step, 100);
    };
    if (target !== this.spinRate) step();
  }

  /** Shows the overlay; while playing it hides again after HIDE_AFTER. */
  show(keep = false) {
    this.root.classList.add("ov");
    clearTimeout(this.hideTimer);
    if (!keep) {
      this.hideTimer = setTimeout(() => {
        if (this.engine.isPlaying()) this.root.classList.remove("ov");
      }, HIDE_AFTER);
    }
  }

  shown(on) {
    this.visible = on;
    if (on) this.wake("shown");
    this.lyr.key = "";
    this.tickLyrics();
    this.update();
  }

  // ---------------------------------------------------------------- lyrics on the picture
  applySubStyle() {
    const s = this.$(".p-subs").style;
    s.setProperty("--sub-size", `${SUB_SIZE[settings.get("subSize")] || 46}px`);
    s.setProperty("--sub-font", SUB_FONT[settings.get("subFont")] || SUB_FONT[""]);
    s.setProperty("--sub-color", settings.get("subColor"));
    s.setProperty("--sub-weight", settings.get("subBold") ? "800" : "600");
    s.setProperty("--sub-stroke", `${settings.get("subOutline") * 1.5}px`);
    s.setProperty("--sub-box", `rgba(0, 0, 0, ${(settings.get("subBox") / 255).toFixed(2)})`);
    s.setProperty("--sub-pad", settings.get("subBox") ? "4px 18px" : "0");
    s.setProperty("--sub-margin", `${settings.get("subMargin")}px`);
  }

  // ---------------------------------------------------------------- the lyrics screen
  lyricsShown() { return settings.get("lyricsScreen"); }

  toggleLyricsScreen(why) {
    settings.set("lyricsScreen", !settings.get("lyricsScreen"));
    this.log(`lyrics screen ${settings.get("lyricsScreen") ? "on" : "off"} (${why})`);
  }

  applyLyricsScreen() {
    this.root.classList.toggle("lyr", this.lyricsShown());
    this.lyr.key = "";
    this.tickLyrics();
    this.update();
  }

  /** 4 times a second while the player is up: the lines on the picture, the lyrics screen's current line. */
  tickLyrics() {
    const it = this.item, e = this.engine;
    if (!this.visible || !it) { this.setSubs("", ""); return; }
    const resp = this.opts.lyrics?.get(it.id) || null;
    const t = e.position();
    if (this.lyricsShown()) {
      this.setSubs("", "");
      this.drawPanel(resp, t);
      return;
    }
    if (!settings.get("subsOn") || it.kind !== "video" || !resp) { this.setSubs("", ""); return; }
    const track = chosen(resp, settings.get("subLang"));
    const two = second(resp, settings.get("subLang2"), track);
    this.setSubs(textAt(track, t), two ? textAt(two, t) : "");
  }

  setSubs(a, b) {
    const key = `${a}\u0000${b}`;
    if (key === this.subKey) return;
    this.subKey = key;
    this.$(".p-sub1").textContent = a;
    this.$(".p-sub2").textContent = b;
    this.$(".p-sub1").classList.toggle("on", !!a);
    this.$(".p-sub2").classList.toggle("on", !!b);
  }

  drawPanel(resp, t) {
    const it = this.item, col = this.$(".p-lyr-col");
    const lang = settings.get("subLang");
    const key = `${it.id}|${lang}|${resp ? "y" : "n"}`;
    if (key !== this.lyr.key) {
      this.lyr = { key, track: null, index: -2, lit: null, plain: -2 };
      col.style.transition = "none";
      col.style.transform = "translateY(0)";
      const track = resp ? chosen(resp, lang) : null;
      this.lyr.track = track;
      if (!resp && it.lyr !== 0) {
        col.innerHTML = "";   // still loading: stay on the lyrics screen
      } else if (track && track.synced) {
        col.className = "p-lyr-col synced";
        col.innerHTML = track.cues.map((c) => `<div class="p-lyr-line">${esc(c.t)}</div>`).join("");
      } else if (track) {
        col.className = "p-lyr-col plain";
        col.innerHTML = `<div class="p-lyr-cap">LYRICS · NOT SYNCED</div>` +
          track.cues.map((c) => (c.t ? `<div class="p-lyr-pline">${esc(c.t)}</div>` : `<div class="p-lyr-gap"></div>`)).join("");
      } else {
        col.className = "p-lyr-col info";
        col.innerHTML = `<div class="p-lyr-cap">NO LYRICS IN THIS FILE</div>` + infoRows(it).map(([k, v]) =>
          `<div class="p-lyr-irow"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("");
      }
      void col.offsetHeight;
      col.style.transition = "";
      this.log(`lyrics screen #${it.id}: ${!resp ? "loading" : !track ? "no lyrics" : track.synced ? `synced ${track.lang || "-"} ${track.cues.length} lines` : `plain ${track.cues.length} lines`}`);
    }
    const track = this.lyr.track;
    if (!track) return;
    const H = 880;
    if (track.synced) {
      const { index, lit } = cueAt(track, t);
      if (index === this.lyr.index && lit === this.lyr.lit) return;
      const lines = col.children;
      if (this.lyr.index >= 0 && lines[this.lyr.index]) lines[this.lyr.index].classList.remove("on");
      this.lyr.index = index;
      this.lyr.lit = lit;
      const el = lines[Math.max(0, index)];
      if (index >= 0 && lit && el) el.classList.add("on");
      if (el) col.style.transform = `translateY(${Math.round(H * ANCHOR - el.offsetTop - el.offsetHeight / 2)}px)`;
    } else {
      const d = this.engine.duration();
      const n = track.cues.length;
      const target = d > 1 ? Math.min(n - 1, Math.floor(Math.min(1, Math.max(0, t / d)) * n)) : 0;
      if (target === this.lyr.plain) return;
      this.lyr.plain = target;
      const rows = col.querySelectorAll(".p-lyr-pline, .p-lyr-gap");
      const el = target <= 0 ? null : rows[target];
      col.style.transform = `translateY(${el ? Math.round(H * 0.12 - el.offsetTop) : 0}px)`;
    }
  }
}

/** SongInfoCard: what the app knows about the item. */
function infoRows(it) {
  const rows = [["Title", it.title]];
  if (it.artist) rows.push(["Artist", it.artist]);
  if (it.album) rows.push(["Album", it.album]);
  if (it.year) rows.push(["Year", String(it.year).slice(0, 4)]);
  if (it.track) rows.push(["Track", String(it.track)]);
  const fmt = it.kind === "video" ? ["Music video"] : [];
  if (it.codec) fmt.push(String(it.codec).toUpperCase());
  if (it.lossless) fmt.push("lossless");
  if (fmt.length) rows.push(["Format", fmt.join(" · ")]);
  if (it.duration) rows.push(["Length", formatTime(it.duration)]);
  if (it.folder) rows.push(["Folder", String(it.folder).split("/").pop()]);
  return rows;
}

export { clock };
