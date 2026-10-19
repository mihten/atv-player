#!/usr/bin/env python3
"""Shared queue with several players (Apple TV + iPhone): one active output, handover keeps the position,
"here" commands, stale messages from an old output, legacy players without a device id. Runs a THROWAWAY
server instance (own temp library, data dir and port; never the real one).
    python3 test_outputs.py   (run on the media server host)
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
PORT = 18093
BASE = f"http://127.0.0.1:{PORT}"
WS = BASE.replace("http", "ws")
failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def tone(path, seconds, freq):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
                    "-metadata", f"title={path.stem}", "-metadata", "artist=Test", str(path)], check=True)


async def drain(ws):
    """All messages that arrive within 0.4 s."""
    out = []
    while True:
        try:
            out.append(await asyncio.wait_for(ws.receive_json(), 0.4))
        except asyncio.TimeoutError:
            return out


def playback(msgs):
    return [m for m in msgs if m.get("type") in ("load", "control")]


async def state(http):
    async with http.get(f"{BASE}/api/state") as r:
        return await r.json()


async def wait_up(http, count):
    for _ in range(60):
        try:
            async with http.get(f"{BASE}/api/library") as r:
                items = (await r.json())["items"]
                if len(items) == count:
                    return items
        except (aiohttp.ClientError, KeyError, ValueError):
            pass
        await asyncio.sleep(0.2)
    return []


async def player(http, device=None, name=None, kind=None):
    q = "role=player" + (f"&device={device}" if device else "") + (f"&name={name}" if name else "") + \
        (f"&kind={kind}" if kind else "")
    ws = await http.ws_connect(f"{WS}/ws?{q}")
    await drain(ws)
    return ws


async def main(tmp):
    lib = tmp / "lib"
    for n, f in enumerate((440, 550, 660)):
        tone(lib / "album" / f"0{n + 1} {'ABC'[n]}.flac", 60, f)
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(tmp / "data"),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT))
    env.pop("ATV_BONJOUR", None)
    proc = subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)
    try:
        async with aiohttp.ClientSession() as http:
            items = sorted(await wait_up(http, 3), key=lambda i: i["title"])
            a, b, c = (it["id"] for it in items)
            check(len(items) == 3, "throwaway library has 3 tones")

            tv = await player(http, "tv1", "Apple TV", "tv")
            st = await state(http)
            check(st["output"] == "tv1" and len(st["outputs"]) == 1, "first player (TV) becomes the output")
            phone = await player(http, "ph1", "iPhone", "phone")
            st = await state(http)
            check(st["output"] == "tv1" and [o["device"] for o in st["outputs"]] == ["tv1", "ph1"],
                  "second player (iPhone) is listed but the TV stays the output")
            remote = await http.ws_connect(f"{WS}/ws?role=remote")
            await drain(remote)

            await remote.send_json({"type": "cmd", "cmd": "play_collection", "collection": "folder",
                                    "root": "music", "folder": "album"})
            tv_msgs, ph_msgs = playback(await drain(tv)), playback(await drain(phone))
            check(any(m["type"] == "load" and m["item"]["id"] == a for m in tv_msgs) and not ph_msgs,
                  "web remote play -> only the TV loads A")
            await tv.send_json({"type": "status", "item": a, "playing": True, "position": 20.0, "duration": 60})
            await drain(phone)

            # Hand over to the iPhone: TV stops + releases, iPhone loads A at ~20 s, playing.
            await remote.send_json({"type": "cmd", "cmd": "output", "device": "ph1"})
            tv_msgs, ph_msgs = playback(await drain(tv)), playback(await drain(phone))
            actions = [m.get("action") for m in tv_msgs]
            check("stop" in actions and "release" in actions and not any(m["type"] == "load" for m in tv_msgs),
                  f"output -> iPhone: TV gets stop + release ({actions})")
            load = next((m for m in ph_msgs if m["type"] == "load"), None)
            check(load is not None and load["item"]["id"] == a and load["autoplay"] and 19.5 <= load["position"] <= 22,
                  f"iPhone loads A at the same position, playing ({load and round(load['position'], 1)} s)")
            st = await state(http)
            check(st["output"] == "ph1", "state: output = iPhone")

            # A late status from the TV must not move the shared position.
            await phone.send_json({"type": "status", "item": a, "playing": True, "position": 25.0, "duration": 60})
            await tv.send_json({"type": "status", "item": a, "playing": False, "position": 3.0, "duration": 60})
            await asyncio.sleep(0.3)
            st = await state(http)
            check(st["position"] == 25.0 and st["playing"], "stale status from the old output is ignored")
            await tv.send_json({"type": "ended", "item": a})
            await asyncio.sleep(0.3)
            check((await state(http))["current"]["id"] == a, "stale 'ended' from the old output does not skip")

            # Commands from the remote go to the iPhone now.
            await remote.send_json({"type": "cmd", "cmd": "next"})
            tv_msgs, ph_msgs = playback(await drain(tv)), playback(await drain(phone))
            check(not tv_msgs and any(m["type"] == "load" and m["item"]["id"] == b for m in ph_msgs),
                  "remote 'next' -> iPhone loads B, TV untouched")
            await phone.send_json({"type": "status", "item": b, "playing": True, "position": 8.0, "duration": 60})

            # Play pressed on the TV (here) takes playback back to the TV at the current position.
            await tv.send_json({"type": "cmd", "cmd": "toggle", "here": True})
            tv_msgs, ph_msgs = playback(await drain(tv)), playback(await drain(phone))
            load = next((m for m in tv_msgs if m["type"] == "load"), None)
            check(load is not None and load["item"]["id"] == b and load["autoplay"] and 7.5 <= load["position"] <= 10,
                  "TV play with here=true -> TV takes over B at ~8 s")
            check("release" in [m.get("action") for m in ph_msgs], "... and the iPhone is released")

            # Starting something on the iPhone with here=true plays it on the iPhone.
            await phone.send_json({"type": "cmd", "cmd": "play_ids", "ids": [c, a], "here": True})
            tv_msgs, ph_msgs = playback(await drain(tv)), playback(await drain(phone))
            load = next((m for m in ph_msgs if m["type"] == "load"), None)
            check(load is not None and load["item"]["id"] == c and load["present"] and
                  "release" in [m.get("action") for m in tv_msgs], "iPhone play_ids here=true -> plays C on the iPhone")
            # Without here: the iPhone acts as a remote for the current output.
            await remote.send_json({"type": "cmd", "cmd": "output", "device": "tv1"})
            await drain(tv), await drain(phone)
            await phone.send_json({"type": "cmd", "cmd": "next"})
            tv_msgs, ph_msgs = playback(await drain(tv)), playback(await drain(phone))
            check(any(m["type"] == "load" for m in tv_msgs) and not ph_msgs,
                  "iPhone 'next' without here -> goes to the TV (remote mode)")

            # Output disconnects: playback state stops; next command picks a connected player.
            await tv.close()
            await asyncio.sleep(0.3)
            st = await state(http)
            check(st["output"] is None and not st["playing"] and len(st["outputs"]) == 1,
                  "TV gone: no output online, not playing")
            await remote.send_json({"type": "cmd", "cmd": "play"})
            ph_msgs = playback(await drain(phone))
            check(any(m.get("action") == "play" for m in ph_msgs), "remote play with the TV gone -> iPhone plays")

            # A player from before 0.9.0 (no device id) still works as an output.
            legacy = await player(http)
            st = await state(http)
            legacy_id = next((o["device"] for o in st["outputs"] if o["device"].startswith("addr-")), None)
            check(legacy_id is not None and st["output"] == "ph1", f"legacy player listed as {legacy_id}, iPhone stays")
            await remote.send_json({"type": "cmd", "cmd": "output", "device": legacy_id})
            lg_msgs = playback(await drain(legacy))
            check(any(m["type"] == "load" for m in lg_msgs), "output -> legacy player works")

            # Output survives a server restart (persisted).
            await phone.close(), await legacy.close(), await remote.close()
            proc.terminate()
            proc.wait(30)
            proc = subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                                    stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)
            await wait_up(http, 3)
            phone = await player(http, "ph1", "iPhone", "phone")
            check((await state(http))["output"] == "ph1", "after a restart the iPhone (only player) is the output")
            await phone.close()
            log = (tmp / "server.log").read_text()
            check("Bonjour" not in log, "test instance does not advertise over Bonjour")
    finally:
        proc.terminate()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="atv-outtest-"))
    try:
        asyncio.run(main(tmp))
    except Exception:
        print((tmp / "server.log").read_text()[-3000:] if (tmp / "server.log").exists() else "")
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
