"""Library scanner for the ATV Player media server.

Walks the read-only library roots, reads tags (mutagen) and video stream info (ffprobe),
and keeps a SQLite table of playable items. Unchanged files (same size + mtime) are skipped,
so a rescan only touches new or modified files.
"""
import base64
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
import unicodedata

import mutagen

log = logging.getLogger("atv-media.scan")

AUDIO_EXT = {".flac", ".mp3", ".m4a", ".wv", ".ogg", ".opus", ".wav", ".aac", ".ape", ".aiff", ".aif"}
VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".mov", ".webm", ".avi"}
LOSSLESS_EXT = {".flac", ".wv", ".wav", ".ape", ".aiff", ".aif"}
LOSSLESS_CODECS = {"flac", "alac", "wavpack", "truehd", "pcm_s16le", "pcm_s24le", "pcm_s32le"}
FOLDER_ART = ("cover", "folder", "front", "albumart")
IMAGE_EXT = (".jpg", ".jpeg", ".png")

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  root TEXT NOT NULL,
  relpath TEXT NOT NULL,
  folder TEXT NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  artist TEXT,
  album TEXT,
  albumartist TEXT,
  track INTEGER,
  disc INTEGER,
  year TEXT,
  duration REAL,
  codec TEXT,
  lossless INTEGER NOT NULL DEFAULT 0,
  channels INTEGER,
  bitrate INTEGER,
  width INTEGER,
  height INTEGER,
  size INTEGER NOT NULL,
  mtime REAL NOT NULL,
  has_art INTEGER NOT NULL DEFAULT 0,
  audio_track TEXT,
  audio_ord INTEGER,
  sub_track TEXT,
  sub_ord INTEGER,
  subs TEXT,
  lufs REAL,
  tpeak REAL,
  loud_fail INTEGER NOT NULL DEFAULT 0,
  hidden INTEGER NOT NULL DEFAULT 0,
  dup_of INTEGER,
  added REAL NOT NULL,
  UNIQUE(root, relpath)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

COLUMNS = (
    "root", "relpath", "folder", "kind", "title", "artist", "album", "albumartist", "track", "disc",
    "year", "duration", "codec", "lossless", "channels", "bitrate", "width", "height", "size", "mtime",
    "has_art", "audio_track", "audio_ord", "sub_track", "sub_ord", "subs", "added",
)


