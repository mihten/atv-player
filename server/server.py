#!/usr/bin/env python3
"""ATV Player media server.

- Library: scans the read-only roots (scanner.py) into SQLite, served as JSON.
- Streaming: /media/<id> serves the original file with HTTP Range (VLC on the Apple TV plays it as is).
- Artwork: /art/<id> embedded/folder cover for audio, a frame grab for video (cached).
- Queue + remote control: one queue per player kind (Apple TV app = "tv", iPhone app = "phone"), so the
  devices play independently. Player apps connect as role=player&kind=<kind>; the web UI at / is a
  role=remote that controls one of the queues (TV by default). All state changes are pushed over
  WebSocket; nothing polls.
"""
import asyncio
import json
import logging
import mimetypes
import os
import random
import re
import socket
import subprocess
import time
from io import BytesIO
from pathlib import Path

from aiohttp import WSMsgType, web

import loudness
import lyrics
import scanner

# Site config comes from the environment (systemd EnvironmentFile, see server/site.example/).
# ATV_ROOTS is required: JSON {"<root key>": "<path>"}; the keys are stored in the library DB.
if not os.environ.get("ATV_ROOTS"):
    raise SystemExit("ATV_ROOTS is not set (JSON object root key -> library path)")
ROOTS = json.loads(os.environ["ATV_ROOTS"])
DATA = Path(os.environ.get("ATV_DATA", "/var/lib/atv-media"))
CACHE = Path(os.environ.get("ATV_CACHE", "/var/cache/atv-media"))
AUDIO_DIR = CACHE / "audio"
DB = DATA / "library.db"
STATE_FILE = DATA / "state.json"
STATIC = Path(__file__).resolve().parent / "static"
PORT = int(os.environ.get("ATV_PORT", "8080"))
VERSION = "0.15.0"
ROOT_NAMES = json.loads(os.environ.get("ATV_ROOT_NAMES") or "{}")  # optional display names per root key

log = logging.getLogger("atv-media")
mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("audio/x-wavpack", ".wv")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("audio/x-matroska", ".mka")

LIB: dict[int, dict] = {}
LAST_SCAN: dict = {}
SCANNING = False
PLAYERS: dict = {}  # ws -> session kind ("tv" / "phone")
REMOTES: dict = {}  # ws -> session kind the web remote controls
ART_LOCKS: dict[int, asyncio.Lock] = {}

SUMMARY_KEYS = ("id", "kind", "title", "artist", "album", "folder", "root", "duration", "lossless",
                "codec", "track", "disc", "year", "width", "height", "hidden", "lufs", "tpeak", "lyr")
FULL_KEYS = SUMMARY_KEYS + ("audio_track", "audio_ord", "sub_track", "sub_ord", "channels", "bitrate")


def summary(it, keys=SUMMARY_KEYS):
    d = {k: it.get(k) for k in keys}
    d["lossless"] = bool(it.get("lossless"))
    d["hidden"] = bool(it.get("hidden"))
    d["art"] = bool(it.get("has_art"))
    return d


def full(it):
    d = summary(it, FULL_KEYS)
    try:
        d["subs"] = json.loads(it.get("subs") or "null")
    except ValueError:
        d["subs"] = None
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


def collection(name, root=None, folder=None, recursive=True, playlist=None):
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
KINDS = ("tv", "phone")
KIND_NAMES = {"tv": "Apple TV", "phone": "iPhone"}
SAVED_KEYS = ("queue", "original", "index", "shuffle", "repeat", "position", "source", "source_items")


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

    @property
    def players(self):
        return [ws for ws, kind in PLAYERS.items() if kind == self.kind]

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
            "playing": self.playing, "position": self.position, "position_at": self.position_at,
            "server_time": time.time(), "duration": self.duration,
            "current": full(cur) if cur else None, "upcoming": upcoming,
            "players": len(self.players), "scanning": SCANNING, "last_scan": LAST_SCAN, "version": VERSION,
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
        self.prune()

    def prune(self):
        """Drop ids that vanished from the library (after a rescan)."""
        cur = self.current_id
        self.queue = [i for i in self.queue if i in LIB]
        self.original = [i for i in self.original if i in LIB]
        if cur in self.queue:
            self.index = self.queue.index(cur)
        elif self.queue:
            self.index = min(max(self.index, 0), len(self.queue) - 1)
        else:
            self.index = -1


