// LTVPlayer: focus engine for one screen (plan 10.3). Focusables = [data-focus] inside the screen root; exactly one has
// the class "focused" (no browser focus, no :hover). Sections = [data-section]; inside a section Left / Right move by
// geometry and never leave it; Up / Down go to the neighbouring section and land on its remembered tile, else the one
// nearest in x. No wrap. The focused tile is kept in view by moving its shelf track (translateX) and the page
// (translateY) — transforms only, no native scrolling. Pointer: mousemove = pointer mode (the tile under the pointer is
// focused, shelf chevrons appear), the TV's pointer click arrives as OK; a wheel turn scrolls the shelf under the
// pointer, elsewhere the page; any arrow key leaves pointer mode. Covers ([data-src]) load within one screen of the view.

const SAFE_TOP = 40, SAFE_BOTTOM = 1080 - 50, VIEW_W = 1760;
const FPS_LOGS = 20;   // shelf / page moves measured per app start (P4 exit: FPS numbers from the TV)

export class Focus {
  /** opts: {onActivate(el), log(text), videoPlaying() -> bool} */
  constructor(root, opts) {
    this.root = root;
    this.opts = opts;
    this.el = null;
    this.memory = new Map();   // section id -> focus id
    this.tx = new Map();       // track -> translateX
    this.ty = 0;
    this.pointer = false;
    this.fpsLeft = FPS_LOGS;
    this.lastOk = 0;
    this.moveRaf = 0;
    root.addEventListener("mousemove", (e) => this.onMouse(e));
    root.addEventListener("click", (e) => this.onClick(e));
    root.addEventListener("wheel", (e) => this.onWheel(e), { passive: true });
    document.addEventListener("cursorStateChange", (e) => {
      if (e.detail && e.detail.visibility === false) this.setPointer(false);
    });
  }

  page() { return this.root.querySelector("[data-page]"); }

  focusables(section = null) {
    const all = [...this.root.querySelectorAll("[data-focus]")].filter((el) => !el.hasAttribute("data-ptr"));
    return section ? all.filter((el) => el.closest("[data-section]") === section) : all;
  }

  sections() {
    return [...this.root.querySelectorAll("[data-section]")].filter((s) => this.focusables(s).length);
  }

  /** After a re-render: keep the focus by its id, else the remembered tile of its section, else the first tile. */
  refresh() {
    const id = this.el?.dataset.focus;
    const sec = this.el?.closest("[data-section]")?.dataset.section;
    let next = id ? this.root.querySelector(`[data-focus="${CSS.escape(id)}"]`) : null;
    if (!next && sec) {
      const s = this.root.querySelector(`[data-section="${CSS.escape(sec)}"]`);
      if (s) next = this.focusables(s)[0];
    }
    next = next || this.focusables()[0] || null;
    for (const t of this.root.querySelectorAll("[data-track]")) {
      if (!this.tx.has(t)) this.tx.set(t, 0);
    }
    this.set(next, { scroll: !this.pointer, quiet: true });
    this.lazy();
  }

  set(el, { scroll = true, quiet = false } = {}) {
    if (this.el && this.el !== el) this.el.classList.remove("focused");
    this.el = el;
    if (!el) return;
    el.classList.add("focused");
    const sec = el.closest("[data-section]");
    if (sec && !el.hasAttribute("data-ptr")) this.memory.set(sec.dataset.section, el.dataset.focus);
    if (scroll) this.reveal(el, quiet);
  }

  // ---------------------------------------------------------------- keys
  move(dir) {
    if (this.pointer) this.setPointer(false);
    const cur = this.el && this.el.isConnected ? this.el : null;
    if (!cur) { this.set(this.focusables()[0] || null); return; }
    const sec = cur.closest("[data-section]");
    let next = null;
    if (dir === "left" || dir === "right") {
      next = this.nearest(cur, this.focusables(sec), dir);
    } else {
      next = this.nearest(cur, this.focusables(sec), dir);   // rows inside the section (grids) first
      if (!next) {
        const list = this.sections(), i = list.indexOf(sec);
        const j = dir === "up" ? i - 1 : i + 1;
        if (i >= 0 && j >= 0 && j < list.length) {
          const target = list[j];
          const remembered = this.memory.get(target.dataset.section);
          next = remembered ? target.querySelector(`[data-focus="${CSS.escape(remembered)}"]`) : null;
          if (!next) next = this.nearestX(cur, this.focusables(target));
        }
      }
    }
    if (next) this.set(next);
    return !!next;
  }

