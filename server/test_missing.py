#!/usr/bin/env python3
"""Test of missing/renamed files and the load "present" flag, against a THROWAWAY server instance
(own temp library, data dir and port; never the real one, never the Apple TV).

    python3 test_missing.py            # needs ffmpeg + the server's Python deps (run on the media server host)
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
PORT = 18089
BASE = f"http://127.0.0.1:{PORT}"
failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def tone(path, seconds, freq):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
                    "-metadata", f"title={path.stem}", "-metadata", "artist=Test", str(path)], check=True)


async def next_msg(ws, types, timeout=8):
    while True:
        msg = await asyncio.wait_for(ws.receive_json(), timeout)
        if msg.get("type") in types:
            return msg


async def main(tmp):
    lib = tmp / "lib" / "album"
    lib.mkdir(parents=True)
    for n, (sec, freq) in enumerate([(3, 440), (4, 550), (5, 660)], 1):
        tone(lib / f"0{n} Song {n}.flac", sec, freq)
    (tmp / "lib" / "other").mkdir()
    tone(tmp / "lib" / "other" / "Keep.flac", 2, 330)  # root never empty (an empty root = share not mounted)
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(tmp / "lib")}), ATV_DATA=str(tmp / "data"),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT))
    proc = subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "w"), stderr=subprocess.STDOUT)
    try:
        async with aiohttp.ClientSession() as http:
            items = []
            for _ in range(50):
                try:
                    async with http.get(f"{BASE}/api/browse", params={"root": "music", "folder": "album"}) as r:
                        items = (await r.json())["items"]
                        if len(items) == 3:
                            break
                except (aiohttp.ClientError, KeyError, ValueError):
                    pass
                await asyncio.sleep(0.2)
            ids = [it["id"] for it in items]
            check(len(ids) == 3, f"library scanned: {ids}")
            player = await http.ws_connect(f"{BASE}/ws?role=player")
            await next_msg(player, {"state"})

            async def cmd(**kw):
                await player.send_json({"type": "cmd", **kw})

            await cmd(cmd="play_collection", collection="folder", root="music", folder="album")
            m = await next_msg(player, {"load"})
            check(m["item"]["id"] == ids[0] and m["present"] is True, "user start: load song 1 with present=true")

            await player.send_json({"type": "ended", "item": ids[0]})
            m = await next_msg(player, {"load"})
            check(m["item"]["id"] == ids[1] and m["present"] is False, "auto-advance: song 2 with present=false")

            # Rename song 3 on disk (as happened on the NAS); the server has not rescanned.
            os.rename(lib / "03 Song 3.flac", lib / "03 Song 3 (renamed).flac")
            await player.send_json({"type": "ended", "item": ids[1]})
            m = await next_msg(player, {"load"})
            check(m["item"]["id"] == ids[2], f"renamed file keeps its id {ids[2]} (got {m['item']['id']})")
            async with http.get(f"{BASE}/media/{ids[2]}", headers={"Range": "bytes=0-3"}) as r:
                check(r.status == 206 and await r.read() == b"fLaC", "renamed file is served")

            await cmd(cmd="jump", index=0)
            m = await next_msg(player, {"load"})
            check(m["item"]["id"] == ids[0] and m["present"] is True, "jump from the queue: present=true")

            # Song 2 deleted: jumping to it must skip to song 3, not hand VLC a 404.
            (lib / "02 Song 2.flac").unlink()
            await cmd(cmd="jump", index=1)
            m = await next_msg(player, {"load"})
            check(m["item"]["id"] == ids[2], f"deleted file skipped, next is song 3 (got {m['item']['id']})")

            # Last item deleted: the queue ends instead of stepping back to an earlier song.
            await cmd(cmd="jump", index=0)
            await next_msg(player, {"load"})
            (lib / "03 Song 3 (renamed).flac").unlink()
            await player.send_json({"type": "ended", "item": ids[0]})
            m = await next_msg(player, {"load", "control"})
            check(m["type"] == "control" and m["action"] == "stop", f"deleted last item: stop ({m.get('type')} {m.get('action')})")

            # Direct /media request for a vanished file: 404 + background rescan drops it.
            (lib / "01 Song 1.flac").unlink()
            async with http.get(f"{BASE}/media/{ids[0]}") as r:
                check(r.status == 404, "missing file: 404")
            await asyncio.sleep(1.5)
            async with http.get(f"{BASE}/api/browse", params={"root": "music", "folder": "album"}) as r:
                left = (await r.json()).get("items", [])
            check(left == [], f"rescan after the 404 removed it (left: {[i['id'] for i in left]})")
            await player.close()
    finally:
        proc.terminate()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()
    print((tmp / "server.log").read_text()[-1500:])


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="atv-test-"))
    try:
        asyncio.run(main(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