SESSIONS = {kind: Session(kind) for kind in KINDS}


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
    cur = s.current_id
    if s.shuffle:
        keep = set(new)
        played = [i for i in s.queue[:max(s.index, 0)] if i in keep]
        rest = list(new)
        for i in played + ([cur] if cur is not None else []):
            if i in rest:
                rest.remove(i)
        random.shuffle(rest)
        s.queue = played + ([cur] if cur is not None else []) + rest
        s.index = len(played) if cur is not None else 0
    elif cur in new:
        s.queue, s.index = list(new), new.index(cur)
    else:
        keep = set(new)
        k = sum(1 for i in s.queue[:max(s.index, 0)] if i in keep)
        s.queue = new[:k] + ([cur] if cur is not None else []) + new[k:]
        s.index = k
    s.original, s.source_items = list(new), list(new)
    log.info("[%s] queue follows playlist %s: %d items now, current %s kept", s.kind, pl_id, len(s.queue), cur)
    return True


async def playlists_changed(pl_id=None):
    save_playlists()
    for s in SESSIONS.values():
        if pl_id is not None and s.source == pl_id and follow_playlist(s, pl_id):
            s.save()
            await broadcast_state(s)
    msg = {"type": "playlists", "id": pl_id}
    for ws in list(PLAYERS) + list(REMOTES):
        await send(ws, msg)


async def send(ws, msg):
    try:
        await ws.send_str(json.dumps(msg))
    except (ConnectionResetError, RuntimeError):
        pass


async def broadcast_state(s):
    """State of session s to its players and to the remotes watching it."""
    msg = {"type": "state", "state": s.to_dict()}
    for ws in s.players + [ws for ws, kind in list(REMOTES.items()) if kind == s.kind]:
        await send(ws, msg)
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
    for ws in s.players:
        await send(ws, msg)


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


async def start_current(s, position=0.0, autoplay=True, present=False):
    if not await ensure_current_file(s):
        return
    s.position, s.position_at = position, time.time()
    s.duration = (s.current or {}).get("duration") or 0.0
    s.want_play = autoplay
    s.playing = False
    s.save()
    await to_players(s, load_message(s, autoplay, position, present))
    await broadcast_state(s)


async def stop_playback(s):
    s.want_play = False
    s.playing = False
    await to_players(s, {"type": "control", "action": "stop"})
    await broadcast_state(s)


async def set_queue(s, ids, start_index=None, shuffle=False):
    """start_index None = beginning (or a random first track when shuffling)."""
    ids = [i for i in ids if i in LIB]
    if not ids:
        return
    if start_index is not None:
        start_index = min(max(int(start_index), 0), len(ids) - 1)
    s.original = list(ids)
    if shuffle:
        rest = [i for n, i in enumerate(ids) if n != start_index]
        random.shuffle(rest)
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
        if s.shuffle:
            random.shuffle(s.queue)
        s.index = 0
    else:
        s.index = len(s.queue) - 1
        await stop_playback(s)
        return
    await start_current(s)


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
            s.want_play = True
        else:
            s.want_play = False
        await to_players(s, {"type": "control", "action": cmd})
    elif cmd == "seek":
        await to_players(s, {"type": "control", "action": "seek", "position": float(msg.get("position", 0))})
    elif cmd == "shuffle":
        on = bool(msg.get("on", not s.shuffle))
        cur = s.current_id
        if on and not s.shuffle:
            rest = s.queue[s.index + 1:]
            random.shuffle(rest)
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
    elif cmd == "remove":
        idx = int(msg.get("index", -1))
        if 0 <= idx < len(s.queue) and idx != s.index:
            s.source, s.source_items = None, []
            del s.queue[idx]
            if idx < s.index:
                s.index -= 1
            s.save()
            await broadcast_state(s)
    elif cmd == "clear":
        s.source, s.source_items = None, []
        cur = s.current_id
        s.queue = [cur] if cur is not None else []
        s.original = list(s.queue)
        s.index = 0 if s.queue else -1
        s.save()
        await broadcast_state(s)
    elif cmd == "rescan":
        asyncio.create_task(run_scan())


