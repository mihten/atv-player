#!/usr/bin/env python3
"""info.py (the TV Player's info popup) without mpv: the 5 s timer (show / hide / toggle), what makes it redraw, the drawing
(title + artist, codec chips, progress geometry, times, Next, play / shuffle / repeat marks, the empty-cover tile), that
everything stays inside the card and long text is cut, ASS escaping, and the cover bitmap (premultiplied BGRA, rounded
corners, cropped to fill, a bad picture -> None).
    python3 test_info.py   (Pillow for the bitmap checks)
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import draw as d  # noqa: E402
import info as I  # noqa: E402

failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


ACCENT, BASE = (1.0, 0.45, 0.25), (0.1, 0.05, 0.08)
ITEM = {"id": 7, "kind": "video", "title": "Million Reasons", "artist": "Arhanna", "codec": "hevc", "acodec": "opus", "year": 2025}


def data(**kw):
    return dict({"item": ITEM, "pos": 95.0, "dur": 118.0, "playing": True, "next": {"id": 8, "title": "Jar Of Hearts"},
                 "shuffle": True, "repeat": "all", "accent": ACCENT, "base": BASE, "thumb": True}, **kw)


def events(ass):
    return [e for e in ass.split("\n") if e]


def texts(ass):
    """[(x, y, tags, text)] of the text events."""
    out = []
    for e in events(ass):
        m = re.match(r"\{([^}]*)\}(.*)$", e)
        if m and "\\p1" not in m.group(1):
            pos = re.search(r"\\pos\(([\d.]+),([\d.]+)\)", m.group(1))
            out.append((float(pos.group(1)), float(pos.group(2)), m.group(1), m.group(2)))
    return out


def shapes(ass):
    """[(tags, [numbers of the path])] of the shape events."""
    out = []
    for e in events(ass):
        m = re.match(r"\{([^}]*)\\p1\}(.*?)\{\\p0\}$", e)
        if m:
            out.append((m.group(1), [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", m.group(2))]))
    return out


def find(ass, word):
    return [t for t in texts(ass) if word in t[3]]


def alpha_of(tags):
    return re.search(r"\\1a(&H[0-9A-F]{2}&)", tags).group(1)


def main():
    # ---- the 5 s timer
    c = Clock()
    f = I.Info(c)
    check(not f.visible, "starts hidden")
    f.show()
    check(f.visible and f.age == 0, "show: visible")
    c.t += 4.9
    check(f.visible, "still up after 4.9 s")
    c.t += 0.2
    check(not f.visible, "gone after 5 s (SHOW_S = 5)")
    check(f.toggle() is True and f.visible, "toggle while hidden: shows")
    c.t += 2
    check(f.toggle() is False and not f.visible, "toggle while up: hides at once")
    f.show()
    c.t += 3
    f.show()
    c.t += 3
    check(f.visible, "show again restarts the 5 s")
    f.hide()
    check(not f.visible, "hide")

    # ---- what makes it redraw
    sig = f.signature(data())
    check(f.signature(data(pos=95.6)) == sig, "the same whole second: no redraw")
    for label, kw in (("a second later", {"pos": 96.0}), ("paused", {"playing": False}), ("another next", {"next": {"id": 9, "title": "X"}}),
                      ("shuffle off", {"shuffle": False}), ("repeat one", {"repeat": "one"}), ("cover arrived", {"thumb": False}),
                      ("another item", {"item": dict(ITEM, id=8)}), ("another colour", {"accent": (0.2, 0.9, 0.3)}),
                      ("longer file", {"dur": 200.0})):
        check(f.signature(data(**kw)) != sig, f"redraw when: {label}")

    # ---- the drawing
    ass = f.render(data())
    t = {w: find(ass, w) for w in ("Million Reasons", "Arhanna", "HEVC", "OPUS", "2025", "1:35", "-0:23", "Next: ", "Jar Of Hearts",
                                   "PLAYING", "SHUFFLE", "REPEAT")}
    check(all(len(v) == 1 for v in t.values()), f"every text drawn once ({ {k: len(v) for k, v in t.items()} })")
    check(t["Million Reasons"][0][0] == I.TX and t["Arhanna"][0][0] > I.TX + d.width("Million Reasons", 44, 800), "artist right of the title")
    check(t["1:35"][0][0] == I.TX and t["-0:23"][0][0] == I.TR, "times: elapsed left, remaining right")
    # progress: the accent bar ends at the elapsed fraction of the text column
    accent = d.bgr(ACCENT)
    fills = [p for tags, p in shapes(ass) if f"\\1c{accent}" in tags and "\\1a&H00&" in tags]
    check(len(fills) == 1 and abs(max(fills[0][0::2]) - (I.TX + I.TW * 95 / 118)) < 1.5,
          f"progress bar ends at 95 / 118 of the column ({max(fills[0][0::2]):.1f} vs {I.TX + I.TW * 95 / 118:.1f})")
    half = I.Info().render(data(pos=59.0))
    fh = [p for tags, p in shapes(half) if f"\\1c{accent}" in tags and "\\1a&H00&" in tags]
    check(abs(max(fh[0][0::2]) - (I.TX + I.TW * 0.5)) < 1.5, "half way -> half the column")
    start = I.Info().render(data(pos=0.0))
    check(not [p for tags, p in shapes(start) if f"\\1c{accent}" in tags and "\\1a&H00&" in tags], "at 0:00 no fill")
    check(f"\\blur" in ass, "the fill has a glow")
    unknown = I.Info().render(data(dur=0, pos=0))
    check("-0:00" in unknown and "Million Reasons" in unknown, "unknown duration: no crash, 0:00")
    # marks
    check(alpha_of(t["PLAYING"][0][2]) == d.alpha(1.0) and f"\\1c{accent}" in t["PLAYING"][0][2], "PLAYING in the accent colour")
    paused = I.Info().render(data(playing=False, shuffle=False, repeat="off"))
    check(find(paused, "PAUSED") and not find(paused, "PLAYING"), "paused -> PAUSED")
    check(alpha_of(find(paused, "SHUFFLE")[0][2]) == d.alpha(0.3) and alpha_of(find(ass, "SHUFFLE")[0][2]) == d.alpha(1.0),
          "SHUFFLE: dim when off, accent when on")
    check(alpha_of(find(paused, "REPEAT")[0][2]) == d.alpha(0.3) and alpha_of(find(ass, "REPEAT")[0][2]) == d.alpha(1.0),
          "REPEAT: dim when off, accent when on")
    check(find(I.Info().render(data(repeat="one")), "REPEAT ONE"), "repeat one is named")
    # chips and Next
    check(t["HEVC"][0][0] < t["OPUS"][0][0] < t["2025"][0][0] <= I.TR, "codec chips in order at the right edge")
    nonext = I.Info().render(data(next=None))
    check("Next: " not in nonext and "PLAYING" in nonext, "no next item: no Next line")
    check(find(I.Info().render(data(item={"id": 7, "title": "Only"})), "Only") and not find(I.Info().render(data(item={"id": 7, "title": "Only"})), "Arhanna"),
          "no artist, no chips: just the title")
    # cover tile
    tile = [tags for tags, p in shapes(ass) if f"\\1c{accent}" in tags and d.alpha(0.22) in tags]
    check(not tile, "with the cover bitmap there is no placeholder tile")
    tile = [tags for tags, p in shapes(I.Info().render(data(thumb=False))) if f"\\1c{accent}" in tags and d.alpha(0.22) in tags]
    check(len(tile) == 1, "without a cover a tinted tile keeps the layout")

    # ---- inside the card, long text cut, escaping
    long_item = dict(ITEM, title="An extremely long video title that goes on and on and on " * 3, artist="An artist " * 10)
    la = I.Info().render(data(item=long_item, next={"id": 3, "title": "A next title that is also much too long to fit " * 3}))
    inside = all(I.X0 <= x <= I.X1 and I.Y0 <= y <= I.Y1 for x, y, tags, _ in texts(la))
    check(inside, "every text starts inside the card")
    right = max((x for x, y, tags, _ in texts(la) if "\\an4" in tags or "\\an7" in tags), default=0)
    check(right < I.TR, "left-aligned texts start before the right edge")
    tl = find(la, "An extremely")[0]
    cut = re.sub(r"\\\\?\u200b", "", tl[3])
    chips_w = sum(d.width(c, 18, 700, 1) + 36 for c in ("HEVC", "OPUS", "2025"))
    check("…" in cut and d.width(cut, 44, 800) <= I.TW - chips_w, f"long title cut with … to fit before the chips ({d.width(cut, 44, 800):.0f} <= {I.TW - chips_w:.0f})")
    check(not find(la, "An artist"), "no room for the artist: dropped")
    nx = find(la, "A next title")[0]
    check("…" in nx[3], "long Next title cut")
    esc = I.Info().render(data(item=dict(ITEM, title="Song {x} \\b1 end", artist="A\nB")))
    check("\\{x\\}" in esc and "\\b1" not in esc.replace("\\\\\u200bb1", ""), "braces and backslashes in a title are escaped")
    # a shapes never leave the screen
    allx = [v for _, p in shapes(ass) for v in p[0::2]]
    ally = [v for _, p in shapes(ass) for v in p[1::2]]
    check(0 <= min(allx) and max(allx) <= I.RES_X and 0 <= min(ally) and max(ally) <= I.RES_Y, "all shapes stay on the screen (incl. the shadow)")
    check(I.X0 == 60 and I.Y1 == 1080 - 60 and I.THUMB[2:] == (240, 135), "geometry: 60 px margins, the Apple TV's 240 x 135 cover")

    # ---- the cover bitmap
    try:
        from PIL import Image
    except ImportError:
        print("SKIP bitmap checks (no Pillow)")
        return
    def png(size, colour):
        b = io.BytesIO()
        Image.new("RGB", size, colour).save(b, "PNG")
        return b.getvalue()
    w, h, r = 240, 135, 18
    buf = I.thumb_bgra(png((640, 360), (200, 40, 40)), w, h, r)
    px = lambda x, y: tuple(buf[(y * w + x) * 4:(y * w + x) * 4 + 4])   # noqa: E731  B, G, R, A
    check(buf is not None and len(buf) == w * h * 4, f"bitmap = w x h x 4 bytes ({len(buf)})")
    check(px(w // 2, h // 2) == (40, 40, 200, 255), f"centre: opaque, BGRA order ({px(w // 2, h // 2)})")
    check(px(0, 0) == (0, 0, 0, 0) and px(w - 1, h - 1) == (0, 0, 0, 0), "corners: fully transparent (rounded)")
    check(px(w // 2, 0)[3] == 255 and px(0, h // 2)[3] == 255, "straight edges stay opaque")
    edge = [px(x, y) for x in range(0, r) for y in range(0, r) if 0 < px(x, y)[3] < 255]
    check(edge and all(b <= a and g <= a and rr <= a for b, g, rr, a in edge), "corner edge: soft alpha, colours premultiplied (<= alpha)")
    sq = I.thumb_bgra(png((100, 100), (10, 200, 10)), w, h, r)
    check(sq is not None and len(sq) == w * h * 4 and tuple(sq[(h // 2 * w + w // 2) * 4:][:4]) == (10, 200, 10, 255), "a square cover is cropped to fill")
    # a cover with a green stripe in the middle of a red square: cropped to fill (the stripe is centred and 2.4 x), not squeezed
    stripe = Image.new("RGB", (100, 100), (220, 20, 20))
    stripe.paste((20, 220, 20), (0, 40, 100, 60))
    sb = io.BytesIO()
    stripe.save(sb, "PNG")
    st = I.thumb_bgra(sb.getvalue(), w, h, r)
    spx = lambda x, y: tuple(st[(y * w + x) * 4:(y * w + x) * 4 + 3])   # noqa: E731
    check(spx(w // 2, 2) == (20, 20, 220) and spx(w // 2, 50) == (20, 220, 20) and spx(w // 2, 130) == (20, 20, 220),
          f"fill crop, not a squeeze: red top, green stripe at row 50, red bottom ({spx(w // 2, 2)}, {spx(w // 2, 50)}, {spx(w // 2, 130)})")
    tall = I.thumb_bgra(png((90, 400), (5, 5, 250)), w, h, r)
    check(tall is not None and len(tall) == w * h * 4, "a tall cover too")
    check(I.thumb_bgra(b"not a picture", w, h, r) is None and I.thumb_bgra(b"", w, h, r) is None, "a bad picture -> None, no exception")
    big = I.thumb_bgra(png((640, 360), (200, 40, 40)), 480, 270, 36)
    check(big is not None and len(big) == 480 * 270 * 4, "any size (the 4K screen's 480 x 270)")


if __name__ == "__main__":
    main()
    print("ALL PASSED" if not failed else f"FAILED {failed}")
    sys.exit(1 if failed else 0)
