// LTVPlayer: the player engine (plan section 9). One <video> element for the whole app life; songs play through it too.
// The server owns the queue: this file only plays what `load` / `control` say and reports `status`, `ended`, `error`.
// Diagnostics go to the server journal as `player[ltv]: ...` lines: every load, metadata, first picture, every seek
// (target, where it landed, time until playback runs again), stalls, and one line when an item is over.

const START_LIMIT = 30000;    // no "playing" this long after a load that should play: the item counts as failed
const STALL_AFTER = 5000;     // playing, not seeking, time not moving this long: one log line (log only, no action)
const SEEK_WATCH = 15000;     // a seek that has not run again after this long is logged as such
const MISSED = 3;             // a seek that lands further than this from its target is marked in the log

const now = () => performance.now();
const r1 = (x) => Math.round(x * 10) / 10;

function ranges(tr) {
  const out = [];
  for (let i = 0; i < tr.length; i++) out.push(r1(tr.start(i)) + "-" + r1(tr.end(i)));
  return out.join(",") || "none";
}

export class Engine {
  /** ui: {item(it), present(), notice(text, ms), changed(), stopped()}. Hooks (set by app.js): gate() -> true while a
   *  start must wait (the headphones' ready), onLoad(item, autoplay) before a new item opens, onSeek() after a seek. */
  constructor(video, sock, ui) {
    this.v = video;
    this.sock = sock;
    this.ui = ui;
    this.item = null;
    this.loadId = 0;
    this.wantPlay = false;
    this.started = false;
    this.blocked = false;
    this.stopped = false;   // Stop: the picture is gone, the item stays (Play loads it again from 0:00, server-side)
    this.released = null;   // {at, why}: the video decoder is given back (release), the item stays; restore() reopens it
    this.pausedSince = null;   // performance.now() of the last pause / paused load (app.js: release after 10 min)
    this.gate = null;
    this.onLoad = null;
    this.onSeek = null;
    this.L = null;   // per-load facts for the log
    this.seek = null;
    this.stall = null;
    this.lastT = 0;
    for (const ev of ["loadedmetadata", "playing", "pause", "seeked", "ended", "error", "waiting", "timeupdate"]) {
      video.addEventListener(ev, () => this["on_" + ev]());
    }
    setInterval(() => this.tick(), 1000);
  }

  log(text) { this.sock.log(text); }

  // ---------------------------------------------------------------- state
  duration() {
    const d = this.v.duration;
    return Number.isFinite(d) && d > 0 ? d : (this.item?.duration || 0);
  }

  position() {
    if (this.released) return this.released.at;   // the element is empty: where it will open again
    if (!this.item || this.stopped) return 0;
    if (this.v.readyState < 1) return this.L ? this.L.startAt : 0;   // before metadata: where it will start
    return this.v.currentTime;
  }

  isPlaying() {
    return !!this.item && !this.stopped && this.started && !this.v.paused && !this.v.ended;
  }

  status() {
    if (!this.item) return;
    this.sock.send({ type: "status", item: this.item.id, playing: this.isPlaying(),
                     position: Math.round(this.position() * 100) / 100, duration: r1(this.duration()) });
    this.ui.changed();
  }

