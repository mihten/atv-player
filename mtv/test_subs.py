#!/usr/bin/env python3
"""subs.py without mpv: settings cleaning (unknown keys / values -> defaults), the mpv properties per style (font, size, box,
bold; the box replaces the shadow), the second line's track (exact language only, never the file's default), the first
page's summary text.
    python3 test_subs.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import subs as S  # noqa: E402

failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def main():
    check(S.clean(None) == S.DEFAULT and S.clean({}) == S.DEFAULT, "nothing stored: defaults")
    check(S.DEFAULT == {"second": "off", "font": "inter", "size": "medium", "box": "off", "bold": True},
          "defaults = the look before the Subtitles page (Inter bold 40, outline, no second line)")
    c = S.clean({"second": "hrv", "font": "comic", "size": "huge", "box": "dark", "bold": "yes", "x": 1})
    check(c == {"second": "hrv", "font": "inter", "size": "medium", "box": "dark", "bold": True},
          f"unknown values fall back, a non-bool bold is ignored, unknown keys dropped ({c})")
    check(S.clean({"bold": False})["bold"] is False, "bold False is kept")

    p = dict(S.props(S.DEFAULT))
    check(p == {"sub-font": "Inter", "sub-bold": True, "sub-font-size": 40, "sub-border-style": "outline-and-shadow",
                "sub-back-color": "#AF000000", "sub-outline-size": 2, "sub-shadow-offset": 1},
          f"default props = the old fixed mpv arguments ({p})")
    p = dict(S.props({"font": "serif", "size": "xl", "box": "light", "bold": False}))
    check(p["sub-font"] == "DejaVu Serif" and p["sub-font-size"] == 62 and p["sub-bold"] is False
          and p["sub-border-style"] == "background-box" and p["sub-back-color"] == "#66000000" and p["sub-shadow-offset"] == 0,
          "serif, extra large, light box, not bold")
    check(dict(S.props({"box": "dark"}))["sub-back-color"] == "#C0000000", "dark box = 75 % black")
    check([s_[0] for s_ in S.SIZES] == ["small", "medium", "large", "xl"] and [px for _, _, px in S.SIZES] == sorted(px for _, _, px in S.SIZES),
          "sizes grow in order")
    check({f for _, _, f in S.FONTS} == {"Inter", "DejaVu Sans", "DejaVu Serif"}, "only fonts installed in the CT")

    subs = [{"ord": 0, "lang": "eng", "is_default": True}, {"ord": 1, "lang": "hrv"}, {"ord": 2, "lang": "hr", "forced": True}]
    item = {"subs": subs}
    check(S.exact_sub_id(item, "hrv") == 2, "Croatian -> its full track (ord 1 -> sid 2), not the forced one")
    check(S.exact_sub_id(item, "eng") == 1, "English -> sid 1")
    check(S.exact_sub_id({"subs": subs[:1]}, "hrv") is None, "a file without Croatian: none (no fallback to the default)")
    check(S.exact_sub_id(item, "off") is None and S.exact_sub_id({}, "hrv") is None and S.exact_sub_id(None, "eng") is None,
          "off / no tracks / no item: none")
    check(S.exact_sub_id({"subs": [{"ord": 0, "lang": "hr", "forced": True}]}, "hrv") == 1, "only a forced one: that one")

    check(S.summary("eng", S.DEFAULT) == "English", "summary: one language")
    check(S.summary("eng", {"second": "hrv"}) == "English + Croatian", "summary: two")
    check(S.summary("hrv", {"second": "hrv"}) == "Croatian", "summary: the same twice counts once")
    check(S.summary("off", {"second": "hrv"}) == "Croatian (top)", "summary: only the top line")
    check(S.summary("off", S.DEFAULT) == "Off" and S.summary("auto", S.DEFAULT) == "Auto", "summary: off / auto")


if __name__ == "__main__":
    main()
    print("ALL PASSED" if not failed else f"FAILED {failed}")
    sys.exit(1 if failed else 0)
