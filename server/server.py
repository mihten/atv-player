#!/usr/bin/env python3
"""ATV Player media server.

- Library: scans the read-only roots (scanner.py) into SQLite, served as JSON.
- Streaming: /media/<id> serves the original file with HTTP Range (VLC on the Apple TV plays it as is).
- atv-audio (optional): a sink process (role=sink) plays the TV's sound elsewhere, from /media/<id>?audio=main.
- Artwork: /art/<id> embedded/folder cover for audio, a frame grab for video (cached).
- Queue + remote control: one queue per player kind (Apple TV app = "tv", iPhone app = "phone"), so the
  devices play independently. Player apps connect as role=player&kind=<kind>; the web UI at / is a
  role=remote that controls one of the queues (TV by default). All state changes are pushed over
  WebSocket; nothing polls.
"""
import asyncio
import datetime
import hashlib
import json
import logging
import mimetypes
import os
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import time
from io import BytesIO
from pathlib import Path

from aiohttp import WSMsgType, web

import box_sound
import loudness
import lyrics
import scanner
import settings as settings_mod

# Site config comes from the environment (systemd EnvironmentFile, see server/site.example/). Library folders, their
# display names, skip patterns, the rescan schedule, the server name and the optional admin password live in
# DATA/settings.json and are changed in the web remote; ATV_ROOTS / ATV_ROOT_NAMES only fill that file on the first
# start (JSON {"<root key>": "<path>"}; the keys are stored in the library DB). Without either the library starts empty.
DATA = Path(os.environ.get("ATV_DATA", "/var/lib/atv-media"))
DATA.mkdir(parents=True, exist_ok=True)
SETTINGS = settings_mod.Settings(str(DATA / "settings.json"))
ROOTS = SETTINGS.roots()  # updated IN PLACE when the settings change (other modules hold this dict)
CACHE = Path(os.environ.get("ATV_CACHE", "/var/cache/atv-media"))
AUDIO_DIR = CACHE / "audio"
DB = DATA / "library.db"
STATE_FILE = DATA / "state.json"
STATIC = Path(__file__).resolve().parent / "static"
PORT = int(os.environ.get("ATV_PORT", "8080"))
VERSION = "0.18.1"
ROOT_NAMES = SETTINGS.root_names()  # display name per root key (same in-place rule)
STARTED = time.time()

log = logging.getLogger("atv-media")
mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("audio/x-wavpack", ".wv")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("audio/x-matroska", ".mka")

LIB: dict[int, dict] = {}
LAST_SCAN: dict = {}
SCANNING = False
PLAYERS: dict = {}  # ws -> session kind ("tv" / "phone" / "box")
REMOTES: dict = {}  # ws -> session kind the web remote controls
SINKS: dict = {}  # session kind -> the atv-audio sink's ws (only "tv"; a new sink replaces the old one)
SINK_INFO: dict = {}  # session kind -> {"name", "output"} as last reported by its sink
RELAY_TYPES = ("route", "sound", "lip_sync", "clock", "volume")  # TV -> sink, replayed in this order on sink connect
SINK_RELAY: dict = {}  # session kind -> {type: last message of that type from its TV}
SINK_LOGGED: dict = {}  # session kind -> the last output line logged
SINK_AUDIO: dict = {}  # session kind -> the sink's last audio_info (outputs, headphones; Settings -> atv-audio)
ART_LOCKS: dict[int, asyncio.Lock] = {}

SUMMARY_KEYS = ("id", "kind", "title", "artist", "album", "folder", "root", "duration", "lossless",
                "codec", "acodec", "track", "disc", "year", "width", "height", "hidden", "lufs", "tpeak", "lyr",
                "fps", "dyn", "videos")
FULL_KEYS = SUMMARY_KEYS + ("audio_track", "audio_ord", "sub_track", "sub_ord", "channels", "bitrate")


def summary(it, keys=SUMMARY_KEYS):
    d = {k: it.get(k) for k in keys}
    d["lossless"] = bool(it.get("lossless"))
    d["hidden"] = bool(it.get("hidden"))
    d["acodec"] = it.get("acodec") or None  # "" = video without audio
    d["fps"] = it.get("fps") or None  # 0 = unknown / song
    d["dyn"] = it.get("dyn") or None  # "sdr" / "pq" / "hlg" / "dv"; "" = song
    d["art"] = bool(it.get("has_art"))
    d["videos"] = (video_count(it["id"]) or None) if it["kind"] == "audio" else None  # linked music videos
    d["loved"] = is_loved(it["id"])
    d["audio_copy"] = it["id"] in AUDIO_COPIES  # web remote badge: the sink's main-track copy exists
    return d


def full(it):
    d = summary(it, FULL_KEYS)
    try:
        d["subs"] = json.loads(it.get("subs") or "null")
    except ValueError:
        d["subs"] = None
    d["versions"] = [{"id": i, "kind": LIB[i]["kind"], "label": VERSION_LABEL.get(i, ""),
                      "duration": LIB[i].get("duration")} for i in VERSIONS.get(it["id"], [])] or None
    return d


def sort_key(it):
    return (it["root"], it["folder"].lower(), it.get("disc") or 0, it.get("track") or 0,
            (it.get("artist") or "").lower(), it["title"].lower())


def video_key(it):
    return ((it.get("artist") or "").lower(), it["title"].lower())


def load_library():
    con = scanner.connect(DB)
    rows = con.execute("SELECT * FROM items").fetchall()
    meta = con.execute("SELECT value FROM meta WHERE key = 'last_scan'").fetchone()
    con.close()
    LIB.clear()
    for r in rows:
        LIB[r["id"]] = dict(r)
    LAST_SCAN.clear()
    if meta:
        LAST_SCAN.update(json.loads(meta["value"]))
    link_versions()


# ---------------------------------------------------------------- song <-> music video versions
# A song and its music videos (official, live, acoustic, lyric video ...) form one group: same normalised artist (main
# artist, without "feat. ...") and title (scanner.norm drops the "(...)" / "[...]" parts). Rebuilt after every
# load_library; hidden duplicates are not members. Shared/LocalLibrary.swift (VersionLinks) has the same rules.

VERSIONS: dict[int, list[int]] = {}  # item id -> every member of its group (2+ members), songs first
VERSION_LABEL: dict[int, str] = {}  # item id -> "Official video", "Live at Wembley 1986", "Song", "Video" ...
VERSION_CLASS: dict[int, int] = {}  # 0 official / plain / remaster, 1 lyric / visualizer / audio, 2 live / other

FEAT_RE = re.compile(r"\s+(?:feat\.|ft\.|featuring\s).*$", re.I)
BRACKET_RE = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")
NOISE_WORDS = {"official", "music", "video", "hd", "4k", "hq"}


def version_key(it):
    artist = scanner.norm(FEAT_RE.sub("", it.get("artist") or ""))
    title = scanner.norm(FEAT_RE.sub("", it.get("title") or ""))
    return (artist, title) if artist and title else None


def version_label(title, kind):
    """(label, class) from the bracket text of a title, e.g. "(Live at Wembley 1986)" -> ("Live at Wembley 1986", 2)."""
    parts, official = [], False
    for text in BRACKET_RE.findall(title or ""):
        words = text.split()
        if not words or words[0].lower().rstrip(".") in ("feat", "ft", "featuring", "with"):
            continue
        official = official or any(w.lower() == "official" for w in words)
        rest = " ".join(w for w in words if w.lower() not in NOISE_WORDS)
        if rest:
            parts.append(rest)
    text = " · ".join(parts)
    low = text.lower()
    words = re.findall(r"[a-z0-9]+", low)
    if any(w == "live" or w.startswith(("acoustic", "unplugged", "remix")) for w in words):
        cls = 2
    elif any(w == "audio" or w.startswith(("lyric", "visuali")) for w in words):
        cls = 1
    elif not words or any(w.startswith("remaster") for w in words):
        cls = 0
    else:
        cls = 2
    if not text:
        text = "Official video" if official and kind == "video" else ("Video" if kind == "video" else "Song")
    elif kind == "video" and low in ("lyric", "lyrics"):
        text = "Lyric video"
    return text[:1].upper() + text[1:], cls


def link_versions():
    VERSIONS.clear()
    VERSION_LABEL.clear()
    VERSION_CLASS.clear()
    groups: dict = {}
    for it in LIB.values():
        if it["hidden"]:
            continue
        key = version_key(it)
        if key:
            groups.setdefault(key, []).append(it["id"])
    for members in groups.values():
        if len(members) < 2:
            continue
        for i in members:
            VERSION_LABEL[i], VERSION_CLASS[i] = version_label(LIB[i]["title"], LIB[i]["kind"])
        members.sort(key=lambda i: (LIB[i]["kind"] != "audio", VERSION_CLASS[i], VERSION_LABEL[i].lower(), i))
        for i in members:
            VERSIONS[i] = members


def video_count(item_id):
    return sum(1 for i in VERSIONS.get(item_id, []) if LIB[i]["kind"] == "video")


def preferred_video(item_id):
    """The video "Prefer videos" plays instead of a song: the first one of the song's own class, else the best one."""
    it = LIB.get(item_id)
    if not it or it["kind"] != "audio":
        return None
    videos = [i for i in VERSIONS.get(item_id, []) if LIB[i]["kind"] == "video"]
    own = VERSION_CLASS.get(item_id, 0)
    return next((i for i in videos if VERSION_CLASS[i] == own), videos[0] if videos else None)


def collection(name, root=None, folder=None, recursive=True, playlist=None):
    if name in AUTO_NAMES:  # auto-playlists from the play history and the hearts
        return auto_items(name)
    if name == "playlist":  # playlist order; hidden duplicates stay playable, vanished files are skipped
        pl = PLAYLISTS.get(playlist) if playlist is not None else None
        return [LIB[i] for i in pl["items"] if i in LIB] if pl else []
    items = [it for it in LIB.values() if not it["hidden"]]
    if name == "music":
        items = [it for it in items if it["kind"] == "audio"]
    elif name == "videos":
        items = [it for it in items if it["kind"] == "video"]
        return sorted(items, key=video_key)
    elif name == "folder":
        folder = (folder or "").strip("/")
        if recursive:
            items = [it for it in items if it["root"] == root and
                     (not folder or it["folder"] == folder or it["folder"].startswith(folder + "/"))]
        else:
            items = [it for it in items if it["root"] == root and it["folder"] == folder]
    return sorted(items, key=sort_key)


# ---------------------------------------------------------------- sessions (one queue per device kind)
# Each kind of player app has its OWN queue: "tv" (the Apple TV app; also any player that sends no kind,
# i.e. apps older than 0.11) and "phone" (the iPhone app). They play independently, also at the same
# time. Several players of one kind would share that kind's queue (this home has one of each).
# The TV session keeps the original state.json; other kinds use state-<kind>.json. A web remote controls
# one session at a time: ?session=<kind> on /ws or a {"type": "select", "session": <kind>} message.

STATE_VERSION = 2  # 2 = repeat "all" is the default (older state files are migrated once)
KINDS = ("tv", "phone", "box")
# "box": the atv-audio program plays this queue by itself (player role kind=box), the web remote is its only UI.
KIND_NAMES = {"tv": "Apple TV", "phone": "iPhone", "box": "atv-audio"}
SAVED_KEYS = ("queue", "original", "index", "shuffle", "repeat", "position", "source", "source_items",
              "prefer_videos", "swapped", "pinned", "dropped", "noted", "sound")


class Session:
    def __init__(self, kind):
        self.kind = kind
        self.file = STATE_FILE if kind == "tv" else DATA / f"state-{kind}.json"
        self.queue: list[int] = []
        self.original: list[int] = []
        self.index = -1
        self.shuffle = False
        # off | all | one. Default "all": playlists, folders and collections play endlessly
        # (user feedback, 2026-10-19); the 🔁 buttons still cycle all → one → off.
        self.repeat = "all"
        self.playing = False
        self.want_play = False
        self.position = 0.0
        self.position_at = time.time()
        self.duration = 0.0
        self.errors = 0
        # Playlist id the queue was started from (None = anything else), and its playable ids at that time:
        # an edit of that playlist rebuilds the upcoming part of the queue (follow_playlist).
        self.source = None
        self.source_items: list[int] = []
        self.noted = None  # item whose current start is already in the play history
        # "Prefer videos": songs in the queue are played as one of their music videos (prefer_list). swapped: queue
        # entry -> the song it replaced (also for a version picked by hand); pinned: entries picked by hand with
        # switch_version, which the flag never changes back; dropped: video -> songs left out for it ("no doubles"),
        # put back when the flag goes off.
        self.prefer_videos = False
        self.swapped: dict[int, int] = {}
        self.pinned: list[int] = []
        self.dropped: dict[int, list[int]] = {}
        # box only: its sound settings (box_sound.clean) and the notice shown in the web remote until the next Play
        self.sound = dict(box_sound.DEFAULT)
        self.notice = None

    @property
    def players(self):
        return [ws for ws, kind in PLAYERS.items() if kind == self.kind]

    @property
    def listeners(self):
        """Players plus the session's sink: everything that follows load/control/state."""
        sink = SINKS.get(self.kind)
        return self.players + ([sink] if sink is not None else [])

    @property
    def current_id(self):
        return self.queue[self.index] if 0 <= self.index < len(self.queue) else None

    @property
    def current(self):
        cid = self.current_id
        return LIB.get(cid) if cid is not None else None

    def to_dict(self):
        cur = self.current
        upcoming = [summary(LIB[i]) for i in self.queue[self.index + 1:self.index + 6] if i in LIB]
        return {
            "session": self.kind, "session_name": KIND_NAMES[self.kind], "sessions": sessions_summary(),
            "queue": self.queue, "index": self.index, "shuffle": self.shuffle, "repeat_mode": self.repeat,
            "prefer_videos": self.prefer_videos,
            "playing": self.playing, "position": self.position, "position_at": self.position_at,
            "server_time": time.time(), "duration": self.duration,
            "current": full(cur) if cur else None, "upcoming": upcoming,
            "players": len(self.players), "scanning": SCANNING, "last_scan": LAST_SCAN, "version": VERSION,
            "server_name": server_name(),
            "sink": sink_state(self.kind) if self.kind == "tv" else box_sink_state() if self.kind == "box" else None,
            **(box_extra(self) if self.kind == "box" else {}),
        }

    def save(self):
        data = {k: getattr(self, k) for k in SAVED_KEYS}
        data["state_version"] = STATE_VERSION
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self.file)

    def load(self):
        try:
            data = json.loads(self.file.read_text())
        except (OSError, ValueError):
            return
        version = data.get("state_version", 1)
        for k in SAVED_KEYS:  # unknown keys (e.g. "output" from server 0.9.0) are ignored
            if k in data:
                setattr(self, k, data[k])
        if version < 2:  # saved before endless playback became the default: switch it on once
            self.repeat = "all"
        self.prefer_videos = bool(self.prefer_videos)
        self.swapped = {int(k): int(v) for k, v in (self.swapped or {}).items()}  # JSON keys are strings
        self.pinned = [int(i) for i in self.pinned or []]
        self.dropped = {int(k): [int(i) for i in v] for k, v in (self.dropped or {}).items()}
        self.sound = box_sound.clean(self.sound)
        self.prune()

    def prune(self):
        """Drop ids that vanished from the library (after a rescan)."""
        cur = self.current_id
        self.queue = [i for i in self.queue if i in LIB]
        self.original = [i for i in self.original if i in LIB]
        self.swapped = {k: v for k, v in self.swapped.items() if k in LIB and v in LIB}
        self.pinned = [i for i in self.pinned if i in LIB]
        self.dropped = {k: [i for i in v if i in LIB] for k, v in self.dropped.items() if k in LIB}
        if cur in self.queue:
            self.index = self.queue.index(cur)
        elif self.queue:
            self.index = min(max(self.index, 0), len(self.queue) - 1)
        else:
            self.index = -1


