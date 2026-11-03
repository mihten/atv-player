#!/usr/bin/env python3
"""atv-audio's own queue (session "box"), server side: the box player gets its `sound` before every load, the web
remote sets the box's sound (presets, Boost, Loudness, saved), the TV wins the shared output (box paused + notice,
a box Play refused while the TV plays there), headphone `button` messages from the sink act on the output's owner,
volume from a box remote reaches the sink, notices from the box player. Gain rules (box_sound) as in the apps.
Runs a THROWAWAY server instance (own temp library, data dir and port; never the real one).
    python3 test_box.py   (needs ffmpeg + the server's deps)
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
import box_sound  # noqa: E402
import test_atv_audio as H  # noqa: E402  (helpers only; its main() does not run on import)

PORT = 18097
H.PORT, H.BASE = PORT, f"http://127.0.0.1:{PORT}"
H.WS = H.BASE.replace("http", "ws")
failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def unit():
    m4 = {"preset": "a:momentum4", "boost": 0, "loudness": "raise"}
    q, loud = {"lufs": -21.0}, {"lufs": -6.0}
    check(box_sound.message(m4, q)["gain"] == 11.0, "Momentum 4 + loudness, -21 LUFS track: +4 + 7 = +11 dB")
    check(box_sound.message(m4, loud)["gain"] == 4.0, "... -6 LUFS track: +4 (loudness never turns down)")
    check(box_sound.message(m4, {"lufs": -11.6})["gain"] == 4.0, "... -11.6 LUFS (Shallow AGT): no lift")
    check(box_sound.message(m4, {"lufs": -14.3})["gain"] == 4.5, "... -14.3 LUFS: +0.3 -> half-dB step +0.5")
    check(box_sound.message({**m4, "boost": 8}, q)["gain"] == 14.0, "+4 + 8 + 7 = 19 -> capped at +14")
    off = box_sound.message({"preset": "off", "boost": 0, "loudness": "off"}, q)
    check(off["processed"] is False and off["gain"] == 0 and off["bands"] == [0.0] * 10, "all off = unprocessed")
    club = box_sound.message({"preset": "p:2", "boost": 0, "loudness": "off"}, None)
    check(club["gain"] == -6.0 and club["bands"][2] == 8.0, "VLC Club: preamp 6 = -6 dB, 125 Hz +8")
    check(box_sound.message({"preset": "p:1", "boost": 0, "loudness": "off"}, None)["bands"][0] == 0.0,
          "VLC's -1.11e-15 bands read as 0")
    names = [n for _, n in box_sound.choices()]
    check(len(names) == 21 and names[:3] == ["Off", "Momentum 4", "Amplify"] and names[-1] == "Techno",
          f"presets: Off + 2 app + 18 VLC in the apps' order ({len(names)})")
    check(box_sound.clean({"preset": "x", "boost": 99, "loudness": "level"}) == box_sound.DEFAULT | {"boost": 8},
          "clean(): unknown preset -> off, boost clamped 0..8, unknown loudness -> default")
    check(box_sound.message(m4, q)["freqs"] == [31.25, 62.5, 125, 250, 500, 1000, 2000, 4000, 8000, 16000],
          "ISO band frequencies (the TV's)")


async def until(ws, kind, seconds=3.0, cond=lambda m: True):
    """Messages until one of `kind` (matching cond) arrives; returns (that message or None, all read)."""
    seen = []
    end = asyncio.get_event_loop().time() + seconds
    while True:
        left = end - asyncio.get_event_loop().time()
        if left <= 0:
            return None, seen
        try:
            msg = await asyncio.wait_for(ws.receive(), left)
        except asyncio.TimeoutError:
            return None, seen
        if msg.type != aiohttp.WSMsgType.TEXT:
            return None, seen
        m = json.loads(msg.data)
        seen.append(m)
        if m.get("type") == kind and cond(m):
            return m, seen


async def main(tmp):
    unit()
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
            items = await H.library(http)
            ids = [items[f"0{n} Song{n}"]["id"] for n in range(1, 5)]
            st = await H.state(http, "box")
            check(st["session"] == "box" and st["session_name"] == "atv-audio", "session box named atv-audio")
            check([x["session"] for x in st["sessions"]] == ["tv", "phone", "box"], "sessions list has the box")
            check(st["sound"] == box_sound.DEFAULT and len(st["sound_presets"]) == 21, "box state: sound + presets")
            check(st["sink"]["connected"] is False and st["sink"]["route"] is False, "box sink: not connected")
            check((await H.state(http, "phone"))["sink"] is None and "sound" not in await H.state(http, "tv"),
                  "tv / phone states have no box fields")

            box = await H.connect(http, "role=player&kind=box")
            remote = await H.connect(http, "role=remote&session=box")
            sink = await http.ws_connect(f"{H.WS}/ws?role=sink&session=tv")
            await H.drain(sink)
            await sink.send_json({"type": "sink", "name": "atv-audio", "output": {
                "kind": "bluetooth", "name": "Headphones", "codec": "aptx_hd", "ready": True, "volume": 50}})
            await H.drain(sink)
            st = await H.state(http, "box")
            check(st["sink"]["connected"] and st["sink"]["route"] and st["sink"]["output"]["volume"] == 50,
                  "box state shows the sink's output (route true: the box always plays through it)")

            # ---- play on the box: sound first, then the load
            await remote.send_json({"type": "cmd", "cmd": "play_ids", "ids": ids})
            load, seen = await until(box, "load")
            sounds = [i for i, m in enumerate(seen) if m.get("type") == "sound"]
            check(load is not None and load["autoplay"] and load["item"]["id"] == ids[0], "box gets load, autoplay")
            check(bool(sounds) and sounds[-1] < seen.index(load), "... with its sound message before the load")
            await box.send_json({"type": "status", "item": ids[0], "playing": True, "position": 1.0, "duration": 4})
            await H.drain(box)
            check((await H.state(http, "box"))["playing"] is True, "box status playing -> state playing")

            # ---- sound settings from the web remote
            await remote.send_json({"type": "cmd", "cmd": "sound", "preset": "a:momentum4", "boost": 2})
            snd, _ = await until(box, "sound")
            check(snd is not None and snd["processed"] and snd["gain"] >= 6.0, f"sound change -> box gets it ({snd})")
            st = await H.state(http, "box")
            check(st["sound"] == {"preset": "a:momentum4", "boost": 2, "loudness": "raise"}, "state.sound saved")
            saved = json.loads((tmp / "data" / "state-box.json").read_text())
            check(saved.get("sound", {}).get("preset") == "a:momentum4", "... and in state-box.json")

            # ---- volume from a box remote reaches the sink
            await remote.send_json({"type": "volume", "percent": 33, "muted": False})
            vol, _ = await until(sink, "volume")
            check(vol == {"type": "volume", "percent": 33, "muted": False}, "box remote volume -> sink")

            # ---- headphone button while the TV is not routed: the box's queue moves
            await sink.send_json({"type": "button", "cmd": "next", "session": "box"})
            load, _ = await until(box, "load")
            check(load is not None and load["item"]["id"] == ids[1], "button next (box) -> box loads item 2")

            # ---- the TV takes the output: the box is paused with a notice
            tv = await H.connect(http, "role=player&kind=tv")
            await tv.send_json({"type": "route", "sink": True})
            await H.drain(tv)
            check(len(of_type(await H.drain(box), "control")) == 0, "TV routed but idle: box keeps playing")
            await box.send_json({"type": "status", "item": ids[1], "playing": True, "position": 0.5, "duration": 4})
            await H.drain(box)
            await tv.send_json({"type": "cmd", "cmd": "play_ids", "ids": [ids[3]]})
            ctl, _ = await until(box, "control")
            check(ctl == {"type": "control", "action": "pause"}, "TV starts with sound on atv-audio -> box paused")
            st = await H.state(http, "box")
            check(st["notice"] and "Apple TV" in st["notice"]["text"] and st["playing"] is False,
                  "... box notice 'in use by the Apple TV'")
            check(st["sink"]["in_use_by_tv"] is True, "... box state: in_use_by_tv")
            await tv.send_json({"type": "status", "item": ids[3], "playing": True, "position": 0.2, "duration": 4})
            await H.drain(tv)
            await remote.send_json({"type": "cmd", "cmd": "toggle"})
            ctl, _ = await until(box, "control", 1.0)
            check(ctl is None, "box Play while the TV plays there: refused (no control to the box)")
            await remote.send_json({"type": "cmd", "cmd": "play_ids", "ids": [ids[2]]})
            load, _ = await until(box, "load")
            check(load is not None and load["autoplay"] is False, "box play_ids while the TV plays: loads paused")

            # ---- button while the TV owns the output: the TV's queue
            await sink.send_json({"type": "button", "cmd": "pause", "session": "tv"})
            ctl, _ = await until(tv, "control")
            check(ctl == {"type": "control", "action": "pause"}, "button pause (tv) -> TV pauses")
            await tv.send_json({"type": "status", "item": ids[3], "playing": False, "position": 0.4, "duration": 4})
            await H.drain(tv)

            # ---- TV paused: the box may play again (only on Play), the notice goes
            await remote.send_json({"type": "cmd", "cmd": "play"})
            ctl, _ = await until(box, "control")
            check(ctl == {"type": "control", "action": "play"}, "TV paused: box Play allowed")
            check((await H.state(http, "box"))["notice"] is None, "... notice cleared by the Play")

            # ---- notice from the box player (headphones gone): paused, shown until dismissed / Play
            await box.send_json({"type": "notice", "text": "Headphones disconnected: paused"})
            await H.drain(box)
            st = await H.state(http, "box")
            check(st["notice"]["text"] == "Headphones disconnected: paused", "box notice from the player")
            await remote.send_json({"type": "cmd", "cmd": "dismiss"})
            await H.drain(remote)
            check((await H.state(http, "box"))["notice"] is None, "dismiss clears it")

            # ---- reconnect: sound before load again; TV and phone unaffected
            await box.close()
            box = await http.ws_connect(f"{H.WS}/ws?role=player&kind=box")
            first = await H.drain(box)
            types = [m["type"] for m in first]
            check("sound" in types and "load" in types and types.index("sound") < types.index("load"),
                  f"box reconnect: sound before load ({types})")
            tvst = await H.state(http, "tv")
            check(tvst["queue"] == [ids[3]] and tvst["session"] == "tv", "TV queue untouched by the box")
            for ws in (box, remote, sink, tv):
                await ws.close()
    finally:
        proc.terminate()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()


def of_type(msgs, kind):
    return [m for m in msgs if m.get("type") == kind]


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="atv-boxtest-"))
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
