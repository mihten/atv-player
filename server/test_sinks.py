#!/usr/bin/env python3
"""Several atv-audio programs on one server (server 0.19): one active, the others in standby; switching hands the
headphones over (old program frees them first); box loads only reach the active program's box player; the choice
survives restarts; the blind A/B test drives both programs and hides which one plays.
THROWAWAY server (own temp library, data dir and port), two fake sink programs.
    python3 test_sinks.py   (needs ffmpeg + the server's deps)
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

PORT = 18097
H.PORT, H.BASE = PORT, f"http://127.0.0.1:{PORT}"
H.WS = H.BASE.replace("http", "ws")
BASE = H.BASE
failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


class FakeProgram:
    """One atv-audio program: a sink ws + a box player ws. Answers standby like the real one: on = output not ready
    (headphones freed), off = ready again after `take` s. The box player reports playing on load + autoplay."""

    def __init__(self, http, sid, name, codec, take=0.2):
        self.http, self.sid, self.name, self.codec, self.take = http, sid, name, codec, take
        self.msgs, self.box_msgs = [], []
        self.ready = True
        self.standby = None

    async def start(self):
        self.ws = await self.http.ws_connect(f"{H.WS}/ws?role=sink&session=tv&id={self.sid}")
        self.box = await self.http.ws_connect(f"{H.WS}/ws?role=player&kind=box&sink={self.sid}")
        await self.report()
        self.tasks = [asyncio.create_task(self.read_sink()), asyncio.create_task(self.read_box())]

    async def report(self):
        await self.ws.send_json({"type": "sink", "name": "atv-audio", "output": {
            "kind": "bluetooth", "name": self.name, "codec": self.codec, "ready": self.ready}})

    async def read_sink(self):
        async for m in self.ws:
            if m.type != aiohttp.WSMsgType.TEXT:
                break
            d = json.loads(m.data)
            self.msgs.append(d)
            if d.get("type") == "standby":
                self.standby = d["on"]
                if d["on"]:
                    self.ready = False
                    await self.report()
                else:
                    await asyncio.sleep(self.take)
                    self.ready = True
                    await self.report()

    async def read_box(self):
        async for m in self.box:
            if m.type != aiohttp.WSMsgType.TEXT:
                break
            d = json.loads(m.data)
            self.box_msgs.append(d)
            if d.get("type") == "load" and d.get("autoplay") and d.get("item"):
                await self.box.send_json({"type": "status", "item": d["item"]["id"], "playing": True,
                                          "position": d.get("position") or 0, "duration": 10})
            elif d.get("type") == "control" and d.get("action") == "pause":
                await self.box.send_json({"type": "status", "item": None, "playing": False, "position": 0})

    def loads(self):
        return [m for m in self.box_msgs if m.get("type") == "load"]

    async def close(self):
        for t in self.tasks:
            t.cancel()
        await self.ws.close()
        await self.box.close()


async def req(http, method, path, **kw):
    async with http.request(method, BASE + path, **kw) as r:
        try:
            body = await r.json()
        except (aiohttp.ContentTypeError, ValueError):
            body = await r.text()
        return r.status, body


async def until(cond, seconds=6.0):
    for _ in range(int(seconds / 0.05)):
        if cond():
            return True
        await asyncio.sleep(0.05)
    return cond()


async def sinks(http):
    return (await H.state(http, "box"))["sink"]


async def main(tmp):
    lib = tmp / "lib"
    for n in range(1, 4):
        H.tone(lib / "music" / f"0{n} Song{n}.flac", 10, 300 + 40 * n)
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib / "music")}), ATV_DATA=str(tmp / "data"),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT), ATV_ABTEST_GAP="0.5")
    env.pop("ATV_BONJOUR", None)

    def start_server():
        return subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                                stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)
    proc = start_server()
    try:
        async with aiohttp.ClientSession() as http:
            items = await H.wait_up(http, 3)
            ids = sorted(it["id"] for it in items)
            dongle = FakeProgram(http, "atv-audio-dongle", "BTD 700", "aptx_adaptive")
            bt = FakeProgram(http, "atv-audio-bt", "Headphones X", "aptx_hd")
            await dongle.start()
            await asyncio.sleep(0.3)
            await bt.start()
            await asyncio.sleep(0.5)
            st = await sinks(http)
            check([(x["id"], x["active"]) for x in st["sinks"]] == [("atv-audio-bt", False), ("atv-audio-dongle", True)],
                  f"two programs listed, the first one active ({st['sinks']})")
            check(st["output"]["name"] == "BTD 700" and bt.standby is True, "state = the active one; the other got standby")

            # ---- box loads only reach the active program's box player
            n_bt = len(bt.loads())
            remote = await H.connect(http, "role=remote&session=box")
            await remote.send_json({"type": "cmd", "cmd": "play_ids", "ids": ids})
            await until(lambda: any(m.get("autoplay") for m in dongle.loads()))
            check(any(m.get("autoplay") and m["item"]["id"] == ids[0] for m in dongle.loads()), "box Play -> active box player")
            await asyncio.sleep(0.3)
            check(len(bt.loads()) == n_bt, "standby box player got nothing")

            # ---- switch to the BlueZ program: old frees first, new takes, box resumes there
            dongle.msgs.clear()
            code, _ = await req(http, "POST", "/api/atv-audio", json={"op": "set_sink", "sink": "atv-audio-bt"})
            check(code == 200, "set_sink accepted")
            await until(lambda: bt.standby is False and (bt.loads() and bt.loads()[-1].get("autoplay")))
            check(dongle.standby is True and bt.standby is False, "dongle standby, bt active")
            check(bt.loads() and bt.loads()[-1]["item"]["id"] == ids[0] and bt.loads()[-1]["autoplay"],
                  f"box resumes on the new program ({bt.loads()[-1:]})")
            st = await sinks(http)
            check(st["output"]["name"] == "Headphones X" and [x["active"] for x in st["sinks"]] == [True, False],
                  "state follows the switch")
            check(st["sinks"][1]["name"] == "BTD 700" and st["sinks"][1]["codec"] == "aptx_adaptive" and not st["sinks"][1]["ready"],
                  f"standby program keeps its last device name + codec ({st['sinks'][1]})")
            saved = json.loads((tmp / "data" / "settings.json").read_text())
            check(saved.get("active_sink") == "atv-audio-bt", "choice saved in settings.json")
            log = (tmp / "server.log").read_text()
            check("switching atv-audio-dongle -> atv-audio-bt" in log, "switch logged")
            n_d = len(dongle.loads())
            await remote.send_json({"type": "cmd", "cmd": "next"})
            await until(lambda: len(bt.loads()) >= 3)
            await asyncio.sleep(0.3)
            check(len(dongle.loads()) == n_d, "after the switch the old box player gets nothing")

            # ---- the standby program restarts: stays standby; active one gone: nothing moves by itself
            await dongle.close()
            dongle = FakeProgram(http, "atv-audio-dongle", "BTD 700", "aptx_adaptive")
            await dongle.start()
            await until(lambda: dongle.standby is not None)
            check(dongle.standby is True, "restarted standby program stays standby")
            await bt.close()
            await asyncio.sleep(0.5)
            st = await sinks(http)
            check(st["connected"] is False and dongle.standby is True, "active one gone: no automatic move")
            bt = FakeProgram(http, "atv-audio-bt", "Headphones X", "aptx_hd")
            await bt.start()
            await asyncio.sleep(0.5)
            st = await sinks(http)
            check(bt.standby is not True and st["output"] and st["output"]["name"] == "Headphones X", "the chosen one back -> active")

            # ---- blind test
            code, _ = await req(http, "POST", "/api/abtest", json={"op": "vote", "choice": "a"})
            check(code == 409, "vote without a test refused")
            code, ab = await req(http, "POST", "/api/abtest", json={"op": "start", "items": [{"id": ids[1], "start": 2}],
                                                                       "per_song": 2, "length": 5})
            check(code == 200 and ab["running"] and ab["total"] == 2, f"blind test started ({ab})")
            await until_phase(http, "play_a", 10)
            st = await sinks(http)
            check(st["sinks"] == [] and st["output"] and st["output"]["name"] == "Blind test" and st["output"]["codec"] is None
                  and st["audio"] is None, "identity hidden while it runs")
            for trial in (1, 2):
                ok = await until_phase(http, "vote", 30)
                check(ok, f"trial {trial}: both excerpts played, waiting for the vote")
                if trial == 1:
                    code, _ = await req(http, "POST", "/api/abtest", json={"op": "replay", "which": "b"})
                    check(code == 200, "replay B accepted")
                    await until_phase(http, "play_b", 10)
                    ok = await until_phase(http, "vote", 30)
                    check(ok, "after the replay: vote again")
                code, _ = await req(http, "POST", "/api/abtest", json={"op": "vote", "choice": "a" if trial == 1 else "same"})
                check(code == 200, f"trial {trial}: vote taken")
            ok = await until_done(http, 15)
            code, ab = await req(http, "GET", "/api/abtest")
            r = ab.get("result") or {}
            check(ok and not ab["running"] and len(r.get("rows", [])) == 2 and r["same"] == 1 and r["decided"] == 1,
                  f"result: 2 rows, 1 same, 1 decided ({r})")
            check(r["rows"][0]["better"] == r["rows"][0]["a"] and "BTD 700" in " ".join(r["labels"])
                  and "Headphones X" in " ".join(r["labels"]), "revealed: A was better in trial 1, names shown")
            check(list((tmp / "data").glob("abtest-*.json")), "result saved")
            plays = [m for m in dongle.loads() + bt.loads() if m.get("autoplay") and m["item"]["id"] == ids[1]]
            check(len(plays) == 5 and all(m["position"] == 2 for m in plays), f"5 excerpts (2x2 + replay) from 2 s ({len(plays)})")
            st = await sinks(http)
            check(st["output"]["name"] == "Headphones X" and len(st["sinks"]) == 2, "after the test: back to the chosen program")

            # ---- restart: the chosen one wins even if the other connects first
            await dongle.close()
            await bt.close()
            await remote.close()
            proc.terminate()
            proc.wait(10)
            proc = start_server()
            await H.wait_up(http, 3)
            dongle = FakeProgram(http, "atv-audio-dongle", "BTD 700", "aptx_adaptive")
            bt = FakeProgram(http, "atv-audio-bt", "Headphones X", "aptx_hd")
            await dongle.start()
            await asyncio.sleep(0.4)
            await bt.start()
            await until(lambda: bt.standby is not True and dongle.standby is True)
            await asyncio.sleep(0.5)
            st = await sinks(http)
            check(bt.standby is not True and dongle.standby is True and st["output"]["name"] == "Headphones X",
                  "server restart: the saved choice takes over again")
            await dongle.close()
            await bt.close()
    finally:
        proc.terminate()
        proc.wait(10)


async def until_phase(http, phase, seconds):
    for _ in range(int(seconds / 0.1)):
        async with http.get(BASE + "/api/abtest") as r:
            ab = await r.json()
        if ab.get("phase") == phase:
            return True
        if not ab.get("running"):
            print("  test ended early:", ab.get("error"))
            return False
        await asyncio.sleep(0.1)
    return False


async def until_done(http, seconds):
    for _ in range(int(seconds / 0.1)):
        async with http.get(BASE + "/api/abtest") as r:
            ab = await r.json()
        if not ab.get("running"):
            return True
        await asyncio.sleep(0.1)
    return False


if __name__ == "__main__":
    tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="atv-sinkstest-")))
    try:
        asyncio.run(main(tmp))
    except Exception:
        log = tmp / "server.log"
        print(log.read_text()[-4000:] if log.exists() else "")
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
