#!/usr/bin/env python3
"""Offline downloads (task 22), server side: /media/<id>?audio=best serves the version of a song / video group with the
best audio (lossless > lossy, then bit depth, sample rate, bit rate; a tie keeps the requested item) as audio only:
a song's original file, a video's default track as the main copy (built and waited for, never the video file), 404
for a video without audio; X-Audio-Source / X-Audio-Format headers; the probe is cached in library.db;
/api/offline/<collection> lists a playlist / auto list with sources, sizes and a stamp that follows edits;
?audio=main and ?audio=1 are unchanged. Runs a THROWAWAY server instance (own temp library, data dir and port).
    python3 test_offline.py   (needs ffmpeg/ffprobe + the server's deps)
"""
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
PORT = 18113
BASE = f"http://127.0.0.1:{PORT}"
WS = BASE.replace("http", "ws")
failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def song(path, artist, title, codec="flac", bits=16, rate=44100, seconds=3):
    path.parent.mkdir(parents=True, exist_ok=True)
    enc = ["-c:a", "flac", "-sample_fmt", "s32" if bits == 24 else "s16"] if codec == "flac" else \
        ["-c:a", "libmp3lame", "-b:a", "192k"]
    ff("-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}:sample_rate={rate}", *enc,
       "-metadata", f"title={title}", "-metadata", f"artist={artist}", str(path))


def clip(path, seconds=3, audio=("flac16",), default=0, rate=44100):
    """H.264 picture + the given audio tracks (flac16 / flac24 / opus); `default` = the default track."""
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["-f", "lavfi", "-i", f"testsrc=size=64x64:rate=10:duration={seconds}"]
    for n, _ in enumerate(audio):
        args += ["-f", "lavfi", "-i", f"sine=frequency={440 + 110 * n}:duration={seconds}:sample_rate={rate}"]
    args += ["-map", "0:v"] + [x for n in range(len(audio)) for x in ("-map", f"{n + 1}:a")]
    args += ["-c:v", "libx264", "-preset", "ultrafast"]
    for n, kind in enumerate(audio):
        if kind == "opus":
            args += [f"-c:a:{n}", "libopus"]
        else:
            args += [f"-c:a:{n}", "flac", f"-sample_fmt:a:{n}", "s32" if kind == "flac24" else "s16"]
        args += [f"-disposition:a:{n}", "default" if n == default else "0"]
    ff(*args, str(path))


def streams(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, check=True).stdout
    return [tuple(line.strip().split(",")) for line in out.splitlines() if line.strip()]


async def get(http, path):
    async with http.get(BASE + path, allow_redirects=False) as r:
        return r.status, dict(r.headers), await r.read()


async def wait_up(http, count):
    for _ in range(150):
        try:
            async with http.get(f"{BASE}/api/library") as r:
                items = (await r.json())["items"]
                if len(items) == count:
                    return items
        except (aiohttp.ClientError, KeyError, ValueError):
            pass
        await asyncio.sleep(0.2)
    return []


