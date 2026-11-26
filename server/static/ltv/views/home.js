// LTVPlayer: Home (port of Sources/HomeView.swift, Aurora look; plan 10.4). Header with the wordmark and the
// connection pill, the Now Playing hero, then the shelves Browse, For you, Playlists, Play, Music videos.
// Every action is a command to the server (plan 8.2: list starts are shuffled, a picked item plays first).
// Browse / For you / Playlists open the list screens (views/lists.js, P5).

import { Focus } from "../focus.js";
import { Backdrop } from "../backdrop.js";
import { icon } from "../icons.js";
import { getJSON, formatTime, plural, playlistDetail, mixDetail, mixEmpty, esc } from "../api.js";
import { thumb, card, poster, bars } from "./tiles.js";

const W = 1760;

export class HomeView {
  /** ctx: {engine, sock, notice(text, ms), showPlayer(), version, isVideoPlaying()} */
  constructor(root, ctx) {
    this.root = root;
    this.ctx = ctx;
    this.state = null;
    this.item = null;
    this.data = { auto: [], autoItems: {}, playlists: [], covers: {}, videos: [] };
    root.innerHTML = `
      <div class="h-bd"></div>
      <div class="h-page" data-page>
        <header class="h-head">
          <div class="h-word">LTVPlayer</div>
          <div class="h-pill"><span class="h-dot"></span><span class="h-conn">Connecting…</span></div>
        </header>
        <section class="h-sec" data-section="hero"><div class="hero" data-focus="hero" data-action="hero"></div></section>
        <div class="h-shelves"></div>
        <div class="h-version">LTVPlayer ${esc(ctx.version)}</div>
      </div>`;
    this.$ = (s) => root.querySelector(s);
    this.backdrop = new Backdrop(this.$(".h-bd"), 0.9);
    this.focus = new Focus(root, {
      onActivate: (el) => this.activate(el),
      log: (t) => ctx.sock.log(t),
      videoPlaying: () => ctx.isVideoPlaying(),
    });
    root.addEventListener("error", (e) => {   // a cover that fails: the placeholder look
      if (e.target.tagName === "IMG") e.target.classList.add("broken");
    }, true);
    this.renderHero();
    this.renderShelves();
  }

  // ---------------------------------------------------------------- data (plan 10.4 "Reload")
  async loadAll() {
    await Promise.all([this.loadAuto(), this.loadPlaylists(), this.loadVideos()]);
  }

  async loadVideos() {
    try { this.data.videos = (await getJSON("/api/collection/videos")).items || []; } catch (e) { this.ctx.sock.log(`home: videos ${e.message}`); }
    this.renderShelves();
  }

  async loadAuto() {
    try {
      const lists = (await getJSON("/api/auto")).filter((l) => l.count > 0);
      const items = {};
      await Promise.all(lists.map(async (l) => {
        try { items[l.key] = (await getJSON(`/api/collection/${encodeURIComponent(l.key)}`)).items || []; } catch (e) { items[l.key] = []; }
      }));
      this.data.auto = lists;
      this.data.autoItems = items;
    } catch (e) { this.ctx.sock.log(`home: auto ${e.message}`); }
    this.renderShelves();
  }

  async loadPlaylists() {
    try {
      const list = (await getJSON("/api/playlists")).playlists || [];
      const covers = {};
      await Promise.all(list.slice(0, 30).map(async (p) => {
        try {
          const d = await getJSON(`/api/playlists/${p.id}`);
          covers[p.id] = (d.items || []).filter((i) => !i.missing).slice(0, 4);
        } catch (e) { covers[p.id] = []; }
      }));
      this.data.playlists = list;
      this.data.covers = covers;
    } catch (e) { this.ctx.sock.log(`home: playlists ${e.message}`); }
    this.renderShelves();
  }

  /** Server messages: playlists / auto changed. */
  onMessage(m) {
    if (m.type === "playlists") this.loadPlaylists();
    else if (m.type === "auto") this.loadAuto();
  }

  setState(st) {
    const scanned = this.state && this.state.scanning && !st.scanning;
    const mixesBefore = JSON.stringify(this.state?.mixes ?? null);
    const upBefore = JSON.stringify((this.state?.upcoming || []).slice(0, 3).map((i) => i.id));
    this.state = st;
    if (scanned) this.loadVideos();
    if (JSON.stringify(st.mixes ?? null) !== mixesBefore) this.renderShelves();
    if (JSON.stringify((st.upcoming || []).slice(0, 3).map((i) => i.id)) !== upBefore) this.renderHero();
  }