  // ---------------------------------------------------------------- server messages
  load(m) {
    const it = m.item;
    if (!it) {
      this.finish("unload");
      this.item = null;
      this.released = null;
      this.loadId += 1;
      this.wantPlay = false;
      this.v.removeAttribute("src");
      this.v.load();
      this.log("load: nothing");
      this.ui.item(null);
      return;
    }
    if (!m.autoplay && this.released && this.item && this.item.id === it.id && !m.restore) {
      // a reconnect resync while the decoder is given back: stay released (the screensaver may be using it)
      this.item = it;
      this.log(`load #${it.id} resync: kept released at ${r1(this.released.at)}`);
      this.status();
      this.ui.item(it);
      return;
    }
    if (!m.autoplay && this.item && this.item.id === it.id && this.v.getAttribute("src")) {
      // a reconnect resync (the Apple TV rule): keep whatever plays now, tell the server
      this.item = it;
      const playing = !this.v.paused && !this.v.ended;
      this.log(`load #${it.id} resync: kept, ${playing ? "playing" : "paused"} at ${r1(this.v.currentTime)}`);
      if (playing) this.sock.cmd("play");   // the server cleared its wish to play while the socket was down
      this.status();
      this.ui.item(it);
      if (m.present) this.ui.present();
      return;
    }
    this.finish("replaced");
    this.loadId += 1;
    this.item = it;
    this.released = null;
    this.pausedSince = m.autoplay ? null : now();
    this.wantPlay = !!m.autoplay;
    this.started = false;
    this.blocked = false;
    this.stopped = false;
    this.seek = null;
    this.stall = null;
    const startAt = Math.max(0, Number(m.position) || 0);
    this.L = { id: it.id, t0: now(), startAt, meta: null, first: null, frame: null, seeks: 0, missed: 0,
               stalls: 0, waiting: 0, done: false, errorSent: false, endedSent: false };
    this.onLoad?.(it, !!m.autoplay);
    this.v.preload = "auto";
    this.v.src = "/media/" + it.id;
    const load = this.loadId;
    if (this.v.requestVideoFrameCallback) {
      this.v.requestVideoFrameCallback(() => {
        if (this.loadId === load && this.L.frame === null) this.L.frame = Math.round(now() - this.L.t0);
      });
    }
    if (this.wantPlay) {
      setTimeout(() => {
        if (this.loadId === load && this.wantPlay && !this.started && !this.blocked && !this.L.errorSent) {
          this.fail(`no playback ${START_LIMIT / 1000} s after the load (readyState ${this.v.readyState}, ` +
                    `networkState ${this.v.networkState}, buffered ${ranges(this.v.buffered)})`);
        }
      }, START_LIMIT);
    }
    this.log(`load #${it.id} '${it.title}' ${it.kind} ${it.codec || "-"}/${it.acodec || "-"} ` +
             `${it.width || 0}x${it.height || 0} at ${r1(startAt)} autoplay=${!!m.autoplay} present=${!!m.present}`);
    this.status();
    this.ui.item(it);
    if (m.present) this.ui.present();
  }

  control(m) {
    if (!this.item) return;
    const a = m.action;
    if (a === "stop") {
      this.stop();
      return;
    }
    if (this.stopped) return;   // nothing loaded: the server answers a Play with a new load
    if (this.released) {   // the decoder was given back: Play / a seek open the item again first
      if (a === "play" || a === "toggle") { this.restore("play", true); return; }
      if (a === "seek") { this.released.at = Math.max(0, Number(m.position) || 0); this.restore("seek"); return; }
      this.status();
      return;
    }
    if (a === "play" || (a === "toggle" && this.v.paused)) {
      this.wantPlay = true;
      this.tryPlay();
    } else if (a === "pause" || a === "toggle") {
      this.wantPlay = false;
      this.v.pause();
    } else if (a === "seek") {
      this.seekTo(Number(m.position) || 0, "server");
    }
    this.status();
  }

  /** Stop (Red / the web remote): the picture goes away, the item stays current; the server's next Play loads it again. */
  stop() {
    if (this.stopped) return;
    const at = this.position();
    if (!this.released) this.finish("stopped");
    this.released = null;
    this.wantPlay = false;
    this.stopped = true;
    this.seek = null;
    this.stall = null;
    this.loadId += 1;
    this.v.pause();
    this.v.removeAttribute("src");
    this.v.load();
    this.log(`stop #${this.item.id} at ${r1(at)}`);
    this.status();
    this.ui.stopped?.();
  }

  /** A pause the app decides itself (headphones gone, sound output changed): the element and the server. */
  localPause(why) {
    if (!this.item || this.stopped || this.released) return;
    this.wantPlay = false;
    this.v.pause();
    this.sock.cmd("pause");
    this.log(`paused (${why})`);
    this.status();
  }

  /** Gives the TV's video decoder back (0.8.2, his choice): the LG screen saver plays its scenes only while no app holds
   *  one, else it shows a clock on black. The element empties; the item and its position stay (position() and the
   *  status report them). Videos only (a song uses no video decoder). restore() opens it again. */
  release(why) {
    if (!this.item || this.item.kind !== "video" || this.stopped || this.released || !this.v.getAttribute("src")) return false;
    const at = this.position();
    this.finish("released");
    this.released = { at, why };
    this.wantPlay = false;
    this.seek = null;
    this.stall = null;
    this.loadId += 1;
    this.v.pause();
    this.v.removeAttribute("src");
    this.v.load();
    this.log(`release #${this.item.id} at ${r1(at)} (${why}): video decoder free`);
    this.status();
    return true;
  }

