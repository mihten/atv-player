"""On-screen menu of mtv-player, drawn with mpv's osd-overlay (ASS) over the video or the lyrics screen.

Pure model + renderer: Menu holds a stack of pages, key() moves / selects, render() returns ASS events. Lists come
from the server's HTTP API through an async `fetch(path)`; commands go back through `send(msg)` (the player's
WebSocket, the server owns the queue). Keys: Up / Down move (CH+ / CH- page), OK / Right select or open, Left back
one page, Blue (Menu), Back or Red closes.

Row kinds (the owner's Apple TV conventions): an ACTION runs on OK; a TOGGLE (two values: Loudness, Lyrics screen,
Shuffle) switches on OK; a CHOICE of more than two values (Lyrics language) steps with Left / Right and stops at the
ends; a LEVEL (Volume, Boost) steps with Left / Right, OK mutes the volume. `ctl` (the player) gives the volume
(`volume_now()`, `volume_step()`, `volume_mute()`), the lyrics language and screen (`lyrics_lang`, `set_lyrics_lang()`,
`lyrics_on`, `set_lyrics_on()`) and the cover colours (`accent()`); Boost and Loudness are the server's sound settings
of this session (state `sound`), changed with the `sound` command.

Look: a dark rounded card tinted with the cover's colours, an eyebrow + title header (now playing on the first page),
the selected row as a light pill with dark text (tvOS focus style), level bars, switches and ‹ value › choices drawn in
the cover's accent colour, key hints as small chips at the bottom. Text is Inter (draw.py).
"""
import time

import draw as d

RES_X, RES_Y = d.RES_X, d.RES_Y
X0, Y0, X1, Y1 = 72, 56, 1112, 1024     # card
PAD = 44                                 # text inset
ROW_Y, ROW_H, ROWS = 262, 54, 13         # first row, row height, visible rows
STEP = 5                                 # % per press on the Volume row
BOOST_MAX = 8
PENDING_S = 2.0                          # a sound value just sent counts until the server's state shows it
LANGS = [("off", "Off"), ("auto", "Auto"), ("eng", "English"), ("hrv", "Croatian")]
DEFAULT_ACCENT = (0.45, 0.68, 1.0)
CARD = (0.055, 0.055, 0.075)
INK, DARK = d.WHITE, (0.07, 0.07, 0.09)
LABEL_SIZE, SUB_SIZE = 33, 27
VALUE_W = 380                            # room for the value / control at the right


def esc(text):
    return d.esc(text)


def clip(text, n=54):
    text = str(text or "")
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def mmss(sec):
    sec = int(sec or 0)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def item_label(it):
    title = it.get("title") or f"#{it.get('id')}"
    return f"{it['artist']} – {title}" if it.get("artist") else title


class Row:
    def __init__(self, label, sub="", act=None, opens=False, closes=False, level=False, kind=None, value=None,
                 vmax=None, key=None):
        self.label, self.sub, self.act, self.opens, self.closes = label, sub, act, opens, closes
        self.level = level                        # a level row (Volume / Boost): Left / Right change it
        self.kind = kind or ("level" if level else "opens" if opens else "action")
        self.value, self.vmax = value, vmax       # level: value 0..vmax; toggle: bool; choice: index
        self.key = key                            # which setting (volume, boost, loudness, lang, lyrics, shuffle)


class Page:
    def __init__(self, title, rows=None, loader=None):
        self.title, self.rows, self.loader = title, rows or [], loader
        self.sel = self.top = 0
        self.loading = loader is not None
        self.error = None


