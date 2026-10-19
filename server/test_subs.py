#!/usr/bin/env python3
"""Subtitle track list (scanner "subs" column + one-time migration of an old DB). Throwaway temp
library/DB only.   python3 test_subs.py   (run on the media server host; needs old_scanner.py = previous scanner)
"""
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def srt(path, text):
    path.write_text(f"1\n00:00:00,500 --> 00:00:03,000\n{text}\n")


def main(tmp):
    lib = tmp / "videos"
    lib.mkdir()
    for name, text in (("en", "Hello"), ("hr", "Bok"), ("forced", "SIGN")):
        srt(tmp / f"{name}.srt", text)
    mkv = lib / "Test Artist - Three Subs.mkv"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25", "-f", "lavfi", "-i", "sine=frequency=440",
        "-i", str(tmp / "en.srt"), "-i", str(tmp / "hr.srt"), "-i", str(tmp / "forced.srt"),
        "-t", "4", "-map", "0:v", "-map", "1:a", "-map", "2", "-map", "3", "-map", "4",
        "-c:v", "libx264", "-c:a", "flac", "-c:s", "srt",
        "-metadata:s:s:0", "language=eng", "-metadata:s:s:0", "title=Lyrics (aligned)",
        "-metadata:s:s:1", "language=hrv", "-metadata:s:s:1", "title=Croatian",
        "-metadata:s:s:2", "language=eng", "-metadata:s:s:2", "title=Signs",
        "-disposition:s:0", "default", "-disposition:s:1", "0", "-disposition:s:2", "forced",
        str(mkv)], check=True)
    roots = {"videos": str(lib)}
    db = tmp / "library.db"

    import old_scanner
    old_scanner.scan(db, roots)
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(items)")}
    check("subs" not in cols, "old scanner made a DB without the subs column")

    import scanner
    stats = scanner.scan(db, roots)
    check(stats["updated"] == 1, f"new scanner re-probed the unchanged video once (updated={stats['updated']})")
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT sub_ord, subs FROM items").fetchone()
    subs = json.loads(row["subs"])
    print("   subs =", subs)
    check([s["lang"] for s in subs] == ["eng", "hrv", "eng"], "three tracks in file order with languages")
    check(subs[0]["is_default"] and not subs[0]["forced"], "track 0 = default English")
    check(not subs[1]["is_default"] and subs[1]["title"] == "Croatian", "track 1 = Croatian, not default")
    check(subs[2]["forced"], "track 2 = forced")
    check(row["sub_ord"] == 0, "sub_ord (old field) still the default track")
    stats2 = scanner.scan(db, roots)
    check(stats2["updated"] == 0, "third scan: nothing re-probed (migration happens once)")


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="atv-subtest-"))
    try:
        main(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
