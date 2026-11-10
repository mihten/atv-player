"""The info popup of mtv-player: the Apple TV app's info bar (ThemedInfoBar) for the TV. A rounded card at the bottom of
the screen with the cover thumbnail, title + artist, codec chips, a glow progress bar with the elapsed / remaining time,
"Next: ..." and the play state / shuffle / repeat marks.

It shows for SHOW_S seconds when a different video starts and when the Back button is pressed with no menu open; Back
while it is up hides it at once. It is not drawn over the lyrics screen (that screen has its own title and progress, as
on the Apple TV), the menu or the screensaver. Pure model + renderer (like menu.py): the player decides when to ask,
passes the data and draws the returned ASS events (osd-overlay 6) plus the thumbnail bitmap (overlay-add 1).

Look: a dark card tinted with the cover's colours, Inter text (draw.py); the thumbnail is a premultiplied BGRA bitmap that
mpv draws on top of the ASS layers (rounded corners come from its alpha), so nothing else may overlap it.
"""
import io
import time

import draw as d
import lyricsview

RES_X, RES_Y = d.RES_X, d.RES_Y
SHOW_S = 5.0
X0, X1, Y1 = 60, 1860, 1020             # card (1920 x 1080 units), bottom edge 60 from the screen's
H = 214
Y0 = Y1 - H
PAD = 34
THUMB = (X0 + PAD, Y0 + PAD, 240, 135)   # x, y, w, h of the cover (the Apple TV's 240 x 135)
THUMB_R = 18
TX = THUMB[0] + THUMB[2] + PAD           # text column
TR = X1 - PAD
TW = TR - TX
CARD = (0.055, 0.055, 0.075)
INK = d.WHITE
Z = 8                                    # below the menu (10), the notice (30) and the screensaver (40)


def clock(sec):
    return lyricsview.clock(sec)


def thumb_bgra(data, w, h, radius):
    """Cover bytes (JPEG / PNG) -> premultiplied BGRA of w x h pixels, cropped to fill, with rounded corners; None when
    Pillow is missing or the picture cannot be read."""
    try:
        from PIL import Image, ImageChops, ImageDraw
        lanczos = getattr(Image, "Resampling", Image).LANCZOS
        im = Image.open(io.BytesIO(data)).convert("RGB")
        sw, sh = im.size
        scale = max(w / sw, h / sh)
        nw, nh = max(w, round(sw * scale)), max(h, round(sh * scale))
        im = im.resize((nw, nh), lanczos)
        left, top = (nw - w) // 2, (nh - h) // 2
        im = im.crop((left, top, left + w, top + h))
        big = Image.new("L", (w * 4, h * 4), 0)       # 4x supersampled mask: smooth corners
        ImageDraw.Draw(big).rounded_rectangle((0, 0, w * 4 - 1, h * 4 - 1), radius * 4, fill=255)
        mask = big.resize((w, h), lanczos)
        r, g, b = im.split()
        r, g, b = (ImageChops.multiply(c, mask) for c in (r, g, b))      # premultiplied alpha
        return Image.merge("RGBA", (b, g, r, mask)).tobytes()
    except Exception:
        return None


