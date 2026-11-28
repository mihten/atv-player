// LTVPlayer's sound on the atv-audio headphones (the sink program on the music server), the way the TV Player does it
// (port of mtv/sinklink.py; owner rules 2026-11-12).
//
// With Headphones chosen the TV shows the picture muted, the app tells the server `route {sink: true}` and sends the
// sink a `clock` (item, position, server time, state) four times a second while it plays; the sink opens the same file
// on the headphones and follows that clock. Rules:
//   - the sound output changes ONLY by a deliberate choice (Settings, the quick menu, the web remote), never by itself;
//   - a server start waits up to 3 s for the sink's `ready` so picture and sound begin together;
//   - the headphones missing at a start, or lost while playing, pause playback with a notice that stays until Play;
//     once per loss: a Play while they are still off plays without sound;
//   - the headphones back while it plays: the sink resumes by itself at the picture's position;
//   - Headphones chosen while they are not ready: ask the server to connect them (`sink_connect`) at once and after 30 s,
//     for one minute, then stop asking for good.
// The server clock comes from the socket's ping offset (ws.js serverNow).

import { settings } from "./settings.js";

const READY_WAIT = 3000;        // ms to wait for the sink's ready before the picture starts without it
const SEEK_SETTLE = 400;        // ms after a seek the clock says "stalled" (the element shows the target before it plays)
const CONNECT_WINDOW = 60000;   // ms the app asks for the headphones after Headphones was chosen
const CONNECT_RETRY = 30000;    // ms between two connect requests (BlueZ's own connect timeout)
const CLOCK_EVERY = 250;

const usableSink = (k) => !!(k && k.connected && k.output && k.output.ready);

export class SinkLink {
  /** ui: {notice(text, ms), version, changed()} */
  constructor(engine, sock, ui) {
    this.e = engine;
    this.sock = sock;
    this.ui = ui;
    this.v = engine.v;
    this.sink = null;          // state.sink of the last state
    this.waiting = false;      // a start waits for the sink's ready
    this.waitT0 = 0;
    this.readyItem = null;
    this.hadItem = false;
    this.lossArmed = true;
    this.sentState = "";
    this.notice = null;
    this.seekUntil = 0;
    this.connectT0 = null;
    this.nextConnect = 0;
    engine.gate = () => this.waiting;
    engine.onLoad = (it, autoplay) => this.beginItem(it, autoplay);
    engine.onSeek = () => this.noteSeek();
    this.applyMute();
    setInterval(() => this.tick(), CLOCK_EVERY);
  }

  get on() { return settings.get("headphones"); }
  get usable() { return this.sock.open && usableSink(this.sink); }
  get connecting() { return this.connectT0 !== null; }
  log(t) { this.sock.log(`atv-audio: ${t}`); }

  targetText() {
    if (!this.sock.open) return "atv-audio: no connection to the server";
    const k = this.sink;
    if (!k || !k.connected) return "atv-audio is not connected";
    if (!k.output) return "atv-audio: no output found";
    return `atv-audio: ${k.output.name || "headphones"} not connected`;
  }

  statusText() {
    const k = this.sink;
    if (!k || !k.connected) return "atv-audio: not connected";
    if (!k.output) return "atv-audio: connected, no output found";
    return `atv-audio: ${k.output.name || "headphones"}${k.output.ready ? "" : " (not available)"}`;
  }

  /** Settings / quick menu line: "Headphones X · aptX HD" | "connecting…" | "not connected". */
  outStatus() {
    if (!this.on) return "";
    if (this.usable) {
      const o = this.sink.output;
      return [o.name || "Headphones", o.codec ? codecName(o.codec) : null].filter(Boolean).join(" · ");
    }
    return this.connecting ? "connecting…" : "not connected";
  }

  applyMute() { this.v.muted = !!this.on; }

  // ---------------------------------------------------------------- messages to the server
  /** On every (re)connect: the clock (once the server time is known), lip sync, route, the output report. */
  sendConfig() {
    this.sentState = "";
    this.clock(true);
    this.sock.send({ type: "lip_sync", ms: settings.get("lipSync") });
    this.sock.send({ type: "route", sink: !!this.on });
    this.report();
  }

