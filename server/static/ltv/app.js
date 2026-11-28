// LTVPlayer: boot, socket wiring, screens and keys. Every user action is a command to the server; playback changes only
// when its load / control comes back. Socket down (plan 9.5): OK still plays / pauses the element itself, ◀ / ▶ seek it,
// next / previous say so.
// Keys (0.6.1): Green = play / pause and Red = Stop on EVERY screen (his remote has no transport keys); in the player OK
// = play / pause on release, Blue = the quick menu (open / close; hold OK opens it too), Yellow = the lyrics screen on / off
// (0.7.2, his choice: with the Magic Remote pointer shown a hold does not arrive), ▲▼ / CH = previous / next,
// ◀ ▶ = jump back / forward (Settings → Playback). Stop: the picture goes away and the menu shows again; Play starts the
// item again from 0:00 (server-side, like the TV Player).

import { Socket } from "./ws.js";
import { Engine } from "./engine.js";
import { PlayerView } from "./views/player.js";
import { HomeView } from "./views/home.js";
import { Lists } from "./views/lists.js";
import { QuickMenu } from "./views/quick.js";
import "./views/settings.js";   // registers the Settings pages in the list stack
import { SinkLink } from "./audio.js";
import { LyricsStore } from "./lyrics.js";
import { settings } from "./settings.js";
import { paletteFor, applyPalette, FALLBACK } from "./palette.js";

export const LTV_VERSION = "0.7.2";

const Q = new URLSearchParams(location.search);
const DEBUG = Q.get("debug") === "1";
const KEY = { LEFT: 37, UP: 38, RIGHT: 39, DOWN: 40, OK: 13, BACK: 461, BACKSPACE: 8, ESC: 27, PLAY: 415, PAUSE: 19,
              STOP: 413, REW: 412, FF: 417, CH_UP: 33, CH_DOWN: 34, RED: 403, GREEN: 404, YELLOW: 405, BLUE: 406 };
const HOLD_MS = 500;

if (innerWidth && innerWidth !== 1920) document.documentElement.style.zoom = String(innerWidth / 1920);

const video = document.getElementById("v");
const $ = (id) => document.getElementById(id);
let screen = "home";
let menu = "home";     // the menu screen the player returns to on Back (home | lists)
let state = null;
let everOpen = false;

// ---------------------------------------------------------------- settings that shape the page
function applyBody() {
  const look = settings.get("look");
  document.body.dataset.look = ["noir", "vinyl"].includes(look) ? look : "aurora";
  document.body.classList.toggle("ts-compact", settings.get("textSize") === "compact");
  document.body.classList.toggle("ts-small", settings.get("textSize") === "small");
  document.body.classList.toggle("no-fx", !settings.get("effects"));
}
applyBody();

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
  item(it) { player.setItem(it); home.setItem(it); lists.setItem(it); accent(it); quick.refresh(); },
  present() { quick.close(); show("player"); },
  notice,
  changed() { player.update(); home.update(); lists.playing(engine.isPlaying()); },
  stopped() { quick.close(); if (screen === "player") show(menu); home.update(); player.update(); },
  version: LTV_VERSION,
};

