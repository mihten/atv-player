#!/usr/bin/env python3
"""Badges (badges.py) and the scanner's sample rate / bit depth: unit table, one-time backfill of an old database,
and end to end against a THROWAWAY server (own temp library, data dir and port; never the real one).
   python3 test_badges.py   (needs aiohttp, mutagen, ffmpeg)
"""
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import badges  # noqa: E402
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


def unit():
    def song(**kw):
        return {"kind": "audio", "acodec": "flac", "codec": "flac", "lossless": 1, "bdepth": 16, "srate": 44100,
                "bitrate": 900000, "channels": 2, **kw}

    def video(**kw):
        return {"kind": "video", "codec": "hevc", "acodec": "opus", "lossless": 0, "width": 1920, "height": 1080,
                "fps": 25.0, "dyn": "sdr", "channels": 2, "srate": 48000, "bdepth": 0, "bitrate": None, **kw}

    table = [
        ("CD flac", song(), ["FLAC 16/44"]),
        ("hi-res flac", song(bdepth=24, srate=96000), ["FLAC 24/96"]),
        ("flac 24/192", song(bdepth=24, srate=192000), ["FLAC 24/192"]),
        ("flac 24/88.2", song(bdepth=24, srate=88200), ["FLAC 24/88"]),
        ("flac without depth", song(bdepth=0, srate=44100), ["FLAC"]),
        ("alac", song(acodec="alac", codec="alac", bdepth=24, srate=48000), ["ALAC 24/48"]),
        ("wavpack", song(acodec="wv", codec="wv"), ["WavPack 16/44"]),
        ("mp3 320", song(acodec="mp3", codec="mp3", lossless=0, bdepth=0, bitrate=320000), ["MP3 320"]),
        ("mp3 near 320 (CBR 319.9)", song(acodec="mp3", codec="mp3", lossless=0, bdepth=0, bitrate=319900), ["MP3 320"]),
        ("mp3 vbr 245 is within 6 % of 256", song(acodec="mp3", codec="mp3", lossless=0, bdepth=0, bitrate=245000),
         ["MP3 256"]),
        ("mp3 208 is no usual step: rounded to 10", song(acodec="mp3", codec="mp3", lossless=0, bdepth=0, bitrate=208000),
         ["MP3 210"]),
        ("mp3 unknown bitrate", song(acodec="mp3", codec="mp3", lossless=0, bdepth=0, bitrate=None), ["MP3"]),
        ("aac m4a", song(acodec="mp4a.40.2", codec="mp4a.40.2", lossless=0, bdepth=16, bitrate=256000), ["AAC 256"]),
        ("opus song", song(acodec="opus", codec="opus", lossless=0, bdepth=0, bitrate=None), ["Opus"]),
        ("5.1 flac song", song(bdepth=24, srate=48000, channels=6), ["FLAC 24/48", "5.1"]),
        ("typical video", video(), ["HEVC", "1080p", "Opus"]),
        ("4K 60fps HLG with flac 5.1",
         video(width=3840, height=2160, fps=59.94, dyn="hlg", acodec="flac", lossless=1, bdepth=24, srate=48000,
               channels=6),
         ["HEVC", "4K", "HLG", "60fps", "FLAC 24/48", "5.1"]),
        ("50 fps", video(fps=50.0), ["HEVC", "1080p", "50fps", "Opus"]),
        ("23.976 fps shows no fps", video(fps=23.976), ["HEVC", "1080p", "Opus"]),
        ("HDR10 and DV", video(dyn="pq"), ["HEVC", "1080p", "HDR10", "Opus"]),
        ("dolby vision", video(dyn="dv"), ["HEVC", "1080p", "DV", "Opus"]),
        ("cinemascope 1920x818 is 1080p", video(height=818), ["HEVC", "1080p", "Opus"]),
        ("1600 tall is 1440p", video(width=2560, height=1440), ["HEVC", "1440p", "Opus"]),
        ("720p h264 aac", video(codec="h264", acodec="aac", width=1280, height=720), ["H.264", "720p", "AAC"]),
        ("SD mpeg4 mp3", video(codec="mpeg4", acodec="mp3", width=640, height=478), ["MPEG-4", "SD", "MP3"]),
        ("video without sound", video(acodec="", channels=None), ["HEVC", "1080p"]),
        ("vp9 pcm", video(codec="vp9", acodec="pcm_s16le"), ["VP9", "1080p", "PCM"]),
    ]
    for label, row, want in table:
        got, _ = badges.item_badges(row)
        check(got == want, f"{label}: {got}")
    # the audio part alone (audio-only streaming away from home): no video codec, picture, HDR or fps
    full, audio = badges.item_badges(video(width=3840, height=2160, fps=60.0, dyn="hlg", acodec="flac", lossless=1,
                                           bdepth=24, srate=96000, channels=6))
    check(audio == ["FLAC 24/96", "5.1"], f"abadges of a 4K video: {audio}")
    check(badges.item_badges(song())[1] == badges.item_badges(song())[0], "a song's abadges = its badges")
    check(len(full) <= 6, "the longest list stays short")


