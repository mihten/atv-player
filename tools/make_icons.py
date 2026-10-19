#!/usr/bin/env python3
"""Generate the tvOS app icon (layered image stacks) and Top Shelf images as an asset catalog.

Run from the repo root: python3 tools/make_icons.py  (needs Pillow).
Output: Sources/Assets.xcassets (tvOS) and iOS/Assets.xcassets (iPhone), both committed; regenerate after
changing the design here. `python3 tools/make_icons.py ios` writes only the iPhone icon.
"""
import sys
import json
import math
import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parent.parent / "Sources" / "Assets.xcassets"
BRAND = OUT / "App Icon & Top Shelf Image.brandassets"
INFO = {"author": "xcode", "version": 1}
SS = 4  # supersampling for anti-aliased shapes

TOP = (24, 28, 74)       # deep indigo
BOTTOM = (122, 38, 160)  # purple
GLOW = (255, 120, 90)    # warm accent
BOLD_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def write_json(path, data):
    path.mkdir(parents=True, exist_ok=True)
    (path / "Contents.json").write_text(json.dumps(data, indent=2) + "\n")


def gradient(w, h):
    """Diagonal indigo→purple gradient with a soft warm glow (opaque back layer)."""
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            t = min(1.0, max(0.0, (0.35 * x / w + 0.65 * y / h)))
            px[x, y] = tuple(int(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3))
    glow = Image.new("RGB", (w, h), GLOW)
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    r = int(h * 0.75)
    cx, cy = int(w * 0.82), int(h * 1.05)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=110)
    mask = mask.filter(ImageFilter.GaussianBlur(h * 0.25))
    return Image.composite(glow, img, mask)


def rings(w, h):
    """Middle layer: faint vinyl-record rings, transparent elsewhere."""
    big = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    cx, cy = w * SS * 0.5, h * SS * 0.5
    base = h * SS * 0.40
    for i in range(9):
        r = base * (1 - i * 0.085)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 255, 255, 34 if i % 2 else 22),
                  width=max(2, int(h * SS * 0.006)))
    d.ellipse((cx - base * 0.16, cy - base * 0.16, cx + base * 0.16, cy + base * 0.16), fill=(255, 255, 255, 40))
    return big.resize((w, h), Image.Resampling.LANCZOS)


def bezier(p0, p1, p2, p3, n=24):
    pts = []
    for i in range(n + 1):
        t = i / n
        a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t ** 2, t ** 3
        pts.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return pts


def mark(w, h):
    """Front layer: rounded play triangle + eighth note, white on transparent."""
    W, H = w * SS, h * SS
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    s = H * 0.36  # mark height (kept inside the focus-crop safe zone)
    cx, cy = W * 0.5, H * 0.5
    # play triangle (left)
    tx = cx - s * 0.80
    d.polygon([(tx, cy - s * 0.5), (tx, cy + s * 0.5), (tx + s * 0.86, cy)], fill=255)
    # eighth note (right): tilted oval head, stem, curved flag
    nx, hy = cx + s * 0.30, cy + s * 0.36
    head = Image.new("L", (int(s * 0.5), int(s * 0.36)), 0)
    ImageDraw.Draw(head).ellipse((0, 0, head.width - 1, head.height - 1), fill=255)
    head = head.rotate(22, expand=True, resample=Image.Resampling.BICUBIC)
    m.paste(255, (int(nx - head.width / 2), int(hy - head.height / 2)), head)
    stem_w = s * 0.075
    sx = nx + s * 0.20 - stem_w
    top = cy - s * 0.60
    d.rectangle((sx, top, sx + stem_w, hy - s * 0.02), fill=255)
    x0 = sx + stem_w
    outer = bezier((x0 - 1, top), (x0 + s * 0.04, top + s * 0.22), (x0 + s * 0.36, top + s * 0.28), (x0 + s * 0.20, top + s * 0.66))
    inner = bezier((x0 + s * 0.20, top + s * 0.66), (x0 + s * 0.27, top + s * 0.44), (x0 + s * 0.06, top + s * 0.38), (x0 - 1, top + s * 0.32))
    d.polygon(outer + inner, fill=255)
    # round every corner: blur, then threshold at 50 % (edges stay put, corners round off)
    m = m.filter(ImageFilter.GaussianBlur(s * 0.035)).point(lambda v: 255 if v >= 128 else 0)
    big = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    big.putalpha(m)
    return big.resize((w, h), Image.Resampling.LANCZOS)