const sock = new Socket({
  onMessage(m) {
    switch (m.type) {
      case "state":
        state = m.state;
        audio.setSink(state.sink);
        player.setState(state); home.setState(state); lists.setState(state); quick.refresh();
        break;
      case "load": engine.load(m); break;
      case "control": engine.control(m); break;
      case "notice": notice(m.text, 5000); break;
      case "sink_ready": audio.ready(m.id); break;
      case "sound_out": audio.setOn(m.mode === "headphones", "web remote"); break;
      case "playlists": case "auto": home.onMessage(m); lists.onMessage(m); break;
      case "mixes": lists.top()?.refresh?.(); break;
      default: break;   // sessions, sound (made for the headphones), volume (the TV's own keys), future types
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
               `zoom=${document.documentElement.style.zoom || 1} from=${Q.get("from") || "-"} palm=${!!window.PalmSystem} ` +
               `settings=${settings.stored} sound=${settings.get("headphones") ? "headphones" : "tv"}`);
    } else {
      sock.log(`socket back after ${Math.round(down / 100) / 10} s`);
    }
    audio.sendConfig();   // the server forgets the route of a player that left
  },
  onClose() {
    $("pill").classList.add("on");
    home.setConnected(false);
    sock.log("socket down");   // kept and sent after the reconnect
  },
});
const engine = new Engine(video, sock, ui);
const audio = new SinkLink(engine, sock, ui);
const lyrics = new LyricsStore((t) => sock.log(t));
const player = new PlayerView($("player"), engine, { lyrics, log: (t) => sock.log(t) });
const isVideoPlaying = () => engine.isPlaying() && engine.item?.kind === "video";
const lists = new Lists($("lists"), {
  engine, sock, notice, isVideoPlaying, audio, version: LTV_VERSION,
  state: () => state,
  homeData: () => home.data,
  prompt: (title, value) => prompt(title, value),
  switchCodec: (c) => switchCodec(c),
});
const home = new HomeView($("home"), {
  engine, sock, notice, version: LTV_VERSION, isVideoPlaying,
  showPlayer: () => show("player"),
  openList: (kind, params) => { lists.open(kind, params); show("lists"); },
  onData: () => lists.homeData(),
});
const quick = new QuickMenu(player.root.querySelector(".qm"), {
  engine, sock, audio, player, notice, state: () => state, log: (t) => sock.log(t),
  openQueue: () => { lists.open("queue", { back: "player" }); show("lists"); },
});
settings.on((k) => { applyBody(); lists.settingsChanged(k); quick.refresh(); });
sock.connect();

// ---------------------------------------------------------------- screens
function show(name) {
  if (screen === name) return;
  screen = name;
  if (name !== "player") { menu = name; quick.close(); }
  document.body.dataset.screen = name;
  if (name === "player") player.show();
  player.shown(name === "player");
  home.shown(name === "home");
  lists.shown(name === "lists");
}
document.body.dataset.screen = screen;

function close() {
  const palm = window.PalmSystem;
  sock.log("exit: Back twice on Home");
  setTimeout(() => { if (palm && palm.platformBack) palm.platformBack(); else window.close(); }, 200);
}

// ---------------------------------------------------------------- the text prompt (the TV's own keyboard)
let promptDone = null;
function prompt(title, value) {
  const box = $("prompt"), input = box.querySelector("input");
  box.querySelector(".pr-t").textContent = title;
  input.value = value || "";
  box.classList.add("on");
  setTimeout(() => { input.focus(); input.select(); }, 50);
  return new Promise((ok) => {
    promptDone = (v) => { promptDone = null; box.classList.remove("on"); input.blur(); ok(v); };
  });
}

// ---------------------------------------------------------------- actions
let seekTarget = null, seekAt = 0, backAt = 0;

function playPause(want) {
  if (!engine.item) return;
  if (engine.stopped) {   // after a Stop: Play loads the item again from 0:00 (server-side)
    if (want === false) return;
    if (!sock.open) { notice("Not connected to the server", 3000); return; }
    audio.acknowledge("play");
    sock.cmd("play");
    sock.log("key: play after stop");
    return;
  }
  const play = want ?? video.paused;
  if (play) {
    if (audio.holdPlay()) return;   // the headphones are missing: the notice once, the next Play plays muted
    audio.acknowledge("play");
  }
  if (sock.open) sock.cmd(play ? "play" : "pause");
  else if (play === video.paused) engine.localToggle();
}

function stop() {
  if (!engine.item || engine.stopped) return;
  if (!sock.open) { notice("Not connected to the server", 3000); return; }
  sock.cmd("stop");
  sock.log(`key: stop on ${screen}`);
}

function step(name) {
  if (!engine.item) return;
  if (!sock.open) { notice("Not connected to the server", 3000); return; }
  if (name === "prev") sock.cmd("prev", { position: Math.round(engine.position() * 100) / 100 });
  else sock.cmd("next");
}

