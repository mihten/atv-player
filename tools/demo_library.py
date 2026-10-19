#!/usr/bin/env python3
"""Generate a small FICTIONAL demo library for design screenshots (CI simulator runs).

    python3 tools/demo_library.py <out-dir>

Writes <out-dir>/music/<Artist>/<Album>/NN Title.flac (embedded cover art, tags) and
<out-dir>/videos/<Artist> - <Title>.mkv (H.264 + FLAC/AAC audio + a default SRT lyrics track).
Every artist, album and song name is made up. Needs ffmpeg and Pillow.
"""
import math
import random
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ALBUMS = [
    # artist, album, year, cover style, colours, tracks (title, seconds)
    ("Neon Harbor", "Midnight Ferries", "2021", "sunset", ["#ff5e62", "#ff9966", "#2b1055"],
     [("Harbor Lights", 214), ("Ferry at 2 AM", 251), ("Salt & Static", 198), ("Lantern Bay", 236)]),
    ("Velvet Static", "Glass Orchard", "2019", "blobs", ["#7f00ff", "#e100ff", "#00d2ff"],
     [("Orchard", 187), ("Prism Rain", 243), ("Slow Bloom", 266)]),
    ("Paper Moons", "Tidal", "2023", "moon", ["#0f2027", "#2c5364", "#f4e2d8"],
     [("Low Tide", 205), ("Moonwalk on Water", 229), ("Undertow", 257), ("Driftwood", 192)]),
    ("Lumen Drift", "Afterglow", "2024", "stripes", ["#f7971e", "#ffd200", "#1f1c2c"],
     [("Afterglow", 248), ("Northern Hum", 221), ("Kite", 176)]),
    ("The Quiet Arcade", "Insert Coin", "2018", "grid", ["#00f260", "#0575e6", "#111111"],
     [("High Score", 183), ("Pixel Heart", 207), ("Continue?", 164)]),
    ("Saffron Skies", "Monsoon Letters", "2022", "waves", ["#e65c00", "#f9d423", "#3a1c71"],
     [("Monsoon", 262), ("Letters Home", 219), ("Cardamom", 201), ("Rooftops", 233)]),
]

VIDEOS = [
    # file stem (Artist - Title), seconds, visual
    ("Lumen Drift - Afterglow (Official Video)", 75, "zoom:stripes:#f7971e,#ffd200,#1f1c2c"),
    ("Neon Harbor - City of Glass (Live)", 70, "mandelbrot"),
    ("Paper Moons - Undertow (Official Video)", 80, "zoom:moon:#0f2027,#2c5364,#f4e2d8"),
    ("Saffron Skies - Monsoon (Lyric Video)", 65, "gradients"),
    ("The Quiet Arcade - Pixel Heart (Official Video)", 60, "life"),
]

LYRICS = ["Lights on the water", "We were drifting slow", "Count the waves with me",
          "Until the morning glow", "Hold the line, hold on", "Every signal fades"]


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def font(size, bold=False):
    for name in (["DejaVuSans-Bold.ttf", "Arial Bold.ttf", "Helvetica.ttc"] if bold
                 else ["DejaVuSans.ttf", "Arial.ttf", "Helvetica.ttc"]):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def cover(style, colours, title=None, artist=None, size=800, seed=0):
    rnd = random.Random(seed)
    c = [hex_rgb(x) for x in colours]
    img = Image.new("RGB", (size, size), c[-1])
    d = ImageDraw.Draw(img)
    if style == "sunset":
        for y in range(size):
            d.line([(0, y), (size, y)], fill=lerp(c[2], c[0], y / size))
        for i, r in enumerate(range(330, 60, -45)):
            d.ellipse([size / 2 - r, size * 0.62 - r, size / 2 + r, size * 0.62 + r],
                      fill=lerp(c[0], c[1], i / 6))
        for y in range(int(size * 0.62), size, 18):
            d.rectangle([0, y, size, y + 7], fill=c[2])
    elif style == "blobs":
        img = Image.new("RGB", (size, size), (20, 6, 40))
        layer = Image.new("RGB", (size, size), (20, 6, 40))
        ld = ImageDraw.Draw(layer)
        for _ in range(7):
            r = rnd.randint(140, 300)
            x, y = rnd.randint(0, size), rnd.randint(0, size)
            ld.ellipse([x - r, y - r, x + r, y + r], fill=c[rnd.randint(0, 2)])
        img = layer.filter(ImageFilter.GaussianBlur(70))
        d = ImageDraw.Draw(img)
    elif style == "moon":
        for y in range(size):
            d.line([(0, y), (size, y)], fill=lerp(c[0], c[1], y / size))
        for _ in range(140):
            x, y = rnd.randint(0, size), rnd.randint(0, int(size * 0.7))
            s = rnd.choice([1, 1, 2])
            d.ellipse([x, y, x + s, y + s], fill=(255, 255, 255))
        r = 170
        d.ellipse([size * 0.62 - r, size * 0.32 - r, size * 0.62 + r, size * 0.32 + r], fill=c[2])
        d.ellipse([size * 0.62 - r + 60, size * 0.32 - r - 20, size * 0.62 + r + 60, size * 0.32 + r - 20],
                  fill=lerp(c[0], c[1], 0.3))
        for i, y in enumerate(range(int(size * 0.72), size, 22)):
            d.line([(0, y), (size, y)], fill=lerp(c[1], c[2], 0.15 + i * 0.05), width=4)
    elif style == "stripes":
        for y in range(size):
            d.line([(0, y), (size, y)], fill=lerp(c[2], (60, 30, 70), y / size))
        for i in range(9):
            off = i * 95 - 200
            d.polygon([(off, size), (off + 50, size), (off + 50 + 420, 0), (off + 420, 0)],
                      fill=lerp(c[0], c[1], i / 8))
    elif style == "grid":
        d.rectangle([0, 0, size, size], fill=c[2])
        step = 50
        for gx in range(0, size, step):
            for gy in range(0, size, step):
                t = (gx + gy) / (2 * size)
                if rnd.random() < 0.55:
                    d.rounded_rectangle([gx + 6, gy + 6, gx + step - 6, gy + step - 6], 8,
                                        fill=lerp(c[0], c[1], t))
    elif style == "waves":
        for y in range(size):
            d.line([(0, y), (size, y)], fill=lerp(c[2], c[0], y / size))
        for k in range(12):
            pts = [(x, size * 0.25 + k * 45 + 30 * math.sin(x / 70 + k)) for x in range(0, size + 10, 10)]
            d.line(pts, fill=lerp(c[1], c[0], k / 12), width=10)
    if title:
        f1, f2 = font(54, True), font(32)
        d.text((48, size - 150), title.upper(), fill=(255, 255, 255), font=f1)
        if artist:
            d.text((50, size - 82), artist, fill=(255, 255, 255), font=f2)
    return img


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def tone(seed):
    """A soft major chord (lavfi aevalsrc), different root per song."""
    root = 196 * (2 ** ((seed % 7) / 12))
    f = [root, root * 1.26, root * 1.5]
    return ("aevalsrc='0.12*sin(2*PI*%.2f*t)+0.08*sin(2*PI*%.2f*t)+0.06*sin(2*PI*%.2f*t)':s=22050"
            % tuple(f))