  report() {
    this.sock.send({ type: "output", name: "LTVPlayer", screen: "LG TV", sound: this.on ? "headphones" : "hdmi",
                     version: this.ui.version });
  }

  clockState() {
    const e = this.e, v = this.v;
    if (!e.item || e.stopped) return null;
    if (v.ended) return "ended";
    if (!e.wantPlay) return "paused";
    if (this.waiting || !e.started || v.paused || v.seeking || v.readyState < 3 || performance.now() < this.seekUntil) {
      return "stalled";
    }
    return "playing";
  }

  /** clock {item, position, at (server time), state}: every tick while playing, at once on a change. */
  clock(force = false) {
    if (!this.on || !this.e.item) return;
    const at = this.sock.serverNow();
    const state = this.clockState();
    if (at === null || state === null) return;
    if (!force && state !== "playing" && state === this.sentState) return;
    this.sentState = state;
    this.sock.send({ type: "clock", item: this.e.item.id, position: Math.round(Math.max(0, this.e.position()) * 1000) / 1000,
                     at: Math.round(at * 1000) / 1000, state });
  }

  noteSeek() {
    this.seekUntil = performance.now() + SEEK_SETTLE;
    this.clock(true);
  }

  setLipSync(ms) {
    settings.set("lipSync", ms);
    this.sock.send({ type: "lip_sync", ms });
    this.log(`lip sync ${ms} ms`);
  }

  volume(percent, muted) {
    this.sock.send({ type: "sink_volume", percent, muted });
  }

  // ---------------------------------------------------------------- the choice
  /** Settings / quick menu / web remote: Sound output = headphones (true) or the TV (false), at once. While it plays:
   *  pause with a notice until Play. Headphones that are not ready: ask for them for a minute. */
  setOn(on, why = "settings") {
    if (on === this.on) {
      if (on && !this.usable && !this.connecting) this.startConnect();   // chosen again on purpose: one more minute
      this.report();
      return;
    }
    const playing = this.e.wantPlay && !!this.e.item && !this.e.stopped;
    if (playing) this.e.localPause("sound output");
    settings.set("headphones", on);
    this.waiting = false;
    this.sentState = "";
    this.applyMute();
    if (on) this.clock(true);   // before the route: a sink that opens the item opens it at our position
    this.sock.send({ type: "route", sink: on });
    this.log(`sound output -> ${on ? "headphones, TV muted" : "the TV's own sound"} (${why})` +
             `${this.e.item ? ` at ${this.e.item.id}` : ""}; ${this.statusText()}`);
    this.lossArmed = playing ? this.usable : true;
    if (on && !this.usable) this.startConnect();
    else if (!on) this.connectT0 = null;
    if (playing) this.showNotice(`Sound output changed to ${on ? "the headphones" : "the TV"} — playback paused`);
    this.report();
    this.ui.changed();
  }

  startConnect() {
    this.connectT0 = this.nextConnect = performance.now();
    this.log(`headphones not ready, asking to connect them for ${CONNECT_WINDOW / 1000} s; ${this.statusText()}`);
  }

  connectTick() {
    if (this.connectT0 === null) return;
    const now = performance.now();
    if (!this.on) { this.connectT0 = null; return; }
    if (this.usable) {
      this.log(`headphones ready ${Math.round((now - this.connectT0) / 1000)} s after the choice; ${this.statusText()}`);
      this.connectT0 = null;
      this.ui.changed();
      return;
    }
    if (now - this.connectT0 >= CONNECT_WINDOW) {
      this.log(`headphones still not ready after ${CONNECT_WINDOW / 1000} s, stopped asking; ${this.statusText()}`);
      this.connectT0 = null;
      this.ui.changed();
      return;
    }
    if (now >= this.nextConnect && this.sock.open) {
      this.nextConnect = now + CONNECT_RETRY;
      this.sock.send({ type: "sink_connect" });
    }
  }

