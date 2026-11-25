// LTVPlayer: markup shared by Home and the list screens: cover thumbs, playlist mosaics, cards, posters, list rows.
// Covers are <img data-src> (loaded lazily by the focus engines); without art a coloured placeholder with a symbol.

import { icon } from "../icons.js";
import { formatTime, esc } from "../api.js";
import { forItem, css, withBrightness } from "../palette.js";

/** Cover <img> (lazy) or a coloured placeholder with a note / film symbol. */
export function thumb(item, cls = "") {
  if (item && item.art) return `<img class="th ${cls}" data-src="/art/${item.id}" decoding="async" alt="">`;
  const p = forItem(item ? item.id : 0);
  return `<div class="th ph ${cls}" style="background:linear-gradient(135deg, ${css(withBrightness(p.vibrant, 0.62, 0.55))}, ` +
         `${css(p.tones[0])} 60%, ${css(p.base)})">${icon(item && item.kind === "video" ? "videos" : "note", 44)}</div>`;
}

/** 2 x 2 mosaic of the first four covers, one cover if fewer, a palette placeholder if none (PlaylistCardLabel). */
export function mosaic(items, seed) {
  if (items.length >= 4) return `<div class="mosaic">${items.slice(0, 4).map((i) => thumb(i)).join("")}</div>`;
  if (items.length) return thumb(items[0]);
  const p = forItem(seed * 7);
  return `<div class="th ph" style="background:linear-gradient(135deg, ${css(withBrightness(p.vibrant, 0.72))}, ` +
         `${css(p.tones[0])} 55%, ${css(p.base)})">${icon("playlists", 120)}</div>`;
}

/** 280 x 280 card: mosaic, fade, name (2 lines) and detail. */
export function card(focusId, action, key, name, detail, items, seed) {
  return `<div class="tile card" data-focus="${esc(focusId)}" data-action="${action}" data-key="${esc(key)}">${mosaic(items, seed)}
    <div class="fade"></div><div class="cap"><div class="cap-t two">${esc(name)}</div><div class="cap-s">${esc(detail)}</div></div></div>`;
}

/** 16:9 music video poster with the title on a dark fade (PosterLabel). cls "grid" = the 410 x 231 list size. */
export function poster(v, cls = "") {
  return `<div class="tile poster ${cls}" data-focus="video:${v.id}" data-action="video" data-key="${v.id}">${thumb(v)}
    <div class="fade"></div><div class="cap"><div class="cap-t">${esc(v.title)}</div>
    ${v.artist ? `<div class="cap-s">${esc(v.artist)}</div>` : ""}</div></div>`;
}

export const bars = (n = 5) => `<span class="eq">${"<i></i>".repeat(n)}</span>`;

/** Item count wording of the apps' play rows (PlayLabels.count). */
export const count = (n) => (n === 1 ? "1 item" : `${n} items`);

/** The inner markup of a list row (plan 10.5): thumb or symbol, title, detail (+ badge chips), ♥, duration.
 *  o: {item, symbol, title, sub, badges, right, current, num} */
export function rowInner(o) {
  const it = o.item;
  let lead;
  if (it) {
    lead = `<div class="r-th ${it.kind === "video" ? "wide" : "sq"}">${thumb(it)}${o.current ? `<span class="r-now">${bars(3)}</span>` : ""}</div>`;
  } else {
    lead = `<div class="r-th sym">${icon(o.symbol || "note", 40)}</div>`;
  }
  const chips = (o.badges || []).map((b) => `<span class="r-chip">${esc(b)}</span>`).join("");
  const right = o.right !== undefined ? o.right
    : it ? `${it.loved ? '<span class="r-love">♥</span>' : ""}${it.duration ? formatTime(it.duration) : ""}` : "";
  return `${o.num !== undefined ? `<div class="r-num">${o.num}</div>` : ""}${lead}
    <div class="r-text"><div class="r-title">${esc(o.title)}</div>
      ${o.sub || chips ? `<div class="r-sub"><span class="r-subt">${esc(o.sub || "")}</span>${chips}</div>` : ""}</div>
    <div class="r-right">${right}</div>`;
}
