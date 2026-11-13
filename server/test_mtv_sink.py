#!/usr/bin/env python3
"""The TV Player on the atv-audio headphones, server side (server 0.26.0), and Stop.

The sink follows ONE session: the Apple TV's, or the TV Player's while its player routes its sound there. This runs a
THROWAWAY server with a fake sink, a fake TV Player player, a fake Apple TV player and remotes, and checks: nothing of
the TV Player reaches the sink before the route; the choice endpoint (typed, refused while the Apple TV plays through the
sink); the route replays the TV Player's state into the sink (load, route, sound, lip sync, clock); loads / controls /
sound follow the TV Player; the Apple TV's messages stop reaching the sink; route / lip sync / clock are typed and bounded;
`ready` goes to the TV Player; the volume slider is the headphones' volume; the box yields to the TV Player; headphone
buttons act on it; route false / the player leaving hands the sink back to the Apple TV; the player sees the sink's
state (output lost / back); Stop and the Play after it.
    python3 test_mtv_sink.py   (needs ffmpeg + the server's deps)
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
import test_atv_audio as H  # noqa: E402
from test_box import until, of_type  # noqa: E402

PORT = 18102
H.PORT, H.BASE = PORT, f"http://127.0.0.1:{PORT}"
H.WS = H.BASE.replace("http", "ws")
failed = 0


def tv_side(msgs):
    """What would mean the sink moved to the Apple TV: its state, a load or a route (the TV Player's own state updates and
    controls reach the sink as long as it follows the TV Player)."""
    return [m["type"] for m in msgs
            if (m["type"] == "state" and m["state"]["session"] == "tv") or m["type"] in ("load", "route", "clock")]


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


READY = {"kind": "bluetooth", "name": "Headphones X", "codec": "aptx_hd", "ready": True, "volume": 40, "muted": False}


async def post(http, path, body):
    async with http.post(f"{H.BASE}{path}", json=body) as r:
        return r.status, await r.text()


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
            mtv = await H.connect(http, "role=player&kind=mtv")
            remote = await H.connect(http, "role=remote&session=mtv")
            tv = await H.connect(http, "role=player&kind=tv")
            tvremote = await H.connect(http, "role=remote&session=tv")
            box = await H.connect(http, "role=player&kind=box")
            boxremote = await H.connect(http, "role=remote&session=box")
            await H.drain(sink)

            # ---- before any route: the sink follows the Apple TV only
            st = await H.state(http, "mtv")
            check(st["sink"] == {"connected": True, "name": "atv-audio", "output": {**READY}, "route": False},
                  f"mtv state: the sink, its output, route false ({st['sink']})")
            await remote.send_json({"type": "cmd", "cmd": "play_ids", "ids": ids})
            load, _ = await until(mtv, "load")
            check(load is not None and load["item"]["id"] == ids[0], "mtv player gets its load")
            sunk = await H.drain(sink)
            check(not of_type(sunk, "load") and not of_type(sunk, "state") and not of_type(sunk, "sound"),
                  f"... the sink gets nothing of the TV Player's queue ({[m['type'] for m in sunk]})")
            await mtv.send_json({"type": "lip_sync", "ms": -100})
            await mtv.send_json({"type": "clock", "item": ids[0], "position": 1.0, "at": 50.0, "state": "playing"})
            check(not await H.drain(sink), "lip sync / clock before the route stay on the server")

            # ---- the choice endpoint
            check((await post(http, "/api/mtv-sound", {"output": "bogus"}))[0] == 400, "bad output refused (400)")
            check((await post(http, "/api/mtv-sound", {}))[0] == 400, "no output refused (400)")
            await tv.send_json({"type": "route", "sink": True})
            await tvremote.send_json({"type": "cmd", "cmd": "play_ids", "ids": ids[2:]})
            await until(tv, "load")
            await tv.send_json({"type": "status", "item": ids[2], "playing": True, "position": 0.5, "duration": 4})
            await H.drain(tv)
            await H.drain(sink)
            status, text = await post(http, "/api/mtv-sound", {"output": "headphones"})
            msg, _ = await until(mtv, "sound_out")
            check(status == 200 and msg == {"type": "sound_out", "mode": "headphones"},
                  f"headphones accepted while the Apple TV plays through the sink: the last Play takes them ({status} {msg})")
            check(not [m for m in await H.drain(sink) if m["type"] in ("sound_out", "state", "load")],
                  "... choosing them moves nothing on the sink")
            await tvremote.send_json({"type": "cmd", "cmd": "pause"})
            await tv.send_json({"type": "status", "item": ids[2], "playing": False, "position": 0.5, "duration": 4})
            await H.drain(tv)
            await tv.send_json({"type": "route", "sink": False})
            await H.drain(tv)
            await H.drain(sink)
            status, _ = await post(http, "/api/mtv-sound", {"output": "headphones"})
            msg, _ = await until(mtv, "sound_out")
            check(status == 200 and msg == {"type": "sound_out", "mode": "headphones"}, f"the player gets sound_out headphones ({status} {msg})")
            check(not of_type(await H.drain(sink), "sound_out"), "... the sink does not")
            status, _ = await post(http, "/api/mtv-sound", {"output": "hdmi"})
            msg, _ = await until(mtv, "sound_out")
            check(status == 200 and msg is not None and msg["mode"] == "hdmi", "hdmi accepted too")

            # ---- the player's output report carries the sound choice, typed
            await mtv.send_json({"type": "output", "name": "TV Player", "volume": 80, "sound": "headphones"})
            await H.drain(mtv)
            check((await H.state(http, "mtv"))["output"]["sound"] == "headphones", "state.output.sound = headphones")
            await mtv.send_json({"type": "output", "name": "TV Player", "volume": 80, "sound": {"x": 1}})
            await H.drain(mtv)
            check((await H.state(http, "mtv"))["output"]["sound"] == "hdmi", "garbage sound -> hdmi")

            # ---- the route: the TV Player's state is replayed into the sink
            await mtv.send_json({"type": "lip_sync", "ms": -100})
            await mtv.send_json({"type": "clock", "item": ids[0], "position": 3.0, "at": 60.0, "state": "paused", "junk": 1})
            await H.drain(sink)
            await mtv.send_json({"type": "route", "sink": True})
            got = await H.drain(sink)
            types = [m["type"] for m in got]
            check(types[0] == "state" and got[0]["state"]["session"] == "mtv", f"the sink gets the TV Player's state first ({types})")
            ld = of_type(got, "load")
            check(ld and ld[0]["item"]["id"] == ids[0] and ld[0]["position"] == 0, "... its current item")
            check(of_type(got, "route") == [{"type": "route", "sink": True}], "... route true")
            snd = of_type(got, "sound")
            check(snd and snd[0] == box_sound.message((await H.state(http, "mtv"))["sound"], (await H.state(http, "mtv"))["current"]),
                  "... the TV Player's own sound message (server-made)")
            check(of_type(got, "lip_sync") == [{"type": "lip_sync", "ms": -100}], "... lip sync")
            ck = of_type(got, "clock")
            check(ck == [{"type": "clock", "item": ids[0], "position": 3.0, "at": 60.0, "state": "paused"}],
                  f"... the last clock, typed (junk dropped) ({ck})")
            check((await H.state(http, "mtv"))["sink"]["route"] is True, "mtv state: route true")
            check(not of_type(await H.drain(tv), "load"), "the Apple TV gets nothing for it")

            # ---- loads, control, sound follow the TV Player; the clock is relayed
            await remote.send_json({"type": "cmd", "cmd": "jump", "index": 2})
            got = await H.drain(sink)
            types = [m["type"] for m in got]
            check(of_type(got, "sound") and of_type(got, "load") and types.index("sound") < types.index("load"), f"a jump: sound, then load to the sink ({types})")
            check(of_type(got, "load")[0]["item"]["id"] == ids[2] and of_type(got, "load")[0]["autoplay"] is True, "... the new item, autoplay")
            await mtv.send_json({"type": "clock", "item": ids[2], "position": 0.4, "at": 61.5, "state": "playing"})
            check(of_type(await H.drain(sink), "clock") == [{"type": "clock", "item": ids[2], "position": 0.4, "at": 61.5, "state": "playing"}],
                  "a clock from the player is relayed")
            for bad in ({"type": "clock", "item": ids[2], "position": 1, "at": 62, "state": "bogus"},
                        {"type": "clock", "item": "x", "position": 1, "at": 62, "state": "playing"},
                        {"type": "clock", "item": ids[2], "position": "x", "at": 62, "state": "playing"},
                        {"type": "clock", "item": ids[2], "position": 1, "at": None, "state": "playing"}):
                await mtv.send_json(bad)
            check(not await H.drain(sink), "a clock with a bad state / item / position / time is dropped")
            await mtv.send_json({"type": "lip_sync", "ms": 99999})
            check(of_type(await H.drain(sink), "lip_sync") == [{"type": "lip_sync", "ms": 400}], "lip sync clamped to +400")
            await mtv.send_json({"type": "lip_sync", "ms": "x"})
            check(not await H.drain(sink), "garbage lip sync dropped")
            await remote.send_json({"type": "cmd", "cmd": "sound", "preset": "t:bass", "boost": 3, "loudness": "off"})
            got = await H.drain(sink)
            check(of_type(got, "sound") and of_type(got, "sound")[0]["gain"] == 2.0, "a sound change reaches the sink")
            await remote.send_json({"type": "cmd", "cmd": "pause"})
            check(of_type(await H.drain(sink), "control")[:1] == [{"type": "control", "action": "pause"}], "pause reaches the sink")
            await H.drain(mtv)

            # ---- the Apple TV's messages no longer reach the sink while the TV Player holds it (choosing atv-audio alone
            # takes nothing; a Play would, see below)
            await tv.send_json({"type": "route", "sink": True})
            await tv.send_json({"type": "clock", "item": ids[3], "position": 1, "at": 70, "state": "playing"})
            got = tv_side(await H.drain(sink))
            check(not got, f"Apple TV route / clock: not forwarded while the TV Player holds the sink ({got})")

            # ---- ready goes to the TV Player
            await H.drain(mtv)
            await H.drain(tv)
            await sink.send_json({"type": "ready", "item": ids[2]})
            rd, _ = await until(mtv, "sink_ready")
            check(rd == {"type": "sink_ready", "id": ids[2]}, f"the sink's ready -> the TV Player ({rd})")
            check(not of_type(await H.drain(tv), "sink_ready"), "... not the Apple TV")

            # ---- volume: the headphones' own
            await H.drain(sink)
            await remote.send_json({"type": "volume", "percent": 55.4, "muted": False})
            got = await H.drain(sink)
            check(of_type(got, "volume") == [{"type": "volume", "percent": 55, "muted": False}], "TV Player slider -> the sink's volume")
            check(not of_type(await H.drain(mtv), "volume"), "... not the player's own mpv volume")
            await remote.send_json({"type": "volume", "percent": "x"})
            check(not await H.drain(sink), "garbage volume dropped")
            await boxremote.send_json({"type": "volume", "percent": 33, "muted": False})
            check(of_type(await H.drain(sink), "volume") == [{"type": "volume", "percent": 33, "muted": False}], "a box remote's volume still reaches the sink")
            # the player's own volume keys / menu: `sink_volume` = the headphones' volume while the sound is routed
            await mtv.send_json({"type": "sink_volume", "percent": 62.4, "muted": True})
            check(of_type(await H.drain(sink), "volume") == [{"type": "volume", "percent": 62, "muted": True}], "player sink_volume -> the sink's volume (typed)")
            await mtv.send_json({"type": "sink_volume", "percent": "x"})
            await mtv.send_json({"type": "sink_volume", "percent": 900})
            got = of_type(await H.drain(sink), "volume")
            check(got == [{"type": "volume", "percent": 100, "muted": False}], f"garbage dropped, 900 clamped to 100 ({got})")
            await mtv.send_json({"type": "volume", "percent": 5})
            check(not of_type(await H.drain(sink), "volume"), "a plain `volume` from the player is still not relayed")
            await remote.send_json({"type": "volume", "percent": 20, "muted": False})
            await H.drain(sink)
            await sink.send_json({"type": "sink", "name": "atv-audio", "output": dict(READY, volume=20)})
            await H.drain(mtv)

            # ---- the box yields to the TV Player while it plays through the sink
            await tvremote.send_json({"type": "cmd", "cmd": "pause"})
            await tv.send_json({"type": "route", "sink": False})
            await H.drain(tv)
            await remote.send_json({"type": "cmd", "cmd": "play"})
            await mtv.send_json({"type": "status", "item": ids[2], "playing": True, "position": 1.0, "duration": 4})
            await H.drain(mtv)
            await boxremote.send_json({"type": "cmd", "cmd": "play_ids", "ids": ids})
            await H.drain(box)
            st = await H.state(http, "box")
            check(st["notice"] and st["notice"]["text"] == "atv-audio is in use by the TV Player: paused", f"box refused with the TV Player's name ({st['notice']})")
            check(st["sink"]["in_use_by_tv"] is True and st["sink"]["in_use_by"] == "TV Player", "box state names who uses atv-audio")

            # ---- headphone buttons act on the TV Player
            await H.drain(mtv)
            await sink.send_json({"type": "button", "cmd": "pause", "session": "tv"})
            ctl, _ = await until(mtv, "control")
            check(ctl is not None and ctl["action"] == "pause", f"a headphone button -> the TV Player ({ctl})")

            # ---- the sink's output lost / back reaches the TV Player
            await H.drain(mtv)
            await sink.send_json({"type": "sink", "name": "atv-audio", "output": dict(READY, ready=False)})
            got = await H.drain(mtv)
            lost = [m for m in of_type(got, "state") if m["state"]["sink"] and m["state"]["sink"]["output"]["ready"] is False]
            check(lost, "output lost: the TV Player gets a state with the output not ready")
            await sink.send_json({"type": "sink", "name": "atv-audio", "output": READY})
            got = await H.drain(mtv)
            check([m for m in of_type(got, "state") if m["state"]["sink"]["output"]["ready"] is True], "output back: ready again")

            # ---- both on the headphones: the last one to press Play takes them, the other is paused with a notice
            await remote.send_json({"type": "cmd", "cmd": "play"})
            await mtv.send_json({"type": "status", "item": ids[2], "playing": True, "position": 1.5, "duration": 4})
            await H.drain(mtv)
            await tv.send_json({"type": "route", "sink": True})
            await H.drain(tv)
            got = tv_side(await H.drain(sink))
            check(not got, f"the Apple TV choosing atv-audio while the TV Player plays: the sink stays with the TV Player ({got})")
            await tvremote.send_json({"type": "cmd", "cmd": "play_ids", "ids": ids[:1]})
            await until(tv, "load")
            got = await H.drain(sink)
            check(got and got[0]["type"] == "state" and got[0]["state"]["session"] == "tv",
                  f"Apple TV Play: the sink follows the Apple TV ({[m['type'] for m in got]})")
            check(of_type(got, "load") and of_type(got, "load")[-1]["item"]["id"] == ids[0], "... with the Apple TV's item")
            seen = await H.drain(mtv)
            check({"type": "control", "action": "pause"} in seen, "the TV Player is paused")
            nt = of_type(seen, "notice")
            check(nt and nt[0]["text"] == "The headphones went to the Apple TV — playback paused", f"... and shows the notice ({nt})")
            st = await H.state(http, "mtv")
            check(st["notice"] and "Apple TV" in st["notice"]["text"] and st["sink"]["route"] is False and st["want_play"] is False
                  if "want_play" in st else st["notice"] and "Apple TV" in st["notice"]["text"] and st["sink"]["route"] is False,
                  f"web remote: the TV Player's notice, its route shows false ({st['notice']}, {st['sink']['route']})")
            await sink.send_json({"type": "ready", "item": ids[0]})
            rd, _ = await until(tv, "sink_ready")
            check(rd == {"type": "sink_ready", "id": ids[0]}, f"the sink's ready -> the Apple TV now ({rd})")
            await mtv.send_json({"type": "status", "item": ids[2], "playing": True, "position": 1.6, "duration": 4})
            await H.drain(mtv)
            got = await H.drain(sink)
            check(not [m for m in got if m["type"] == "state" and m["state"]["session"] == "mtv"],
                  f"a TV Player status still in flight from before its pause takes nothing back (guard) ({[m['type'] for m in got]})")
            await mtv.send_json({"type": "status", "item": ids[2], "playing": False, "position": 1.6, "duration": 4})
            await tv.send_json({"type": "status", "item": ids[0], "playing": True, "position": 0.2, "duration": 4})
            await H.drain(mtv)
            await H.drain(tv)
            await H.drain(sink)
            await asyncio.sleep(2.1)  # past the guard
            await remote.send_json({"type": "cmd", "cmd": "play"})
            got = await H.drain(sink)
            check(got and got[0]["type"] == "state" and got[0]["state"]["session"] == "mtv",
                  f"TV Player Play: it takes the headphones back ({[m['type'] for m in got]})")
            seen = await H.drain(tv)
            nt = of_type(seen, "notice")
            check({"type": "control", "action": "pause"} in seen and nt and nt[0]["text"] == "The headphones went to the TV Player — playback paused",
                  f"the Apple TV is paused with the notice ({nt})")
            await tv.send_json({"type": "status", "item": ids[0], "playing": False, "position": 0.3, "duration": 4})
            await H.drain(tv)
            await tv.send_json({"type": "sink_codec", "codec": "aptx_adaptive"})
            nt, _ = await until(tv, "notice")
            check(nt and "in use by the TV Player" in nt["text"], f"Apple TV codec request while the TV Player holds them: refused ({nt})")
            check(not of_type(await H.drain(sink), "audio_cmd"), "... nothing sent to the sink")
            await tv.send_json({"type": "route", "sink": False})
            await H.drain(tv)
            got = tv_side(await H.drain(sink))
            check(not got, f"the Apple TV leaves atv-audio: the TV Player keeps the headphones ({got})")

            # ---- route false: the sink follows the Apple TV again; the output rule (HDMI = EQ off + Boost 0)
            await remote.send_json({"type": "cmd", "cmd": "pause"})
            await H.drain(mtv)
            await H.drain(sink)
            await mtv.send_json({"type": "route", "sink": False})
            got = await H.drain(sink)
            rule_sent = of_type(await H.drain(mtv), "sound")
            types = [m["type"] for m in got]
            check(types and types[0] == "state" and got[0]["state"]["session"] == "tv", f"route false: the sink gets the Apple TV's state again ({types})")
            check(of_type(got, "route") and of_type(got, "route")[-1]["sink"] is False, "... and the Apple TV's route (false)")
            await remote.send_json({"type": "cmd", "cmd": "next"})
            await H.drain(mtv)
            check(not [m for m in await H.drain(sink) if m["type"] in ("load", "control")], "the TV Player's next is no longer sent to the sink")
            await mtv.send_json({"type": "sink_volume", "percent": 70, "muted": False})
            check(not of_type(await H.drain(sink), "volume"), "sink_volume while the sound is on HDMI is ignored (it is not the headphones')")
            snd = (await H.state(http, "mtv"))["sound"]
            check(snd["preset"] == "off" and snd["boost"] == 0 and snd["loudness"] == "off" and snd["eq_last"] == "t:bass",
                  f"output rule, HDMI: equalizer off, boost 0, loudness as it was ({snd})")
            check(rule_sent and rule_sent[-1]["processed"] is False, f"... the player gets the unprocessed sound ({rule_sent})")
            await mtv.send_json({"type": "route", "sink": True})
            snd = (await H.state(http, "mtv"))["sound"]
            check(snd["preset"] == "t:bass" and snd["boost"] == 0, f"output rule, headphones: the last equalizer back on ({snd})")
            await mtv.send_json({"type": "route", "sink": True})
            await remote.send_json({"type": "cmd", "cmd": "sound", "preset": "off"})
            await H.drain(remote)
            await mtv.send_json({"type": "route", "sink": True})
            check((await H.state(http, "mtv"))["sound"]["preset"] == "off",
                  "a repeated route (reconnect) is no change: an equalizer turned off on the headphones stays off")
            await mtv.send_json({"type": "route", "sink": False})
            await H.drain(sink)

            # ---- the player leaves while routed: the sink goes back to the Apple TV
            await mtv.send_json({"type": "route", "sink": True})
            await H.drain(sink)
            await mtv.close()
            got = await H.drain(sink)
            check(got and got[0]["type"] == "state" and got[0]["state"]["session"] == "tv", f"the TV Player left: the sink follows the Apple TV ({[m['type'] for m in got]})")
            check(of_type(got, "load") and of_type(got, "load")[-1]["item"]["id"] == ids[0] and of_type(got, "route") == [{"type": "route", "sink": False}],
                  "... its item is loaded again and the route is the Apple TV's (false): the whole state is replayed, not just a broadcast")
            check((await H.state(http, "mtv"))["players"] == 0, "no TV Player")
            status, _ = await post(http, "/api/mtv-sound", {"output": "hdmi"})
            check(status == 503, "the choice with no TV Player running -> 503")

            # ---- codec from the Apple TV (Settings, it holds the headphones and is paused): set_codec to the sink
            await tv.send_json({"type": "route", "sink": True})
            await tv.send_json({"type": "status", "item": ids[0], "playing": True, "position": 0.5, "duration": 4})
            await H.drain(tv)
            await H.drain(sink)
            await tv.send_json({"type": "sink_codec", "codec": "aptx_adaptive"})
            nt, _ = await until(tv, "notice")
            check(nt and "pause first" in nt["text"], f"codec while the Apple TV plays: refused ({nt})")
            await tv.send_json({"type": "status", "item": ids[0], "playing": False, "position": 0.6, "duration": 4})
            await H.drain(tv)
            await tv.send_json({"type": "sink_codec", "codec": "ldac"})
            nt, _ = await until(tv, "notice")
            check(nt and "unknown codec" in nt["text"], "an unknown codec: refused")
            await H.drain(sink)
            await tv.send_json({"type": "sink_codec", "codec": "aptx_adaptive"})
            cmd, _ = await until(sink, "audio_cmd")
            check(cmd == {"type": "audio_cmd", "op": "set_codec", "output": "aptx_adaptive"}, f"paused: set_codec reaches the sink ({cmd})")
            await tv.send_json({"type": "route", "sink": False})
            await H.drain(tv)

            # ---- Stop (and Play after it)
            mtv = await H.connect(http, "role=player&kind=mtv")
            remote = await H.connect(http, "role=remote&session=mtv")
            await remote.send_json({"type": "cmd", "cmd": "play_ids", "ids": ids})
            await until(mtv, "load")
            await mtv.send_json({"type": "status", "item": ids[0], "playing": True, "position": 2.5, "duration": 4})
            await H.drain(mtv)
            await H.drain(remote)
            await remote.send_json({"type": "cmd", "cmd": "stop"})
            ctl, _ = await until(mtv, "control")
            check(ctl == {"type": "control", "action": "stop"}, f"Stop -> control stop to the player ({ctl})")
            st = await H.state(http, "mtv")
            check(st["playing"] is False and st["index"] == 0 and st["queue"] == ids and st["position"] == 0.0 and st["current"]["id"] == ids[0],
                  "stopped: not playing, the item stays current, position 0")
            await remote.send_json({"type": "cmd", "cmd": "pause"})
            check(not of_type(await H.drain(mtv), "load"), "Pause after a Stop does not restart it")
            await remote.send_json({"type": "cmd", "cmd": "toggle"})
            ld, seen = await until(mtv, "load")
            check(ld is not None and ld["item"]["id"] == ids[0] and ld["autoplay"] is True and ld["position"] == 0.0,
                  f"Play after a Stop: the item loads again from 0:00 ({ld and (ld['item']['id'], ld['autoplay'], ld['position'])})")
            check(of_type(seen, "sound"), "... with its sound message first")
            await mtv.send_json({"type": "status", "item": ids[0], "playing": True, "position": 0.3, "duration": 4})
            await H.drain(mtv)
            await remote.send_json({"type": "cmd", "cmd": "stop"})
            await until(mtv, "control")
            await remote.send_json({"type": "cmd", "cmd": "next"})
            ld, _ = await until(mtv, "load")
            check(ld is not None and ld["item"]["id"] == ids[1] and ld["autoplay"], "Next after a Stop plays the next item")
            await remote.send_json({"type": "cmd", "cmd": "stop"})
            await until(mtv, "control")
            await remote.send_json({"type": "cmd", "cmd": "play"})
            ld, _ = await until(mtv, "load")
            check(ld is not None and ld["item"]["id"] == ids[1] and ld["position"] == 0.0, "Play (not toggle) after a Stop restarts it too")
            await H.drain(mtv)
            await boxremote.send_json({"type": "cmd", "cmd": "stop"})
            check(not of_type(await H.drain(box), "control"), "stop is the TV Player's: the box ignores it")
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="atv-mtvsink-"))
    try:
        asyncio.run(main(tmp))
    except Exception:
        import traceback
        traceback.print_exc()
        log = tmp / "server.log"
        print(log.read_text()[-3000:] if log.exists() else "")
        failed += 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASSED" if not failed else f"FAILED {failed}")
    sys.exit(1 if failed else 0)
