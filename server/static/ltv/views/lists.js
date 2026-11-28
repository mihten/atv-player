// LTVPlayer: the list screens behind Home (plan 10.5, 10.7; ports of PlaylistsView.swift, ContentView.swift's
// VideoListView / FolderRootsView / FolderView and QueueView.swift, Aurora look). A stack of screens on one layer:
// Back pops to the previous screen with its focus, the last Back returns to Home. Grids (Playlists, Music videos) use
// the focus engine of Home; row lists use VList, which renders only a window of rows (a queue can hold 2000 entries).
// Every start is a command to the server and follows the Apple TV app: shuffled, a picked entry plays first (plan 8.2).

import { Focus, measureMove, markMoving } from "../focus.js";
import { Backdrop } from "../backdrop.js";
import { getJSON, plural, playlistDetail, esc } from "../api.js";
import { card, poster, rowInner, count } from "./tiles.js";
import { settings } from "../settings.js";

// Row heights per Settings → Theme → Text size in lists (the CSS classes ts-compact / ts-small on <body> match them).
const SIZES = { normal: { row: 84, pitch: 96 }, compact: { row: 68, pitch: 78 }, small: { row: 56, pitch: 64 } };
const HEADER = 84, TEXT = 76;
const SAFE_TOP = 60, SAFE_BOTTOM = 1080 - 80, WINDOW = 700;   // rows rendered beyond the screen above and below
const size = () => SIZES[settings.get("textSize")] || SIZES.normal;

// ==================================================================== VList: a virtualised vertical list
// entries: {key, type: "row" | "header" | "text", focusable, html() -> inner markup, act()}
export class VList {
  /** page: the translated page element; body: the positioned container of the rows (inside page).
   *  opts: {name, log, videoPlaying, fps} */
  constructor(screenRoot, page, body, opts) {
    this.root = screenRoot;
    this.page = page;
    this.body = body;
    this.opts = opts;
    this.entries = [];
    this.ys = [];
    this.total = 0;
    this.fi = -1;          // focused entry index
    this.ty = 0;
    this.nodes = new Map();   // entry index -> element (the rendered window)
    this.raf = 0;   // keys only (0.7.2): no pointer focus, clicks or wheel in the lists
  }

  /** New entries; the focus stays on the entry with the same key (else the same index, else the first focusable). */
  setEntries(entries, { focusKey = undefined } = {}) {
    const keep = focusKey !== undefined ? focusKey : this.entries[this.fi]?.key;
    const oldIndex = this.fi;
    this.entries = entries;
    this.ys = [];
    let y = 0;
    const pitch = size().pitch;
    for (const e of entries) {
      this.ys.push(y);
      y += e.type === "header" ? HEADER : e.type === "text" ? TEXT : pitch;
    }
    this.total = y;
    this.body.style.height = `${y}px`;
    for (const el of this.nodes.values()) el.remove();
    this.nodes.clear();
    let fi = keep !== undefined ? entries.findIndex((e) => e.key === keep && e.focusable) : -1;
    if (fi < 0 && oldIndex >= 0) fi = this.nearestFocusable(Math.min(oldIndex, entries.length - 1));
    if (fi < 0) fi = this.nearestFocusable(0);
    this.fi = fi;
    this.clampTy();
    this.render();
  }

  nearestFocusable(i) {
    for (let d = 0; d < this.entries.length; d += 1) {
      if (this.entries[i + d]?.focusable) return i + d;
      if (this.entries[i - d]?.focusable) return i - d;
    }
    return -1;
  }

  height(i) { const t = this.entries[i].type; return t === "header" ? HEADER : t === "text" ? TEXT : size().row; }
  bodyTop() { return this.body.offsetTop; }
  minTy() { return Math.min(0, 1080 - 70 - (this.bodyTop() + this.total)); }
  clampTy() {
    const before = this.ty;
    this.ty = Math.max(this.minTy(), Math.min(0, this.ty));
    if (this.ty !== before || this.page.style.transform !== `translateY(${this.ty}px)`) markMoving();
    this.page.style.transform = `translateY(${this.ty}px)`;
  }

