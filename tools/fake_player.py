#!/usr/bin/env python3
"""Fake player app for testing the web remote against a THROWAWAY server (no real playback).

    ATV_URL=http://127.0.0.1:18097 python3 tools/fake_player.py [tv|phone]

Connects like the Apple TV / iPhone app (role=player&kind=...), follows load/control messages and
reports status once a second, so the web remote shows a playing item with a moving progress bar.
Never point this at a real server while a real app is connected: it would take part in that queue.
"""
import asyncio
import os
import sys
import time

import aiohttp

URL = os.environ.get("ATV_URL", "http://127.0.0.1:8080")
KIND = sys.argv[1] if len(sys.argv) > 1 else "tv"


async def main():
    item, duration, position, playing, since = None, 0.0, 0.0, False, time.time()

    def pos():
        return position + (time.time() - since if playing else 0)

    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(f"{URL}/ws?role=player&kind={KIND}") as ws:

            async def status():
                if item is not None:
                    await ws.send_json({"type": "status", "item": item, "playing": playing,
                                        "position": round(pos(), 2), "duration": duration})

            async def ticker():
                while True:
                    await asyncio.sleep(1)
                    if item is not None and playing and duration and pos() >= duration:
                        await ws.send_json({"type": "ended", "item": item})
                    else:
                        await status()

            task = asyncio.create_task(ticker())
            async for raw in ws:
                m = raw.json()
                if m.get("type") == "load":
                    it = m.get("item") or {}
                    item, duration = it.get("id"), float(it.get("duration") or 180)
                    position, since, playing = float(m.get("position") or 0), time.time(), bool(m.get("autoplay"))
                elif m.get("type") == "control":
                    a = m.get("action")
                    position, since = pos(), time.time()
                    if a == "seek":
                        position = float(m.get("position") or 0)
                    elif a in ("toggle", "play", "pause"):
                        playing = a == "play" or (a == "toggle" and not playing)
                    elif a == "stop":
                        playing = False
                else:
                    continue
                await status()
            task.cancel()


asyncio.run(main())
