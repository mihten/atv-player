#!/usr/bin/env python3
"""Build an HTML gallery from a design-shots artifact (tools/design_shots.sh output).

    design_gallery.py <shots dir> <out dir> [title]

Copies the PNGs as JPEG (smaller) and the MP4s, and writes index.html: one section per platform, one row
per screen with every theme side by side, then the transition recordings.
"""
import html
import re
import shutil
import sys
from pathlib import Path

from PIL import Image

ORDER = ["aurora", "noir", "vinyl", "classic"]
SCREENS = {
    "tv": ["home", "player", "player-later", "video", "playlist", "folder", "settings"],
    "phone": ["library", "player", "video", "playlist", "queue", "settings"],
}
LABELS = {"tv": "Apple TV", "phone": "iPhone"}


def main():
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    title = sys.argv[3] if len(sys.argv) > 3 else "ATV Player design"
    out.mkdir(parents=True, exist_ok=True)
    shots = {}
    for f in sorted(src.glob("*.png")):
        m = re.match(r"(tv|phone)-([a-z]+)-(.+)\.png$", f.name)
        if not m:
            continue
        platform, theme, screen = m.groups()
        im = Image.open(f).convert("RGB")
        if im.width > 1920:
            im = im.resize((1920, round(im.height * 1920 / im.width)))
        name = f.stem + ".jpg"
        im.save(out / name, quality=88)
        shots.setdefault(platform, {}).setdefault(screen, {})[theme] = name
    videos = {}
    for f in sorted(src.glob("*.mp4")):
        m = re.match(r"(tv|phone)-([a-z]+)-(transitions|open)\.mp4$", f.name)
        if m and f.stat().st_size > 10_000:
            shutil.copy(f, out / f.name)
            videos.setdefault(m.group(1), {}).setdefault(m.group(3), {})[m.group(2)] = f.name
    themes_seen = sorted({t for p in shots.values() for s in p.values() for t in s}, key=lambda t: ORDER.index(t) if t in ORDER else 9)

    parts = [f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
body{{background:#0d0d10;color:#eee;font:15px -apple-system,Segoe UI,sans-serif;margin:24px}}
h1{{font-weight:800}} h2{{margin-top:40px;border-bottom:1px solid #333;padding-bottom:6px}} h3{{color:#aaa;font-weight:600;margin:22px 0 8px}}
.row{{display:grid;grid-template-columns:repeat({len(themes_seen)},1fr);gap:14px}}
.cell{{background:#18181c;border-radius:10px;padding:8px}} .cell b{{display:block;margin:2px 0 6px;color:#ccc}}
img,video{{width:100%;border-radius:6px;display:block}} .phone img,.phone video{{max-width:330px;margin:auto}}
a{{color:#8cf}}
</style></head><body><h1>{html.escape(title)}</h1>
<p>Looks: {", ".join(t.capitalize() for t in themes_seen)}. Simulator screenshots with a made-up demo library. Click a picture for full size.</p>"""]
    for platform in ("tv", "phone"):
        if platform not in shots:
            continue
        parts.append(f'<h2>{LABELS[platform]}</h2><div class="{platform}">')
        screens = SCREENS[platform] + sorted(set(shots[platform]) - set(SCREENS[platform]))
        for screen in screens:
            row = shots[platform].get(screen)
            if not row:
                continue
            parts.append(f"<h3>{html.escape(screen)}</h3><div class='row'>")
            for theme in themes_seen:
                name = row.get(theme)
                cell = f"<a href='{name}'><img loading='lazy' src='{name}'></a>" if name else "<i>—</i>"
                parts.append(f"<div class='cell'><b>{theme.capitalize()}</b>{cell}</div>")
            parts.append("</div>")
        for kind, label in (("open", "Opening the player"), ("transitions", "Next, next, previous, pause, play")):
            clips = videos.get(platform, {}).get(kind)
            if not clips:
                continue
            parts.append(f"<h3>Video: {label}</h3><div class='row'>")
            for theme in themes_seen:
                name = clips.get(theme)
                cell = f"<video src='{name}' controls muted loop playsinline preload='metadata'></video>" if name else "<i>—</i>"
                parts.append(f"<div class='cell'><b>{theme.capitalize()}</b>{cell}</div>")
            parts.append("</div>")
        parts.append("</div>")
    parts.append("</body></html>")
    (out / "index.html").write_text("\n".join(parts))
    print(f"{out / 'index.html'}: {sum(len(s) for p in shots.values() for s in p.values())} screenshots, "
          f"{sum(len(c) for v in videos.values() for c in v.values())} videos")


if __name__ == "__main__":
    main()
