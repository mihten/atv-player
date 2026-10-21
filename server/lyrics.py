"""Embedded lyrics, read once per file and stored in the library DB (table `lyrics`, items.lyr).

Only lyrics that are INSIDE the file are used (nothing is fetched or guessed):
  * music videos: the subtitle tracks (every subtitle track in this library is lyrics / dialogue + lyrics),
    converted by ffmpeg to SRT, one entry per cue with start and end time;
  * songs: the lyrics tag (ID3 USLT/SYLT, Vorbis/FLAC LYRICS, MP4 ©lyr, APE Lyrics). Text with LRC time
    stamps ("[01:23.45]line", honouring [offset:]) is synced; anything else is plain text.

items.lyr: NULL = not looked at yet, 0 = no lyrics, 1 = plain text only, 2 = at least one synced track.
A changed file clears both (scanner); files that cannot be read get lyr = 0 and are not retried.
"""
import html
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time

import mutagen
import mutagen.mp4

log = logging.getLogger("atv-media")

STOP = threading.Event()
LOW_PRIORITY = ["nice", "-n", "19"] + (["ionice", "-c", "3"] if shutil.which("ionice") else [])
TAG_KEYS = {"lyrics", "unsyncedlyrics", "unsynced lyrics", "syncedlyrics", "synced lyrics", "lyrics-eng", "lrc"}
LRC_TIME = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
LRC_OFFSET = re.compile(r"^\[offset:\s*([+-]?\d+)\s*\]$", re.I)
LRC_HEADER = re.compile(r"^\[[a-z]{2,8}:[^\]]*\]$", re.I)  # [ti:..] [ar:..] [al:..] [by:..] ...
LRC_WORD = re.compile(r"<\d{1,3}:\d{2}(?:[.:]\d{1,3})?>")  # enhanced LRC word times
SRT_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")
MARKUP = re.compile(r"<[^>]*>|\{\\[^}]*\}")

# Music-note decorations that subtitle authors put around sung lines. They carry no information, so they are
# dropped (the player ignores them, nobody has to edit the files). Besides the real symbols this also covers
# the garbled forms: UTF-8 bytes of a note read as DOS text (cp437: "ΓÖ¬" for ♪), Windows-1252 ("â™ª") or Latin-1.
CLEAN_VERSION = "4"  # bump when the text cleaning changes: every file is read again once (ensure_current)
NOTE_CHARS = "♩♪♫♬♭♮♯🎵🎶🎼" + "".join(chr(c) for c in range(0x1D100, 0x1D1FF))


def _note_patterns():
    pats = set()
    for ch in NOTE_CHARS:
        raw = ch.encode("utf-8")
        for enc in ("cp437", "cp1252", "latin-1"):
            try:
                garbled = raw.decode(enc)
            except UnicodeDecodeError:
                continue
            if garbled != ch:
                pats.add(garbled)
    return sorted(pats, key=len, reverse=True)


NOTE_RE = re.compile("|".join(re.escape(p) for p in _note_patterns()) + "|[" + re.escape(NOTE_CHARS) + "]")
HASH_MARK = re.compile(r"^#\s+|\s+#$")  # "# lyrics #": a lone hash around a line is the same kind of mark

# Junk that is not lyrics: placeholders, watermarks, release-group credits, scraped web text, text garbled by a
# wrong encoding. A line is dropped, or only its junk part is cut, so the real lyrics stay. Nobody has to edit
# files. Add new patterns here (and bump CLEAN_VERSION).
BRACKET_TAG = re.compile(r"^\s*(?:\[[^\]]{0,40}\][\s.\-_~=*@#>:|]*)+|(?:[\s.\-_~=*@#>:|]*\[[^\]]{0,40}\])+\s*$")
JS_CODE = re.compile(r"\s*\b(?:var|let|const)\s+[\w$'\\]+\s*=.*$")  # scraped page code glued to a line
WEB_ADDRESS = re.compile(r"https?://|\bwww\.|\bt\.me/|\S+@\S+\.[a-z]{2,}", re.I)
EQUALS_WRAPPED = re.compile(r"^\s*=.*=\s*$")  # "=Willis Lyrics="
ENCODING_GARBAGE = re.compile("×÷|÷×|[ˇ˘˙˛˝¸]")  # a Chinese (GBK) credit line read as Latin text: "ÖĆ×÷Őß"
CREDIT_LINE = re.compile(
    r"\b(?:signature|synced|subtitles?|subtitled|lyrics?|lrc|transcribed|ripped|uploaded|timed|tagged)\s+by\b"
    r"|\b(?:uploader|tifon|tfm)\b|letölt|feltölt|kiadás|publication\b|>>"
    r"|\bthank(?:s| you)\b.{0,10}\bd\w{0,3}w\w{0,2}lo\w+"  # "Thank you download(ing)" and typos of it
    r"|^\s*(?:ripp?|rip)\s*:|^\s*enjoy[.!]*\s*$", re.I)