async def main(tmp):
    lib = tmp / "lib"
    m, v = lib / "music", lib / "videos"
    song(m / "Alpha.flac", "Tester", "Alpha", bits=24, rate=48000)
    clip(v / "Tester - Alpha (Official Video).mkv", audio=("flac16",))
    song(m / "Beta.mp3", "Tester", "Beta", codec="mp3")
    clip(v / "Tester - Beta (Official Video).mkv", audio=("flac16",))
    song(m / "Gamma.flac", "Tester", "Gamma")
    clip(v / "Tester - Gamma (Official Video).mkv", audio=("flac16",))
    clip(v / "Clipper - Delta.mkv", audio=("flac24", "opus"), default=1)
    clip(v / "Clipper - Silent.mkv", audio=())
    total = 8
    cache, data = tmp / "cache", tmp / "data"
    audio = cache / "audio"
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(m), "videos": str(v)}), ATV_DATA=str(data),
               ATV_CACHE=str(cache), ATV_PORT=str(PORT))
    env.pop("ATV_BONJOUR", None)
    proc = subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)
    try:
        async with aiohttp.ClientSession() as http:
            items = await wait_up(http, total)
            check(len(items) == total, f"throwaway library has {total} items")
            by = {it["title"]: it for it in items}
            ids = {t: by[t]["id"] for t in by}
            a_song, a_video = ids["Alpha"], ids["Alpha (Official Video)"]
            b_song, b_video = ids["Beta"], ids["Beta (Official Video)"]
            g_song, g_video = ids["Gamma"], ids["Gamma (Official Video)"]
            delta, silent = ids["Delta"], ids["Silent"]
            check(by["Alpha"]["videos"] == 1, "Alpha song has its music video")

            # ---- song: the original bytes (24-bit song beats the 16-bit video, from either side)
            st, h, body = await get(http, f"/media/{a_song}?audio=best")
            check(st == 200 and body == (m / "Alpha.flac").read_bytes(), "song -> its original file, unchanged")
            check(h.get("X-Audio-Source") == str(a_song) and h.get("X-Audio-Format") == "flac 24/48000",
                  f"... X-Audio-Source / X-Audio-Format 'flac 24/48000' (got {h.get('X-Audio-Source')}, "
                  f"{h.get('X-Audio-Format')})")
            check(h.get("Content-Length") == str(len(body)), "... Content-Length")
            st, h, body = await get(http, f"/media/{a_video}?audio=best")
            check(st == 200 and h.get("X-Audio-Source") == str(a_song) and body == (m / "Alpha.flac").read_bytes(),
                  "24-bit song + 16-bit video, video asked -> the song")
            async with http.get(f"{BASE}/media/{a_song}?audio=best", headers={"Range": "bytes=10-19"}) as r:
                part = await r.read()
            check(r.status == 206 and part == (m / "Alpha.flac").read_bytes()[10:20], "... Range requests work")

            # ---- lossless video beats a lossy song; no copy yet -> built, waited for, one audio track, no picture
            copy = audio / f"{b_video}.main.mka"
            check(not copy.exists(), "Beta video has no main copy yet")
            st, h, body = await get(http, f"/media/{b_song}?audio=best")
            check(st == 200 and h.get("X-Audio-Source") == str(b_video), "lossy song + lossless video -> the video")
            check(copy.is_file() and body == copy.read_bytes(), "... its main copy, built and waited for")
            out = tmp / "b.mka"
            out.write_bytes(body)
            check(streams(out) == [("flac", "audio")], f"... one FLAC audio track, no video (got {streams(out)})")
            check(h.get("Content-Type") == "audio/x-matroska" and h.get("X-Audio-Format") == "flac 16/44100",
                  f"... audio/x-matroska, 'flac 16/44100' (got {h.get('X-Audio-Format')})")

            # ---- tie: each side keeps itself
            _, h1, _ = await get(http, f"/media/{g_song}?audio=best")
            _, h2, _ = await get(http, f"/media/{g_video}?audio=best")
            check(h1.get("X-Audio-Source") == str(g_song) and h2.get("X-Audio-Source") == str(g_video),
                  "equal audio: the requested item is kept (song -> song, video -> video)")

            # ---- a video's default track (the 2nd, Opus) only; with a copy present it is served as is
            st, h, body = await get(http, f"/media/{delta}?audio=best")
            out = tmp / "d.mka"
            out.write_bytes(body)
            check(st == 200 and streams(out) == [("opus", "audio")],
                  f"video with 2 tracks -> only its default (Opus) track (got {streams(out)})")
            copy = audio / f"{delta}.main.mka"
            mtime = copy.stat().st_mtime
            st, _, again = await get(http, f"/media/{delta}?audio=best")
            check(st == 200 and again == body and copy.stat().st_mtime == mtime, "... second request: the same copy")

            st, _, _ = await get(http, f"/media/{silent}?audio=best")
            check(st == 404, f"video without audio -> 404 (got {st})")

            # ---- probe cache in library.db
            con = sqlite3.connect(data / "library.db")
            rows = {r[0]: r[1:] for r in con.execute("SELECT item_id, bits, rate FROM audio_probe")}
            con.close()
            check(rows.get(a_song) == (24, 48000) and rows.get(a_video) == (16, 44100),
                  f"probe results stored in library.db audio_probe (got {rows})")

            # ---- ?audio=main / ?audio=1 / plain: unchanged
            st, h, body = await get(http, f"/media/{a_song}?audio=main")
            check(st == 200 and body == (m / "Alpha.flac").read_bytes() and h.get("X-Audio-Copy") == "original"
                  and "X-Audio-Source" not in h, "?audio=main on a song: original file, X-Audio-Copy, no best headers")
            st, h, _ = await get(http, f"/media/{delta}?audio=main")
            check(st == 302 and h.get("Location") == f"/media/{delta}?audio=main&v=copy",
                  "?audio=main on a video: still the redirect to a fixed variant")
            st, h, body = await get(http, f"/media/{delta}?audio=main&v=copy")
            check(st == 200 and body == (audio / f"{delta}.main.mka").read_bytes() and "X-Audio-Source" not in h,
                  "?audio=main&v=copy: the main copy bytes")
            st, h, body = await get(http, f"/media/{delta}?audio=1")
            out = tmp / "d1.mka"
            out.write_bytes(body)
            check(st == 200 and [s[1] for s in streams(out)] == ["audio", "audio"]
                  and body == (audio / f"{delta}.mka").read_bytes() and "X-Audio-Source" not in h,
                  "?audio=1: every audio track, the audio-only copy as before")
            st, _, body = await get(http, f"/media/{delta}")
            check(body == (v / "Clipper - Delta.mkv").read_bytes(), "plain /media: the video file")

            # ---- /api/offline
            async with http.post(f"{BASE}/api/playlists",
                                 json={"name": "Car", "ids": [a_video, b_song, g_song, silent, a_song]}) as r:
                pl = (await r.json())["id"]
            async with http.get(f"{BASE}/api/offline/playlist?playlist={pl}") as r:
                off = await r.json()
            got = [(e["id"], e["source"]) for e in off["items"]]
            check(off["name"] == "Car" and got == [(a_video, a_song), (b_song, b_video), (g_song, g_song),
                                                   (a_song, a_song)],
                  f"/api/offline/playlist: entries -> sources in order, the silent video left out (got {got})")
            first = off["items"][0]
            check(first["size"] == (m / "Alpha.flac").stat().st_size and first["size_exact"]
                  and first["item"]["id"] == a_song and first["item"]["kind"] == "audio" and first["rev"],
                  "... size, item and rev of the served version")
            check(off["size"] == sum(e["size"] for e in off["items"]), "... total size")
            async with http.put(f"{BASE}/api/playlists/{pl}", json={"items": [g_song]}) as r:
                check(r.status == 200, "playlist edited")
            async with http.get(f"{BASE}/api/offline/playlist?playlist={pl}") as r:
                off2 = await r.json()
            check(off2["stamp"] != off["stamp"] and [e["id"] for e in off2["items"]] == [g_song],
                  "... stamp changes with the list")
            async with http.get(f"{BASE}/api/offline/playlist?playlist={pl}") as r:
                check((await r.json())["stamp"] == off2["stamp"], "... and stays the same without a change")
            async with http.get(f"{BASE}/api/offline/playlist?playlist=999") as r:
                check(r.status == 404, "unknown playlist -> 404")
            ws = await http.ws_connect(f"{WS}/ws?role=player&kind=phone")
            await ws.send_json({"type": "cmd", "cmd": "love", "id": b_song, "on": True})
            await asyncio.sleep(0.4)
            await ws.close()
            async with http.get(f"{BASE}/api/offline/loved") as r:
                loved = await r.json()
            check(loved["name"] == "Loved" and [(e["id"], e["source"]) for e in loved["items"]] == [(b_song, b_video)],
                  "/api/offline/loved: the loved song -> its better video")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="atv-offline-test-"))
    try:
        asyncio.run(main(tmp))
    finally:
        if failed:
            print((tmp / "server.log").read_text()[-3000:])
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
