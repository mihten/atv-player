"""Drawing helpers for mtv-player's on-screen layers (mpv osd-overlay, ASS events on a 1920x1080 canvas).

Shapes (rectangles, rounded rectangles, circles), text in the Inter typeface, colours from 0..1 RGB tuples, and text
measuring for wrapping: with Pillow and the Inter font files the width is exact (libass draws a font at its win ascent +
descent height, ~0.82-0.83 of the em size Pillow uses: SCALE, measured against libass renders); without them a
per-character estimate. Everything here is pure (no mpv), so the layouts can be unit-tested and rendered with ffmpeg's
`ass` filter (same libass) to look at them.
"""
import os

RES_X, RES_Y = 1920, 1080
FONT = "Inter"
FONT_DIRS = [d for d in (os.environ.get("MTV_FONT_DIR"), "/usr/share/fonts/opentype/inter") if d]
FONT_FILES = {400: "Inter-Regular.otf", 500: "Inter-Medium.otf", 600: "Inter-SemiBold.otf", 700: "Inter-Bold.otf",
              800: "Inter-ExtraBold.otf"}
SCALE = {400: 0.829, 500: 0.822, 600: 0.816, 700: 0.831, 800: 0.822}   # libass width / (Pillow width per em x size)
EST_EM = 0.50          # average character width per em (x SCALE) when the font cannot be measured
K = 0.5523             # cubic Bezier factor for quarter circles

WHITE = (1.0, 1.0, 1.0)
BLACK = (0.0, 0.0, 0.0)


def esc(text):
    """Text for an ASS event: no override blocks, no escapes, one line."""
    s = str(text or "").replace("\\", "\\\u200b").replace("{", "\\{").replace("}", "\\}")
    return s.replace("\r", " ").replace("\n", " ")


def bgr(rgb):
    """(r, g, b) 0..1 -> ASS colour &HBBGGRR&."""
    r, g, b = (max(0, min(255, int(round(c * 255)))) for c in rgb)
    return f"&H{b:02X}{g:02X}{r:02X}&"


def alpha(opacity):
    """Opacity 0..1 -> ASS alpha (&H00& = opaque)."""
    return f"&H{max(0, min(255, int(round((1 - max(0.0, min(1.0, opacity))) * 255)))):02X}&"


