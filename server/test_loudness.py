#!/usr/bin/env python3
"""Loudness measurement (loudness.py + scanner + API), against a THROWAWAY server instance (own temp
library, data dir and port; never the real one).   python3 test_loudness.py   (run on the media server host)
"""
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
PORT = 18093
BASE = f"http://127.0.0.1:{PORT}"
failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def tone(path, db, seconds=6, freq=1000):
    """Sine tone `db` dB relative to ffmpeg's sine source, which itself peaks at -18 dBFS."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ff("-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}", "-af", f"volume={db}dB",
       "-metadata", f"title={path.stem}", "-metadata", "artist=Test", str(path))


def start(tmp, env):
    return subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)


def stop(proc):
    proc.terminate()
    try:
        proc.wait(30)
    except subprocess.TimeoutExpired:
        proc.kill()


async def get(http, path):
    async with http.get(f"{BASE}{path}") as r:
        return await r.json()


async def wait_loudness(http, pending_target=0, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            c = await get(http, "/api/loudness")
            if c["pending"] == pending_target and not c["running"] and c["done"] + c["failed"] > 0:
                return c
        except (aiohttp.ClientError, KeyError, ValueError):
            pass
        await asyncio.sleep(0.5)
    return None


async def main(tmp):
    lib = tmp / "lib"
    tone(lib / "quiet.flac", -26)
    tone(lib / "loud.flac", -6)
    # video with a quiet first audio track and a loud second one: audio_ord must pick the right stream
    ff("-f", "lavfi", "-i", "color=c=black:s=64x64:d=6", "-f", "lavfi", "-i", "sine=frequency=500:duration=6",
       "-f", "lavfi", "-i", "sine=frequency=700:duration=6", "-map", "0", "-map", "1", "-map", "2",
       "-filter:a:0", "volume=-30dB", "-filter:a:1", "volume=-8dB", "-c:v", "libx264", "-c:a", "flac",
       "-disposition:a:0", "0", "-disposition:a:1", "default", str(lib / "clip.mkv"))
    # digital silence cannot be measured: marked as failed, never retried
    ff("-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "4", str(lib / "silence.flac"))
    data = tmp / "data"
    data.mkdir()
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(data),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT))
    proc = start(tmp, env)
    try:
        async with aiohttp.ClientSession() as http:
            counts = await wait_loudness(http)
            check(counts is not None, "background measurement finishes")
            if counts is None:
                return
            check(counts["done"] == 3 and counts["failed"] == 1, f"3 measured, silence marked failed: {counts}")
            items = {it["title"]: it for it in (await get(http, "/api/library?all=1"))["items"]}
            q, l, v = items["quiet"], items["loud"], items["clip"]
            check(q["lufs"] is not None and l["lufs"] is not None, "library items carry lufs / tpeak")
            check(abs((l["lufs"] - q["lufs"]) - 20.0) < 0.5, f"20 dB level difference measured: {q['lufs']} vs {l['lufs']}")
            check(abs(q["tpeak"] - (-26 - 18)) < 0.6 and abs(l["tpeak"] - (-6 - 18)) < 0.6,
                  f"true peaks -44 / -24 dBFS (volume -26 / -6 dB on the -18 dBFS source): {q['tpeak']} / {l['tpeak']}")
            check(abs(v["tpeak"] - (-8 - 18)) < 0.6, f"video: the default (second) audio stream is measured: {v['tpeak']}")
            check(items["silence"]["lufs"] is None, "silent file has no loudness value")

            # A changed file is measured again; unchanged files are not.
            db = data / "library.db"
            con = sqlite3.connect(db)
            before = con.execute("SELECT lufs FROM items WHERE id = ?", (q["id"],)).fetchone()[0]
            con.close()
            tone(lib / "quiet.flac", -16)  # same name, 10 dB louder
            os.utime(lib / "quiet.flac", (time.time() + 5, time.time() + 5))
            async with http.post(f"{BASE}/api/rescan?wait=1") as r:
                check(r.status == 200, "rescan after the file changed")
            counts = await wait_loudness(http)
            items = {it["title"]: it for it in (await get(http, "/api/library?all=1"))["items"]}
            check(counts is not None and abs(items["quiet"]["lufs"] - before - 10.0) < 0.5,
                  f"changed file re-measured: {before} -> {items['quiet']['lufs']}")
            check(abs(items["loud"]["lufs"] - l["lufs"]) < 0.01, "unchanged file kept its value")

            # Restart: values come from the DB, nothing is measured again.
            stop(proc)
            proc = start(tmp, env)
            for _ in range(60):
                try:
                    items = (await get(http, "/api/library?all=1"))["items"]
                    if len(items) == 4:
                        break
                except (aiohttp.ClientError, KeyError, ValueError):
                    pass
                await asyncio.sleep(0.2)
            c = await get(http, "/api/loudness")
            check(c["pending"] == 0 and c["done"] == 3, f"after a restart nothing is pending: {c}")
    finally:
        stop(proc)


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="atv-loud-"))
    try:
        asyncio.run(main(tmp))
    finally:
        log = tmp / "server.log"
        if failed and log.exists():
            print(log.read_text()[-3000:])
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