SESSIONS = {kind: Session(kind) for kind in KINDS}


# ---------------------------------------------------------------- play history (shuffle with memory)
# A plain random.shuffle forgets what played before: every new "Shuffle ..." and every repeat-all lap was a fresh
# permutation, so recently heard songs came back far too often (user report 2026-10-26). The server remembers when each
# item last started playing (shared by all sessions, DATA/history.json, entries expire after HISTORY_DAYS) and every
# shuffle puts the least recently heard items first.

HISTORY_FILE = DATA / "history.json"
HISTORY_DAYS = 30
PLAYED: dict[int, float] = {}  # item id -> time it last started playing


def load_history():
    try:
        data = json.loads(HISTORY_FILE.read_text())
    except (OSError, ValueError):
        return
    cutoff = time.time() - HISTORY_DAYS * 86400
    PLAYED.clear()
    PLAYED.update({int(k): float(v) for k, v in data.get("played", {}).items() if float(v) > cutoff})


def save_history():
    tmp = HISTORY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"played": {str(k): round(v, 1) for k, v in PLAYED.items()}}))
    tmp.replace(HISTORY_FILE)


def note_played(item_id):
    now = time.time()
    PLAYED[item_id] = now
    note_play(item_id, now)
    cutoff = now - HISTORY_DAYS * 86400
    for k in [k for k, v in PLAYED.items() if v <= cutoff]:
        del PLAYED[k]
    try:
        save_history()
    except OSError as e:
        log.warning("history not saved: %s", e)


def smart_shuffle(ids):
    """Shuffled copy of ids that keeps recently heard items away from the front: items not played within HISTORY_DAYS
    first (random order), then the recently played ones split by age into an older and a newer half, each half shuffled.
    The items heard last therefore come last, also on a repeat-all lap, while a small, often played playlist still gets a
    different order every time."""
    cutoff = time.time() - HISTORY_DAYS * 86400
    fresh, recent = [], []
    for i in ids:
        (recent if PLAYED.get(i, 0) > cutoff else fresh).append(i)
    random.shuffle(fresh)
    recent.sort(key=lambda i: PLAYED[i])
    half = len(recent) // 2
    older, newer = recent[:half], recent[half:]
    random.shuffle(older)
    random.shuffle(newer)
    return fresh + older + newer


# ---------------------------------------------------------------- listening history and hearts (auto-playlists)
# DATA/listening.db keeps every start for good (plays) and the hearts (loved), for the auto-playlists (Loved, Most played,
# Recently added, Not heard in a while, On this day). PLAYED above stays the 30-day shuffle memory. On first start the
# last plays of history.json are imported as plays.

LISTEN_DB = DATA / "listening.db"
LISTEN = None  # sqlite3 connection (listening())
PLAY_COUNT: dict[int, int] = {}  # item id -> number of starts
LAST_PLAY: dict[int, float] = {}  # item id -> last start (no expiry)
LOVED: dict[int, float] = {}  # item id -> when it got its heart
AUTO_LISTS = (("loved", "Loved"), ("most_played", "Most played"), ("recent", "Recently added"),
              ("forgotten", "Not heard in a while"), ("on_this_day", "On this day"))
AUTO_NAMES = {key for key, _ in AUTO_LISTS}
AUTO_SIZE = 100
FORGOTTEN_DAYS = 90
FIRST_SCAN_GAP = 600  # s without a new item ends the first scan (its items all got about the same "added")


