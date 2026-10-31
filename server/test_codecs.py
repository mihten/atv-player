#!/usr/bin/env python3
"""Video + audio codec per item ("codec" / "acodec"), incl. the one-time backfill of a DB scanned before "acodec"
existed. Throwaway temp library, DB and port only.   python3 test_codecs.py   (needs ffmpeg + the server's deps)
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
sys.path.insert(0, str(HERE))
import scanner  # noqa: E402

PORT = 18097
BASE = f"http://127.0.0.1:{PORT}"
failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def make_library(lib):
    lib.mkdir()
    src = ["-f", "lavfi", "-i", "testsrc=size=160x90:rate=25:duration=2"]
    tone = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
    # two audio tracks, the SECOND one is the default -> acodec = flac, not aac
    ff(*src, *tone, *tone, "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "libx264", "-c:a:0", "aac",
       "-c:a:1", "flac", "-disposition:a:0", "0", "-disposition:a:1", "default", str(lib / "A - Two Tracks.mkv"))
    ff(*src, *tone, "-c:v", "libx264", "-c:a", "aac", str(lib / "A - Plain.mkv"))
    ff(*src, "-c:v", "libx264", str(lib / "A - Silent.mkv"))
    ff(*tone, "-metadata", "title=Song", str(lib / "song.flac"))
    ff(*tone, "-metadata", "title=Tune", str(lib / "tune.mp3"))
    ff(*tone, "-c:a", "libopus", "-metadata", "title=Oggopus", str(lib / "oggopus.ogg"))


def rows(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    out = {r["title"]: dict(r) for r in con.execute("SELECT * FROM items")}
    con.close()
    return out


def scanner_checks(tmp):
    lib = tmp / "lib"
    make_library(lib)
    db = tmp / "old.db"
    scanner.scan(str(db), {"music": str(lib)})
    by = rows(db)
    check(by["Two Tracks"]["codec"] == "h264" and by["Two Tracks"]["acodec"] == "flac",
          f"video: codec h264, acodec of the DEFAULT audio track = flac ({by['Two Tracks']['acodec']})")
    check(by["Plain"]["acodec"] == "aac", f"video: acodec aac ({by['Plain']['acodec']})")
    check(by["Silent"]["acodec"] == "", f"video without audio: acodec '' ({by['Silent']['acodec']!r})")
    check(by["Song"]["acodec"] == by["Song"]["codec"] == "flac", f"song: acodec = codec ({by['Song']['acodec']})")
    check(by["Tune"]["acodec"] == by["Tune"]["codec"], f"mp3: acodec = codec ({by['Tune']['acodec']})")
    check(by["Oggopus"]["acodec"] == "opus", f"Opus in .ogg: acodec opus, not 'ogg' ({by['Oggopus']['acodec']})")

    # An "old" DB: no acodec column, loudness + lyrics already measured. Opening it migrates; a normal rescan
    # re-probes the videos once (keeping their loudness), and after that skips them again.
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE items DROP COLUMN acodec")
    con.execute("UPDATE items SET lufs = -14.0, tpeak = -1.0, lyr = 0")
    con.commit()
    con.close()
    stats = scanner.scan(str(db), {"music": str(lib)})
    by = rows(db)
    check(stats["updated"] == 3 and stats["added"] == 0, f"backfill: the 3 videos re-probed once ({stats})")
    check(by["Two Tracks"]["acodec"] == "flac" and by["Plain"]["acodec"] == "aac" and by["Silent"]["acodec"] == "",
          "backfill: video acodec filled")
    check(by["Song"]["acodec"] == "flac" and by["Tune"]["acodec"] == by["Tune"]["codec"],
          "backfill: song acodec = codec (from the migration, no re-read)")
    check(all(r["lufs"] == -14.0 and r["lyr"] == 0 for r in by.values()),
          "backfill: loudness + lyrics of the unchanged files kept")
    stats = scanner.scan(str(db), {"music": str(lib)})
    check(stats["updated"] == 0, f"next rescan: nothing re-probed ({stats})")
    return lib


async def server_checks(tmp, lib):
    data = tmp / "data"
    data.mkdir()
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(data),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT))
    proc = subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)
    try:
        async with aiohttp.ClientSession() as http:
            items = None
            end = time.time() + 30
            while time.time() < end:
                try:
                    async with http.get(f"{BASE}/api/library?all=1") as r:
                        body = await r.json()
                        if r.status == 200 and len(body["items"]) >= 6:
                            items = body["items"]
                            break
                except (aiohttp.ClientError, ValueError, KeyError):
                    pass
                await asyncio.sleep(0.5)
            check(items is not None, "throwaway server up with 6 items")
            if items is None:
                return
            by = {it["title"]: it for it in items}
            check(by["Two Tracks"]["codec"] == "h264" and by["Two Tracks"]["acodec"] == "flac",
                  "API: video codec + acodec sent")
            check(by["Silent"]["acodec"] is None, "API: video without audio -> acodec null")
            check(by["Song"]["acodec"] == "flac", "API: song acodec sent")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    log = (tmp / "server.log").read_text(errors="replace")
    check("Traceback" not in log, "no traceback in the server log")


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="atv-codecs-"))
    try:
        library = scanner_checks(tmp)
        asyncio.run(server_checks(tmp, library))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS" if failed == 0 else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
