// LTVPlayer: the socket to the server as the player of session "ltv" (plan 8.1, 9.5).
// Reconnects with 1, 2, 4, 8, 10 s back-off; pings every 15 s and reconnects when no pong came for 40 s.
// `log` lines written while the socket is down are kept (up to 60) and sent after the reconnect; status reports
// are not kept (the next one replaces them).

const BACKOFF = [1000, 2000, 4000, 8000, 10000];
const PING_EVERY = 15000;
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
    setInterval(() => this.ping(), PING_EVERY);
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
      const kept = this.logs.splice(0);
      for (const m of kept) ws.send(JSON.stringify(m));
      this.onOpen?.(down);
    };
    ws.onmessage = (ev) => {
      if (this.ws !== ws) return;
      let m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m && m.type === "pong") { this.lastPong = Date.now(); return; }
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
    this.send({ type: "ping", n: ++this.n });
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