function seekBy(delta) {
  if (!engine.item || engine.stopped) return;
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

/** Settings → Sound → Codec: pause first (the server refuses while it plays), switch, play again once the headphones
 *  report the new codec (the switch cuts the sound for 2-4 s). */
async function switchCodec(codec) {
  const was = engine.isPlaying();
  if (was) playPause(false);
  const until = async (cond, ms) => {
    const end = performance.now() + ms;
    while (performance.now() < end && !cond()) await new Promise((r) => setTimeout(r, 100));
    return cond();
  };
  if (was) await until(() => state && !state.playing, 3000);
  sock.send({ type: "sink_codec", codec });
  sock.log(`settings: codec -> ${codec}`);
  const ok = await until(() => state?.sink?.output?.codec === codec, 12000);
  sock.log(`settings: codec ${ok ? "now" : "still not"} ${codec}`);
  if (was) playPause(true);
}

// ---------------------------------------------------------------- keys
// okHeld: OK is down (keydown seen, no keyup yet). A keydown while held within 600 ms of the last one is the TV's key
// repeat (also when it comes without e.repeat); a lost keyup cannot block OK for longer than that.
let okDown = 0, okLast = 0, okHeld = false, holdTimer = null, holdFired = false, okSwallow = false;

function playerKey(k, e) {
  if (k === KEY.OK) {
    const t = performance.now(), repeat = e.repeat || (okHeld && t - okLast < 600);
    okLast = t;
    // held: one press (the TV repeats keydown). Also keeps the repeats of the hold that opened the quick menu out of it.
    if (repeat) return;
    okHeld = true;
    if (quick.isOpen) { quick.key(k); return; }
    clearTimeout(holdTimer);
    okDown = t;
    holdFired = false;
    holdTimer = setTimeout(() => {   // still down after HOLD_MS: the quick menu
      holdTimer = null;
      holdFired = true;
      sock.log("key: OK held: quick menu");
      quick.open();
    }, HOLD_MS);
    return;
  }
  if (quick.isOpen && quick.key(k)) return;
  switch (k) {
    case KEY.BLUE: if (quick.isOpen) quick.close(); else { sock.log("key: Blue: quick menu"); quick.open(); } return;
    case KEY.YELLOW: player.toggleLyricsScreen("Yellow"); quick.refresh(); return;
    case KEY.PLAY: playPause(true); break;
    case KEY.PAUSE: playPause(false); break;
    case KEY.UP: case KEY.CH_DOWN: step("prev"); break;
    case KEY.DOWN: case KEY.CH_UP: step("next"); break;
    case KEY.LEFT: case KEY.REW: seekBy(-settings.get("seekBack")); break;
    case KEY.RIGHT: case KEY.FF: seekBy(settings.get("seekForward")); break;
    case KEY.BACK: case KEY.BACKSPACE: case KEY.ESC: show(menu); return;
    default: return;
  }
  player.show();
}

function okUp() {
  sock.log(`key: OK up after ${Math.round(performance.now() - okDown)} ms${pointerShown ? " (pointer shown)" : ""}`);
  if (okSwallow) { okSwallow = false; return; }
  if (holdTimer) {   // released before HOLD_MS: a press = play / pause
    clearTimeout(holdTimer);
    holdTimer = null;
    playPause();
    player.show();
  } else if (holdFired) {
    holdFired = false;
  }
}

function listsKey(k) {
  if (k === KEY.OK) return;   // menus act on OK's release (menuOkUp): a held OK does nothing there
  if (lists.key(k)) return;   // arrows move the focus, OK activates
  if (k === KEY.PLAY || k === KEY.PAUSE) {
    playPause(k === KEY.PLAY);
  } else if (k === KEY.BACK || k === KEY.BACKSPACE || k === KEY.ESC) {
    if (lists.top()?.params?.back === "player") { lists.back(); show("player"); return; }   // the queue from the quick menu
    if (!lists.back()) show("home");   // the last list screen: Home with its focus
  }
}

function homeKey(k) {
  if (k === KEY.OK) return;   // see listsKey
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
  if (promptDone) {   // the text prompt has the TV keyboard: only OK / Back are ours
    if (k === KEY.OK) { e.preventDefault(); promptDone($("prompt").querySelector("input").value); }
    else if (k === KEY.BACK || k === KEY.ESC) { e.preventDefault(); promptDone(null); }
    return;
  }
  if (Object.values(KEY).includes(k)) e.preventDefault();
  if (engine.blocked && (k === KEY.OK || k === KEY.PLAY || k === KEY.GREEN)) {
    engine.unblock();
    if (k === KEY.OK) okSwallow = true;
    return;
  }
  if (e.repeat && (k === KEY.RED || k === KEY.GREEN || k === KEY.STOP || k === KEY.BLUE || k === KEY.YELLOW)) return;
  if (k === KEY.GREEN) { playPause(); if (screen === "player") player.show(); quick.refresh(); return; }
  if (k === KEY.RED || k === KEY.STOP) { stop(); return; }
  if (k === KEY.OK && screen !== "player") {   // menus: remember the press, act on its release (0.7.1, his report)
    const t = performance.now(), repeat = e.repeat || (okHeld && t - okLast < 600);
    okLast = t;
    if (repeat) return;
    okHeld = true;
    menuOk = { at: t, screen };
    return;
  }
  if (screen === "player") playerKey(k, e); else if (screen === "lists") listsKey(k); else homeKey(k);
}, true);

