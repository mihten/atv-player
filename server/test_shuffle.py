#!/usr/bin/env python3
"""Shuffle with memory (play history, server 0.16.0), against a THROWAWAY server instance (own temp library, data dir
and port; never the real one).   python3 test_shuffle.py
Part 1 tests smart_shuffle() in-process, part 2 the whole path over WebSocket: status -> history, new shuffle, repeat-all
lap, restart.
"""
import asyncio
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
PORT = 18096
BASE = f"http://127.0.0.1:{PORT}"
N = 8
failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def tone(path, seconds, freq):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
                    "-metadata", f"title={path.stem}", "-metadata", "artist=Test", str(path)], check=True)


def unit(tmp):
    os.environ.update(ATV_ROOTS=json.dumps({"music": str(tmp / "none")}), ATV_DATA=str(tmp / "unit"),
                      ATV_CACHE=str(tmp / "unitcache"))
    (tmp / "unit").mkdir()
    sys.path.insert(0, str(HERE))
    srv = importlib.import_module("server")
    now = time.time()
    ids = list(range(1, 21))
    srv.PLAYED.clear()
    for n, i in enumerate(range(11, 21)):  # 11..20 played recently, 20 last
        srv.PLAYED[i] = now - 3600 + n
    srv.PLAYED[5] = now - 40 * 86400  # older than HISTORY_DAYS: counts as not played
    runs = [srv.smart_shuffle(ids) for _ in range(300)]
    check(all(sorted(r) == ids for r in runs), "smart_shuffle keeps every item exactly once")
    check(all(set(r[:10]) == set(range(1, 11)) for r in runs), "not recently played items (incl. one 40 days old) come first")
    check(all(set(r[15:]) == set(range(16, 21)) for r in runs), "the most recently played half comes last")
    check(len({tuple(r[:10]) for r in runs}) > 250, "the fresh part is still random")
    srv.PLAYED.clear()
    small = [1, 2, 3, 4, 5, 6]
    for n, i in enumerate(small):
        srv.PLAYED[i] = now - 100 + n
    orders = {tuple(srv.smart_shuffle(small)) for _ in range(300)}
    check(len(orders) == 36, f"fully played small playlist still varies ({len(orders)} of 36 possible orders)")
    check(all(set(o[3:]) == {4, 5, 6} for o in orders), "  ... and the last three heard stay at the back")
    check(srv.smart_shuffle([]) == [] and srv.smart_shuffle([7]) == [7], "empty and single-item lists")
    srv.note_played(3)
    data = json.loads((tmp / "unit" / "history.json").read_text())
    check("3" in data["played"], "note_played writes history.json")


async def next_msg(ws, types, timeout=8):
    while True:
        msg = await asyncio.wait_for(ws.receive_json(), timeout)
        if msg.get("type") in types:
            return msg


def start(tmp, env):
    return subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)


def stop(proc):
    proc.terminate()
    try:
        proc.wait(30)
    except subprocess.TimeoutExpired:
        proc.kill()


async def wait_up(http):
    for _ in range(100):
        try:
            async with http.get(f"{BASE}/api/library") as r:
                items = (await r.json())["items"]
                if len(items) == N:
                    return items
        except (aiohttp.ClientError, KeyError, ValueError):
            pass
        await asyncio.sleep(0.2)
    return []


async def history(http):
    async with http.get(f"{BASE}/api/history?limit=100") as r:
        return await r.json()


async def play_through(player, item, playing=True):
    """What the app does: report playing (maybe twice), then ended. Returns the next load (or control) message."""
    if playing:
        for pos in (0.5, 1.5):
            await player.send_json({"type": "status", "item": item, "playing": True, "position": pos, "duration": 2})
    await asyncio.sleep(0.05)
    await player.send_json({"type": "ended", "item": item})
    return await next_msg(player, {"load", "control"})