  /** Render the rows near the view (and the focused one); drop the rest. */
  render() {
    const top = -this.ty - this.bodyTop() - WINDOW, bottom = -this.ty - this.bodyTop() + 1080 + WINDOW;
    const want = new Set();
    for (let i = 0; i < this.entries.length; i += 1) {
      if (this.ys[i] + this.height(i) >= top && this.ys[i] <= bottom) want.add(i);
    }
    if (this.fi >= 0) want.add(this.fi);
    for (const [i, el] of this.nodes) {
      if (!want.has(i)) { el.remove(); this.nodes.delete(i); }
    }
    for (const i of want) {
      if (this.nodes.has(i)) continue;
      const e = this.entries[i];
      const el = document.createElement("div");
      el.className = e.type === "row" ? `row${e.focusable ? "" : " dim"}${e.cls ? ` ${e.cls}` : ""}` : `l-${e.type}`;
      el.dataset.vi = String(i);
      if (e.focusable) el.dataset.focus = String(e.key);
      el.style.top = `${this.ys[i]}px`;   // top, not transform: the focus lift is a transform
      el.innerHTML = e.html();
      this.body.appendChild(el);
      this.nodes.set(i, el);
    }
    for (const [i, el] of this.nodes) el.classList.toggle("focused", i === this.fi);
    for (const img of this.body.querySelectorAll("img[data-src]")) { img.src = img.dataset.src; img.removeAttribute("data-src"); }
  }

  /** Rebuild the markup of the rendered rows (playing marker, titles that arrived later). */
  repaint() {
    for (const [i, el] of this.nodes) {
      el.innerHTML = this.entries[i].html();
    }
    for (const img of this.body.querySelectorAll("img[data-src]")) { img.src = img.dataset.src; img.removeAttribute("data-src"); }
  }

  focused() { return this.entries[this.fi] || null; }

  set(i, { scroll = true } = {}) {
    if (i < 0 || !this.entries[i]?.focusable) return;
    this.fi = i;
    if (scroll) this.reveal(i); else this.render();
  }

  move(dir) {
    if (dir !== "up" && dir !== "down") return false;   // rows: ◀ / ▶ do nothing
    const step = dir === "down" ? 1 : -1;
    let i = this.fi + step;
    while (i >= 0 && i < this.entries.length && !this.entries[i].focusable) i += step;
    if (i < 0 || i >= this.entries.length) {
      if (dir === "up" && this.ty !== 0) { this.ty = 0; this.clampTy(); this.render(); this.measure(); }   // top: the title again
      return false;
    }
    this.set(i);
    return true;
  }

  /** Keep entry i in view; the first focusable entry brings the title back. */
  reveal(i, quiet = false) {
    const top = this.bodyTop() + this.ys[i], bottom = top + this.height(i);
    let y = this.ty;
    if (bottom + y > SAFE_BOTTOM) y = SAFE_BOTTOM - bottom;
    if (top + y < SAFE_TOP) y = SAFE_TOP - top;
    if (i === this.nearestFocusable(0)) y = 0;
    const before = this.ty;
    this.ty = y;
    this.clampTy();
    this.render();
    if (this.ty !== before && !quiet) this.measure();
  }

  /** Centre entry i at once, without the glide (the queue opens on the playing entry, like the Apple TV app). */
  centre(i) {
    if (i < 0) return;
    this.fi = i;
    this.ty = 540 - (this.bodyTop() + this.ys[i] + size().row / 2);
    this.page.style.transition = "none";
    this.clampTy();
    void this.page.offsetHeight;   // apply the jump before the transition comes back
    this.page.style.transition = "";
    this.render();
  }

  activate() {
    const e = this.focused();
    if (!e) return;
    const el = this.nodes.get(this.fi);
    if (el) { el.classList.add("pressed"); setTimeout(() => el.classList.remove("pressed"), 120); }
    e.act?.();
  }

  measure() {
    measureMove(this.opts.fps, "list", this.opts.name, this.opts.log, !!this.opts.videoPlaying?.());
  }
}

// ==================================================================== screens
/** Common frame of a list screen: title (58 bold) + count line, then the content. */
function frame(title) {
  const el = document.createElement("div");
  el.className = "l-screen";
  el.innerHTML = `<div class="l-page" data-page><header class="l-head"><h1 class="l-title">${esc(title)}</h1>
    <div class="l-count"></div></header><div class="l-content"></div></div>`;
  return el;
}