def listening():
    global LISTEN
    if LISTEN is None:
        DATA.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(LISTEN_DB)
        con.executescript("""
            CREATE TABLE IF NOT EXISTS plays (id INTEGER NOT NULL, at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS plays_at ON plays (at);
            CREATE TABLE IF NOT EXISTS loved (id INTEGER PRIMARY KEY, at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        LISTEN = con
    return LISTEN


def load_listening():
    con = listening()
    if con.execute("SELECT 1 FROM meta WHERE key = 'history_imported'").fetchone() is None:
        try:
            played = json.loads(HISTORY_FILE.read_text()).get("played") or {}
        except (OSError, ValueError, AttributeError):
            played = {}
        rows = []
        for k, v in played.items():
            try:
                rows.append((int(k), float(v)))
            except (TypeError, ValueError):
                pass
        con.executemany("INSERT INTO plays (id, at) VALUES (?, ?)", rows)
        con.execute("INSERT INTO meta (key, value) VALUES ('history_imported', ?)", (str(len(rows)),))
        con.commit()
        log.info("listening history: %d plays imported from %s", len(rows), HISTORY_FILE.name)
    PLAY_COUNT.clear()
    LAST_PLAY.clear()
    LOVED.clear()
    for i, n, last in con.execute("SELECT id, COUNT(*), MAX(at) FROM plays GROUP BY id"):
        PLAY_COUNT[i], LAST_PLAY[i] = n, last
    LOVED.update(dict(con.execute("SELECT id, at FROM loved").fetchall()))


def note_play(item_id, at):
    PLAY_COUNT[item_id] = PLAY_COUNT.get(item_id, 0) + 1
    LAST_PLAY[item_id] = max(at, LAST_PLAY.get(item_id, 0))
    try:
        con = listening()
        con.execute("INSERT INTO plays (id, at) VALUES (?, ?)", (item_id, at))
        con.commit()
    except sqlite3.Error as e:
        log.warning("play not saved: %s", e)


def is_loved(item_id):
    """The heart is per version group: a song loved shows as loved while its video plays (and the other way round)."""
    return any(i in LOVED for i in VERSIONS.get(item_id, (item_id,)))


def set_loved(item_id, on):
    """Heart on / off. On marks the item, off clears every member of its group. Returns False when nothing changed."""
    if on == is_loved(item_id):
        return False
    now = time.time()
    gone = [i for i in VERSIONS.get(item_id, (item_id,)) if i in LOVED]
    try:
        con = listening()
        if on:
            con.execute("INSERT OR REPLACE INTO loved (id, at) VALUES (?, ?)", (item_id, now))
        else:
            con.executemany("DELETE FROM loved WHERE id = ?", [(i,) for i in gone])
        con.commit()
    except sqlite3.Error as e:
        log.warning("heart not saved: %s", e)
        return False
    if on:
        LOVED[item_id] = now
    else:
        for i in gone:
            LOVED.pop(i, None)
    return True


def group_last_play(item_id):
    """Last start of any version of the item (a song heard as its video counts as heard)."""
    return max(LAST_PLAY.get(i, 0) for i in VERSIONS.get(item_id, (item_id,)))


def added_time(items):
    """Item id -> time it came into the library. The first scan gives every item about the same "added" (one block
    without a FIRST_SCAN_GAP pause from the earliest one): those items use their file time instead, never later than
    "added" (a retagged old file gets a new mtime)."""
    first_end, prev = None, None
    for it in sorted(items, key=lambda it: it["added"]):
        if prev is not None and it["added"] - prev > FIRST_SCAN_GAP:
            break
        prev = first_end = it["added"]
    first = first_end is not None
    return {it["id"]: (min(it["mtime"], it["added"]) if first and it["added"] <= first_end else it["added"])
            for it in items}


def on_this_day(now=None):
    """Ids played within 3 days of today's date in earlier years, newest year first (in a year: last played first)."""
    now = now or time.time()
    con = listening()
    first = con.execute("SELECT MIN(at) FROM plays").fetchone()[0]
    if first is None:
        return []
    today = datetime.date.fromtimestamp(now)
    out, seen = [], set()
    for year in range(today.year - 1, datetime.date.fromtimestamp(first).year - 2, -1):
        try:
            day = today.replace(year=year)
        except ValueError:  # 29 February
            day = today.replace(year=year, day=28)
        lo = time.mktime((day - datetime.timedelta(days=3)).timetuple())
        hi = min(time.mktime((day + datetime.timedelta(days=4)).timetuple()), now - 300 * 86400)
        rows = con.execute("SELECT id, MAX(at) AS last FROM plays WHERE at >= ? AND at < ? GROUP BY id "
                           "ORDER BY last DESC", (lo, hi))
        for i, _ in rows:
            if i in LIB and i not in seen:
                seen.add(i)
                out.append(i)
    return out[:AUTO_SIZE]


def auto_items(name, now=None):
    """Items of an auto-playlist, in its order. History lists keep hidden duplicates that were played (like playlists)."""
    now = now or time.time()
    if name == "loved":  # once per version group, by its newest loved member
        ids, seen = [], set()
        for i in sorted((i for i in LOVED if i in LIB), key=lambda i: -LOVED[i]):
            if i not in seen:
                seen.update(VERSIONS.get(i, (i,)))
                ids.append(i)
    elif name == "most_played":
        ids = sorted((i for i in PLAY_COUNT if i in LIB), key=lambda i: (-PLAY_COUNT[i], -LAST_PLAY[i]))[:AUTO_SIZE]
    elif name == "recent":
        visible = [it for it in LIB.values() if not it["hidden"]]
        added = added_time(visible)
        ids = sorted(added, key=lambda i: (-added[i], -i))[:AUTO_SIZE]
    elif name == "forgotten":
        cutoff = now - FORGOTTEN_DAYS * 86400
        ids = sorted(it["id"] for it in LIB.values() if not it["hidden"] and group_last_play(it["id"]) < cutoff)
        random.Random(datetime.date.fromtimestamp(now).isoformat()).shuffle(ids)  # same order all day
        ids = ids[:AUTO_SIZE]
    elif name == "on_this_day":
        try:
            ids = on_this_day(now)
        except sqlite3.Error as e:
            log.warning("on this day: %s", e)
            ids = []
    else:
        return []
    return [LIB[i] for i in ids]


def auto_lists():
    """The auto-playlists for the menus: non-empty ones, Loved always."""
    out = []
    for key, name in AUTO_LISTS:
        items = auto_items(key)
        if items or key == "loved":
            entry = {"key": key, "name": name, "count": len(items),
                     "first_art": next((it["id"] for it in items if it.get("has_art")), None)}
            if key == "loved":  # every item shown with a heart (the web remote's rows): loved ones and their versions
                entry["ids"] = sorted({j for it in items for j in VERSIONS.get(it["id"], (it["id"],))})
            out.append(entry)
    return out


def session_for(kind):
    return SESSIONS.get(kind) or SESSIONS["tv"]


def sessions_summary():
    out = []
    for kind, s in SESSIONS.items():
        cur = s.current
        out.append({"session": kind, "name": KIND_NAMES[kind], "players": len(s.players), "playing": s.playing,
                    "title": cur["title"] if cur else None})
    return out


# ---------------------------------------------------------------- playlists
# Stored in DATA/playlists.json as item ids (ids survive renames/moves, see scanner.carry_over_ids).
# Ids whose file vanished stay in the list (shown as missing, skipped when playing), so a file that
# comes back keeps its place. Managed over HTTP (/api/playlists...); every change is pushed to all
# clients as {"type": "playlists"} so open web remotes reload.

PLAYLISTS_FILE = DATA / "playlists.json"
PLAYLISTS: dict[int, dict] = {}
NEXT_PLAYLIST_ID = 1


def load_playlists():
    global NEXT_PLAYLIST_ID
    try:
        data = json.loads(PLAYLISTS_FILE.read_text())
    except (OSError, ValueError):
        return
    PLAYLISTS.clear()
    for pl in data.get("playlists", []):
        PLAYLISTS[int(pl["id"])] = {"id": int(pl["id"]), "name": str(pl["name"]),
                                    "items": [int(i) for i in pl.get("items", [])],
                                    "created": pl.get("created") or time.time(),
                                    "updated": pl.get("updated") or time.time()}
    NEXT_PLAYLIST_ID = max([data.get("next_id", 1)] + [i + 1 for i in PLAYLISTS])


def save_playlists():
    data = {"next_id": NEXT_PLAYLIST_ID, "playlists": sorted(PLAYLISTS.values(), key=lambda p: p["id"])}
    tmp = PLAYLISTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(PLAYLISTS_FILE)


def playlist_summary(pl):
    present = [LIB[i] for i in pl["items"] if i in LIB]
    return {"id": pl["id"], "name": pl["name"], "count": len(pl["items"]), "missing": len(pl["items"]) - len(present),
            "videos": sum(1 for it in present if it["kind"] == "video"),
            "duration": sum(it.get("duration") or 0 for it in present),
            "first_art": next((it["id"] for it in present if it.get("has_art")), None),
            "created": pl["created"], "updated": pl["updated"]}


def playlist_detail(pl):
    d = playlist_summary(pl)
    d["items"] = [summary(LIB[i]) if i in LIB else {"id": i, "missing": True, "title": f"Missing file (#{i})"}
                  for i in pl["items"]]
    return d


def follow_playlist(s, pl_id):
    """The playlist this session's queue came from was edited: rebuild the queue from its new contents. The current
    item keeps playing (also when it was removed); items already played this round stay behind it; with shuffle the
    rest is shuffled again, without it the playlist order is used from the current item on. Deleted / emptied = stop
    following (the queue stays as it is)."""
    pl = PLAYLISTS.get(pl_id)
    new = [i for i in pl["items"] if i in LIB] if pl else []
    if not new:
        s.source, s.source_items = None, []
        return False
    if new == s.source_items:  # rename only
        return False
    playing = s.current_id
    # Compared and rebuilt with the playlist's own entries: a version swap counts as the song it replaced.
    queue = [s.swapped.get(i, i) for i in s.queue]
    cur = queue[s.index] if 0 <= s.index < len(queue) else None
    if s.shuffle:
        # Played part: each id once, in the order first heard, and never the current item (it is playing again, not
        # behind). Without this a playlist that holds an entry twice could put that id twice into the rebuilt queue.
        keep, seen, played = set(new), {cur}, []
        for i in queue[:max(s.index, 0)]:
            if i in keep and i not in seen:
                seen.add(i)
                played.append(i)
        rest = list(new)
        for i in played + ([cur] if cur is not None else []):
            if i in rest:
                rest.remove(i)
        rest = smart_shuffle(rest)
        s.queue = played + ([cur] if cur is not None else []) + rest
        s.index = len(played) if cur is not None else 0
    elif cur in new:
        s.queue, s.index = list(new), new.index(cur)
    else:
        keep = set(new)
        k = sum(1 for i in queue[:max(s.index, 0)] if i in keep)
        s.queue = new[:k] + ([cur] if cur is not None else []) + new[k:]
        s.index = k
    s.original, s.source_items = list(new), list(new)
    s.dropped = {}  # rebuilt from the playlist's own entries
    if playing is not None and playing != cur:
        # The current entry stays the version that is playing; the playlist's own entry of that video goes (no doubles).
        other = next((n for n, i in enumerate(s.queue) if i == playing and n != s.index), None)
        if other is not None:
            del s.queue[other]
            if other < s.index:
                s.index -= 1
        s.queue[s.index] = playing
        if cur in s.original:
            if playing in s.original:
                s.original.remove(playing)
            s.original[s.original.index(cur)] = playing
    # The rest of the queue is the playlist's own entries now: only the playing version can still stand for a song
    # (a stale swap would later turn a video added to the playlist into its song).
    s.swapped = {k: v for k, v in s.swapped.items() if k == playing and playing != cur}
    if s.prefer_videos:
        s.queue, where = prefer_list(s, s.queue, set(s.queue), keep=s.index)
        s.index = where[s.index] if s.queue else -1
        at = s.original.index(playing) if playing in s.original else None
        s.original, _ = prefer_list(s, s.original, set(s.original), keep=at)
    log.info("[%s] queue follows playlist %s: %d items now, current %s kept", s.kind, pl_id, len(s.queue), playing)
    return True


async def playlists_changed(pl_id=None):
    save_playlists()
    for s in SESSIONS.values():
        if pl_id is not None and s.source == pl_id and follow_playlist(s, pl_id):
            s.save()
            await broadcast_state(s)
    msg = {"type": "playlists", "id": pl_id}
    for ws in list(PLAYERS) + list(SINKS.values()) + list(REMOTES):
        await send(ws, msg)


async def loved_changed():
    """A heart changed: every queue's state (current / upcoming carry "loved"), and {"type": "auto"} to every client
    so open auto-playlist screens reload."""
    await broadcast_all()
    for ws in list(PLAYERS) + list(SINKS.values()) + list(REMOTES):
        await send(ws, {"type": "auto"})


async def send(ws, msg):
    try:
        await ws.send_str(json.dumps(msg))
    except (ConnectionResetError, RuntimeError):
        pass


async def broadcast_state(s):
    """State of session s to its players and to the remotes watching it."""
    msg = {"type": "state", "state": s.to_dict()}
    for ws in s.listeners + [ws for ws, kind in list(REMOTES.items()) if kind == s.kind]:
        await send(ws, msg)
    prefetch_copies(s)
    if s.kind == "tv" and tv_owns_output():
        await box_yield_to_tv()
    # Remotes watching ANOTHER queue only get who is connected/playing, so a web page in "Auto" mode
    # can follow whichever app starts playing.
    other = {"type": "sessions", "sessions": sessions_summary()}
    for ws, kind in list(REMOTES.items()):
        if kind != s.kind:
            await send(ws, other)


async def broadcast_all():
    """Every session (scan status, player connected/gone: the session list in every state changes)."""
    for s in SESSIONS.values():
        await broadcast_state(s)


def load_message(s, autoplay, position=0.0, present=False):
    """present: the user picked something to play (show the player). False for auto-advance,
    next/previous, error skips and reconnect resyncs."""
    cur = s.current
    return {"type": "load", "item": full(cur) if cur else None, "position": position, "autoplay": autoplay,
            "present": present}


async def to_players(s, msg):
    for ws in s.listeners:
        await send(ws, msg)


# ---------------------------------------------------------------- atv-audio sink
# A separate process (server/sink/atv_audio.py) plays the TV's sound on another output. It connects as role=sink to
# session "tv", follows the same load/control/state as the players, gets the TV's route/sound/lip_sync/clock messages
# relayed and reports its output; it never counts as a player.

def sink_state(kind):
    # route: the TV's Sound output is atv-audio (its last route message; false once the TV is gone)
    route = bool((SINK_RELAY.get(kind, {}).get("route") or {}).get("sink"))
    if SINKS.get(kind) is None:
        return {"connected": False, "name": None, "output": None, "route": route, "audio": None}
    info = SINK_INFO.get(kind) or {}
    return {"connected": True, "name": info.get("name"), "output": info.get("output"), "route": route,
            "audio": SINK_AUDIO.get(kind)}


def clean_audio_info(data):
    """The sink's audio_info: {outputs: [{key, name, kind, codec, default}], devices: [{address, name, paired, connected,
    audio}], scanning, busy, error, can_bluetooth, can_outputs, dongle: {name, state, codec, firmware} | None (a USB
    Bluetooth dongle that reports its headphones, e.g. BTD 700)}. Bounded and typed, the rest dropped."""
    def s(v, n=120):
        return str(v)[:n] if v is not None else None
    outs = [{"key": s(o.get("key"), 200), "name": s(o.get("name")), "kind": s(o.get("kind"), 20),
             "codec": s(o.get("codec"), 40), "default": bool(o.get("default"))}
            for o in (data.get("outputs") or [])[:20] if isinstance(o, dict) and o.get("key")]
    devs = [{"address": s(d.get("address"), 40), "name": s(d.get("name")), "paired": bool(d.get("paired")),
             "connected": bool(d.get("connected")), "audio": bool(d.get("audio"))}
            for d in (data.get("devices") or [])[:40] if isinstance(d, dict) and d.get("address")]
    dg = data.get("dongle")
    dongle = ({"name": s(dg.get("name"), 60), "state": s(dg.get("state"), 20), "codec": s(dg.get("codec"), 40),
               "firmware": s(dg.get("firmware"), 20)} if isinstance(dg, dict) else None)
    return {"outputs": outs, "devices": devs, "scanning": bool(data.get("scanning")), "busy": s(data.get("busy"), 200),
            "error": s(data.get("error"), 300), "can_bluetooth": bool(data.get("can_bluetooth")),
            "can_outputs": bool(data.get("can_outputs")), "dongle": dongle}


# ---------------------------------------------------------------- the "atv-audio" session (box)
# The same atv-audio program also joins as a PLAYER of session "box" and plays that queue by itself. One output:
# the TV wins (owner decision). While the TV plays with its Sound output = atv-audio, the box is paused and a Play
# for the box is refused with a notice; the box plays again only on a new Play. Headphone buttons (the sink's
# `button` messages) act on the session that owns the output: the TV while its sound goes to atv-audio, else the box.

IN_USE_NOTICE = "atv-audio is in use by the Apple TV: paused"
BOX_CURVES = {v: box_sound.curve(v) for v, _ in box_sound.choices() if v not in ("off", "custom")}  # editor: start from


def tv_routed():
    return bool((SINK_RELAY.get("tv", {}).get("route") or {}).get("sink")) and bool(SESSIONS["tv"].players)


def tv_owns_output():
    """The TV plays (or is starting) with its sound on atv-audio."""
    tv = SESSIONS["tv"]
    return tv_routed() and (tv.playing or tv.want_play)


def box_sink_state():
    """The box always plays through the sink: its status in the box state (route = it is the box's output)."""
    st = sink_state("tv")
    return dict(st, route=st["connected"], in_use_by_tv=tv_owns_output())


def box_extra(s):
    cur = s.current
    return {"sound": s.sound, "sound_presets": box_sound.choices(), "sound_curves": BOX_CURVES,
            "sound_label": box_sound.label(s.sound, cur), "notice": s.notice}


async def box_send_sound(s):
    """The box player's `sound` (from its settings and the current track's loudness); sent before every load."""
    if s.kind == "box":
        msg = box_sound.message(s.sound, s.current)
        for ws in s.players:
            await send(ws, msg)


async def box_notice(s, text):
    s.notice = {"text": text, "at": time.time()}
    log.info("[box] notice: %s", text)
    await broadcast_state(s)


async def box_yield_to_tv():
    """The TV took the output: pause the box (it does not resume by itself)."""
    box = SESSIONS["box"]
    if box.want_play or box.playing:
        box.want_play = box.playing = False
        await to_players(box, {"type": "control", "action": "pause"})
        await box_notice(box, IN_USE_NOTICE)


def owner_session(asked=None):
    """Session the headphone buttons act on: the sink names the one that holds its output; checked here."""
    if asked == "tv" and tv_routed():
        return SESSIONS["tv"]
    if asked == "box" and SESSIONS["box"].players:
        return SESSIONS["box"]
    return SESSIONS["tv"] if tv_routed() else SESSIONS["box"]


def live_position(s):
    return s.position + (time.time() - s.position_at if s.playing else 0.0)


async def on_button(data):
    """Headphone button from the sink: {cmd play|pause|next|prev|seek, session}; seek has delta (s, +- 60)."""
    c = data.get("cmd")
    if c not in ("play", "pause", "next", "prev", "seek"):
        return
    s = owner_session(data.get("session"))
    msg: dict = {"cmd": c}
    extra = ""
    if c == "prev":
        msg["position"] = live_position(s)
    elif c == "seek":
        if s.current is None:
            return
        try:
            delta = max(-60.0, min(60.0, float(data.get("delta"))))
        except (TypeError, ValueError, OverflowError):
            return
        dur = s.duration or (s.current or {}).get("duration") or 0
        pos = live_position(s) + delta
        msg["position"] = max(0.0, min(pos, dur - 1.0) if dur > 1 else pos)
        s.position, s.position_at = msg["position"], time.time()  # a second step before the next status adds up
        extra = f" {delta:+.0f} s -> {msg['position']:.1f}"
    log.info("button: %s%s -> %s (%s)", c, extra, KIND_NAMES[s.kind], s.kind)
    await handle_cmd(s, msg)


def clean_volume(data):
    """{percent 0..100, muted} or None."""
    try:
        percent = max(0, min(100, int(round(float(data.get("percent"))))))
    except (TypeError, ValueError, AttributeError, OverflowError):  # OverflowError: Infinity / 1e999
        return None
    return {"percent": percent, "muted": bool(data.get("muted"))}


def clean_output(out):
    if not isinstance(out, dict):
        return None
    kind = out.get("kind") if out.get("kind") in ("bluetooth", "usb", "other") else "other"
    codec = out.get("codec")
    clean = {"kind": kind, "name": str(out.get("name") or "")[:120],
             "codec": str(codec)[:40] if codec else None, "ready": bool(out.get("ready"))}
    vol = clean_volume({"percent": out.get("volume"), "muted": out.get("muted")})
    if vol is not None:  # the output's own volume (sink 0.16.6+)
        clean["volume"], clean["muted"] = vol["percent"], vol["muted"]
    return clean


def pong(data):
    return {"type": "pong", "n": data.get("n"), "t": time.monotonic()}


async def sink_connected(s, ws):
    old = SINKS.get(s.kind)
    SINKS[s.kind] = ws
    SINK_INFO.pop(s.kind, None)
    SINK_LOGGED.pop(s.kind, None)
    if old is not None:
        log.info("sink (%s) replaced by a new connection", s.kind)
        asyncio.create_task(old.close(message=b"replaced"))
    await send(ws, {"type": "state", "state": s.to_dict()})
    if s.current and file_exists(s.current):
        await send(ws, load_message(s, s.want_play, s.position))
    for kind in RELAY_TYPES:
        last = SINK_RELAY.get(s.kind, {}).get(kind)
        if last is not None:
            await send(ws, last)
    PREFETCHED.pop(s.kind, None)  # queue copies for what plays now
    await broadcast_state(s)


async def sink_message(s, ws, data):
    kind = data.get("type")
    if SINKS.get(s.kind) is not ws and kind != "ping":  # a replaced sink's late messages
        return
    if kind == "ping":
        await send(ws, pong(data))
    elif kind == "sink":
        SINK_INFO[s.kind] = {"name": str(data.get("name") or "")[:120] or None, "output": clean_output(data.get("output"))}
        out = SINK_INFO[s.kind]["output"]
        if out and out["ready"] and "volume" in out:  # a reconnecting sink gets the last volume again
            SINK_RELAY.setdefault(s.kind, {})["volume"] = {"type": "volume", "percent": out["volume"],
                                                           "muted": out["muted"]}
        line = f"{out['kind']} '{out['name']}' {out['codec'] or 'codec unknown'} ready={out['ready']}" if out else "none"
        if SINK_LOGGED.get(s.kind) != line:  # volume-only changes (a slider) are not logged
            SINK_LOGGED[s.kind] = line
            log.info("sink[%s]: %s, output %s", s.kind, SINK_INFO[s.kind]["name"], line)
        await broadcast_state(s)
    elif kind == "ready":
        await asyncio.gather(*(send(p, {"type": "sink_ready", "id": data.get("item")}) for p in s.players))
    elif kind == "log":
        log.info("sink[%s]: %s", s.kind, str(data.get("message"))[:1500])
    elif kind == "button":
        await on_button(data)
    elif kind == "audio_info":
        info = clean_audio_info(data)
        if info != SINK_AUDIO.get(s.kind):
            SINK_AUDIO[s.kind] = info
            await broadcast_state(s)
            if SESSIONS["box"].kind != s.kind:
                await broadcast_state(SESSIONS["box"])


async def relay_to_sink(s, data):
    """TV player -> sink: remembered for the next sink connect, forwarded unchanged."""
    SINK_RELAY.setdefault(s.kind, {})[data["type"]] = data
    sink = SINKS.get(s.kind)
    if sink is not None:
        await send(sink, data)


# ---------------------------------------------------------------- main audio track copies (for the sink)
# CACHE/audio/<id>.main.mka = the video's default audio track only (stream copy), so the sink does not read the
# picture over the LAN. One ffmpeg at a time (low priority) for all of it: the next items of the TV queue while a
# sink is connected, missing copies on request, and the web remote's "Extract missing audio" job.

AUDIO_COPIES: set = set()  # ids of videos with a fresh main copy (the summary's audio_copy)
COPY_QUEUE: list = []  # ids waiting for the worker, first = next
COPY_FAILED: dict = {}  # id -> item mtime the copy failed for (not retried automatically)
COPY_WAKE = asyncio.Event()
COPY_WORKER = None
COPY_JOB = {"running": False, "done": 0, "total": 0, "ids": set()}
COPY_PUSH = {"at": 0.0, "pending": False}
PREFETCHED: dict = {}  # session kind -> the queue slice copies were last queued for
PREFETCH = 4  # current item + the next 3


def main_copy_path(item_id):
    return AUDIO_DIR / f"{item_id}.main.mka"


def main_copy_fresh(it):
    try:
        return main_copy_path(it["id"]).stat().st_mtime >= it["mtime"]
    except OSError:
        return False


def wants_copy(it):
    return it is not None and it["kind"] == "video" and it.get("acodec") != ""  # "" = no audio track


def load_audio_copies():
    """AUDIO_COPIES from the files in CACHE/audio (startup and after every scan)."""
    AUDIO_COPIES.clear()
    for f in AUDIO_DIR.glob("*.main.mka"):
        try:
            it = LIB.get(int(f.name.split(".", 1)[0]))
        except ValueError:
            continue
        if wants_copy(it) and main_copy_fresh(it):
            AUDIO_COPIES.add(it["id"])


def make_main_copy(it, out):
    """Stream copy of the default audio track (audio_ord; None = the only / first track) into a .mka."""
    tmp = out.with_name(f"{out.stem}.{os.getpid()}.{time.monotonic_ns()}.tmp.mka")
    low = ["nice", "-n", "10"] + (["ionice", "-c3"] if shutil.which("ionice") else [])
    try:
        res = subprocess.run(
            low + ["ffmpeg", "-v", "error", "-y", "-i", item_path(it), "-map", f"0:a:{it.get('audio_ord') or 0}",
                   "-c", "copy", "-f", "matroska", str(tmp)],
            capture_output=True, timeout=900,
        )
    except BaseException:  # timeout, no nice/ffmpeg: no partial file left behind
        tmp.unlink(missing_ok=True)
        raise
    if res.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        log.warning("main audio copy of %s failed: %s", it["id"], res.stderr.decode("utf-8", "replace")[-200:])
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(out)
    return True


def copy_candidates():
    return [it for it in LIB.values() if not it["hidden"] and wants_copy(it)]


def copies_status():
    videos = copy_candidates()
    copies = sum(1 for it in videos if it["id"] in AUDIO_COPIES)
    return {"running": COPY_JOB["running"], "done": COPY_JOB["done"], "total": COPY_JOB["total"],
            "missing": len(videos) - copies, "copies": copies}


async def push_copies(force=False):
    """Progress to the web remotes, at most ~2 per second (the last state always goes out)."""
    wait = COPY_PUSH["at"] + 0.5 - time.monotonic()
    if wait > 0 and not force:
        if not COPY_PUSH["pending"]:
            COPY_PUSH["pending"] = True
            asyncio.get_running_loop().call_later(wait, lambda: asyncio.create_task(push_copies(force=True)))
        return
    COPY_PUSH["at"], COPY_PUSH["pending"] = time.monotonic(), False
    msg = dict(type="audio_copies", **copies_status())
    for ws in list(REMOTES):
        await send(ws, msg)


def queue_copies(ids, front=False):
    """Queue main copies (missing / stale ones only). front: before everything else, in the given order."""
    ids = [i for i in ids if wants_copy(LIB.get(i)) and i not in AUDIO_COPIES and COPY_FAILED.get(i) != LIB[i]["mtime"]]
    if not ids:
        return
    if front:
        COPY_QUEUE[:] = ids + [i for i in COPY_QUEUE if i not in ids]
    else:
        COPY_QUEUE.extend(i for i in ids if i not in COPY_QUEUE)
    global COPY_WORKER
    if COPY_WORKER is None or COPY_WORKER.done():
        COPY_WORKER = asyncio.create_task(copy_worker())
    COPY_WAKE.set()


def job_step(item_id):
    if COPY_JOB["running"] and item_id in COPY_JOB["ids"]:
        COPY_JOB["ids"].discard(item_id)
        COPY_JOB["done"] += 1
        if not COPY_JOB["ids"]:
            COPY_JOB["running"] = False
            log.info("audio copies: job done, %d items", COPY_JOB["total"])


async def copy_worker():
    loop = asyncio.get_running_loop()
    while True:
        if not COPY_QUEUE:
            COPY_WAKE.clear()
            await COPY_WAKE.wait()
            continue
        item_id = COPY_QUEUE.pop(0)
        it = LIB.get(item_id)
        if wants_copy(it) and not main_copy_fresh(it) and os.path.isfile(item_path(it)):
            async with MAIN_LOCKS.setdefault(item_id, asyncio.Lock()):  # an offline download may be building it
                if not main_copy_fresh(it):
                    started = time.time()
                    try:
                        ok = await loop.run_in_executor(None, make_main_copy, it, main_copy_path(item_id))
                    except Exception as e:  # noqa: BLE001 - the worker must keep running (and the job must end)
                        log.warning("main audio copy of %s: %s", item_id, e)
                        ok = False
                    if ok:
                        log.info("main audio copy %s in %.1f s ('%s')", item_id, time.time() - started, it["title"])
                    else:
                        COPY_FAILED[item_id] = it["mtime"]
        if it is not None and main_copy_fresh(it):
            AUDIO_COPIES.add(item_id)
        job_step(item_id)
        await push_copies(force=not COPY_JOB["running"])


def prefetch_copies(s):
    """While a sink is connected to the session (or the box player to its own): copies for the current item and
    the next 3 (queue order)."""
    if (SINKS.get(s.kind) is None and not (s.kind == "box" and s.players)) or s.index < 0:
        return
    ahead = tuple(s.queue[s.index:s.index + PREFETCH])
    if PREFETCHED.get(s.kind) == ahead:
        return
    PREFETCHED[s.kind] = ahead
    queue_copies(list(ahead), front=True)


def file_exists(it):
    return os.path.isfile(item_path(it))


async def ensure_current_file(s):
    """Files get renamed/replaced on the NAS between scans; VLC then just stops (HTTP 404, no
    error event) and looks stuck on "paused". Rescan right away (a rename keeps the id), then
    drop queue entries whose file is still missing. Returns False if nothing is left to play."""
    cur = s.current
    if cur is None or file_exists(cur):
        return cur is not None
    log.warning("[%s] file missing for item %s (%s/%s): rescanning", s.kind, cur["id"], cur["root"], cur["relpath"])
    was_last = s.index == len(s.queue) - 1
    await run_scan(wait=True)
    if cur["id"] not in LIB and was_last:  # prune() stepped back onto the previous item
        s.index = len(s.queue) - 1
        s.save()
        await stop_playback(s)
        return False
    while s.current is not None and not file_exists(s.current):
        gone = s.queue.pop(s.index)
        s.original = [i for i in s.original if i != gone]
        log.warning("[%s] still missing after rescan, removed from queue: %s", s.kind, gone)
        if s.index >= len(s.queue):
            s.index = len(s.queue) - 1
            s.save()
            await stop_playback(s)
            return False
    s.save()
    if s.current is None:
        await stop_playback(s)
        return False
    return True


async def start_current(s, position=0.0, autoplay=True, present=False, noted=None):
    if not await ensure_current_file(s):
        return
    if s.kind == "box" and autoplay:
        if tv_owns_output():
            autoplay = False
            s.notice = {"text": IN_USE_NOTICE, "at": time.time()}
        else:
            s.notice = None
    s.position, s.position_at = position, time.time()
    s.duration = (s.current or {}).get("duration") or 0.0
    s.want_play = autoplay
    s.playing = False
    s.noted = noted  # None: the next "playing" status of this start goes into the play history
    s.save()
    await box_send_sound(s)
    await to_players(s, load_message(s, autoplay, position, present))
    await broadcast_state(s)


async def stop_playback(s):
    s.want_play = False
    s.playing = False
    await to_players(s, {"type": "control", "action": "stop"})
    await broadcast_state(s)


def prefer_list(s, ids, present, keep=None):
    """ids with every song that has a music video replaced by preferred_video (noted in s.swapped). No doubles: a song
    whose video is already in `present` (or was chosen for another song) is dropped (noted in s.dropped). keep: a
    position left as it is (the current item). Returns (new list, new position of every old position; a dropped song
    gets its video's)."""
    out, where, chosen, dropped = [], [], {}, {}
    for n, i in enumerate(ids):
        video = None if n == keep or i in s.pinned else preferred_video(i)
        if video is None:
            where.append(len(out))
            out.append(i)
        elif (video in present and video not in chosen) or chosen.get(video, i) != i:
            where.append(None)
            dropped[n] = (video, len(out))
            songs = s.dropped.setdefault(video, [])
            if i not in songs:
                songs.append(i)
        else:
            chosen[video] = i
            s.swapped[video] = i
            where.append(len(out))
            out.append(video)
    for n, (video, at) in dropped.items():
        where[n] = out.index(video) if video in out else min(at, max(len(out) - 1, 0))
    return out, where


def unswap(s, i):
    """The song a "Prefer videos" swap replaced (entries picked by hand stay)."""
    return s.swapped[i] if i in s.swapped and i not in s.pinned else i


def restore_list(s, ids, start=0, keep=None):
    """Prefer videos off: the entries from `start` on (except position keep) go back to their song, and the songs
    dropped for a video come back right after it (after the current item when that is the video, at the end when the
    video is gone). Songs already in ids are not added again."""
    back, seen = {}, set(ids)
    for video, songs in s.dropped.items():
        for song in songs:
            if song not in seen:
                seen.add(song)
                back.setdefault(video, []).append(song)
    out = list(ids[:start])
    if start > 0:
        out.extend(back.pop(ids[start - 1], []))
    for n in range(start, len(ids)):
        out.append(ids[n] if n == keep else unswap(s, ids[n]))
        out.extend(back.pop(ids[n], []))
    for songs in back.values():
        out.extend(songs)
    return out


def apply_prefer(s, whole=False):
    """The flag changed (or a repeat-all lap starts, whole=True): on = songs after the current item become their
    videos, off = swapped entries after it go back to their song and dropped songs come back. The unshuffled order
    follows (all but the current item), so shuffle off keeps the choice."""
    cur = s.current_id
    start = 0 if whole else s.index + 1
    at = s.original.index(cur) if not whole and cur in s.original else None
    if s.prefer_videos:
        rest, _ = prefer_list(s, s.queue[start:], set(s.queue))
        s.queue = s.queue[:start] + rest
        s.original, _ = prefer_list(s, s.original, set(s.original), keep=at)
    else:
        s.queue = restore_list(s, s.queue, start)
        s.original = restore_list(s, s.original, keep=at)
        s.dropped = {}
        s.swapped = {k: v for k, v in s.swapped.items() if k in s.queue}
    if whole:
        s.index = 0 if s.queue else -1


async def set_queue(s, ids, start_index=None, shuffle=False):
    """start_index None = beginning (or a random first track when shuffling)."""
    ids = [i for i in ids if i in LIB]
    if not ids:
        return
    if start_index is not None:
        start_index = min(max(int(start_index), 0), len(ids) - 1)
    s.swapped, s.pinned, s.dropped = {}, [], {}
    if s.prefer_videos:
        ids, where = prefer_list(s, ids, set(ids))
        if start_index is not None:
            start_index = where[start_index]
    s.original = list(ids)
    if shuffle:
        rest = smart_shuffle([i for n, i in enumerate(ids) if n != start_index])
        s.queue = ([ids[start_index]] if start_index is not None else []) + rest
        s.index = 0
    else:
        s.queue = list(ids)
        s.index = start_index or 0
    s.shuffle = shuffle
    s.errors = 0
    await start_current(s, present=True)


async def advance(s, user):
    if not s.queue:
        return
    if s.repeat == "one" and not user:
        pass
    elif s.index + 1 < len(s.queue):
        s.index += 1
    elif s.repeat == "all":
        if s.shuffle:  # new lap: the items just heard go to the back, not straight to the front again
            s.queue = smart_shuffle(s.queue)
        s.index = 0
        if s.prefer_videos or s.swapped:
            apply_prefer(s, whole=True)
    else:
        s.index = len(s.queue) - 1
        await stop_playback(s)
        return
    await start_current(s)


async def switch_version(s, msg):
    """Replace the current entry with another version of it (song <-> video, official <-> live) and start it: at the
    same position when both are the same cut (durations within 5 s), else from the start."""
    cur = s.current
    try:
        new_id = int(msg.get("id"))
    except (TypeError, ValueError):
        return
    if cur is None or new_id == cur["id"] or new_id not in VERSIONS.get(cur["id"], []):
        return
    old_id, new = cur["id"], LIB[new_id]
    live = s.position + (time.time() - s.position_at if s.playing else 0)
    position = float(msg["position"]) if msg.get("position") is not None else live
    a, b = cur.get("duration") or 0, new.get("duration") or 0
    same_cut = a > 0 and b > 0 and abs(a - b) <= 5
    s.queue[s.index] = new_id
    if s.index < len(s.original) and s.original[s.index] == old_id:
        s.original[s.index] = new_id
    elif old_id in s.original:
        s.original[s.original.index(old_id)] = new_id
    raw = s.swapped.get(old_id, old_id)  # the playlist / collection entry this slot came from
    if old_id not in s.queue:
        s.swapped.pop(old_id, None)
    if raw != new_id:
        s.swapped[new_id] = raw
    else:
        s.swapped.pop(new_id, None)
    if new_id not in s.pinned:
        s.pinned.append(new_id)
    # The same listen goes on: not a new start in the play history (the shuffle memory still gets the new id).
    noted = None
    if s.noted == old_id:
        noted = new_id
        PLAYED[new_id] = time.time()
        try:
            save_history()
        except OSError as e:
            log.warning("history not saved: %s", e)
    log.info("[%s] version %s -> %s (%s) at %.0f s", s.kind, old_id, new_id, VERSION_LABEL.get(new_id, ""),
             position if same_cut else 0)
    s.errors = 0
    await start_current(s, position=max(position, 0) if same_cut else 0.0, present=True, noted=noted)


async def handle_cmd(s, msg):
    cmd = msg.get("cmd")
    if cmd == "play_ids":
        s.source, s.source_items = None, []
        await set_queue(s, msg.get("ids") or [], msg.get("start_index"), bool(msg.get("shuffle")))
    elif cmd == "play_collection":
        name = msg.get("collection", "all")
        playlist = None
        if name == "playlist":
            try:
                playlist = int(msg.get("playlist"))
            except (TypeError, ValueError):
                return
        items = collection(name, msg.get("root"), msg.get("folder"),
                           msg.get("recursive", True) is not False, playlist)
        ids = [it["id"] for it in items]
        if msg.get("start_index") is not None:  # playlists may hold the same song twice
            start = int(msg["start_index"])
            if playlist is not None:  # index in the stored list -> index among the playable items
                stored = PLAYLISTS[playlist]["items"] if playlist in PLAYLISTS else []
                start = sum(1 for i in stored[:start] if i in LIB)
        else:
            start = ids.index(msg["start_id"]) if msg.get("start_id") in ids else None
        log.info("[%s] play %s%s: %d items, shuffle %s", s.kind, name, f" {playlist}" if playlist is not None else "",
                 len(ids), bool(msg.get("shuffle")))
        await set_queue(s, ids, start, bool(msg.get("shuffle")))
        if ids:
            s.source, s.source_items = playlist, (list(ids) if playlist is not None else [])
            s.save()
    elif cmd == "enqueue":
        ids = [i for i in (msg.get("ids") or []) if i in LIB]
        if not ids:
            return
        if not s.queue:
            await set_queue(s, ids)
            return
        for i in ids:  # picked by hand: an older automatic swap of the same video no longer applies to it
            s.swapped.pop(i, None)
        if s.prefer_videos:
            ids, _ = prefer_list(s, ids, set(s.queue) | set(ids))
        pos = s.index + 1 if msg.get("mode") == "next" else len(s.queue)
        s.source, s.source_items = None, []  # edited by hand: no longer just the playlist
        s.queue[pos:pos] = ids
        s.original.extend(ids)
        s.save()
        await broadcast_state(s)
    elif cmd == "next":
        await advance(s, user=True)
    elif cmd == "prev":
        position = float(msg.get("position", s.position) or 0)
        if position > 5 or s.index <= 0:
            await to_players(s, {"type": "control", "action": "seek", "position": 0})
        else:
            s.index -= 1
            await start_current(s)
    elif cmd == "jump":
        idx = int(msg.get("index", -1))
        if 0 <= idx < len(s.queue):
            s.index = idx
            await start_current(s, present=True)
    elif cmd in ("toggle", "play", "pause"):
        if cmd == "play" or (cmd == "toggle" and not s.playing):
            if s.kind == "box":
                if tv_owns_output():
                    await box_notice(s, IN_USE_NOTICE)
                    return
                s.notice = None
                cmd = "play"
            s.want_play = True
        else:
            s.want_play = False
            if s.kind == "box":
                cmd = "pause"
        await to_players(s, {"type": "control", "action": cmd})
        if s.kind == "box":
            await broadcast_state(s)
    elif cmd == "sound" and s.kind == "box":  # web remote: {preset, boost, loudness}
        new = box_sound.clean({**s.sound, **{k: msg[k] for k in ("preset", "boost", "loudness", "custom") if k in msg}})
        if new != s.sound:
            s.sound = new
            s.save()
            log.info("[box] sound: %s", box_sound.label(s.sound, s.current))
            await box_send_sound(s)
        await broadcast_state(s)
    elif cmd == "dismiss" and s.kind == "box":
        s.notice = None
        await broadcast_state(s)
    elif cmd == "seek":
        await to_players(s, {"type": "control", "action": "seek", "position": float(msg.get("position", 0))})
    elif cmd == "shuffle":
        on = bool(msg.get("on", not s.shuffle))
        cur = s.current_id
        if on and not s.shuffle:
            rest = smart_shuffle(s.queue[s.index + 1:])
            s.queue = s.queue[:s.index + 1] + rest
        elif not on and s.shuffle and s.original:
            s.queue = list(s.original)
            s.index = s.queue.index(cur) if cur in s.queue else 0
        s.shuffle = on
        s.save()
        await broadcast_state(s)
    elif cmd == "repeat":
        mode = msg.get("mode")
        if mode not in ("off", "all", "one"):
            mode = {"off": "all", "all": "one", "one": "off"}[s.repeat]
        s.repeat = mode
        s.save()
        await broadcast_state(s)
    elif cmd == "prefer_videos":
        on = bool(msg.get("on", not s.prefer_videos))
        if on != s.prefer_videos:
            s.prefer_videos = on
            apply_prefer(s)
            log.info("[%s] prefer videos %s", s.kind, "on" if on else "off")
        s.save()
        await broadcast_state(s)
    elif cmd == "switch_version":
        await switch_version(s, msg)
    elif cmd == "remove":
        idx = int(msg.get("index", -1))
        if 0 <= idx < len(s.queue) and idx != s.index:
            s.source, s.source_items = None, []
            gone = s.queue.pop(idx)
            if idx < s.index:
                s.index -= 1
            if gone not in s.queue:
                s.swapped.pop(gone, None)
                s.dropped.pop(gone, None)
            s.save()
            await broadcast_state(s)
    elif cmd == "clear":
        s.source, s.source_items = None, []
        cur = s.current_id
        s.queue = [cur] if cur is not None else []
        s.original = list(s.queue)
        s.index = 0 if s.queue else -1
        s.swapped = {k: v for k, v in s.swapped.items() if k in s.queue}
        s.pinned = [i for i in s.pinned if i in s.queue]
        s.dropped = {}
        s.save()
        await broadcast_state(s)
    elif cmd == "love":  # {id (default: the current item), on (missing = toggle)}
        try:
            item_id = int(msg["id"]) if msg.get("id") is not None else s.current_id
        except (TypeError, ValueError):
            return
        if item_id not in LIB:
            return
        on = not is_loved(item_id) if msg.get("on") is None else bool(msg.get("on"))
        if set_loved(item_id, on):
            log.info("[%s] heart %s: %s '%s'", s.kind, "on" if on else "off", item_id, LIB[item_id]["title"])
            await loved_changed()
    elif cmd == "love_ids":  # web remote select mode: {ids, on (default true)}, one push at the end
        on = msg.get("on", True) is not False
        changed = 0
        for i in msg.get("ids") or []:
            try:
                item_id = int(i)
            except (TypeError, ValueError):
                continue
            if item_id in LIB and set_loved(item_id, on):
                changed += 1
        if changed:
            log.info("[%s] heart %s: %d items", s.kind, "on" if on else "off", changed)
            await loved_changed()
    elif cmd == "rescan":
        asyncio.create_task(run_scan())


async def on_message(s, data, role, ws=None):
    kind = data.get("type")
    if kind == "cmd":
        await handle_cmd(s, data)
    elif kind == "ping" and ws is not None:
        await send(ws, pong(data))
    elif kind == "volume" and role in ("player", "remote") and s.kind in ("tv", "box"):  # TV quick panel, web remote
        vol = clean_volume(data)
        if vol is not None:
            await relay_to_sink(SESSIONS["tv"], {"type": "volume", **vol})  # one output: the sink of both
    elif role == "player" and kind == "notice" and s.kind == "box":  # e.g. the headphones went away: paused
        s.want_play = False
        await box_notice(s, str(data.get("text") or "")[:200])
    elif role == "player" and kind in RELAY_TYPES and s.kind == "tv":
        await relay_to_sink(s, data)
        if kind == "route":
            await broadcast_state(s)  # web remote: the volume slider follows the TV's sound output
    elif role == "player" and kind == "status":
        if data.get("item") != s.current_id:
            return
        s.playing = bool(data.get("playing"))
        s.position = float(data.get("position") or 0)
        s.position_at = time.time()
        if data.get("duration"):
            s.duration = float(data["duration"])
        if s.playing:
            s.errors = 0
            if s.noted != s.current_id:
                s.noted = s.current_id
                s.save()  # a server restart in mid-track must not count this start again
                note_played(s.current_id)
        await broadcast_state(s)
    elif role == "player" and kind == "log":
        log.info("player[%s]: %s", s.kind, str(data.get("message"))[:1500])  # seek trace lines are ~700 chars
    elif role == "player" and kind == "ended":
        if data.get("item") == s.current_id:
            await advance(s, user=False)
    elif role == "player" and kind == "error":
        if data.get("item") == s.current_id:
            s.errors += 1
            log.warning("[%s] player error on %s: %s", s.kind, data.get("item"), data.get("message"))
            if s.errors >= 3:
                await stop_playback(s)
            else:
                await advance(s, user=False)


# ---------------------------------------------------------------- HTTP handlers

@web.middleware
async def api_no_store(request, handler):
    """API answers change with every edit; without this the apps' URL cache may hand out an old playlist."""
    resp = await handler(request)
    if request.path.startswith("/api/") and "Cache-Control" not in resp.headers:
        resp.headers["Cache-Control"] = "no-store"
    return resp


async def ws_handler(request):
    q = request.query
    role = q.get("role") if q.get("role") in ("player", "sink") else "remote"
    ws = web.WebSocketResponse(heartbeat=45)
    await ws.prepare(request)
    if role == "sink":
        kind = "tv"  # only the TV session has a sink
    elif role == "player":
        kind = q.get("kind") if q.get("kind") in KINDS else "tv"  # apps before 0.11 send no kind: the TV
        PLAYERS[ws] = kind
    else:
        kind = q.get("session") if q.get("session") in KINDS else "tv"
        REMOTES[ws] = kind
    s = SESSIONS[kind]
    log.info("%s (%s) connected from %s", role, kind, request.remote)
    if role == "sink":
        await sink_connected(s, ws)
    else:
        await send(ws, {"type": "state", "state": s.to_dict()})
    if role == "player":
        if s.current and await ensure_current_file(s):
            if s.kind == "box":
                await send(ws, box_sound.message(s.sound, s.current))
                if s.want_play and tv_owns_output():
                    s.want_play = False
            await send(ws, load_message(s, s.want_play, s.position))
        await broadcast_all()
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            if role == "remote" and data.get("type") == "select":  # web remote switches TV / iPhone queue
                s = session_for(data.get("session"))
                REMOTES[ws] = s.kind
                await send(ws, {"type": "state", "state": s.to_dict()})
                continue
            if role == "sink":
                await sink_message(s, ws, data)
                continue
            await on_message(s, data, role, ws)
    finally:
        log.info("%s (%s) disconnected", role, s.kind)
        if role == "sink":
            if SINKS.get(s.kind) is ws:  # not when a newer sink replaced this one
                SINKS.pop(s.kind, None)
                SINK_INFO.pop(s.kind, None)
                SINK_AUDIO.pop(s.kind, None)
                PREFETCHED.pop(s.kind, None)
                await broadcast_state(s)
        elif role == "player":
            PLAYERS.pop(ws, None)
            if not s.players:
                s.playing = False
                s.want_play = False
                s.save()
                if s.kind == "tv":  # the TV plays its own sound again: silence the sink now, and on its next connect
                    await relay_to_sink(s, {"type": "route", "sink": False})
            await broadcast_all()
        else:
            REMOTES.pop(ws, None)
    return ws


async def index(_request):
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def api_library(request):
    kind = request.query.get("kind")
    show_all = request.query.get("all") == "1"
    items = [it for it in LIB.values() if (show_all or not it["hidden"]) and (not kind or it["kind"] == kind)]
    items.sort(key=video_key if kind == "video" else sort_key)
    resp = web.json_response({"items": [summary(it) for it in items], "last_scan": LAST_SCAN})
    resp.enable_compression()
    return resp


async def api_collection(request):
    items = collection(request.match_info["name"], request.query.get("root"), request.query.get("folder"))
    resp = web.json_response({"items": [summary(it) for it in items]})
    resp.enable_compression()
    return resp


async def api_folders(_request):
    """Top-level folders per root with visible item counts."""
    counts: dict = {}
    for it in LIB.values():
        if it["hidden"]:
            continue
        top = it["folder"].split("/", 1)[0] if it["folder"] else ""
        key = (it["root"], top)
        c = counts.setdefault(key, {"root": it["root"], "folder": top, "audio": 0, "video": 0})
        c[it["kind"]] += 1
    folders = sorted(counts.values(), key=lambda c: (c["root"], c["folder"].lower()))
    return web.json_response({"folders": folders})


async def api_browse(request):
    """No root: the library roots. With root (+ folder): direct subfolders and the folder's own files."""
    root = request.query.get("root")
    if not root:
        roots = [{"root": r, "name": ROOT_NAMES.get(r, r),
                  "count": sum(1 for it in LIB.values() if it["root"] == r and not it["hidden"])} for r in ROOTS]
        return web.json_response({"roots": roots})
    if root not in ROOTS:
        raise web.HTTPNotFound()
    folder = (request.query.get("folder") or "").strip("/")
    prefix = folder + "/" if folder else ""
    subs: dict = {}
    items = []
    total = 0
    for it in LIB.values():
        if it["hidden"] or it["root"] != root:
            continue
        f = it["folder"]
        if f == folder:
            items.append(it)
            total += 1
        elif f.startswith(prefix):
            name = f[len(prefix):].split("/", 1)[0]
            subs[name] = subs.get(name, 0) + 1
            total += 1
    subfolders = [{"name": n, "folder": prefix + n, "count": c}
                  for n, c in sorted(subs.items(), key=lambda kv: kv[0].lower())]
    items.sort(key=sort_key)
    resp = web.json_response({"root": root, "folder": folder, "subfolders": subfolders,
                              "items": [summary(it) for it in items], "total": total})
    resp.enable_compression()
    return resp


async def api_item(request):
    it = LIB.get(int(request.match_info["id"]))
    if not it:
        raise web.HTTPNotFound()
    return web.json_response(full(it))


async def api_state(request):
    return web.json_response(session_for(request.query.get("session")).to_dict())


async def api_auto(_request):
    return web.json_response(auto_lists())


async def api_history(request):
    """Play history behind the shuffle: count and the most recently started items (?limit=N, default 20)."""
    limit = min(max(int(request.query.get("limit", 20)), 0), 500)
    latest = sorted(PLAYED.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return web.json_response({"days": HISTORY_DAYS, "count": len(PLAYED), "recent": [
        {"id": i, "at": at, "title": LIB[i]["title"] if i in LIB else None,
         "artist": LIB[i].get("artist") if i in LIB else None} for i, at in latest]})


# Playlist API. Bodies are JSON. Item lists are library ids; unknown ids are rejected on add.

def _playlist_or_404(request):
    pl = PLAYLISTS.get(int(request.match_info["id"]))
    if pl is None:
        raise web.HTTPNotFound(text="no such playlist")
    return pl


async def _json_body(request):
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="JSON body expected")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="JSON object expected")
    return body


