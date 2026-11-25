#!/usr/bin/env python3
"""LTVPlayer's own queue (session "ltv"), server side: listed and named, its own queue and state file, "prefer videos" on
for a fresh session, a plain player (load / control / status / ended / error, no `sound`, no sink, no Stop, no volume),
never affected by the Apple TV on the headphones, and its page files under /static/ltv/ revalidated on every load.
Runs a THROWAWAY server instance from a temp copy of this directory (own library, data dir and port; never the real one).
    python3 test_ltv.py   (needs ffmpeg + the server's deps)
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
import test_atv_audio as H  # noqa: E402  (helpers only; its main() does not run on import)
from test_box import until, of_type  # noqa: E402

PORT = 18120
H.PORT, H.BASE = PORT, f"http://127.0.0.1:{PORT}"
H.WS = H.BASE.replace("http", "ws")
failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


async def status(ws, item, playing, position, duration=4):
    await ws.send_json({"type": "status", "item": item, "playing": playing, "position": position, "duration": duration})


async def main(tmp):
    lib = tmp / "lib"
    for n in range(1, 5):
        H.tone(lib / "songs" / f"0{n} Song{n}.flac", 4, 300 + 40 * n)
    # the server serves STATIC next to server.py: run a copy that holds a page under static/ltv/ (the repo stays as is)
    srv = tmp / "srv"
    shutil.copytree(HERE, srv, ignore=shutil.ignore_patterns("__pycache__", "site", "test_*", "sink"))
    (srv / "static" / "ltv").mkdir(parents=True, exist_ok=True)
    (srv / "static" / "ltv" / "index.html").write_text("<!doctype html><title>t</title>\n")
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(tmp / "data"),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT))
    env.pop("ATV_BONJOUR", None)
    proc = subprocess.Popen([sys.executable, str(srv / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)
    try:
        async with aiohttp.ClientSession() as http:
            check(len(await H.wait_up(http, 4)) == 4, "throwaway library has 4 songs")
            ids = sorted(it["id"] for it in (await H.library(http)).values())

            # ---- the session exists, is named, has no sink and no server-side sound; prefer videos on when fresh
            st = await H.state(http, "ltv")
            check(st["session"] == "ltv" and st["session_name"] == "LTVPlayer", "state of session ltv, named LTVPlayer")
            check(st["sink"] is None, f"ltv: sink is null ({st['sink']!r})")
            check("sound" not in st and "notice" not in st and "output" not in st,
                  "ltv: no server-side sound, notice or output fields")
            check([x["session"] for x in st["sessions"]] == ["tv", "phone", "box", "mtv", "ltv"],
                  "sessions list ends with ltv")
            check(st["prefer_videos"] is True, "a fresh ltv session has prefer videos on")
            check((await H.state(http, "tv"))["prefer_videos"] is False
                  and (await H.state(http, "mtv"))["prefer_videos"] is False, "other fresh sessions keep it off")

            # ---- a player joins: counted, gets the state and no load while the queue is empty
            ltv = await http.ws_connect(f"{H.WS}/ws?role=player&kind=ltv")
            first = await H.drain(ltv)
            check(first and first[0]["type"] == "state" and not of_type(first, "load"),
                  f"empty queue: the player gets the state, no load ({[m['type'] for m in first]})")
            remote = await H.connect(http, "role=remote&session=ltv")
            box = await H.connect(http, "role=player&kind=box")
            check((await H.state(http, "ltv"))["players"] == 1, "ltv player counted in its session")
            summ = {x["session"]: x for x in (await H.state(http, "tv"))["sessions"]}
            check(summ["ltv"]["players"] == 1 and summ["ltv"]["name"] == "LTVPlayer", "sessions summary: ltv player")

            # ---- a remote starts a collection: load (autoplay, present), no sound message, only to ltv
            await remote.send_json({"type": "cmd", "cmd": "play_collection", "collection": "music"})
            load, seen = await until(ltv, "load")
            types = [m["type"] for m in seen]
            check(load is not None and load["autoplay"] is True and load["present"] is True
                  and load["item"]["id"] in ids, f"play_collection -> load, autoplay + present ({load and load['item']['id']})")
            check("sound" not in types, f"no sound message before the load ({types})")
            check(not of_type(await H.drain(box), "load"), "box player gets nothing from the ltv queue")
            st = await H.state(http, "ltv")
            check(sorted(st["queue"]) == ids, "ltv queue holds the collection")
            cur = st["current"]["id"]
            await status(ltv, cur, True, 1.0)
            await H.drain(ltv)
            st = await H.state(http, "ltv")
            check(st["playing"] is True and abs(st["position"] - 1.0) < 0.01, "ltv status playing -> state playing")
            saved = json.loads((tmp / "data" / "state-ltv.json").read_text())
            check(saved["queue"] == st["queue"] and saved["prefer_videos"] is True, "queue saved in state-ltv.json")
            check((await H.state(http, "box"))["queue"] == [] and (await H.state(http, "tv"))["queue"] == []
                  and (await H.state(http, "mtv"))["queue"] == [], "box, TV and mtv queues untouched")

            # ---- commands from the remote reach the player as control / load
            await remote.send_json({"type": "cmd", "cmd": "pause"})
            ctl, _ = await until(ltv, "control")
            check(ctl is not None and ctl["action"] == "pause", f"cmd pause -> control pause ({ctl})")
            await status(ltv, cur, False, 1.2)
            await remote.send_json({"type": "cmd", "cmd": "play"})
            ctl, _ = await until(ltv, "control")
            check(ctl is not None and ctl["action"] == "play", f"cmd play -> control play ({ctl})")
            await remote.send_json({"type": "cmd", "cmd": "seek", "position": 2.5})
            ctl, _ = await until(ltv, "control")
            check(ctl is not None and ctl["action"] == "seek" and ctl["position"] == 2.5, f"cmd seek -> control seek ({ctl})")
            await remote.send_json({"type": "cmd", "cmd": "next"})
            load, _ = await until(ltv, "load")
            st = await H.state(http, "ltv")
            check(load is not None and load["item"]["id"] == st["queue"][1] and load["present"] is False,
                  "cmd next -> load of the second entry, not presented")
            await remote.send_json({"type": "cmd", "cmd": "prev", "position": 1.0})
            load, _ = await until(ltv, "load")
            check(load is not None and load["item"]["id"] == st["queue"][0], "cmd prev (at 1 s) -> load of the first entry")
            await remote.send_json({"type": "cmd", "cmd": "prev", "position": 8.0})
            ctl, _ = await until(ltv, "control")
            check(ctl is not None and ctl["action"] == "seek" and ctl["position"] == 0, "cmd prev (at 8 s) -> restart")
            await remote.send_json({"type": "cmd", "cmd": "jump", "index": 2})
            load, _ = await until(ltv, "load")
            check(load is not None and load["item"]["id"] == st["queue"][2] and load["present"] is True,
                  "cmd jump -> load of that entry, presented")

            # ---- the player's own commands work the same way (it is player and remote in one)
            await ltv.send_json({"type": "cmd", "cmd": "next"})
            load, _ = await until(ltv, "load")
            check(load is not None and load["item"]["id"] == st["queue"][3], "the player's own cmd next -> load")

            # ---- ended -> next load; three errors in a row -> stop
            cur = (await H.state(http, "ltv"))["current"]["id"]
            await ltv.send_json({"type": "ended", "item": cur})
            load, _ = await until(ltv, "load")
            check(load is not None and load["autoplay"] is True and load["present"] is False
                  and load["item"]["id"] != cur, "ended -> next load (autoplay, not presented)")
            ctl = None
            for n in range(3):
                cur = (await H.state(http, "ltv"))["current"]["id"]
                await ltv.send_json({"type": "error", "item": cur, "message": "MEDIA_ERR 4 test"})
                msg, _ = await until(ltv, "control" if n == 2 else "load")
                if n == 2:
                    ctl = msg
            check(ctl is not None and ctl["action"] == "stop", f"three errors in a row -> control stop ({ctl})")
            check((await H.state(http, "ltv"))["playing"] is False, "... and the session is not playing")

            # ---- the Apple TV on the headphones and LTVPlayer do not touch each other
            sink = await http.ws_connect(f"{H.WS}/ws?role=sink&session=tv")
            await H.drain(sink)
            await sink.send_json({"type": "sink", "name": "atv-audio", "output": {
                "kind": "bluetooth", "name": "Headphones X", "codec": "aptx_hd", "ready": True}})
            await H.drain(sink)
            tv = await H.connect(http, "role=player&kind=tv")
            tvremote = await H.connect(http, "role=remote&session=tv")
            await tv.send_json({"type": "route", "sink": True})
            await tvremote.send_json({"type": "cmd", "cmd": "play_ids", "ids": ids[:2]})
            await until(tv, "load")
            await tv.send_json({"type": "status", "item": ids[0], "playing": True, "position": 0.5, "duration": 4})
            await H.drain(tv)
            await H.drain(sink)
            check(not of_type(await H.drain(ltv), "control"), "TV plays via the headphones: ltv gets no control")
            await remote.send_json({"type": "cmd", "cmd": "play"})
            ctl, _ = await until(ltv, "control")
            check(ctl is not None and ctl["action"] == "play", f"ltv Play while the TV plays via the sink -> play ({ctl})")
            tv_msgs, sink_msgs = await H.drain(tv), await H.drain(sink)
            check(not of_type(tv_msgs, "control") and not of_type(tv_msgs, "load")
                  and not of_type(sink_msgs, "control") and not of_type(sink_msgs, "load"),
                  "... the TV and the sink get no control / load from it")
            check((await H.state(http, "tv"))["playing"] is True, "... and the TV session still plays")
            await remote.send_json({"type": "volume", "percent": 20})
            await remote.send_json({"type": "cmd", "cmd": "stop"})
            await remote.send_json({"type": "cmd", "cmd": "sound", "preset": "t:bass", "boost": 3})
            sink_msgs, ltv_msgs = await H.drain(sink), await H.drain(ltv)
            check(not of_type(sink_msgs, "volume"), "a remote's volume for ltv does not reach the headphones")
            check(not of_type(ltv_msgs, "volume") and not of_type(ltv_msgs, "control")
                  and not of_type(ltv_msgs, "sound"), "... and volume / stop / sound reach nothing on ltv either")
            st = await H.state(http, "ltv")
            check("sound" not in st and st["sink"] is None, "ltv state unchanged by them")

            # ---- reconnect: the session stops playing; the new player gets the state, then a paused load, no sound
            await status(ltv, st["current"]["id"], True, 1.5)
            await H.drain(ltv)
            await ltv.close()
            await H.drain(remote)
            st = await H.state(http, "ltv")
            check(st["players"] == 0 and st["playing"] is False, "ltv player gone -> 0 players, not playing")
            ltv = await http.ws_connect(f"{H.WS}/ws?role=player&kind=ltv")
            msgs = await H.drain(ltv)
            types = [m["type"] for m in msgs]
            loads = of_type(msgs, "load")
            check(types and types[0] == "state" and loads and loads[0]["autoplay"] is False and "sound" not in types,
                  f"ltv reconnect: state, then a paused load, no sound ({types})")

            # ---- the page: revalidated on every load; the web remote and the API keep their own headers
            async with http.get(f"{H.BASE}/static/ltv/index.html") as r:
                cc, etag = r.headers.get("Cache-Control"), r.headers.get("ETag")
                check(r.status == 200 and cc == "no-cache", f"/static/ltv/index.html: Cache-Control no-cache ({cc})")
            async with http.get(f"{H.BASE}/static/ltv/index.html", headers={"If-None-Match": etag or "x"}) as r:
                check(etag is not None and r.status == 304, f"... and a 304 on revalidation ({r.status})")
            async with http.get(f"{H.BASE}/static/manifest.webmanifest") as r:
                check(r.headers.get("Cache-Control") is None, "other /static/ files unchanged (no Cache-Control)")
            async with http.get(f"{H.BASE}/api/state?session=ltv") as r:
                check(r.headers.get("Cache-Control") == "no-store", "API answers still no-store")
            async with http.get(f"{H.BASE}/") as r:
                check(r.headers.get("Cache-Control") == "no-cache", "web remote page still no-cache")
            for ws in (ltv, remote, box, sink, tv, tvremote):
                await ws.close()
    finally:
        proc.terminate()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="atv-ltvtest-"))
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
