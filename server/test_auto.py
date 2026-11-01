#!/usr/bin/env python3
"""Auto-playlists and hearts: listening.db (plays kept for good, loved), the import of history.json, every list
(loved, most_played, recent, forgotten, on_this_day: counts and order), /api/auto, the love command and its
broadcasts, play_collection of an auto list, and a restart keeping the data. Throwaway temp library, data dir and
port only.
    python3 test_auto.py   (needs ffmpeg + the server's deps)
"""
import asyncio
import datetime
import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
PORT = 18091
BASE = f"http://127.0.0.1:{PORT}"
DAY = 86400
failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def song(path, seconds, title, freq=440):
    path.parent.mkdir(parents=True, exist_ok=True)
    ff("-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
       "-metadata", f"title={title}", "-metadata", "artist=Test", str(path))


def video(path, seconds):
    path.parent.mkdir(parents=True, exist_ok=True)
    ff("-f", "lavfi", "-i", f"testsrc=size=160x90:rate=25:duration={seconds}",
       "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}", "-c:v", "libx264", "-c:a", "aac", str(path))


def at_local(year, month, day, hour=12):
    return time.mktime(datetime.datetime(year, month, day, hour).timetuple())


def unit(tmp):
    os.environ.update(ATV_ROOTS=json.dumps({"music": str(tmp / "none")}), ATV_DATA=str(tmp / "udata"),
                      ATV_CACHE=str(tmp / "ucache"))
    srv = importlib.import_module("server")
    (tmp / "udata").mkdir(parents=True, exist_ok=True)
    now = time.time()
    first = now - 400 * DAY  # the first scan: items 1-4 within a few minutes, file times older
    lib = {
        1: dict(added=first, mtime=first - 900 * DAY),
        2: dict(added=first + 120, mtime=first - 10 * DAY),   # newest file time of the first scan
        3: dict(added=first + 300, mtime=first - 500 * DAY),
        4: dict(added=first + 500, mtime=first - 20 * DAY),
        5: dict(added=now - 30 * DAY, mtime=first - 1000 * DAY),  # added later: its "added" counts, not the old file time
        6: dict(added=now - 2 * DAY, mtime=now - 2 * DAY),
        7: dict(added=now - 1 * DAY, mtime=now - 1 * DAY, hidden=1),  # hidden duplicate
    }
    srv.LIB.clear()
    for i, d in lib.items():
        srv.LIB[i] = {"id": i, "kind": "audio", "title": f"T{i}", "artist": "A", "root": "music", "folder": "",
                      "hidden": d.get("hidden", 0), "has_art": 1 if i != 1 else 0, "added": d["added"],
                      "mtime": d["mtime"], "duration": 10}
    added = srv.added_time(list(srv.LIB.values()))
    check(added[1] == lib[1]["mtime"] and added[4] == lib[4]["mtime"], "first-scan items use their file time")
    check(added[5] == lib[5]["added"] and added[6] == lib[6]["added"], "later items use their added time")
    recent = [it["id"] for it in srv.auto_items("recent", now)]
    check(recent == [6, 5, 2, 4, 3, 1], f"recent: newest first, first scan by file time, hidden left out: {recent}")
    srv.LIB[3]["mtime"] = now - 3600  # an old first-scan file retagged today
    recent = [it["id"] for it in srv.auto_items("recent", now)]
    check(recent == [6, 5, 3, 2, 4, 1], f"recent: a retagged first-scan file is not newer than its scan: {recent}")
    srv.LIB[3]["mtime"] = lib[3]["mtime"]

    # history.json import (first start only), then counts / last play
    srv.HISTORY_FILE.write_text(json.dumps({"played": {"1": now - 5 * DAY, "2": now - 100 * DAY, "99": now - DAY}}))
    srv.load_listening()
    check(srv.PLAY_COUNT == {1: 1, 2: 1, 99: 1}, f"history.json imported as plays: {srv.PLAY_COUNT}")
    srv.load_listening()
    check(srv.PLAY_COUNT == {1: 1, 2: 1, 99: 1}, "imported once only")
    for i, t in ((3, now - 3 * DAY), (3, now - 2 * DAY), (4, now - 4 * DAY), (4, now - 1 * DAY), (6, now - 10 * DAY),
                 (6, now - 9 * DAY)):
        srv.note_play(i, t)
    most = [it["id"] for it in srv.auto_items("most_played", now)]
    check(most == [4, 3, 6, 1, 2], f"most played: by count, ties by last play, unknown ids left out: {most}")
    srv.load_listening()
    check(srv.PLAY_COUNT[4] == 2 and srv.LAST_PLAY[4] == now - DAY, "counts and last play read back from the DB")

    forgotten = [it["id"] for it in srv.auto_items("forgotten", now)]
    check(sorted(forgotten) == [2, 5], f"forgotten: never played or last play > 90 days ago, visible only: {forgotten}")
    check(forgotten == [it["id"] for it in srv.auto_items("forgotten", now + 60)], "forgotten: same order all day")
    big = {i: {"id": i, "kind": "audio", "title": f"B{i}", "root": "music", "folder": "", "hidden": 0, "has_art": 0,
               "added": first, "mtime": first} for i in range(100, 400)}
    srv.LIB.update(big)
    f1 = [it["id"] for it in srv.auto_items("forgotten", now)]
    f2 = [it["id"] for it in srv.auto_items("forgotten", now + DAY)]
    check(len(f1) == 100 and f1 != f2, "forgotten: 100 items, another order the next day")
    for i in big:
        del srv.LIB[i]

    # hearts: newest first, toggled off, kept in the DB
    check(srv.set_loved(3, True) and srv.set_loved(1, True), "hearts set")
    check(not srv.set_loved(3, True), "heart set twice: no change")
    srv.LOVED[1] += 1  # 1 is the newer heart
    loved = [it["id"] for it in srv.auto_items("loved", now)]
    check(loved == [1, 3], f"loved: newest heart first: {loved}")
    check(srv.summary(srv.LIB[3])["loved"] is True and srv.summary(srv.LIB[2])["loved"] is False, "summary carries loved")
    srv.set_loved(1, False)
    srv.load_listening()
    check(list(srv.LOVED) == [3], f"hearts read back from the DB: {list(srv.LOVED)}")

    # on this day: within 3 days of today's date in earlier years, newest year first; this year and far days not
    today = datetime.date.fromtimestamp(now)
    y1, y2 = today.year - 1, today.year - 3
    same_day = lambda y: today.replace(year=y) if (today.month, today.day) != (2, 29) else today.replace(year=y, day=28)  # noqa: E731
    d1, d2 = same_day(y1), same_day(y2)
    stamp = lambda d, plus=0: time.mktime(datetime.datetime(d.year, d.month, d.day, 12).timetuple()) + plus * DAY  # noqa: E731
    check([it["id"] for it in srv.auto_items("on_this_day", now)] == [], "on this day: empty without old plays")
    srv.note_play(5, stamp(d2, -2))  # three years ago, 2 days before
    srv.note_play(6, stamp(d1, 2))   # last year, 2 days after
    srv.note_play(4, stamp(d1, -1))  # last year
    srv.note_play(5, stamp(d1, -1) - 3600)  # also last year: listed there (its newest year)
    srv.note_play(2, stamp(d1, 6))   # 6 days off: not listed
    srv.note_play(1, stamp(d2, 20))  # 20 days off
    srv.note_play(7, stamp(d1, 0))   # hidden duplicate that was played: listed (it was heard)
    otd = [it["id"] for it in srv.auto_items("on_this_day", now)]
    check(otd[:1] == [6] and set(otd) == {6, 4, 5, 7} and otd.index(5) < 4, f"on this day: {otd}")
    check(otd == [6, 7, 4, 5], f"on this day order: last year's (latest play first), then older years: {otd}")

    lists = srv.auto_lists()
    keys = [x["key"] for x in lists]
    check(keys == ["loved", "most_played", "recent", "forgotten", "on_this_day"], f"auto lists: {keys}")
    loved_entry = lists[0]
    check(loved_entry == {"key": "loved", "name": "Loved", "count": 1, "first_art": 3, "ids": [3]},
          f"loved entry: {loved_entry}")
    srv.set_loved(3, False)
    check(srv.auto_lists()[0]["count"] == 0, "loved is listed while empty")
    check([x["id"] for x in srv.collection("most_played")][:2] == [4, 6], "collection() answers the auto names")

    # version groups: the heart and "Not heard in a while" count for the whole song / video group
    srv.LIB[8] = {"id": 8, "kind": "video", "title": "T2 (Official Video)", "artist": "A", "root": "music",
                  "folder": "", "hidden": 0, "has_art": 0, "added": first, "mtime": first, "duration": 10}
    srv.link_versions()
    check(srv.VERSIONS.get(2) == [2, 8], f"song 2 and video 8 are one group: {srv.VERSIONS.get(2)}")
    check(srv.set_loved(2, True), "heart on the song")
    check(srv.summary(srv.LIB[8])["loved"] is True and srv.full(srv.LIB[8])["loved"] is True,
          "its video shows as loved")
    check(not srv.set_loved(8, True), "heart on the video of a loved song: no change (no second entry)")
    check([it["id"] for it in srv.auto_items("loved", now)] == [2], "loved: the group once")
    check(srv.auto_lists()[0]["ids"] == [2, 8], f"loved entry ids: the group's members {srv.auto_lists()[0]['ids']}")
    srv.LOVED[8] = srv.LOVED[2] + 1  # both members loved (hearts from before the group existed)
    check([it["id"] for it in srv.auto_items("loved", now)] == [8], "loved: the group once, by its newest heart")
    check(srv.set_loved(8, False) and not srv.LOVED and not srv.is_loved(2), "heart off on the video: the group's gone")
    srv.load_listening()
    check(not srv.LOVED, f"group heart off saved: {list(srv.LOVED)}")
    check(2 in [it["id"] for it in srv.auto_items("forgotten", now)], "forgotten: song 2 before its video is played")
    srv.note_play(8, now - 60)  # heard as its video ("Prefer videos" or a version picked by hand)
    forgotten = [it["id"] for it in srv.auto_items("forgotten", now)]
    check(2 not in forgotten and 8 not in forgotten, f"forgotten: a group heard as any version is left out: {forgotten}")
    del srv.LIB[8]
    srv.link_versions()