  // ---------------------------------------------------------------- a start
  /** A new item opens. Headphones chosen: the picture is muted for every item; a start with autoplay waits for the
   *  sink's ready (max 3 s) when the headphones are there, else it holds with the notice once. */
  beginItem(item, autoplay) {
    this.waiting = false;
    this.sentState = "";
    this.seekUntil = 0;
    if (!this.on) return;
    this.applyMute();
    this.hadItem = this.readyItem === item.id;
    this.readyItem = null;
    if (!autoplay) return;
    this.acknowledge("play");   // an item that starts playing ends a notice
    if (!this.usable) {
      if (!this.checkTarget(`not available at the start of ${item.id}`)) {
        this.log(`${item.id} starts muted without atv-audio; ${this.targetText()}`);
      }
      return;
    }
    this.waiting = true;
    this.waitT0 = performance.now();
  }

  endWait() {
    if (!this.waiting) return;
    this.waiting = false;
    if (this.e.wantPlay) this.e.tryPlay();
  }

  /** Server: the sink has the item open and buffered. */
  ready(id) {
    this.readyItem = id;
    if (this.waiting && this.e.item && this.e.item.id === id) {
      this.log(`ready after ${Math.round(performance.now() - this.waitT0)} ms`);
      this.endWait();
    }
  }

  // ---------------------------------------------------------------- loss
  /** A new state.sink (or the first state on a new socket). */
  setSink(next) {
    const old = this.sink;
    this.sink = next || null;
    if (next && next.connected && !(old && old.connected)) this.sendConfig();
    if (!this.on) return;
    const was = usableSink(old), now = usableSink(next);
    if (was && !now) this.log(`${next && next.connected ? "output gone" : "disconnected"}; the TV stays muted; ${this.statusText()}`);
    else if (!was && now) this.log(`available again; ${this.statusText()}`);
    this.checkTarget(next && next.connected ? "output lost" : "sink disconnected");
    if (this.waiting && !this.usable) {
      this.log("lost while waiting for ready, starting muted");
      this.endWait();
    }
  }

  /** The headphones are missing while playback is wanted: pause and a notice that stays until Play. Once per loss (re-armed
   *  when they are usable again). True when it paused. */
  checkTarget(why) {
    if (!this.on) return false;
    if (this.usable) { this.lossArmed = true; return false; }
    if (!(this.lossArmed && this.e.wantPlay)) return false;
    this.lossArmed = false;
    this.e.localPause("atv-audio");
    this.showNotice(`${this.targetText()} — playback paused`);
    this.log(`${why} -> paused (notice); ${this.statusText()}`);
    return true;
  }

  /** Play pressed while the headphones are missing and their loss was not shown yet: the notice instead of starting. */
  holdPlay() {
    if (!(this.on && !this.usable && this.lossArmed)) return false;
    this.lossArmed = false;
    this.showNotice(`${this.targetText()} — playback paused`);
    this.log(`not available at play -> paused (notice); ${this.statusText()}`);
    return true;
  }

  showNotice(text) {
    this.notice = text;
    this.ui.notice(text, 0);
    this.sock.send({ type: "notice", text });
  }

  acknowledge(how) {
    if (this.notice === null) return false;
    this.notice = null;
    this.ui.notice("", 0);
    this.log(`notice acknowledged (${how})`);
    this.sock.cmd("dismiss");
    return true;
  }

  tick() {
    this.connectTick();
    if (this.waiting && performance.now() - this.waitT0 >= READY_WAIT) {
      this.log(this.hadItem ? `no ready within ${READY_WAIT / 1000} s, the sink already has the item open -> starting`
                            : "timeout, starting muted");
      this.endWait();
    }
    this.clock();
  }
}

export const codecName = (c) => ({ aptx_hd: "aptX HD", aptx_adaptive: "aptX Adaptive", aptx: "aptX", sbc: "SBC", aac: "AAC",
                                   ldac: "LDAC" }[c] || c);
