#!/usr/bin/env python3
"""The iPhone with Sound output ATV-Server on the server's own player (server 0.31.0).

THROWAWAY server with a fake sink + its box player (the server's player), a fake Apple TV player and a fake iPhone
player. Checks: `server_play on` hands the iPhone's queue to the server's player (paused, at the iPhone's position) and
pauses the Web Player with a notice; the iPhone then only watches (no load / control, its player reports ignored); the
iPhone's commands and the end of a track drive the server's player; the iPhone's sound settings reach it; the iPhone
leaving (locked) does not stop it and a reconnect neither loads the iPhone nor restarts the server's player; the Apple
TV's Play pauses it with a notice to the iPhone, the iPhone's Play takes the headphones back; headphone buttons act on
the iPhone's queue; the Web Player's Play is refused meanwhile; the server's player reconnecting resumes the iPhone's
queue; `server_play off` gives the queue back to the iPhone (paused) and the Web Player its player.
    python3 test_phone_server.py   (needs ffmpeg + the server's deps)
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

PORT = 18107
H.PORT, H.BASE = PORT, f"http://127.0.0.1:{PORT}"
H.WS = H.BASE.replace("http", "ws")
failed = 0

READY = {"kind": "bluetooth", "name": "Headphones X", "codec": "aptx_hd", "ready": True, "volume": 40, "muted": False}
IN_USE_PHONE = "atv-audio is in use by the iPhone: paused"


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def types(msgs):
    return [m["type"] for m in msgs]


def plays(msgs):
    """load / control messages: what a player is told to play."""
    return [m for m in msgs if m["type"] in ("load", "control")]


async def main(tmp):
    lib = tmp / "lib"
    for n in range(1, 6):
        H.tone(lib / "songs" / f"0{n} Song{n}.flac", 4, 300 + 40 * n)
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(tmp / "data"),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT))
    env.pop("ATV_BONJOUR", None)
    proc = subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)
    try:
        async with aiohttp.ClientSession() as http:
            check(len(await H.wait_up(http, 5)) == 5, "throwaway library has 5 songs")
            ids = sorted(it["id"] for it in (await H.library(http)).values())

            sink = await http.ws_connect(f"{H.WS}/ws?role=sink&id=atv-audio")
            await H.drain(sink)
            await sink.send_json({"type": "sink", "name": "atv-audio", "output": READY})
            box = await H.connect(http, "role=player&kind=box&sink=atv-audio")
            tv = await H.connect(http, "role=player&kind=tv")
            tvremote = await H.connect(http, "role=remote&session=tv")
            boxremote = await H.connect(http, "role=remote&session=box")
            phone = await H.connect(http, "role=player&kind=phone")
            phremote = await H.connect(http, "role=remote&session=phone")
            for ws in (sink, box, tv, phone):
                await H.drain(ws)

            # ---- the Web Player plays; the iPhone plays its own queue itself
            await boxremote.send_json({"type": "cmd", "cmd": "play_ids", "ids": ids[3:]})
            ld, _ = await until(box, "load")
            check(ld["item"]["id"] == ids[3] and ld["autoplay"] is True, "Web Player: its player plays its queue")
            await box.send_json({"type": "status", "item": ids[3], "playing": True, "position": 0.5, "duration": 4})
            await phremote.send_json({"type": "cmd", "cmd": "play_ids", "ids": ids[:3]})
            ld, _ = await until(phone, "load")
            check(ld["item"]["id"] == ids[0], "iPhone: plays its own queue")
            await phone.send_json({"type": "status", "item": ids[0], "playing": False, "position": 1.25, "duration": 4})
            await H.drain(phone)
            await H.drain(box)
            await H.drain(boxremote)

            # ---- Sound output ATV-Server: the server's player takes the iPhone's queue
            await phone.send_json({"type": "server_play", "on": True})
            got = await H.drain(box, 0.8)
            check({"type": "control", "action": "pause"} in got, f"server_play on: the Web Player's player is paused ({types(got)})")
            sts = [m for m in of_type(got, "state") if m["state"]["session"] == "phone"]
            check(bool(sts), "... it gets the iPhone's state")
            snd = of_type(got, "sound")
            loads = of_type(got, "load")
            check(bool(snd) and loads and loads[-1]["item"]["id"] == ids[0] and loads[-1]["autoplay"] is False
                  and abs(loads[-1]["position"] - 1.25) < 0.3,
                  f"... sound + the iPhone's item, paused, at the iPhone's position ({loads[-1] if loads else None})")
            seen = await H.drain(boxremote)
            notes = [m["state"].get("notice") for m in of_type(seen, "state") if m["state"]["session"] == "box"]
            check(any(n and n["text"] == IN_USE_PHONE for n in notes), f"... the Web Player shows the notice ({notes[-1:]})")
            st = await H.state(http, "phone")
            check(st["sink"]["server"] is True and st["sink"]["route"] is False and st["players"] == 1,
                  f"phone state: server true, route false, one player ({st['sink'].get('server')}, {st['players']})")
            check(not plays(await H.drain(phone)), "the iPhone gets no load / control")

            # ---- the iPhone's commands drive the server's player
            await phone.send_json({"type": "cmd", "cmd": "play"})
            got = await H.drain(box)
            check({"type": "control", "action": "play"} in got, f"iPhone Play -> the server's player ({types(got)})")
            check(not plays(await H.drain(phone)), "... not the iPhone")
            await box.send_json({"type": "status", "item": ids[0], "playing": True, "position": 1.5, "duration": 4})
            await H.drain(phone)
            await phone.send_json({"type": "status", "item": ids[0], "playing": False, "position": 0.0, "duration": 4})
            await phone.send_json({"type": "route", "sink": True})
            await phone.send_json({"type": "clock", "item": ids[0], "position": 0.0, "at": 5.0, "state": "paused"})
            await asyncio.sleep(0.3)
            st = await H.state(http, "phone")
            check(st["playing"] is True and abs(st["position"] - 1.5) < 0.01 and st["sink"]["route"] is False,
                  f"the iPhone's own status / route / clock are ignored ({st['playing']}, {st['position']}, {st['sink']['route']})")
            check(not of_type(await H.drain(sink), "clock"), "... nothing of it reaches the sink")
            await phone.send_json({"type": "cmd", "cmd": "seek", "position": 2.0})
            got = await H.drain(box)
            check({"type": "control", "action": "seek", "position": 2.0} in got, "iPhone seek -> the server's player")
            await box.send_json({"type": "ended", "item": ids[0]})
            got = await H.drain(box)
            loads = of_type(got, "load")
            check(of_type(got, "sound") and loads and loads[-1]["item"]["id"] == ids[1] and loads[-1]["autoplay"] is True,
                  f"track end on the server's player: the next one plays there ({types(got)})")
            check(not plays(await H.drain(phone)), "... the iPhone gets no load")
            await box.send_json({"type": "status", "item": ids[1], "playing": True, "position": 0.2, "duration": 4})
            await phone.send_json({"type": "volume", "percent": 33, "muted": False})
            got = await H.drain(sink)
            check({"type": "volume", "percent": 33, "muted": False} in got, "the iPhone's volume sets the headphones' volume")
            await H.drain(box)
            await phone.send_json({"type": "server_sound", "preset": "a:bass", "boost": 2, "loudness": "off"})
            got = await H.drain(box)
            snd = of_type(got, "sound")
            check(snd and snd[-1]["processed"] is True and snd[-1]["bands"][0] > 0,
                  f"the iPhone's sound settings reach the server's player (a:bass -> bass curve) ({snd[-1:] })")

            # ---- the iPhone is locked (its socket goes): the server's player keeps playing
            await phone.close()
            got = await H.drain(box)
            check(not plays(got), f"iPhone gone: the server's player is not stopped ({types(got)})")
            st = await H.state(http, "phone")
            check(st["players"] == 1 and st["playing"] is True, "... the iPhone's queue still plays on it")
            phone = await http.ws_connect(f"{H.WS}/ws?role=player&kind=phone")  # no drain: its first messages count
            got = await H.drain(phone)
            check(not plays(got) and of_type(got, "state"), f"iPhone back: state, no load ({types(got)})")
            await phone.send_json({"type": "server_play", "on": True})
            check(not plays(await H.drain(box)), "... its server_play again restarts nothing")

            # ---- the Apple TV takes the headphones; the iPhone's Play takes them back
            await tv.send_json({"type": "route", "sink": True})
            await tvremote.send_json({"type": "cmd", "cmd": "play_ids", "ids": ids[4:]})
            await until(tv, "load")
            await tv.send_json({"type": "status", "item": ids[4], "playing": True, "position": 0.3, "duration": 4})
            got = await H.drain(box)
            check({"type": "control", "action": "pause"} in got, f"Apple TV plays: the server's player is paused ({types(got)})")
            seen = await H.drain(phone)
            nt = of_type(seen, "notice")
            check(nt and nt[-1]["text"] == "atv-audio is in use by the Apple TV: paused", f"... the iPhone gets the notice ({nt})")
            await box.send_json({"type": "status", "item": ids[1], "playing": False, "position": 0.4, "duration": 4})
            await H.drain(tv)
            await phone.send_json({"type": "cmd", "cmd": "play"})
            seen = await H.drain(tv, 0.6)
            nt = of_type(seen, "notice")
            check({"type": "control", "action": "pause"} in seen and nt
                  and nt[-1]["text"] == "The headphones went to the iPhone — playback paused",
                  f"iPhone Play: the Apple TV is paused with the notice ({nt})")
            await tv.send_json({"type": "status", "item": ids[4], "playing": False, "position": 0.5, "duration": 4})
            got = await H.drain(box, 1.5)
            check({"type": "control", "action": "play"} in got, f"... and the server's player plays the iPhone's queue ({types(got)})")
            await box.send_json({"type": "status", "item": ids[1], "playing": True, "position": 0.5, "duration": 4})
            await H.drain(box)

            # ---- headphone buttons act on the iPhone's queue; the Web Player's Play is refused
            await sink.send_json({"type": "button", "cmd": "pause", "session": "box"})
            got = await H.drain(box)
            check({"type": "control", "action": "pause"} in got, f"headphones Pause -> the iPhone's queue on the server's player ({types(got)})")
            await box.send_json({"type": "status", "item": ids[1], "playing": False, "position": 0.6, "duration": 4})
            await H.drain(box)
            await tv.send_json({"type": "route", "sink": False})  # the Apple TV's sound back on the TV
            await H.drain(box)
            await sink.send_json({"type": "button", "cmd": "play"})  # a button without a session: the output's owner
            got = await H.drain(box, 0.8)
            check({"type": "control", "action": "play"} in got, f"headphones Play (no session) -> the iPhone's queue ({types(got)})")
            await box.send_json({"type": "status", "item": ids[1], "playing": True, "position": 0.6, "duration": 4})
            await sink.send_json({"type": "button", "cmd": "pause"})
            await H.drain(box)
            await box.send_json({"type": "status", "item": ids[1], "playing": False, "position": 0.7, "duration": 4})
            await H.drain(box)
            await H.drain(boxremote)
            await boxremote.send_json({"type": "cmd", "cmd": "play"})
            got = await H.drain(box)
            seen = await H.drain(boxremote)
            notes = [m["state"].get("notice") for m in of_type(seen, "state") if m["state"]["session"] == "box"]
            check(not plays(got) and any(n and n["text"] == IN_USE_PHONE for n in notes),
                  f"Web Player Play while the iPhone uses the server's player: refused with the notice ({types(got)})")

            # ---- the server's player reconnects (atv-audio restart): it gets the iPhone's queue again
            await box.close()
            await asyncio.sleep(0.3)
            box = await http.ws_connect(f"{H.WS}/ws?role=player&kind=box&sink=atv-audio")
            got = await H.drain(box, 0.8)
            loads = of_type(got, "load")
            check(loads and loads[-1]["item"]["id"] == ids[1], f"server's player reconnects: the iPhone's item again ({loads[-1:]})")
            st = await H.state(http, "phone")
            check(st["sink"]["server"] is True and st["players"] == 1, "... still the iPhone's player")

            # ---- Sound output back on the iPhone
            await H.drain(phone)
            await phone.send_json({"type": "server_play", "on": False})
            got = await H.drain(box, 0.8)
            loads = of_type(got, "load")
            check({"type": "control", "action": "pause"} in got and loads and loads[-1]["item"]["id"] == ids[3]
                  and loads[-1]["autoplay"] is False,
                  f"server_play off: the server's player gets the Web Player's queue back, paused ({loads[-1:]})")
            seen = await H.drain(phone)
            loads = of_type(seen, "load")
            check(loads and loads[-1]["item"]["id"] == ids[1] and loads[-1]["autoplay"] is False,
                  f"... the iPhone gets its queue back, paused ({loads[-1:]})")
            st = await H.state(http, "phone")
            check(st["sink"]["server"] is False and st["players"] == 1, "... phone state: server false, the iPhone is its player")
            await phone.send_json({"type": "status", "item": ids[1], "playing": True, "position": 0.9, "duration": 4})
            await asyncio.sleep(0.3)
            st = await H.state(http, "phone")
            check(st["playing"] is True and abs(st["position"] - 0.9) < 0.01, "... its status counts again")
            await boxremote.send_json({"type": "cmd", "cmd": "play"})
            got = await H.drain(box)
            check({"type": "control", "action": "play"} in got, "... and the Web Player plays again")
            for ws in (sink, box, tv, tvremote, boxremote, phone, phremote):
                await ws.close()
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="atv-phonesrv-"))
    try:
        asyncio.run(main(tmp))
    except Exception:
        log = tmp / "server.log"
        print(log.read_text()[-3000:] if log.exists() else "")
        raise
    finally:
        if failed:
            log = tmp / "server.log"
            print(log.read_text()[-5000:] if log.exists() else "")
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