async def on_message(s, data, role):
    kind = data.get("type")
    if kind == "cmd":
        await handle_cmd(s, data)
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
    role = "player" if q.get("role") == "player" else "remote"
    ws = web.WebSocketResponse(heartbeat=45)
    await ws.prepare(request)
    if role == "player":
        kind = q.get("kind") if q.get("kind") in KINDS else "tv"  # apps before 0.11 send no kind: the TV
        PLAYERS[ws] = kind
    else:
        kind = q.get("session") if q.get("session") in KINDS else "tv"
        REMOTES[ws] = kind
    s = SESSIONS[kind]
    log.info("%s (%s) connected from %s", role, kind, request.remote)
    await send(ws, {"type": "state", "state": s.to_dict()})
    if role == "player":
        if s.current and await ensure_current_file(s):
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
            await on_message(s, data, role)
    finally:
        log.info("%s (%s) disconnected", role, s.kind)
        if role == "player":
            PLAYERS.pop(ws, None)
            if not s.players:
                s.playing = False
                s.want_play = False
                s.save()
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
    return os.path.join(ROOTS[it["root"]], it["relpath"])


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


async def media(request):
    it = LIB.get(int(request.match_info["id"]))
    if not it:
        raise web.HTTPNotFound()
    path = item_path(it)
    if not os.path.isfile(path):
        log.warning("media %s: file missing (%s), rescanning", it["id"], it["relpath"])
        asyncio.create_task(run_scan())
        raise web.HTTPNotFound()
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
                None, loudness.run, DB, ROOTS, _loudness_result)
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
            await asyncio.get_running_loop().run_in_executor(None, lyrics.run, DB, ROOTS, _lyrics_result)
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
    found = await asyncio.get_running_loop().run_in_executor(None, lyrics.get_or_extract, DB, ROOTS, item_id)
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
            stats = await asyncio.get_running_loop().run_in_executor(None, scanner.scan, DB, ROOTS, force)
            log.info("scan done: %s", stats)
            for item_id in stats.get("renamed_ids", []):  # art is cached by id; the file changed
                for stale in (*(CACHE / "art").glob(f"{item_id}.*"), *AUDIO_DIR.glob(f"{item_id}.*")):
                    stale.unlink(missing_ok=True)
            load_library()
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
        info = AsyncServiceInfo("_atvmedia._tcp.local.", f"ATV Player on {host}._atvmedia._tcp.local.",
                                addresses=[socket.inet_aton(ip)], port=PORT, server=f"{host}.local.",
                                properties={"url": f"http://{ip}:{PORT}", "version": VERSION})
        zc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        await zc.async_register_service(info, allow_name_change=True)
        app["zeroconf"] = (zc, info)
        log.info("Bonjour: advertising _atvmedia._tcp at http://%s:%s", ip, PORT)
    except Exception:
        log.exception("Bonjour advertising failed")


async def on_cleanup(app):
    loudness.STOP.set()
    lyrics.STOP.set()
    if "zeroconf" in app:
        zc, info = app["zeroconf"]
        await zc.async_unregister_service(info)
        await zc.async_close()


async def on_startup(app):
    DATA.mkdir(parents=True, exist_ok=True)
    (CACHE / "art").mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    load_library()
    for s in SESSIONS.values():
        s.load()
    load_playlists()
    asyncio.create_task(run_scan())
    await advertise(app)


def make_app():
    app = web.Application(middlewares=[api_no_store])
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/api/library", api_library)
    app.router.add_get("/api/collection/{name}", api_collection)
    app.router.add_get("/api/folders", api_folders)
    app.router.add_get("/api/browse", api_browse)
    app.router.add_get("/api/item/{id:\\d+}", api_item)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/playlists", api_playlists)
    app.router.add_post("/api/playlists", api_playlist_create)
    app.router.add_get("/api/playlists/{id:\\d+}", api_playlist_get)
    app.router.add_put("/api/playlists/{id:\\d+}", api_playlist_update)
    app.router.add_post("/api/playlists/{id:\\d+}/items", api_playlist_add)
    app.router.add_delete("/api/playlists/{id:\\d+}", api_playlist_delete)
    app.router.add_post("/api/rescan", api_rescan)
    app.router.add_get("/api/loudness", api_loudness)
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
