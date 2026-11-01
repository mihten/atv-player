#!/usr/bin/env python3
"""Song <-> music video versions: grouping, labels, order, switch_version (keeps / resets the position) and the
"Prefer videos" queue flag (on / off, no doubles, hand picks stay). Throwaway temp library, data dir and port only.
    python3 test_versions.py   (needs ffmpeg + the server's deps)
"""
import asyncio
import importlib
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
PORT = 18098
BASE = f"http://127.0.0.1:{PORT}"
failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def song(path, seconds, artist, title, freq=440, ext_args=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    ff("-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}", *ext_args,
       "-metadata", f"title={title}", "-metadata", f"artist={artist}", str(path))


def video(path, seconds):
    path.parent.mkdir(parents=True, exist_ok=True)
    ff("-f", "lavfi", "-i", f"testsrc=size=160x90:rate=25:duration={seconds}",
       "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}", "-c:v", "libx264", "-c:a", "aac", str(path))


def make_library(lib):
    album = lib / "music" / "Lumen Drift" / "Afterglow"
    song(album / "01 Afterglow.flac", 10, "Lumen Drift", "Afterglow")
    song(album / "02 Afterglow (Live).flac", 21, "Lumen Drift", "Afterglow (Live)", 500)
    song(album / "03 Kite.flac", 4, "Lumen Drift", "Kite", 600)
    song(lib / "music" / "Twin" / "Dup" / "01 Dup.flac", 3, "Twin", "Dup", 700)
    song(lib / "music" / "Twin" / "Dup" / "02 Dup.mp3", 3, "Twin", "Dup", 700)  # hidden duplicate of the FLAC
    vids = lib / "videos"
    video(vids / "Lumen Drift - Afterglow (Official Video).mkv", 12)  # same cut as the song (10 s)
    video(vids / "Lumen Drift - Afterglow (Live at Wembley 1986).mkv", 20)
    video(vids / "Lumen Drift feat. Guest - Afterglow (Lyric Video).mkv", 10)
    video(vids / "Quiet - Tune (Official Video).mkv", 3)
    video(vids / "Quiet - Tune (Acoustic Version).mkv", 4)
    video(vids / "Twin - Dup (Official Music Video).mkv", 3)


def unit(tmp):
    os.environ.update(ATV_ROOTS=json.dumps({"music": str(tmp / "none")}), ATV_DATA=str(tmp / "udata"),
                      ATV_CACHE=str(tmp / "ucache"))
    srv = importlib.import_module("server")
    cases = [
        ("Afterglow (Official Video)", "video", "Official video", 0),
        ("Afterglow (Official Music Video) [HD]", "video", "Official video", 0),
        ("Afterglow (Live at Wembley 1986)", "video", "Live at Wembley 1986", 2),
        ("Afterglow (Acoustic Version)", "video", "Acoustic Version", 2),
        ("Afterglow (Lyric Video)", "video", "Lyric video", 1),
        ("Afterglow (Official Visualizer)", "video", "Visualizer", 1),
        ("Afterglow (Official Audio)", "video", "Audio", 1),
        ("Afterglow", "video", "Video", 0),
        ("Afterglow", "audio", "Song", 0),
        ("Afterglow (Live)", "audio", "Live", 2),
        ("Afterglow (Remastered 2011)", "audio", "Remastered 2011", 0),
        ("Afterglow (feat. Guest)", "audio", "Song", 0),
        ("Afterglow (Stayin Alive Mix)", "video", "Stayin Alive Mix", 2),
        ("Afterglow (Club Remix)", "video", "Club Remix", 2),
    ]
    for title, kind, label, cls in cases:
        got = srv.version_label(title, kind)
        check(got == (label, cls), f"label {title!r} ({kind}) -> {got}")
    key = srv.version_key
    check(key({"artist": "Lumen Drift feat. Guest", "title": "Afterglow (Lyric Video)"}) ==
          key({"artist": "Lumen Drift", "title": "Afterglow"}) == ("lumen drift", "afterglow"),
          "group key: main artist (no feat.), title without brackets")
    check(key({"artist": "Lumen Drift", "title": "Afterglow ft. Guest"}) == ("lumen drift", "afterglow"),
          "group key: title without ft. ...")
    check(key({"artist": "", "title": "Afterglow"}) is None, "no artist: not linked")


async def next_msg(ws, types, timeout=8):
    while True:
        msg = await asyncio.wait_for(ws.receive_json(), timeout)
        if msg.get("type") in types:
            return msg


async def state(http):
    async with http.get(f"{BASE}/api/state?session=tv") as r:
        return await r.json()


def start(tmp, env):
    return subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)