async def next_msg(ws, types, timeout=8):
    while True:
        msg = await asyncio.wait_for(ws.receive_json(), timeout)
        if msg.get("type") in types:
            return msg


def plays(data):
    con = sqlite3.connect(data / "listening.db")
    try:
        return con.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
    finally:
        con.close()


async def get(http, path):
    async with http.get(f"{BASE}{path}") as r:
        return await r.json()


def start(tmp, env):
    return subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)


def stop(proc):
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


async def wait_up(http, count):
    for _ in range(150):
        try:
            async with http.get(f"{BASE}/api/library") as r:
                items = (await r.json())["items"]
                if len(items) == count:
                    return items
        except (aiohttp.ClientError, KeyError, ValueError):
            pass
        await asyncio.sleep(0.2)
    return []


async def e2e(tmp):
    lib = tmp / "lib"
    for n, name in enumerate(("Alpha", "Beta", "Gamma", "Delta")):
        song(lib / "music" / f"{n + 1:02d} {name}.flac", 3, name, 400 + 50 * n)
    video(lib / "videos" / "Test - Omega.mkv", 3)
    data = tmp / "data"
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(data),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT))
    env.pop("ATV_BONJOUR", None)
    proc = start(tmp, env)
    try:
        async with aiohttp.ClientSession() as http:
            items = await wait_up(http, 5)
            ids = {it["title"]: it["id"] for it in items}
            check(len(ids) == 5 and all(it["loved"] is False for it in items), "library: 5 items, no hearts")
            A, B, C, D, V = ids["Alpha"], ids["Beta"], ids["Gamma"], ids["Delta"], ids["Omega"]
            stop(proc)
            # an older server's history: imported once on the first start with listening.db
            (data / "listening.db").unlink(missing_ok=True)
            now = time.time()
            (data / "history.json").write_text(json.dumps({"played": {str(B): now - 3600, str(C): now - 7200}}))
            proc = start(tmp, env)
            await wait_up(http, 5)
            auto = await get(http, "/api/auto")
            keys = [x["key"] for x in auto]
            check(keys == ["loved", "most_played", "recent", "forgotten"], f"/api/auto lists (on this day hidden): {keys}")
            by = {x["key"]: x for x in auto}
            check(by["loved"]["count"] == 0 and by["most_played"]["count"] == 2 and by["recent"]["count"] == 5 and
                  by["forgotten"]["count"] == 3, f"/api/auto counts: {auto}")
            check(by["most_played"]["name"] == "Most played" and by["most_played"]["first_art"] in (B, C, None),
                  "/api/auto entry: name and first_art")
            most = [it["id"] for it in (await get(http, "/api/collection/most_played"))["items"]]
            check(most == [B, C], f"imported plays: most played {most}")

            player = await http.ws_connect(f"{BASE}/ws?role=player&kind=tv")
            remote = await http.ws_connect(f"{BASE}/ws?role=remote&session=phone")
            await next_msg(player, {"state"})
            await next_msg(remote, {"state"})

            async def cmd(name, **args):
                await player.send_json({"type": "cmd", "cmd": name, **args})

            async def play(item):
                await player.send_json({"type": "status", "item": item, "playing": True, "position": 1, "duration": 3})

            # plays: every start counts once
            await cmd("play_ids", ids=[D, A])
            await next_msg(player, {"load"})
            await play(D)
            await play(D)
            await cmd("jump", index=0)
            await next_msg(player, {"load"})
            await play(D)
            await cmd("next")
            await next_msg(player, {"load"})
            await play(A)
            await asyncio.sleep(0.3)
            most = [it["id"] for it in (await get(http, "/api/collection/most_played"))["items"]]
            check(most == [D, A, B, C], f"most played: D twice, then by last play: {most}")

            # hearts
            await cmd("love", id=C)
            m = await next_msg(player, {"auto"})
            check(m == {"type": "auto"}, "love: players get {type: auto}")
            await next_msg(remote, {"auto"})
            check(True, "love: remotes of another queue get {type: auto} too")
            st = await get(http, "/api/state?session=tv")
            check(st["current"]["id"] == A and st["current"]["loved"] is False, "state.current carries loved")
            await cmd("love")  # no id: the current item, toggled on
            m = await next_msg(player, {"state"})
            check(m["state"]["current"]["loved"] is True, "love without id: current item, state broadcast")
            await next_msg(player, {"auto"})
            await cmd("love", id=C, on=True)  # already loved: nothing changes
            try:
                await next_msg(player, {"auto"}, timeout=0.8)
                check(False, "love on an already loved item: no broadcast")
            except asyncio.TimeoutError:
                check(True, "love on an already loved item: no broadcast")
            loved = [it["id"] for it in (await get(http, "/api/collection/loved"))["items"]]
            check(loved == [A, C], f"loved: newest heart first: {loved}")
            lib_items = {it["id"]: it for it in (await get(http, "/api/library"))["items"]}
            check(lib_items[A]["loved"] and lib_items[C]["loved"] and not lib_items[B]["loved"], "library rows carry loved")
            await cmd("love", id=A)  # toggle off
            await next_msg(player, {"auto"})
            await cmd("love", id=V, on=True)
            await next_msg(player, {"auto"})
            await cmd("love", id=12345, on=True)  # unknown id: ignored
            await cmd("love", id="x")
            await asyncio.sleep(0.3)
            loved = [it["id"] for it in (await get(http, "/api/collection/loved"))["items"]]
            check(loved == [V, C], f"loved after toggle off / unknown ids: {loved}")
            check((await get(http, "/api/auto"))[0]["count"] == 2, "/api/auto: loved count")

            # an auto list plays like any collection: in order, or shuffled
            await cmd("play_collection", collection="most_played")
            m = await next_msg(player, {"load"})
            st = await get(http, "/api/state?session=tv")
            check(m["item"]["id"] == D and st["queue"] == [D, A, B, C] and not st["shuffle"],
                  f"play_collection most_played in order: {st['queue']}")
            await cmd("play_collection", collection="loved", start_id=C)
            m = await next_msg(player, {"load"})
            check(m["item"]["id"] == C, "play_collection loved from start_id")
            await cmd("play_collection", collection="recent", shuffle=True)
            await next_msg(player, {"load"})
            st = await get(http, "/api/state?session=tv")
            check(st["shuffle"] and sorted(st["queue"]) == sorted([A, B, C, D, V]), "recent shuffled: all five items")
            cur = st["queue"][st["index"]]
            await play(cur)
            await asyncio.sleep(0.3)
            most1 = [it["id"] for it in (await get(http, "/api/collection/most_played"))["items"]]
            plays1 = plays(data)
            check(plays1 == 6, f"plays in listening.db: 2 imported + 4 starts: {plays1}")

            # restart keeps plays and hearts; history.json is not imported again
            await player.close()
            await remote.close()
            stop(proc)
            proc = start(tmp, env)
            await wait_up(http, 5)
            most2 = [it["id"] for it in (await get(http, "/api/collection/most_played"))["items"]]
            check(most2 == most1 and plays(data) == plays1, f"restart: plays kept, not imported twice: {most2}")
            # the player reconnects in mid-track: the same start is not counted again
            player = await http.ws_connect(f"{BASE}/ws?role=player&kind=tv")
            m = await next_msg(player, {"load"})
            check(m["item"]["id"] == cur, "restart: the player gets the same item again")
            await play(cur)
            await asyncio.sleep(0.3)
            check(plays(data) == plays1, f"restart in mid-track: no second play: {plays(data)}")
            await player.close()
            loved = [it["id"] for it in (await get(http, "/api/collection/loved"))["items"]]
            check(loved == [V, C], f"restart: hearts kept: {loved}")
            hist = await get(http, "/api/history?limit=10")
            check(hist["count"] == len({A, B, C, D, cur}), f"/api/history keeps its meaning (30 days): {hist['count']}")
            log = (tmp / "server.log").read_text()
            check("Traceback" not in log, "no tracebacks in the server log")
            check("2 plays imported" in log, "import logged")
    finally:
        stop(proc)


def rename_keeps_added(tmp):
    import scanner
    lib = tmp / "rlib"
    song(lib / "a" / "01 Song.flac", 3, "Song")
    db = tmp / "r.db"
    scanner.scan(db, {"music": str(lib)})
    con = scanner.connect(db)
    before = con.execute("SELECT id, added FROM items").fetchone()
    con.close()
    time.sleep(1.1)
    (lib / "a" / "01 Song.flac").rename(lib / "a" / "01 Song (renamed).flac")
    stats = scanner.scan(db, {"music": str(lib)})
    con = scanner.connect(db)
    after = con.execute("SELECT id, added, relpath FROM items").fetchone()
    con.close()
    check(stats["renamed"] == 1 and after["id"] == before["id"] and after["added"] == before["added"],
          "scanner: a renamed file keeps its id and its added time")


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="atv-autotest-"))
    try:
        unit(tmp)
        rename_keeps_added(tmp)
        asyncio.run(e2e(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