export class Screen {
  constructor(lists, kind, params, title) {
    this.lists = lists;
    this.ctx = lists.ctx;
    this.kind = kind;
    this.params = params;
    this.el = frame(title);
    this.$ = (s) => this.el.querySelector(s);
  }
  title(t) { this.$(".l-title").textContent = t; }
  countLine(t) { this.$(".l-count").textContent = t || ""; }
  /** play_collection with shuffle: true (plan 8.2), a log line, the player opens when the server's load arrives. */
  start(args, what) {
    const { sock, notice } = this.ctx;
    if (!sock.open) { notice("Not connected to the server", 3000); return; }
    sock.cmd("play_collection", { ...args, shuffle: true });
    sock.log(`list: ${what}`);
  }
  mount() {}
  shown() {}
  onMessage() {}
  setState() {}
  repaint() {}
  homeData() {}
  destroy() { this.el.remove(); }
}

// -------------------------------------------------------------------- row screens (VList)
export class RowScreen extends Screen {
  constructor(lists, kind, params, title) {
    super(lists, kind, params, title);
    const body = document.createElement("div");
    body.className = "l-rows";
    this.$(".l-content").appendChild(body);
    this.list = new VList(this.el, this.$(".l-page"), body, {
      name: kind, log: (t) => this.ctx.sock.log(t), videoPlaying: () => this.ctx.isVideoPlaying(), fps: lists.fps,
    });
  }
  key(k) {
    const dirs = { 37: "left", 38: "up", 39: "right", 40: "down" };
    if (dirs[k]) { this.list.move(dirs[k]); return true; }
    if (k === 13) { this.list.activate(); return true; }
    return false;
  }
  focusId() { const e = this.list.focused(); return e ? String(e.key) : null; }
  repaint() { this.list.repaint(); }
  error(e) {
    this.ctx.sock.log(`list: ${this.kind} ${e.message}`);
    this.list.setEntries([text("err", e.message)]);
  }
}

export const header = (key, label) => ({ key: `h:${key}`, type: "header", focusable: false, html: () => esc(label) });
export const text = (key, label) => ({ key: `t:${key}`, type: "text", focusable: false, html: () => esc(label) });

/** A media item row: title, detail, playing marker, ♥, duration. */
function itemRow(lists, key, it, sub, act, extra = {}) {
  return {
    key, type: "row", focusable: true, act,
    html: () => rowInner({ item: it, title: it.title, sub, badges: it.badges, current: lists.isCurrent(it.id), ...extra }),
  };
}

const artistLine = (it) => it.artist || "";

class AutoScreen extends RowScreen {
  constructor(lists, p) { super(lists, "auto", p, p.name); }
  async mount() {
    try {
      const items = (await getJSON(`/api/collection/${encodeURIComponent(this.params.key)}`)).items || [];
      this.countLine(count(items.length));
      const e = [];
      if (!items.length) e.push(text("empty", this.params.key === "loved" ? "Nothing loved yet." : "Nothing here yet."));
      else e.push(playRow("play", "Play", count(items.length), () => this.start({ collection: this.params.key }, `${this.params.key}: play`)));
      for (const it of items) {
        e.push(itemRow(this.lists, `i:${it.id}`, it, artistLine(it),
          () => this.start({ collection: this.params.key, start_id: it.id }, `${this.params.key}: start ${it.id}`)));
      }
      this.list.setEntries(e);
    } catch (err) { this.error(err); }
  }
  onMessage(m) { if (m.type === "auto" || m.type === "reconnect") this.mount(); }
}

class PlaylistScreen extends RowScreen {
  constructor(lists, p) { super(lists, "playlist", p, p.name); }
  async mount() {
    let d;
    try {
      const r = await fetch(`/api/playlists/${this.params.id}`, { cache: "no-store" });
      if (r.status === 404) {
        this.countLine("");
        this.list.setEntries([text("gone", "This playlist was deleted.")]);
        return;
      }
      if (!r.ok) throw new Error(`/api/playlists/${this.params.id}: HTTP ${r.status}`);
      d = await r.json();
    } catch (err) { this.error(err); return; }
    this.title(d.name || this.params.name);
    this.countLine(playlistDetail(d));
    const items = d.items || [];
    const playable = items.filter((i) => !i.missing).length;
    const e = [];
    if (playable) e.push(playRow("play", "Play", count(playable), () => this.start({ collection: "playlist", playlist: this.params.id }, `playlist ${this.params.id}: play`)));
    else e.push(text("empty", `Empty. Add songs and videos in the web remote: ${location.host}`));
    if (items.length) e.push(header("items", "Songs and videos"));
    items.forEach((it, index) => {   // by position: the same song can be in a playlist twice
      if (it.missing) {
        e.push({ key: `p:${index}`, type: "row", focusable: false,
                 html: () => rowInner({ symbol: "warning", title: it.title, sub: "file was deleted or moved", right: "" }) });
      } else {
        e.push(itemRow(this.lists, `p:${index}`, it, artistLine(it),
          () => this.start({ collection: "playlist", playlist: this.params.id, start_index: index }, `playlist ${this.params.id}: start at ${index}`)));
      }
    });
    this.list.setEntries(e);
    this.ctx.sock.log(`playlist ${this.params.id} shown: ${items.length} entries`);
  }
  onMessage(m) { if (m.type === "playlists" || m.type === "reconnect") this.mount(); }
}