  /** Opens a released item again at its position, paused (play = true: then plays, no new wait for the headphones). */
  restore(why, play = false) {
    const r = this.released;
    if (!r || !this.item) return false;
    this.log(`restore #${this.item.id} at ${r1(r.at)} (${why})`);
    this.load({ item: this.item, position: r.at, autoplay: false, restore: true });
    if (play) { this.wantPlay = true; this.pausedSince = null; }
    return true;
  }

  // ---------------------------------------------------------------- local actions
  tryPlay() {
    if (this.stopped || !this.v.getAttribute("src")) return;
    if (this.gate && this.gate()) return;   // the headphones' sound is not ready yet (at most 3 s)
    const load = this.loadId;
    let p;
    try { p = this.v.play(); } catch (e) { p = Promise.reject(e); }
    p?.catch?.((e) => {
      if (this.loadId !== load) return;
      const name = e?.name || "Error";
      if (name === "NotAllowedError") {
        this.blocked = true;
        this.log(`play #${this.item.id}: blocked (${name}), waiting for a key`);
        this.ui.notice("Press OK to play", 0);
      } else if (name !== "AbortError") {
        this.fail(`play() rejected: ${name} ${e?.message || ""}`);
      }
    });
  }

  /** The key that answers "Press OK to play" (a user gesture). */
  unblock() {
    if (!this.blocked) return false;
    this.blocked = false;
    this.wantPlay = true;
    this.ui.notice("", 0);
    this.log(`play #${this.item.id}: started by a key`);
    this.tryPlay();
    return true;
  }

  /** Socket down (plan 9.5): OK still plays / pauses the element itself. */
  localToggle() {
    if (!this.item) return;
    if (this.released) { this.restore("play", true); return; }
    if (this.v.paused) { this.wantPlay = true; this.tryPlay(); } else { this.wantPlay = false; this.v.pause(); }
    this.status();
  }

  seekTo(target, why) {
    if (!this.item || this.stopped) return;
    if (this.released) { this.released.at = Math.max(0, target); this.restore(`seek (${why})`); return; }
    const d = this.duration();
    const t = Math.max(0, d ? Math.min(target, d - 0.5) : target);
    if (this.L && this.L.meta === null) {   // before the metadata: the start seek of the load would undo it
      this.L.startAt = t;
      this.status();
      return;
    }
    if (this.seek && this.seek.running === null) this.seekLine("superseded");
    this.seek = { from: this.v.currentTime, target: t, why, at: now(), seeked: null, landed: null, running: null,
                  paused: this.v.paused };
    this.L.seeks += 1;
    this.v.currentTime = t;
    this.onSeek?.();
  }

  // ---------------------------------------------------------------- element events
  on_loadedmetadata() {
    if (!this.L || this.L.meta !== null) return;
    this.L.meta = Math.round(now() - this.L.t0);
    const v = this.v, it = this.item;
    const at = v.audioTracks, vt = v.videoTracks;
    let tracks = `at=${at ? at.length : "-"} vt=${vt ? vt.length : "-"}`;
    const ord = it.audio_ord;
    if (at && ord !== null && ord !== undefined && ord < at.length && !at[ord].enabled) {
      for (let i = 0; i < at.length; i++) at[i].enabled = i === ord;
      tracks += ` audio track ${ord} selected`;
    } else if (ord !== null && ord !== undefined) {
      tracks += ` audio track ${ord} ${at && ord < at.length ? "already on" : "not listed"}`;
    }
    this.log(`meta #${it.id} ${this.L.meta}ms ${v.videoWidth}x${v.videoHeight} dur=${r1(v.duration)}/${r1(it.duration || 0)} ${tracks}`);
    if (this.L.startAt > 0.5) this.seekTo(this.L.startAt, "start");
    if (this.wantPlay) this.tryPlay();
    this.status();
  }

  on_playing() {
    if (!this.L) return;
    this.pausedSince = null;
    if (!this.started) {
      this.started = true;
      this.L.first = Math.round(now() - this.L.t0);
      this.log(`playing #${this.item.id} after ${this.L.first}ms (meta ${this.L.meta}ms)`);
    }
    if (this.blocked) { this.blocked = false; this.ui.notice("", 0); }
    this.status();
  }

