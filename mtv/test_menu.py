#!/usr/bin/env python3
"""menu.py without mpv or a server: root rows follow the state, navigation (wrap, page, scroll window), opening lists
from a fake API, the commands sent for every kind of row, back / close, load errors, ASS escaping and rendering.
    python3 test_menu.py
"""
import asyncio
import os
import re
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
    check(labels == ["Shuffle", "Repeat", "Queue", "Playlists", "For you", "Music videos"],
          f"root rows: settings first, the four lists last, no Play / Pause or Stop ({labels})")
    check(m.page.rows[2].sub == "3 items" and [r.sep for r in m.page.rows] == [False, False, True, False, False, False],
          "root subs: queue size; the divider is above Queue only")
    drawn = m.render()
    check("}Song 1" in drawn and "}Artist 1" in drawn and "NOW PLAYING" in drawn, "header: now playing, title, artist")

    # ---- wrap, shuffle / repeat stay open, root follows state
    await m.key("up")
    check(m.page.sel == 5, "Up on the first row wraps to the last")
    await m.key("down")
    m.page.sel = 1
    await m.key("ok")
    check(sent[-1] == {"cmd": "repeat", "type": "cmd"} and m.open, "Repeat row: command, menu stays open")
    state["repeat_mode"] = "one"
    m.refresh_root()
    check(m.page.rows[1].sub == "one" and m.page.sel == 1, "state change refreshes the root, selection kept")
    m.page.sel = 0
    await m.key("ok")
    check(sent[-1] == {"cmd": "shuffle", "type": "cmd"} and m.open, "Shuffle row: command, menu stays open")
    await m.key("up")
    check(m.page.sel == 5, "Up on the first row wraps to the last (again)")
    await m.key("down")
    check(m.page.sel == 0, "Down on the last row wraps to the first")
    check(await m.key("right") is False and m.open, "Right on a toggle row does nothing")
    check(await m.key("left") is False and m.open, "Left on the root does nothing")

    # ---- queue: titles from the library, current marked and selected, jump closes
    m.page.sel = 2
    await m.key("right")
    check(m.page.title == "Queue" and calls[-1] == "/api/library", "Right opens Queue (library fetched)")
    check([r.label for r in m.page.rows] == ["Artist 0 – Song 0", "▶ Artist 1 – Song 1", "Artist 2 – Song 2"]
          and m.page.sel == 1, "queue rows, current marked + selected")
    await m.key("down")
    await m.key("ok")
    check(sent[-1] == {"cmd": "jump", "index": 2, "type": "cmd"} and not m.open, "queue OK -> jump 2, closed")

    # ---- playlists -> items: play all, shuffle, by index (a playlist may list an item twice)
    await m.show()
    m.page.sel = 3
    await m.key("ok")
    check(m.page.title == "Playlists" and [r.label for r in m.page.rows] == ["Fav {Spotovi}", "Talent"], "playlists")
    await m.key("ok")
    check(m.page.title == "Fav {Spotovi}" and [r.label for r in m.page.rows][:1] == ["▶  Shuffle all"]
          and "Play all" not in "".join(r.label for r in m.page.rows) and len(m.page.rows) == 4,
          "playlist page: only 'Shuffle all' (play icon), then the 3 items")
    check(m.page.rows[0].sub == "3" and m.page.rows[2].sub == "3:21", "Shuffle all shows the count; item duration m:ss")
    await m.key("ok")
    check(sent[-1] == {"cmd": "play_collection", "collection": "playlist", "playlist": 5, "shuffle": True, "type": "cmd"},
          "Shuffle all -> play_collection shuffle")
    await m.show()
    m.page.sel = 3
    await m.key("ok")
    await m.key("down")
    await m.key("ok")  # Talent: 30 items
    m.page.sel = 2     # second item (index 1)
    await m.key("ok")
    check(sent[-1] == {"cmd": "play_collection", "collection": "playlist", "playlist": 4, "start_index": 1, "type": "cmd"},
          "playlist item -> play_collection with start_index")

    # ---- paging + scroll window on a long list
    await m.show()
    m.page.sel = 3
    await m.key("ok")
    await m.key("down")
    await m.key("ok")
    page = m.page
    await m.key("ch_down")
    check(page.sel == Mn.ROWS and page.top == 1, f"CH- pages down one screen, window follows (sel {page.sel}, top {page.top})")
    await m.key("ch_down")
    await m.key("ch_down")
    check(page.sel == 30 and page.top == 31 - Mn.ROWS, "paging stops at the last row")
    await m.key("ch_up")
    check(page.sel == 30 - Mn.ROWS and page.top == 30 - Mn.ROWS, "CH+ pages up")
    ass = m.render()
    check(f"{31 - Mn.ROWS} / 31" in ass and "TV PLAYER  ›  PLAYLISTS" in ass and "}Talent" in ass,
          "render: position counter, breadcrumb (eyebrow), page title")
    pill = Mn.d.bgr((0.96, 0.96, 0.97))
    check(ass.count(pill) == 1, "render: exactly one selection pill")
    check(ass.count("\\p1") >= 6, "render: card, shadow, pill, scroll bar, key chips are shapes")

    # ---- back / close
    await m.key("left")
    check(m.page.title == "Playlists", "Left goes back one page")
    await m.key("red")
    check(not m.open, "Red closes from any depth")
    await m.show()
    m.page.sel = 3
    await m.key("ok")
    await m.key("blue")
    check(not m.open, "Blue closes from any depth")
    await m.show()
    m.page.sel = 3
    await m.key("ok")
    await m.key("ok")
    check(len(m.pages) == 3 and m.page.title == "Fav {Spotovi}", "two pages deep")
    depth = len(m.pages)
    await m.key("back")
    check(m.open and len(m.pages) == depth - 1 and m.page.title == "Playlists", "Back goes up ONE page (owner), not out of the menu")
    await m.key("back")
    check(m.open and len(m.pages) == 1 and m.page.title == "TV Player", "Back again: the first page")
    await m.key("back")
    check(not m.open, "Back on the first page closes the menu")
    await m.show()
    m.page.sel = 3
    await m.key("ok")
    check(m.page.title == "Playlists" and m.page.loading is False, "(setup) a list is open")
    m.pages[-1].loading = True
    await m.key("back")
    check(len(m.pages) == 1, "Back works on a page that is still loading")
    await m.show()
    check("}Menu" in m.render() and "}close" in m.render(), "the key hints name Menu: close")

    # ---- For you: start_id; empty list; music videos
    await m.show()
    m.page.sel = 4
    await m.key("ok")
    check([r.label for r in m.page.rows] == ["Loved", "Recently added"], "For you lists")
    await m.key("ok")
    m.page.sel = 2
    await m.key("ok")
    check(sent[-1] == {"cmd": "play_collection", "collection": "loved", "start_id": 301, "type": "cmd"},
          "auto list item -> start_id")
    await m.show()
    m.page.sel = 4
    await m.key("ok")
    await m.key("down")
    await m.key("ok")
    check([r.label for r in m.page.rows] == ["Empty"] and await m.key("ok") is False, "empty list: one inert row")
    await m.show()
    m.page.sel = 5
    await m.key("ok")
    check(m.page.title == "Music videos" and m.page.rows[0].label == "▶  Shuffle all" and len(m.page.rows) == 6,
          "Music videos: Shuffle all + 5 videos")
    await m.key("ok")
    check(sent[-1] == {"cmd": "play_collection", "collection": "videos", "shuffle": True, "type": "cmd"} and not m.open,
          "Music videos -> Shuffle all (the only play-all), menu closes")

    # ---- load error shown, not raised
    await m.show()
    await m._push(m.items_page("Broken", "/api/broken", {"cmd": "play_collection"}))
    check(m.page.rows[0].label == "Could not load this list" and "boom" in m.page.error, "load error -> a row")

    # ---- nothing playing: the same rows, the queue says empty
    state.update(current=None, queue=[])
    await m.show()
    check([r.label for r in m.page.rows][:2] == ["Shuffle", "Repeat"] and m.page.rows[2].label == "Queue"
          and m.page.rows[2].sub == "empty", "nothing loaded: same rows, Queue is empty")

    # ---- Equalizer: a short picker (Off, Momentum 4, Pop, Rock, Full bass, Classical), the rest is in the web remote
    presets = [["off", "Off"], ["a:momentum4", "Momentum 4"], ["a:amplify", "Amplify"], ["p:0", "Flat"], ["p:1", "Classical"],
               ["p:4", "Full bass"], ["p:11", "Pop"], ["p:13", "Rock"], ["p:17", "Techno"], ["custom", "Custom"]]
    eq_state = dict(state, sound={"preset": "off", "boost": 0, "loudness": "raise"}, sound_presets=presets)
    eqm = Mn.Menu(fetch, sent.append, lambda: eq_state)
    await eqm.show()
    labels = [r.label for r in eqm.page.rows]
    check(labels == ["Boost", "Loudness", "Equalizer", "Shuffle", "Repeat", "Queue", "Playlists", "For you", "Music videos"],
          f"Equalizer sits after Loudness, before Shuffle ({labels})")
    eq = eqm.page.rows[2]
    check(eq.kind == "choice" and [o[1] for o in eq.options] == ["Off", "Momentum 4", "Pop", "Rock", "Full bass", "Classical"]
          and eq.sub == "Off" and eq.value == 0, "picker: Off + 5 presets (no Flat / Amplify / Techno / Custom)")
    eqm.page.sel = 2
    n0 = len(sent)
    await eqm.key("left")
    await eqm.key("ok")
    check(len(sent) == n0 and eqm.open, "Left at the first value and OK do nothing")
    await eqm.key("right")
    check(sent[-1] == {"cmd": "sound", "preset": "a:momentum4", "type": "cmd"} and eqm.page.rows[2].sub == "Momentum 4",
          "Right -> Momentum 4 sent, the row shows it at once")
    await eqm.key("right")
    check(sent[-1]["preset"] == "p:11" and eqm.page.rows[2].sub == "Pop", "a second quick Right steps from the pending value (Pop)")
    for _ in range(5):
        await eqm.key("right")
    n1 = len(sent)
    await eqm.key("right")
    check(len(sent) == n1 and eqm.page.rows[2].sub == "Classical", "stops at the last value (Classical)")
    await eqm.key("left")
    check(sent[-1]["preset"] == "p:4" and eqm.page.rows[2].sub == "Full bass", "Left steps back")
    check(("◀ ▶" in "".join(k for k, _ in eqm.hint(eqm.page.rows[2]))) and "}change" in eqm.render(), "hint: ◀ ▶ change")
    eq_state["sound"] = {"preset": "custom", "boost": 0, "loudness": "raise"}   # set in the web remote
    eqm.pending.clear()
    eqm.refresh_root()
    eq = eqm.page.rows[2]
    check(eq.sub == "Custom" and eq.value == 0 and len(eq.options) == 7 and eq.options[1][1] == "Off",
          "a preset outside the list (Custom) is shown first and can be left")
    await eqm.key("right")
    check(sent[-1]["preset"] == "off", "Right from Custom -> Off")
    eq_state["sound"] = {"preset": "off", "boost": 0, "loudness": "raise"}
    eqm.pending.clear()
    eqm.refresh_root()
    check(len(eqm.page.rows[2].options) == 6, "after leaving Custom the list is the short one again")
    eq_state.pop("sound_presets")
    eqm.refresh_root()
    check("Equalizer" not in [r.label for r in eqm.page.rows], "no preset list from the server: no Equalizer row")

    # ---- the divider between settings and lists: one extra shape, the lists move down by SEP_GAP
    await m.show()
    with_sep = m.render()
    m.page.rows[2].sep = False
    without = m.render()
    check(with_sep.count("\\p1") == without.count("\\p1") + 1, "divider: exactly one more shape than without it")
    ys = lambda ass, word: [float(x) for x in re.findall(r"\\pos\([\d.]+,([\d.]+)\)[^}]*\}" + word, ass)]
    check(ys(with_sep, "Queue")[0] - ys(without, "Queue")[0] == Mn.SEP_GAP and ys(with_sep, "Repeat") == ys(without, "Repeat"),
          f"divider: Queue and the rows below move down {Mn.SEP_GAP} px, the settings stay")
    check(ys(with_sep, "Music videos")[0] + 24 < 1024 - 50, "divider: the last row still ends above the key hints")

    # ---- escaping
    check(Mn.esc("a{\\b}c\nd") == "a\\{\\\u200bb\\}c d", "ASS escaping of braces, backslash, newline")
    check(Mn.clip("x" * 70).endswith("…") and len(Mn.clip("x" * 70)) == 54, "long playlist names clipped")
    long = Mn.Menu(fetch, sent.append, lambda: state)
    long.pages = [Mn.Page("T", [Mn.Row("y" * 80, "12:34", act={"cmd": "x"}), Mn.Row("z" * 80, act={"cmd": "x"}),
                               Mn.Row("Play", "x" * 90, act={"cmd": "x"})])]
    drawn = long.render()
    run = lambda ch: max((len(m_) for m_ in re.findall(ch + "+", drawn)), default=0)
    check("y" * 10 in drawn and "…" in drawn and run("y") < run("z") < 80,
          f"labels are cut to the row width, shorter next to a duration (y {run('y')} < z {run('z')})")
    check("}12:34" in drawn and run("x") < 90 and "}Play" in drawn, "a duration stays whole; a long value gives way, the label stays")
    check("{Spotovi}" not in Mn.Menu(fetch, sent.append, lambda: state).render(), "closed menu renders nothing")
    m2 = Mn.Menu(fetch, sent.append, lambda: state)
    await m2.show()
    await m2._push(m2.playlists_page())
    check("Fav \\{Spotovi\\}" in m2.render(), "titles with braces are escaped in the drawing")


if __name__ == "__main__":
    asyncio.run(main())
    print("ALL PASSED" if not failed else f"FAILED {failed}")
    sys.exit(1 if failed else 0)