function playRow(key, label, detail, act, symbol = "play") {
  return { key, type: "row", focusable: true, cls: "play", act,
           html: () => rowInner({ symbol, title: label, sub: detail, right: "" }) };
}

class RootsScreen extends RowScreen {
  constructor(lists) { super(lists, "folders", {}, "Folders"); }
  async mount() {
    try {
      const roots = (await getJSON("/api/browse")).roots || [];
      this.countLine(count(roots.reduce((n, r) => n + (r.count || 0), 0)));
      this.list.setEntries(roots.map((r) => ({
        key: `r:${r.root}`, type: "row", focusable: true,
        html: () => rowInner({ symbol: "drive", title: r.name, sub: count(r.count), right: "" }),
        act: () => this.lists.open("folder", { root: r.root, folder: "", name: r.name }),
      })));
    } catch (err) { this.error(err); }
  }
  onMessage(m) { if (m.type === "reconnect") this.mount(); }
}

class FolderScreen extends RowScreen {
  constructor(lists, p) { super(lists, "folder", p, p.name); }
  async mount() {
    const { root, folder } = this.params;
    let r;
    try {
      r = await getJSON(`/api/browse?root=${encodeURIComponent(root)}&folder=${encodeURIComponent(folder)}`);
    } catch (err) { this.error(err); return; }
    const subs = r.subfolders || [], items = r.items || [];
    this.countLine(count(r.total || 0));
    const play = (recursive, startId) => {
      const args = { collection: "folder", root, folder, recursive };
      if (startId !== undefined) args.start_id = startId;
      this.start(args, `folder ${root}/${folder || "-"}${recursive ? "" : " only"}${startId !== undefined ? ` start ${startId}` : ""}`);
    };
    const e = [];
    if (subs.length) {
      e.push(playRow("play", "Play (including subfolders)", count(r.total || 0), () => play(true)));
      if (items.length) e.push(playRow("play-only", "Play (only this folder)", count(items.length), () => play(false), "playOutline"));
    } else {
      e.push(playRow("play", "Play", count(r.total || 0), () => play(true)));
    }
    if (subs.length) {
      e.push(header("folders", "Folders"));
      for (const s of subs) {
        e.push({ key: `f:${s.folder}`, type: "row", focusable: true,
                 html: () => rowInner({ symbol: "folder", title: s.name, sub: count(s.count), right: "" }),
                 act: () => this.lists.open("folder", { root, folder: s.folder, name: s.name }) });
      }
    }
    if (items.length) {
      e.push(header("files", "Files"));
      for (const it of items) {
        e.push(itemRow(this.lists, `i:${it.id}`, it, artistLine(it),
          () => play(false, it.id)));
      }
    }
    this.list.setEntries(e);
  }
  onMessage(m) { if (m.type === "reconnect") this.mount(); }
}

/** The play queue (QueueView.swift): numbered, the playing entry marked; OK = jump (the server starts it and opens the
 *  player). Opens centred on the playing entry; later track changes do not move the focus. */
class QueueScreen extends RowScreen {
  constructor(lists, p = {}) {   // p.back = "player": opened from the quick menu, Back returns to the player
    super(lists, "queue", p, "Queue");
    this.byId = new Map();     // titles from /api/library?all=1 (loaded when the queue holds an unknown id)
    this.loading = false;
    this.lastQueue = "";
    this.didFocus = false;
  }
  mount() { this.build(this.ctx.state()); }
  setState(st) {
    const q = JSON.stringify([st?.queue || [], st?.index ?? -1, st?.current?.id ?? null]);
    if (q === this.lastQueue) return;
    this.build(st);
  }
  onMessage(m) { if (m.type === "reconnect") { this.lastQueue = ""; this.build(this.ctx.state()); } }