  setConnected(on) {
    this.root.classList.toggle("online", on);
    this.$(".h-conn").textContent = on ? "Connected" : "Connecting…";
  }

  setItem(it) {
    if ((it && it.id) === (this.item && this.item.id)) { this.item = it; this.update(); return; }
    this.item = it;
    this.renderHero();
  }

  // ---------------------------------------------------------------- hero
  renderHero() {
    const el = this.$(".hero"), it = this.item;
    el.classList.toggle("idle", !it);
    el.classList.toggle("song", !!it && it.kind !== "video");
    if (!it) {
      el.innerHTML = `<div class="hero-shuffle">${icon("shuffle", 90)}</div>
        <div class="hero-idle"><div class="hero-etitle">Nothing playing yet</div>
        <div class="hero-esub">Select to shuffle music videos</div></div>`;
    } else {
      const sub = [it.artist, it.kind === "video" ? null : it.album].filter(Boolean).join(" · ");
      const up = (this.state?.upcoming || []).slice(0, 3);
      el.innerHTML = `
        <div class="hero-art">${it.art ? `<img src="/art/${it.id}?s=640" decoding="async" alt="">` : thumb(it)}
          ${it.kind === "video" ? `<span class="hero-badge">${icon("videos", 34)}</span>` : ""}</div>
        <div class="hero-text">
          <div class="hero-eyebrow">${bars()}<span class="hero-state"></span></div>
          <div class="hero-title">${esc(it.title)}</div>
          ${sub ? `<div class="hero-artist">${esc(sub)}</div>` : ""}
          <div class="hero-prog"><div class="hero-fill"></div></div>
          <div class="hero-times"><span class="hero-el"></span><span class="hero-dur"></span></div>
        </div>
        ${up.length ? `<div class="hero-next"><div class="hero-nlabel">UP NEXT</div>${up.map((n) => `
          <div class="hero-nrow">${thumb(n, n.kind === "video" ? "wide" : "sq")}
            <div class="hero-ntext"><div class="hero-ntitle">${esc(n.title)}</div>
            <div class="hero-nartist">${esc(n.artist || "")}</div></div></div>`).join("")}</div>` : ""}`;
    }
    this.focus.refresh();
    this.update();
  }

  /** Once a second and on every play / pause: eyebrow, bars, progress. */
  update() {
    const e = this.ctx.engine, el = this.$(".hero");
    if (!this.item || !el) return;
    const playing = e.isPlaying();
    el.classList.toggle("playing", playing);
    const st = el.querySelector(".hero-state");
    if (st) st.textContent = playing ? "NOW PLAYING" : e.stopped ? "STOPPED" : "PAUSED";
    const d = e.duration(), t = e.position();
    const fill = el.querySelector(".hero-fill");
    if (fill) fill.style.transform = `scaleX(${d ? Math.min(1, t / d) : 0})`;
    const a = el.querySelector(".hero-el"), b = el.querySelector(".hero-dur");
    if (a) a.textContent = formatTime(t);
    if (b) b.textContent = formatTime(d);
  }

  // ---------------------------------------------------------------- shelves
  shelf(id, title, inner, scroller = true) {
    const chev = scroller ? `<div class="chev chev-l" data-focus="${id}:chev-l" data-ptr="left">${icon("chevronLeft", 44)}</div>
      <div class="chev chev-r" data-focus="${id}:chev-r" data-ptr="right">${icon("chevronRight", 44)}</div>` : "";
    return `<section class="shelf" data-section="${id}"><h2 class="shelf-title">${esc(title)}</h2>
      <div class="track-wrap"><div class="track" data-track>${inner}</div>${chev}</div></section>`;
  }