def _clean_name(value):
    name = " ".join(str(value or "").split())[:120]
    if not name:
        raise web.HTTPBadRequest(text="name required")
    return name


def _clean_ids(value, known_only=True):
    if not isinstance(value, list):
        raise web.HTTPBadRequest(text="ids must be a list")
    try:
        ids = [int(i) for i in value]
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="ids must be integers")
    return [i for i in ids if i in LIB] if known_only else ids


def _ids_from_source(body):
    """ids, or a source to expand: {"folder": {root, folder, recursive}}, {"queue": true},
    {"playlist": id}. Folders follow the library order and skip hidden duplicates (like Play all)."""
    if "ids" in body:
        return _clean_ids(body["ids"])
    if body.get("queue"):  # {"queue": true, "session": "tv"|"phone"}: the queue the web remote shows
        return [i for i in session_for(body.get("session")).queue if i in LIB]
    if isinstance(body.get("folder"), dict):
        f = body["folder"]
        if f.get("root") not in ROOTS:
            raise web.HTTPBadRequest(text="unknown root")
        return [it["id"] for it in collection("folder", f["root"], f.get("folder") or "", f.get("recursive", True) is not False)]
    if body.get("playlist") is not None:
        src = PLAYLISTS.get(int(body["playlist"]))
        return list(src["items"]) if src else []
    return []