  title_of(id, st) {
    if (st?.current?.id === id) return st.current;
    const up = (st?.upcoming || []).find((u) => u.id === id);
    return up || this.byId.get(id) || null;
  }

  async ensureTitles(queue, st) {
    if (this.loading || !queue.some((id) => !this.title_of(id, st))) return;
    this.loading = true;
    try {
      const items = (await getJSON("/api/library?all=1")).items || [];
      this.byId = new Map(items.map((i) => [i.id, i]));
    } catch (err) {
      this.ctx.sock.log(`queue: titles ${err.message}`);
    }
    this.loading = false;
    this.list.repaint();
  }

  build(st) {
    const queue = st?.queue || [], cur = st?.index ?? -1;
    this.lastQueue = JSON.stringify([queue, cur, st?.current?.id ?? null]);
    this.title(queue.length ? `Queue · ${queue.length}` : "Queue");
    const e = queue.length ? [] : [text("empty", "The queue is empty. Start something from Playlists, Folders or Music videos.")];
    queue.forEach((id, index) => {
      e.push({
        key: `q:${index}`, type: "row", focusable: true, cls: index === cur ? "now" : "",
        html: () => {
          const it = this.title_of(id, this.ctx.state());
          const playing = index === (this.ctx.state()?.index ?? -1);
          return rowInner({ num: index + 1, item: it, symbol: "note", title: it ? it.title : `Item #${id}`,
                            sub: [it?.artist, playing ? "Now playing" : null].filter(Boolean).join(" · "),
                            current: playing, badges: [], right: it?.duration ? undefined : "" });
        },
        act: () => {
          const { sock, notice } = this.ctx;
          if (!sock.open) { notice("Not connected to the server", 3000); return; }
          sock.cmd("jump", { index });
          sock.log(`queue: jump to ${index + 1} of ${queue.length} (item ${id})`);
        },
      });
    });
    this.list.setEntries(e);
    if (!this.didFocus && cur >= 0 && cur < queue.length) {
      this.didFocus = true;
      this.list.centre(cur);
    }
    this.ensureTitles(queue, st);
  }
}

// -------------------------------------------------------------------- grid screens (focus engine of Home)
class GridScreen extends Screen {
  constructor(lists, kind, params, title) {
    super(lists, kind, params, title);
    this.focus = new Focus(this.el, {
      onActivate: (el) => this.activate(el), log: (t) => this.ctx.sock.log(t),
      videoPlaying: () => this.ctx.isVideoPlaying(), fps: lists.fps,
    });
  }
  key(k) {
    const dirs = { 37: "left", 38: "up", 39: "right", 40: "down" };
    if (dirs[k]) { this.focus.move(dirs[k]); return true; }
    if (k === 13) { this.focus.activate(); return true; }
    return false;
  }
  focusId() { return this.focus.el?.dataset.focus || null; }
  fill(html) {
    this.$(".l-content").innerHTML = html;
    this.focus.refresh();
  }
  shown() { this.focus.refresh(); }
}

/** Playlists (PlaylistsView.swift): For you (auto-lists) and the playlists as 280 cards in a grid. */
class PlaylistsScreen extends GridScreen {
  constructor(lists) { super(lists, "playlists", {}, "Playlists"); }
  mount() { this.homeData(); }
  homeData() {
    const d = this.ctx.homeData();
    this.countLine(plural(d.playlists.length, "playlist"));
    const out = [];
    if (d.auto.length) {
      out.push(`<section class="l-sec" data-section="foryou" data-grid><h2 class="l-h">For you</h2><div class="grid">${
        d.auto.map((l, i) => card(`auto:${l.key}`, "auto", l.key, l.key === "loved" ? `♥ ${l.name}` : l.name,
          plural(l.count, "item"), d.autoItems[l.key] || [], 1000000 + i)).join("")}</div></section>`);
    }
    out.push(`<section class="l-sec" data-section="playlists" data-grid>${d.auto.length ? '<h2 class="l-h">Playlists</h2>' : ""}${
      d.playlists.length ? `<div class="grid">${d.playlists.map((p) => card(`playlist:${p.id}`, "playlist", p.id, p.name,
        playlistDetail(p), d.covers[p.id] || [], p.id)).join("")}</div>`
        : `<div class="l-text">No playlists yet. Create them in the web remote: ${esc(location.host)} → Playlists.</div>`}</section>`);
    this.fill(out.join(""));
  }
  activate(el) {
    const d = this.ctx.homeData(), key = el.dataset.key;
    if (el.dataset.action === "auto") {
      const l = d.auto.find((x) => x.key === key);
      this.lists.open("auto", { key, name: l ? l.name : key });
    } else if (el.dataset.action === "playlist") {
      const p = d.playlists.find((x) => String(x.id) === key);
      this.lists.open("playlist", { id: Number(key), name: p ? p.name : "Playlist" });
    }
  }
}

