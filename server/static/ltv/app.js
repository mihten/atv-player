// LTVPlayer: boot, socket wiring, screens and keys (Home with the focus engine since P4, plain player since P3).
// Every user action is a command to the server; playback changes only when its load / control comes back.
// Socket down (plan 9.5): OK still plays / pauses the element itself, ◀ / ▶ seek it, next / previous say so.

import { Socket } from "./ws.js";
import { Engine } from "./engine.js";
import { PlayerView } from "./views/player.js";
import { HomeView } from "./views/home.js";
import { Lists } from "./views/lists.js";
import { paletteFor, applyPalette, FALLBACK } from "./palette.js";

export const LTV_VERSION = "0.5.0";

const Q = new URLSearchParams(location.search);
const DEBUG = Q.get("debug") === "1";
const KEY = { LEFT: 37, UP: 38, RIGHT: 39, DOWN: 40, OK: 13, BACK: 461, BACKSPACE: 8, ESC: 27, PLAY: 415, PAUSE: 19,
              STOP: 413, REW: 412, FF: 417, CH_UP: 33, CH_DOWN: 34 };
const SEEK_STEP = 10;

if (innerWidth && innerWidth !== 1920) document.documentElement.style.zoom = String(innerWidth / 1920);

const video = document.getElementById("v");
const $ = (id) => document.getElementById(id);
let screen = "home";
let menu = "home";     // the menu screen the player returns to on Back (home | lists)
let state = null;
let everOpen = false;

// ---------------------------------------------------------------- notice capsule, reconnect pill
let noticeTimer = null;
function notice(text, ms) {
  const el = $("capsule");
  clearTimeout(noticeTimer);
  el.textContent = text || "";
  el.classList.toggle("on", !!text);
  if (text && ms) noticeTimer = setTimeout(() => el.classList.remove("on"), ms);
}

// ---------------------------------------------------------------- accent = the current cover's colours (plan 11.1)
let accentFor = null;
function accent(it) {
  const id = it ? it.id : null;
  if (id === accentFor) return;
  accentFor = id;
  paletteFor(it).then((p) => { if (accentFor === id) applyPalette(p); });
}
applyPalette(FALLBACK);

const ui = {
  item(it) { player.setItem(it); home.setItem(it); lists.setItem(it); accent(it); },
  present() { show("player"); },
  notice,
  changed() { player.update(); home.update(); lists.playing(engine.isPlaying()); },
};

const sock = new Socket({
  onMessage(m) {
    switch (m.type) {
      case "state": state = m.state; player.setState(state); home.setState(state); lists.setState(state); break;
      case "load": engine.load(m); break;
      case "control": engine.control(m); break;
      case "notice": notice(m.text, 5000); break;
      case "playlists": case "auto": home.onMessage(m); lists.onMessage(m); break;
      default: break;   // sessions, audio_copies, future types
    }
  },
  onOpen(down) {
    $("pill").classList.remove("on");
    home.setConnected(true);
    home.loadAll();   // on start and after every reconnect (plan 10.4)
    if (everOpen) lists.onMessage({ type: "reconnect" });
    if (!everOpen) {
      everOpen = true;
      sock.log(`ltv ${LTV_VERSION} ua=${navigator.userAgent} ${innerWidth}x${innerHeight} dpr=${devicePixelRatio} ` +
               `zoom=${document.documentElement.style.zoom || 1} from=${Q.get("from") || "-"} palm=${!!window.PalmSystem}`);
    } else {
      sock.log(`socket back after ${Math.round(down / 100) / 10} s`);
    }
  },
  onClose() {
    $("pill").classList.add("on");
    home.setConnected(false);
    sock.log("socket down");   // kept and sent after the reconnect
  },
});
const engine = new Engine(video, sock, ui);
const player = new PlayerView($("player"), engine);
const isVideoPlaying = () => engine.isPlaying() && engine.item?.kind === "video";
const lists = new Lists($("lists"), {
  engine, sock, notice, isVideoPlaying,
  state: () => state,
  homeData: () => home.data,
});
const home = new HomeView($("home"), {
  engine, sock, notice, version: LTV_VERSION, isVideoPlaying,
  showPlayer: () => show("player"),
  openList: (kind, params) => { lists.open(kind, params); show("lists"); },
  onData: () => lists.homeData(),
});
sock.connect();

// ---------------------------------------------------------------- screens
function show(name) {
  if (screen === name) return;
  screen = name;
  if (name !== "player") menu = name;
  document.body.dataset.screen = name;
  if (name === "player") player.show();
  home.shown(name === "home");
  lists.shown(name === "lists");
}
document.body.dataset.screen = screen;