HEADER_LINE = re.compile(r"^\s*(?:artist|album|title|by|length|composer|lyricist)\s*[:：]", re.I)
HEADER_CUES = 8  # an Artist:/Album:/Title: block counts only at the start of a track


def clean_line(line):
    """One line of lyrics -> the line without junk, or "" when nothing real is left."""
    line = NOTE_RE.sub("", line)
    line = JS_CODE.sub("", line)
    line = BRACKET_TAG.sub("", line)
    line = HASH_MARK.sub("", re.sub(r"\s+", " ", line).strip()).strip()
    if (not any(ch.isalnum() for ch in line) or WEB_ADDRESS.search(line) or EQUALS_WRAPPED.match(line)
            or ENCODING_GARBAGE.search(line) or CREDIT_LINE.search(line)):
        return ""
    return line


def drop_headers(cues):
    """Remove an "Artist: …/Album: …/Title: …" block from the first cues (LRC id tags shown as text)."""
    out, seen = [], 0
    for cue in cues:
        if seen < HEADER_CUES and HEADER_LINE.match(cue["t"]):
            seen += 1
            continue
        if cue["t"]:
            seen += 1
        out.append(cue)
    return out


def _secs(h, m, s, frac):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(frac) / 10 ** len(frac)


def clean_text(text):
    text = html.unescape(MARKUP.sub("", text))
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.replace("\r", "").split("\n")]
    lines = [re.sub(r"^-\s*", "", ln) if len(lines) > 1 else ln for ln in lines]  # "- " speaker dashes
    lines = [clean_line(ln) for ln in lines]
    return "\n".join(ln for ln in lines if ln)


def parse_srt(text):
    """-> [{s, e, t}] sorted by start; empty cues dropped."""
    cues = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip())
    for block in blocks:
        lines = block.split("\n")
        for i, line in enumerate(lines[:3]):
            m = SRT_TIME.search(line)
            if m:
                g = m.groups()
                body = clean_text("\n".join(lines[i + 1:]))
                if body:
                    cues.append({"s": round(_secs(g[0], g[1], g[2], g[3]), 2),
                             "e": round(_secs(g[4], g[5], g[6], g[7]), 2), "t": body})
                break
    cues.sort(key=lambda c: c["s"])
    return cues


def has_lrc_times(text):
    return len(LRC_TIME.findall(text or "")) >= 3


def parse_lrc(text):
    """-> [{s, e, t}]. Displayed time = stamp - [offset:] (LRC spec: a positive offset makes the lyrics appear
    sooner; checked against the audio of two files, one with +500 and one with -4499 ms). A line without text
    only ends the previous line. A line with several stamps (repeated chorus) becomes several cues."""
    offset = 0.0
    raw = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        m = LRC_OFFSET.match(line)
        if m:
            offset = int(m.group(1)) / 1000
            continue
        if LRC_HEADER.match(line) and not LRC_TIME.match(line):
            continue
        # "[t1]text1[t2]text2": every stamp starts its own text (compressed LRC); several stamps in a row share
        # the text after them (repeated chorus). Text before the first stamp is ignored.
        stamps, pos, found = [], 0, False

        def flush(text):
            body = clean_text(LRC_WORD.sub("", text))
            for t in stamps:
                raw.append((max(0.0, t - offset), body))

        for m in LRC_TIME.finditer(line):
            segment = line[pos:m.start()]
            if stamps and segment.strip():
                flush(segment)
                stamps = []
            frac = m.group(3) or "0"
            stamps.append(int(m.group(1)) * 60 + int(m.group(2)) + int(frac) / 10 ** len(frac))
            found = True
            pos = m.end()
        if found:
            flush(line[pos:])
    raw.sort(key=lambda r: r[0])
    cues = []
    for i, (start, body) in enumerate(raw):
        if not body:
            continue
        end = raw[i + 1][0] if i + 1 < len(raw) else None
        cues.append({"s": round(start, 2), "e": round(end, 2) if end is not None else None, "t": body})
    return cues