/** Music videos: Play, then a 4-column poster grid of 410 x 231 (plan 10.5). */
class VideosScreen extends GridScreen {
  constructor(lists) { super(lists, "videos", {}, "Music videos"); }
  mount() { this.homeData(); }
  homeData() {
    const v = this.ctx.homeData().videos;
    this.countLine(count(v.length));
    this.fill(`<section class="l-sec" data-section="play"><div class="row play" data-focus="play" data-action="play">${
      rowInner({ symbol: "play", title: "Play", sub: count(v.length), right: "" })}</div></section>
      <section class="l-sec" data-section="grid" data-grid><div class="grid g4">${v.map((x) => poster(x, "grid")).join("")}</div></section>`);
  }
  activate(el) {
    if (el.dataset.action === "play") this.start({ collection: "videos" }, "music videos: play");
    else if (el.dataset.action === "video") this.start({ collection: "videos", start_id: Number(el.dataset.key) }, `music videos: start ${el.dataset.key}`);
  }
}

export const KINDS = {
  playlists: (l) => new PlaylistsScreen(l), videos: (l) => new VideosScreen(l), folders: (l) => new RootsScreen(l),
  queue: (l, p) => new QueueScreen(l, p), auto: (l, p) => new AutoScreen(l, p), playlist: (l, p) => new PlaylistScreen(l, p),
  folder: (l, p) => new FolderScreen(l, p),
};

// ==================================================================== the stack
export class Lists {
  /** ctx: {engine, sock, notice, state() -> session state, homeData() -> Home's data, isVideoPlaying()} */
  constructor(root, ctx) {
    this.root = root;
    this.ctx = ctx;
    this.stack = [];
    this.fps = { left: 20, measuring: false };   // FPS lines of the list screens per app start
    root.innerHTML = `<div class="l-bd"></div>`;
    this.backdrop = new Backdrop(root.querySelector(".l-bd"), 0.9);
    this.backdrop.still(true);
    root.addEventListener("error", (e) => { if (e.target.tagName === "IMG") e.target.classList.add("broken"); }, true);
  }

  top() { return this.stack[this.stack.length - 1] || null; }

  /** Push a screen and load it. */
  open(kind, params = {}) {
    const make = KINDS[kind];
    if (!make) return;
    const s = make(this, params);
    this.top()?.el.classList.add("hidden");
    this.stack.push(s);
    this.root.appendChild(s.el);
    this.ctx.sock.log(`list: open ${kind}${params.key ? ` ${params.key}` : ""}${params.id !== undefined ? ` ${params.id}` : ""}` +
                      `${params.root ? ` ${params.root}/${params.folder || "-"}` : ""}`);
    s.mount();
  }

  /** Back: pop; false when the stack is empty (the app goes Home). */
  back() {
    const s = this.stack.pop();
    if (s) s.destroy();
    const t = this.top();
    if (!t) return false;
    t.el.classList.remove("hidden");
    t.shown();
    return true;
  }

  clear() { while (this.stack.length) this.stack.pop().destroy(); }

  key(k) { return this.top()?.key(k) || false; }
  isCurrent(id) { return this.ctx.engine.item?.id === id; }
  setState(st) { for (const s of this.stack) s.setState(st); }
  /** A setting changed: the text size re-lays every list; the top screen redraws its values. */
  settingsChanged(k) {
    if (k === "textSize") for (const s of this.stack) s.list?.setEntries(s.list.entries);
    this.top()?.refresh?.();
  }
  setItem() { this.top()?.repaint(); }
  playing(on) { this.root.classList.toggle("playing", on); }
  onMessage(m) { for (const s of this.stack) s.onMessage(m); }
  homeData() { for (const s of this.stack) s.homeData(); }
  shown(on) { this.backdrop.still(!on); if (on) this.top()?.shown(); }
}