def make_music(out):
    n = 0
    for artist, album, year, style, colours, tracks in ALBUMS:
        folder = out / "music" / artist / album
        folder.mkdir(parents=True, exist_ok=True)
        art = folder / "cover.jpg"
        cover(style, colours, album, artist, seed=sum(map(ord, album))).save(art, "JPEG", quality=90)
        for i, (title, secs) in enumerate(tracks, 1):
            n += 1
            dest = folder / f"{i:02d} {title.replace('?', '')}.flac"
            run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-t", str(secs), "-i", tone(n),
                 "-i", str(art), "-map", "0:a", "-map", "1:v", "-c:a", "flac", "-compression_level", "8",
                 "-c:v", "mjpeg", "-disposition:v", "attached_pic",
                 "-metadata", f"title={title}", "-metadata", f"artist={artist}",
                 "-metadata", f"album={album}", "-metadata", f"date={year}",
                 "-metadata", f"track={i}", str(dest)])
        art.unlink()  # the art lives in the files (embedded), as in a real library
    return n


def make_videos(out):
    folder = out / "videos"
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for k, (stem, secs, visual) in enumerate(VIDEOS):
            srt = tmp / f"{k}.srt"
            lines = []
            for j in range(secs // 5):
                a, b = j * 5, j * 5 + 4
                lines.append(f"{j + 1}\n00:{a // 60:02d}:{a % 60:02d},000 --> 00:{b // 60:02d}:{b % 60:02d},500\n"
                             f"{LYRICS[(j + k) % len(LYRICS)]}\n")
            srt.write_text("\n".join(lines))
            if visual.startswith("zoom:"):
                _, style, cols = visual.split(":")
                img = tmp / f"{k}.png"
                cover(style, cols.split(","), size=1600, seed=k).resize((1920, 1080)).save(img)
                vin = ["-loop", "1", "-framerate", "25", "-t", str(secs), "-i", str(img)]
                vf = f"zoompan=z='min(zoom+0.0008,1.4)':d={secs * 25}:s=1280x720:fps=25"
            elif visual == "mandelbrot":
                vin = ["-f", "lavfi", "-t", str(secs), "-i", "mandelbrot=s=1280x720:rate=25:maxiter=600"]
                vf = "hue=h=t*4"
            elif visual == "gradients":
                vin = ["-f", "lavfi", "-t", str(secs), "-i",
                       "gradients=s=1280x720:r=25:c0=#e65c00:c1=#f9d423:c2=#3a1c71:c3=#ff0080:n=4:speed=0.015"]
                vf = "null"
            else:  # life
                vin = ["-f", "lavfi", "-t", str(secs), "-i",
                       "life=s=320x180:r=25:mold=12:ratio=0.12:life_color=#00f260:death_color=#0575e6:mold_color=#111133"]
                vf = "scale=1280:720:flags=neighbor"
            run(["ffmpeg", "-y", "-v", "error", *vin, "-f", "lavfi", "-t", str(secs), "-i", tone(k + 3),
                 "-i", str(srt), "-map", "0:v", "-map", "1:a", "-map", "2:s", "-vf", vf,
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-maxrate", "2M", "-bufsize", "4M",
                 "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "96k", "-c:s", "srt", "-metadata:s:s:0", "language=eng",
                 "-disposition:s:0", "default", "-shortest", str(folder / f"{stem}.mkv")])
    return len(VIDEOS)


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "demo-library")
    out.mkdir(parents=True, exist_ok=True)
    songs = make_music(out)
    videos = make_videos(out)
    print(f"demo library in {out}: {songs} songs, {videos} music videos")


if __name__ == "__main__":
    main()
