#!/usr/bin/env python3
"""Endless playback (repeat "all" default + one-time migration), against a THROWAWAY server instance
(own temp library, data dir and port; never the real one).   python3 test_repeat.py   (run on the media server host)
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
PORT = 18092
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


async def state(http):
    async with http.get(f"{BASE}/api/state") as r:
        return await r.json()


async def wait_up(http):
    for _ in range(60):
        try:
            async with http.get(f"{BASE}/api/library") as r:
                items = (await r.json())["items"]
                if len(items) == 2:
                    return items
        except (aiohttp.ClientError, KeyError, ValueError):
            pass
        await asyncio.sleep(0.2)
    return []


async def main(tmp):
    lib = tmp / "lib"
    tone(lib / "album" / "01 A.flac", 3, 440)
    tone(lib / "album" / "02 B.flac", 3, 550)
    data = tmp / "data"
    data.mkdir()
    # State file as written by server 0.6.0 and older: repeat off, no state_version.
    (data / "state.json").write_text(json.dumps({"queue": [], "original": [], "index": -1, "shuffle": False,
                                                 "repeat": "off", "position": 0}))
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(data),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT))
    proc = start(tmp, env)
    try:
        async with aiohttp.ClientSession() as http:
            items = await wait_up(http)
            a, b = (it["id"] for it in sorted(items, key=lambda i: i["title"]))
            check((await state(http))["repeat_mode"] == "all", "old state file (repeat off) migrated to repeat all")
            player = await http.ws_connect(f"{BASE}/ws?role=player")
            await next_msg(player, {"state"})
            async with http.post(f"{BASE}/api/playlists", json={"name": "Two", "ids": [a, b]}) as r:
                pid = (await r.json())["id"]
            await player.send_json({"type": "cmd", "cmd": "play_collection", "collection": "playlist", "playlist": pid})
            m = await next_msg(player, {"load"})
            check(m["item"]["id"] == a, "playlist starts with A")
            await player.send_json({"type": "ended", "item": a})
            m = await next_msg(player, {"load"})
            check(m["item"]["id"] == b, "then B")
            await player.send_json({"type": "ended", "item": b})
            m = await next_msg(player, {"load", "control"})
            check(m["type"] == "load" and m["item"]["id"] == a and m["autoplay"] and not m["present"],
                  "after the last item: back to A, playing, player not popped up")
            await player.send_json({"type": "ended", "item": a})
            await next_msg(player, {"load"})
            await player.send_json({"type": "ended", "item": b})
            m = await next_msg(player, {"load", "control"})
            check(m["type"] == "load" and m["item"]["id"] == a, "and again (second lap)")

            # Folder with shuffle: still endless.
            await player.send_json({"type": "cmd", "cmd": "play_collection", "collection": "folder",
                                    "root": "music", "folder": "album", "shuffle": True})
            first = (await next_msg(player, {"load"}))["item"]["id"]
            await player.send_json({"type": "ended", "item": first})
            second = (await next_msg(player, {"load"}))["item"]["id"]
            await player.send_json({"type": "ended", "item": second})
            m = await next_msg(player, {"load", "control"})
            check(m["type"] == "load" and {first, second} == {a, b}, "shuffled folder wraps around too")

            # The web remote toggle still works and survives a restart.
            await player.send_json({"type": "cmd", "cmd": "repeat", "mode": "off"})
            await asyncio.sleep(0.3)
            cur = (await state(http))["current"]["id"]
            other = b if cur == a else a
            st = await state(http)
            if st["index"] == 0:
                await player.send_json({"type": "ended", "item": cur})
                await next_msg(player, {"load"})
                cur = other
            await player.send_json({"type": "ended", "item": cur})
            m = await next_msg(player, {"load", "control"})
            check(m["type"] == "control" and m["action"] == "stop", "repeat off (toggle): stops at the end")
            await player.close()
            stop(proc)
            proc = start(tmp, env)
            await wait_up(http)
            check((await state(http))["repeat_mode"] == "off", "a deliberate 'off' is kept after a restart")
    finally:
        stop(proc)


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="atv-reptest-"))
    try:
        asyncio.run(main(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
