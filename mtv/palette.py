"""Cover-art colours for mtv-player's screens: the same palette the apps compute (Shared/Artwork.swift Palette).

extract(jpeg bytes) -> Palette from a 32x32 thumbnail: a hue histogram weighted by saturation x brightness (vivid
colours win), shadows give the depth colour; grey art gets a cool neutral palette. for_item(id) is the art-free
fallback (a fixed hue per item id). Needs Pillow for extract; without it every item uses for_item.
"""
import colorsys
import io
import math


class Palette:
    def __init__(self, base, tones, vibrant):
        self.base, self.tones, self.vibrant = base, list(tones), vibrant

    def __eq__(self, other):
        return isinstance(other, Palette) and (self.base, self.tones, self.vibrant) == (other.base, other.tones, other.vibrant)

    def __repr__(self):
        return f"Palette(base={self.base}, tones={self.tones}, vibrant={self.vibrant})"


def hsv(h, s, v):
    return colorsys.hsv_to_rgb(h - math.floor(h), max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))


def to_hsv(rgb):
    return colorsys.rgb_to_hsv(*rgb)


def saturated(rgb, s, v):
    """Same hue, at least saturation s, brightness v (RGB.saturated)."""
    h, s0, _ = to_hsv(rgb)
    return hsv(h, max(s0, s), v)


def with_brightness(rgb, v, s=None):
    h, s0, _ = to_hsv(rgb)
    return hsv(h, s0 if s is None else s, v)


def from_hue(hue):
    return Palette(hsv(hue, 0.6, 0.10),
                   [hsv(hue, 0.75, 0.45), hsv(hue + 0.08, 0.7, 0.35), hsv(hue - 0.1, 0.65, 0.4), hsv(hue + 0.5, 0.5, 0.25)],
                   hsv(hue, 0.65, 1.0))


def for_item(item_id):
    return from_hue((int(item_id or 0) * 0.618033988) % 1)


FALLBACK = from_hue(0.72)


def from_pixels(pixels):
    """Palette from (r, g, b) 0..255 tuples (a 32x32 thumbnail); None when there are none."""
    bins = [[0.0, 0.0, 0.0, 0.0] for _ in range(12)]
    dark = [0.0, 0.0, 0.0, 0.0]
    total = 0.0
    n = 0
    for p in pixels:
        n += 1
        px = (p[0] / 255, p[1] / 255, p[2] / 255)
        h, s, v = to_hsv(px)
        if v < 0.5:   # shadows: the depth colour of the background
            w = 0.2 + s
            dark[0] += px[0] * w; dark[1] += px[1] * w; dark[2] += px[2] * w; dark[3] += w
        if not (s > 0.18 and v > 0.14):
            continue
        w = s * v * v
        k = min(11, int(h * 12))
        b = bins[k]
        b[0] += px[0] * w; b[1] += px[1] * w; b[2] += px[2] * w; b[3] += w
        total += w
    if not n:
        return None
    ranked = [(b[0] / b[3], b[1] / b[3], b[2] / b[3])
              for b in sorted((b for b in bins if b[3] > 0 and b[3] > total * 0.04), key=lambda b: -b[3])]
    shadow = (dark[0] / dark[3], dark[1] / dark[3], dark[2] / dark[3]) if dark[3] > 0 else None
    if not ranked:   # grey / black-and-white art: a cool neutral palette
        h = to_hsv(shadow)[0] if shadow else 0.6
        return Palette(hsv(h, 0.15, 0.08), [hsv(h, 0.12, 0.32), hsv(h, 0.1, 0.22), hsv(h + 0.5, 0.1, 0.18), hsv(h, 0.05, 0.4)],
                       hsv(0.58, 0.25, 0.95))
    top = ranked[0]
    th, ts, _ = to_hsv(top)
    if shadow:
        sh, ss, _ = to_hsv(shadow)
        depth = hsv(sh, min(0.8, max(0.35, ss * 1.4)), 0.3)
    else:
        depth = hsv(th, ts * 0.8, 0.28)

    def lift(c):
        return with_brightness(c, min(0.62, max(0.38, to_hsv(c)[2] * 0.7)))
    t0 = lift(top)
    t1 = lift(ranked[1]) if len(ranked) > 1 else hsv(th, ts, 0.3)
    t3 = lift(ranked[2]) if len(ranked) > 2 else tuple((a + b) / 2 for a, b in zip(t0, depth))
    dh, ds, _ = to_hsv(depth)
    return Palette(hsv(dh, min(0.7, ds), 0.09), [t0, t1, depth, t3], saturated(top, 0.55, 1.0))


def extract(data):
    """Palette from image bytes (JPEG / PNG), or None (no Pillow, unreadable image)."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img = img.resize((32, 32), getattr(getattr(Image, "Resampling", Image), "BILINEAR"))
        return from_pixels([tuple(p) for p in img.getdata()])
    except Exception:
        return None
