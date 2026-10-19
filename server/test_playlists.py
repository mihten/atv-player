#!/usr/bin/env python3
"""Playlist API + scanner id safety, against a THROWAWAY server instance (own temp library, data
dir and port; never the real one, never the Apple TV).   python3 test_playlists.py   (run on the media server host)
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
PORT = 18090
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


def start_server(tmp, env):
    return subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)


async def wait_up(http, want):
    for _ in range(60):
        try:
            async with http.get(f"{BASE}/api/library") as r:
                items = (await r.json())["items"]
                if len(items) == want:
                    return items
        except (aiohttp.ClientError, KeyError, ValueError):
            pass
        await asyncio.sleep(0.2)
    return []


async def main(tmp):
    lib = tmp / "lib"
    for n, (sec, freq) in enumerate([(3, 440), (4, 550), (5, 660)], 1):
        tone(lib / "album" / f"0{n} Song {n}.flac", sec, freq)
    tone(lib / "other" / "Solo.flac", 6, 770)
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(tmp / "data"),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT))
    proc = start_server(tmp, env)
    try:
        async with aiohttp.ClientSession() as http:
            items = await wait_up(http, 4)
            by_title = {it["title"]: it["id"] for it in items}
            s1, s2, s3, solo = (by_title[t] for t in ("01 Song 1", "02 Song 2", "03 Song 3", "Solo"))
            check(len(items) == 4, f"library: {by_title}")
            remote = await http.ws_connect(f"{BASE}/ws?role=remote")
            player = await http.ws_connect(f"{BASE}/ws?role=player")
            await next_msg(player, {"state"})

            async with http.post(f"{BASE}/api/playlists", json={"name": "  Road   trip "}) as r:
                pl = await r.json()
                check(r.status == 201 and pl["name"] == "Road trip" and pl["items"] == [], "create empty, name cleaned")
            m = await next_msg(remote, {"playlists"})
            check(m["id"] == pl["id"], "remote notified of the change")
            pid = pl["id"]
            async with http.post(f"{BASE}/api/playlists/{pid}/items",
                                 json={"folder": {"root": "music", "folder": "album"}}) as r:
                check((await r.json())["added"] == 3, "add folder: 3 songs in album order")
            async with http.post(f"{BASE}/api/playlists/{pid}/items", json={"ids": [solo, 999999, s1]}) as r:
                check((await r.json())["added"] == 2, "add ids: unknown id dropped, duplicate allowed")
            async with http.post(f"{BASE}/api/playlists/{pid}/items", json={"ids": [s1], "skip_existing": True}) as r:
                check((await r.json())["added"] == 0, "skip_existing")
            async with http.get(f"{BASE}/api/playlists/{pid}") as r:
                pl = await r.json()
            check([it["id"] for it in pl["items"]] == [s1, s2, s3, solo, s1], f"order {[it['id'] for it in pl['items']]}")
            check(abs(pl["duration"] - 21) < 0.5 and pl["count"] == 5, f"summary count 5, duration {pl['duration']:.1f}")

            async with http.put(f"{BASE}/api/playlists/{pid}", json={"name": "Trip", "items": [solo, s3, s1]}) as r:
                pl = await r.json()
            check(pl["name"] == "Trip" and [it["id"] for it in pl["items"]] == [solo, s3, s1], "rename + reorder/remove")
            async with http.put(f"{BASE}/api/playlists/{pid}", json={"name": "   "}) as r:
                check(r.status == 400, "empty name rejected")

            await player.send_json({"type": "cmd", "cmd": "play_collection", "collection": "playlist",
                                    "playlist": pid, "start_index": 1})
            m = await next_msg(player, {"load"})
            check(m["item"]["id"] == s3 and m["present"] is True, "play playlist from index 1 -> song 3")
            async with http.get(f"{BASE}/api/state") as r:
                check((await r.json())["queue"] == [solo, s3, s1], "queue = playlist order")

            async with http.post(f"{BASE}/api/playlists", json={"name": "From queue", "queue": True}) as r:
                q = await r.json()
                check([it["id"] for it in q["items"]] == [solo, s3, s1], "save queue as playlist")

            # Delete Solo (the HIGHEST id, which SQLite would hand out again) and add a new file.
            check(solo == max(s1, s2, s3, solo), "Solo has the highest id")
            (lib / "other" / "Solo.flac").unlink()
            await http.post(f"{BASE}/api/rescan")
            await asyncio.sleep(1.5)
            tone(lib / "album" / "04 New song.flac", 7, 880)
            await http.post(f"{BASE}/api/rescan")
            items = await wait_up(http, 4)
            new_id = next((it["id"] for it in items if it["title"] == "04 New song"), None)
            check(new_id is not None and new_id not in (s1, s2, s3, solo), f"new file got a fresh id {new_id}, not reused {solo}")
            async with http.get(f"{BASE}/api/playlists/{pid}") as r:
                pl = await r.json()
            check(pl["items"][0].get("missing") is True and pl["missing"] == 1, "deleted song shows as missing, keeps its slot")
            await player.send_json({"type": "cmd", "cmd": "play_collection", "collection": "playlist",
                                    "playlist": pid, "start_index": 2})
            m = await next_msg(player, {"load"})
            check(m["item"]["id"] == s1, "start_index past a missing entry maps to the right song")
            async with http.get(f"{BASE}/api/state") as r:
                check((await r.json())["queue"] == [s3, s1], "missing entry skipped in the queue")

            # Unmounted share: the whole root empty -> keep its items.
            await player.close()
            await remote.close()
            proc.terminate(); proc.wait(30)
            os.rename(lib, tmp / "lib-away")
            lib.mkdir()
            proc = start_server(tmp, env)
            items = await wait_up(http, 4)
            await asyncio.sleep(1.0)
            async with http.get(f"{BASE}/api/library") as r:
                n = len((await r.json())["items"])
            check(n == 4, f"empty root (share not mounted) keeps its {n} items")
            async with http.get(f"{BASE}/api/playlists") as r:
                names = sorted(p["name"] for p in (await r.json())["playlists"])
            check(names == ["From queue", "Trip"], f"playlists survive a restart: {names}")
            remote = await http.ws_connect(f"{BASE}/ws?role=remote")
            async with http.delete(f"{BASE}/api/playlists/{q['id']}") as r:
                check(r.status == 200, "delete playlist")
            async with http.get(f"{BASE}/api/playlists/{q['id']}") as r:
                check(r.status == 404, "deleted playlist is gone")
            await remote.close()
    finally:
        proc.terminate()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()
    log = (tmp / "server.log").read_text()
    print("\n".join(line for line in log.splitlines() if "WARNING" in line or "id " in line or "playlist" in line)[-2000:])


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="atv-pltest-"))
    try:
        asyncio.run(main(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