async def api_playlists(_request):
    pls = sorted(PLAYLISTS.values(), key=lambda p: p["name"].lower())
    return web.json_response({"playlists": [playlist_summary(p) for p in pls]})


async def api_playlist_get(request):
    resp = web.json_response(playlist_detail(_playlist_or_404(request)))
    resp.enable_compression()
    return resp


async def api_playlist_create(request):
    global NEXT_PLAYLIST_ID
    body = await _json_body(request)
    now = time.time()
    pl = {"id": NEXT_PLAYLIST_ID, "name": _clean_name(body.get("name")), "items": _ids_from_source(body),
          "created": now, "updated": now}
    NEXT_PLAYLIST_ID += 1
    PLAYLISTS[pl["id"]] = pl
    log.info("playlist %s created: %r, %d items", pl["id"], pl["name"], len(pl["items"]))
    await playlists_changed(pl["id"])
    return web.json_response(playlist_detail(pl), status=201)


async def api_playlist_update(request):
    """{"name": ...} renames; {"items": [...]} replaces the whole list (reorder/remove, missing ids kept)."""
    pl = _playlist_or_404(request)
    body = await _json_body(request)
    if "name" in body:
        pl["name"] = _clean_name(body["name"])
    if "items" in body:
        known = set(pl["items"]) | set(LIB)
        pl["items"] = [i for i in _clean_ids(body["items"], known_only=False) if i in known]
    pl["updated"] = time.time()
    await playlists_changed(pl["id"])
    return web.json_response(playlist_detail(pl))


