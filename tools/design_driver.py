#!/usr/bin/env python3
"""Drive a THROWAWAY media server like the web remote does, for design screenshots in CI.

    design_driver.py setup                         wait for the scan, create demo playlists
    design_driver.py play <session> <collection> [title] [--shuffle]
    design_driver.py cmd <session> <cmd> ['{"json": "args"}']
    design_driver.py state <session>

session = tv | phone. Server URL from ATV_URL (default http://127.0.0.1:8080).
Never point this at a real server: it changes that server's queues and playlists.
"""
import asyncio
import json
import os
import sys
import time
import urllib.request

import aiohttp

URL = os.environ.get("ATV_URL", "http://127.0.0.1:8080")


def get(path):
    with urllib.request.urlopen(URL + path, timeout=30) as r:
        return json.load(r)


def post(path, body=None):
    req = urllib.request.Request(URL + path, data=json.dumps(body or {}).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def library():
    return get("/api/library")["items"]


def find(title):
    items = library()
    for it in items:
        if title.lower() in (it["title"] or "").lower():
            return it
    raise SystemExit(f"no item matching {title!r}")


async def send(session, message):
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(f"{URL}/ws?role=remote&session={session}") as ws:
            await ws.receive(timeout=10)  # initial state
            await ws.send_str(json.dumps(message))
            await asyncio.sleep(0.5)


def setup():
    for _ in range(120):
        try:
            if len(library()) >= 20:
                break
        except OSError:
            pass
        time.sleep(1)
    post("/api/rescan?wait=1")
    items = library()
    print(f"library: {len(items)} items")
    by = lambda *names: [it["id"] for n in names for it in items if n.lower() in it["title"].lower()]  # noqa: E731
    if not get("/api/playlists")["playlists"]:
        post("/api/playlists", {"name": "Late Night Drive", "ids": by(
            "Harbor Lights", "Afterglow", "Undertow", "Northern Hum", "City of Glass", "Prism Rain",
            "Lantern Bay", "Monsoon")})
        post("/api/playlists", {"name": "Sunday Morning", "ids": by(
            "Slow Bloom", "Low Tide", "Letters Home", "Kite", "Cardamom", "Driftwood")})
        post("/api/playlists", {"name": "Music videos only", "ids": [it["id"] for it in items if it["kind"] == "video"]})
        post("/api/playlists", {"name": "Arcade", "ids": by("High Score", "Pixel Heart", "Continue")})
    print("playlists:", [p["name"] for p in get("/api/playlists")["playlists"]])


def main():
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    if args[0] == "setup":
        setup()
    elif args[0] == "play":
        session, collection = args[1], args[2]
        msg = {"type": "cmd", "cmd": "play_collection", "collection": collection, "shuffle": "--shuffle" in args}
        rest = [a for a in args[3:] if not a.startswith("--")]
        if rest:
            msg["start_id"] = find(rest[0])["id"]
        asyncio.run(send(session, msg))
    elif args[0] == "cmd":
        msg = {"type": "cmd", "cmd": args[2], **(json.loads(args[3]) if len(args) > 3 else {})}
        asyncio.run(send(args[1], msg))
    elif args[0] == "state":
        print(json.dumps(get(f"/api/state?session={args[1]}"), indent=1)[:2000])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
