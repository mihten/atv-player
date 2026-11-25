// LTVPlayer: cover colours, ported 1:1 from Shared/Artwork.swift (RGB helpers, Palette.from(hue:), forItem, extract).
// A palette = {base, tones[4], vibrant}; colours are {r, g, b} in 0..1. The current item's palette is the app's accent:
// applyPalette() writes it as CSS variables (registered in app.css, so they cross-fade).

export function hsb(c) {
  const maxV = Math.max(c.r, c.g, c.b), minV = Math.min(c.r, c.g, c.b), d = maxV - minV;
  let h = 0;
  if (d > 0) {
    if (maxV === c.r) h = ((c.g - c.b) / d) % 6;
    else if (maxV === c.g) h = (c.b - c.r) / d + 2;
    else h = (c.r - c.g) / d + 4;
    h /= 6;
    if (h < 0) h += 1;
  }
  return { h, s: maxV === 0 ? 0 : d / maxV, v: maxV };
}

export function fromHSB(h, s, v) {
  const h6 = (h - Math.floor(h)) * 6, i = Math.trunc(h6) % 6, f = h6 - Math.floor(h6);
  const p = v * (1 - s), q = v * (1 - s * f), t = v * (1 - s * (1 - f));
  switch (i) {
    case 0: return { r: v, g: t, b: p };
    case 1: return { r: q, g: v, b: p };
    case 2: return { r: p, g: v, b: t };
    case 3: return { r: p, g: q, b: v };
    case 4: return { r: t, g: p, b: v };
    default: return { r: v, g: p, b: q };
  }
}

/** Same hue, chosen saturation (at least) and brightness. */
export function saturated(c, s, v) {
  const x = hsb(c);
  return fromHSB(x.h, Math.max(x.s, s), v);
}

export function withBrightness(c, v, s = null) {
  const x = hsb(c);
  return fromHSB(x.h, s ?? x.s, v);
}

export function css(c, alpha = 1) {
  const k = (x) => Math.round(Math.min(1, Math.max(0, x)) * 255);
  return alpha >= 1 ? `rgb(${k(c.r)}, ${k(c.g)}, ${k(c.b)})` : `rgba(${k(c.r)}, ${k(c.g)}, ${k(c.b)}, ${alpha})`;
}

/** Artwork-free palette: a deterministic hue (per item id) so songs without art still differ. */
export function fromHue(hue) {
  return {
    base: fromHSB(hue, 0.6, 0.10),
    tones: [fromHSB(hue, 0.75, 0.45), fromHSB(hue + 0.08, 0.7, 0.35), fromHSB(hue - 0.1, 0.65, 0.4), fromHSB(hue + 0.5, 0.5, 0.25)],
    vibrant: fromHSB(hue, 0.65, 1),
  };
}

export const FALLBACK = fromHue(0.72);

export function forItem(id) {
  return fromHue((id * 0.618033988) % 1);
}