  nearest(cur, list, dir) {
    const a = cur.getBoundingClientRect();
    const ac = { x: a.left + a.width / 2, y: a.top + a.height / 2 };
    let best = null, bestScore = Infinity;
    for (const el of list) {
      if (el === cur) continue;
      const b = el.getBoundingClientRect();
      const bc = { x: b.left + b.width / 2, y: b.top + b.height / 2 };
      let primary, cross;
      if (dir === "left") { if (bc.x >= ac.x - 1) continue; primary = Math.max(0, a.left - b.right); cross = Math.abs(bc.y - ac.y); }
      else if (dir === "right") { if (bc.x <= ac.x + 1) continue; primary = Math.max(0, b.left - a.right); cross = Math.abs(bc.y - ac.y); }
      else if (dir === "up") { if (bc.y >= ac.y - 1) continue; primary = Math.max(0, a.top - b.bottom); cross = Math.abs(bc.x - ac.x); }
      else { if (bc.y <= ac.y + 1) continue; primary = Math.max(0, b.top - a.bottom); cross = Math.abs(bc.x - ac.x); }
      const score = primary + 2 * cross;
      if (score < bestScore) { bestScore = score; best = el; }
    }
    return best;
  }

  nearestX(cur, list) {
    const a = cur.getBoundingClientRect(), ax = a.left + a.width / 2;
    let best = null, bestD = Infinity;
    for (const el of list) {
      const b = el.getBoundingClientRect(), d = Math.abs(b.left + b.width / 2 - ax);
      if (d < bestD) { bestD = d; best = el; }
    }
    return best;
  }

  activate() {
    const el = this.el;
    if (!el || !el.isConnected) return;
    this.lastOk = performance.now();
    el.classList.add("pressed");
    setTimeout(() => el.classList.remove("pressed"), 120);
    if (el.hasAttribute("data-ptr")) { this.page_track(el); return; }
    this.opts.onActivate(el);
  }

  // ---------------------------------------------------------------- keep in view (transforms only)
  reveal(el, quiet = false) {
    let moved = null;
    const track = el.closest("[data-track]");
    if (track) {
      let x = this.tx.get(track) || 0;
      const left = el.offsetLeft, right = left + el.offsetWidth;
      if (right + x > VIEW_W) x = VIEW_W - right;
      if (left + x < 0) x = -left;
      x = this.clampX(track, x);
      if (x !== (this.tx.get(track) || 0)) { this.setX(track, x); moved = "shelf"; }
    }
    const sec = el.closest("[data-section]"), page = this.page();
    if (sec && page) {
      let y = this.ty;
      const top = sec.offsetTop, bottom = top + sec.offsetHeight;
      if (bottom + y > SAFE_BOTTOM) y = SAFE_BOTTOM - bottom;
      if (top + y < SAFE_TOP) y = SAFE_TOP - top;
      if (this.sections()[0] === sec) y = 0;   // the first section brings the header back
      y = Math.min(0, y);
      if (y !== this.ty) { this.setY(y); moved = moved ? "both" : "page"; }
    }
    if (moved && !quiet) this.measure(moved, sec?.dataset.section || "-");
    if (moved) setTimeout(() => this.lazy(), 360);
  }

  clampX(track, x) {
    const max = Math.max(0, track.scrollWidth - VIEW_W);
    return Math.min(0, Math.max(-max, x));
  }

  setX(track, x) {
    this.tx.set(track, x);
    track.style.transform = `translateX(${x}px)`;
    const shelf = track.closest("[data-section]");
    if (shelf) {
      shelf.classList.toggle("can-left", x < 0);
      shelf.classList.toggle("can-right", x > -Math.max(0, track.scrollWidth - VIEW_W));
    }
  }