// A menu OK counts on release when it was shorter than HOLD_MS (held longer = nothing: in the player a hold opens the
// quick menu, in the menus it used to fire the focused tile again with every key repeat).
let menuOk = null;
function menuOkUp() {
  const m = menuOk;
  menuOk = null;
  if (!m || m.screen !== screen) return;
  const held = performance.now() - m.at;
  if (held >= HOLD_MS) { sock.log(`key: OK held ${Math.round(held)} ms on ${screen}: ignored`); return; }
  if (screen === "lists") lists.key(KEY.OK); else if (screen === "home") home.key(KEY.OK);
}

// The wheel in the player = the headphones' volume (5 % a notch; nothing with the TV's own sound: an app cannot set it).
let wheelAcc = 0;
document.addEventListener("wheel", (e) => {
  if (screen !== "player" || quick.isOpen) return;
  wheelAcc += e.deltaY;
  if (Math.abs(wheelAcc) < 100) return;
  const up = wheelAcc < 0;
  wheelAcc = 0;
  const vol = quick.volume();
  if (!audio.on || !vol) return;
  const p = up ? Math.min(100, (Math.floor(vol.percent / 5) + 1) * 5) : Math.max(0, (Math.ceil(vol.percent / 5) - 1) * 5);
  quick.setVolume(p, up ? false : vol.muted);
  notice(`Headphones ${p} %`, 1500);
}, { passive: true });

document.addEventListener("keyup", (e) => {
  if (e.keyCode === KEY.OK) okHeld = false;
  if (promptDone) { menuOk = null; return; }
  if (e.keyCode === KEY.OK && menuOk) { menuOkUp(); return; }
  if (e.keyCode === KEY.OK && screen === "player" && !quick.isOpen) okUp();
  else if (e.keyCode === KEY.OK) { clearTimeout(holdTimer); holdTimer = null; okSwallow = false; }
}, true);

// The Magic Remote pointer (0.7.2): the app is keys only. A web app cannot switch the pointer off (LG); an arrow key ends
// pointer mode. Its state goes to the journal with every OK in the player, to see how OK arrives while it is shown.
let pointerShown = false;
document.addEventListener("cursorStateChange", (e) => {
  const v = !!(e.detail && e.detail.visibility);
  if (v === pointerShown) return;
  pointerShown = v;
  sock.log(`pointer ${v ? "shown" : "hidden"} on ${screen}`);
});

// ---------------------------------------------------------------- life cycle (plan 12.2)
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    const was = !video.paused;
    if (was && settings.get("keepMusic") && engine.item?.kind === "audio") {   // music keeps playing, videos pause
      sock.log("hidden: music keeps playing");
      return;
    }
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

window.ltv = { engine, sock, player, home, lists, quick, audio, lyrics, settings,
               get screen() { return screen; }, get state() { return state; } };