/** Hue histogram over 32x32 RGBA pixels, weighted by saturation x brightness (vivid colours win). */
export function extractPixels(pixels) {
  const bins = Array.from({ length: 12 }, () => ({ r: 0, g: 0, b: 0, weight: 0 }));
  const dark = { r: 0, g: 0, b: 0, weight: 0 };
  let total = 0;
  for (let i = 0; i < pixels.length; i += 4) {
    const px = { r: pixels[i] / 255, g: pixels[i + 1] / 255, b: pixels[i + 2] / 255 };
    const c = hsb(px);
    if (c.v < 0.5) {   // shadows: the depth colour of the background
      const w = 0.2 + c.s;
      dark.r += px.r * w; dark.g += px.g * w; dark.b += px.b * w; dark.weight += w;
    }
    if (!(c.s > 0.18 && c.v > 0.14)) continue;
    const w = c.s * c.v * c.v;
    const k = Math.min(11, Math.trunc(c.h * 12));
    bins[k].r += px.r * w; bins[k].g += px.g * w; bins[k].b += px.b * w;
    bins[k].weight += w;
    total += w;
  }
  const ranked = bins.filter((b) => b.weight > 0 && b.weight > total * 0.04)
    .sort((a, b) => b.weight - a.weight)
    .map((b) => ({ r: b.r / b.weight, g: b.g / b.weight, b: b.b / b.weight }));
  const shadow = dark.weight > 0 ? { r: dark.r / dark.weight, g: dark.g / dark.weight, b: dark.b / dark.weight } : null;
  if (!ranked.length) {   // grey / black-and-white art: a cool neutral palette
    const h = shadow ? hsb(shadow).h : 0.6;
    return {
      base: fromHSB(h, 0.15, 0.08),
      tones: [fromHSB(h, 0.12, 0.32), fromHSB(h, 0.1, 0.22), fromHSB(h + 0.5, 0.1, 0.18), fromHSB(h, 0.05, 0.4)],
      vibrant: fromHSB(0.58, 0.25, 0.95),
    };
  }
  const top = ranked[0], t = hsb(top);
  let depth;
  if (shadow) {
    const c = hsb(shadow);
    depth = fromHSB(c.h, Math.min(0.8, Math.max(0.35, c.s * 1.4)), 0.3);
  } else {
    depth = fromHSB(t.h, t.s * 0.8, 0.28);
  }
  const lift = (c) => withBrightness(c, Math.min(0.62, Math.max(0.38, hsb(c).v * 0.7)));
  const t0 = lift(top);
  const t1 = ranked.length > 1 ? lift(ranked[1]) : fromHSB(t.h, t.s, 0.3);
  const t3 = ranked.length > 2 ? lift(ranked[2]) : { r: (t0.r + depth.r) / 2, g: (t0.g + depth.g) / 2, b: (t0.b + depth.b) / 2 };
  const d = hsb(depth);
  return {
    base: fromHSB(d.h, Math.min(0.7, d.s), 0.09),
    tones: [t0, t1, depth, t3],
    vibrant: saturated(top, 0.55, 1),
  };
}

let canvas = null;
export function extractImage(img) {
  canvas = canvas || Object.assign(document.createElement("canvas"), { width: 32, height: 32 });
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  ctx.imageSmoothingQuality = "medium";
  ctx.clearRect(0, 0, 32, 32);
  ctx.drawImage(img, 0, 0, 32, 32);
  return extractPixels(ctx.getImageData(0, 0, 32, 32).data);
}

// ---------------------------------------------------------------- per item, cached (40 entries, like ArtworkStore)
const cache = new Map();

/** The palette of an item: from its cover when it has one, else forItem(id). */
export function paletteFor(item) {
  if (!item) return Promise.resolve(FALLBACK);
  if (cache.has(item.id)) return Promise.resolve(cache.get(item.id));
  const keep = (p) => {
    cache.set(item.id, p);
    if (cache.size > 40) cache.delete(cache.keys().next().value);
    return p;
  };
  if (!item.art) return Promise.resolve(keep(forItem(item.id)));
  return new Promise((resolve) => {
    const img = new Image();
    img.decoding = "async";
    img.onload = () => {
      let p;
      try { p = extractImage(img); } catch (e) { p = forItem(item.id); }
      resolve(keep(p));
    };
    img.onerror = () => resolve(keep(forItem(item.id)));
    img.src = `/art/${item.id}`;
  });
}

// The UI accent (icons, eyebrow, EQ, progress bars, glows) is calmer than the extracted `vibrant`: on the OLED a
// vivid cover (e.g. a pure blue) made it far too strong. Same hue, saturation 0.30-0.42, brightness 0.92 (chosen on
// the TV, option C). Extraction itself stays the 1:1 Artwork.swift port.
export const UI_SAT_MIN = 0.30, UI_SAT_MAX = 0.42, UI_BRI = 0.92;
export function uiAccent(v) {
  const x = hsb(v);
  return fromHSB(x.h, Math.min(UI_SAT_MAX, Math.max(UI_SAT_MIN, x.s)), UI_BRI);
}

/** The app-wide accent: CSS variables on the root (they cross-fade, see @property in app.css). */
export function applyPalette(p) {
  const s = document.documentElement.style, a = uiAccent(p.vibrant);
  s.setProperty("--accent", css(a));
  s.setProperty("--accent-55", css(a, 0.55));
  s.setProperty("--base", css(p.base));
  p.tones.forEach((t, i) => s.setProperty(`--t${i}`, css(t)));
  s.setProperty("--vib75", css(withBrightness(p.vibrant, 0.75)));
}
