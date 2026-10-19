#!/usr/bin/env python3
"""Separate queues per device kind (Apple TV app = "tv", iPhone app = "phone"): each app plays only its own
queue, both can play at once, the web remote controls one queue (?session= / select), queues persist in
state.json (TV) and state-phone.json, players without a kind count as the TV. Runs a THROWAWAY server
instance (own temp library, data dir and port; never the real one).
    python3 test_sessions.py   (run on the media server host)
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


def loads(msgs):
    return [m for m in msgs if m.get("type") == "load"]


def controls(msgs):
    return [m.get("action") for m in msgs if m.get("type") == "control"]


def last_state(msgs):
    states = [m["state"] for m in msgs if m.get("type") == "state"]
    return states[-1] if states else None


async def state(http, session=None):
    async with http.get(f"{BASE}/api/state" + (f"?session={session}" if session else "")) as r:
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


async def connect(http, query):
    ws = await http.ws_connect(f"{WS}/ws?{query}")
    await drain(ws)
    return ws


def start(tmp, env):
    return subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)


async def main(tmp):
    lib = tmp / "lib"
    for n, f in enumerate((440, 550, 660)):
        tone(lib / "album" / f"0{n + 1} {'ABC'[n]}.flac", 60, f)
    data = tmp / "data"
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(data),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT))
    env.pop("ATV_BONJOUR", None)
    proc = start(tmp, env)
    try:
        async with aiohttp.ClientSession() as http:
            items = sorted(await wait_up(http, 3), key=lambda i: i["title"])
            a, b, c = (it["id"] for it in items)
            check(len(items) == 3, "throwaway library has 3 tones")

            tv = await connect(http, "role=player&kind=tv")
            phone = await connect(http, "role=player&kind=phone")
            tv_state, ph_state = await state(http, "tv"), await state(http, "phone")
            check(tv_state["session"] == "tv" and ph_state["session"] == "phone" and
                  tv_state["players"] == 1 and ph_state["players"] == 1, "TV and iPhone each have their own session")

            # iPhone starts the folder: only the iPhone loads.
            await phone.send_json({"type": "cmd", "cmd": "play_collection", "collection": "folder",
                                   "root": "music", "folder": "album"})
            ph_msgs, tv_msgs = await drain(phone), await drain(tv)
            check([m["item"]["id"] for m in loads(ph_msgs)] == [a] and not loads(tv_msgs) and not controls(tv_msgs),
                  "iPhone play -> only the iPhone loads A, TV gets nothing")
            # TV plays something else at the same time.
            await tv.send_json({"type": "cmd", "cmd": "play_ids", "ids": [c, b]})
            tv_msgs, ph_msgs = await drain(tv), await drain(phone)
            check([m["item"]["id"] for m in loads(tv_msgs)] == [c] and not loads(ph_msgs) and not controls(ph_msgs),
                  "TV play -> only the TV loads C, iPhone untouched")
            await phone.send_json({"type": "status", "item": a, "playing": True, "position": 12.0, "duration": 60})
            await tv.send_json({"type": "status", "item": c, "playing": True, "position": 3.0, "duration": 60})
            await asyncio.sleep(0.3)
            tv_state, ph_state = await state(http, "tv"), await state(http, "phone")
            check(tv_state["playing"] and ph_state["playing"] and tv_state["current"]["id"] == c and
                  ph_state["current"]["id"] == a and ph_state["position"] == 12.0,
                  "both play at the same time, each with its own item and position")
            summary = {x["session"]: x for x in tv_state["sessions"]}
            check(summary["tv"]["playing"] and summary["phone"]["playing"] and summary["phone"]["title"] == "01 A",
                  "state lists both sessions (name, playing, title)")

            # Next / ended stay within each device.
            await phone.send_json({"type": "cmd", "cmd": "next"})
            ph_msgs, tv_msgs = await drain(phone), await drain(tv)
            check([m["item"]["id"] for m in loads(ph_msgs)] == [b] and not loads(tv_msgs),
                  "iPhone next -> iPhone loads B, TV keeps C")
            await tv.send_json({"type": "ended", "item": c})
            tv_msgs, ph_msgs = await drain(tv), await drain(phone)
            check([m["item"]["id"] for m in loads(tv_msgs)] == [b] and not loads(ph_msgs),
                  "TV item ended -> TV advances to its own next (B), iPhone untouched")
            await phone.send_json({"type": "ended", "item": c})  # an item that is not the iPhone's current
            await asyncio.sleep(0.3)
            check((await state(http, "phone"))["current"]["id"] == b, "'ended' for a foreign item is ignored")

            # Web remote: TV by default, can switch to the iPhone's queue.
            remote = await http.ws_connect(f"{WS}/ws?role=remote")
            first = last_state(await drain(remote))
            check(first is not None and first["session"] == "tv", "web remote starts on the TV queue")
            await remote.send_json({"type": "cmd", "cmd": "pause"})
            tv_msgs, ph_msgs = await drain(tv), await drain(phone)
            check(controls(tv_msgs) == ["pause"] and not controls(ph_msgs), "remote pause -> TV only")
            await remote.send_json({"type": "select", "session": "phone"})
            sel = last_state(await drain(remote))
            check(sel is not None and sel["session"] == "phone" and sel["current"]["id"] == b,
                  "remote select phone -> gets the iPhone queue")
            await remote.send_json({"type": "cmd", "cmd": "jump", "index": 2})
            ph_msgs, tv_msgs = await drain(phone), await drain(tv)
            check([m["item"]["id"] for m in loads(ph_msgs)] == [c] and not loads(tv_msgs),
                  "remote jump while on the iPhone queue -> iPhone loads C")
            await phone.send_json({"type": "status", "item": c, "playing": True, "position": 1.0, "duration": 60})
            upd = last_state(await drain(remote))
            check(upd is not None and upd["session"] == "phone" and upd["current"]["id"] == c,
                  "remote receives the iPhone's state updates")
            remote_tv = await connect(http, "role=remote&session=tv")
            await tv.send_json({"type": "status", "item": b, "playing": False, "position": 5.0, "duration": 60})
            msgs = await drain(remote)
            check(last_state(msgs) is None, "iPhone-queue remote gets no TV queue state")
            seen = [m for m in msgs if m.get("type") == "sessions"]
            check(seen and not seen[-1]["sessions"][0]["playing"] and seen[-1]["sessions"][1]["playing"],
                  "... but a 'sessions' message saying the TV stopped and the iPhone plays")
            # A remote on the TV queue learns when the iPhone starts playing (web "Auto" mode follows it).
            await drain(remote_tv)
            await phone.send_json({"type": "status", "item": c, "playing": False, "position": 2.0, "duration": 60})
            await phone.send_json({"type": "status", "item": c, "playing": True, "position": 3.0, "duration": 60})
            msgs = await drain(remote_tv)
            seen = [m for m in msgs if m.get("type") == "sessions"]
            check(seen and seen[-1]["sessions"][1]["session"] == "phone" and seen[-1]["sessions"][1]["playing"],
                  "TV-queue remote receives 'sessions' when the iPhone starts playing")
            check(last_state(msgs) is None, "... without the iPhone's queue state")

            # Save the iPhone queue as a playlist.
            async with http.post(f"{BASE}/api/playlists", json={"name": "From phone", "queue": True,
                                                                "session": "phone"}) as r:
                pl = await r.json()
            async with http.get(f"{BASE}/api/playlists/{pl['id']}") as r:
                pl_items = [i["id"] for i in (await r.json())["items"]]
            check(pl_items == (await state(http, "phone"))["queue"], "save queue as playlist uses the chosen queue")

            # A player without a kind (TV app before 0.11) joins the TV queue.
            legacy = await connect(http, "role=player")
            check((await state(http, "tv"))["players"] == 2, "player without kind counts as the TV")
            await legacy.close()

            # iPhone app closes: its session stops, the TV session is unaffected.
            await tv.send_json({"type": "status", "item": b, "playing": True, "position": 6.0, "duration": 60})
            await phone.close()
            await asyncio.sleep(0.3)
            tv_state, ph_state = await state(http, "tv"), await state(http, "phone")
            check(not ph_state["playing"] and ph_state["players"] == 0 and tv_state["playing"],
                  "iPhone gone: iPhone session not playing, TV still playing")
            phone = await http.ws_connect(f"{WS}/ws?role=player&kind=phone")
            back = loads(await drain(phone))
            check(len(back) == 1 and back[0]["item"]["id"] == c and not back[0]["autoplay"],
                  "iPhone reconnects -> its own last item, paused")

            # Persistence: separate files, survives a restart; a 0.9.0 state file ("output" key) still loads.
            for ws in (tv, phone, remote, remote_tv):
                await ws.close()
            proc.terminate()
            proc.wait(30)
            tv_file, ph_file = data / "state.json", data / "state-phone.json"
            check(tv_file.is_file() and ph_file.is_file(), "queues saved to state.json and state-phone.json")
            saved = json.loads(tv_file.read_text())
            saved["output"] = "some-device-uuid"
            tv_file.write_text(json.dumps(saved))
            proc = start(tmp, env)
            await wait_up(http, 3)
            tv_state, ph_state = await state(http, "tv"), await state(http, "phone")
            check(tv_state["queue"] == [c, b] and ph_state["queue"] == [a, b, c] and ph_state["current"]["id"] == c,
                  "after a restart each device has its own queue back (0.9.0 'output' key ignored)")
            check(tv_state["repeat_mode"] == "all" and ph_state["repeat_mode"] == "all", "repeat all on both")
            check("Bonjour" not in (tmp / "server.log").read_text(), "test instance does not advertise over Bonjour")
    finally:
        proc.terminate()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="atv-sesstest-"))
    try:
        asyncio.run(main(tmp))
    except Exception:
        log = tmp / "server.log"
        print(log.read_text()[-3000:] if log.exists() else "")
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
