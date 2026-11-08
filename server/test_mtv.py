#!/usr/bin/env python3
"""The TV Player's own queue (session "mtv"), server side: listed and named, its own queue and state file, the mtv
player gets its `sound` before every load, the web remote sets its sound (independent of the box's), play/pause are
sent as explicit actions, notices from the player, sound settings in the backup. Unlike "box" it never yields to the
Apple TV: a TV playing through the atv-audio sink neither pauses it nor refuses its Play.
Runs a THROWAWAY server instance (own temp library, data dir and port; never the real one).
    python3 test_mtv.py   (needs ffmpeg + the server's deps)
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
from test_box import until, of_type  # noqa: E402

PORT = 18101
H.PORT, H.BASE = PORT, f"http://127.0.0.1:{PORT}"
H.WS = H.BASE.replace("http", "ws")
failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


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

            # ---- the session exists, is named, has server-side sound and no sink
            st = await H.state(http, "mtv")
            check(st["session"] == "mtv" and st["session_name"] == "TV Player", "state of session mtv, named TV Player")
            check(st["sink"] is None and st["sound"] == box_sound.DEFAULT and st["notice"] is None,
                  "mtv: no sink, default sound settings, no notice")
            check([x["session"] for x in st["sessions"]] == ["tv", "phone", "box", "mtv"], "sessions list has mtv last")
            check("sound" not in await H.state(http, "tv") and "sound" not in await H.state(http, "phone"),
                  "TV and iPhone sessions still carry no server-side sound")

            mtv = await H.connect(http, "role=player&kind=mtv")
            remote = await H.connect(http, "role=remote&session=mtv")
            box = await H.connect(http, "role=player&kind=box")
            check((await H.state(http, "mtv"))["players"] == 1, "mtv player counted in its session")
            summ = {x["session"]: x for x in (await H.state(http, "tv"))["sessions"]}
            check(summ["mtv"]["players"] == 1 and summ["box"]["players"] == 1, "sessions summary: mtv and box players")

            # ---- play from the remote: sound, then load, only to the mtv player
            await remote.send_json({"type": "cmd", "cmd": "play_ids", "ids": ids})
            load, seen = await until(mtv, "load")
            types = [m["type"] for m in seen]
            check(load is not None and load["item"]["id"] == ids[0] and load["autoplay"] is True, "mtv player gets the load")
            check("sound" in types and types.index("sound") < types.index("load"), f"sound before load ({types})")
            check(not of_type(await H.drain(box), "load"), "box player gets nothing from the mtv queue")
            await mtv.send_json({"type": "status", "item": ids[0], "playing": True, "position": 1.0, "duration": 4})
            await H.drain(mtv)
            st = await H.state(http, "mtv")
            check(st["playing"] is True and st["queue"] == ids, "mtv status playing -> state playing, own queue")
            saved = json.loads((tmp / "data" / "state-mtv.json").read_text())
            check(saved["queue"] == ids, "queue saved in state-mtv.json")
            check((await H.state(http, "box"))["queue"] == [] and (await H.state(http, "tv"))["queue"] == [],
                  "box and TV queues untouched")

            # ---- sound settings: own, saved, sent to the player; the box keeps its own
            await remote.send_json({"type": "cmd", "cmd": "sound", "preset": "p:2", "boost": 3, "loudness": "off"})
            snd, _ = await until(mtv, "sound")
            check(snd is not None and snd["gain"] == -3.0 and snd["bands"][2] == 8.0,
                  f"mtv gets the new sound (Club preamp -6 + boost 3 = -3 dB): {snd and snd['gain']}")
            await H.drain(remote)
            check((await H.state(http, "mtv"))["sound"]["boost"] == 3, "mtv sound saved in its state")
            check((await H.state(http, "box"))["sound"] == box_sound.DEFAULT, "box sound unchanged")
            check(not of_type(await H.drain(box), "sound"), "box player gets no sound message for an mtv change")

            # ---- play/pause as explicit actions
            await remote.send_json({"type": "cmd", "cmd": "toggle"})
            ctl, _ = await until(mtv, "control")
            check(ctl is not None and ctl["action"] == "pause", f"toggle while playing -> pause ({ctl})")
            await mtv.send_json({"type": "status", "item": ids[0], "playing": False, "position": 1.5, "duration": 4})
            await H.drain(mtv)

            # ---- the Apple TV playing through the atv-audio sink does not affect mtv
            sink = await http.ws_connect(f"{H.WS}/ws?role=sink&session=tv")
            await H.drain(sink)
            await sink.send_json({"type": "sink", "name": "atv-audio", "output": {
                "kind": "bluetooth", "name": "Headphones X", "codec": "aptx_hd", "ready": True}})
            await H.drain(sink)
            tv = await H.connect(http, "role=player&kind=tv")
            tvremote = await H.connect(http, "role=remote&session=tv")
            await tv.send_json({"type": "route", "sink": True})
            await tvremote.send_json({"type": "cmd", "cmd": "play_ids", "ids": ids[3:]})
            await until(tv, "load")
            await tv.send_json({"type": "status", "item": ids[3], "playing": True, "position": 0.5, "duration": 4})
            await H.drain(tv)
            check(not of_type(await H.drain(mtv), "control"), "TV takes the sink: mtv gets no pause")
            await remote.send_json({"type": "cmd", "cmd": "toggle"})
            ctl, _ = await until(mtv, "control")
            check(ctl is not None and ctl["action"] == "play", f"mtv Play while the TV plays via the sink -> play ({ctl})")
            await H.drain(remote)
            check((await H.state(http, "mtv"))["notice"] is None, "... and no in-use notice")

            # ---- notice from the mtv player, dismiss
            await mtv.send_json({"type": "notice", "text": "Screen disconnected: paused"})
            await H.drain(mtv)
            check((await H.state(http, "mtv"))["notice"]["text"] == "Screen disconnected: paused", "mtv notice shown")
            await remote.send_json({"type": "cmd", "cmd": "dismiss"})
            await H.drain(remote)
            check((await H.state(http, "mtv"))["notice"] is None, "dismiss clears it")

            # ---- reconnect: sound before load again
            await mtv.close()
            await H.drain(remote)
            check((await H.state(http, "mtv"))["players"] == 0, "mtv player gone -> 0 players")
            check((await H.state(http, "mtv"))["output"] is None, "... and its output is forgotten")
            mtv = await http.ws_connect(f"{H.WS}/ws?role=player&kind=mtv")
            types = [m["type"] for m in await H.drain(mtv)]
            check("sound" in types and "load" in types and types.index("sound") < types.index("load"),
                  f"mtv reconnect: sound before load ({types})")

            # ---- the TV Player's output: reported by the player, in the state; the web remote's volume reaches it
            check((await H.state(http, "mtv")).get("output") is None, "no output reported yet -> state.output null")
            check("output" not in await H.state(http, "tv") and "output" not in await H.state(http, "box"),
                  "TV and box states carry no output")
            await mtv.send_json({"type": "output", "name": "TV Player", "screen": "3840x2160 @ 60 Hz",
                                 "device": "alsa/hdmi", "volume": 72.6, "muted": False, "version": "0.4.0", "junk": 1})
            await H.drain(mtv)
            out = (await H.state(http, "mtv")).get("output")
            check(out == {"name": "TV Player", "screen": "3840x2160 @ 60 Hz", "device": "alsa/hdmi", "volume": 73,
                          "muted": False, "version": "0.4.0"}, f"output in the state, typed and bounded ({out})")
            await mtv.send_json({"type": "output", "name": "x" * 500, "volume": 900})
            await H.drain(mtv)
            out = (await H.state(http, "mtv"))["output"]
            check(out["volume"] == 100 and len(out["name"]) == 60, "output values clamped")
            await H.drain(mtv)
            await remote.send_json({"type": "volume", "percent": 41.2, "muted": True})
            vol, _ = await until(mtv, "volume")
            check(vol == {"type": "volume", "percent": 41, "muted": True}, f"web remote volume -> the mtv player ({vol})")
            await remote.send_json({"type": "volume", "percent": "x"})
            vol, _ = await until(mtv, "volume", 0.6)
            check(vol is None, "garbage volume not relayed")
            sinkvol = [m for m in await H.drain(sink) if m.get("type") == "volume"]
            check(not sinkvol, "an mtv volume never reaches the atv-audio sink")
            await mtv.send_json({"type": "volume", "percent": 5})  # a player cannot set the volume this way
            check(not [m for m in await H.drain(mtv) if m.get("type") == "volume"]
                  and not [m for m in await H.drain(sink) if m.get("type") == "volume"],
                  "volume sent BY the mtv player is not relayed (to itself or the sink)")
            # a volume from a box remote still reaches the sink, as before
            boxremote = await H.connect(http, "role=remote&session=box")
            await boxremote.send_json({"type": "volume", "percent": 33, "muted": False})
            v2, _ = await until(sink, "volume")
            check(v2 is not None and v2["percent"] == 33, "box remote volume still reaches the sink")
            await boxremote.close()

            # ---- remote profiles of the player's Flirc: reported in the output, commands from the web remote reach the player
            await mtv.send_json({"type": "output", "name": "TV Player", "volume": 100, "remote": {
                "can": True, "profiles": ["Vip STB", "Other"] + ["x"] * 60, "active": ["Vip STB", "Other"], "junk": 1,
                "progress": {"state": "learning", "name": "Other", "index": 6, "total": 8, "label": "Next", "optional": 1,
                             "note": "n" * 300, "text": "t" * 500}}})
            await H.drain(mtv)
            rem = (await H.state(http, "mtv"))["output"].get("remote")
            check(rem and rem["can"] and rem["active"] == ["Vip STB", "Other"] and rem["profiles"][:2] == ["Vip STB", "Other"]
                  and len(rem["profiles"]) == 40, f"output.remote typed, profile list bounded ({rem and rem['profiles'][:3]})")
            check(rem["progress"] == {"state": "learning", "name": "Other", "text": "t" * 160, "index": 6, "total": 8,
                                      "label": "Next", "optional": True, "note": "n" * 80}, "progress typed and bounded")
            await mtv.send_json({"type": "output", "name": "TV Player", "volume": 100, "remote": {"can": True, "progress": {
                "state": "testing", "name": "T", "phase": "test2", "expected": ["Play / Pause"] + ["f" * 40] * 20,
                "lit": ["Play / Pause"], "strays": ["Down x2", "green"], "problems": ["p" * 400] * 9, "junk": 1}}})
            await H.drain(mtv)
            pr = (await H.state(http, "mtv"))["output"]["remote"]["progress"]
            check(pr["state"] == "testing" and pr["phase"] == "test2" and pr["lit"] == ["Play / Pause"]
                  and pr["strays"] == ["Down x2", "green"] and len(pr["expected"]) == 12 and len(pr["expected"][1]) == 16
                  and len(pr["problems"]) == 4 and len(pr["problems"][0]) == 160 and "junk" not in pr,
                  f"testing progress typed and bounded ({pr['state']}, {len(pr['expected'])} expected)")
            check(pr.get("end") == "", "testing progress: no end key given -> empty")
            await mtv.send_json({"type": "output", "name": "TV Player", "volume": 100, "remote": {"can": True, "progress": {
                "state": "testing", "name": "T", "phase": "test2", "end": "Play / Pause" + "x" * 40}}})
            await H.drain(mtv)
            pr = (await H.state(http, "mtv"))["output"]["remote"]["progress"]
            check(pr["end"] == ("Play / Pause" + "x" * 40)[:16], f"the key that ends step 2 is passed on, bounded ({pr['end']})")
            # the overview: the player's functions, per profile the ones with a button and the names typed for them
            fns = ["Play / Pause", "Menu", "Up", "Down", "Left", "Right", "Next", "Previous", "Back"]
            await mtv.send_json({"type": "output", "name": "TV Player", "volume": 100, "remote": {
                "can": True, "profiles": ["TotalTV", "STB Vip"], "active": ["TotalTV"], "functions": fns + ["f" * 40] * 20,
                "details": {"TotalTV": {"set": ["Play / Pause", "Up", "Nope"], "names": {"Play / Pause": "OK" + "y" * 50, "Nope": "x", "Up": ""}},
                            "STB Vip": "junk", "Ghost": {"set": ["Up"], "names": {}}}}})
            await H.drain(mtv)
            rem = (await H.state(http, "mtv"))["output"]["remote"]
            check(rem["functions"][:9] == fns and len(rem["functions"]) == 16 and len(rem["functions"][9]) == 16, "functions typed and bounded")
            check(rem["details"]["TotalTV"] == {"set": ["Play / Pause", "Up"], "names": {"Play / Pause": ("OK" + "y" * 50)[:30]}},
                  f"details: only known functions, names bounded, empty names dropped ({rem['details']['TotalTV']})")
            check(rem["details"]["STB Vip"] == {"set": [], "names": {}} and "Ghost" not in rem["details"],
                  "details: broken entry -> empty, a profile not in the list is dropped")
            await mtv.send_json({"type": "output", "name": "TV Player", "volume": 100, "remote": {"can": True, "profiles": ["A"]}})
            await H.drain(mtv)
            rem = (await H.state(http, "mtv"))["output"]["remote"]
            check(rem["functions"] == [] and rem["details"] == {"A": {"set": [], "names": {}}}, "an older player without details: empty overview")
            await mtv.send_json({"type": "output", "name": "TV Player", "volume": 100, "remote": {"can": True, "progress": {
                "state": "review", "name": "T", "phase": "bogus", "problems": ["no answer from Up"]}}})
            await H.drain(mtv)
            pr = (await H.state(http, "mtv"))["output"]["remote"]["progress"]
            check(pr["state"] == "review" and pr["phase"] == "" and pr["problems"] == ["no answer from Up"], f"review progress ({pr})")
            await mtv.send_json({"type": "output", "name": "TV Player", "volume": 100, "remote": {"progress": {"state": "x"}}})
            await H.drain(mtv)
            rem = (await H.state(http, "mtv"))["output"]["remote"]
            check(rem["progress"]["state"] == "idle" and rem["profiles"] == [] and rem["active"] == [] and not rem["can"],
                  "unknown progress state -> idle, empty lists")
            await mtv.send_json({"type": "output", "name": "TV Player", "volume": 100, "remote": {"can": True, "active": "Single", "profiles": ["Single"]}})
            await H.drain(mtv)
            check((await H.state(http, "mtv"))["output"]["remote"]["active"] == ["Single"], "an old player's single active name becomes a list")
            await mtv.send_json({"type": "output", "name": "TV Player", "volume": 100, "remote": {"can": True, "active": ["a"] * 60, "profiles": []}})
            await H.drain(mtv)
            check(len((await H.state(http, "mtv"))["output"]["remote"]["active"]) == 40, "the active list is bounded")
            await mtv.send_json({"type": "output", "name": "TV Player", "volume": 100})
            await H.drain(mtv)
            check("remote" not in (await H.state(http, "mtv"))["output"], "a player without a learnable receiver reports no remote")
            await H.drain(mtv)
            for body in ({"op": "learn", "name": "Cambridge"}, {"op": "use", "name": "Vip STB"}, {"op": "add", "name": "TotalTV"},
                         {"op": "remove", "name": "TotalTV"},
                         {"op": "delete", "name": "Old"}, {"op": "cancel"}, {"op": "skip"}, {"op": "keep"},
                         {"op": "discard"}, {"op": "label", "name": "TotalTV", "function": "Play / Pause", "text": "OK"},
                         {"op": "label", "name": "TotalTV", "function": "Menu", "text": ""},
                         {"op": "label", "name": "TotalTV", "function": "Up", "text": "u" * 100}):
                async with http.post(f"{H.BASE}/api/mtv-remote", json=body) as r:
                    check(r.status == 200, f"POST /api/mtv-remote {body['op']} -> {r.status}")
                got, _ = await until(mtv, "remote")
                want = {"type": "remote", "op": body["op"], **({"name": body["name"]} if "name" in body else {}),
                        **({"function": body["function"], "text": body["text"][:60]} if body["op"] == "label" else {})}
                check(got == want, f"mtv player gets {want} ({got})")
            for body, what in (({"op": "format", "name": "x"}, "unknown op"), ({"op": "learn"}, "learn without a name"), ({"op": "add"}, "add without a name"),
                               ({"op": "use", "name": "x" * 80}, "name too long"), ({"op": "use", "name": 5}, "name not text"),
                               ({"op": "label", "name": "A", "text": "x"}, "label without a function"),
                               ({"op": "label", "name": "A", "function": "Up", "text": 5}, "label text not text"),
                               ({"op": "label", "name": "A", "function": "Up", "text": "x" * 300}, "label text too long"),
                               ({"op": "label", "function": "Up", "text": "x"}, "label without a profile")):
                async with http.post(f"{H.BASE}/api/mtv-remote", json=body) as r:
                    check(r.status == 400, f"{what} -> 400 ({r.status})")
            got, _ = await until(mtv, "remote", 0.6)
            check(got is None, "refused requests reach no player")
            check(not [m for m in await H.drain(sink) if m.get("type") == "remote"], "remote commands never reach the atv-audio sink")

            # ---- backup carries mtv_sound; restore applies it (and an old backup without it changes nothing)
            async with http.get(f"{H.BASE}/api/backup") as r:
                backup = await r.json()
            check(backup.get("mtv_sound", {}).get("boost") == 3, "backup has mtv_sound")
            async with http.post(f"{H.BASE}/api/backup", json={**backup, "mtv_sound": {**backup["mtv_sound"], "boost": 5}}) as r:
                check(r.status == 200, f"restore accepted ({r.status})")
            check((await H.state(http, "mtv"))["sound"]["boost"] == 5, "restore sets the mtv sound")
            old = {k: v for k, v in backup.items() if k != "mtv_sound"}
            async with http.post(f"{H.BASE}/api/backup", json=old) as r:
                check(r.status == 200, "old backup without mtv_sound accepted")
            check((await H.state(http, "mtv"))["sound"]["boost"] == 5, "... mtv sound kept")
            for ws in (mtv, remote, box, sink, tv, tvremote):
                await ws.close()
    finally:
        proc.terminate()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="atv-mtvtest-"))
    try:
        asyncio.run(main(tmp))
    except Exception:
        log = tmp / "server.log"
        print(log.read_text()[-3000:] if log.exists() else "no server log")
        raise
    finally:
        if failed:
            print((tmp / "server.log").read_text()[-3000:] if (tmp / "server.log").exists() else "")
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{'FAILED ' + str(failed) if failed else 'ALL PASSED'}")
    sys.exit(1 if failed else 0)
