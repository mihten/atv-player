// LTVPlayer: the Home of phase P3 — the wordmark, the connection pill and the Now Playing card (port of the empty /
// playing hero of Sources/HomeView.swift). OK on the card opens the player, or shuffles all music videos when nothing
// is loaded. The shelves, tiles and the Aurora backdrop replace this file in P4.

import { clock } from "./player.js";

export class HomeView {
  constructor(root, engine, version) {
    this.root = root;
    this.engine = engine;
    this.connected = false;
    root.innerHTML = `
      <header class="h-head">
        <div class="h-word">LTVPlayer</div>
        <div class="h-pill"><span class="h-dot"></span><span class="h-conn">Connecting…</span></div>
      </header>
      <div class="h-card focused" data-focus="hero">
        <div class="h-empty">
          <div class="h-shuffle">⤮</div>
          <div class="h-etitle">Nothing playing yet</div>
          <div class="h-esub">Select to shuffle music videos</div>
        </div>
        <div class="h-now">
          <img class="h-art" alt="">
          <div class="h-text">
            <div class="h-eyebrow"></div>
            <div class="h-title"></div>
            <div class="h-artist"></div>
            <div class="h-prog"><div class="h-fill"></div></div>
            <div class="h-times"><span class="h-el"></span><span class="h-dur"></span></div>
          </div>
        </div>
      </div>
      <div class="h-version">LTVPlayer ${version}</div>`;
    this.$ = (sel) => root.querySelector(sel);
    const img = this.$(".h-art");
    img.addEventListener("error", () => { img.style.visibility = "hidden"; });
    img.addEventListener("load", () => { img.style.visibility = ""; });
  }

  setConnected(on) {
    this.connected = on;
    this.root.classList.toggle("online", on);
    this.$(".h-conn").textContent = on ? "Connected" : "Connecting…";
  }

  setItem(it) {
    this.root.classList.toggle("has-item", !!it);
    this.root.classList.toggle("song", !!it && it.kind === "audio");
    this.$(".h-title").textContent = it ? it.title || "" : "";
    this.$(".h-artist").textContent = it ? it.artist || "" : "";
    const img = this.$(".h-art");
    if (it && it.art) { img.style.visibility = ""; img.src = `/art/${it.id}`; } else { img.style.visibility = "hidden"; img.removeAttribute("src"); }
    this.update();
  }

  update() {
    const e = this.engine;
    if (!e.item) return;
    this.$(".h-eyebrow").textContent = e.isPlaying() ? "NOW PLAYING" : "PAUSED";
    const d = e.duration(), t = e.position();
    this.$(".h-fill").style.transform = `scaleX(${d ? Math.min(1, t / d) : 0})`;
    this.$(".h-el").textContent = clock(t);
    this.$(".h-dur").textContent = d ? clock(d) : "";
  }
}
