#!/usr/bin/env python3
"""Thumbnails of videos: the picture stored INSIDE the file wins over a frame grab, in every place a file can carry one
(Matroska image attachment as ffmpeg exposes it, Matroska attachment by name, MP4 cover atom, a still-image track),
a broken picture falls back to the next one / a frame grab, and thumbnails never run more than one at a time nor more
heavy ffmpeg jobs than ATV_FFMPEG_JOBS. No server process: imports server with temp dirs, builds tiny test videos.
    python3 test_art.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from io import BytesIO
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="atv-art-"))
LIBDIR = TMP / "lib"
LIBDIR.mkdir()
os.environ.update(ATV_ROOTS=json.dumps({"v": str(LIBDIR)}), ATV_DATA=str(TMP / "data"), ATV_CACHE=str(TMP / "cache"))
sys.path.insert(0, str(HERE))
import scanner  # noqa: E402
import server  # noqa: E402
from PIL import Image  # noqa: E402

failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, capture_output=True)


def solid(name, rgb, size="320x240"):
    p = TMP / name
    ff("-f", "lavfi", "-i", f"color=c=0x{rgb}:s={size}", "-frames:v", "1", str(p))
    return p


def base_video(name, ext="mkv"):
    """2 s blue video + tone: a frame grab is BLUE."""
    p = TMP / f"{name}.base.{ext}"
    ff("-f", "lavfi", "-i", "color=c=blue:s=320x240:d=2", "-f", "lavfi", "-i", "sine=d=2",
       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(p))
    return p


RED = solid("red.jpg", "ff0000")
GREEN = solid("green.jpg", "00ff00")
YELLOW = solid("yellow.png", "ffff00")
BAD = TMP / "bad.jpg"
BAD.write_bytes(b"\xff\xd8 this is not a jpeg")
FONT = TMP / "font.ttf"
FONT.write_bytes(b"not an image")


def mkv_attach(name, *files):
    out = LIBDIR / f"{name}.mkv"
    args = ["mkvmerge", "-q", "-o", str(out)]
    for path, aname, mime in files:
        args += ["--attachment-name", aname, "--attachment-mime-type", mime, "--attach-file", str(path)]
    subprocess.run(args + [str(base_video(name))], check=True, capture_output=True)
    return out


cases = {}
cases["cover"] = (mkv_attach("cover", (RED, "cover.jpg", "image/jpeg")), "red")
# name order: cover beats small_cover even when small_cover comes first; a font attachment is ignored
cases["names"] = (mkv_attach("names", (GREEN, "small_cover.jpg", "image/jpeg"), (FONT, "font.ttf", "font/ttf"),
                             (RED, "cover.jpg", "image/jpeg")), "red")
cases["png"] = (mkv_attach("png", (YELLOW, "cover.png", "image/png")), "yellow")
# broken cover.jpg -> the next picture (small_cover, green)
cases["broken"] = (mkv_attach("broken", (BAD, "cover.jpg", "image/jpeg"), (GREEN, "small_cover.jpg", "image/jpeg")),
                   "green")
# broken picture and nothing else -> frame grab (blue)
cases["broken_only"] = (mkv_attach("broken_only", (BAD, "cover.jpg", "image/jpeg")), "blue")
# an image type ffmpeg does not turn into a picture stream stays a plain attachment -> dumped as a file
cases["rawattach"] = (mkv_attach("rawattach", (GREEN, "cover.jpg", "image/x-custom")), "green")
# MP4 cover atom (attached picture)
mp4 = LIBDIR / "mp4cover.mp4"
ff("-i", str(base_video("mp4cover", "mp4")), "-i", str(RED), "-map", "0", "-map", "1", "-c", "copy",
   "-disposition:v:1", "attached_pic", str(mp4))
cases["mp4"] = (mp4, "red")
# still-image second video track (Apple Music style V_MJPEG cover track)
still = LIBDIR / "still.mkv"
ff("-i", str(base_video("still")), "-i", str(GREEN), "-map", "0", "-map", "1", "-c", "copy", str(still))
cases["still"] = (still, "green")
# nothing inside -> frame grab
plain = LIBDIR / "plain.mkv"
plain.write_bytes(base_video("plain").read_bytes())
cases["plain"] = (plain, "blue")


def colour(data):
    r, g, b = Image.open(BytesIO(data)).convert("RGB").resize((1, 1)).getpixel((0, 0))
    if r > 200 and g > 200 and b < 80:
        return "yellow"
    if r > 200 and g < 80 and b < 80:
        return "red"
    if g > 200 and r < 80 and b < 80:
        return "green"
    if b > 150 and r < 80 and g < 80:
        return "blue"
    return f"rgb({r},{g},{b})"


# 1. make_art on each case
server.CACHE.joinpath("art").mkdir(parents=True, exist_ok=True)
for n, (name, (p, want)) in enumerate(cases.items(), 1):
    it = {"id": n, "kind": "video", "root": "v", "relpath": p.name, "duration": 2.0, "mtime": p.stat().st_mtime}
    out = server.art_path(n)
    ok = server.make_art(it, out)
    got = colour(out.read_bytes()) if ok and out.exists() else "none"
    check(got == want, f"{name}: thumbnail is {want} (got {got})")
    check(out.name == f"{n}.v2.jpg", f"{name}: cache file name is versioned ({out.name})")

# 2. picture order from video_pictures
kinds = [k for k, _, _ in scanner.video_pictures(str(cases["names"][0]))]
names = [nm for _, _, nm in scanner.video_pictures(str(cases["names"][0]))]
check(names[:2] == ["cover.jpg", "small_cover.jpg"], f"names: cover.jpg ranked before small_cover.jpg ({names})")
check("font.ttf" not in names, "names: non-image attachment ignored")
check(scanner.video_pictures(str(cases["plain"][0])) == [], "plain video: no pictures found")
check([k for k, _, _ in scanner.video_pictures(str(cases["still"][0]))] == ["still"], "still track found as 'still'")

# 3. old unversioned cache files are removed at startup, versioned ones kept
art_dir = server.CACHE / "art"
(art_dir / "999.jpg").write_bytes(b"x")
(art_dir / "998.none").write_bytes(b"")
(art_dir / "997.v2.none").write_bytes(b"")
server.drop_old_art()
check(not (art_dir / "999.jpg").exists() and not (art_dir / "998.none").exists(), "old <id>.jpg / <id>.none removed")
check((art_dir / "997.v2.none").exists() and server.art_path(1).exists(), "versioned files kept")

# 4. concurrency: 8 thumbnails + 3 audio copies at once -> thumbnails 1 at a time, heavy jobs <= ATV_FFMPEG_JOBS (2)
running = {"art": 0, "copy": 0, "all": 0, "max_art": 0, "max_all": 0}
lock = threading.Lock()


def tracked(kind):
    def job(*_a, **_k):
        with lock:
            running[kind] += 1
            running["all"] += 1
            running["max_art"] = max(running["max_art"], running["art"])
            running["max_all"] = max(running["max_all"], running["all"])
        time.sleep(0.15)
        with lock:
            running[kind] -= 1
            running["all"] -= 1
        return True
    return job


class Req:
    def __init__(self, i):
        self.match_info = {"id": str(i)}


async def concurrency():
    server.make_art = tracked("art")
    server.make_audio_only = tracked("copy")
    server.LIB.clear()
    for i in range(100, 108):
        server.LIB[i] = {"id": i, "kind": "video", "root": "v", "relpath": "plain.mkv", "mtime": time.time() + 99}
    for i in range(200, 203):
        server.LIB[i] = {"id": i, "kind": "video", "root": "v", "relpath": "plain.mkv", "mtime": time.time() + 99}

    async def one_art(i):
        try:
            await server.art(Req(i))
        except Exception:  # noqa: BLE001 - the fake make_art writes no file: 404 is expected here
            pass

    async def one_copy(i):
        it = server.LIB[i]
        out = server.AUDIO_DIR / f"{i}.mka"
        async with server.AUDIO_LOCKS.setdefault(i, asyncio.Lock()):
            async with server.FFMPEG_JOBS:
                await asyncio.get_running_loop().run_in_executor(None, server.make_audio_only, it, out)

    started = time.monotonic()
    await asyncio.gather(*[one_art(i) for i in range(100, 108)], *[one_copy(i) for i in range(200, 203)])
    return time.monotonic() - started


elapsed = asyncio.run(concurrency())
check(running["max_art"] == 1, f"thumbnails never run in parallel (max {running['max_art']})")
check(running["max_all"] <= 2, f"heavy jobs never exceed ATV_FFMPEG_JOBS=2 (max {running['max_all']})")
check(running["max_all"] == 2, "an audio copy runs NEXT TO a thumbnail (2 slots used)")
check(elapsed < 8 * 0.15 + 1.0, f"all 11 jobs finished ({elapsed:.1f} s)")

print("ALL PASS" if not failed else f"{failed} FAILED")
sys.exit(1 if failed else 0)