async def e2e(tmp):
    lib = tmp / "lib"
    for n in range(N):
        tone(lib / "album" / f"{n + 1:02d} T{n + 1}.flac", 2, 300 + 50 * n)
    data = tmp / "data"
    data.mkdir()
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(data),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT))
    proc = start(tmp, env)
    try:
        async with aiohttp.ClientSession() as http:
            items = await wait_up(http)
            check(len(items) == N, f"throwaway server up with {N} items")
            h = await history(http)
            check(h["count"] == 0 and h["days"] == 30, "no history file yet: empty history")
            player = await http.ws_connect(f"{BASE}/ws?role=player&kind=tv")
            await next_msg(player, {"state"})

            # First shuffle: play 3 items (one of them skipped without a playing status = not heard).
            await player.send_json({"type": "cmd", "cmd": "play_collection", "collection": "music", "shuffle": True})
            cur = (await next_msg(player, {"load"}))["item"]["id"]
            heard = []
            for _ in range(3):
                heard.append(cur)
                cur = (await play_through(player, cur))["item"]["id"]
            skipped = cur
            await player.send_json({"type": "cmd", "cmd": "next"})
            cur = (await next_msg(player, {"load"}))["item"]["id"]
            h = await history(http)
            check(h["count"] == 3 and {r["id"] for r in h["recent"]} == set(heard),
                  "history = the 3 items that reported playing (skipped-before-start one not counted)")
            check(h["recent"][0]["id"] == heard[-1] and h["recent"][0]["title"], "/api/history newest first, with titles")
            check(skipped not in {r["id"] for r in h["recent"]}, "item skipped before it played is not in history")

            # New "Shuffle music": the heard ones must not be at the front.
            await player.send_json({"type": "cmd", "cmd": "play_collection", "collection": "music", "shuffle": True})
            await next_msg(player, {"load"})
            async with http.get(f"{BASE}/api/state?session=tv") as r:
                q = (await r.json())["queue"]
            check(sorted(q) == sorted(it["id"] for it in items), "new shuffle holds every item once")
            check(set(q[-3:]) == set(heard), f"new shuffle: the 3 heard items are at the back ({q})")

            # Play a whole lap; at the repeat-all wrap the items heard last must not come straight back.
            order = []
            cur = q[0]
            for _ in range(N - 1):
                order.append(cur)
                cur = (await play_through(player, cur))["item"]["id"]
            order.append(cur)
            m = await play_through(player, cur)
            check(m["type"] == "load", "repeat all: wraps to a new lap")
            async with http.get(f"{BASE}/api/state?session=tv") as r:
                st = await r.json()
            q2 = st["queue"]
            check(st["index"] == 0 and m["item"]["id"] == q2[0], "new lap starts at its first item")
            check(set(q2[:N // 2]) == set(order[:N // 2]), "new lap starts with the half heard longest ago")
            check(q2[0] != order[-1], "the item that just ended does not play again right away")

            # Shuffle toggle on an unshuffled queue also uses the history.
            await player.send_json({"type": "cmd", "cmd": "play_collection", "collection": "music", "shuffle": False})
            await next_msg(player, {"load"})
            await player.send_json({"type": "cmd", "cmd": "shuffle", "on": True})
            await asyncio.sleep(0.3)
            async with http.get(f"{BASE}/api/state?session=tv") as r:
                st = await r.json()
            at = {r["id"]: r["at"] for r in (await history(http))["recent"]}
            rest = st["queue"][st["index"] + 1:]
            by_age = sorted(rest, key=lambda i: at[i])
            newer = by_age[len(by_age) // 2:]
            check(st["shuffle"] and set(rest[-len(newer):]) == set(newer),
                  "shuffle toggle: the most recently heard half of the upcoming items is at the back")

            await player.close()
            stop(proc)
            before = (json.loads((data / "history.json").read_text()))["played"]
            proc = start(tmp, env)
            await wait_up(http)
            h = await history(http)
            check(h["count"] == len(before) == N, f"history survives a restart ({h['count']} items)")
    finally:
        stop(proc)


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="atv-shuftest-"))
    try:
        unit(tmp)
        asyncio.run(e2e(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
