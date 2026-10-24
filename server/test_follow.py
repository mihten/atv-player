#!/usr/bin/env python3
"""A queue started from a playlist follows later edits of that playlist; API answers are not cacheable.
Runs against a THROWAWAY server instance (own temp library, data dir and port; never the real one).
    python3 test_follow.py
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
PORT = 18096
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


async def state(http):
    async with http.get(f"{BASE}/api/state") as r:
        return await r.json()


async def put_items(http, pid, items):
    async with http.put(f"{BASE}/api/playlists/{pid}", json={"items": items}) as r:
        assert r.status == 200
    await asyncio.sleep(0.2)


async def play(player, pid, shuffle):
    await player.send_json({"type": "cmd", "cmd": "play_collection", "collection": "playlist", "playlist": pid,
                            "shuffle": shuffle})
    return (await next_msg(player, {"load"}))["item"]["id"]


async def main(tmp):
    lib = tmp / "lib"
    for n in range(1, 9):
        tone(lib / "album" / f"0{n} Song {n}.flac", 2, 300 + 50 * n)
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(tmp / "data"),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT))
    proc = subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)
    try:
        async with aiohttp.ClientSession() as http:
            items = []
            for _ in range(60):
                try:
                    async with http.get(f"{BASE}/api/library") as r:
                        items = (await r.json())["items"]
                        if len(items) == 8:
                            break
                except (aiohttp.ClientError, KeyError, ValueError):
                    pass
                await asyncio.sleep(0.2)
            ids = [it["id"] for it in sorted(items, key=lambda i: i["title"])]
            check(len(ids) == 8, "library of 8 songs")
            player = await http.ws_connect(f"{BASE}/ws?role=player")
            await next_msg(player, {"state"})

            async with http.get(f"{BASE}/api/playlists") as r:
                check(r.headers.get("Cache-Control") == "no-store", f"API answer not cacheable ({r.headers.get('Cache-Control')})")
            async with http.post(f"{BASE}/api/playlists", json={"name": "Fav", "ids": ids[:6] + ids[:2]}) as r:
                pid = (await r.json())["id"]

            # --- in order (no shuffle): current item keeps playing, the queue becomes the edited playlist
            cur = await play(player, pid, shuffle=False)
            st = await state(http)
            check(st["queue"] == ids[:6] + ids[:2] and cur == ids[0], "playlist started in order, with its duplicates")
            await player.send_json({"type": "cmd", "cmd": "jump", "index": 2})
            cur = (await next_msg(player, {"load"}))["item"]["id"]
            dedup = ids[:6]
            await put_items(http, pid, dedup)
            st = await state(http)
            check(st["queue"] == dedup and st["queue"][st["index"]] == cur, f"dedup: queue {len(st['queue'])} = playlist, current kept at {st['index']}")
            await put_items(http, pid, [ids[0], ids[1], ids[3], ids[4], ids[7]])  # current (ids[2]) removed, ids[7] added
            st = await state(http)
            check(st["queue"][st["index"]] == cur and st["queue"] == [ids[0], ids[1], cur, ids[3], ids[4], ids[7]],
                  f"current removed from the playlist keeps playing, rest follows: {st['queue']}")
            loads = 0
            try:
                await next_msg(player, {"load"}, timeout=1)
                loads = 1
            except asyncio.TimeoutError:
                pass
            check(loads == 0, "an edit does not restart playback")

            # --- shuffled: played items stay behind, the rest is reshuffled from the new contents
            await put_items(http, pid, ids[:6] + ids[:3])
            cur = await play(player, pid, shuffle=True)
            await player.send_json({"type": "cmd", "cmd": "next"})
            await next_msg(player, {"load"})
            await player.send_json({"type": "cmd", "cmd": "next"})
            cur = (await next_msg(player, {"load"}))["item"]["id"]
            st = await state(http)
            played = st["queue"][:st["index"]]
            new = ids[2:8]  # dedup + drop ids[0..1] + add ids[6..7]
            await put_items(http, pid, new)
            st = await state(http)
            q = st["queue"]
            check(q[st["index"]] == cur, "shuffle: current kept")
            check(q[:st["index"]] == [i for i in played if i in new], "shuffle: played items still in the playlist stay behind")
            want = sorted(new + ([cur] if cur not in new else []))
            check(sorted(q) == want, f"shuffle: queue holds every playlist item once (+ current if removed): {len(q)}")

            # --- rename only: nothing changes; a hand edit stops the following
            before = (await state(http))["queue"]
            async with http.put(f"{BASE}/api/playlists/{pid}", json={"name": "Renamed"}) as r:
                assert r.status == 200
            await asyncio.sleep(0.2)
            check((await state(http))["queue"] == before, "rename leaves the queue alone")
            await player.send_json({"type": "cmd", "cmd": "enqueue", "ids": [ids[0]], "mode": "end"})
            await asyncio.sleep(0.2)
            await put_items(http, pid, ids[:2])
            st = await state(http)
            check(st["queue"] == before + [ids[0]], "after a hand edit (enqueue) the queue no longer follows")

            # --- deleting the playlist leaves the queue as it is
            cur = await play(player, pid, shuffle=False)
            q = (await state(http))["queue"]
            async with http.delete(f"{BASE}/api/playlists/{pid}") as r:
                assert r.status == 200
            await asyncio.sleep(0.2)
            check((await state(http))["queue"] == q, "deleted playlist: queue kept")

            # --- the source survives a server restart
            async with http.post(f"{BASE}/api/playlists", json={"name": "Two", "ids": ids[:4]}) as r:
                pid2 = (await r.json())["id"]
            await play(player, pid2, shuffle=False)
            await player.close()
            proc.terminate()
            proc.wait(10)
            proc = subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                                    stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)
            for _ in range(60):
                try:
                    await state(http)
                    break
                except aiohttp.ClientError:
                    await asyncio.sleep(0.2)
            await put_items(http, pid2, ids[:3])
            check((await state(http))["queue"] == ids[:3], "follows after a restart (source saved with the state)")
    finally:
        proc.terminate()
        proc.wait(10)


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="atv-follow-"))
    try:
        asyncio.run(main(tmp))
    finally:
        if failed:
            print((tmp / "server.log").read_text()[-3000:])
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