def stop(proc):
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


async def wait_up(http, count):
    for _ in range(150):
        try:
            async with http.get(f"{BASE}/api/library?all=1") as r:
                items = (await r.json())["items"]
                if len(items) == count:
                    return items
        except (aiohttp.ClientError, KeyError, ValueError):
            pass
        await asyncio.sleep(0.2)
    return []


async def e2e(tmp):
    lib = tmp / "lib"
    make_library(lib)
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(tmp / "data"),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT))
    env.pop("ATV_BONJOUR", None)
    proc = start(tmp, env)
    try:
        async with aiohttp.ClientSession() as http:
            items = await wait_up(http, 11)
            check(len(items) == 11, f"library of 11 items ({len(items)})")
            by = {(it["title"], it["kind"], it["hidden"]): it for it in items}
            ids = {}
            for it in items:
                if not it["hidden"]:
                    ids[it["title"] if it["kind"] == "audio" else "v:" + it["title"]] = it["id"]
            hidden = next(it for it in items if it["hidden"])
            S, SL, K, D = ids["Afterglow"], ids["Afterglow (Live)"], ids["Kite"], ids["Dup"]
            VO, VL, VY = (ids["v:Afterglow (Official Video)"], ids["v:Afterglow (Live at Wembley 1986)"],
                          ids["v:Afterglow (Lyric Video)"])
            TO, TA, DV = ids["v:Tune (Official Video)"], ids["v:Tune (Acoustic Version)"], ids["v:Dup (Official Music Video)"]
            summ = {it["id"]: it for it in items}
            check(summ[S]["videos"] == 3 and summ[SL]["videos"] == 3, "summary: songs count their 3 videos")
            check(summ[K]["videos"] is None and summ[VO]["videos"] is None, "summary: no videos -> null; videos -> null")
            check(summ[D]["videos"] == 1 and hidden["videos"] is None, "hidden duplicate is not a member")

            async with http.get(f"{BASE}/api/item/{S}") as r:
                full = await r.json()
            vs = full["versions"]
            check([v["id"] for v in vs] == [S, SL, VO, VY, VL],
                  f"versions: songs first, then videos by class (official, lyric, live): {[v['label'] for v in vs]}")
            check([v["label"] for v in vs] == ["Song", "Live", "Official video", "Lyric video", "Live at Wembley 1986"],
                  "version labels")
            check(vs[2]["kind"] == "video" and abs(vs[2]["duration"] - 12) < 0.5, "version entry: kind + duration")
            async with http.get(f"{BASE}/api/item/{TA}") as r:
                tune = (await r.json())["versions"]
            check([v["id"] for v in tune] == [TO, TA], "videos without a song are linked to each other")
            async with http.get(f"{BASE}/api/item/{K}") as r:
                check((await r.json())["versions"] is None, "an item without versions: versions null")

            player = await http.ws_connect(f"{BASE}/ws?role=player&kind=tv")
            await next_msg(player, {"state"})

            async def cmd(name, **args):
                await player.send_json({"type": "cmd", "cmd": name, **args})

            async def playing(item):
                await player.send_json({"type": "status", "item": item, "playing": True, "position": 1, "duration": 10})

            # --- switch_version: same cut keeps the position, another cut starts at 0; history not a new start
            await cmd("play_ids", ids=[S, K])
            m = await next_msg(player, {"load"})
            check(m["item"]["id"] == S and len(m["item"]["versions"]) == 5, "load carries the versions")
            await playing(S)
            await asyncio.sleep(0.2)
            await cmd("switch_version", id=VO, position=4.5)
            m = await next_msg(player, {"load"})
            check(m["item"]["id"] == VO and m["position"] == 4.5, f"switch to the same cut keeps the position ({m['position']})")
            st = await state(http)
            check(st["queue"] == [VO, K] and st["index"] == 0, "switch replaces the current queue entry")
            await cmd("switch_version", id=VL, position=4.5)
            m = await next_msg(player, {"load"})
            check(m["item"]["id"] == VL and m["position"] == 0, f"switch to another cut starts at 0 ({m['position']})")
            await cmd("switch_version", id=TO, position=1)
            try:
                await next_msg(player, {"load"}, timeout=0.8)
                check(False, "switch to an item outside the group is ignored")
            except asyncio.TimeoutError:
                check(True, "switch to an item outside the group is ignored")
            async with http.get(f"{BASE}/api/history?limit=10") as r:
                hist = {h["id"] for h in (await r.json())["recent"]}
            check(S in hist and VL in hist, "history: the switched-to version counts as heard")

            # --- prefer videos: turned on swaps the upcoming songs, off swaps them back
            await cmd("play_collection", collection="music")
            m = await next_msg(player, {"load"})
            st = await state(http)
            check(st["queue"] == [S, SL, K, D] and not st["prefer_videos"], f"music in order, flag off: {st['queue']}")
            await cmd("prefer_videos", on=True)
            await asyncio.sleep(0.3)
            st = await state(http)
            check(st["prefer_videos"] and st["queue"] == [S, VL, K, DV],
                  f"on: upcoming songs -> videos of their own class (live song -> live video): {st['queue']}")
            await cmd("shuffle", on=True)
            await cmd("shuffle", on=False)
            await asyncio.sleep(0.3)
            check((await state(http))["queue"] == [S, VL, K, DV], "shuffle on + off keeps the swapped entries")
            await cmd("prefer_videos")  # missing on = toggle
            await asyncio.sleep(0.3)
            st = await state(http)
            check(not st["prefer_videos"] and st["queue"] == [S, SL, K, D], f"off: swapped entries back to songs: {st['queue']}")

            # --- a new queue with the flag on; no doubles
            await cmd("prefer_videos", on=True)
            await cmd("play_ids", ids=[S, K], start_index=0)
            m = await next_msg(player, {"load"})
            check(m["item"]["id"] == VO, "flag on: a new queue starts with the song's video")
            await cmd("play_collection", collection="all")
            await next_msg(player, {"load"})
            q = (await state(http))["queue"]
            check(len(q) == len(set(q)) and not {S, SL, D} & set(q) and K in q and len(q) == 7,
                  f"'all' with the flag on: songs with videos dropped (videos already there), no doubles: {q}")
            await cmd("play_collection", collection="music", start_id=K)
            m = await next_msg(player, {"load"})
            st = await state(http)
            check(m["item"]["id"] == K and st["queue"] == [VO, VL, K, DV] and st["index"] == 2,
                  f"start_id maps onto the new list: {st['queue']} @ {st['index']}")
            await cmd("enqueue", ids=[S, TA], mode="end")
            await asyncio.sleep(0.3)
            q = (await state(http))["queue"]
            check(q == [VO, VL, K, DV, TA], f"enqueue: a song whose video is queued is dropped: {q}")
            await cmd("play_ids", ids=[K])
            await next_msg(player, {"load"})
            await cmd("enqueue", ids=[S, VO], mode="end")
            await cmd("enqueue", ids=[TA, D, DV], mode="end")
            await asyncio.sleep(0.3)
            q = (await state(http))["queue"]
            check(q == [K, VO, TA, DV], f"enqueue a song together with its own video: the video once: {q}")

            # --- on + off gives the dropped songs back (no song lost)
            await cmd("prefer_videos", on=False)
            await cmd("play_ids", ids=[K, S, VO, D, DV])
            await next_msg(player, {"load"})
            await cmd("prefer_videos", on=True)
            await asyncio.sleep(0.3)
            st = await state(http)
            check(st["queue"] == [K, VO, DV], f"on: songs whose video is queued are dropped: {st['queue']}")
            await cmd("prefer_videos", on=False)
            await asyncio.sleep(0.3)
            st = await state(http)
            check(st["queue"] == [K, VO, S, DV, D] and sorted(st["queue"]) == sorted([K, S, VO, D, DV]),
                  f"off: the dropped songs come back after their video: {st['queue']}")

            # --- a video queued by hand is not turned into the song by an older swap
            await cmd("prefer_videos", on=True)
            await cmd("play_ids", ids=[S, K])
            await next_msg(player, {"load"})  # VO (swapped for S)
            await cmd("enqueue", ids=[VO], mode="end")
            await cmd("prefer_videos", on=False)
            await asyncio.sleep(0.3)
            q = (await state(http))["queue"]
            check(q == [VO, K, VO], f"off: a video enqueued by hand stays the video: {q}")
            await cmd("prefer_videos", on=True)

            # --- a version picked by hand is never undone by the flag (also not on a repeat-all lap)
            await cmd("play_ids", ids=[S, K])
            await next_msg(player, {"load"})  # VO
            await cmd("switch_version", id=VL)
            await next_msg(player, {"load"})
            await cmd("switch_version", id=S)
            m = await next_msg(player, {"load"})
            check(m["item"]["id"] == S, "switch back to the song")
            await cmd("next")
            await next_msg(player, {"load"})
            await cmd("next")  # repeat all: new lap, the flag is applied to the whole queue
            m = await next_msg(player, {"load"})
            st = await state(http)
            check(m["item"]["id"] == S and st["queue"] == [S, K], f"hand-picked song stays a song on a new lap: {st['queue']}")
            await cmd("play_ids", ids=[K, S])
            await next_msg(player, {"load"})
            await cmd("prefer_videos", on=False)
            await asyncio.sleep(0.3)
            check((await state(http))["queue"] == [K, S], "off: a swapped upcoming entry goes back to the song")
            await cmd("prefer_videos", on=True)
            await asyncio.sleep(0.3)
            check((await state(http))["queue"] == [K, VO], "on again: swapped again")

            # --- a queue that follows its playlist keeps the swaps and the playing version
            async with http.post(f"{BASE}/api/playlists", json={"name": "P", "ids": [S, K]}) as r:
                pid = (await r.json())["id"]
            await cmd("play_collection", collection="playlist", playlist=pid)
            m = await next_msg(player, {"load"})
            check(m["item"]["id"] == VO, "playlist with the flag on starts with the video")
            async with http.put(f"{BASE}/api/playlists/{pid}", json={"items": [S, K, SL]}) as r:
                check(r.status == 200, "playlist edited")
            await asyncio.sleep(0.3)
            st = await state(http)
            check(st["queue"] == [VO, K, VL] and st["index"] == 0, f"followed edit: swaps kept, new song swapped: {st['queue']}")
            async with http.put(f"{BASE}/api/playlists/{pid}", json={"items": [S, K, SL, VO]}) as r:
                check(r.status == 200, "playlist edited: the playing song's video added")
            await asyncio.sleep(0.3)
            st = await state(http)
            check(st["queue"] == [VO, K, VL] and st["index"] == 0 and st["current"]["id"] == VO,
                  f"followed edit: the playing video is not doubled: {st['queue']}")
            async with http.put(f"{BASE}/api/playlists/{pid}", json={"items": [S, K, SL]}) as r:
                check(r.status == 200, "playlist edited back")
            await asyncio.sleep(0.3)
            check((await state(http))["queue"] == [VO, K, VL], "followed edit back: same queue")

            # --- the flag survives a restart
            await player.close()
            stop(proc)
            saved = json.loads((tmp / "data" / "state.json").read_text())
            check(saved.get("prefer_videos") is True and str(VO) in saved.get("swapped", {}), "flag and swaps saved")
            proc = start(tmp, env)
            await wait_up(http, 11)
            st = await state(http)
            check(st["prefer_videos"] is True and st["queue"] == [VO, K, VL], "flag restored after a restart")

            # --- an old swap does not turn a video added to the followed playlist into its song (flag off, new lap)
            player = await http.ws_connect(f"{BASE}/ws?role=player&kind=tv")
            await next_msg(player, {"state"})
            try:  # a new player gets the current item again
                await next_msg(player, {"load"}, timeout=1)
            except asyncio.TimeoutError:
                pass
            async with http.post(f"{BASE}/api/playlists", json={"name": "Q", "ids": [S, D, K]}) as r:
                qid = (await r.json())["id"]
            await cmd("play_collection", collection="playlist", playlist=qid)
            await next_msg(player, {"load"})  # VO
            await cmd("next")
            m = await next_msg(player, {"load"})
            check(m["item"]["id"] == DV, "playlist with the flag on: the second song plays as its video")
            await cmd("prefer_videos", on=False)
            async with http.put(f"{BASE}/api/playlists/{qid}", json={"items": [S, D, K, VO]}) as r:
                check(r.status == 200, "playlist edited: a video added as its own entry")
            await asyncio.sleep(0.3)
            st = await state(http)
            check(st["queue"] == [S, DV, K, VO] and st["index"] == 1, f"followed edit with the flag off: {st['queue']}")
            for _ in range(3):  # K, VO, then the repeat-all lap
                await cmd("next")
                m = await next_msg(player, {"load"})
            st = await state(http)
            check(m["item"]["id"] == S and st["queue"] == [S, D, K, VO],
                  f"new lap: the added video stays a video, no song twice: {st['queue']}")
            await player.close()
            log = (tmp / "server.log").read_text()
            check("Traceback" not in log, "no tracebacks in the server log")
    finally:
        stop(proc)


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="atv-vertest-"))
    try:
        unit(tmp)
        asyncio.run(e2e(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