def mix(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def _n(v):
    v = round(v, 1)
    return int(v) if v == int(v) else v


def _shape(path, color, opacity=1.0, blur=0.0, clip=None):
    tags = f"\\an7\\pos(0,0)\\bord0\\shad0\\1c{bgr(color)}\\1a{alpha(opacity)}"
    if blur:
        tags += f"\\blur{_n(blur)}"
    if clip:
        tags += "\\clip({},{},{},{})".format(*(_n(c) for c in clip))
    return f"{{{tags}\\p1}}{path}{{\\p0}}"


def rect(x0, y0, x1, y1, color, opacity=1.0, blur=0.0, clip=None):
    x0, y0, x1, y1 = (_n(v) for v in (x0, y0, x1, y1))
    return _shape(f"m {x0} {y0} l {x1} {y0} {x1} {y1} {x0} {y1}", color, opacity, blur, clip)


def rrect_path(x0, y0, x1, y1, r):
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    k = r * (1 - K)
    p = [f"m {_n(x0 + r)} {_n(y0)}", f"l {_n(x1 - r)} {_n(y0)}",
         f"b {_n(x1 - k)} {_n(y0)} {_n(x1)} {_n(y0 + k)} {_n(x1)} {_n(y0 + r)}", f"l {_n(x1)} {_n(y1 - r)}",
         f"b {_n(x1)} {_n(y1 - k)} {_n(x1 - k)} {_n(y1)} {_n(x1 - r)} {_n(y1)}", f"l {_n(x0 + r)} {_n(y1)}",
         f"b {_n(x0 + k)} {_n(y1)} {_n(x0)} {_n(y1 - k)} {_n(x0)} {_n(y1 - r)}", f"l {_n(x0)} {_n(y0 + r)}",
         f"b {_n(x0)} {_n(y0 + k)} {_n(x0 + k)} {_n(y0)} {_n(x0 + r)} {_n(y0)}"]
    return " ".join(p)


def rrect(x0, y0, x1, y1, r, color, opacity=1.0, blur=0.0, clip=None):
    return _shape(rrect_path(x0, y0, x1, y1, r), color, opacity, blur, clip)


def circle(cx, cy, r, color, opacity=1.0, blur=0.0):
    return rrect(cx - r, cy - r, cx + r, cy + r, r, color, opacity, blur)


def text(x, y, s, size, color=WHITE, opacity=1.0, weight=400, an=7, spacing=0.0, scale=100.0, clip=None, font=FONT):
    """One line of text. an: ASS alignment (7 top left, 9 top right, 4/5/6 middle, 1/3 bottom)."""
    tags = (f"\\an{an}\\pos({_n(x)},{_n(y)})\\fn{font}\\b{weight}\\fs{_n(size)}\\bord0\\shad0"
            f"\\1c{bgr(color)}\\1a{alpha(opacity)}")
    if spacing:
        tags += f"\\fsp{_n(spacing)}"
    if scale != 100:
        tags += f"\\fscx{_n(scale)}\\fscy{_n(scale)}"
    if clip:
        tags += "\\clip({},{},{},{})".format(*(_n(c) for c in clip))
    return f"{{{tags}}}{esc(s)}"


# -- measuring
_FONTS = {}


def _pil_font(weight):
    if weight in _FONTS:
        return _FONTS[weight]
    font = None
    try:
        from PIL import ImageFont
        for d in FONT_DIRS:
            path = os.path.join(d, FONT_FILES[_weight(weight)])
            if os.path.isfile(path):
                font = ImageFont.truetype(path, 100)
                break
    except Exception:  # no Pillow / unreadable file: estimate
        font = None
    _FONTS[weight] = font
    return font


def _weight(weight):
    return min(FONT_FILES, key=lambda w: abs(w - weight))


def can_measure():
    return _pil_font(400) is not None


def width(s, size, weight=400, spacing=0):
    """Width in canvas pixels of one line as libass draws it."""
    s = str(s or "")
    if not s:
        return 0.0
    w = _weight(weight)
    font = _pil_font(w)
    em = font.getlength(s) / 100 if font is not None else EST_EM * len(s) / SCALE[w]
    return em * size * SCALE[w] + spacing * max(0, len(s) - 1)


def ellipsize(s, size, max_w, weight=400, spacing=0):
    """The text, cut with "…" so it fits max_w."""
    s = str(s or "")
    if width(s, size, weight, spacing) <= max_w:
        return s
    lo, hi = 0, len(s)
    while lo < hi:   # longest prefix that fits with the ellipsis
        mid = (lo + hi + 1) // 2
        if width(s[:mid].rstrip() + "…", size, weight, spacing) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return (s[:lo].rstrip() + "…") if lo else "…"


def wrap(s, size, max_w, weight=400, max_lines=None):
    """Word wrap to max_w; a word longer than a line is cut. max_lines: the last line ends with "…" when text is left."""
    words = str(s or "").split()
    lines, cur = [], ""
    for word in words:
        trial = f"{cur} {word}" if cur else word
        if width(trial, size, weight) <= max_w:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        while width(word, size, weight) > max_w and len(word) > 1:   # a very long word
            n = len(word)
            while n > 1 and width(word[:n], size, weight) > max_w:
                n -= 1
            lines.append(word[:n])
            word = word[n:]
        cur = word
    if cur:
        lines.append(cur)
    if max_lines is not None and len(lines) > max_lines:
        rest = " ".join(lines[max_lines - 1:])
        lines = lines[:max_lines - 1] + [ellipsize(rest, size, max_w, weight)]
    return lines


def fit(s, size, max_w, max_lines, weight=400, min_scale=0.55):
    """(size, lines): the largest size down to min_scale x size at which the text fits max_lines lines."""
    floor = size * min_scale
    cur = size
    while cur > floor:
        lines = wrap(s, cur, max_w, weight)
        if len(lines) <= max_lines:
            return cur, lines
        cur = max(floor, cur - 2)
    return floor, wrap(s, floor, max_w, weight, max_lines)
