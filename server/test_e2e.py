#!/usr/bin/env python3
"""End-to-end test of the media server: HTTP endpoints + simulated Apple TV player and a phone remote."""
import asyncio
import json
import sys

import aiohttp

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        check.failed += 1


check.failed = 0


async def next_msg(ws, mtype, timeout=5):
    while True:
        msg = await asyncio.wait_for(ws.receive_json(), timeout)
        if msg.get("type") == mtype:
            return msg


async def wait_load(ws, item_id, timeout=5):
    """Skip loads of other items (e.g. the resume load sent on connect)."""
    while True:
        msg = await next_msg(ws, "load", timeout)
        if msg["item"] and msg["item"]["id"] == item_id and msg["autoplay"]:
            return msg


async def wait_state(ws, pred, timeout=5):
    """Skip stale state pushes until one satisfies pred."""
    while True:
        msg = await next_msg(ws, "state", timeout)
        if pred(msg["state"]):
            return msg


async def main():
    async with aiohttp.ClientSession() as http:
        lib = await (await http.get(f"{BASE}/api/library")).json()
        check(len(lib["items"]) > 1000, f"library visible items = {len(lib['items'])}")
        folders = (await (await http.get(f"{BASE}/api/folders")).json())["folders"]
        check(len(folders) > 0, f"folders = {len(folders)}")
        vids = (await (await http.get(f"{BASE}/api/collection/videos")).json())["items"]
        check(len(vids) >= 40, f"videos = {len(vids)}")
        flac = next(i for i in lib["items"] if i["kind"] == "audio" and i["codec"] == "flac" and i["art"])

        r = await http.get(f"{BASE}/media/{flac['id']}", headers={"Range": "bytes=100-199"})
        body = await r.read()
        check(r.status == 206 and len(body) == 100, f"range request on FLAC -> {r.status}, {len(body)} B, {r.headers.get('Content-Type')}")
        r = await http.get(f"{BASE}/art/{flac['id']}")
        check(r.status == 200 and (await r.read())[:2] == b"\xff\xd8", f"FLAC cover art -> {r.status}")
        r = await http.get(f"{BASE}/art/{vids[0]['id']}")
        check(r.status == 200 and (await r.read())[:2] == b"\xff\xd8", f"video frame grab -> {r.status}")
        roots = (await (await http.get(f"{BASE}/api/browse")).json())["roots"]
        check(len(roots) >= 1, f"browse roots = {[(r['name'], r['count']) for r in roots]}")
        root = max(roots, key=lambda r: r["count"])["root"]
        top = await (await http.get(f"{BASE}/api/browse", params={"root": root, "folder": ""})).json()
        sub = top["subfolders"][0]
        deep = await (await http.get(f"{BASE}/api/browse", params={"root": root, "folder": sub["folder"]})).json()
        check(len(top["subfolders"]) > 10 and deep["total"] == sub["count"],
              f"browse {root}: {len(top['subfolders'])} folders, '{sub['name']}' {sub['count']} = {deep['total']}")
        fresh_art = [i for i in lib["items"] if i["art"]][-1]["id"]
        rs = await asyncio.gather(*[http.get(f"{BASE}/art/{fresh_art}") for _ in range(6)])
        check(all(r.status == 200 for r in rs), f"6 concurrent art requests -> {[r.status for r in rs]}")
        item = await (await http.get(f"{BASE}/api/item/{vids[0]['id']}")).json()
        print("     video item:", {k: item[k] for k in ("title", "artist", "codec", "audio_track", "audio_ord", "sub_track", "sub_ord")})

        live = await (await http.get(f"{BASE}/api/state")).json()
        if live["players"]:
            print("ABORT: a real Apple TV player is connected; the queue test would take over its playback")
            sys.exit(2)
        player = await http.ws_connect(f"{BASE.replace('http', 'ws')}/ws?role=player")
        remote = await http.ws_connect(f"{BASE.replace('http', 'ws')}/ws?role=remote")
        st = await next_msg(remote, "state")
        check(st["state"]["players"] == 1, "remote sees 1 player connected")

        await remote.send_json({"type": "cmd", "cmd": "play_collection", "collection": "videos", "start_id": vids[3]["id"]})
        load = await wait_load(player, vids[3]["id"])
        check(load["item"]["id"] == vids[3]["id"] and load["autoplay"], f"player got load of start video: {load['item']['title']}")
        await player.send_json({"type": "status", "item": load["item"]["id"], "playing": True, "position": 1.0, "duration": 200})
        st = await wait_state(remote, lambda s: s["playing"])
        check(st["state"]["playing"] and st["state"]["index"] == 3, "remote sees playing, index 3")

        await player.send_json({"type": "ended", "item": load["item"]["id"]})
        load2 = await wait_load(player, vids[4]["id"])
        check(load2["item"]["id"] == vids[4]["id"], "ended -> next video loaded")

        await player.send_json({"type": "cmd", "cmd": "prev", "position": 2})
        load3 = await wait_load(player, vids[3]["id"])
        check(load3["item"]["id"] == vids[3]["id"], "prev (pos 2 s) -> previous video")
        await player.send_json({"type": "cmd", "cmd": "prev", "position": 30})
        ctl = await next_msg(player, "control")
        check(ctl["action"] == "seek" and ctl["position"] == 0, "prev (pos 30 s) -> restart current")

        await remote.send_json({"type": "cmd", "cmd": "shuffle", "on": True})
        st = await wait_state(remote, lambda s: s["shuffle"])
        check(st["state"]["shuffle"] and st["state"]["current"]["id"] == vids[3]["id"], "shuffle on keeps current track")
        await remote.send_json({"type": "cmd", "cmd": "shuffle", "on": False})
        st = await wait_state(remote, lambda s: not s["shuffle"])
        check(not st["state"]["shuffle"] and st["state"]["index"] == 3, "shuffle off restores order")

        await remote.send_json({"type": "cmd", "cmd": "enqueue", "ids": [flac["id"]], "mode": "next"})
        st = await wait_state(remote, lambda s: flac["id"] in s["queue"])
        check(st["state"]["queue"][4] == flac["id"], "enqueue next inserts after current (mixed audio + video queue)")
        await player.send_json({"type": "cmd", "cmd": "next"})
        load4 = await wait_load(player, flac["id"])
        check(load4["item"]["id"] == flac["id"] and load4["item"]["kind"] == "audio", f"next -> FLAC: {load4['item']['title']}")

        await remote.send_json({"type": "cmd", "cmd": "toggle"})
        ctl = await next_msg(player, "control")
        check(ctl["action"] == "toggle", "remote toggle -> player control")

        await remote.send_json({"type": "cmd", "cmd": "play_collection", "collection": "all", "shuffle": True})
        load5 = await next_msg(player, "load")
        st = await wait_state(remote, lambda s: len(s["queue"]) > 1000)
        check(st["state"]["shuffle"] and len(st["state"]["queue"]) > 1000, f"shuffle everything: {len(st['state']['queue'])} items, first {load5['item']['kind']}")
        await player.close()
        await remote.close()
    print("FAILED" if check.failed else "ALL PASSED")
    sys.exit(1 if check.failed else 0)


asyncio.run(main())
