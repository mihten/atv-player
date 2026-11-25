// LTVPlayer: REST helpers (plan 8.5). All paths are relative: the page is served by the server itself.

export async function getJSON(path) {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

/** Swift formatTime: 0:00, m:ss, h:mm:ss. */
export function formatTime(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0:00";
  const t = Math.floor(seconds), h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

export const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** PlaylistInfo.detail of the apps: "40 items · 40 videos · 3:00:44 · 1 missing". */
export function playlistDetail(p) {
  const parts = [plural(p.count, "item")];
  if (p.videos > 0) parts.push(plural(p.videos, "video"));
  if (p.duration > 0) parts.push(formatTime(p.duration));
  if (p.missing > 0) parts.push(`${p.missing} missing`);
  return parts.join(" · ");
}

/** MixSummary.detail: "Not set", "2 folders", "1 folder · 3 playlists". */
export function mixDetail(m) {
  const parts = [];
  if (m.folders > 0) parts.push(plural(m.folders, "folder"));
  if (m.playlists > 0) parts.push(plural(m.playlists, "playlist"));
  return parts.length ? parts.join(" · ") : "Not set";
}

export const mixEmpty = (m) => (m.folders || 0) + (m.playlists || 0) === 0;

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