function close() {
  const palm = window.PalmSystem;
  sock.log("exit: Back twice on Home");
  setTimeout(() => { if (palm && palm.platformBack) palm.platformBack(); else window.close(); }, 200);
}

// ---------------------------------------------------------------- keys
let seekTarget = null, seekAt = 0, backAt = 0;

function playPause(want) {
  if (!engine.item) return;
  const play = want ?? video.paused;
  if (sock.open) sock.cmd(play ? "play" : "pause");
  else if (play === video.paused) engine.localToggle();
}

function step(name) {
  if (!engine.item) return;
  if (!sock.open) { notice("Not connected to the server", 3000); return; }
  if (name === "prev") sock.cmd("prev", { position: Math.round(engine.position() * 100) / 100 });
  else sock.cmd("next");
}

function seekBy(delta) {
  if (!engine.item) return;
  const d = engine.duration();
  const base = seekTarget !== null && performance.now() - seekAt < 1500 ? seekTarget : engine.position();
  let target = Math.max(0, base + delta);
  if (d) target = Math.min(target, Math.max(0, d - 2));
  seekTarget = target;   // held keys add up before the server's seek comes back (the TV Player's rule)
  seekAt = performance.now();
  player.preview = target;
  setTimeout(() => { if (performance.now() - seekAt >= 1400) { player.preview = null; player.update(); } }, 1500);
  if (sock.open) sock.cmd("seek", { position: Math.round(target * 1000) / 1000 });
  else engine.seekTo(target, "key");
  player.update();
}

function playerKey(k) {
  switch (k) {
    case KEY.OK: playPause(); break;
    case KEY.PLAY: playPause(true); break;
    case KEY.PAUSE: case KEY.STOP: playPause(false); break;
    case KEY.UP: case KEY.CH_DOWN: step("prev"); break;
    case KEY.DOWN: case KEY.CH_UP: step("next"); break;
    case KEY.LEFT: case KEY.REW: seekBy(-SEEK_STEP); break;
    case KEY.RIGHT: case KEY.FF: seekBy(SEEK_STEP); break;
    case KEY.BACK: case KEY.BACKSPACE: case KEY.ESC: show(menu); return;
    default: return;
  }
  player.show();
}

function listsKey(k) {
  if (lists.key(k)) return;   // arrows move the focus, OK activates
  if (k === KEY.PLAY || k === KEY.PAUSE) {
    playPause(k === KEY.PLAY);
  } else if (k === KEY.BACK || k === KEY.BACKSPACE || k === KEY.ESC) {
    if (!lists.back()) show("home");   // the last list screen: Home with its focus
  }
}

function homeKey(k) {
  if (home.key(k)) return;   // arrows move the focus, OK activates the focused tile
  if (k === KEY.PLAY || k === KEY.PAUSE) {
    playPause(k === KEY.PLAY);
  } else if (k === KEY.BACK || k === KEY.BACKSPACE || k === KEY.ESC) {
    if (performance.now() - backAt < 2000) { close(); return; }
    backAt = performance.now();
    notice("Press Back again to close", 2000);
  }
}

document.addEventListener("keydown", (e) => {
  const k = e.keyCode;
  if (DEBUG) sock.log(`key ${k}${e.repeat ? " repeat" : ""} on ${screen}`);
  if (Object.values(KEY).includes(k)) e.preventDefault();
  if (engine.blocked && (k === KEY.OK || k === KEY.PLAY)) { engine.unblock(); return; }
  if (screen === "player") playerKey(k); else if (screen === "lists") listsKey(k); else homeKey(k);
}, true);

document.addEventListener("mousemove", () => { if (screen === "player") player.show(); }, true);

// ---------------------------------------------------------------- life cycle (plan 12.2)
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    const was = !video.paused;
    if (was) { engine.wantPlay = false; video.pause(); sock.cmd("pause"); }
    sock.log(`hidden${was ? ": paused" : ""}`);
  } else {
    sock.log("visible");
    sock.wake();
  }
});

window.addEventListener("error", (e) => {
  if (e.target && e.target !== window) return;   // media / image errors have their own handling
  sock.log(`JSERR ${e.message || ""} ${(e.filename || "").split("/").pop()}:${e.lineno || ""}`);
});
window.addEventListener("unhandledrejection", (e) => {
  sock.log(`JSERR unhandled rejection ${e.reason && (e.reason.stack || e.reason.message) || e.reason}`.slice(0, 600));
});

window.ltv = { engine, sock, player, home, lists, get screen() { return screen; }, get state() { return state; } };
