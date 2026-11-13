#!/usr/bin/env python3
"""Sync before a rescan (server 0.28.4, ATV_SYNC_SPOOL / ATV_SYNC_STATUS) on a THROWAWAY server with a fake sync job:
the library folder is a COPY of a "source" folder, and only the job copies. Checks: Rescan (?wait=1) asks the job,
waits for it and then finds the new file; the job's failure is reported and the copy is still scanned; a job that
never answers gives up after ATV_SYNC_START_WAIT and removes the request; an old status with another token is
ignored; ?sync=0 does not ask; the background rescan (no wait) also syncs first.
    python3 test_library_sync.py   (needs ffmpeg + the server's deps)
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_atv_audio as H  # noqa: E402

PORT = 18114
H.PORT, H.BASE = PORT, f"http://127.0.0.1:{PORT}"
BASE = H.BASE
failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


class FakeJob:
    """Plays the site's root job: watches <spool>/request, deletes it, copies source -> lib, writes the status."""

    def __init__(self, spool, status, src, lib):
        self.spool, self.status, self.src, self.lib = spool, status, src, lib
        self.mode = "ok"  # ok | fail | silent
        self.requests = 0

    def write(self, **st):
        tmp = self.status.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        os.replace(tmp, self.status)

    async def run(self):
        req = self.spool / "request"
        while True:
            await asyncio.sleep(0.1)
            if self.mode == "silent" or not req.exists():
                continue
            token = req.read_text().strip()
            req.unlink()
            self.requests += 1
            self.write(token=token, state="running")
            await asyncio.sleep(0.8)  # the server must wait for "done", not take "running" as the end
            if self.mode == "fail":
                self.write(token=token, state="failed", message="source not mounted")
                continue
            n = 0
            for f in self.src.rglob("*"):
                dst = self.lib / f.relative_to(self.src)
                if f.is_file() and not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dst)
                    n += 1
            self.write(token=token, state="done", changes=n)


async def rescan(http, query="wait=1"):
    t0 = time.monotonic()
    async with http.post(f"{BASE}/api/rescan?{query}") as r:
        return r.status, await r.json(), time.monotonic() - t0


async def main(tmp):
    src, lib, spool, status = tmp / "src", tmp / "lib", tmp / "spool", tmp / "status.json"
    for n in range(2):
        H.tone(src / "songs" / f"0{n} Song.flac", 2, 300 + 40 * n)
    shutil.copytree(src, lib)
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(tmp / "data"),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT), ATV_SYNC_SPOOL=str(spool),
               ATV_SYNC_STATUS=str(status), ATV_SYNC_START_WAIT="2")
    env.pop("ATV_BONJOUR", None)
    proc = subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)
    job = FakeJob(spool, status, src, lib)
    job_task = asyncio.create_task(job.run())
    try:
        async with aiohttp.ClientSession() as http:
            check(len(await H.wait_up(http, 2)) == 2, "library copy has 2 items")

            # 1. a new file in the SOURCE only: Rescan syncs, then finds it
            H.tone(src / "songs" / "02 New song.flac", 2, 500)
            code, j, secs = await rescan(http)
            check(code == 200 and j["ok"], "rescan answers ok")
            check(j.get("sync") == {"ok": True, "changes": 1, "error": ""}, f"sync result reported ({j.get('sync')})")
            check(j["added"] == 1 and "Tester - 02 New song" in j["new"], f"new source file added ({j['new']})")
            check(secs >= 0.8, f"waited for the job's done, not its running ({secs:.1f} s)")
            check(job.requests == 1 and not (spool / "request").exists(), "one request, picked up")

            # 2. nothing new: sync ok with 0 changes
            code, j, _ = await rescan(http)
            check(j["sync"] == {"ok": True, "changes": 0, "error": ""} and j["added"] == 0, "second rescan: source unchanged")

            # 3. the job fails: reported, the copy is still scanned (a file put into the copy directly appears)
            job.mode = "fail"
            H.tone(lib / "songs" / "03 Local.flac", 2, 600)
            code, j, _ = await rescan(http)
            check(code == 200 and j["ok"] and j["sync"]["ok"] is False and "source not mounted" in j["sync"]["error"],
                  f"failed sync reported ({j.get('sync')})")
            check(j["added"] == 1, "copy scanned despite the failed sync")

            # 4. no answer + a stale status of an older request: give up after START_WAIT, request removed
            job.mode = "silent"
            job.write(token="0123456789abcdef", state="done", changes=5)
            code, j, secs = await rescan(http)
            check(j["sync"]["ok"] is False and "did not answer" in j["sync"]["error"], f"silent job: gave up ({j['sync']})")
            check(2 <= secs < 6, f"gave up after the start wait ({secs:.1f} s)")
            check(not (spool / "request").exists(), "unanswered request removed")

            # 5. ?sync=0 (the job's own rescan after a scheduled copy): no request at all
            job.mode = "ok"
            before = job.requests
            code, j, secs = await rescan(http, "wait=1&sync=0")
            check(j.get("sync") is None and job.requests == before and secs < 2, "sync=0: no sync asked")

            # 6. background rescan (Settings -> Library -> Rescan) also syncs first
            H.tone(src / "songs" / "04 Background.flac", 2, 700)
            code, j, _ = await rescan(http, "")
            check(code == 200 and j == {"scanning": True}, "background rescan accepted")
            found = False
            for _ in range(100):
                await asyncio.sleep(0.1)
                if "04 Background" in await H.library(http):
                    found = True
                    break
            check(found and job.requests == before + 1, "background rescan synced, then found the new file")
    finally:
        job_task.cancel()
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
    log = (tmp / "server.log").read_text()
    check("Traceback" not in log, "no traceback in the server log")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        asyncio.run(main(Path(d)))
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