async def api_playlist_add(request):
    """Append (or insert at "position") ids or a source, see _ids_from_source. Duplicates allowed
    unless "skip_existing": true."""
    pl = _playlist_or_404(request)
    body = await _json_body(request)
    ids = _ids_from_source(body)
    if body.get("skip_existing"):
        have = set(pl["items"])
        ids = [i for i in ids if i not in have]
    pos = body.get("position")
    pos = len(pl["items"]) if pos is None else max(0, min(int(pos), len(pl["items"])))
    pl["items"][pos:pos] = ids
    pl["updated"] = time.time()
    await playlists_changed(pl["id"])
    return web.json_response({"added": len(ids), "playlist": playlist_summary(pl)})


async def api_playlist_delete(request):
    pl = _playlist_or_404(request)
    del PLAYLISTS[pl["id"]]
    log.info("playlist %s deleted: %r", pl["id"], pl["name"])
    await playlists_changed(pl["id"])
    return web.json_response({"deleted": pl["id"]})


# Apple TV screenshots (web remote 📷, or POST from scripts). The capture runs in a separate process
# (ATV_SHOT_CMD, pymobiledevice3 over Wi-Fi, no root); files go to ATV_SHOT_DIR (unset = disabled).
# ATV_SHOT_SHARE optionally names that folder as users see it (e.g. a Windows share path) for the
# web remote's toast. One capture at a time, nothing polls.
SHOT_DIR = Path(os.environ["ATV_SHOT_DIR"]) if os.environ.get("ATV_SHOT_DIR") else None
SHOT_SHARE = os.environ.get("ATV_SHOT_SHARE", "").rstrip("\\/")
SHOT_CMD = os.environ.get("ATV_SHOT_CMD", "/opt/atv-shot/atv-shot.py")
SHOT_LOCK = asyncio.Lock()
SHOT_NAME = re.compile(r"^atv-\d{8}-\d{6}(-\d+)?\.(png|jpg)$")


async def api_screenshot(_request):
    if SHOT_DIR is None or not os.access(SHOT_CMD, os.X_OK) or not SHOT_DIR.is_dir():
        raise web.HTTPServiceUnavailable(text="screenshot helper or target folder not available")
    if SHOT_LOCK.locked():
        raise web.HTTPConflict(text="a screenshot is already being taken")
    async with SHOT_LOCK:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name, n = f"atv-{stamp}.png", 1
        while (SHOT_DIR / name).exists() or (SHOT_DIR / name).with_suffix(".jpg").exists():
            n += 1
            name = f"atv-{stamp}-{n}.png"
        proc = await asyncio.create_subprocess_exec(
            SHOT_CMD, str(SHOT_DIR / name), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=150)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise web.HTTPGatewayTimeout(text="screenshot timed out")
        line = (out.decode(errors="replace").strip().splitlines() or [""])[-1]
        try:
            result = json.loads(line)
        except ValueError:
            result = {"ok": False, "error": (err.decode(errors="replace").strip().splitlines() or ["no output"])[-1]}
        if not result.get("ok"):
            log.warning("screenshot failed (exit %s): %s", proc.returncode, result.get("error"))
            return web.json_response({"ok": False, "error": result.get("error")}, status=502)
        jpg = name[:-4] + ".jpg" if result.get("jpg") else None
        log.info("screenshot %s (%s bytes; jpg %s bytes; %s s)", name, result.get("bytes"),
                 result.get("jpg_bytes"), result.get("seconds"))
        return web.json_response({"ok": True, "name": name, "url": f"/shots/{name}",
                                  "share_path": f"{SHOT_SHARE}\\{name}" if SHOT_SHARE else None,
                                  "jpg": jpg, "jpg_url": f"/shots/{jpg}" if jpg else None,
                                  "jpg_share_path": f"{SHOT_SHARE}\\{jpg}" if jpg and SHOT_SHARE else None,
                                  "bytes": result.get("bytes"), "jpg_bytes": result.get("jpg_bytes"),
                                  "seconds": result.get("seconds")})


async def shot_file(request):
    name = request.match_info["name"]
    if SHOT_DIR is None or not SHOT_NAME.match(name) or not (SHOT_DIR / name).is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(SHOT_DIR / name, headers={"Cache-Control": "no-cache"})


async def api_rescan(request):
    """Default: start a scan in the background. ?wait=1 (web remote ⟳ Rescan button): run it now
    (waiting for a running one first) and return what changed, with the titles of new items."""
    force = request.query.get("full") == "1"
    if request.query.get("wait") != "1":
        if not SCANNING:
            asyncio.create_task(run_scan(force=force))
        return web.json_response({"scanning": True})
    before = {i: it["hidden"] for i, it in LIB.items()}
    await run_scan(force=force, wait=True)
    added = [LIB[i] for i in LIB if i not in before]
    removed = sum(1 for i in before if i not in LIB)
    visible = [it for it in added if not it["hidden"]]
    return web.json_response({
        "ok": True, "added": len(added), "added_visible": len(visible), "removed": removed,
        "renamed": len(LAST_SCAN.get("renamed_ids") or []), "updated": LAST_SCAN.get("updated", 0),
        "items": len(LIB), "seconds": LAST_SCAN.get("seconds"),
        "new": [" - ".join(x for x in (it.get("artist"), it["title"]) if x) for it in visible[:10]],
    })


def item_path(it):
    """A folder removed in the settings a moment ago: no path (treated as a missing file until the rescan)."""
    return os.path.join(ROOTS.get(it["root"], "/nonexistent-library-folder"), it["relpath"])


AUDIO_LOCKS: dict[int, asyncio.Lock] = {}
# Video keyframe times per item (seconds), computed once per file version with ffprobe; the iPhone app uses them to
# cover the picture until the next keyframe after its picture track comes back mid-stream (else it is blocky).
KEYFRAMES: dict[int, tuple[float, list]] = {}
KEYFRAME_SEM = asyncio.Semaphore(1)  # one ffprobe at a time on the small CT


def video_keyframes(path):
    """Presentation times (s) of the first video stream's keyframes, sorted. Reads the packet index only, no decoding."""
    res = subprocess.run(
        ["nice", "-n", "10", "ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", path],
        capture_output=True, text=True, timeout=300,
    )
    times = []
    for line in res.stdout.splitlines():
        parts = line.split(",")
        if len(parts) >= 2 and "K" in parts[1]:
            try:
                times.append(round(float(parts[0]), 3))
            except ValueError:
                pass
    return sorted(set(times))


def make_audio_only(it, out):
    """Copy the audio tracks of a music video into a .mka (no re-encoding, a few seconds). The remote
    (mobile data) setting "audio only" streams this instead of the 4K video. Track order is kept, so the
    app's audio ordinal still points at the right track."""
    tmp = out.with_name(f"{out.stem}.{os.getpid()}.{time.monotonic_ns()}.tmp.mka")
    res = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", item_path(it), "-map", "0:a", "-c:a", "copy", "-f", "matroska", str(tmp)],
        capture_output=True, timeout=900,
    )
    if res.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        log.warning("audio-only copy of %s failed: %s", it["id"], res.stderr.decode("utf-8", "replace")[-200:])
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(out)
    return True


# ---------------------------------------------------------------- offline downloads (iPhone app)
# /media/<id>?audio=best: the item's OWN audio, never another version of its group (a group mixes different recordings:
# studio song, live videos, covers by other singers) and never transcoded: a song's original file, a video's default
# audio track (the best one in this library by the owner's default-track rule) as a stream copy (the main copy the
# atv-audio sink uses). /api/offline/<collection> lists what that serves for a whole list.

AUDIO_INFO: dict[int, tuple] = {}  # item id -> (mtime, audio_ord, info) from the audio_probe table / ffprobe
MAIN_LOCKS: dict[int, asyncio.Lock] = {}


def probe_audio(it):
    """codec, bit depth (lossless only), sample rate and bit rate of the default audio track."""
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", f"a:{it.get('audio_ord') or 0}", "-show_entries",
         "stream=codec_name,bits_per_raw_sample,bits_per_sample,sample_rate,bit_rate:stream_tags=BPS:format=bit_rate",
         "-of", "json", item_path(it)],
        capture_output=True, text=True, timeout=60,
    )
    data = json.loads(res.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]

    def num(*values):
        for v in values:
            try:
                n = int(float(v))
            except (TypeError, ValueError):
                continue
            if n > 0:
                return n
        return None

    tags = {k.upper(): v for k, v in (stream.get("tags") or {}).items()}
    bitrate = num(stream.get("bit_rate"), tags.get("BPS"),
                  (data.get("format") or {}).get("bit_rate") if it["kind"] == "audio" else None)
    return {"codec": stream.get("codec_name") or it.get("acodec") or it.get("codec"),
            "bits": num(stream.get("bits_per_raw_sample"), stream.get("bits_per_sample")),
            "rate": num(stream.get("sample_rate")), "bitrate": bitrate or num(it.get("bitrate"))}


def audio_info(it):
    """Probe result for the item's default audio track: memory, then library.db, then ffprobe (stored)."""
    key = (it["mtime"], it.get("audio_ord"))
    hit = AUDIO_INFO.get(it["id"])
    if hit and hit[:2] == key:
        return hit[2]
    info = None
    try:
        con = sqlite3.connect(DB)
        row = con.execute("SELECT mtime, audio_ord, codec, bits, rate, bitrate FROM audio_probe WHERE item_id = ?",
                          (it["id"],)).fetchone()
        if row and abs(row[0] - it["mtime"]) < 1 and row[1] == it.get("audio_ord"):
            info = {"codec": row[2], "bits": row[3], "rate": row[4], "bitrate": row[5]}
        else:
            try:
                info = probe_audio(it)
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                log.warning("audio probe of %s: %s", it["id"], e)
                info = {"codec": it.get("acodec") or it.get("codec"), "bits": None, "rate": None,
                        "bitrate": it.get("bitrate")}
            else:
                con.execute("INSERT OR REPLACE INTO audio_probe (item_id, mtime, audio_ord, codec, bits, rate, bitrate)"
                            " VALUES (?, ?, ?, ?, ?, ?, ?)", (it["id"], it["mtime"], it.get("audio_ord"), info["codec"],
                                                              info["bits"], info["rate"], info["bitrate"]))
                con.commit()
        con.close()
    except sqlite3.Error as e:
        log.warning("audio probe cache: %s", e)
        info = info or probe_audio(it)
    AUDIO_INFO[it["id"]] = key + (info,)
    return info


def has_audio(it):
    return it["kind"] == "audio" or it.get("acodec") != ""  # "" = a video without an audio track


def probe_all(items):
    """Executor: probe results for these item dicts (snapshots; never reads LIB)."""
    return {it["id"]: audio_info(it) for it in items}


async def probe(items):
    unique = list({it["id"]: it for it in items}.values())
    if not unique:
        return {}
    return await asyncio.get_running_loop().run_in_executor(None, probe_all, unique)


def audio_format(it, info):
    codec = (info or {}).get("codec") or it.get("acodec") or it.get("codec") or "?"
    bits, rate = (info or {}).get("bits"), (info or {}).get("rate")
    if bits and rate and it.get("lossless"):
        return f"{codec} {bits}/{rate}"
    return f"{codec} {rate}" if rate else codec


def offline_rev(it):
    """Changes when the served file changes (rescan: new mtime, new default track)."""
    return f"{it['id']}:{int(it['mtime'])}:{it.get('audio_ord') or 0}"


def offline_entry(entry_id, src, info):
    if src["kind"] == "audio":
        size, exact = src["size"], True
    elif main_copy_fresh(src):
        size, exact = main_copy_path(src["id"]).stat().st_size, True
    else:  # estimated until the copy exists
        rate = (info or {}).get("bitrate") or 0
        size, exact = (int(rate * (src.get("duration") or 0) / 8) or None), False
    return {"id": entry_id, "source": src["id"], "rev": offline_rev(src), "size": size, "size_exact": exact,
            "format": audio_format(src, info) if info else None, "item": full(src)}


async def offline_list(name, playlist=None):
    """LIB / VERSIONS are read on the event loop only; the executor probes snapshots of the items."""
    if name == "playlist":
        pl = PLAYLISTS.get(playlist)
        if pl is None:
            return None
        title = pl["name"]
    elif name in AUTO_NAMES:
        title = dict(AUTO_LISTS)[name]
    else:
        return None
    rows = [it for it in collection(name, playlist=playlist) if has_audio(it)]
    infos = await probe(rows)  # format label, and the size estimate of a video without its copy yet (cached in the DB)
    entries = [offline_entry(it["id"], it, infos.get(it["id"])) for it in rows]
    stamp = hashlib.sha1(json.dumps([title] + [(e["id"], e["rev"]) for e in entries]).encode()).hexdigest()[:16]
    return {"collection": name, "playlist": playlist, "name": title, "stamp": stamp, "items": entries,
            "size": sum(e["size"] or 0 for e in entries)}


async def api_offline(request):
    """A list for the iPhone's offline downloads: each entry with the id ?audio=best serves (source = the entry), rev, size
    (exact, or estimated from the bit rate until a video's copy exists) and item; stamp changes with the list."""
    try:
        playlist = int(request.query["playlist"]) if "playlist" in request.query else None
    except ValueError:
        raise web.HTTPBadRequest(text="bad playlist id")
    out = await offline_list(request.match_info["name"], playlist)
    if out is None:
        raise web.HTTPNotFound(text="no such list")
    resp = web.json_response(out)
    resp.enable_compression()
    return resp


async def media_best(it):
    if not has_audio(it):
        raise web.HTTPNotFound(text="no audio track")
    src, src_id = it, it["id"]  # the item itself (a snapshot: a rescan meanwhile does not change it under us)
    info = (await probe([src]))[src_id]
    headers = {"X-Audio-Source": str(src_id), "X-Audio-Format": audio_format(src, info)}
    if src["kind"] == "audio":
        path = item_path(src)
        if not os.path.isfile(path):
            raise web.HTTPNotFound()
        headers["Content-Type"] = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return web.FileResponse(path, chunk_size=512 * 1024, headers=headers)
    out = main_copy_path(src_id)
    if not main_copy_fresh(src):  # build it and wait; never the video file
        if not os.path.isfile(item_path(src)):
            raise web.HTTPNotFound()
        async with MAIN_LOCKS.setdefault(src_id, asyncio.Lock()):  # shared with copy_worker: built once
            if not main_copy_fresh(src):
                started = time.time()
                ok = await asyncio.get_running_loop().run_in_executor(None, make_main_copy, src, out)
                if not ok:
                    COPY_FAILED[src_id] = src["mtime"]
                    raise web.HTTPServiceUnavailable(text="audio copy failed")
                log.info("main audio copy %s in %.1f s for a download ('%s')", src_id, time.time() - started,
                         src["title"])
        AUDIO_COPIES.add(src_id)
        await push_copies()
    headers["Content-Type"] = "audio/x-matroska"
    return web.FileResponse(out, chunk_size=512 * 1024, headers=headers)