def save(img, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG")


def image_stack(name, sizes, idiom):
    """sizes: list of (scale, w, h). Writes a 3-layer imagestack."""
    stack = BRAND / f"{name}.imagestack"
    layers = [("Front", mark), ("Middle", rings), ("Back", gradient)]
    write_json(stack, {"info": INFO, "layers": [{"filename": f"{n}.imagestacklayer"} for n, _ in layers]})
    for layer_name, draw in layers:
        layer = stack / f"{layer_name}.imagestacklayer"
        write_json(layer, {"info": INFO})
        imageset = layer / "Content.imageset"
        images = []
        for scale, w, h in sizes:
            fname = f"{layer_name.lower()}-{w}x{h}.png"
            img = draw(w, h)
            if layer_name == "Back":
                img = img.convert("RGB")
            save(img, imageset / fname)
            images.append({"filename": fname, "idiom": idiom, "scale": scale})
        write_json(imageset, {"images": images, "info": INFO})


def top_shelf(name, sizes):
    imageset = BRAND / f"{name}.imageset"
    images = []
    for scale, w, h in sizes:
        img = gradient(w, h).convert("RGBA")
        logo_h = int(h * 0.55)
        logo = Image.alpha_composite(rings(int(logo_h * 5 / 3), logo_h), mark(int(logo_h * 5 / 3), logo_h))
        img.alpha_composite(logo, (int(w * 0.08), (h - logo_h) // 2))
        d = ImageDraw.Draw(img)
        font = ImageFont.truetype(BOLD_FONT, int(h * 0.16))
        sub = ImageFont.truetype(BOLD_FONT, int(h * 0.055))
        x = int(w * 0.08 + logo.width + w * 0.03)
        d.text((x, h * 0.34), "ATV Player", font=font, fill=(255, 255, 255, 255))
        d.text((x, h * 0.56), "Music · Music videos · One queue", font=sub, fill=(255, 255, 255, 200))
        fname = f"{name.lower().replace(' ', '-')}-{w}x{h}.png"
        save(img.convert("RGB"), imageset / fname)
        images.append({"filename": fname, "idiom": "tv", "scale": scale})
    write_json(imageset, {"images": images, "info": INFO})


IOS_OUT = Path(__file__).resolve().parent.parent / "iOS" / "Assets.xcassets"


def ios_icon():
    """iPhone: one opaque 1024x1024 icon (Xcode derives every size), same design as the TV icon,
    the mark 25 % larger because a square has no tvOS focus-crop margin."""
    if IOS_OUT.exists():
        shutil.rmtree(IOS_OUT)
    write_json(IOS_OUT, {"info": INFO})
    iconset = IOS_OUT / "AppIcon.appiconset"
    big = 1280
    img = gradient(big, big).convert("RGBA")
    img.alpha_composite(rings(big, big))
    img.alpha_composite(mark(big, big))
    off = (big - 1024) // 2
    img = img.crop((off, off, off + 1024, off + 1024)).convert("RGB")
    save(img, iconset / "icon-1024.png")
    write_json(iconset, {"images": [{"filename": "icon-1024.png", "idiom": "universal", "platform": "ios",
                                     "size": "1024x1024"}], "info": INFO})
    print("written", IOS_OUT)


def main():
    if sys.argv[1:] == ["ios"]:
        ios_icon()
        return
    if OUT.exists():
        shutil.rmtree(OUT)
    write_json(OUT, {"info": INFO})
    write_json(BRAND, {
        "assets": [
            {"filename": "App Icon - App Store.imagestack", "idiom": "tv", "role": "primary-app-icon", "size": "1280x768"},
            {"filename": "App Icon.imagestack", "idiom": "tv", "role": "primary-app-icon", "size": "400x240"},
            {"filename": "Top Shelf Image Wide.imageset", "idiom": "tv", "role": "top-shelf-image-wide", "size": "2320x720"},
            {"filename": "Top Shelf Image.imageset", "idiom": "tv", "role": "top-shelf-image", "size": "1920x720"},
        ],
        "info": INFO,
    })
    image_stack("App Icon", [("1x", 400, 240), ("2x", 800, 480)], "tv")
    image_stack("App Icon - App Store", [("1x", 1280, 768)], "tv")
    top_shelf("Top Shelf Image", [("1x", 1920, 720), ("2x", 3840, 1440)])
    top_shelf("Top Shelf Image Wide", [("1x", 2320, 720), ("2x", 4640, 1440)])
    # flattened preview for review (not part of the catalog)
    prev = gradient(800, 480).convert("RGBA")
    prev.alpha_composite(rings(800, 480))
    prev.alpha_composite(mark(800, 480))
    prev.convert("RGB").save(OUT.parent.parent / "tools" / "icon-preview.png")
    print("written", OUT)
    ios_icon()


if __name__ == "__main__":
    main()
