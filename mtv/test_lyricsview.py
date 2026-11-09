#!/usr/bin/env python3
"""lyricsview.py, draw.py and palette.py without mpv: the language rule (apps' subtitleOrdinal), the mpv subtitle id,
only time-synced lyrics shown, the current line / lit window / scroll target and its animation, the drawing (bright
current line, dim others, edge fade, escaping, the note when there are no timed lyrics), the left column, the shader text
(and, when ffmpeg with libplacebo + Vulkan is there, that libplacebo compiles and runs it), text measuring / wrapping, and
the cover palette.
    python3 test_lyricsview.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import draw as D  # noqa: E402
import lyricsview as L  # noqa: E402
import palette as P  # noqa: E402

failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def T(lang, ord_, default=False, forced=False, synced=True, cues=None):
    return {"ord": ord_, "lang": lang, "is_default": default, "forced": forced, "synced": synced,
            "cues": cues if cues is not None else [{"s": 1.0, "e": 2.0, "t": f"{lang} line"}]}


CUES = [{"s": 5.0, "e": 8.0, "t": "First line"}, {"s": 9.0, "e": 12.0, "t": "Second {line}"},
        {"s": 13.0, "e": 15.0, "t": "Third line"}, {"s": 30.0, "e": 33.0, "t": "After a gap"}] + \
       [{"s": 40.0 + 3 * i, "e": 42.0 + 3 * i, "t": f"Later line {i}"} for i in range(25)]
ITEM = {"id": 77, "kind": "video", "title": "A Song", "artist": "Singer", "codec": "hevc", "acodec": "opus", "year": "2025",
        "duration": 200, "lyr": 2, "art": True}


def texts(ass):
    """[(text, alpha hex, y)] of the text events (not shapes)."""
    out = []
    for line in ass.split("\n"):
        m = re.match(r"\{(.*?)\}(.*)$", line)
        if not m or "\\p1" in m.group(1):
            continue
        a = re.search(r"\\1a&H([0-9A-F]{2})&", m.group(1))
        y = re.search(r"\\pos\(([-\d.]+),([-\d.]+)\)", m.group(1))
        out.append((m.group(2), int(a.group(1), 16) if a else 0, float(y.group(2)) if y else None))
    return out


def main():
    # ---- the language rule
    eng_d, eng_f, hrv = T("eng", 0, default=True), T("eng", 1, forced=True), T("hrv", 2)
    check(L.pick([eng_f, eng_d, hrv], "eng") is eng_d, "English: the default-flagged English track")
    check(L.pick([eng_f, T("eng", 3), hrv], "eng")["ord"] == 3, "English: a full track before a forced one")
    check(L.pick([eng_f, hrv], "eng") is eng_f, "English: a forced one when it is the only English")
    check(L.pick([eng_d, hrv], "hrv") is hrv and L.pick([eng_d, T("hr", 4)], "hrv")["ord"] == 4,
          "Croatian: hrv, also the 2-letter code")
    check(L.pick([hrv, eng_d], "hrv") is hrv and L.pick([eng_d, T("ita", 5)], "hrv") is eng_d,
          "a language the file lacks: the file's default")
    check(L.pick([T("ita", 0), T("eng", 1, forced=True)], "auto")["ord"] == 1 and L.pick([T("ita", 0), T("eng", 1)], "auto")["ord"] == 0,
          "Auto: default, else forced, else the first")
    check(L.pick([eng_d], "off") is None and L.pick([], "eng") is None, "Off / no tracks: none")
    item = {"subs": [{"ord": 0, "lang": "eng", "is_default": True}, {"ord": 1, "lang": "hrv"}]}
    check(L.sub_id(item, "hrv") == 2 and L.sub_id(item, "eng") == 1 and L.sub_id(item, "auto") == 1,
          "mpv sid = ord + 1 (subtitle tracks count from 1)")
    check(L.sub_id(item, "off") is None and L.sub_id({"subs": None}, "eng") is None and L.sub_id(None, "eng") is None,
          "no sid: Off, no subtitles")

    # ---- only time-synced lyrics
    v = L.LyricsView()
    v.set_item(ITEM)
    check(v.state == "loading", "item with lyrics, not fetched yet: loading")
    check(not v.set_lyrics(78, [T("eng", 0, cues=CUES)]) and v.tracks is None, "lyrics of another item are ignored")
    v.set_lyrics(77, [T("eng", 0, synced=False, cues=[{"s": None, "e": None, "t": "plain"}])])
    check(v.state == "none" and not v.blocks, "untimed lyrics only: not shown (state none)")
    v.set_lyrics(77, [T("eng", 0, default=True, cues=CUES), T("hrv", 1, cues=[{"s": 2.0, "e": 3.0, "t": "Prvi stih"}])])
    check(v.state == "synced" and v.blocks[0][1] == ["First line"], "synced English shown")
    v.set_lang("hrv")
    check(v.blocks[0][1] == ["Prvi stih"], "language Croatian: the Croatian track")
    v.set_lang("off")
    check(v.blocks[0][1] == ["First line"], "Off: the screen still shows lyrics (the file's default track)")
    v.set_lang("eng")
    v2 = L.LyricsView()
    v2.set_item(dict(ITEM, lyr=0))
    check(v2.state == "none", "an item without lyrics: none, no loading")
    v5 = L.LyricsView()
    v5.set_item(ITEM)
    v5.set_lyrics(77, [T("eng", 0, default=True, synced=False, cues=[{"s": None, "e": None, "t": "plain text"}]),
                       T("hrv", 1, cues=[{"s": 2.0, "e": 3.0, "t": "Prvi stih"}])])
    check(v5.state == "synced" and v5.blocks[0][1] == ["Prvi stih"],
          "default track untimed, another one timed: the timed one is shown (Auto)")

    # ---- current line, lit window, scroll target, animation
    check(v.update(1.0, 100.0) is False and v.idx == -1 and not v.lit, "before the first line: nothing current")
    check(v.update(5.5, 100.0) is True and v.idx == 0 and v.lit, "at 5.5 s: line 0, lit")
    check(v.update(6.0, 100.1) is False, "same line: no change")
    check(v.update(9.2, 101.0) and v.idx == 1 and v.lit, "at 9.2 s: line 1")
    v.update(15.0, 102.0)
    check(v.idx == 2 and v.lit, "line 2 at its end")
    v.update(16.4, 103.0)
    check(v.idx == 2 and v.lit, "still lit 1.4 s after its end")
    check(v.update(16.6, 104.0) and v.idx == 2 and not v.lit, "dark 1.6 s after its end (gap between verses)")
    v.update(31.0, 105.0)
    check(v.idx == 3 and v.lit, "after the gap: line 3")
    v.update(0.5, 106.0)
    check(v.idx == -1 and v.offset == 0.0, "seek back before the first line: nothing current, top of the list")
    check(v._target(0) == 0.0, "the first lines do not scroll (offset clamped at 0)")
    far = len(v.blocks) - 1
    _c, _l, h, y = v.blocks[far]
    check(abs(v._target(far) - (y + L.ANCHOR * h - L.ANCHOR * L.PANEL_H)) < 1e-6 and v._target(far) > 0,
          "a later line sits at 38 % of the panel")
    v.update(CUES[far]["s"] + 0.1, 200.0)
    target = v._target(far)
    check(v.animating(200.1) and 0 < v.scroll(200.25) < target, "the scroll is animated (half way after 0.25 s)")
    check(abs(v.scroll(200.6) - target) < 1e-6 and not v.animating(200.6), "it arrives after 0.5 s and stops animating")
    v.update(CUES[far - 1]["s"] + 0.1, 201.0)
    now_at = v.scroll(201.2)
    v.update(CUES[far]["s"] + 0.1, 201.2)
    check(abs(v.anim[0] - now_at) < 1e-6 and abs(now_at - v._target(far - 1)) > 1 and v.anim[1] == v._target(far),
          "a new line during a scroll starts from where the list is now")

    # ---- drawing
    v = L.LyricsView()
    v.set_item(ITEM)
    v.set_lyrics(77, [T("eng", 0, default=True, cues=CUES)])
    v.update(9.5, 300.0)
    ass = v.front(9.5, 200, True, [{"id": 1, "title": "Next {one}", "artist": "B"}], 301.0)
    tx = texts(ass)
    cur = [t for t in tx if t[0] == "Second \\{line\\}"]
    dim = [t for t in tx if t[0] == "First line"]
    check(cur and cur[0][1] == 0, "the current line: full opacity (braces escaped)")
    check(dim and abs(dim[0][1] - round(0.7 * 255)) <= 2, "another line: 30 % opacity")
    check("\\fscx93" in ass and "\\fscx100" not in [t for t in ass.split("\n") if "Second" in t][0],
          "others at 93 %, the current one at full size")
    mid_anim = v.front(9.5, 200, True, [], 300.1)
    a_mid = [t for t in texts(mid_anim) if t[0].startswith("Second")][0][1]
    check(0 < a_mid < round(0.7 * 255), "lighting up is animated (opacity between dim and full after 0.1 s)")
    check("NOW PLAYING" in ass and "A Song" in ass and "Singer" in ass and "HEVC" in ass and "OPUS" in ass and "2025" in ass,
          "left column: status, title, artist, facts")
    check("UP NEXT" in ass and "Next \\{one\\}" in ass and "0:09" in ass and "-3:10" in ass, "up next, elapsed / remaining")
    check("PAUSED" in v.front(9.5, 200, False, [], 301.0), "paused: PAUSED")
    v.update(150.0, 400.0)
    tx = texts(v.front(150.0, 200, True, [], 401.0))
    lyr = [t for t in tx if t[0].startswith("Later line") and t[2] is not None]
    top = min(lyr, key=lambda t: t[2])
    check(top[1] > round(0.7 * 255), f"a line at the panel's top edge fades out (alpha {top[1]})")
    check(all(L.PANEL_Y0 - 70 <= t[2] <= L.PANEL_Y1 + 10 for t in lyr), "only lines inside the panel are drawn")
    v3 = L.LyricsView()
    v3.set_item(ITEM)
    v3.set_lyrics(77, [T("eng", 0, synced=False, cues=[{"s": None, "t": "x"}])])
    note = v3.front(10, 200, True, [], 1.0)
    check("No time-synced lyrics in this video" in note and "}x" not in note, "untimed lyrics: a note, no text")
    v4 = L.LyricsView()
    v4.set_item(ITEM)
    check("No time-synced" not in v4.front(1, 200, True, [], 1.0), "still loading: no note yet")
    check(v.signature(150.2, True, []) == v.signature(150.7, True, []) and v.signature(150.7, True, []) != v.signature(151.1, True, []),
          "signature changes per second (progress), not per frame")
    long = L.LyricsView()
    long.set_item(ITEM)
    long.set_lyrics(77, [T("eng", 0, cues=[{"s": 1.0, "e": 5.0, "t": "word " * 40}])])
    check(len(long.blocks[0][1]) > 1 and all(D.width(x, L.LINE, L.LINE_W) <= L.RX1 - L.RX0 + 1 for x in long.blocks[0][1]),
          "a long line wraps inside the panel")

    # ---- shader
    v.palette = P.Palette((0.1, 0.0, 0.0), [(0.5, 0.1, 0.1), (0.1, 0.5, 0.1), (0.1, 0.1, 0.5), (0.3, 0.3, 0.0)], (1, 0.5, 0.5))
    sh = v.shader()
    check(sh.startswith("//!HOOK OUTPUT") and "vec4 hook()" in sh and "vec3(0.1000, 0.0000, 0.0000)" in sh
          and "vec3(0.1000, 0.1000, 0.5000)" in sh and "frame" in sh, "shader: OUTPUT hook, the palette's colours, drift by frame")
    check(sh != L.LyricsView().shader(), "another palette, another shader")
    check("rect" in dir(D) and v.solid().count("\\p1") == 1, "solid fallback: one rectangle")
    flat = L.LyricsView()
    flat.set_item(ITEM)
    flat.palette = P.Palette((0.2, 0.4, 0.6), [(0.2, 0.4, 0.6)] * 4, (1, 1, 1))
    ff = shutil.which("ffmpeg")
    if ff and "libplacebo" in subprocess.run([ff, "-hide_banner", "-filters"], capture_output=True, text=True).stdout:
        tmp = tempfile.mkdtemp()
        path, out = os.path.join(tmp, "s.glsl"), os.path.join(tmp, "s.png")
        open(path, "w").write(flat.shader())
        r = subprocess.run([ff, "-v", "error", "-y", "-init_hw_device", "vulkan=vk", "-filter_hw_device", "vk", "-f", "lavfi",
                            "-i", "testsrc2=s=320x180:d=1", "-vf",
                            f"format=yuv420p,hwupload,libplacebo=w=320:h=180:format=rgba:custom_shader_path={path},"
                            "hwdownload,format=rgba", "-frames:v", "1", out], capture_output=True, text=True)
        if r.returncode == 0:
            try:
                from PIL import Image
                px = Image.open(out).convert("RGB").getpixel((160, 10))
                k = 1 - (0.10 + 0.25 * ((10.5 / 180) / 0.5))      # the darkening at that row
                want = [c * k * 255 for c in (0.2, 0.4, 0.6)]
                check(all(abs(a - b) <= 4 for a, b in zip(px, want)),
                      f"libplacebo compiled and ran the shader: one-colour palette, darkened as expected ({px} ~ {[round(w) for w in want]})")
            except ImportError:
                check(True, "libplacebo compiled and ran the shader")
        elif "vulkan" in r.stderr.lower() and "device" in r.stderr.lower():
            print("SKIP no Vulkan device for libplacebo")
        else:
            check(False, f"libplacebo rejected the shader: {r.stderr[-300:]}")
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print("SKIP no ffmpeg with libplacebo")

    # ---- draw helpers
    check(D.bgr((1.0, 0.5, 0.0)) == "&H0080FF&" and D.alpha(1) == "&H00&" and D.alpha(0) == "&HFF&", "colour / alpha")
    lines = D.wrap("one two three four five six seven eight nine ten", 40, 300)
    check(len(lines) > 1 and all(D.width(x, 40) <= 300 for x in lines) and " ".join(lines).split() == "one two three four five six seven eight nine ten".split(),
          "wrap: every line fits, no word lost")
    check(D.wrap("x" * 200, 40, 300, max_lines=2)[-1].endswith("…") and len(D.wrap("x" * 200, 40, 300, max_lines=2)) == 2,
          "wrap: a long word is cut, max_lines ends with …")
    e = D.ellipsize("A very long title that does not fit at all", 40, 300)
    check(e.endswith("…") and D.width(e, 40) <= 300 and D.ellipsize("Short", 40, 300) == "Short", "ellipsize")
    size, ls = D.fit("A title long enough to need a smaller size for three lines max", 62, 400, 3)
    check(size < 62 and len(ls) <= 3, f"fit: smaller size, at most 3 lines ({size}, {len(ls)})")
    path = D.rrect_path(0, 0, 100, 50, 10)
    check(path.startswith("m 10 0") and path.count(" b ") == 4, "rounded rectangle: 4 curves")
    check(D.esc("a{b}\\c\nd") == "a\\{b\\}\\\u200bc d", "escape")

    # ---- palette
    red = [(220, 30, 30)] * 900 + [(10, 5, 5)] * 124
    p = P.from_pixels(red)
    h, s, _ = P.to_hsv(p.vibrant)
    check((h < 0.05 or h > 0.95) and s >= 0.55 and abs(max(p.vibrant) - 1.0) < 1e-6, "red art: a bright red accent")
    check(max(p.base) <= 0.0901, "base is very dark")
    grey = P.from_pixels([(128, 128, 128)] * 1024)
    check(grey.vibrant == P.hsv(0.58, 0.25, 0.95), "grey art: the cool neutral palette")
    check(P.for_item(5) == P.for_item(5) and P.for_item(5) != P.for_item(6), "art-free palette: fixed per item id")
    check(P.from_pixels([]) is None and P.extract(b"not an image") is None, "no pixels / bad bytes: None")
    try:
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (30, 60, 200)).save(buf, "PNG")
        pb = P.extract(buf.getvalue())
        hb = P.to_hsv(pb.vibrant)[0]
        check(0.55 < hb < 0.72, f"extract from a blue PNG: blue accent (hue {hb:.2f})")
    except ImportError:
        print("SKIP Pillow missing")


if __name__ == "__main__":
    main()
    print("ALL PASSED" if not failed else f"FAILED {failed}")
    sys.exit(1 if failed else 0)