class Menu:
    def __init__(self, fetch, send, state=lambda: None, volume=None, mono=time.monotonic):
        self.fetch, self.send, self.state = fetch, send, state
        self.vol = volume            # the player (volume, lyrics, colours); None in tests of the plain menu
        self.mono = mono
        self.pages = []
        self.library = None          # id -> summary, for queue titles (loaded once per open)
        self.pending = {}            # sound key -> (value, time sent)

    @property
    def open(self):
        return bool(self.pages)

    @property
    def page(self):
        return self.pages[-1] if self.pages else None

    # -- settings shown on the first page
    def sound(self, key):
        """Boost / loudness: the value just sent (for PENDING_S, until the state shows it), else the server's."""
        st = (self.state() or {}).get("sound") or {}
        cur = st.get(key)
        if key in self.pending:
            value, at = self.pending[key]
            if value == cur or self.mono() - at > PENDING_S:
                del self.pending[key]
            else:
                return value
        return cur

    def send_sound(self, key, value):
        self.pending[key] = (value, self.mono())
        self.send({"type": "cmd", "cmd": "sound", key: value})

    def _has(self, name):
        return self.vol is not None and hasattr(self.vol, name)

    # -- pages
    def root(self):
        st = self.state() or {}
        cur = st.get("current") or {}
        playing = bool(st.get("playing"))
        rows = []
        if cur:
            rows.append(Row("Pause" if playing else "Play", "",
                            act={"cmd": "toggle"}))  # stays open (owner): the label follows the state; title in the header
            rows.append(Row("Stop", "screen goes black", act={"cmd": "stop"}, closes=True))
        if self._has("volume_now"):
            label, pct, muted = self.vol.volume_now()
            rows.append(Row(label, "not connected" if pct is None else "muted" if muted else f"{pct} %",
                            act=self.noop, level=True, value=None if pct is None or muted else pct, vmax=100,
                            key="volume"))
        if isinstance(st.get("sound"), dict):
            boost = int(self.sound("boost") or 0)
            rows.append(Row("Boost", f"+{boost} dB" if boost else "off", act=self.noop, level=True, value=boost,
                            vmax=BOOST_MAX, key="boost"))
            raise_ = self.sound("loudness") == "raise"
            rows.append(Row("Loudness", "raise quiet tracks" if raise_ else "off", act=self.noop, kind="toggle",
                            value=raise_, key="loudness"))
        if self._has("lyrics_lang"):
            codes = [c for c, _ in LANGS]
            lang = self.vol.lyrics_lang if self.vol.lyrics_lang in codes else "auto"
            rows.append(Row("Lyrics", dict(LANGS)[lang], act=self.noop, kind="choice", value=codes.index(lang),
                            vmax=len(LANGS) - 1, key="lang"))
            on = bool(self.vol.lyrics_on)
            rows.append(Row("Lyrics screen", "on" if on else "off", act=self.noop, kind="toggle", value=on,
                            key="lyrics"))
        n = len(st.get("queue") or [])
        rows += [
            Row("Queue", f"{n} item{'s' if n != 1 else ''}" if n else "empty", act=self.queue_page, opens=True),
            Row("Playlists", act=self.playlists_page, opens=True),
            Row("For you", act=self.auto_page, opens=True),
            Row("Music videos", act=lambda: self.items_page("Music videos", "/api/collection/videos",
                                                              {"cmd": "play_collection", "collection": "videos"}),
                opens=True),
            Row("Shuffle", "on" if st.get("shuffle") else "off", act={"cmd": "shuffle"}, kind="toggle",
                value=bool(st.get("shuffle")), key="shuffle"),
            Row("Repeat", st.get("repeat_mode") or "all", act={"cmd": "repeat"}),
        ]
        return Page("TV Player", rows)

    async def noop(self):
        """Placeholder act of level / choice / toggle rows (they must be actions so they are selectable)."""

    def queue_page(self):
        async def load(page):
            st = self.state() or {}
            ids, index = st.get("queue") or [], st.get("index", -1)
            if self.library is None:
                data = await self.fetch("/api/library")
                self.library = {it["id"]: it for it in data.get("items", [])}
            page.rows = [Row(("▶ " if n == index else "") + item_label(self.library.get(i, {"id": i})),
                             mmss(self.library.get(i, {}).get("duration")), act={"cmd": "jump", "index": n},
                             closes=True) for n, i in enumerate(ids)]
            if not page.rows:
                page.rows = [Row("The queue is empty")]
            page.sel = max(0, index)
        return Page("Queue", loader=load)

    def playlists_page(self):
        async def load(page):
            data = await self.fetch("/api/playlists")
            page.rows = [Row(clip(p["name"]), f"{p.get('count', 0)}",
                             act=(lambda p=p: self.items_page(
                                 p["name"], f"/api/playlists/{p['id']}",
                                 {"cmd": "play_collection", "collection": "playlist", "playlist": p["id"]},
                                 by_index=True)), opens=True)
                         for p in data.get("playlists", [])] or [Row("No playlists")]
        return Page("Playlists", loader=load)

    def auto_page(self):
        async def load(page):
            data = await self.fetch("/api/auto")
            page.rows = [Row(clip(a["name"]), f"{a.get('count', 0)}",
                             act=(lambda a=a: self.items_page(
                                 a["name"], f"/api/collection/{a['key']}",
                                 {"cmd": "play_collection", "collection": a["key"]})), opens=True)
                         for a in data] or [Row("Nothing here yet")]
        return Page("For you", loader=load)

    def items_page(self, title, path, play, by_index=False):
        """A list: Play all, Shuffle all, then its items (OK plays from that item). by_index: a playlist (may hold an
        item twice: the server wants the position, not the id)."""
        async def load(page):
            data = await self.fetch(path)
            items = data.get("items", [])
            page.rows = [Row("▶  Play all", f"{len(items)}", act=dict(play), closes=True),
                         Row("⤨  Shuffle all", act=dict(play, shuffle=True), closes=True)]
            for n, it in enumerate(items):
                act = dict(play, start_index=n) if by_index else dict(play, start_id=it["id"])
                page.rows.append(Row(item_label(it), mmss(it.get("duration")), act=act, closes=True))
            if not items:
                page.rows = [Row("Empty")]
        return Page(title, loader=load)

    # -- control
    async def show(self):
        self.pages = [self.root()]
        self.library = None

    def close(self):
        self.pages = []

    async def _push(self, page):
        self.pages.append(page)
        if page.loader:
            await self._load(page)

    async def _load(self, page):
        try:
            await page.loader(page)
            page.error = None
        except Exception as e:  # network, server, bad data: shown in the page, never fatal
            page.rows, page.error = [Row("Could not load this list")], repr(e)
        page.loading = False
        self._scroll(page)

    def refresh_root(self):
        """Server state or a setting changed (play/pause, shuffle, repeat, sound, lyrics): the first page follows it."""
        if self.pages and len(self.pages) == 1:
            sel = self.pages[0].sel
            self.pages[0] = self.root()
            self.pages[0].sel = min(sel, len(self.pages[0].rows) - 1)
            self._scroll(self.pages[0])

    def _scroll(self, page):
        page.sel = max(0, min(page.sel, len(page.rows) - 1))
        if page.sel < page.top:
            page.top = page.sel
        elif page.sel >= page.top + ROWS:
            page.top = page.sel - ROWS + 1
        page.top = max(0, min(page.top, max(0, len(page.rows) - ROWS)))

    async def _setting(self, row, name):
        """Left / Right / OK on a level, choice or toggle row. True when the key was used."""
        k = row.key
        if row.kind == "level" and name in ("left", "right", "ok"):
            if k == "volume" and self._has("volume_step"):
                if name == "ok":
                    await self.vol.volume_mute()
                else:
                    await self.vol.volume_step(STEP if name == "right" else -STEP)
            elif k == "boost" and name != "ok":
                new = max(0, min(BOOST_MAX, int(row.value or 0) + (1 if name == "right" else -1)))
                if new != row.value:
                    self.send_sound("boost", new)
            return True
        if row.kind == "choice" and name in ("left", "right"):
            new = max(0, min(row.vmax, row.value + (1 if name == "right" else -1)))   # stops at the ends
            if new != row.value and k == "lang" and self._has("set_lyrics_lang"):
                await self.vol.set_lyrics_lang(LANGS[new][0])
            return True
        if row.kind == "toggle" and name == "ok":
            if k == "loudness":
                self.send_sound("loudness", "off" if row.value else "raise")
            elif k == "lyrics" and self._has("set_lyrics_on"):
                await self.vol.set_lyrics_on(not row.value)
            elif isinstance(row.act, dict):
                self.send(dict(row.act, type="cmd"))
            return True
        return row.kind == "choice" and name == "ok"   # OK on a choice row does nothing (owner's convention)

    async def key(self, name):
        """A remote key while the menu is open. Returns True when the menu changed (redraw)."""
        page = self.page
        if page is None:
            return False
        if name in ("red", "blue", "back"):  # Blue (the Menu function) opens and closes, Back and Red close
            self.close()
            return True
        row = page.rows[page.sel] if page.rows and not page.loading and 0 <= page.sel < len(page.rows) else None
        if row is not None and len(self.pages) == 1 and row.kind in ("level", "choice", "toggle") \
                and await self._setting(row, name):
            self.refresh_root()
            return True
        if name == "left":
            if len(self.pages) > 1:
                self.pages.pop()
                return True
            return False
        if page.loading or not page.rows:
            return False
        step = {"up": -1, "down": 1, "ch_up": -ROWS, "ch_down": ROWS}.get(name)
        if step is not None:
            n = len(page.rows)
            if abs(step) == 1 and n:  # arrows wrap around, page keys stop at the ends
                page.sel = (page.sel + step) % n
            else:
                page.sel += step
            self._scroll(page)
            return True
        if name in ("ok", "right"):
            row = page.rows[page.sel]
            if row.act is None or (name == "right" and not row.opens):
                return False
            if row.opens:
                await self._push(row.act())
            else:
                self.send(dict(row.act, type="cmd"))
                if row.closes:
                    self.close()
            return True
        return False

    # -- drawing
    def accent(self):
        if self._has("accent"):
            try:
                return self.vol.accent() or DEFAULT_ACCENT
            except Exception:
                return DEFAULT_ACCENT
        return DEFAULT_ACCENT

    def tint(self):
        if self._has("backdrop_base"):
            try:
                base = self.vol.backdrop_base()
                if base:
                    return d.mix(CARD, base, 0.45)
            except Exception:
                pass
        return CARD

    def hint(self, row):
        kind = row.kind if row is not None and len(self.pages) == 1 else None
        if kind == "level":
            keys = [("◀ ▶", "adjust")] + ([("OK", "mute")] if row.key == "volume" else [])
        elif kind == "choice":
            keys = [("◀ ▶", "change")]
        elif kind == "toggle":
            keys = [("OK", "switch")]
        else:
            keys = [("OK", "select")]
        if len(self.pages) > 1:
            keys.append(("◀", "back"))
        keys.append(("Menu", "close"))
        return keys

    def render(self):
        page = self.page
        if page is None:
            return ""
        accent, card = self.accent(), self.tint()
        ev = []
        # card with a soft shadow (stacked, no blur: cheap at 4K)
        for g, op in ((18, 0.10), (10, 0.14), (4, 0.20)):
            ev.append(d.rrect(X0 - g, Y0 - g + 8, X1 + g, Y1 + g + 8, 28 + g, d.BLACK, op))
        ev.append(d.rrect(X0, Y0, X1, Y1, 28, card, 0.9))
        # header
        tx = X0 + PAD
        width = X1 - X0 - 2 * PAD
        st = self.state() or {}
        cur = st.get("current") or {}
        if len(self.pages) == 1:
            eyebrow = "TV PLAYER  ·  " + ("NOW PLAYING" if st.get("playing") else "PAUSED" if cur else "NOTHING PLAYING")
            title = (cur.get("title") or "TV Player") if cur else "TV Player"
            subtitle = cur.get("artist") or ""
        else:
            eyebrow = "  ›  ".join(p.title for p in self.pages[:-1]).upper()
            title, subtitle = page.title, ""
        ev.append(d.text(tx, Y0 + 38, d.ellipsize(eyebrow, 20, width, 700, 4), 20, accent, 1, 700, spacing=4))
        count = f"{page.sel + 1} / {len(page.rows)}" if len(page.rows) > ROWS else ""
        cw = d.width(count, 24, 500) + 20 if count else 0
        ev.append(d.text(tx, Y0 + 72, d.ellipsize(title, 44, width - cw, 800), 44, INK, 1, 800))
        if subtitle:
            ev.append(d.text(tx, Y0 + 128, d.ellipsize(subtitle, 27, width, 500), 27, INK, 0.6, 500))
        if count:
            ev.append(d.text(X1 - PAD, Y0 + 86, count, 24, INK, 0.5, 500, an=9))
        ev.append(d.rect(tx, ROW_Y - 22, X1 - PAD, ROW_Y - 20, INK, 0.10))
        if page.loading:
            ev.append(d.text(tx, ROW_Y + 10, "Loading…", LABEL_SIZE, INK, 0.5, 500))
        # rows
        for n, row in enumerate(page.rows[page.top:page.top + ROWS]):
            i = page.top + n
            self._row(ev, row, ROW_Y + n * ROW_H, i == page.sel, accent)
        # scroll bar
        if len(page.rows) > ROWS:
            top, bottom = ROW_Y, ROW_Y + ROWS * ROW_H - 8
            ev.append(d.rrect(X1 - 18, top, X1 - 13, bottom, 2.5, INK, 0.12))
            h = max(40, (bottom - top) * ROWS / len(page.rows))
            y = top + (bottom - top - h) * page.top / max(1, len(page.rows) - ROWS)
            ev.append(d.rrect(X1 - 18, y, X1 - 13, y + h, 2.5, INK, 0.55))
        # key hints
        row = page.rows[page.sel] if page.rows and 0 <= page.sel < len(page.rows) else None
        x, y = tx, Y1 - 50
        for key, what in self.hint(row):
            kw = d.width(key, 19, 700) + 22
            ev.append(d.rrect(x, y, x + kw, y + 32, 8, INK, 0.16))
            ev.append(d.text(x + kw / 2, y + 16, key, 19, INK, 0.9, 700, an=5))
            ev.append(d.text(x + kw + 10, y + 16, what, 22, INK, 0.55, 500, an=4))
            x += kw + 10 + d.width(what, 22, 500) + 28
        return "\n".join(ev)

    def _row(self, ev, row, y, selected, accent):
        x0, x1 = X0 + 20, X1 - 30
        mid = y + (ROW_H - 8) / 2
        if selected:
            ev.append(d.rrect(x0, y, x1, y + ROW_H - 8, 14, (0.96, 0.96, 0.97), 1))
        ink = DARK if selected else INK
        active = row.act is not None
        right = x1 - 22
        # right-hand side: control / value
        sub = row.sub
        if row.kind == "level" and row.vmax:
            bw = 170
            bx = right - bw
            ev.append(d.rrect(bx, mid - 4, right, mid + 4, 4, ink, 0.18))
            if row.value:
                fw = max(8, bw * min(1.0, row.value / row.vmax))
                ev.append(d.rrect(bx, mid - 4, bx + fw, mid + 4, 4, DARK if selected else accent, 0.85 if selected else 1))
            right = bx - 18
        elif row.kind == "toggle":
            if row.key != "loudness":   # the switch says on / off; only Loudness names what "on" does
                sub = ""
            sw, sh = 60, 32
            sx = right - sw
            on = bool(row.value)
            track = (accent if not selected else DARK) if on else ink
            ev.append(d.rrect(sx, mid - sh / 2, right, mid + sh / 2, sh / 2, track, 0.9 if on else 0.22))
            kx = right - sh / 2 if on else sx + sh / 2
            ev.append(d.circle(kx, mid, sh / 2 - 4, d.WHITE, 1))
            right = sx - 18
        elif row.kind == "choice":
            lo, hi = row.value > 0, row.value < (row.vmax or 0)
            vw = d.width(sub, SUB_SIZE, 600)
            ev.append(d.text(right, mid, "›", 34, ink, 0.8 if hi else 0.2, 600, an=6))
            ev.append(d.text(right - 30, mid, sub, SUB_SIZE, ink, 0.9, 600, an=6))
            ev.append(d.text(right - 30 - vw - 14, mid, "‹", 34, ink, 0.8 if lo else 0.2, 600, an=6))
            sub = ""
            right -= 30 + vw + 40
        elif row.kind == "opens":
            ev.append(d.text(right, mid, "›", 36, ink, 0.6, 600, an=6))
            right -= 30
        label_room = right - (x0 + 24)
        if sub:
            sub_w = min(d.width(sub, SUB_SIZE, 500), max(90, label_room * (0.25 if d.width(row.label, LABEL_SIZE, 500)
                                                                               > label_room * 0.6 else 0.6)))
            text_sub = d.ellipsize(sub, SUB_SIZE, sub_w, 500)
            ev.append(d.text(right, mid, text_sub, SUB_SIZE, ink, 0.6 if not selected else 0.7, 500, an=6))
            label_room -= d.width(text_sub, SUB_SIZE, 500) + 24
        weight = 600 if selected else 500
        label = d.ellipsize(row.label, LABEL_SIZE, max(60, label_room), weight)
        ev.append(d.text(x0 + 24, mid, label, LABEL_SIZE, ink, (1 if selected else 0.92) if active else 0.45, weight, an=4))
