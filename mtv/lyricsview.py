"""Live lyrics screen of mtv-player: the Apple TV's lyrics view (Sources/TVLyrics.swift + Shared/Lyrics.swift), drawn
with mpv osd-overlays over the video, which keeps playing underneath.

Two layers: backdrop() = an opaque Aurora background in the cover's colours (drawn once per item), front() = the left
column (status, title, artist, album, facts, progress, up next) and the time-synced lyrics on the right: the current
line bright and full size, the others dim and a little smaller, the list scrolls by itself so the current line sits at
38 % of the panel (0.5 s ease in / out); lines fade out at the panel's top and bottom edges. No manual scrolling. Only
TIME-SYNCED lyrics are shown (owner's rule): a file with untimed lyrics or none shows the left column and a short note.

Pure model + renderer: set_item / set_lyrics / set_lang feed it, update(position, now) moves the current line and
returns True when it changed, front(...) returns ASS events for that moment (animations are computed from `now`, so the
player redraws a few times per second while animating() is true).

Lyrics language (shared with the subtitles on the video): "off", "auto" (the file's default track), "eng", "hrv";
pick() is the apps' rule (PlayerSettings.subtitleOrdinal / LyricsResponse.chosen).
"""
import draw as d
import palette as pal

LANGS = [("off", "Off"), ("auto", "Auto"), ("eng", "English"), ("hrv", "Croatian")]
LANG_CODES = {"eng": {"eng", "en"}, "hrv": {"hrv", "hr", "scr", "cro", "hbs", "sh"}}

# layout (Apple TV points = canvas pixels): padding 100 / top 90 / bottom 110, left column 720, spacing 90
PAD_X, TOP, BOTTOM = 100, 90, 110
LEFT_W, GAP = 720, 90
LX0 = PAD_X
RX0, RX1 = PAD_X + LEFT_W + GAP, d.RES_X - PAD_X
PANEL_Y0, PANEL_Y1 = TOP, d.RES_Y - BOTTOM
PANEL_H = PANEL_Y1 - PANEL_Y0
# lyrics (LyricsMetrics.tv): line 54, gap 34, top 140, anchor 0.38
LINE, LINE_W, LINE_LH = 54, 700, 1.17          # size, weight, line height factor
CUE_GAP, CONTENT_TOP, ANCHOR = 34, 140, 0.38
DIM, DIM_SCALE = 0.3, 93                       # inactive lines: opacity, scale %
FADE_TOP, FADE_BOTTOM = 0.09, 0.10             # edge fade (fraction of the panel height)
SCROLL_S, LIT_S = 0.5, 0.3                     # animation lengths
LIT_AFTER_END = 1.5                            # a line stays lit 1.5 s after its end time
INK = d.WHITE


def pick(tracks, lang):
    """The track to show for a language choice (apps' rule): the matching language (default-flagged, then a full one, then
    forced); else, and for "auto", the file's default, else a forced one, else the first. None for "off" / no tracks."""
    if not tracks or lang == "off":
        return None
    codes = LANG_CODES.get(lang)
    if codes:
        matching = [t for t in tracks if (t.get("lang") or "") in codes]
        for cond in (lambda t: t.get("is_default") and not t.get("forced"), lambda t: not t.get("forced"), lambda t: True):
            hit = next((t for t in matching if cond(t)), None)
            if hit is not None:
                return hit
    for cond in (lambda t: t.get("is_default"), lambda t: t.get("forced"), lambda t: True):
        hit = next((t for t in tracks if cond(t)), None)
        if hit is not None:
            return hit
    return None


def sub_id(item, lang):
    """mpv `sid` for the video's subtitle (lyrics) track in this language, or None (off / the file has none). Subtitle
    tracks are numbered from 1 in file order, `ord` is the ordinal among them."""
    subs = (item or {}).get("subs") or []
    t = pick(subs, lang)
    if t is None:
        return None
    try:
        return int(t.get("ord", 0)) + 1
    except (TypeError, ValueError):
        return None