def plain_cues(text):
    """Unsynced text: one cue per line; runs of blank lines become a single empty cue (a gap). Junk lines
    (credits, placeholders, notes…) are left out like they were not there."""
    cues, blank = [], False
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            blank = bool(cues)
            continue
        line = clean_line(line)
        if not line:
            continue
        if blank:
            cues.append({"s": None, "e": None, "t": ""})
            blank = False
        cues.append({"s": None, "e": None, "t": line})
    return cues


def _track(cues, synced, ord_=None, lang="", title="", default=False, forced=False):
    cues = drop_headers(cues)
    return {"ord": ord_, "lang": lang, "title": title, "is_default": default, "forced": forced,
            "synced": synced, "cues": cues}


def read_tag_lyrics(path):
    """-> a track dict from the file's lyrics tag, or None."""
    f = mutagen.File(path)
    if f is None or not f.tags:
        return None
    tags = f.tags
    texts, sylt = [], []
    if hasattr(tags, "getall"):  # ID3
        for frame in tags.getall("SYLT"):
            if getattr(frame, "format", 2) == 2:  # timestamps in ms
                sylt.append(frame)
        texts += [str(fr.text) for fr in tags.getall("USLT")]
    elif isinstance(f, mutagen.mp4.MP4):
        texts += [str(v) for v in tags.get("\xa9lyr", [])]
    else:  # Vorbis comments (FLAC, Ogg, Opus), APE (WavPack)
        for key, value in tags.items():
            if str(key).lower() in TAG_KEYS:
                values = value if isinstance(value, (list, tuple)) else [value]
                texts += [str(v) for v in values]
    for frame in sylt:
        cues = [{"s": round(ms / 1000, 2), "e": None, "t": clean_text(txt)} for txt, ms in frame.text if clean_text(txt)]
        cues.sort(key=lambda c: c["s"])
        for i in range(len(cues) - 1):
            cues[i]["e"] = cues[i + 1]["s"]
        if len(cues) >= 3:
            return _track(cues, True)
    for text in texts:
        if has_lrc_times(text):
            cues = parse_lrc(text)
            if len(cues) >= 3:
                return _track(cues, True)
    for text in texts:
        if text.strip():
            body = LRC_TIME.sub("", text)  # a couple of stray stamps do not make it synced
            cues = plain_cues(body)
            if cues:
                return _track(cues, False)
    return None


def _ffmpeg_srt(path, ords, tmp):
    """Extract subtitle streams `ords` (ordinal among subtitle streams) to SRT files; returns {ord: text}."""
    out = {}

    def run(batch):
        cmd = [*LOW_PRIORITY, "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", path]
        for n in batch:
            cmd += ["-map", f"0:s:{n}", "-f", "srt", os.path.join(tmp, f"{n}.srt")]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=900).returncode == 0

    try:
        ok = run(ords)
        if not ok and len(ords) > 1:  # one bad (e.g. bitmap) track must not lose the others
            for n in ords:
                run([n])
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("lyrics: ffmpeg %s: %s", path, exc)
    for n in ords:
        p = os.path.join(tmp, f"{n}.srt")
        if os.path.exists(p):
            with open(p, encoding="utf-8", errors="replace") as fh:
                out[n] = fh.read()
    return out


def read_video_lyrics(path, subs):
    """-> list of track dicts for the file's subtitle tracks (`subs` = the scanner's JSON list)."""
    subs = [s for s in (subs or []) if isinstance(s, dict) and s.get("ord") is not None]
    if not subs:
        return []
    with tempfile.TemporaryDirectory(prefix="atv-lyr-") as tmp:
        texts = _ffmpeg_srt(path, [s["ord"] for s in subs], tmp)
    tracks = []
    for s in subs:
        cues = parse_srt(texts.get(s["ord"], ""))
        if cues:
            tracks.append(_track(cues, True, s["ord"], (s.get("lang") or "").lower(), s.get("title") or "",
                                 bool(s.get("is_default")), bool(s.get("forced"))))
    return tracks


