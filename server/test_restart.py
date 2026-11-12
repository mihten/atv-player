#!/usr/bin/env python3
"""Settings -> Server -> Restart server (POST /api/restart, server 0.28.0) on a THROWAWAY server (own temp library,
data dir and port): admin lock, answer before the exit, state files written, exit status 75 (the unit's
Restart=on-failure starts it again), quick exit with a player websocket still open, and a second start that keeps
the queue.
    python3 test_restart.py   (needs ffmpeg + the server's deps)
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
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


PROCS = []  # every server started; all are stopped at the end even when a check failed


def start(tmp, env):
    PROCS.append(subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT))
    return PROCS[-1]


async def req(http, method, path, **kw):
    async with http.request(method, BASE + path, **kw) as r:
        txt = await r.text()
        try:
            return r.status, json.loads(txt)
        except ValueError:
            return r.status, txt


async def main(tmp):
    lib = tmp / "lib"
    for n in range(3):
        H.tone(lib / "songs" / f"0{n} Song.flac", 3, 300 + 40 * n)
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(tmp / "data"),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT))
    env.pop("ATV_BONJOUR", None)
    proc = start(tmp, env)
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:  # cookies for 127.0.0.1
            check(len(await H.wait_up(http, 3)) == 3, "throwaway library has 3 items")
            items = await H.library(http)
            ids = sorted(it["id"] for it in items.values())
            code, st = await req(http, "GET", "/api/status")
            started = st["started"]
            check(st["version"] == "0.28.3", f"version 0.28.2 ({st['version']})")

            # admin lock: a locked browser cannot restart
            code, _ = await req(http, "POST", "/api/settings/password", json={"password": "secret123"})
            async with aiohttp.ClientSession() as other:
                code, txt = await req(other, "POST", "/api/restart")
            check(code == 403 and "locked" in str(txt) and proc.poll() is None, "locked browser: 403, server keeps running")
            code, r = await req(http, "POST", "/api/settings/password", json={"password": ""})
            check(code == 200 and r["admin"] is False, "password removed again")

            # a queue and an open player websocket, then restart
            tv = await H.connect(http, "role=player&kind=tv")
            await tv.send_json({"type": "cmd", "cmd": "play_ids", "ids": ids})
            await asyncio.sleep(0.5)
            state_file = tmp / "data" / "state.json"   # the tv session keeps the old name (STATE_FILE)
            if state_file.exists():
                state_file.unlink()   # proves the restart writes it again
            t0 = time.time()
            try:
                code, r = await req(http, "POST", "/api/restart")
            except aiohttp.ClientError as e:   # the server died before it answered
                code, r = None, repr(e)
            check(code is not None, f"the restart request gets an answer ({r if code is None else code})")
            if code is None:
                code, r = 0, {"ok": False, "started": 0, "version": ""}
            check(code == 200 and r["ok"] is True and r["started"] == started and r["version"] == "0.28.3",
                  f"restart answers before the exit ({code} {r})")
            check(state_file.exists() and json.loads(state_file.read_text()).get("queue") == ids,
                  "state.json (tv queue) written again before the exit")
            try:
                rc = proc.wait(10)
            except subprocess.TimeoutExpired:
                rc = None
            took = time.time() - t0
            check(rc == 75, f"exit status 75 (got {rc})")
            check(rc is not None and took < 3, f"exits quickly with a player still connected ({took:.1f} s)")
            await tv.close()
            if rc is None:
                return

            # what systemd does: start again; the queue is kept, start time is newer
            proc = start(tmp, env)
            check(len(await H.wait_up(http, 3)) == 3, "second start: library back")
            code, st2 = await req(http, "GET", "/api/status")
            check(st2["started"] > started, "status.started is newer (the page waits for this)")
            st_tv = await H.state(http, "tv")
            check(st2["started"] > started and (st_tv.get("queue") == ids or [q.get("id") for q in st_tv.get("queue", []) if isinstance(q, dict)] == ids),
                  "queue survived the restart")
            log = (tmp / "server.log").read_text()
            check("restart requested" in log and "exiting with status 75" in log, "log names the request and the exit")
    finally:
        for p in PROCS:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(30)
                except subprocess.TimeoutExpired:
                    p.kill()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        asyncio.run(main(Path(d)))
    print("ALL PASSED" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