  renderShelves() {
    const d = this.data, out = [];
    const browse = [["playlists", "Playlists", "playlists"], ["folders", "Folders", "folder"],
                    ["videos", "Music videos", "videos"], ["queue", "Queue", "queue"], ["settings", "Settings", "gear"]];
    out.push(this.shelf("browse", "Browse", browse.map(([k, t, ic]) => `
      <div class="tile browse" data-focus="browse:${k}" data-action="browse" data-key="${k}">${icon(ic, 40)}<span>${t}</span></div>`).join(""), false));
    if (d.auto.length) {
      out.push(this.shelf("foryou", "For you", d.auto.map((l, i) => card(`auto:${l.key}`, "auto", l.key,
        l.key === "loved" ? `♥ ${l.name}` : l.name, plural(l.count, "item"), d.autoItems[l.key] || [], 1000000 + i)).join("")));
    }
    if (d.playlists.length) {
      out.push(this.shelf("playlists", "Playlists", d.playlists.slice(0, 30).map((p) => card(`playlist:${p.id}`, "playlist", p.id,
        p.name, playlistDetail(p), d.covers[p.id] || [], p.id)).join("")));
    }
    out.push(this.shelf("play", "Play", this.playTiles(), false));
    if (d.videos.length) {
      out.push(this.shelf("videos", "Music videos", d.videos.slice(0, 40).map((v) => poster(v)).join("")));
    }
    this.$(".h-shelves").innerHTML = out.join("");
    this.focus.tx.clear();
    for (const t of this.root.querySelectorAll("[data-track]")) this.focus.setX(t, 0);
    this.focus.refresh();
    this.ctx.onData?.();
  }

  /** Mix tiles that hold something (an empty mix shows no tile) + "Shuffle music videos" over the rest of the width. */
  playTiles() {
    const mixes = (this.state?.mixes || []).map((m, i) => ({ ...m, n: i + 1 })).filter((m) => !mixEmpty(m)).slice(0, 2);
    const small = (W - 80) / 4;
    const big = W - mixes.length * (small + 40);
    const tile = (focusId, action, key, title, sub, ic, variant, width) => `
      <div class="tile action v${variant}" data-focus="${focusId}" data-action="${action}" data-key="${key}" style="width:${width}px">
        <div class="sheen"></div>${icon(ic, 120, "big")}<div class="cap"><div class="act-t">${esc(title)}</div>
        <div class="act-s">${esc(sub)}</div></div></div>`;
    return mixes.map((m) => tile(`mix:${m.n}`, "mix", m.n, m.name || `Mix ${m.n}`, mixDetail(m), "shuffle", m.n, small)).join("") +
      tile("shuffle-videos", "shuffle", "videos", "Shuffle music videos", "All music videos", "film", 0, big);
  }

  // ---------------------------------------------------------------- keys + actions
  /** Arrow / OK keys of Home; false = not handled here. */
  key(k) {
    const dirs = { 37: "left", 38: "up", 39: "right", 40: "down" };
    if (dirs[k]) { this.focus.move(dirs[k]); return true; }
    if (k === 13) { this.focus.activate(); return true; }
    return false;
  }

  activate(el) {
    const { sock, notice, showPlayer, engine, openList } = this.ctx;
    const a = el.dataset.action, key = el.dataset.key;
    const send = (args, what) => {
      if (!sock.open) { notice("Not connected to the server", 3000); return; }
      sock.cmd("play_collection", { ...args, shuffle: true });
      sock.log(`home: ${what}`);
    };
    if (a === "hero") {
      if (engine.item && engine.stopped) {   // after a Stop: Play starts the item again from 0:00 (the server loads it)
        if (!sock.open) { notice("Not connected to the server", 3000); return; }
        sock.cmd("play");
        sock.log("home: play after stop (hero)");
        showPlayer();
      } else if (engine.item) showPlayer();
      else send({ collection: "videos" }, "shuffle music videos (hero)");
    } else if (a === "shuffle") {
      send({ collection: "videos" }, "shuffle music videos");
    } else if (a === "mix") {
      send({ collection: "mix", mix: Number(key) }, `mix ${key}`);
    } else if (a === "video") {
      send({ collection: "videos", start_id: Number(key) }, `music video ${key}`);
    } else if (a === "browse") {
      openList(key);   // playlists | folders | videos | queue | settings
    } else if (a === "auto") {
      const l = this.data.auto.find((x) => x.key === key);
      openList("auto", { key, name: l ? l.name : key });
    } else if (a === "playlist") {
      const p = this.data.playlists.find((x) => String(x.id) === key);
      openList("playlist", { id: Number(key), name: p ? p.name : "Playlist" });
    }
  }

  shown(on) {
    this.backdrop.still(!on);
    if (on) this.focus.refresh();
  }
}