def connect(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(items)")}
    if "subs" not in cols:  # added 2026-10-19 (subtitle language choice); NULL = videos get re-probed once
        con.execute("ALTER TABLE items ADD COLUMN subs TEXT")
        con.commit()
    if "lufs" not in cols:  # added 2026-10-20 (loudness leveling; filled by loudness.py)
        con.execute("ALTER TABLE items ADD COLUMN lufs REAL")
        con.execute("ALTER TABLE items ADD COLUMN tpeak REAL")
        con.execute("ALTER TABLE items ADD COLUMN loud_fail INTEGER NOT NULL DEFAULT 0")
        con.commit()
    return con


def parse_filename(stem):
    """'01 - Artist - Title' / 'Artist - Title' -> (artist, title)."""
    s = re.sub(r"^\s*\d{1,3}\s*[-._)]\s*", "", stem)
    for sep in (" - ", " – ", " — "):
        if sep in s:
            artist, title = s.split(sep, 1)
            return artist.strip() or None, title.strip() or s.strip()
    return None, s.strip()


def _first(tags, *keys):
    if not tags:
        return None
    for key in keys:
        try:
            value = tags.get(key)
        except Exception:  # some tag classes raise on odd keys
            value = None
        if value:
            if isinstance(value, (list, tuple)):
                value = value[0]
            value = str(value).strip()
            if value:
                return value
    return None


def _num(value):
    if not value:
        return None
    m = re.match(r"\s*(\d+)", str(value))
    return int(m.group(1)) if m else None


def read_audio(path):
    f = mutagen.File(path, easy=True)
    if f is None:
        return None
    info, tags = f.info, f.tags
    ext = os.path.splitext(path)[1].lower()
    codec = getattr(info, "codec", None) or ext.lstrip(".")
    return {
        "title": _first(tags, "title"),
        "artist": _first(tags, "artist"),
        "album": _first(tags, "album"),
        "albumartist": _first(tags, "albumartist", "album artist"),
        "track": _num(_first(tags, "tracknumber", "track")),
        "disc": _num(_first(tags, "discnumber", "disc")),
        "year": _first(tags, "date", "year"),
        "duration": getattr(info, "length", None),
        "channels": getattr(info, "channels", None),
        "bitrate": getattr(info, "bitrate", None) or None,
        "codec": str(codec),
        "lossless": 1 if ext in LOSSLESS_EXT or str(codec).lower() == "alac" else 0,
    }


def embedded_art(path):
    """Embedded front cover bytes, or None."""
    try:
        f = mutagen.File(path)
    except Exception:
        return None
    if f is None:
        return None
    pictures = getattr(f, "pictures", None)  # FLAC
    if pictures:
        front = [p for p in pictures if p.type == 3] or pictures
        return front[0].data
    tags = f.tags
    if tags is None:
        return None
    if hasattr(tags, "getall"):  # ID3
        apic = tags.getall("APIC")
        if apic:
            front = [p for p in apic if p.type == 3] or apic
            return front[0].data
        return None
    for key in ("covr", "Cover Art (Front)", "metadata_block_picture"):
        try:
            value = tags.get(key)
        except Exception:
            value = None
        if not value:
            continue
        if key == "covr":  # MP4
            return bytes(value[0])
        if key == "metadata_block_picture":  # Ogg/Opus
            from mutagen.flac import Picture
            try:
                return Picture(base64.b64decode(value[0])).data
            except Exception:
                return None
        data = getattr(value, "value", None) or bytes(value)  # APEv2 binary: "name\0<data>"
        i = data.find(b"\x00")
        return data[i + 1:] if 0 <= i < 256 else data
    return None


def folder_art(dirpath):
    """Path of cover/folder/front image in the directory, or None."""
    try:
        names = os.listdir(dirpath)
    except OSError:
        return None
    for preferred in FOLDER_ART:
        for name in names:
            stem, ext = os.path.splitext(name.lower())
            if ext in IMAGE_EXT and stem == preferred:
                return os.path.join(dirpath, name)
    return None


def probe_video(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True, text=True, timeout=120,
    )
    data = json.loads(out.stdout or "{}")
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}

    def is_default(s):
        return (s.get("disposition") or {}).get("default") == 1

    def title_of(s):
        t = s.get("tags") or {}
        return t.get("title") or t.get("TITLE")

    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    audio_def = next((s for s in audios if is_default(s)), audios[0] if audios else None)
    sub_def = next((s for s in subs if is_default(s)), None)
    try:
        duration = float(fmt.get("duration") or 0) or None
    except ValueError:
        duration = None
    return {
        "title": tags.get("title"),
        "artist": tags.get("artist"),
        "album": tags.get("album"),
        "year": tags.get("date") or tags.get("year"),
        "duration": duration,
        "codec": (video or {}).get("codec_name"),
        "width": (video or {}).get("width"),
        "height": (video or {}).get("height"),
        "channels": (audio_def or {}).get("channels"),
        "lossless": 1 if audio_def and audio_def.get("codec_name") in LOSSLESS_CODECS else 0,
        "audio_track": title_of(audio_def) if audio_def else None,
        "audio_ord": audios.index(audio_def) if audio_def and len(audios) > 1 else None,
        "sub_track": (title_of(sub_def) or (sub_def.get("tags") or {}).get("language")) if sub_def else None,
        "sub_ord": subs.index(sub_def) if sub_def else None,
        # Every subtitle track in file order (= the player's ordinal), for the app's language choice.
        "subs": json.dumps([{
            "ord": n,
            "lang": ((s.get("tags") or {}).get("language") or (s.get("tags") or {}).get("LANGUAGE") or "").lower(),
            "title": title_of(s) or "",
            "is_default": is_default(s),
            "forced": (s.get("disposition") or {}).get("forced") == 1,
        } for n, s in enumerate(subs)]),
    }