  on_pause() { if (this.item && !this.released) this.pausedSince = now(); this.status(); }

  on_waiting() { if (this.L) this.L.waiting += 1; }

  on_seeked() {
    const s = this.seek;
    if (!s || s.seeked !== null) { this.status(); return; }
    s.seeked = Math.round(now() - s.at);
    s.landed = this.v.currentTime;
    if (Math.abs(s.landed - s.target) > MISSED) this.L.missed += 1;
    if (this.v.paused) this.seekLine("paused");
    this.status();
  }

  on_timeupdate() {
    const s = this.seek;
    if (s && s.seeked !== null && s.running === null && !this.v.seeking && !this.v.paused &&
        this.v.currentTime > s.landed + 0.25) {
      s.running = Math.round(now() - s.at);
      this.seekLine("running");
    }
  }

  on_ended() {
    if (!this.L || this.L.endedSent || !this.item) return;
    this.L.endedSent = true;
    this.finish("ended");
    this.sock.send({ type: "ended", item: this.item.id });
    this.status();
  }

  on_error() {
    const e = this.v.error;
    if (!e || !this.item || !this.v.getAttribute("src")) return;
    this.fail(`MEDIA_ERR ${e.code} ${e.message || ""}`.trim());
  }

  fail(text) {
    if (!this.L || this.L.errorSent) return;
    this.L.errorSent = true;
    const it = this.item;
    this.log(`error #${it.id} '${it.title}': ${text}`);
    this.ui.notice(`Cannot play ${it.title} on this TV — skipped`, 5000);
    this.finish("error");
    this.sock.send({ type: "error", item: it.id, message: text.slice(0, 300) });
  }

  // ---------------------------------------------------------------- once a second
  tick() {
    if (!this.item || !this.L) return;
    const v = this.v, t = v.currentTime, s = this.seek;
    if (s && s.running === null) {
      if (now() - s.at > SEEK_WATCH && !v.paused) this.seekLine(`NOT running after ${SEEK_WATCH / 1000} s`);
    } else if (this.isPlaying() && !v.seeking) {
      if (Math.abs(t - this.lastT) < 0.05) {
        if (!this.stall) this.stall = { at: now() - 1000, t, logged: false };
        if (!this.stall.logged && now() - this.stall.at >= STALL_AFTER) {
          this.stall.logged = true;
          this.L.stalls += 1;
          this.log(`stall #${this.item.id} at ${r1(t)}: readyState ${v.readyState} networkState ${v.networkState} ` +
                   `buffered ${ranges(v.buffered)}`);
        }
      } else if (this.stall) {
        if (this.stall.logged) this.log(`stall #${this.item.id} over after ${r1((now() - this.stall.at) / 1000)} s`);
        this.stall = null;
      }
    } else {
      this.stall = null;
    }
    this.lastT = t;
    if (!v.seeking) this.status();
  }

  seekLine(outcome) {
    const s = this.seek, v = this.v;
    if (!s || s.logged) return;
    s.logged = true;
    if (s.running === null) s.running = -1;
    const landed = s.landed === null ? "-" : r1(s.landed);
    const miss = s.landed !== null && Math.abs(s.landed - s.target) > MISSED ? ` MISSED by ${r1(s.landed - s.target)}` : "";
    this.log(`seek #${this.item.id} ${s.why} ${r1(s.from)} -> ${r1(s.target)}: seeked ${s.seeked === null ? "-" : s.seeked + "ms"} ` +
             `landed ${landed}${miss}, ${outcome}${outcome === "running" ? " after " + s.running + "ms" : ""} ` +
             `t=${r1(v.currentTime)} rs=${v.readyState} buf=${ranges(v.buffered)} vt=${v.videoTracks ? v.videoTracks.length : "-"}`);
  }

  finish(reason) {
    const L = this.L;
    if (!L || L.done || !this.item) return;
    L.done = true;
    if (this.seek && !this.seek.logged) this.seekLine("item over");
    this.log(`end #${L.id} ${reason} at ${r1(this.v.currentTime)}/${r1(this.duration())}: meta ${L.meta ?? "-"}ms ` +
             `playing ${L.first ?? "-"}ms frame ${L.frame ?? "-"}ms seeks ${L.seeks} missed ${L.missed} ` +
             `stalls ${L.stalls} waiting ${L.waiting}`);
  }
}
