// LTVPlayer: lyrics from the server (`/api/lyrics/<id>`: {lyr, tracks: [{ord, lang, title, is_default, forced, synced,
// cues: [{s, e, t}]}]}), with the Apple TV app's track choice (Shared/Lyrics.swift): the chosen language (default-flagged
// first, then any full track, then a forced one), else the file's default, else a forced track, else the first; the
// second language = a timed full track in that language that is not the one on screen. The current item and the next
// one are fetched ahead; at most 40 answers are kept.

const CODES = { eng: ["eng", "en"], hrv: ["hrv", "hr", "scr", "cro", "hbs", "sh"] };
const KEEP = 40;

export function chosen(resp, lang) {
  const tracks = (resp && resp.tracks) || [];
  if (tracks.length <= 1) return tracks[0] || null;
  const codes = CODES[lang];
  if (codes) {
    const m = tracks.filter((t) => codes.includes(t.lang));
    const pick = m.find((t) => t.is_default && !t.forced) || m.find((t) => !t.forced) || m[0];
    if (pick) return pick;
  }
  return tracks.find((t) => t.is_default) || tracks.find((t) => t.forced) || tracks[0];
}

export function second(resp, lang, shown) {
  const codes = CODES[lang];
  if (!codes || !resp) return null;
  const m = (resp.tracks || []).filter((t) => t.synced && codes.includes(t.lang) && t !== shown);
  return m.find((t) => !t.forced && t.is_default) || m.find((t) => !t.forced) || null;
}

/** Lines on screen at time t: every cue with s <= t < e, joined (overlapping cues stack). */
export function textAt(track, t) {
  if (!track || !track.synced) return "";
  const out = [];
  for (const c of track.cues || []) {
    if (c.s === null || c.s === undefined) continue;
    if (c.s > t) break;
    if (c.e !== null && c.e !== undefined && t < c.e) out.push(c.t);
  }
  return out.join("\n");
}

/** The index of the last cue that started at t (-1 before the first) and whether it is still "lit" (until its end +1.5 s). */
export function cueAt(track, t) {
  let found = -1;
  const cues = (track && track.cues) || [];
  for (let i = 0; i < cues.length; i += 1) {
    if (cues[i].s !== null && cues[i].s !== undefined && cues[i].s <= t) found = i; else break;
  }
  const c = cues[found];
  return { index: found, lit: found >= 0 && (c.e === null || c.e === undefined || t <= c.e + 1.5) };
}

export class LyricsStore {
  constructor(log) {
    this.log = log;
    this.cache = new Map();   // id -> response | {tracks: []} when the file has none / the fetch failed
    this.inflight = new Map();
  }

  has(id) { return this.cache.has(id); }
  get(id) { return this.cache.get(id) || null; }

  /** The answer for an item (fetched once); items whose `lyr` is 0 have none, no request. */
  load(item) {
    if (!item) return Promise.resolve(null);
    if (this.cache.has(item.id)) return Promise.resolve(this.cache.get(item.id));
    if (item.lyr === 0) { this.put(item.id, { lyr: 0, tracks: [] }); return Promise.resolve(this.cache.get(item.id)); }
    if (this.inflight.has(item.id)) return this.inflight.get(item.id);
    const p = fetch(`/api/lyrics/${item.id}`, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : { lyr: 0, tracks: [] }))
      .catch((e) => { this.log?.(`lyrics #${item.id}: ${e.message}`); return { lyr: 0, tracks: [], failed: true }; })
      .then((d) => { this.inflight.delete(item.id); this.put(item.id, d); return d; });
    this.inflight.set(item.id, p);
    return p;
  }

  put(id, d) {
    this.cache.set(id, d);
    while (this.cache.size > KEEP) this.cache.delete(this.cache.keys().next().value);
  }
}
