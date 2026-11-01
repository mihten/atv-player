#!/usr/bin/env python3
"""atv-audio sink (task 17), server side: role=sink gets what the TV's players get plus the TV's relayed
route/sound/lip_sync/clock (replayed on connect), its status is in the TV state, one sink per session, ready ->
sink_ready, ping/pong; /media/<id>?audio=main (original at once, then a one-track copy of the default track),
prefetch of the current + next 3 queue items while a sink is connected, the "Extract missing audio" job and the
audio_copy badge field. Runs a THROWAWAY server instance (own temp library, data dir and port; never the real one).
    python3 test_atv_audio.py   (needs ffmpeg/ffprobe + the server's deps)
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
FAKE = HERE.parent / "tools" / "fake_player.py"
PORT = 18099
BASE = f"http://127.0.0.1:{PORT}"
WS = BASE.replace("http", "ws")
failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def tone(path, seconds, freq):
    path.parent.mkdir(parents=True, exist_ok=True)
    ff("-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
       "-metadata", f"title={path.stem}", "-metadata", "artist=Tester", str(path))


def clip(path, seconds, second_default=False, tracks=2):
    """Small MKV: H.264 picture + FLAC (+ Opus). Default audio = the FLAC, or the Opus with second_default."""
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["-f", "lavfi", "-i", f"testsrc=size=64x64:rate=10:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=660:duration={seconds}",
            "-map", "0:v", "-map", "1:a"] + (["-map", "2:a"] if tracks == 2 else []) + [
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a:0", "flac"] + (["-c:a:1", "libopus"] if tracks == 2 else [])
    if tracks == 2:
        args += ["-disposition:a:0", "0" if second_default else "default",
                 "-disposition:a:1", "default" if second_default else "0"]
    ff(*args, "-metadata", f"title={path.stem}", "-metadata", "artist=Clipper", str(path))


def audio_codecs(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_name",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def stream_types(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


async def drain(ws, wait=0.4):
    """All text messages that arrive within `wait` s of each other."""
    out = []
    while True:
        try:
            msg = await asyncio.wait_for(ws.receive(), wait)
        except asyncio.TimeoutError:
            return out
        if msg.type != aiohttp.WSMsgType.TEXT:
            return out
        out.append(json.loads(msg.data))


def of(msgs, kind):
    return [m for m in msgs if m.get("type") == kind]


def last_state(msgs):
    states = [m["state"] for m in of(msgs, "state")]
    return states[-1] if states else None


async def state(http, session=None):
    async with http.get(f"{BASE}/api/state" + (f"?session={session}" if session else "")) as r:
        return await r.json()


async def library(http):
    async with http.get(f"{BASE}/api/library") as r:
        return {it["title"]: it for it in (await r.json())["items"]}


async def wait_up(http, count):
    for _ in range(150):
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


async def wait_for(cond, seconds=15):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        await asyncio.sleep(0.1)
    return cond()


async def main(tmp):
    lib = tmp / "lib"
    tone(lib / "songs" / "01 Song.flac", 5, 330)
    clips = {"Main": (lib / "clips" / "Main.mkv", False), "Second": (lib / "clips" / "Second.mkv", True),
             "Single": (lib / "clips" / "Single.mkv", False)}
    names = ["Main", "Second", "Single", "S1", "S2"] + [f"Q{n}" for n in range(1, 6)] + [f"J{n}" for n in range(1, 7)]
    for n, name in enumerate(names):
        path, second = clips.get(name, (lib / "clips" / f"{name}.mkv", False))
        clip(path, round(3 + 0.7 * n, 1), second_default=second, tracks=1 if name == "Single" else 2)
    total = 1 + len(names)
    cache = tmp / "cache"
    audio = cache / "audio"
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(tmp / "data"),
               ATV_CACHE=str(cache), ATV_PORT=str(PORT))
    env.pop("ATV_BONJOUR", None)
    audio.mkdir(parents=True)
    leftover = audio / "99.main.123.456.tmp.mka"  # partial copy of a server stopped mid-copy
    leftover.write_bytes(b"partial")
    proc = subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)
    fake = None
    try:
        async with aiohttp.ClientSession() as http:
            check(len(await wait_up(http, total)) == total, f"throwaway library has {total} items")
            check(not leftover.exists(), "startup removes partial copies (*.tmp.mka)")
            items = await library(http)
            ids = {name: items[name]["id"] for name in names}
            song = items["01 Song"]["id"]
            main_copy = lambda name: audio / f"{ids[name]}.main.mka"  # noqa: E731
            async with http.get(f"{BASE}/api/item/{ids['Second']}") as r:
                second_ord = (await r.json())["audio_ord"]
            async with http.get(f"{BASE}/api/item/{ids['Main']}") as r:
                main_ord = (await r.json())["audio_ord"]
            check(main_ord == 0 and second_ord == 1, f"scanner: default FLAC = audio_ord 0, default Opus = 1 "
                                                     f"(got {main_ord}, {second_ord})")
            check(all(it["audio_copy"] is False for it in items.values()), "no audio copies yet (audio_copy false)")

            tv = await connect(http, "role=player&kind=tv")
            phone = await connect(http, "role=player&kind=phone")
            remote = await connect(http, "role=remote&session=tv")
            tv_state, ph_state = await state(http, "tv"), await state(http, "phone")
            check(tv_state["sink"] == {"connected": False, "name": None, "output": None} and ph_state["sink"] is None,
                  "state.sink: not connected on tv, null on phone")

            # ---- the TV's config/clock messages before any sink: remembered for the replay
            await tv.send_json({"type": "cmd", "cmd": "play_ids", "ids": [song]})
            await drain(tv)
            route = {"type": "route", "sink": True}
            sound = {"type": "sound", "processed": True, "gain": -3.5, "freqs": [31.25 * 2 ** n for n in range(10)],
                     "bands": [1.0] * 10}
            lip = {"type": "lip_sync", "ms": 220}
            clock1 = {"type": "clock", "item": song, "position": 1.0, "at": 100.0, "state": "playing"}
            clock2 = {"type": "clock", "item": song, "position": 1.25, "at": 100.25, "state": "playing"}
            for m in (lip, sound, route, clock1, clock2):  # replay order is fixed, not the order they came in
                await tv.send_json(m)
            await phone.send_json({"type": "clock", "item": 1, "position": 9.0, "at": 1.0, "state": "playing"})
            await asyncio.sleep(0.3)

            sink = await http.ws_connect(f"{WS}/ws?role=sink&session=tv")
            first = await drain(sink)
            types = [m["type"] for m in first]
            check(types[:6] == ["state", "load", "route", "sound", "lip_sync", "clock"],
                  f"sink connect: state, load, route, sound, lip_sync, clock (got {types})")
            check(first[0]["state"]["sink"]["connected"] and first[1]["item"]["id"] == song and
                  first[1]["present"] is False, "... state says connected, load is the current item, present false")
            check(first[2] == route and first[3] == sound and first[4] == lip and first[5] == clock2,
                  "... replayed messages unchanged, the LAST clock only")
            check(not of(first[6:], "clock") and (await state(http, "tv"))["players"] == 1,
                  "phone's clock not relayed; the sink does not count as a player")

            # ---- sink status -> TV state (players + web remote), not the phone's
            out = {"kind": "bluetooth", "name": "MOMENTUM 4", "codec": "aptx_hd", "ready": True}
            await sink.send_json({"type": "sink", "name": "atv-audio", "output": out})
            tv_msgs, ph_msgs, rm_msgs = await drain(tv), await drain(phone), await drain(remote)
            want = {"connected": True, "name": "atv-audio", "output": out}
            check((last_state(tv_msgs) or {}).get("sink") == want, "sink status reaches the TV player's state")
            check((last_state(rm_msgs) or {}).get("sink") == want, "... and the TV web remote's state")
            check(last_state(ph_msgs) is None or last_state(ph_msgs)["sink"] is None, "... not the phone's")
            await sink.send_json({"type": "sink", "name": "atv-audio", "output": dict(out, ready=False)})
            check((last_state(await drain(tv)) or {}).get("sink", {}).get("output", {}).get("ready") is False,
                  "output change (ready false) is broadcast")
            await sink.send_json({"type": "sink", "name": "atv-audio", "output": None})
            check((await state(http, "tv"))["sink"] == {"connected": True, "name": "atv-audio", "output": None},
                  "output null = connected without an output")
            await sink.send_json({"type": "sink", "name": "atv-audio", "output": out})
            await drain(sink)

            # ---- what the players get, the sink gets
            await remote.send_json({"type": "cmd", "cmd": "play_ids", "ids": [ids["S1"], ids["S2"]]})
            sk, tvm = await drain(sink), await drain(tv)
            check([m["item"]["id"] for m in of(sk, "load")] == [ids["S1"]] == [m["item"]["id"] for m in of(tvm, "load")]
                  and of(sk, "load")[0]["item"]["audio_ord"] == 0, "sink gets the same load (full item) as the TV")
            for cmd, extra in (("pause", {}), ("play", {}), ("seek", {"position": 2.5})):
                await remote.send_json({"type": "cmd", "cmd": cmd, **extra})
            sk = await drain(sink)
            check([(m["action"], m.get("position")) for m in of(sk, "control")] ==
                  [("pause", None), ("play", None), ("seek", 2.5)], "sink gets control pause / play / seek")
            await phone.send_json({"type": "cmd", "cmd": "play_ids", "ids": [song]})
            sk = await drain(sink)
            await drain(phone)
            check(not of(sk, "load") and not of(sk, "control"), "phone session commands do not reach the sink")
            await tv.send_json({"type": "cmd", "cmd": "next"})
            check([m["item"]["id"] for m in of(await drain(sink), "load")] == [ids["S2"]], "next -> sink loads too")
            async with http.post(f"{BASE}/api/playlists", json={"name": "P", "ids": [song]}) as r:
                await r.json()
            check(of(await drain(sink), "playlists"), "sink gets the playlists message")
            await tv.send_json({"type": "cmd", "cmd": "love", "id": song, "on": True})
            check(of(await drain(sink), "auto"), "sink gets the auto message (heart changed)")
            await drain(tv)

            # ---- relays: TV -> sink unchanged; remote clocks are not relayed
            clock3 = {"type": "clock", "item": ids["S2"], "position": 0.5, "at": 200.0, "state": "paused"}
            await tv.send_json(clock3)
            await tv.send_json({"type": "lip_sync", "ms": -40})
            await remote.send_json({"type": "clock", "item": 1, "position": 3.0, "at": 2.0, "state": "playing"})
            sk = await drain(sink)
            check(of(sk, "clock") == [clock3] and of(sk, "lip_sync") == [{"type": "lip_sync", "ms": -40}],
                  "TV clock / lip_sync relayed unchanged, remote's clock not")

            # ---- ready -> sink_ready {id} to the session's players only
            await sink.send_json({"type": "ready", "item": ids["S2"]})
            tvm, phm = await drain(tv), await drain(phone)
            check(of(tvm, "sink_ready") == [{"type": "sink_ready", "id": ids["S2"]}] and not of(phm, "sink_ready"),
                  "sink ready -> TV gets sink_ready with key id, phone nothing")

            # ---- ping/pong (server monotonic time), only to the sender
            for ws, n in ((tv, 7), (sink, 3), (phone, 11)):
                await ws.send_json({"type": "ping", "n": n})
            got = {name: of(await drain(ws), "pong") for name, ws in (("tv", tv), ("sink", sink), ("phone", phone))}
            now = time.monotonic()
            check([p["n"] for p in got["tv"]] == [7] and [p["n"] for p in got["sink"]] == [3] and
                  [p["n"] for p in got["phone"]] == [11], "pong to each sender only, with its n")
            check(all(abs(p["t"] - now) < 5 for ps in got.values() for p in ps), "pong t = server monotonic time")

            # ---- sink log line
            await sink.send_json({"type": "log", "message": "hello from the sink " + "x" * 2000})
            await asyncio.sleep(0.3)
            logged = [line for line in (tmp / "server.log").read_text().splitlines() if "sink[tv]: hello" in line]
            check(len(logged) == 1 and len(logged[0]) < 1700, "sink log line in the server log (max 1500 chars)")

            # ---- one sink per session: a new one replaces the old
            await drain(tv)
            sink2 = await http.ws_connect(f"{WS}/ws?role=sink&session=phone")  # any session -> tv
            try:  # late messages of the replaced sink are ignored
                await sink.send_json({"type": "sink", "name": "old-sink", "output": out})
                await sink.send_json({"type": "ready", "item": ids["S2"]})
            except (ConnectionError, RuntimeError):
                pass
            first2 = await drain(sink2)
            closed = await asyncio.wait_for(sink.receive(), 5)
            check(closed.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED),
                  "second sink connects -> server closes the first")
            check([m["type"] for m in first2][:6] == ["state", "load", "route", "sound", "lip_sync", "clock"] and
                  first2[4]["ms"] == -40 and first2[5] == clock3, "... the new one gets the latest replay")
            await asyncio.sleep(0.3)
            st = await state(http, "tv")
            check(st["sink"]["connected"] and st["sink"]["output"] is None and st["players"] == 1,
                  "after the replace: still connected (new sink, no output reported yet), players 1")
            check(st["sink"]["name"] != "old-sink" and not of(await drain(tv), "sink_ready"),
                  "... the replaced sink's late status / ready are ignored")
            ph = await state(http, "phone")
            check(ph["sink"] is None, "session=phone sink went to the tv session")
            await tv.send_json({"type": "status", "item": ids["S2"], "playing": True, "position": 1.0, "duration": 9})
            await asyncio.sleep(0.2)
            await sink2.close()
            await asyncio.sleep(0.3)
            st = await state(http, "tv")
            check(st["sink"] == {"connected": False, "name": None, "output": None} and st["playing"] and
                  st["players"] == 1, "sink disconnects -> state.sink not connected, TV still playing")
            await drain(tv)

            # ---- tools/fake_player.py sink: status, ready answer, printed relays
            fake = await asyncio.create_subprocess_exec(sys.executable, str(FAKE), "sink", env=dict(env, ATV_URL=BASE),
                                                        stdout=asyncio.subprocess.PIPE)
            for _ in range(30):
                st = await state(http, "tv")
                if st["sink"]["connected"] and st["sink"]["name"] == "fake-sink":
                    break
                await asyncio.sleep(0.1)
            ok = st["sink"]["name"] == "fake-sink" and (st["sink"]["output"] or {}).get("codec") == "aptx_hd"
            check(ok, "fake_player.py sink: connects and reports its output")
            await drain(tv)
            await tv.send_json({"type": "cmd", "cmd": "jump", "index": 0})
            tvm = await drain(tv, 1.0)
            check({"type": "sink_ready", "id": ids["S1"]} in tvm, "fake sink answers load with ready -> sink_ready")
            await tv.send_json({"type": "clock", "item": ids["S1"], "position": 0.0, "at": 300.0, "state": "playing"})
            printed = []
            end = time.time() + 5
            while time.time() < end and not any(m.get("type") == "clock" and m.get("at") == 300.0 for m in printed):
                try:
                    line = await asyncio.wait_for(fake.stdout.readline(), 1)
                except asyncio.TimeoutError:
                    continue
                if not line:
                    break
                printed.append(json.loads(line))
            check(any(m.get("type") == "clock" and m.get("at") == 300.0 for m in printed) and
                  any(m.get("type") == "pong" and "offset" in m for m in printed) and
                  any(m.get("type") == "load" for m in printed), "fake sink prints load, pong (offset) and the clock")
            fake.terminate()
            await fake.wait()
            fake = None
            await asyncio.sleep(0.3)
            check(not (await state(http, "tv"))["sink"]["connected"], "fake sink gone -> not connected")

            # ---- ?audio=main: original at once, then the copy (one track = the default one)
            for name, codec in (("Main", "flac"), ("Second", "opus"), ("Single", "flac")):
                url = f"{BASE}/media/{ids[name]}?audio=main"
                async with http.get(url) as r:
                    body = await r.read()
                    hdr = r.headers.get("X-Audio-Copy")
                orig = clips[name][0]
                check(r.status == 200 and hdr == "original" and len(body) == orig.stat().st_size and
                      str(r.url).endswith("v=orig"), f"{name}: ?audio=main without a copy -> the original file at once")
                await wait_for(lambda: main_copy(name).exists())
                # an open stream seeks with Range on the redirected URL: still the original after the copy is made
                async with http.get(f"{url}&v=orig", headers={"Range": "bytes=100-199"}) as r:
                    part = await r.read()
                check(r.status == 206 and part == orig.read_bytes()[100:200],
                      f"{name}: Range on v=orig after the copy exists -> bytes of the original")
                async with http.get(url, allow_redirects=False) as r:
                    loc = r.headers.get("Location", "")
                    cache = r.headers.get("Cache-Control", "")
                check(r.status == 302 and loc.endswith(f"/media/{ids[name]}?audio=main&v=copy") and "max-age" in cache,
                      f"{name}: ?audio=main now redirects (cacheable) to v=copy")
                async with http.get(url, headers={"Range": "bytes=0-"}) as r:
                    body = await r.read()
                    hdr = r.headers.get("X-Audio-Copy")
                got = tmp / f"got-{name}.mka"
                got.write_bytes(body)
                check(hdr == "main" and r.status in (200, 206) and stream_types(got) == ["audio"] and
                      audio_codecs(got) == [codec], f"{name}: then the copy: exactly one audio track ({codec}), no video")
            async with http.get(f"{BASE}/media/{ids['Main']}?audio=main", headers={"Range": "bytes=0-99"}) as r:
                check(r.status == 206 and len(await r.read()) == 100, "copy is served with Range")
            async with http.get(f"{BASE}/media/{ids['Q1']}?audio=main&v=copy") as r:
                check(r.status == 404, "v=copy without a copy -> 404 (never another file)")
            async with http.get(f"{BASE}/media/{ids['Main']}?audio=1") as r:
                body = await r.read()
            (tmp / "all.mka").write_bytes(body)
            check(audio_codecs(tmp / "all.mka") == ["flac", "opus"] and "X-Audio-Copy" not in r.headers,
                  "?audio=1 unchanged: all audio tracks in file order")
            async with http.get(f"{BASE}/media/{song}?audio=main") as r:
                check(r.headers.get("X-Audio-Copy") == "original" and len(await r.read()) ==
                      (lib / "songs" / "01 Song.flac").stat().st_size, "song: ?audio=main = the original file")
            items = await library(http)
            check(items["Main"]["audio_copy"] and items["Second"]["audio_copy"] and not items["Q1"]["audio_copy"]
                  and not items["01 Song"]["audio_copy"], "audio_copy badge true for made copies only")

            # ---- prefetch: only with a sink, current + next 3
            await tv.send_json({"type": "cmd", "cmd": "play_ids", "ids": [ids[f"Q{n}"] for n in range(1, 6)]})
            await asyncio.sleep(1.5)
            check(not any(main_copy(f"Q{n}").exists() for n in range(1, 6)), "no sink: nothing prefetched")
            sink = await connect(http, "role=sink&session=tv")
            ok = await wait_for(lambda: all(main_copy(f"Q{n}").exists() for n in range(1, 5)))
            await asyncio.sleep(1.0)
            check(ok and not main_copy("Q5").exists(), "sink connected: copies of the current + next 3, not the 5th")
            check(not any(main_copy(f"J{n}").exists() for n in range(1, 7)), "... nothing outside the queue")
            await tv.send_json({"type": "ended", "item": ids["Q1"]})
            check(await wait_for(lambda: main_copy("Q5").exists()), "index moves on -> the new 4th item is copied")

            # ---- "Extract missing audio" job
            await asyncio.sleep(0.5)
            async with http.get(f"{BASE}/api/audio_copies") as r:
                before = await r.json()
            check(before == {"running": False, "done": 0, "total": 0, "missing": 6, "copies": 10},
                  f"GET /api/audio_copies before the job (got {before})")
            await drain(remote)
            async with http.post(f"{BASE}/api/audio_copies") as r1:
                j1 = await r1.json()
            async with http.post(f"{BASE}/api/audio_copies") as r2:
                j2 = await r2.json()
            check(r1.status == 200 and j1["running"] and j1["total"] == 6, f"POST starts the job (got {j1})")
            check(r2.status == 409 and j2 == {"running": True}, "second POST while running -> 409 running")
            pushes = []
            end = time.time() + 30
            while time.time() < end and not (pushes and not pushes[-1]["running"]):
                pushes += of(await drain(remote, 1.0), "audio_copies")
            done = pushes[-1] if pushes else {}
            check(done.get("running") is False and done.get("done") == 6 and done.get("total") == 6 and
                  done.get("missing") == 0 and done.get("copies") == 16, f"job pushes progress to the remote, ends "
                                                                         f"6/6 (last {done})")
            check(any(p["running"] for p in pushes) and [p["done"] for p in pushes] == sorted(p["done"] for p in pushes),
                  "... with running updates, done counting up")
            check(all(main_copy(f"J{n}").exists() for n in range(1, 7)), "every missing copy was made")
            async with http.post(f"{BASE}/api/audio_copies") as r:
                again = await r.json()
            check(r.status == 200 and not again["running"] and again["total"] == 0, "nothing missing -> job ends at once")
            items = await library(http)
            check(all(items[n]["audio_copy"] for n in names) and not items["01 Song"]["audio_copy"],
                  "/api/library: audio_copy true for every video, false for the song")

            # ---- rescan: renamed -> copy removed; changed file -> stale (badge off, original again)
            jid, qid = ids["J6"], ids["Q5"]
            os.rename(lib / "clips" / "J6.mkv", lib / "clips" / "J6 renamed.mkv")
            future = time.time() + 60
            os.utime(lib / "clips" / "Q5.mkv", (future, future))
            async with http.post(f"{BASE}/api/rescan?wait=1") as r:
                res = await r.json()
            items = {it["id"]: it for it in (await library(http)).values()}
            check(res.get("renamed") == 1 and jid in items and not main_copy("J6").exists() and
                  not items[jid]["audio_copy"], "renamed file keeps its id, its copy is removed, badge off")
            check(main_copy("Q5").exists() and not items[qid]["audio_copy"], "changed file: old copy is stale, badge off")
            async with http.get(f"{BASE}/media/{qid}?audio=main") as r:
                await r.read()
                check(r.headers.get("X-Audio-Copy") == "original", "... and ?audio=main serves the original again")
            async with http.get(f"{BASE}/api/audio_copies") as r:
                st = await r.json()
            check(st["missing"] >= 1 and not st["running"], "no extraction after the scan (button only)")

            # ---- the TV goes away: the sink is told route false at once, and gets it on its next connect
            await drain(sink)
            await tv.close()
            check({"type": "route", "sink": False} in of(await drain(sink), "route"),
                  "TV disconnects -> sink gets route false")
            await sink.close()
            sink = await http.ws_connect(f"{WS}/ws?role=sink&session=tv")
            check(of(await drain(sink), "route") == [{"type": "route", "sink": False}],
                  "... a sink connecting with no TV is replayed route false")
            for ws in (tv, phone, remote, sink):
                await ws.close()
    finally:
        if fake is not None:
            fake.terminate()
        proc.terminate()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="atv-audiotest-"))
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