def build_record(root, base, full, st, kind):
    rel = os.path.relpath(full, base)
    stem = os.path.splitext(os.path.basename(full))[0]
    fn_artist, fn_title = parse_filename(stem)
    rec: dict = {k: None for k in COLUMNS}
    rec.update(root=root, relpath=rel, folder=os.path.dirname(rel), kind=kind,
               size=st.st_size, mtime=st.st_mtime, lossless=0, has_art=0, added=time.time())
    if kind == "audio":
        info = read_audio(full) or {}
        rec.update({k: v for k, v in info.items() if v is not None})
        art = embedded_art(full) is not None or folder_art(os.path.dirname(full)) is not None
        rec["has_art"] = 1 if art else 0
    else:
        info = probe_video(full)
        rec.update({k: v for k, v in info.items() if v is not None})
        rec["has_art"] = 1  # a frame grab
        if fn_artist:  # file names are curated; embedded titles are raw YouTube titles
            rec["artist"], rec["title"] = fn_artist, fn_title
    rec["title"] = rec.get("title") or fn_title
    rec["artist"] = rec.get("artist") or rec.get("albumartist") or fn_artist
    return rec


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def dedupe(con):
    """Hide audio duplicates: same artist+title, same channels, duration within 3 s.
    The best copy stays visible (lossless > higher bitrate > shorter path)."""
    con.execute("UPDATE items SET hidden = 0, dup_of = NULL")
    rows = con.execute(
        "SELECT id, artist, title, lossless, bitrate, channels, duration, relpath FROM items WHERE kind = 'audio'"
    ).fetchall()
    groups = {}
    for r in rows:
        a, t = norm(r["artist"]), norm(r["title"])
        if a and t:
            groups.setdefault((a, t), []).append(r)
    hidden = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda r: (-r["lossless"], -(r["bitrate"] or 0), len(r["relpath"])))
        keep = []
        for r in members:
            twin = next((k for k in keep if (k["channels"] or 2) == (r["channels"] or 2)
                         and abs((k["duration"] or 0) - (r["duration"] or 0)) <= 3), None)
            if twin is None:
                keep.append(r)
            else:
                con.execute("UPDATE items SET hidden = 1, dup_of = ? WHERE id = ?", (twin["id"], r["id"]))
                hidden += 1
    return hidden


def carry_over_ids(con, gone, new_keys):
    """A file renamed or moved on the NAS shows up as one removed + one added row. Give the new row
    the old id so queues (and the item playing right now) keep working. Match: same root and kind,
    duration within 0.5 s, and either the same size, or the same folder with size within 2 %
    (re-tagged). Only one-to-one matches count. Returns the ids that were carried over."""
    if not gone or not new_keys:
        return set()
    fresh = []
    for root, rel in new_keys:
        row = con.execute("SELECT id, root, relpath, folder, kind, size, duration FROM items "
                          "WHERE root = ? AND relpath = ?", (root, rel)).fetchone()
        if row:
            fresh.append(row)

    def same(old, new):
        if old["root"] != new["root"] or old["kind"] != new["kind"]:
            return False
        if not old["duration"] or not new["duration"] or abs(old["duration"] - new["duration"]) > 0.5:
            return False
        if old["size"] == new["size"]:
            return True
        return old["folder"] == new["folder"] and abs(old["size"] - new["size"]) <= 0.02 * old["size"]

    pairs = {}
    for old in gone:
        candidates = [new for new in fresh if same(old, new)]
        if len(candidates) == 1:
            pairs[old["id"]] = candidates[0]
    claims = {}
    for new in pairs.values():
        claims[new["id"]] = claims.get(new["id"], 0) + 1
    carried = set()
    for old_id, new in pairs.items():
        if claims[new["id"]] != 1:
            continue
        con.execute("DELETE FROM items WHERE id = ?", (old_id,))
        con.execute("UPDATE items SET id = ? WHERE id = ?", (old_id, new["id"]))
        log.info("renamed: id %s now %s/%s", old_id, new["root"], new["relpath"])
        carried.add(old_id)
    return carried


