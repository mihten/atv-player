"""Subtitle (lyrics) settings of mtv-player: the second language shown at the top, and the look of both lines.

The video's lyrics tracks are plain SRT, so mpv's own --sub-* style options apply to them (and to the second track, which mpv
draws at the top with --secondary-sub-pos). Pure data + helpers: the player keeps the choice in lyrics.json, the menu shows
the rows (menu.py, "Subtitles" page), props() gives the mpv properties for a style.

Owner 2026-11-11: a Subtitles submenu with language, font, size, box, and two subtitles at once (English bottom, Croatian
top). Fonts are the ones installed in the CT (fonts-inter, fonts-dejavu-core).
"""
import lyricsview

SECOND = [("off", "Off"), ("eng", "English"), ("hrv", "Croatian")]
FONTS = [("inter", "Inter", "Inter"), ("sans", "Sans", "DejaVu Sans"), ("serif", "Serif", "DejaVu Serif")]
SIZES = [("small", "Small", 32), ("medium", "Medium", 40), ("large", "Large", 50), ("xl", "Extra large", 62)]
BOXES = [("off", "Off", None), ("light", "Light", "#66000000"), ("dark", "Dark", "#C0000000")]
DEFAULT = {"second": "off", "font": "inter", "size": "medium", "box": "off", "bold": True}
SHADOW_COLOR = "#AF000000"     # mpv's default sub-back-color: the shadow colour when there is no box
SECOND_POS = 10                # --secondary-sub-pos: % from the top (checked on a real frame: clear of the edge)
CHOICES = {"second": [(k, n) for k, n in SECOND], "font": [(k, n) for k, n, _ in FONTS],
           "size": [(k, n) for k, n, _ in SIZES], "box": [(k, n) for k, n, _ in BOXES]}


def clean(data):
    """Stored / requested settings -> a full style dict with known values only (unknown keys / values -> defaults)."""
    out = dict(DEFAULT)
    if isinstance(data, dict):
        for key, options in CHOICES.items():
            if data.get(key) in dict(options):
                out[key] = data[key]
        if isinstance(data.get("bold"), bool):
            out["bold"] = data["bold"]
    return out


def props(style):
    """[(mpv property, value)] for a style; the same for the bottom and the top line (mpv styles both with sub-*)."""
    s = clean(style)
    font = next(f for k, _, f in FONTS if k == s["font"])
    size = next(px for k, _, px in SIZES if k == s["size"])
    box = next(c for k, _, c in BOXES if k == s["box"])
    return [("sub-font", font), ("sub-bold", s["bold"]), ("sub-font-size", size),
            ("sub-border-style", "background-box" if box else "outline-and-shadow"),
            ("sub-back-color", box or SHADOW_COLOR), ("sub-outline-size", 2), ("sub-shadow-offset", 0 if box else 1)]


def exact_sub_id(item, lang):
    """mpv `secondary-sid` for this language, or None: only a track really in that language (the second line never falls
    back to the file's default track, that would repeat the bottom line)."""
    codes = lyricsview.LANG_CODES.get(lang)
    if not codes:
        return None
    tracks = [t for t in ((item or {}).get("subs") or []) if (t.get("lang") or "") in codes]
    for cond in (lambda t: t.get("is_default") and not t.get("forced"), lambda t: not t.get("forced"), lambda t: True):
        hit = next((t for t in tracks if cond(t)), None)
        if hit is not None:
            try:
                return int(hit.get("ord", 0)) + 1
            except (TypeError, ValueError):
                return None
    return None


def summary(lang, style):
    """The Subtitles row's value on the menu's first page: "English + Croatian", "Auto", "Off"."""
    first = dict(lyricsview.LANGS).get(lang, "Auto")
    second = clean(style)["second"]
    if second == "off" or second == lang:
        return first
    name = dict(SECOND)[second]
    return name + " (top)" if lang == "off" else f"{first} + {name}"