async def media(request):
    it = LIB.get(int(request.match_info["id"]))
    if not it:
        raise web.HTTPNotFound()
    path = item_path(it)
    if not os.path.isfile(path):
        log.warning("media %s: file missing (%s), rescanning", it["id"], it["relpath"])
        asyncio.create_task(run_scan())
        raise web.HTTPNotFound()
    if request.query.get("audio") == "best":  # offline download (iPhone): audio only, best version of the group
        return await media_best(it)
    if request.query.get("audio") == "main":  # the sink: only the track the TV plays
        variant = request.query.get("v")
        if it["kind"] == "video" and variant not in ("copy", "orig"):
            # Redirect to a fixed variant, so an open stream's later Range requests never switch files when the
            # copy appears. Cacheable: ffmpeg re-requests the first URL on every seek unless the redirect is cached.
            variant = "copy" if main_copy_fresh(it) else "orig"
            if variant == "orig":  # no waiting: the original now (the sink picks the track), the copy later
                queue_copies([it["id"]], front=True)
            raise web.HTTPFound(f"/media/{it['id']}?audio=main&v={variant}",
                                headers={"Cache-Control": "private, max-age=86400",
                                         "X-Audio-Copy": "main" if variant == "copy" else "original"})
        if it["kind"] == "video" and variant == "copy":
            if not main_copy_path(it["id"]).is_file():
                raise web.HTTPNotFound()
            return web.FileResponse(main_copy_path(it["id"]), chunk_size=512 * 1024,
                                    headers={"Content-Type": "audio/x-matroska", "X-Audio-Copy": "main"})
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return web.FileResponse(path, chunk_size=512 * 1024, headers={"Content-Type": ctype, "X-Audio-Copy": "original"})
    if request.query.get("audio") == "1" and it["kind"] == "video":
        out = AUDIO_DIR / f"{it['id']}.mka"
        fresh = lambda: out.exists() and out.stat().st_mtime >= it["mtime"]  # noqa: E731
        if not fresh():
            async with AUDIO_LOCKS.setdefault(it["id"], asyncio.Lock()):  # concurrent requests: copy once
                if not fresh():
                    await asyncio.get_running_loop().run_in_executor(None, make_audio_only, it, out)
        if fresh():
            return web.FileResponse(out, chunk_size=512 * 1024, headers={"Content-Type": "audio/x-matroska"})
        # copy failed: fall through and serve the original file
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return web.FileResponse(path, chunk_size=512 * 1024, headers={"Content-Type": ctype})


def make_art(it, out):
    """Write a <=800 px JPEG for the item to out. Returns True if art exists."""
    from PIL import Image
    path = item_path(it)
    data = None
    if it["kind"] == "audio":
        data = scanner.embedded_art(path)
        if data is None:
            fa = scanner.folder_art(os.path.dirname(path))
            if fa:
                data = Path(fa).read_bytes()
    else:
        t = min(30.0, (it.get("duration") or 0) * 0.25)
        res = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{t:.1f}", "-i", path, "-frames:v", "1",
             "-vf", "scale=800:-2", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
            capture_output=True, timeout=120,
        )
        data = res.stdout or None
    if not data:
        out.with_suffix(".none").touch()
        return False
    img = Image.open(BytesIO(data)).convert("RGB")
    img.thumbnail((800, 800))
    tmp = out.with_name(f"{out.stem}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    img.save(tmp, "JPEG", quality=85)
    tmp.replace(out)
    return True


async def art(request):
    it = LIB.get(int(request.match_info["id"]))
    if not it:
        raise web.HTTPNotFound()
    out = CACHE / "art" / f"{it['id']}.jpg"
    none_marker = out.with_suffix(".none")
    fresh = lambda p: p.exists() and p.stat().st_mtime >= it["mtime"]  # noqa: E731
    lock = ART_LOCKS.setdefault(it["id"], asyncio.Lock())
    async with lock:  # concurrent requests for the same item: generate once
        if fresh(none_marker):
            raise web.HTTPNotFound()
        if not fresh(out):
            ok = await asyncio.get_running_loop().run_in_executor(None, make_art, it, out)
            if not ok:
                raise web.HTTPNotFound()
    return web.FileResponse(out, headers={"Cache-Control": "max-age=86400"})


SCAN_LOCK = asyncio.Lock()


LOUDNESS_TASK = None


def _loudness_result(item_id, lufs, tpeak):
    it = LIB.get(item_id)
    if it is not None:
        it["lufs"], it["tpeak"] = lufs, tpeak


async def run_loudness():
    """Measure loudness of files that have no value yet, in the background (low priority). The apps
    get the values with the next queue/library push."""
    global LOUDNESS_TASK
    if LOUDNESS_TASK is not None and not LOUDNESS_TASK.done():
        return
    async def job():
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, loudness.run, DB, dict(ROOTS), _loudness_result)
        except Exception:
            log.exception("loudness job failed")
    LOUDNESS_TASK = asyncio.create_task(job())


LYRICS_TASK = None


def _lyrics_result(item_id, lvl):
    it = LIB.get(item_id)
    if it is not None:
        it["lyr"] = lvl


async def run_lyrics():
    """Read the embedded lyrics of files that have not been looked at yet (fast: tags + one ffmpeg per video)."""
    global LYRICS_TASK
    if LYRICS_TASK is not None and not LYRICS_TASK.done():
        return

    async def job():
        try:
            await asyncio.get_running_loop().run_in_executor(None, lyrics.run, DB, dict(ROOTS), _lyrics_result)
        except Exception:
            log.exception("lyrics job failed")
    LYRICS_TASK = asyncio.create_task(job())


async def api_keyframes(request):
    """{"id", "times": [s, ...]} = video keyframe times of one item (empty for songs). Cached per file mtime."""
    it = LIB.get(int(request.match_info["id"]))
    if not it:
        raise web.HTTPNotFound(text="no such item")
    times = []
    if it["kind"] == "video":
        cached = KEYFRAMES.get(it["id"])
        if cached and cached[0] == it["mtime"]:
            times = cached[1]
        else:
            path = item_path(it)
            if not os.path.isfile(path):
                raise web.HTTPNotFound(text="file missing")
            async with KEYFRAME_SEM:
                cached = KEYFRAMES.get(it["id"])
                if cached and cached[0] == it["mtime"]:
                    times = cached[1]
                else:
                    started = time.time()
                    try:
                        times = await asyncio.get_running_loop().run_in_executor(None, video_keyframes, path)
                    except (subprocess.SubprocessError, OSError) as e:
                        log.warning("keyframes %s: %s", it["id"], e)
                        raise web.HTTPServiceUnavailable(text="keyframe index failed")
                    KEYFRAMES[it["id"]] = (it["mtime"], times)
                    log.info("keyframes %s: %d in %.1f s ('%s')", it["id"], len(times), time.time() - started, it["title"])
    resp = web.json_response({"id": it["id"], "times": times})
    resp.enable_compression()
    return resp


async def api_lyrics(request):
    """Embedded lyrics of one item: {"id", "lyr": 0|1|2, "tracks": [{ord, lang, title, is_default, forced,
    synced, cues: [{s, e, t}]}]}. Extracted on demand when the background job has not reached the file yet."""
    item_id = int(request.match_info["id"])
    found = await asyncio.get_running_loop().run_in_executor(None, lyrics.get_or_extract, DB, dict(ROOTS), item_id)
    if found is None:
        raise web.HTTPNotFound(text="no such item")
    lvl, data = found
    _lyrics_result(item_id, lvl)
    resp = web.json_response({"id": item_id, "lyr": lvl, "tracks": data["tracks"]})
    resp.headers["Cache-Control"] = "no-cache"
    resp.enable_compression()
    return resp


async def api_lyrics_status(request):
    counts = await asyncio.get_running_loop().run_in_executor(None, lyrics.counts, DB)
    counts["running"] = LYRICS_TASK is not None and not LYRICS_TASK.done()
    return web.json_response(counts)


async def api_audio_copies(request):
    """GET: progress. POST: build every missing main audio copy in the background (409 while that job runs)."""
    if request.method == "POST":
        if COPY_JOB["running"]:
            return web.json_response({"running": True}, status=409)
        ids = [it["id"] for it in copy_candidates() if it["id"] not in AUDIO_COPIES]
        for i in ids:
            COPY_FAILED.pop(i, None)  # the button retries failed ones
        COPY_JOB.update(running=bool(ids), done=0, total=len(ids), ids=set(ids))
        log.info("audio copies: job started, %d missing", len(ids))
        queue_copies(ids)
        await push_copies(force=True)
    return web.json_response(copies_status())


async def api_loudness(request):
    counts = await asyncio.get_running_loop().run_in_executor(None, loudness.counts, DB)
    counts["running"] = LOUDNESS_TASK is not None and not LOUDNESS_TASK.done()
    return web.json_response(counts)


async def run_scan(force=False, wait=False):
    """wait=False: skip if a scan is already running. wait=True: wait for it, then scan again
    (unchanged files are skipped, a rescan takes well under a second)."""
    global SCANNING
    if SCAN_LOCK.locked() and not wait:
        return
    async with SCAN_LOCK:
        SCANNING = True
        await broadcast_all()
        try:
            stats = await asyncio.get_running_loop().run_in_executor(
                None, scanner.scan, DB, dict(ROOTS), force, settings_mod.skip_matcher(SETTINGS.skip))
            log.info("scan done: %s", stats)
            for item_id in stats.get("renamed_ids", []):  # art is cached by id; the file changed
                for stale in (*(CACHE / "art").glob(f"{item_id}.*"), *AUDIO_DIR.glob(f"{item_id}.*")):
                    stale.unlink(missing_ok=True)
            load_library()
            load_audio_copies()  # stale / removed copies drop out of the badge set
            for s in SESSIONS.values():
                s.prune()
                s.save()
        except Exception:
            log.exception("scan failed")
        finally:
            SCANNING = False
            await broadcast_all()
    await run_lyrics()
    await run_loudness()

# ---------------------------------------------------------------- settings, status, backup (web remote: Settings)
# Every change below is open on the home network; with an admin password set (optional, Settings -> Password) it needs
# an unlocked browser (cookie with a server-side token). Playback, queues and playlists never need it.

APP_REF: dict = {}
RESCAN_WAKE = asyncio.Event()
ADMIN_COOKIE = "atv_admin"


def server_name():
    return SETTINGS.server_name(socket.gethostname().split(".")[0])


def admin_ok(request):
    return SETTINGS.authorized(request.cookies.get(ADMIN_COOKIE))


def need_admin(request):
    if not admin_ok(request):
        raise web.HTTPForbidden(text="locked: enter the admin password in Settings")


def settings_error(e):
    return web.HTTPBadRequest(text=str(e))


def apply_roots():
    """settings.json -> ROOTS / ROOT_NAMES (in place: the scanner, loudness and lyrics workers hold these dicts)."""
    ROOTS.clear()
    ROOTS.update(SETTINGS.roots())
    ROOT_NAMES.clear()
    ROOT_NAMES.update(SETTINGS.root_names())


def root_usage():
    """Per root key: items, how many playlist entries and hearts point at them (what removing it would affect)."""
    by_root: dict = {}
    for it in LIB.values():
        by_root.setdefault(it["root"], set()).add(it["id"])
    out = {}
    for key in set(ROOTS) | set(by_root):
        ids = by_root.get(key, set())
        out[key] = {"items": len(ids),
                    "playlist_entries": sum(1 for pl in PLAYLISTS.values() for i in pl["items"] if i in ids),
                    "loved": sum(1 for i in LOVED if i in ids)}
    return out


def settings_view(request):
    v = SETTINGS.public()
    usage = root_usage()
    for r in v["roots"]:
        r.update(usage.get(r["key"], {"items": 0, "playlist_entries": 0, "loved": 0}))
        r["online"] = os.path.isdir(r["path"])
    v.update({"unlocked": admin_ok(request), "server_name_default": socket.gethostname().split(".")[0],
              "next_rescan": settings_mod.next_rescan(SETTINGS.rescan, LAST_SCAN.get("finished_at")),
              "scanning": SCANNING})
    return v


async def api_settings(request):
    return web.json_response(settings_view(request))


async def api_settings_unlock(request):
    body = await _json_body(request)
    token = SETTINGS.unlock(str(body.get("password") or ""))
    if token is None:
        await asyncio.sleep(1.0)  # slows down guessing
        log.warning("settings: wrong admin password from %s", request.remote)
        raise web.HTTPForbidden(text="wrong password")
    resp = web.json_response({"ok": True})
    resp.set_cookie(ADMIN_COOKIE, token, max_age=settings_mod.TOKEN_SECONDS, httponly=True, samesite="Strict",
                    secure=request.secure)
    return resp


async def api_settings_lock(request):
    SETTINGS.tokens.pop(request.cookies.get(ADMIN_COOKIE) or "", None)
    resp = web.json_response({"ok": True})
    resp.del_cookie(ADMIN_COOKIE)
    return resp


async def api_settings_password(request):
    need_admin(request)
    body = await _json_body(request)
    password = str(body.get("password") or "")
    if password and len(password) < 6:
        raise web.HTTPBadRequest(text="at least 6 characters")
    SETTINGS.set_password(password)
    log.info("settings: admin password %s", "set" if password else "removed")
    resp = web.json_response({"ok": True, "admin": SETTINGS.has_password})
    if password:  # this browser stays unlocked
        resp.set_cookie(ADMIN_COOKIE, SETTINGS.unlock(password), max_age=settings_mod.TOKEN_SECONDS, httponly=True,
                        samesite="Strict", secure=request.secure)
    return resp


async def api_settings_browse(request):
    need_admin(request)
    try:
        return web.json_response(SETTINGS.browse(request.query.get("path")))
    except settings_mod.SettingsError as e:
        raise settings_error(e)


async def library_changed(wait):
    """After a library-folder / skip change: rescan (now and waited for, or in the background) and tell every page."""
    apply_roots()
    if wait:
        await run_scan(wait=True)
    else:
        asyncio.create_task(run_scan())
    await broadcast_all()


async def api_settings_root_add(request):
    need_admin(request)
    body = await _json_body(request)
    try:
        key = SETTINGS.add_root(str(body.get("path") or ""), str(body.get("name") or ""))
    except settings_mod.SettingsError as e:
        raise settings_error(e)
    log.info("settings: library folder %s added (%s)", key, ROOTS.get(key) or SETTINGS.roots()[key])
    await library_changed(wait=False)  # a new folder may take minutes to read: in the background
    return web.json_response(settings_view(request))


async def api_settings_root_update(request):
    need_admin(request)
    key = request.match_info["key"]
    body = await _json_body(request)
    old_path = ROOTS.get(key)
    try:
        SETTINGS.update_root(key, body.get("name"), body.get("path"))
    except settings_mod.SettingsError as e:
        raise settings_error(e)
    moved = SETTINGS.roots().get(key) != old_path
    log.info("settings: library folder %s -> name %r, path %s", key, SETTINGS.root_names()[key], SETTINGS.roots()[key])
    await library_changed(wait=moved)
    return web.json_response(settings_view(request))


async def api_settings_root_delete(request):
    need_admin(request)
    key = request.match_info["key"]
    try:
        SETTINGS.remove_root(key)
    except settings_mod.SettingsError as e:
        raise settings_error(e)
    log.info("settings: library folder %s removed", key)
    await library_changed(wait=True)  # its items go now: no request may still look for its path
    return web.json_response(settings_view(request))


async def api_settings_skip(request):
    need_admin(request)
    body = await _json_body(request)
    try:
        SETTINGS.set_skip(body.get("skip") if isinstance(body.get("skip"), list) else [])
    except settings_mod.SettingsError as e:
        raise settings_error(e)
    log.info("settings: skip %s", SETTINGS.skip)
    await library_changed(wait=True)
    return web.json_response(settings_view(request))