class Info:
    def __init__(self, mono=time.monotonic):
        self.mono = mono
        self.until = 0.0
        self.shown_at = -1e9

    @property
    def visible(self):
        return self.mono() < self.until

    def show(self):
        self.shown_at = self.mono()
        self.until = self.mono() + SHOW_S

    @property
    def age(self):
        return self.mono() - self.shown_at

    def hide(self):
        self.until = 0.0

    def toggle(self):
        """Back with no menu: hides the popup when it is up, else shows it. Returns whether it is up now."""
        if self.visible:
            self.hide()
        else:
            self.show()
        return self.visible

    def signature(self, data):
        """What the popup looks like: redraw only when this changes (once a second while playing)."""
        it = data.get("item") or {}
        nx = data.get("next") or {}
        return (it.get("id"), it.get("title"), it.get("artist"), int(data.get("pos") or 0), int(data.get("dur") or 0),
                bool(data.get("playing")), nx.get("title"), bool(data.get("shuffle")), data.get("repeat"),
                data.get("accent"), data.get("base"), bool(data.get("thumb")))

    def render(self, data):
        """ASS events of the card. data: item (summary), pos, dur (s), playing, next (summary or None), shuffle (bool),
        repeat ("all" | "one" | "off"), accent / base (rgb 0..1 or None), thumb (the bitmap is drawn: bool)."""
        it = data.get("item") or {}
        accent = data.get("accent") or (0.45, 0.68, 1.0)
        base = data.get("base")
        card = d.mix(CARD, base, 0.45) if base else CARD
        ev = []
        for g, op in ((18, 0.10), (10, 0.14), (4, 0.20)):          # soft shadow (stacked, no blur: cheap at 4K)
            ev.append(d.rrect(X0 - g, Y0 - g + 8, X1 + g, Y1 + g + 8, 30 + g, d.BLACK, op))
        ev.append(d.rrect(X0, Y0, X1, Y1, 30, card, 1))   # opaque: the video's own subtitle would ghost through
        tx, y = THUMB[0], THUMB[1]
        if not data.get("thumb"):                                  # no cover: a tinted tile keeps the layout
            ev.append(d.rrect(tx, y, tx + THUMB[2], y + THUMB[3], THUMB_R, accent, 0.22))
        # row 1: title + artist, the codec chips at the right edge
        chips = lyricsview.facts(it)
        cw = 0
        chip_ev = []
        cx = TR
        for c in reversed(chips):
            w = d.width(c, 18, 700, 1) + 26
            cx -= w
            chip_ev.append(d.rrect(cx, Y0 + 44, cx + w, Y0 + 44 + 32, 8, INK, 0.13))
            chip_ev.append(d.text(cx + w / 2, Y0 + 44 + 16, c, 18, INK, 0.85, 700, an=5, spacing=1))
            cx -= 10
        cw = TR - cx - 10 if chips else 0
        ev += chip_ev
        room = TW - (cw + 28 if cw else 0)
        title = it.get("title") or f"#{it.get('id', '')}"
        shown = d.ellipsize(title, 44, room, 800)
        t_w = min(d.width(shown, 44, 800) * 1.04 + 26, room)     # libass sets bold Inter a little wider than measured
        ev.append(d.text(TX, Y0 + 60, shown, 44, INK, 1, 800, an=4))
        artist = it.get("artist")
        if artist and room - t_w > 140:
            ev.append(d.text(TX + t_w, Y0 + 62, d.ellipsize(artist, 30, room - t_w, 500), 30, INK, 0.7, 500, an=4))
        # row 2: progress (glow style) and the times
        pos, dur = float(data.get("pos") or 0), float(data.get("dur") or 0)
        by = Y0 + 104
        frac = max(0.0, min(1.0, pos / dur)) if dur else 0.0
        ev.append(d.rrect(TX, by, TR, by + 10, 5, INK, 0.18))
        if frac > 0:
            fx = TX + max(10, TW * frac)
            ev.append(d.rrect(TX, by - 2, fx, by + 12, 7, accent, 0.55, blur=9))
            ev.append(d.rrect(TX, by, fx, by + 10, 5, accent))
        ev.append(d.text(TX, Y0 + 140, clock(pos), 24, INK, 0.65, 500, an=4))
        ev.append(d.text(TR, Y0 + 140, "-" + clock(max(0, dur - pos)), 24, INK, 0.65, 500, an=6))
        # row 3: Next: ... at the left, play state / shuffle / repeat at the right
        marks = []
        playing = bool(data.get("playing"))
        marks.append(("PLAYING" if playing else "PAUSED", accent if playing else INK, 1.0 if playing else 0.75))
        marks.append(("SHUFFLE", accent if data.get("shuffle") else INK, 1.0 if data.get("shuffle") else 0.3))
        rep = data.get("repeat") or "all"
        marks.append(("REPEAT ONE" if rep == "one" else "REPEAT", accent if rep != "off" else INK, 1.0 if rep != "off" else 0.3))
        x = TR
        for label, color, op in reversed(marks):
            ev.append(d.text(x, Y0 + 178, label, 19, color, op, 700, an=6, spacing=3))
            x -= d.width(label, 19, 700, 3) + 30
        nxt = data.get("next")
        if nxt:
            label = "Next: "
            lw = d.width(label, 22, 500)
            ev.append(d.text(TX, Y0 + 178, label, 22, INK, 0.5, 500, an=4))
            ev.append(d.text(TX + lw, Y0 + 178, d.ellipsize(nxt.get("title") or f"#{nxt.get('id', '')}", 22, x - TX - lw - 20, 500),
                             22, INK, 0.8, 500, an=4))
        return "\n".join(ev)
