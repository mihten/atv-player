// LTVPlayer: the plain player screen of phase P3 (title, artist, badges, progress, "Next"). Video: the picture full
// screen with the info overlay at the bottom; song: its cover in the middle. The overlay appears on every key and
// pointer move, hides 5 s later while playing and stays while paused. The full Aurora screens come in P4-P6.

const HIDE_AFTER = 5000;

function clock(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

export class PlayerView {
  constructor(root, engine) {
    this.root = root;
    this.engine = engine;
    this.state = null;
    this.preview = null;   // a seek target from ◀ / ▶ shown on the bar until the element is there
    this.hideTimer = null;
    root.innerHTML = `
      <div class="p-cover"><img alt=""></div>
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
      </div>`;
    this.$ = (sel) => root.querySelector(sel);
    for (const img of root.querySelectorAll("img")) {
      img.addEventListener("error", () => { img.style.visibility = "hidden"; });
      img.addEventListener("load", () => { img.style.visibility = ""; });
    }
  }

  setItem(it) {
    const song = it && it.kind === "audio";
    this.root.classList.toggle("song", !!song);
    this.$(".p-title").textContent = it ? it.title || "" : "";
    this.$(".p-artist").textContent = it ? [it.artist, song ? it.album : null].filter(Boolean).join(" · ") : "";
    this.$(".p-badges").innerHTML = "";
    for (const b of (it && it.badges) || []) {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = b;
      this.$(".p-badges").appendChild(chip);
    }
    const art = it && it.art ? `/art/${it.id}` : "";
    for (const img of [this.$(".p-thumb"), this.$(".p-cover img")]) {
      img.style.visibility = art ? "" : "hidden";
      if (art) img.src = art; else img.removeAttribute("src");
    }
    this.preview = null;
    this.update();
    this.show();
  }

  setState(st) {
    this.state = st;
    this.update();
  }

  update() {
    const e = this.engine;
    const d = e.duration();
    const t = this.preview !== null ? this.preview : e.position();
    const f = d ? Math.min(1, Math.max(0, t / d)) : 0;
    this.$(".p-fill").style.transform = `scaleX(${f})`;
    this.$(".p-knob").style.transform = `translateX(${f * 1760}px)`;
    this.$(".p-el").textContent = e.item ? clock(t) : "";
    this.$(".p-rem").textContent = e.item && d ? "-" + clock(d - t) : "";
    const up = this.state && this.state.upcoming && this.state.upcoming[0];
    this.$(".p-next").textContent = up ? `Next: ${up.title}` : "";
    const paused = !!e.item && !e.isPlaying();
    this.$(".p-state").textContent = !e.item ? "" : paused ? "❚❚" : "▶";
    this.root.classList.toggle("paused", paused);
    if (paused) this.show(true);
    else if (this.wasPaused) this.show();   // playing again: the overlay goes HIDE_AFTER later
    this.wasPaused = paused;
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
}

export { clock };