def extract(path, kind, subs_json):
    """-> {"tracks": [...]} for one library file."""
    if kind == "video":
        try:
            subs = json.loads(subs_json or "null")
        except ValueError:
            subs = None
        tracks = read_video_lyrics(path, subs)
    else:
        track = read_tag_lyrics(path)
        tracks = [track] if track else []
    return {"tracks": [t for t in tracks if t["cues"]]}  # a track that was only junk is no lyrics


def level(data):
    tracks = (data or {}).get("tracks") or []
    if any(t["synced"] for t in tracks):
        return 2
    return 1 if tracks else 0


def ensure_current(con):
    """The cleaning rules changed (CLEAN_VERSION): forget everything read so far, the job reads it again."""
    row = con.execute("SELECT value FROM meta WHERE key = 'lyrics_version'").fetchone()
    if row and row[0] == CLEAN_VERSION:
        return False
    con.execute("UPDATE items SET lyr = NULL")
    con.execute("DELETE FROM lyrics")
    con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('lyrics_version', ?)", (CLEAN_VERSION,))
    con.commit()
    return True


def store(con, item_id, data):
    lvl = level(data)
    if lvl:
        con.execute("INSERT OR REPLACE INTO lyrics (item_id, data) VALUES (?, ?)",
                    (item_id, json.dumps(data, ensure_ascii=False, separators=(",", ":"))))
    else:
        con.execute("DELETE FROM lyrics WHERE item_id = ?", (item_id,))
    con.execute("UPDATE items SET lyr = ? WHERE id = ?", (lvl, item_id))
    return lvl


def load(con, item_id):
    row = con.execute("SELECT data FROM lyrics WHERE item_id = ?", (item_id,)).fetchone()
    return json.loads(row[0]) if row else {"tracks": []}


def get_or_extract(db_path, roots, item_id):
    """Lyrics of one item for the API: stored ones, else extracted now (and stored). -> (level, data) or None."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT id, root, relpath, kind, subs, lyr FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            return None
        if row["lyr"] is not None:
            return row["lyr"], load(con, item_id)
        base = roots.get(row["root"])
        data = {"tracks": []}
        if base:
            try:
                data = extract(os.path.join(base, row["relpath"]), row["kind"], row["subs"])
            except Exception as exc:  # unreadable file: no lyrics, do not retry until it changes
                log.warning("lyrics: %s/%s: %s", row["root"], row["relpath"], exc)
        lvl = store(con, item_id, data)
        con.commit()
        return lvl, data
    finally:
        con.close()


def counts(db_path):
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("SELECT lyr, COUNT(*) FROM items GROUP BY lyr").fetchall()
    finally:
        con.close()
    names = {None: "pending", 0: "none", 1: "plain", 2: "synced"}
    return {names[k]: n for k, n in rows}


def run(db_path, roots, on_result=None):
    """Look at every item without a value (visible files first, videos first). Returns how many were done."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    done = 0
    started = time.time()
    try:
        if ensure_current(con):
            log.info("lyrics: cleaning rules changed (version %s), reading every file again", CLEAN_VERSION)
        todo = con.execute("SELECT id, root, relpath, kind, subs FROM items WHERE lyr IS NULL "
                           "ORDER BY hidden, kind DESC, id").fetchall()
        for row in todo:
            if STOP.is_set():
                break
            base = roots.get(row["root"])
            if not base:
                continue
            try:
                data = extract(os.path.join(base, row["relpath"]), row["kind"], row["subs"])
            except Exception as exc:
                log.warning("lyrics: %s/%s: %s", row["root"], row["relpath"], exc)
                data = {"tracks": []}
            lvl = store(con, row["id"], data)
            con.commit()
            if on_result:
                on_result(row["id"], lvl)
            done += 1
    finally:
        con.close()
    if done:
        log.info("lyrics: %d files looked at in %.0f s", done, time.time() - started)
    return done