def ease_in_out(t):
    t = max(0.0, min(1.0, t))
    return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def ease_out(t):
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def clock(sec):
    sec = max(0, int(sec or 0))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def facts(item):
    """"HEVC" "OPUS" "Lossless" "2025" (Ornaments.swift `facts`: codec badges, lossless, year)."""
    it = item or {}
    out = []
    if it.get("kind") == "video" and it.get("codec"):
        out.append(str(it["codec"]).upper())
    a = it.get("acodec") if it.get("kind") == "video" else it.get("codec")
    if a:
        out.append(str(a).upper())
    if it.get("lossless"):
        out.append("Lossless")
    y = str(it.get("year") or "")
    if len(y) >= 4:
        out.append(y[:4])
    return out


class LyricsView:
    def __init__(self):
        self.on = False
        self.lang = "auto"
        self.item = None
        self.tracks = None       # None = not fetched yet, [] = none
        self.track = None        # the synced track shown
        self.blocks = []         # [(cue, [wrapped lines], height, content_y)]
        self.idx = -1
        self.lit = False
        self.offset = 0.0        # scroll offset now (target of the running animation)
        self.anim = (0.0, 0.0, -1e9)   # (from offset, to offset, start time)
        self.lit_at = -1e9       # when the current line lit up / went dark
        self.palette = None

    # -- data
    def set_item(self, item):
        if (item or {}).get("id") == (self.item or {}).get("id") and item is not None:
            self.item = item
            return
        self.item = item
        self.tracks = None
        self.track = None
        self.blocks = []
        self.idx, self.lit, self.offset = -1, False, 0.0
        self.anim = (0.0, 0.0, -1e9)
        self.palette = None

    def set_lyrics(self, item_id, tracks):
        if self.item is None or self.item.get("id") != item_id:
            return False
        self.tracks = list(tracks or [])
        self._choose()
        return True

    def set_lang(self, lang):
        self.lang = lang if lang in dict(LANGS) else "auto"
        if self.tracks is not None:
            self._choose()

    def _choose(self):
        synced = [t for t in self.tracks or [] if t.get("synced") and t.get("cues")]
        track = pick(synced, "auto" if self.lang == "off" else self.lang)
        if track is not self.track:
            self.track = track
            self._layout()
            self.idx, self.lit, self.offset = -1, False, 0.0
            self.anim = (0.0, 0.0, -1e9)

    def _layout(self):
        self.blocks = []
        y = CONTENT_TOP
        lh = LINE * LINE_LH
        for cue in (self.track or {}).get("cues") or []:
            text = " ".join(str(cue.get("t") or "").split())
            if not text or cue.get("s") is None:
                continue
            lines = d.wrap(text, LINE, RX1 - RX0, LINE_W)
            h = lh * len(lines)
            self.blocks.append((cue, lines, h, y))
            y += h + CUE_GAP

    @property
    def state(self):
        """'loading', 'synced' or 'none' (no time-synced lyrics in this file)."""
        if self.item is None:
            return "none"
        if self.tracks is None:
            return "loading" if (self.item.get("lyr") or 0) != 0 else "none"
        return "synced" if self.blocks else "none"

    # -- time
    def _target(self, idx):
        if idx < 0 or not self.blocks:
            return 0.0
        _cue, _lines, h, y = self.blocks[idx]
        return max(0.0, y + ANCHOR * h - ANCHOR * PANEL_H)

    def scroll(self, now):
        a, b, at = self.anim
        p = ease_in_out((now - at) / SCROLL_S)
        return a + (b - a) * p

    def update(self, pos, now):
        """Moves to the line at `pos`. True when the current line or its lit state changed."""
        if not self.blocks:
            return False
        found = -1
        for i, (cue, _l, _h, _y) in enumerate(self.blocks):
            if cue["s"] <= pos:
                found = i
            else:
                break
        end = self.blocks[found][0].get("e") if found >= 0 else None
        lit = found >= 0 and (end is None or pos <= end + LIT_AFTER_END)
        changed = False
        if found != self.idx:
            target = self._target(found)
            self.anim = (self.scroll(now), target, now)
            self.offset = target
            self.idx = found
            changed = True
        if lit != self.lit:
            self.lit = lit
            self.lit_at = now
            changed = True
        return changed

    def animating(self, now):
        return now - self.anim[2] < SCROLL_S or now - self.lit_at < LIT_S

    # -- drawing
    def shader(self):
        """The Aurora background as an mpv user shader (gpu-next / libplacebo, hook OUTPUT): the cover's base colour, four
        big soft glows in its tones drifting slowly (libplacebo's `frame` counter, still while paused), darker towards the
        bottom (black .10 -> .35 -> .62, AuroraBackground's gradient), plus its own dither noise (the player runs
        --dither=no). Drawn by the GPU UNDER every OSD layer, so the text stays on top and the CPU does nothing; libass
        needed seconds per 4K frame for the same blurred shapes. The video keeps decoding underneath."""
        p = self.palette or pal.for_item((self.item or {}).get("id"))

        def v3(c):
            return "vec3({:.4f}, {:.4f}, {:.4f})".format(*c)
        blobs = []
        for i, ((ox, oy), tone, (sx, sy, ph)) in enumerate(zip(
                ((-0.3, -0.25), (0.3, -0.2), (-0.2, 0.3), (0.25, 0.25)), (2, 0, 1, 3),
                ((0.13, 0.11, 0.0), (0.17, 0.09, 2.0), (0.12, 0.15, 4.0), (0.09, 0.13, 5.0)))):
            blobs.append(f"    c = over(c, {v3(p.tones[tone])}, 0.85 * blob(q, ctr + vec2({ox}, {oy}) + "
                         f"0.05 * vec2(sin(t * {sx} + {ph}), sin(t * {sy} + {ph + 1}))));")
        return "\n".join([
            "//!HOOK OUTPUT", "//!BIND HOOKED", "//!DESC mtv-player lyrics backdrop", "",
            "vec3 over(vec3 under, vec3 col, float a) { return mix(under, col, clamp(a, 0.0, 1.0)); }",
            "float blob(vec2 q, vec2 c) { return 1.0 - smoothstep(0.17, 0.53, distance(q, c)); }", "",
            "vec4 hook() {",
            "    vec2 size = HOOKED_size;",
            "    float s = max(size.x, size.y);",
            "    vec2 q = HOOKED_pos * size / s;",
            "    vec2 ctr = 0.5 * size / s;",
            "    float t = float(frame) / 60.0;",
            f"    vec3 c = {v3(p.base)};",
            *blobs,
            "    float y = HOOKED_pos.y;",
            "    c *= 1.0 - (y < 0.5 ? 0.10 + 0.25 * (y / 0.5) : 0.35 + 0.27 * ((y - 0.5) / 0.5));",
            "    float n = fract(sin(dot(HOOKED_pos * size, vec2(12.9898, 78.233)) + float(frame)) * 43758.5453);",
            "    return vec4(c + (n - 0.5) / 255.0, 1.0);",
            "}", ""])

    def solid(self):
        """ASS background for an item with no picture at all (no video, no cover: the shader has nothing to draw on)."""
        p = self.palette or pal.for_item((self.item or {}).get("id"))
        return d.rect(0, 0, d.RES_X, d.RES_Y, p.base)

    def front(self, pos, duration, playing, upcoming, now):
        p = self.palette or pal.for_item((self.item or {}).get("id"))
        accent = p.vibrant
        ev = []
        self._left(ev, pos, duration, playing, upcoming or [], accent)
        if self.state == "synced":
            self._lyrics(ev, now)
        elif self.state == "none":
            y = PANEL_Y0 + CONTENT_TOP
            ev.append(d.text(RX0, y, "LYRICS", 20, INK, 0.45, 800, spacing=3))
            ev.append(d.text(RX0, y + 44, "No time-synced lyrics in this " + (
                "video" if (self.item or {}).get("kind") == "video" else "song"), 38, INK, 0.55, 500))
        return "\n".join(ev)

    def _left(self, ev, pos, duration, playing, upcoming, accent):
        it = self.item or {}
        x, y = LX0, PANEL_Y0
        # status: four bars + NOW PLAYING / PAUSED (StatusEyebrow)
        for i, lv in enumerate((0.55, 1.0, 0.7, 0.85) if playing else (0.22,) * 4):
            bh = 28 * lv
            ev.append(d.rrect(x + i * 9, y + 28 - bh, x + i * 9 + 6, y + 28, 3, accent))
        ev.append(d.text(x + 50, y + 1, "NOW PLAYING" if playing else "PAUSED", 24, accent, 1, 700, spacing=5))
        y += 28 + 34
        size, lines = d.fit(it.get("title") or f"#{it.get('id', '')}", 62, LEFT_W, 3, 800)
        for line in lines:
            ev.append(d.text(x, y, line, size, INK, 1, 800))
            y += size * 1.12
        y += 6
        if it.get("artist"):
            ev.append(d.text(x, y, d.ellipsize(it["artist"], 36, LEFT_W, 600), 36, INK, 0.82, 600))
            y += 48
        if it.get("album"):
            ev.append(d.text(x, y, d.ellipsize(it["album"], 28, LEFT_W), 28, INK, 0.55))
            y += 38
        chips = facts(it)
        if chips:   # FactChip: small capsules
            y += 10
            cx = x
            for c in chips:
                w = d.width(c, 20, 700, 1) + 28
                ev.append(d.rrect(cx, y, cx + w, y + 36, 9, INK, 0.13))
                ev.append(d.text(cx + w / 2, y + 18, c, 20, INK, 0.85, 700, an=5, spacing=1))
                cx += w + 12
            y += 36
        # progress (glow style)
        y += 42
        frac = max(0.0, min(1.0, pos / duration)) if duration else 0.0
        ev.append(d.rrect(x, y, x + LEFT_W, y + 10, 5, INK, 0.18))
        if frac > 0:
            fx = x + max(10, LEFT_W * frac)
            ev.append(d.rrect(x, y - 2, fx, y + 12, 7, accent, 0.55, blur=9))
            ev.append(d.rrect(x, y, fx, y + 10, 5, accent))
        y += 22
        ev.append(d.text(x, y, clock(pos), 24, INK, 0.65, 500))
        ev.append(d.text(x + LEFT_W, y, "-" + clock(max(0, (duration or 0) - pos)), 24, INK, 0.65, 500, an=9))
        y += 34
        # up next (numbered)
        if upcoming:
            y += 40
            ev.append(d.text(x, y, "UP NEXT", 20, INK, 0.6, 700, spacing=4))
            y += 40
            for n, nx in enumerate(upcoming[:3]):
                if y > PANEL_Y1 - 30:
                    break
                ev.append(d.text(x, y, f"{n + 1}.", 24, accent, 1, 500))
                title = nx.get("title") or f"#{nx.get('id', '')}"
                tw = min(d.width(title, 26, 500), LEFT_W - 44)
                ev.append(d.text(x + 44, y - 1, d.ellipsize(title, 26, LEFT_W - 44, 500), 26, INK, 1, 500))
                if nx.get("artist") and LEFT_W - 44 - tw > 80:
                    room = LEFT_W - 44 - tw - 14
                    ev.append(d.text(x + 44 + tw + 14, y + 1, d.ellipsize("— " + nx["artist"], 24, room), 24, INK, 0.55))
                y += 26 + 14

    def _lyrics(self, ev, now):
        off = self.scroll(now)
        lh = LINE * LINE_LH
        clip = (RX0 - 40, PANEL_Y0, d.RES_X, PANEL_Y1)
        lit_p = ease_out((now - self.lit_at) / LIT_S)
        top_fade, bottom_fade = FADE_TOP * PANEL_H, FADE_BOTTOM * PANEL_H
        for i, (_cue, lines, h, y) in enumerate(self.blocks):
            sy = PANEL_Y0 + y - off
            if sy + h < PANEL_Y0 - 10 or sy > PANEL_Y1 + 10:
                continue
            active = i == self.idx and self.lit
            if active:
                t = lit_p
            elif i == self.idx:      # just went dark
                t = 1 - lit_p
            else:
                t = 0.0
            op = DIM + (1 - DIM) * t
            scale = DIM_SCALE + (100 - DIM_SCALE) * t
            for n, line in enumerate(lines):
                ly = sy + n * lh * (scale / 100)
                mid = ly + lh / 2
                edge = 1.0
                if mid < PANEL_Y0 + top_fade:
                    edge = max(0.0, (mid - PANEL_Y0) / top_fade)
                elif mid > PANEL_Y1 - bottom_fade:
                    edge = max(0.0, (PANEL_Y1 - mid) / bottom_fade)
                if op * edge <= 0.01:
                    continue
                ev.append(d.text(RX0, ly, line, LINE, INK, op * edge, LINE_W, scale=scale, clip=clip))

    def signature(self, pos, playing, upcoming):
        """Changes whenever the front layer looks different outside an animation (redraw then)."""
        return (self.state, self.idx, self.lit, int(pos), bool(playing),
                tuple((u.get("id") for u in (upcoming or [])[:3])), (self.item or {}).get("id"), id(self.palette))