async def api_settings_rescan(request):
    need_admin(request)
    body = await _json_body(request)
    try:
        SETTINGS.set_rescan({k: body[k] for k in ("mode", "hours", "at") if k in body})
    except settings_mod.SettingsError as e:
        raise settings_error(e)
    log.info("settings: rescan %s", SETTINGS.rescan)
    RESCAN_WAKE.set()
    return web.json_response(settings_view(request))


async def api_settings_server(request):
    need_admin(request)
    body = await _json_body(request)
    SETTINGS.set_server_name(body.get("name"))
    log.info("settings: server name %r", server_name())
    asyncio.create_task(readvertise())
    await broadcast_all()
    return web.json_response(settings_view(request))


async def readvertise():
    app = APP_REF.get("app")
    if app is None or "zeroconf" not in app:
        return
    zc, info = app.pop("zeroconf")
    try:
        await zc.async_unregister_service(info)
        await zc.async_close()
    except Exception:
        log.exception("Bonjour: unregister failed")
    await advertise(app)


async def rescan_loop():
    """The library rescan schedule (Settings -> Library). Replaces the old systemd timer."""
    while True:
        due = settings_mod.next_rescan(SETTINGS.rescan, LAST_SCAN.get("finished_at"))
        RESCAN_WAKE.clear()
        timeout = None if due is None else max(1.0, due - time.time())
        try:
            await asyncio.wait_for(RESCAN_WAKE.wait(), timeout)
            continue  # schedule changed: compute again
        except asyncio.TimeoutError:
            pass
        log.info("scheduled rescan (%s)", SETTINGS.rescan)
        try:
            await run_scan()
        except Exception:
            log.exception("scheduled rescan failed")
        await asyncio.sleep(1.0)


def dir_size(path, pattern="*"):
    n = size = 0
    for f in Path(path).glob(pattern):
        try:
            if f.is_file():
                n += 1
                size += f.stat().st_size
        except OSError:
            pass
    return n, size


async def api_status(request):
    loop = asyncio.get_running_loop()
    loud = await loop.run_in_executor(None, loudness.counts, DB)
    lyr = await loop.run_in_executor(None, lyrics.counts, DB)
    audio_n, audio_b = await loop.run_in_executor(None, dir_size, AUDIO_DIR, "*.mka")
    art_n, art_b = await loop.run_in_executor(None, dir_size, CACHE / "art")
    usage = root_usage()
    visible = [it for it in LIB.values() if not it["hidden"]]
    return web.json_response({
        "version": VERSION, "server_name": server_name(), "started": STARTED, "now": time.time(),
        "items": {"songs": sum(1 for it in visible if it["kind"] == "audio"),
                  "videos": sum(1 for it in visible if it["kind"] == "video"),
                  "hidden": len(LIB) - len(visible)},
        "roots": [{"key": k, "name": ROOT_NAMES.get(k, k), "path": p, "online": os.path.isdir(p),
                   "items": usage.get(k, {}).get("items", 0)} for k, p in ROOTS.items()],
        "scanning": SCANNING, "last_scan": LAST_SCAN, "rescan": SETTINGS.rescan,
        "next_rescan": settings_mod.next_rescan(SETTINGS.rescan, LAST_SCAN.get("finished_at")),
        "cache": {"audio_files": audio_n, "audio_bytes": audio_b, "art_files": art_n, "art_bytes": art_b,
                  "copy_job": bool(COPY_JOB["running"])},
        "loudness": loud, "lyrics": lyr, "playlists": len(PLAYLISTS), "loved": len(LOVED),
        "plays": sum(PLAY_COUNT.values()), "sessions": sessions_summary(), "sink": sink_state("tv"),
        "admin": SETTINGS.has_password,
    })


async def api_cache_clear(request):
    """Delete the audio copies (they are made again when needed). Not while the copy job runs."""
    need_admin(request)
    if COPY_JOB["running"]:
        raise web.HTTPConflict(text="audio copies are being made right now: try again when that is done")
    removed = freed = 0
    for f in list(AUDIO_DIR.glob("*.mka")):
        if f.name.endswith(".tmp.mka"):
            continue
        try:
            freed += f.stat().st_size
            f.unlink()
            removed += 1
        except OSError:
            pass
    load_audio_copies()
    PREFETCHED.clear()
    COPY_FAILED.clear()
    await push_copies(force=True)
    log.info("cache: %d audio copies deleted (%.1f MB)", removed, freed / 1e6)
    return web.json_response({"ok": True, "removed": removed, "freed": freed})


# Backup: playlists, hearts and the play history in one JSON file. Every id is stored with its file (root + path), so
# a restore onto a rebuilt library (new ids) finds the same files again; entries whose file is gone are left out.
BACKUP_FORMAT = "atv-media-backup"


def backup_data():
    con = listening()
    loved = con.execute("SELECT id, at FROM loved ORDER BY at").fetchall()
    plays = con.execute("SELECT id, at FROM plays ORDER BY at").fetchall()
    ids = {i for i, _ in loved} | {i for i, _ in plays} | {i for pl in PLAYLISTS.values() for i in pl["items"]}
    return {"format": BACKUP_FORMAT, "version": 1, "created": time.time(), "server": server_name(),
            "server_version": VERSION,
            "files": {str(i): [LIB[i]["root"], LIB[i]["relpath"]] for i in sorted(ids) if i in LIB},
            "playlists": [{"name": pl["name"], "items": pl["items"], "created": pl["created"]}
                          for pl in sorted(PLAYLISTS.values(), key=lambda p: p["id"])],
            "loved": [[i, at] for i, at in loved], "plays": [[i, at] for i, at in plays],
            "box_sound": SESSIONS["box"].sound}


async def api_backup_get(request):
    need_admin(request)
    stamp = time.strftime("%Y%m%d-%H%M")
    return web.json_response(backup_data(), headers={
        "Content-Disposition": f'attachment; filename="atv-media-backup-{stamp}.json"'})


async def api_backup_restore(request):
    """Replaces playlists, hearts and the play history with the backup's (ids mapped through the stored file paths)."""
    global NEXT_PLAYLIST_ID
    need_admin(request)
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text="not a JSON file")
    if not isinstance(data, dict) or data.get("format") != BACKUP_FORMAT:
        raise web.HTTPBadRequest(text="not an ATV Player backup")
    by_path = {(it["root"], it["relpath"]): i for i, it in LIB.items()}
    rel_count: dict = {}
    for it in LIB.values():
        rel_count[it["relpath"]] = rel_count.get(it["relpath"], 0) + 1
    by_rel = {it["relpath"]: i for i, it in LIB.items() if rel_count[it["relpath"]] == 1}  # another root key
    files = data.get("files") if isinstance(data.get("files"), dict) else {}
    lost: set = set()

    def mapped(old):
        try:
            old = int(old)
        except (TypeError, ValueError):
            return None
        f = files.get(str(old))
        new = None
        if isinstance(f, list) and len(f) == 2:
            new = by_path.get((f[0], f[1]))
            if new is None:
                new = by_rel.get(f[1])
        if new is None:
            lost.add(old)
        return new

    def pairs(rows):
        out = []
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, list) and len(row) == 2:
                new = mapped(row[0])
                try:
                    at = float(row[1])
                except (TypeError, ValueError):
                    continue
                if new is not None:
                    out.append((new, at))
        return out

    loved = pairs(data.get("loved"))
    plays = pairs(data.get("plays"))
    lists = []
    for pl in data.get("playlists") if isinstance(data.get("playlists"), list) else []:
        if isinstance(pl, dict) and str(pl.get("name") or "").strip():
            items = [n for n in (mapped(i) for i in pl.get("items") or []) if n is not None]
            lists.append((str(pl["name"]).strip()[:120], items, pl.get("created")))
    con = listening()
    with con:
        con.execute("DELETE FROM loved")
        con.executemany("INSERT OR REPLACE INTO loved (id, at) VALUES (?, ?)", loved)
        con.execute("DELETE FROM plays")
        con.executemany("INSERT INTO plays (id, at) VALUES (?, ?)", plays)
    load_listening()
    cutoff = time.time() - HISTORY_DAYS * 86400
    PLAYED.clear()
    for i, at in plays:
        if at > cutoff and at > PLAYED.get(i, 0):
            PLAYED[i] = at
    save_history()
    PLAYLISTS.clear()
    now = time.time()
    for n, (name, items, created) in enumerate(lists, start=1):
        PLAYLISTS[n] = {"id": n, "name": name, "items": items, "created": created or now, "updated": now}
    NEXT_PLAYLIST_ID = len(lists) + 1
    save_playlists()
    if isinstance(data.get("box_sound"), dict):
        SESSIONS["box"].sound = box_sound.clean(data["box_sound"])
        SESSIONS["box"].save()
    for s in SESSIONS.values():
        s.source, s.source_items = None, []
    log.info("backup restored: %d playlists, %d hearts, %d plays, %d ids without their file",
             len(lists), len(loved), len(plays), len(lost))
    await playlists_changed()
    await loved_changed()
    return web.json_response({"ok": True, "playlists": len(lists), "loved": len(loved), "plays": len(plays),
                              "missing": len(lost)})


async def api_atv_audio(request):
    """Web remote -> the atv-audio program: sound output and headphones (list / scan / pair / connect / disconnect /
    forget). The program answers with an `audio_info` message, shown in the state's sink.audio."""
    need_admin(request)
    body = await _json_body(request)
    op = body.get("op")
    if op not in ("refresh", "set_output", "scan", "pair", "connect", "disconnect", "forget"):
        raise web.HTTPBadRequest(text="unknown op")
    sink = SINKS.get("tv")
    if sink is None:
        raise web.HTTPServiceUnavailable(text="atv-audio is not connected")
    msg = {"type": "audio_cmd", "op": op}
    for k in ("address", "output"):
        if isinstance(body.get(k), str):
            msg[k] = body[k][:200]
    log.info("atv-audio: %s %s", op, msg.get("address") or msg.get("output") or "")
    await send(sink, msg)
    return web.json_response({"ok": True})



def advertised_ip():
    """Address the apps should use: ATV_ADVERTISE_IP, else the source address of the default route
    (UDP connect sends no packet)."""
    if os.environ.get("ATV_ADVERTISE_IP"):
        return os.environ["ATV_ADVERTISE_IP"]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("192.0.2.1", 9))  # TEST-NET-1, never actually contacted
        return sock.getsockname()[0]


async def advertise(app):
    """Bonjour/mDNS service _atvmedia._tcp so the apps find the server without a configured address.
    Only with ATV_BONJOUR=1 (set in the systemd unit), so throwaway test instances stay invisible."""
    if os.environ.get("ATV_BONJOUR") != "1":
        return
    try:
        from zeroconf import IPVersion
        from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf
    except ImportError:
        log.warning("python3-zeroconf not installed: not advertising over Bonjour")
        return
    try:
        ip = advertised_ip()
        host = socket.gethostname().split(".")[0]
        name = re.sub(r"[.\x00-\x1f]", " ", server_name())[:50]
        info = AsyncServiceInfo("_atvmedia._tcp.local.", f"ATV Player on {name}._atvmedia._tcp.local.",
                                addresses=[socket.inet_aton(ip)], port=PORT, server=f"{host}.local.",
                                properties={"url": f"http://{ip}:{PORT}", "version": VERSION, "name": server_name()})
        zc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        await zc.async_register_service(info, allow_name_change=True)
        app["zeroconf"] = (zc, info)
        log.info("Bonjour: advertising _atvmedia._tcp at http://%s:%s", ip, PORT)
    except Exception:
        log.exception("Bonjour advertising failed")


async def on_cleanup(app):
    loudness.STOP.set()
    lyrics.STOP.set()
    if LISTEN is not None:
        LISTEN.close()
    if "zeroconf" in app:
        zc, info = app["zeroconf"]
        await zc.async_unregister_service(info)
        await zc.async_close()


async def on_startup(app):
    DATA.mkdir(parents=True, exist_ok=True)
    (CACHE / "art").mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    for f in AUDIO_DIR.glob("*.tmp.mka"):  # partial copies of a server that was stopped mid-copy
        f.unlink(missing_ok=True)
    load_library()
    load_audio_copies()
    for s in SESSIONS.values():
        s.load()
    load_playlists()
    load_history()
    load_listening()
    APP_REF["app"] = app
    asyncio.create_task(run_scan())
    asyncio.create_task(rescan_loop())
    await advertise(app)


def make_app():
    app = web.Application(middlewares=[api_no_store], client_max_size=64 * 1024 * 1024)  # backup restore
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/api/library", api_library)
    app.router.add_get("/api/collection/{name}", api_collection)
    app.router.add_get("/api/offline/{name}", api_offline)
    app.router.add_get("/api/folders", api_folders)
    app.router.add_get("/api/browse", api_browse)
    app.router.add_get("/api/item/{id:\\d+}", api_item)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/history", api_history)
    app.router.add_get("/api/auto", api_auto)
    app.router.add_get("/api/playlists", api_playlists)
    app.router.add_post("/api/playlists", api_playlist_create)
    app.router.add_get("/api/playlists/{id:\\d+}", api_playlist_get)
    app.router.add_put("/api/playlists/{id:\\d+}", api_playlist_update)
    app.router.add_post("/api/playlists/{id:\\d+}/items", api_playlist_add)
    app.router.add_delete("/api/playlists/{id:\\d+}", api_playlist_delete)
    app.router.add_post("/api/rescan", api_rescan)
    app.router.add_get("/api/settings", api_settings)
    app.router.add_post("/api/settings/unlock", api_settings_unlock)
    app.router.add_post("/api/settings/lock", api_settings_lock)
    app.router.add_post("/api/settings/password", api_settings_password)
    app.router.add_get("/api/settings/browse", api_settings_browse)
    app.router.add_post("/api/settings/roots", api_settings_root_add)
    app.router.add_put("/api/settings/roots/{key}", api_settings_root_update)
    app.router.add_delete("/api/settings/roots/{key}", api_settings_root_delete)
    app.router.add_put("/api/settings/skip", api_settings_skip)
    app.router.add_put("/api/settings/rescan", api_settings_rescan)
    app.router.add_put("/api/settings/server", api_settings_server)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/cache/clear", api_cache_clear)
    app.router.add_get("/api/backup", api_backup_get)
    app.router.add_post("/api/backup", api_backup_restore)
    app.router.add_post("/api/atv-audio", api_atv_audio)
    app.router.add_get("/api/loudness", api_loudness)
    app.router.add_get("/api/audio_copies", api_audio_copies)
    app.router.add_post("/api/audio_copies", api_audio_copies)
    app.router.add_get("/api/lyrics", api_lyrics_status)
    app.router.add_get("/api/lyrics/{id:\\d+}", api_lyrics)
    app.router.add_get("/api/keyframes/{id:\\d+}", api_keyframes)
    app.router.add_post("/api/screenshot", api_screenshot)
    app.router.add_get("/shots/{name}", shot_file)
    app.router.add_get("/media/{id:\\d+}", media)
    app.router.add_get("/art/{id:\\d+}", art)
    app.router.add_static("/static/", STATIC)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    web.run_app(make_app(), port=PORT, access_log=None)