def backfill(tmp):
    """A database made before srate / bdepth existed: the columns are added empty, the next scan fills them for
    files that did not change and leaves everything else (loudness, lyrics flag) alone."""
    lib = tmp / "old-lib"
    lib.mkdir()
    ff("-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-sample_fmt", "s32", "-ar", "96000",
       "-metadata", "title=Old", "-metadata", "artist=Test", str(lib / "Old.flac"))
    db = tmp / "old.db"
    old_schema = scanner.SCHEMA.replace("  srate INTEGER,\n  bdepth INTEGER,\n", "")
    con = sqlite3.connect(db)
    con.executescript(old_schema)
    con.commit()
    con.close()
    # scan with the new code once, then wipe the new columns + set a loudness value to simulate "scanned before"
    scanner.scan(db, {"m": str(lib)})
    con = scanner.connect(db)
    con.execute("UPDATE items SET srate = NULL, bdepth = NULL, lufs = -9.5")
    con.commit()
    con.close()
    stats = scanner.scan(db, {"m": str(lib)})
    con = scanner.connect(db)
    row = con.execute("SELECT srate, bdepth, lufs FROM items").fetchone()
    con.close()
    check(stats["updated"] == 1 and stats["added"] == 0, f"backfill re-reads the unchanged file once: {stats}")
    check(row["srate"] == 96000 and row["bdepth"] == 24, f"sample rate + bit depth stored: {dict(row)}")
    check(row["lufs"] == -9.5, "the measured loudness is kept by the backfill")
    stats = scanner.scan(db, {"m": str(lib)})
    check(stats["updated"] == 0, "and nothing is re-read a second time")
    # a real migration: a table without the columns gets them
    con = sqlite3.connect(tmp / "legacy.db")
    con.executescript(old_schema)
    con.commit()
    con.close()
    con = scanner.connect(tmp / "legacy.db")
    cols = {r["name"] for r in con.execute("PRAGMA table_info(items)")}
    con.close()
    check({"srate", "bdepth"} <= cols, "connect() adds the two columns to an old database")


async def e2e(tmp):
    lib = tmp / "lib"
    (lib / "songs").mkdir(parents=True)
    (lib / "videos").mkdir(parents=True)
    ff("-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-sample_fmt", "s32", "-ar", "96000",
       "-metadata", "title=Hires", "-metadata", "artist=T", str(lib / "songs" / "Hires.flac"))
    ff("-f", "lavfi", "-i", "sine=frequency=550:duration=2", "-ar", "44100", "-sample_fmt", "s16",
       "-metadata", "title=Cd", "-metadata", "artist=T", str(lib / "songs" / "Cd.flac"))
    ff("-f", "lavfi", "-i", "sine=frequency=660:duration=2", "-ar", "44100", "-b:a", "320k",
       "-metadata", "title=Lossy", "-metadata", "artist=T", str(lib / "songs" / "Lossy.mp3"))
    # a 4K 60 fps HLG video with 5.1 FLAC sound (tiny: 2 s, ultrafast)
    ff("-f", "lavfi", "-i", "testsrc=size=3840x2160:rate=60:duration=2",
       "-f", "lavfi", "-i", "sine=frequency=330:duration=2,pan=5.1|FL=c0|FR=c0|FC=c0|LFE=c0|BL=c0|BR=c0",
       "-vf", "format=yuv420p10le,setparams=color_trc=arib-std-b67:color_primaries=bt2020:colorspace=bt2020nc",
       "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=none",
       "-c:a", "flac", "-sample_fmt", "s32", "-ar", "48000", "-metadata:s:a", "title=main",
       str(lib / "videos" / "Test - Big.mkv"))
    ff("-f", "lavfi", "-i", "testsrc=size=1280x720:rate=25:duration=2", "-f", "lavfi", "-i", "sine=frequency=220:duration=2",
       "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "libopus", str(lib / "videos" / "Test - Small.mkv"))
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(tmp / "data"),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT))
    proc = subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)
    try:
        async with aiohttp.ClientSession() as http:
            items = []
            for _ in range(150):
                try:
                    async with http.get(f"{BASE}/api/library") as r:
                        items = (await r.json())["items"]
                    if len(items) == 5:
                        break
                except (aiohttp.ClientError, KeyError, ValueError):
                    pass
                await asyncio.sleep(0.2)
            by = {i["title"]: i for i in items}
            check(len(items) == 5, f"library has 5 items: {sorted(by)}")
            check(by["Hires"]["badges"] == ["FLAC 24/96"], f"hi-res flac: {by['Hires']['badges']}")
            check(by["Cd"]["badges"] == ["FLAC 16/44"], f"cd flac: {by['Cd']['badges']}")
            check(by["Lossy"]["badges"] == ["MP3 320"], f"mp3: {by['Lossy']['badges']}")
            big = by.get("Big") or {}
            check(big.get("badges") == ["HEVC", "4K", "HLG", "60fps", "FLAC 24/48", "5.1"],
                  f"4K HLG 60fps flac 5.1 video: {big.get('badges')}")
            check(big.get("abadges") == ["FLAC 24/48", "5.1"], f"its audio-only badges: {big.get('abadges')}")
            small = by.get("Small") or {}
            check(small.get("badges") == ["H.264", "720p", "Opus"], f"720p h264 opus video: {small.get('badges')}")
            async with http.get(f"{BASE}/api/item/{by['Hires']['id']}") as r:
                one = await r.json()
            check(one.get("badges") == ["FLAC 24/96"], "the full item reply carries the badges too")
    finally:
        proc.terminate()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()


def main():
    unit()
    with tempfile.TemporaryDirectory(prefix="atv-badges-") as t:
        backfill(Path(t))
    with tempfile.TemporaryDirectory(prefix="atv-badges-") as t:
        asyncio.run(e2e(Path(t)))
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
