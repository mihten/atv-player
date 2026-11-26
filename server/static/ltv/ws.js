// LTVPlayer: the socket to the server as the player of session "ltv" (plan 8.1, 9.5).
// Reconnects with 1, 2, 4, 8, 10 s back-off; pings every 2 s for the first 8 pings after a connect, then every 15 s, and
// reconnects when no pong came for 40 s. The pongs carry the server's monotonic clock: serverNow() = this page's clock
// moved by the offset of the ping with the lowest round trip of the last 8 (as NTP does; port of mtv/sinklink.py), for
// the headphones' clock messages. `log` lines written while the socket is down are kept (up to 60) and sent after the
// reconnect; status reports are not kept (the next one replaces them).

const BACKOFF = [1000, 2000, 4000, 8000, 10000];
const PING_FAST = 2000, PING_SLOW = 15000, FAST_PINGS = 8;
const PONG_LIMIT = 40000;
const LOG_KEEP = 60;

export class Socket {
  constructor({ onMessage, onOpen, onClose }) {
    this.onMessage = onMessage;
    this.onOpen = onOpen;
    this.onClose = onClose;
    this.ws = null;
    this.open = false;
    this.tries = 0;
    this.logs = [];
    this.lastPong = 0;
    this.n = 0;
    this.downSince = Date.now();
    this.timer = null;
    this.sentAt = new Map();   // ping n -> performance.now() when sent
    this.samples = [];         // [round trip ms, server s - local s] of the last pings
    this.pings = 0;
    this.pingTimer = null;
  }

  /** The server's monotonic clock now (seconds), or null before the first pong of this connection. */
  serverNow() {
    if (!this.samples.length) return null;
    let best = this.samples[0];
    for (const s of this.samples) if (s[0] < best[0]) best = s;
    return performance.now() / 1000 + best[1];
  }

  /** The best round trip so far (ms), for the log. */
  rtt() {
    return this.samples.length ? Math.round(Math.min(...this.samples.map((s) => s[0]))) : null;
  }

  schedulePing() {
    clearTimeout(this.pingTimer);
    this.pingTimer = setTimeout(() => { this.ping(); this.schedulePing(); },
                                this.pings < FAST_PINGS ? PING_FAST : PING_SLOW);
  }

  url() {
    return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws?role=player&kind=ltv";
  }

  connect() {
    clearTimeout(this.timer);
    let ws;
    try {
      ws = new WebSocket(this.url());
    } catch (e) {
      this.retry();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      if (this.ws !== ws) return;
      const down = Date.now() - this.downSince;
      this.open = true;
      this.tries = 0;
      this.lastPong = Date.now();
      this.samples = [];   // a restarted server has a new monotonic clock
      this.sentAt.clear();
      this.pings = 0;
      const kept = this.logs.splice(0);
      for (const m of kept) ws.send(JSON.stringify(m));
      this.onOpen?.(down);
      this.ping();
      this.schedulePing();
    };
    ws.onmessage = (ev) => {
      if (this.ws !== ws) return;
      let m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m && m.type === "pong") { this.pong(m); return; }
      if (m && typeof m.type === "string") this.onMessage(m);
    };
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.dropped();
    };
    ws.onerror = () => { /* onclose follows */ };
  }

  dropped() {
    const was = this.open;
    this.open = false;
    this.ws = null;
    clearTimeout(this.pingTimer);
    if (was) {
      this.downSince = Date.now();
      this.onClose?.();
    }
    this.retry();
  }

  retry() {
    clearTimeout(this.timer);
    const wait = BACKOFF[Math.min(this.tries, BACKOFF.length - 1)];
    this.tries += 1;
    this.timer = setTimeout(() => this.connect(), wait);
  }

  /** Reconnect now if the socket is down (the page became visible again). */
  wake() {
    if (!this.open) { this.tries = 0; this.connect(); }
  }

  ping() {
    if (!this.open) return;
    if (Date.now() - this.lastPong > PONG_LIMIT) {
      const ws = this.ws;
      this.dropped();
      try { ws?.close(); } catch (e) { /* already gone */ }
      return;
    }
    const n = ++this.n;
    this.pings += 1;
    this.sentAt.set(n, performance.now());
    for (const k of this.sentAt.keys()) if (k <= n - 16) this.sentAt.delete(k);
    this.send({ type: "ping", n });
  }

  pong(m) {
    this.lastPong = Date.now();
    const t0 = this.sentAt.get(m.n);
    this.sentAt.delete(m.n);
    if (t0 === undefined || typeof m.t !== "number") return;
    const now = performance.now();
    this.samples.push([now - t0, m.t - (t0 + now) / 2000]);
    if (this.samples.length > 8) this.samples.shift();
  }

  send(m) {
    if (!this.open) {
      if (m.type === "log") {
        this.logs.push(m);
        if (this.logs.length > LOG_KEEP) this.logs.shift();
      }
      return false;
    }
    try {
      this.ws.send(JSON.stringify(m));
      return true;
    } catch (e) {
      return false;
    }
  }

  log(text) {
    this.send({ type: "log", message: String(text).slice(0, 1450) });
  }

  cmd(name, args = {}) {
    return this.send({ type: "cmd", cmd: name, ...args });
  }
}
