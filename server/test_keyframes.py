#!/usr/bin/env python3
"""Video keyframe index (GET /api/keyframes/<id>), against a THROWAWAY server instance (own temp library, data dir
and port; never the real one).   python3 test_keyframes.py   (run on the media server host)
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
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


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


async def get(http, path):
    async with http.get(BASE + path) as r:
        return r.status, (await r.json() if r.content_type == "application/json" else None)


async def wait_up(http, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            status, body = await get(http, "/api/library?all=1")
            if status == 200 and len(body["items"]) >= 2:
                return body["items"]
        except aiohttp.ClientError:
            pass
        await asyncio.sleep(0.5)
    return None


async def main(tmp):
    lib = tmp / "lib"
    lib.mkdir()
    # 6 s of video at 25 fps with a keyframe every 50 frames (2 s) -> keyframes at 0, 2, 4 s
    ff("-f", "lavfi", "-i", "testsrc=size=160x90:rate=25:duration=6", "-f", "lavfi", "-i", "sine=frequency=500:duration=6",
       "-c:v", "libx264", "-g", "50", "-keyint_min", "50", "-sc_threshold", "0", "-c:a", "aac", str(lib / "gop2.mkv"))
    ff("-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-metadata", "title=song", str(lib / "song.flac"))
    data = tmp / "data"
    data.mkdir()
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(data),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT))
    proc = subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)
    try:
        async with aiohttp.ClientSession() as http:
            items = await wait_up(http)
            check(items is not None, "throwaway server up with 2 items")
            if items is None:
                return
            by = {it["title"]: it for it in items}
            vid, song = by["gop2"], by["song"]

            t0 = time.time()
            status, body = await get(http, f"/api/keyframes/{vid['id']}")
            first = time.time() - t0
            check(status == 200 and body["id"] == vid["id"], f"video: 200 ({status})")
            times = body["times"] if body else []
            check(len(times) == 3 and all(abs(a - b) < 0.05 for a, b in zip(times, [0.0, 2.0, 4.0])),
                  f"video: keyframes at 0/2/4 s: {times}")
            check(times == sorted(times), "times sorted")

            t0 = time.time()
            status2, body2 = await get(http, f"/api/keyframes/{vid['id']}")
            second = time.time() - t0
            check(status2 == 200 and body2["times"] == times, "second request: same answer")
            check(second < max(first, 0.05), f"second request served from the cache ({second * 1000:.0f} ms vs {first * 1000:.0f} ms)")

            status, body = await get(http, f"/api/keyframes/{song['id']}")
            check(status == 200 and body["times"] == [], f"song: 200 with no keyframes ({status}, {body})")
            status, _ = await get(http, "/api/keyframes/99999")
            check(status == 404, "unknown id: 404")

            # file changed (new mtime) -> index recomputed: re-encode with a 1 s GOP and rescan
            ff("-f", "lavfi", "-i", "testsrc=size=160x90:rate=25:duration=6", "-f", "lavfi", "-i", "sine=frequency=500:duration=6",
               "-c:v", "libx264", "-g", "25", "-keyint_min", "25", "-sc_threshold", "0", "-c:a", "aac", str(lib / "gop2.mkv"))
            async with http.post(f"{BASE}/api/rescan?wait=1") as r:
                check(r.status == 200, "rescan after the file changed")
            status, body = await get(http, f"/api/keyframes/{vid['id']}")
            check(status == 200 and len(body["times"]) == 6, f"changed file: index recomputed (6 keyframes): {body['times'] if body else None}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    log = (tmp / "server.log").read_text(errors="replace")
    check("Traceback" not in log, "no traceback in the server log")


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="atv-keyframes-"))
    try:
        asyncio.run(main(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS" if failed == 0 else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
