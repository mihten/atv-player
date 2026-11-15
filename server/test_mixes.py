#!/usr/bin/env python3
"""Mixes (Home: the two configurable shuffle tiles), against a THROWAWAY server instance (own temp library, data dir and
port; never the real one).   python3 test_mixes.py   (needs aiohttp, mutagen, ffmpeg)
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
PORT = 18098
BASE = f"http://127.0.0.1:{PORT}"
failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def tone(path, seconds, freq):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
                    "-metadata", f"title={path.stem}", "-metadata", "artist=Test", str(path)], check=True)


async def next_msg(ws, types, timeout=8):
    while True:
        msg = await asyncio.wait_for(ws.receive_json(), timeout)
        if msg.get("type") in types:
            return msg


def start(tmp, env):
    return subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)


def stop(proc):
    proc.terminate()
    try:
        proc.wait(30)
    except subprocess.TimeoutExpired:
        proc.kill()


async def wait_up(http, want):
    for _ in range(100):
        try:
            async with http.get(f"{BASE}/api/library") as r:
                items = (await r.json())["items"]
                if len(items) == want:
                    return items
        except (aiohttp.ClientError, KeyError, ValueError):
            pass
        await asyncio.sleep(0.2)
    return []


async def state(http):
    async with http.get(f"{BASE}/api/state") as r:
        return await r.json()


async def put_mix(http, n, body):
    async with http.put(f"{BASE}/api/mixes/{n}", json=body) as r:
        return r.status, await r.json(content_type=None) if r.status == 200 else await r.text()


async def play(player, n, **extra):
    await player.send_json({"type": "cmd", "cmd": "play_collection", "collection": "mix", "mix": n, "shuffle": True,
                            **extra})


async def main(tmp):
    lib = tmp / "lib"
    for n in range(1, 4):
        tone(lib / "rock" / f"Rock {n}.flac", 2, 300 + n * 50)
    tone(lib / "rock" / "live" / "Rock Live.flac", 2, 700)
    for n in range(1, 3):
        tone(lib / "jazz" / f"Jazz {n}.flac", 2, 800 + n * 50)
    tone(lib / "solo" / "Solo.flac", 2, 1200)
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(tmp / "data"),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT))
    proc = start(tmp, env)
    try:
        async with aiohttp.ClientSession() as http:
            items = await wait_up(http, 7)
            by = {i["title"]: i["id"] for i in items}
            check(len(items) == 7, f"library: {sorted(by)}")

            async with http.get(f"{BASE}/api/mixes") as r:
                mixes = (await r.json())["mixes"]
            check([m["name"] for m in mixes] == ["Mix 1", "Mix 2"] and all(not m["sources"] for m in mixes),
                  "two empty mixes named Mix 1 / Mix 2 to start with")
            st = await state(http)
            check(st["mixes"] == [{"name": "Mix 1", "folders": 0, "playlists": 0},
                                  {"name": "Mix 2", "folders": 0, "playlists": 0}], "the state carries the mix summary")

            player = await http.ws_connect(f"{BASE}/ws?role=player")
            await next_msg(player, {"state"})
            await play(player, 1)
            await asyncio.sleep(0.5)
            check((await state(http))["queue"] == [], "an empty mix plays nothing")

            # ---- validation
            code, _ = await put_mix(http, 3, {"name": "x"})
            check(code == 404, f"mix 3 does not exist: {code}")
            code, _ = await put_mix(http, 1, {"sources": [{"type": "playlist", "id": 99}]})
            check(code == 400, f"unknown playlist refused: {code}")
            code, _ = await put_mix(http, 1, {"sources": [{"type": "folder", "root": "nope", "folder": ""}]})
            check(code == 400, f"unknown root refused: {code}")
            code, _ = await put_mix(http, 1, {"sources": ["junk"]})
            check(code == 400, f"junk source refused: {code}")
            code, _ = await put_mix(http, 1, {"sources": "rock"})
            check(code == 400, f"sources must be a list: {code}")
            check((await state(http))["mixes"][0]["folders"] == 0, "a refused edit changes nothing")

            # ---- a mix of a folder (with subfolders) + a playlist that overlaps it
            async with http.post(f"{BASE}/api/playlists",
                                 json={"name": "Faves", "ids": [by["Rock 1"], by["Jazz 1"], by["Solo"]]}) as r:
                pid = (await r.json())["id"]
            code, body = await put_mix(http, 1, {"name": "Evening", "sources": [
                {"type": "folder", "root": "music", "folder": "rock", "recursive": True},
                {"type": "playlist", "id": pid},
                {"type": "playlist", "id": pid}]})  # the same source twice is kept once
            check(code == 200 and body["mixes"][0]["name"] == "Evening" and len(body["mixes"][0]["sources"]) == 2,
                  f"mix 1 saved, the double source dropped: {code}")
            check(body["mixes"][0]["count"] == 6, f"6 different files (4 rock + Jazz 1 + Solo): {body['mixes'][0]['count']}")
            sources = body["mixes"][0]["sources"]
            check(sources[0]["name"] == "rock" and sources[0]["count"] == 4 and sources[1]["name"] == "Faves"
                  and sources[1]["count"] == 3, f"source details: {[(x['name'], x['count']) for x in sources]}")
            got = None
            try:
                while got is None or got["mixes"][0]["name"] != "Evening":  # older queued states come first
                    got = (await next_msg(player, {"state"}, timeout=3))["state"]
            except asyncio.TimeoutError:
                pass
            check(got is not None and got["mixes"][0] == {"name": "Evening", "folders": 1, "playlists": 1},
                  "a connected player got the new summary at once")

            await play(player, 1)
            load = await next_msg(player, {"load"})
            st = await state(http)
            want = {by[t] for t in ("Rock 1", "Rock 2", "Rock 3", "Rock Live", "Jazz 1", "Solo")}
            check(len(st["queue"]) == 6 and set(st["queue"]) == want, f"queue = the union, each file once: {len(st['queue'])}")
            check(st["shuffle"] and st["repeat_mode"] == "all", "shuffled, repeat all")
            check(load["item"]["id"] in want, "the first item belongs to the mix")

            # ---- not recursive
            await put_mix(http, 2, {"name": "Rock only", "sources": [
                {"type": "folder", "root": "music", "folder": "rock", "recursive": False}]})
            await play(player, 2)
            await next_msg(player, {"load"})
            q = (await state(http))["queue"]
            check(len(q) == 3 and by["Rock Live"] not in q, f"a folder without subfolders: {len(q)} files")

            # ---- renaming, empty name, long name
            code, body = await put_mix(http, 2, {"name": "   "})
            check(body["mixes"][1]["name"] == "Mix 2", "an empty name falls back to Mix 2")
            code, body = await put_mix(http, 2, {"name": "x" * 80})
            check(len(body["mixes"][1]["name"]) == 40, "a long name is cut to 40")
            await put_mix(http, 2, {"name": "Rock only"})

            # ---- a vanished folder adds nothing; the mix still plays the rest
            await put_mix(http, 2, {"sources": [{"type": "folder", "root": "music", "folder": "gone"},
                                                {"type": "folder", "root": "music", "folder": "jazz"}]})
            await play(player, 2)
            await next_msg(player, {"load"})
            check(len((await state(http))["queue"]) == 2, "a missing folder is skipped, the jazz folder plays")
            async with http.get(f"{BASE}/api/mixes") as r:
                det = (await r.json())["mixes"][1]["sources"]
            check(det[0]["missing"] and not det[1]["missing"], "the missing folder is flagged in the details")

            # ---- survives a restart
            await player.close()
            stop(proc)
            proc = start(tmp, env)
            await wait_up(http, 7)
            st = await state(http)
            check(st["mixes"][0] == {"name": "Evening", "folders": 1, "playlists": 1}, f"mixes kept after a restart: {st['mixes']}")

            # ---- deleting a playlist removes it from the mixes
            async with http.delete(f"{BASE}/api/playlists/{pid}") as r:
                check(r.status == 200, "playlist deleted")
            st = await state(http)
            check(st["mixes"][0] == {"name": "Evening", "folders": 1, "playlists": 0}, f"the playlist left the mix: {st['mixes'][0]}")

            # ---- a corrupt file does not stop the server
            stop(proc)
            (tmp / "data" / "mixes.json").write_text("{not json")
            proc = start(tmp, env)
            await wait_up(http, 7)
            check((await state(http))["mixes"][0]["name"] == "Mix 1", "a corrupt mixes.json falls back to the defaults")
    finally:
        stop(proc)


def backup_roundtrip(tmp):
    """backup_mixes / restore_mixes (a restore renumbers the playlists 1..n)."""
    os.environ.update(ATV_ROOTS=json.dumps({"music": str(tmp)}), ATV_DATA=str(tmp / "d2"), ATV_CACHE=str(tmp / "c2"))
    (tmp / "d2").mkdir()
    sys.path.insert(0, str(HERE))
    import server as srv
    srv.PLAYLISTS.clear()
    for pid, name in ((4, "A"), (9, "B"), (12, "C")):
        srv.PLAYLISTS[pid] = {"id": pid, "name": name, "items": [], "created": 0, "updated": 0}
    srv.MIXES[:] = [{"name": "One", "sources": [{"type": "playlist", "id": 9},
                                               {"type": "folder", "root": "music", "folder": "x", "recursive": True},
                                               {"type": "playlist", "id": 77}]},  # 77 no longer exists
                    {"name": "Two", "sources": [{"type": "playlist", "id": 12}]}]
    rows = srv.backup_mixes()
    check(rows[0]["sources"][0] == {"type": "playlist", "at": 1} and len(rows[0]["sources"]) == 2,
          f"backup stores playlists by position and leaves a dead one out: {rows[0]['sources']}")
    srv.restore_mixes(rows, 3)  # playlists are now 1, 2, 3 = A, B, C
    check(srv.MIXES[0]["sources"][0] == {"type": "playlist", "id": 2} and srv.MIXES[1]["sources"] == [{"type": "playlist", "id": 3}],
          f"restore maps them to the new numbers: {srv.MIXES}")
    srv.restore_mixes(None, 3)  # a backup from before mixes: folders stay, playlist sources go (numbers changed)
    check(srv.MIXES[0]["sources"] == [{"type": "folder", "root": "music", "folder": "x", "recursive": True}]
          and srv.MIXES[1]["sources"] == [], "an old backup keeps the folders and drops the playlists")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="atv-mixes-") as t:
        asyncio.run(main(Path(t)))
    with tempfile.TemporaryDirectory(prefix="atv-mixes2-") as t:
        backup_roundtrip(Path(t))
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