def scan(db_path, roots, force=False):
    started = time.time()
    con = connect(db_path)
    existing = {(r["root"], r["relpath"]): r for r in con.execute(
        "SELECT id, root, relpath, folder, kind, size, mtime, duration, subs FROM items")}
    seen, added, updated, failed = set(), 0, 0, 0
    new_keys = []
    found = {root: 0 for root in roots}
    meta = con.execute("SELECT value FROM meta WHERE key = 'max_id'").fetchone()
    high_water = max([int(meta["value"]) if meta else 0] + [r["id"] for r in existing.values()])
    placeholders = ", ".join("?" for _ in COLUMNS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in COLUMNS if c not in ("root", "relpath", "added"))
    sql = (f"INSERT INTO items ({', '.join(COLUMNS)}) VALUES ({placeholders}) "
           f"ON CONFLICT(root, relpath) DO UPDATE SET {updates}")
    for root, base in roots.items():
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                if name.startswith("."):
                    continue
                ext = os.path.splitext(name)[1].lower()
                kind = "audio" if ext in AUDIO_EXT else "video" if ext in VIDEO_EXT else None
                if kind is None:
                    continue
                full = os.path.join(dirpath, name)
                key = (root, os.path.relpath(full, base))
                seen.add(key)
                found[root] += 1
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                old = existing.get(key)
                if not force and old and old["size"] == st.st_size and abs(old["mtime"] - st.st_mtime) < 1 \
                        and not (kind == "video" and old["subs"] is None):  # one-time re-probe for "subs"
                    continue
                try:
                    rec = build_record(root, base, full, st, kind)
                except Exception as exc:  # unreadable/corrupt file: skip, keep scanning
                    log.warning("skip %s: %s", full, exc)
                    failed += 1
                    continue
                con.execute(sql, [rec[c] for c in COLUMNS])
                if old:
                    # the file changed: its measured loudness is stale
                    con.execute("UPDATE items SET lufs = NULL, tpeak = NULL, loud_fail = 0 "
                                "WHERE root = ? AND relpath = ?", key)
                    updated += 1
                else:
                    added += 1
                    new_keys.append(key)
    # A root with no media files at all is a missing/unmounted share, not an empty library: keep its
    # rows (and their ids, which queues and playlists point at) until the files are visible again.
    offline = sorted(root for root, n in found.items() if n == 0 and any(k[0] == root for k in existing))
    for root in offline:
        log.warning("root %s (%s): no media files found, keeping its %d items (share not mounted?)",
                    root, roots[root], sum(1 for k in existing if k[0] == root))
    gone = [row for key, row in existing.items() if key not in seen and key[0] not in offline]
    renamed = carry_over_ids(con, gone, new_keys)
    removed = 0
    for row in gone:
        if row["id"] not in renamed:
            con.execute("DELETE FROM items WHERE id = ?", (row["id"],))
            removed += 1
    added -= len(renamed)
    # Never hand out an id twice: SQLite reuses the highest id after it was deleted, and a playlist
    # entry for a deleted song would then play whatever new file got that id.
    for root, rel in new_keys:
        row = con.execute("SELECT id FROM items WHERE root = ? AND relpath = ?", (root, rel)).fetchone()
        if row is None or row["id"] in renamed:
            continue
        if row["id"] <= high_water:
            top = con.execute("SELECT MAX(id) AS m FROM items").fetchone()["m"] or 0
            new_id = max(high_water, top) + 1
            con.execute("UPDATE items SET id = ? WHERE id = ?", (new_id, row["id"]))
            log.info("id %s was used before, new file %s/%s gets id %s", row["id"], root, rel, new_id)
            high_water = new_id
        else:
            high_water = row["id"]
    top = con.execute("SELECT MAX(id) AS m FROM items").fetchone()["m"] or 0
    con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('max_id', ?)", (str(max(high_water, top)),))
    hidden = dedupe(con)
    stats = {"added": added, "updated": updated, "removed": removed, "renamed": len(renamed),
             "renamed_ids": sorted(renamed), "failed": failed, "offline_roots": offline,
             "hidden_duplicates": hidden, "seconds": round(time.time() - started, 1),
             "finished_at": time.time()}
    con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_scan', ?)", (json.dumps(stats),))
    con.commit()
    con.close()
    return stats
