#!/usr/bin/env python3
"""menu.py without mpv or a server: root rows follow the state, navigation (wrap, page, scroll window), opening lists
from a fake API, the commands sent for every kind of row, back / close, load errors, ASS escaping and rendering.
    python3 test_menu.py
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import menu as Mn  # noqa: E402

failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def items(n, start=100, kind="video"):
    return [{"id": start + i, "kind": kind, "title": f"Song {i}", "artist": f"Artist {i}", "duration": 200 + i}
            for i in range(n)]


API = {
    "/api/playlists": {"playlists": [{"id": 5, "name": "Fav {Spotovi}", "count": 3}, {"id": 4, "name": "Talent", "count": 30}]},
    "/api/playlists/5": {"items": items(3)},
    "/api/playlists/4": {"items": items(30, 200)},
    "/api/auto": [{"key": "loved", "name": "Loved", "count": 2}, {"key": "recent", "name": "Recently added", "count": 0}],
    "/api/collection/loved": {"items": items(2, 300)},
    "/api/collection/recent": {"items": []},
    "/api/collection/videos": {"items": items(5, 400)},
    "/api/library": {"items": items(30, 200) + items(3)},
}


async def main():
    calls, sent = [], []
    state = {"current": {"id": 101, "title": "Song 1", "artist": "Artist 1"}, "playing": True, "queue": [100, 101, 102],
             "index": 1, "shuffle": False, "repeat_mode": "all"}

    async def fetch(path):
        calls.append(path)
        if path == "/api/broken":
            raise OSError("boom")
        return API[path]

    m = Mn.Menu(fetch, sent.append, lambda: state)
    check(not m.open and m.render() == "", "closed: nothing drawn")
    await m.show()
    labels = [r.label for r in m.page.rows]
    check(labels == ["Pause", "Queue", "Playlists", "For you", "Music videos", "Shuffle", "Repeat"],
          f"root rows ({labels})")
    check(m.page.rows[0].sub == "Artist 1 – Song 1" and m.page.rows[1].sub == "3 items", "root subs: current, queue size")

    # ---- Play/Pause row: toggle and close
    await m.key("ok")
    check(sent[-1] == {"cmd": "toggle", "type": "cmd"} and m.open, "OK on Pause -> toggle, menu stays open")
    state["playing"] = False
    m.refresh_root()
    check(m.page.rows[0].label == "Play" and m.page.sel == 0, "paused state -> 'Play', still selected")

    # ---- wrap, shuffle / repeat stay open, root follows state
    await m.key("up")
    check(m.page.sel == 6, "Up on the first row wraps to the last")
    await m.key("ok")
    check(sent[-1] == {"cmd": "repeat", "type": "cmd"} and m.open, "Repeat row: command, menu stays open")
    state["repeat_mode"] = "one"
    m.refresh_root()
    check(m.page.rows[6].sub == "one" and m.page.sel == 6, "state change refreshes the root, selection kept")
    await m.key("down")
    check(m.page.sel == 0, "Down on the last row wraps to the first")
    check(await m.key("right") is False and m.open, "Right on an action row does nothing")
    check(await m.key("left") is False and m.open, "Left on the root does nothing")

    # ---- queue: titles from the library, current marked and selected, jump closes
    m.page.sel = 1
    await m.key("right")
    check(m.page.title == "Queue" and calls[-1] == "/api/library", "Right opens Queue (library fetched)")
    check([r.label for r in m.page.rows] == ["Artist 0 – Song 0", "▶ Artist 1 – Song 1", "Artist 2 – Song 2"]
          and m.page.sel == 1, "queue rows, current marked + selected")
    await m.key("down")
    await m.key("ok")
    check(sent[-1] == {"cmd": "jump", "index": 2, "type": "cmd"} and not m.open, "queue OK -> jump 2, closed")

    # ---- playlists -> items: play all, shuffle, by index (a playlist may list an item twice)
    await m.show()
    m.page.sel = 2
    await m.key("ok")
    check(m.page.title == "Playlists" and [r.label for r in m.page.rows] == ["Fav {Spotovi}", "Talent"], "playlists")
    await m.key("ok")
    check(m.page.title == "Fav {Spotovi}" and [r.label for r in m.page.rows][:2] == ["▶  Play all", "⤨  Shuffle all"]
          and len(m.page.rows) == 5, "playlist page: Play all, Shuffle all, 3 items")
    check(m.page.rows[3].sub == "3:21", "item duration m:ss")
    await m.key("down")
    await m.key("ok")
    check(sent[-1] == {"cmd": "play_collection", "collection": "playlist", "playlist": 5, "shuffle": True, "type": "cmd"},
          "Shuffle all -> play_collection shuffle")
    await m.show()
    m.page.sel = 2
    await m.key("ok")
    await m.key("down")
    await m.key("ok")  # Talent: 30 items
    m.page.sel = 3     # second item (index 1)
    await m.key("ok")
    check(sent[-1] == {"cmd": "play_collection", "collection": "playlist", "playlist": 4, "start_index": 1, "type": "cmd"},
          "playlist item -> play_collection with start_index")

    # ---- paging + scroll window on a long list
    await m.show()
    m.page.sel = 2
    await m.key("ok")
    await m.key("down")
    await m.key("ok")
    page = m.page
    await m.key("ch_down")
    check(page.sel == 12 and page.top == 1, f"CH- pages down 12 rows, window follows (sel {page.sel}, top {page.top})")
    await m.key("ch_down")
    await m.key("ch_down")
    check(page.sel == 31 and page.top == 32 - Mn.ROWS, "paging stops at the last row")
    await m.key("ch_up")
    check(page.sel == 19 and page.top == 19, "CH+ pages up")
    ass = m.render()
    check(f"20 / 32" in ass and "Playlists › TV Player" not in ass and "TV Player › Playlists" in ass,
          "render: position counter, breadcrumb")
    check(ass.count("\\p1") == 2, "render: panel + one selection box")

    # ---- back / close
    await m.key("left")
    check(m.page.title == "Playlists", "Left goes back one page")
    await m.key("red")
    check(not m.open, "Red closes from any depth")
    await m.show()
    m.page.sel = 2
    await m.key("ok")
    await m.key("blue")
    check(not m.open, "Blue closes from any depth")

    # ---- For you: start_id; empty list; music videos
    await m.show()
    m.page.sel = 3
    await m.key("ok")
    check([r.label for r in m.page.rows] == ["Loved", "Recently added"], "For you lists")
    await m.key("ok")
    m.page.sel = 3
    await m.key("ok")
    check(sent[-1] == {"cmd": "play_collection", "collection": "loved", "start_id": 301, "type": "cmd"},
          "auto list item -> start_id")
    await m.show()
    m.page.sel = 3
    await m.key("ok")
    await m.key("down")
    await m.key("ok")
    check([r.label for r in m.page.rows] == ["Empty"] and await m.key("ok") is False, "empty list: one inert row")
    await m.show()
    m.page.sel = 4
    await m.key("ok")
    await m.key("ok")
    check(sent[-1] == {"cmd": "play_collection", "collection": "videos", "type": "cmd"}, "Music videos -> Play all")

    # ---- load error shown, not raised
    await m.show()
    await m._push(m.items_page("Broken", "/api/broken", {"cmd": "play_collection"}))
    check(m.page.rows[0].label == "Could not load this list" and "boom" in m.page.error, "load error -> a row")

    # ---- nothing playing: no Play/Pause row
    state.update(current=None, queue=[])
    await m.show()
    check(m.page.rows[0].label == "Queue" and m.page.rows[0].sub == "empty", "nothing loaded: starts with Queue")

    # ---- escaping
    check(Mn.esc("a{\\b}c\nd") == "a\\{\\\u200bb\\}c d", "ASS escaping of braces, backslash, newline")
    check(Mn.clip("x" * 70).endswith("…") and len(Mn.clip("x" * 70)) == Mn.LABEL_MAX, "long labels clipped")
    long = Mn.Menu(fetch, sent.append, lambda: state)
    long.pages = [Mn.Page("T", [Mn.Row("y" * 80, "12:34", act={"cmd": "x"}), Mn.Row("z" * 80, act={"cmd": "x"})])]
    drawn = long.render()
    check(Mn.fit("Play", "Dua Lipa – Training Season (Royal Albert Hall live 2026 version)")[0] == "Play"
          and Mn.fit("Play", "x" * 70)[1].endswith("…"), "short label + long sub: label whole, sub clipped")
    lab, sub = Mn.fit("Queue", "31 items")
    check((lab, sub) == ("Queue", "31 items"), "both fit: unchanged")
    check("y" * (Mn.LABEL_MAX - 10) in drawn and "y" * (Mn.LABEL_MAX - 9) not in drawn and "z" * (Mn.LABEL_MAX - 1) in drawn,
          "a label next to a duration is clipped shorter than one without")
    check("{Spotovi}" not in Mn.Menu(fetch, sent.append, lambda: state).render(), "closed menu renders nothing")
    m2 = Mn.Menu(fetch, sent.append, lambda: state)
    await m2.show()
    await m2._push(m2.playlists_page())
    check("Fav \\{Spotovi\\}" in m2.render(), "titles with braces are escaped in the drawing")


if __name__ == "__main__":
    asyncio.run(main())
    print("ALL PASSED" if not failed else f"FAILED {failed}")
    sys.exit(1 if failed else 0)