  setY(y) {
    this.ty = y;
    const page = this.page();
    if (page) page.style.transform = `translateY(${y}px)`;
  }

  /** Chevron (pointer only): one view further along its shelf. */
  page_track(chev) {
    const track = chev.closest("[data-section]")?.querySelector("[data-track]");
    if (!track) return;
    const step = (chev.dataset.ptr === "left" ? 1 : -1) * VIEW_W * 0.8;
    this.setX(track, this.clampX(track, (this.tx.get(track) || 0) + step));
    this.measure("shelf", track.closest("[data-section]")?.dataset.section || "-");
    setTimeout(() => { this.lazy(); this.hitTest(); }, 360);
  }

  // ---------------------------------------------------------------- pointer
  setPointer(on) {
    if (this.pointer === on) return;
    this.pointer = on;
    document.body.classList.toggle("pointer", on);
  }

  onMouse(e) {
    this.setPointer(true);
    this.px = e.clientX;
    this.py = e.clientY;
    if (this.moveRaf) return;
    this.moveRaf = requestAnimationFrame(() => { this.moveRaf = 0; this.hitTest(); });
  }

  /** The tile under the pointer gets the focus (also after a shelf moved under a still pointer). */
  hitTest() {
    if (!this.pointer || this.px === undefined) return;
    const hit = document.elementFromPoint(this.px, this.py)?.closest("[data-focus]");
    if (hit && this.root.contains(hit) && hit !== this.el) this.set(hit, { scroll: false });
  }

  onClick(e) {
    if (performance.now() - this.lastOk < 400) return;   // the TV's pointer OK already came as Enter
    const hit = e.target.closest?.("[data-focus]");
    if (!hit || !this.root.contains(hit)) return;
    this.set(hit, { scroll: false });
    this.activate();
  }

  onWheel(e) {
    this.setPointer(true);
    const track = e.target.closest?.("[data-section]")?.querySelector("[data-track]");
    if (track && e.target.closest("[data-section]").contains(e.target)) {
      this.setX(track, this.clampX(track, (this.tx.get(track) || 0) - Math.sign(e.deltaY) * 420));
      this.measure("shelf", track.closest("[data-section]").dataset.section);
    } else {
      const page = this.page();
      if (!page) return;
      const min = Math.min(0, SAFE_BOTTOM - page.offsetHeight);
      this.setY(Math.max(min, Math.min(0, this.ty - Math.sign(e.deltaY) * 300)));
      this.measure("page", "-");
    }
    setTimeout(() => { this.lazy(); this.hitTest(); }, 360);
  }

  // ---------------------------------------------------------------- covers within one screen of the view
  lazy() {
    for (const img of this.root.querySelectorAll("img[data-src]")) {
      const r = img.getBoundingClientRect();
      if (r.right > -1920 && r.left < 3840 && r.bottom > -1080 && r.top < 2160) {
        img.src = img.dataset.src;
        img.removeAttribute("data-src");
      }
    }
  }

  // ---------------------------------------------------------------- FPS of a move (requestAnimationFrame for 0.7 s)
  measure(kind, section) {
    if (this.fpsLeft <= 0 || this.measuring) return;
    this.fpsLeft -= 1;
    this.measuring = true;
    const video = !!this.opts.videoPlaying?.();
    let t0 = 0, frames = 0, last = 0, gap = 0;
    const tick = (t) => {
      frames += 1;
      gap = Math.max(gap, t - last);
      last = t;
      if (t - t0 < 700) { requestAnimationFrame(tick); return; }
      this.measuring = false;
      const fps = frames / ((t - t0) / 1000);
      this.opts.log?.(`fps ${kind} ${section}: ${fps.toFixed(1)} fps, max frame gap ${Math.round(gap)} ms` +
                      `${video ? ", over a playing video" : ""}`);
    };
    requestAnimationFrame((t) => { t0 = last = t; requestAnimationFrame(tick); });
  }
}
