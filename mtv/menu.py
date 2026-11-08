"""On-screen menu of mtv-player, drawn with mpv's osd-overlay (ASS) over the video.

Pure model + renderer: Menu holds a stack of pages, key() moves / selects, render() returns ASS events. Lists come
from the server's HTTP API through an async `fetch(path)`; commands go back through `send(msg)` (the player's
WebSocket, the server owns the queue). Keys: Up / Down move (CH+ / CH- page), OK / Right select or open, Left back
one page, Blue or Red closes.
"""
RES_X, RES_Y = 1920, 1080
X0, Y0, X1, Y1 = 80, 70, 1160, 1010      # panel
ROW_Y, ROW_H, ROWS = 200, 62, 12         # first row, row height, visible rows
LABEL_MAX = 54
SUB_CHARS = 1.25                         # label characters given up per character of the right-hand text

BG = "&H101010&"
BG_ALPHA = "&H30&"
SEL = "&HC07038&"                        # BGR: a calm blue
WHITE, GREY, DIM = "&HFFFFFF&", "&HB4B4B4&", "&H808080&"


def esc(text):
    """Text for an ASS event: no override blocks, no escapes, one line."""
    s = str(text or "").replace("\\", "\\\u200b").replace("{", "\\{").replace("}", "\\}")
    return s.replace("\r", " ").replace("\n", " ")


def clip(text, n=LABEL_MAX):
    text = str(text or "")
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def mmss(sec):
    sec = int(sec or 0)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def item_label(it):
    title = it.get("title") or f"#{it.get('id')}"
    return f"{it['artist']} – {title}" if it.get("artist") else title


def fit(label, sub):
    """Label and right-hand text sharing one row (36 px label font, 28 px sub font). A short sub (duration, count)
    stays whole and the label gives way; a long sub (the current title next to "Play") gives way to the label."""
    if not sub:
        return clip(label), ""
    need = len(label) + int(len(sub) * SUB_CHARS) + 3
    if need <= LABEL_MAX:
        return label, sub
    if len(sub) <= 8:
        return clip(label, LABEL_MAX - int(len(sub) * SUB_CHARS) - 3), sub
    room = max(8, int((LABEL_MAX - min(len(label), LABEL_MAX // 2) - 3) / SUB_CHARS))
    return clip(label, LABEL_MAX - int(room * SUB_CHARS) - 3), clip(sub, room)


class Row:
    def __init__(self, label, sub="", act=None, opens=False, closes=False, local=None):
        self.label, self.sub, self.act, self.opens, self.closes = label, sub, act, opens, closes
        self.local = local   # async callable run on this box (not sent to the server); the menu closes first


class Page:
    def __init__(self, title, rows=None, loader=None):
        self.title, self.rows, self.loader = title, rows or [], loader
        self.sel = self.top = 0
        self.loading = loader is not None
        self.error = None


class Menu:
    def __init__(self, fetch, send, state=lambda: None, learn=None):
        self.fetch, self.send, self.state = fetch, send, state
        self.learn = learn           # async callable: "Learn remote" row (receiver that can learn buttons), or None
        self.pages = []
        self.library = None          # id -> summary, for queue titles (loaded once per open)

    @property
    def open(self):
        return bool(self.pages)

    @property
    def page(self):
        return self.pages[-1] if self.pages else None

    # -- pages
    def root(self):
        st = self.state() or {}
        cur = st.get("current") or {}
        playing = bool(st.get("playing"))
        rows = []
        if cur:
            rows.append(Row("Pause" if playing else "Play", item_label(cur),
                            act={"cmd": "toggle"}))  # stays open (Mihovil): the label follows the state
        n = len(st.get("queue") or [])
        rows += [
            Row("Queue", f"{n} item{'s' if n != 1 else ''}" if n else "empty", act=self.queue_page, opens=True),
            Row("Playlists", act=self.playlists_page, opens=True),
            Row("For you", act=self.auto_page, opens=True),
            Row("Music videos", act=lambda: self.items_page("Music videos", "/api/collection/videos",
                                                              {"cmd": "play_collection", "collection": "videos"}),
                opens=True),
            Row("Shuffle", "on" if st.get("shuffle") else "off", act={"cmd": "shuffle"}),
            Row("Repeat", st.get("repeat_mode") or "all", act={"cmd": "repeat"}),
        ]
        if self.learn:
            rows.append(Row("Learn remote", "teach the receiver", local=self.learn))
        return Page("TV Player", rows)

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
        """Server state changed (play/pause, shuffle, repeat): the root page's labels follow it."""
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

    async def key(self, name):
        """A remote key while the menu is open. Returns True when the menu changed (redraw)."""
        page = self.page
        if page is None:
            return False
        if name in ("red", "blue"):  # Blue opens and closes, Red closes
            self.close()
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
            if row.local and name == "ok":
                self.close()
                await row.local()
                return True
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
    def render(self):
        page = self.page
        if page is None:
            return ""
        ev = []

        def box(x0, y0, x1, y1, color, alpha):
            ev.append(f"{{\\an7\\pos(0,0)\\bord0\\shad0\\1c{color}\\1a{alpha}\\p1}}"
                      f"m {x0} {y0} l {x1} {y0} {x1} {y1} {x0} {y1}{{\\p0}}")

        def text(x, y, s, size, color=WHITE, an=7, bold=False):
            ev.append(f"{{\\an{an}\\pos({x},{y})\\fs{size}\\bord0\\shad0\\1c{color}\\b{1 if bold else 0}}}{esc(s)}")

        box(X0, Y0, X1, Y1, BG, BG_ALPHA)
        crumbs = " › ".join(p.title for p in self.pages[:-1])
        if crumbs:
            text(X0 + 40, Y0 + 22, clip(crumbs, 60), 26, GREY)
        text(X0 + 40, Y0 + 56, clip(page.title, 34), 46, WHITE, bold=True)
        if len(page.rows) > ROWS:
            text(X1 - 40, Y0 + 66, f"{page.sel + 1} / {len(page.rows)}", 28, GREY, an=9)
        if page.loading:
            text(X0 + 40, ROW_Y + 10, "Loading…", 36, GREY)
        for n, row in enumerate(page.rows[page.top:page.top + ROWS]):
            i = page.top + n
            y = ROW_Y + n * ROW_H
            if i == page.sel:
                box(X0 + 16, y - 4, X1 - 16, y + ROW_H - 10, SEL, "&H00&")
            label, sub = fit(row.label + ("   ›" if row.opens else ""), row.sub)
            text(X0 + 40, y + 6, label, 36, WHITE if row.act else GREY)
            if sub:
                text(X1 - 40, y + 10, sub, 28, GREY if i != page.sel else WHITE, an=9)
        hint = "OK select   ◀ back   Blue / Red close" if len(self.pages) > 1 else "OK select   Blue / Red close"
        text(X0 + 40, Y1 - 46, hint, 26, DIM)
        return "\n".join(ev)
