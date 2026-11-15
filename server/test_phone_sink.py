#!/usr/bin/env python3
"""The iPhone on the atv-audio headphones (server 0.30.1): "last Play wins" across the Apple TV and the iPhone.

THROWAWAY server with a fake sink, a fake Apple TV player and a fake iPhone player. Checks: the iPhone's state shows the
sink (route = its own choice); its route / lip sync / clock wait on the server while the Apple TV holds the headphones;
choosing them alone takes nothing; the iPhone's Play takes them (the sink gets the iPhone's state + load, the Apple TV is
paused with the notice); the sink's `ready` goes to the iPhone; its clock / volume reach the sink; the Apple TV's Play
takes them back; the iPhone leaving while it holds them hands the sink back (the Apple TV gets it again).
    python3 test_phone_sink.py   (needs ffmpeg + the server's deps)
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
sys.path.insert(0, str(HERE))
import test_atv_audio as H  # noqa: E402
from test_box import until, of_type  # noqa: E402

PORT = 18103
H.PORT, H.BASE = PORT, f"http://127.0.0.1:{PORT}"
H.WS = H.BASE.replace("http", "ws")
failed = 0

READY = {"kind": "bluetooth", "name": "Headphones X", "codec": "aptx_hd", "ready": True, "volume": 40, "muted": False}


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def sessions_of(msgs):
    return [m["state"]["session"] for m in msgs if m["type"] == "state"]


async def main(tmp):
    lib = tmp / "lib"
    for n in range(1, 5):
        H.tone(lib / "songs" / f"0{n} Song{n}.flac", 4, 300 + 40 * n)
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(tmp / "data"),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT))
    env.pop("ATV_BONJOUR", None)
    proc = subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)
    try:
        async with aiohttp.ClientSession() as http:
            check(len(await H.wait_up(http, 4)) == 4, "throwaway library has 4 songs")
            ids = sorted(it["id"] for it in (await H.library(http)).values())

            sink = await http.ws_connect(f"{H.WS}/ws?role=sink&session=tv")
            await H.drain(sink)
            await sink.send_json({"type": "sink", "name": "atv-audio", "output": READY})
            await H.drain(sink)
            tv = await H.connect(http, "role=player&kind=tv")
            tvremote = await H.connect(http, "role=remote&session=tv")
            phone = await H.connect(http, "role=player&kind=phone")
            phremote = await H.connect(http, "role=remote&session=phone")
            await H.drain(sink)

            # ---- the iPhone sees the sink; its route is its own
            st = await H.state(http, "phone")
            check(st["sink"] and st["sink"]["connected"] is True and st["sink"]["output"]["ready"] is True
                  and st["sink"]["route"] is False, f"phone state: the sink and its output, route false ({st['sink']})")

            # ---- the sink's status reaches the iPhone (its Settings show it); its volume does nothing while it does not
            # use the headphones
            await H.drain(phone)
            await sink.send_json({"type": "sink", "name": "atv-audio", "output": dict(READY, ready=False)})
            got = await H.drain(phone)
            check([m for m in of_type(got, "state") if m["state"]["sink"]["output"]["ready"] is False],
                  "sink output lost: the iPhone gets a state with it")
            await sink.send_json({"type": "sink", "name": "atv-audio", "output": READY})
            await H.drain(phone)
            await H.drain(sink)
            await phone.send_json({"type": "volume", "percent": 12, "muted": False})
            check(not of_type(await H.drain(sink), "volume"), "the iPhone's volume before it chose the headphones: not sent")

            # ---- the Apple TV holds the headphones and plays
            await tv.send_json({"type": "route", "sink": True})
            await tvremote.send_json({"type": "cmd", "cmd": "play_ids", "ids": ids[:2]})
            await until(tv, "load")
            await tv.send_json({"type": "status", "item": ids[0], "playing": True, "position": 0.5, "duration": 4})
            await H.drain(tv)
            got = await H.drain(sink)
            check("tv" in sessions_of(got) and of_type(got, "load"), "the sink follows the Apple TV")

            # ---- the iPhone queues something and chooses the headphones while the Apple TV plays: nothing moves
            await phremote.send_json({"type": "cmd", "cmd": "play_ids", "ids": ids[2:]})
            await until(phone, "load")
            await phone.send_json({"type": "status", "item": ids[2], "playing": False, "position": 0, "duration": 4})
            await H.drain(phone)
            await phremote.send_json({"type": "cmd", "cmd": "pause"})
            await H.drain(phone)
            await H.drain(sink)
            await phone.send_json({"type": "route", "sink": True})
            await phone.send_json({"type": "lip_sync", "ms": -100})
            await phone.send_json({"type": "clock", "item": ids[2], "position": 0.0, "at": 10.0, "state": "paused"})
            got = await H.drain(sink)
            check(not [m for m in got if m["type"] in ("route", "lip_sync", "clock", "load")]
                  and "phone" not in sessions_of(got),
                  f"phone chooses the headphones while the Apple TV plays: the sink stays with the Apple TV ({[m['type'] for m in got]})")
            st = await H.state(http, "phone")
            check(st["sink"]["route"] is True, "... the phone's state shows its route true")
            seen = await H.drain(tv)
            check(not of_type(seen, "notice") and {"type": "control", "action": "pause"} not in seen,
                  "... the Apple TV keeps playing")

            # ---- the iPhone's Play takes them
            await H.drain(phone)
            await phremote.send_json({"type": "cmd", "cmd": "play"})
            await until(phone, "control")
            # like the app: Play reaches the player, it reports playing (the server learns of a Play from the status)
            await phone.send_json({"type": "status", "item": ids[2], "playing": True, "position": 0.1, "duration": 4})
            got = await H.drain(sink)
            check(got and got[0]["type"] == "state" and got[0]["state"]["session"] == "phone",
                  f"iPhone Play: the sink follows the iPhone ({[m['type'] for m in got]})")
            loads = of_type(got, "load")
            check(loads and loads[-1]["item"]["id"] == ids[2], "... with the iPhone's item")
            check({"type": "route", "sink": True} in got and {"type": "lip_sync", "ms": -100} in got,
                  "... and the iPhone's route + lip sync replayed")
            seen = await H.drain(tv)
            nt = of_type(seen, "notice")
            check({"type": "control", "action": "pause"} in seen and nt
                  and nt[0]["text"] == "The headphones went to the iPhone — playback paused",
                  f"the Apple TV is paused with the notice ({nt})")
            await tv.send_json({"type": "status", "item": ids[0], "playing": False, "position": 0.6, "duration": 4})
            await H.drain(tv)
            await sink.send_json({"type": "ready", "item": ids[2]})
            rd, _ = await until(phone, "sink_ready")
            check(rd == {"type": "sink_ready", "id": ids[2]}, f"the sink's ready -> the iPhone ({rd})")
            await phone.send_json({"type": "status", "item": ids[2], "playing": True, "position": 0.3, "duration": 4})
            await H.drain(phone)
            await H.drain(sink)
            await phone.send_json({"type": "clock", "item": ids[2], "position": 0.4, "at": 12.0, "state": "playing"})
            got = await H.drain(sink)
            check(of_type(got, "clock") and of_type(got, "clock")[-1]["item"] == ids[2],
                  f"the iPhone's clock reaches the sink ({[m['type'] for m in got]})")
            await tv.send_json({"type": "clock", "item": ids[0], "position": 0.6, "at": 12.0, "state": "paused"})
            got = await H.drain(sink)
            check(not of_type(got, "clock"), "... the Apple TV's clock does not while the iPhone holds them")
            await phone.send_json({"type": "volume", "percent": 55, "muted": False})
            got = await H.drain(sink)
            check({"type": "volume", "percent": 55, "muted": False} in got, "the iPhone's volume sets the headphones' volume")
            st = await H.state(http, "box")
            check(st["sink"]["in_use_by"] == "iPhone", f"Web Player: atv-audio in use by the iPhone ({st['sink'].get('in_use_by')})")

            # ---- the Apple TV takes them back (after the guard)
            await asyncio.sleep(2.1)
            await H.drain(phone)
            await tvremote.send_json({"type": "cmd", "cmd": "play"})
            await until(tv, "control")
            await tv.send_json({"type": "status", "item": ids[0], "playing": True, "position": 0.6, "duration": 4})
            got = await H.drain(sink)
            check(got and got[0]["type"] == "state" and got[0]["state"]["session"] == "tv",
                  f"Apple TV Play: it takes the headphones back ({[m['type'] for m in got]})")
            seen = await H.drain(phone)
            nt = of_type(seen, "notice")
            check({"type": "control", "action": "pause"} in seen and nt
                  and nt[0]["text"] == "The headphones went to the Apple TV — playback paused",
                  f"the iPhone is paused with the notice ({nt})")
            await phone.send_json({"type": "status", "item": ids[2], "playing": False, "position": 0.5, "duration": 4})
            await H.drain(phone)

            # ---- the iPhone takes them again, then leaves: the sink goes back to the Apple TV
            await tvremote.send_json({"type": "cmd", "cmd": "pause"})
            await tv.send_json({"type": "status", "item": ids[0], "playing": False, "position": 0.7, "duration": 4})
            await H.drain(tv)
            await asyncio.sleep(2.1)
            await phremote.send_json({"type": "cmd", "cmd": "play"})
            await until(phone, "control")
            await phone.send_json({"type": "status", "item": ids[2], "playing": True, "position": 0.5, "duration": 4})
            got = await H.drain(sink)
            check("phone" in sessions_of(got), "the iPhone's Play takes them again")
            await H.drain(tv)
            await phone.close()
            got = await H.drain(sink)
            check("tv" in sessions_of(got) and of_type(got, "load") and of_type(got, "load")[-1]["item"]["id"] == ids[0],
                  f"the iPhone leaves: the sink follows the Apple TV again ({[m['type'] for m in got]})")
            st = await H.state(http, "phone")
            check(st["sink"]["route"] is False, "... the iPhone's route is off until it connects again")
            await tv.send_json({"type": "clock", "item": ids[0], "position": 0.7, "at": 20.0, "state": "paused"})
            got = await H.drain(sink)
            check(of_type(got, "clock"), "... and the Apple TV's clock reaches the sink again")
            for ws in (sink, tv, tvremote, phremote):
                await ws.close()
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="atv-phonesink-"))
    try:
        asyncio.run(main(tmp))
    except Exception:
        log = tmp / "server.log"
        print(log.read_text()[-3000:] if log.exists() else "")
        raise
    finally:
        if failed:
            log = tmp / "server.log"
            print(log.read_text()[-4000:] if log.exists() else "")
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
