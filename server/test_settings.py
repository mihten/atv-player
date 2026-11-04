#!/usr/bin/env python3
"""Server settings (web remote -> Settings): library folders (add / rename / move / remove, only under the allowed
bases), skip patterns, rescan schedule, server name, the OPTIONAL admin password, /api/status, audio-copy cache clear,
backup + restore (ids mapped through the stored file paths), and the atv-audio relay (audio_cmd / audio_info).
Unit checks for settings.py first, then a THROWAWAY server (own temp library, data dir and port).
    python3 test_settings.py   (needs ffmpeg + the server's deps)
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
import settings as S  # noqa: E402
import test_atv_audio as H  # noqa: E402

PORT = 18096
H.PORT, H.BASE = PORT, f"http://127.0.0.1:{PORT}"
H.WS = H.BASE.replace("http", "ws")
BASE = H.BASE
failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def unit(tmp):
    skip = S.skip_matcher(["_work", "*demo*", "live/old/*"])
    check(skip("a/_work/x.flac") and skip("_work") and skip("b/my DEMO take.flac") and skip("Live/Old/x.mkv"),
          "skip: folder name, file glob (case-insensitive), path pattern")
    check(not skip("a/work/x.flac") and not skip("live/new/x.mkv") and S.skip_matcher([]) is None, "skip: no false hits")
    now = time.mktime((2026, 11, 4, 3, 0, 0, 0, 0, -1))
    nxt = S.next_rescan({"mode": "daily", "hours": 6, "at": "04:15"}, None, now)
    check(time.localtime(nxt)[3:5] == (4, 15) and nxt - now == 4500, "daily 04:15 -> today 04:15 when it is 03:00")
    nxt = S.next_rescan({"mode": "daily", "hours": 6, "at": "02:00"}, None, now)
    check(nxt - now == 23 * 3600, "daily 02:00 at 03:00 -> tomorrow")
    check(S.next_rescan({"mode": "hours", "hours": 6, "at": "04:15"}, now - 3600, now) == now + 5 * 3600,
          "every 6 h, last scan 1 h ago -> in 5 h")
    check(S.next_rescan({"mode": "off", "hours": 6, "at": "04:15"}, None, now) is None, "off -> none")
    for bad in ("25:00", "4.15", "x"):
        try:
            S.clean_rescan({"at": bad})
            check(False, f"bad time {bad} refused")
        except S.SettingsError:
            check(True, f"bad time {bad} refused")
    a = S.hash_password("secret1")
    check(S.check_password(a, "secret1") and not S.check_password(a, "secret2"), "password hash / check")
    env = {"ATV_ROOTS": json.dumps({"m": str(tmp / "x" / "m")})}
    st = S.Settings(str(tmp / "unit-settings.json"), env)
    check(st.bases == [os.path.realpath(tmp / "x")], f"default base = parent of the first-run roots ({st.bases})")
    check(oct(os.stat(tmp / "unit-settings.json").st_mode & 0o777) == "0o600", "settings.json is 0600")
    check(S.slug("Music videos (live)", {"music-videos-live"}) == "music-videos-live-2", "slug unique")


async def req(http, method, path, **kw):
    async with http.request(method, BASE + path, **kw) as r:
        try:
            body = await r.json()
        except (aiohttp.ContentTypeError, ValueError):
            body = await r.text()
        return r.status, body


async def main(tmp):
    unit(tmp)
    tmp = Path(os.path.realpath(tmp))  # the server reports real paths (scratch dirs may be symlinks)
    lib = tmp / "lib"
    for n in range(1, 4):
        H.tone(lib / "music" / "Album" / f"0{n} Song{n}.flac", 3, 300 + 40 * n)
    H.tone(lib / "music" / "_work" / "Stem.flac", 3, 500)
    H.tone(lib / "music" / "Album" / "04 Demo take.flac", 3, 520)
    H.tone(lib / "videos" / "Clip1.flac", 3, 600)
    H.tone(lib / "extra" / "Bonus.flac", 3, 700)
    outside = tmp / "elsewhere"
    H.tone(outside / "Nope.flac", 3, 800)
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib / "music"), "videos": str(lib / "videos")}),
               ATV_ROOT_NAMES=json.dumps({"music": "Music"}), ATV_DATA=str(tmp / "data"),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT))
    env.pop("ATV_BONJOUR", None)
    env.pop("ATV_LIBRARY_BASES", None)
    proc = subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
            check(len(await H.wait_up(http, 6)) == 6, "first run: 6 items from ATV_ROOTS")
            saved = json.loads((tmp / "data" / "settings.json").read_text())
            check([r["key"] for r in saved["roots"]] == ["music", "videos"] and saved["roots"][0]["name"] == "Music"
                  and saved["rescan"] == {"mode": "daily", "hours": 6, "at": "04:15"},
                  "settings.json seeded from the environment (names, daily 04:15)")
            code, st = await req(http, "GET", "/api/settings")
            check(code == 200 and st["bases"] == [str(lib)] and st["admin"] is False and st["unlocked"] is True,
                  f"settings: base = {st.get('bases')}, no password, unlocked")
            music = next(r for r in st["roots"] if r["key"] == "music")
            check(music["items"] == 5 and music["online"], f"music root: 5 items, online ({music})")
            ids_before = {it["title"]: it["id"] for it in (await H.library(http)).values()}

            # ---- browse: only inside the bases
            code, b = await req(http, "GET", "/api/settings/browse")
            check(code == 200 and [d["path"] for d in b["dirs"]] == [str(lib)], "browse: the bases")
            code, b = await req(http, "GET", "/api/settings/browse", params={"path": str(lib)})
            check(code == 200 and [d["name"] for d in b["dirs"]] == ["extra", "music", "videos"], "browse: subfolders")
            code, _ = await req(http, "GET", "/api/settings/browse", params={"path": str(outside)})
            check(code == 400, "browse outside the bases refused")
            code, _ = await req(http, "GET", "/api/settings/browse", params={"path": str(lib / "music" / "..") + "/../elsewhere"})
            check(code == 400, "browse with .. out of the base refused")

            # ---- display name
            code, st = await req(http, "PUT", "/api/settings/roots/videos", json={"name": "Music videos"})
            async with http.get(BASE + "/api/browse") as r:
                roots = (await r.json())["roots"]
            check(code == 200 and {x["root"]: x["name"] for x in roots}["videos"] == "Music videos",
                  "rename: /api/browse shows the new display name")
            check({it["title"]: it["id"] for it in (await H.library(http)).values()} == ids_before, "rename keeps every id")

            # ---- add a folder (inside / outside / twice)
            code, _ = await req(http, "POST", "/api/settings/roots", json={"path": str(outside), "name": "x"})
            check(code == 400, "add outside the bases refused")
            code, _ = await req(http, "POST", "/api/settings/roots", json={"path": str(lib / "music"), "name": "dup"})
            check(code == 400, "add the same folder twice refused")
            code, st = await req(http, "POST", "/api/settings/roots", json={"path": str(lib / "extra"), "name": "Extra stuff"})
            check(code == 200 and any(r["key"] == "extra-stuff" for r in st["roots"]), "add: key from the name")
            check(len(await H.wait_up(http, 7)) == 7, "added folder scanned (7 items)")

            # ---- skip patterns
            code, st = await req(http, "PUT", "/api/settings/skip", json={"skip": ["_work", "*demo*"]})
            titles = set((await H.library(http)).keys())
            check(code == 200 and "Stem" not in titles and "04 Demo take" not in titles and len(titles) == 5,
                  f"skip _work + *demo*: 5 left ({sorted(titles)})")
            code, st = await req(http, "PUT", "/api/settings/skip", json={"skip": ["*"]})
            titles = set((await H.library(http)).keys())
            check(len(titles) == 0, f"skip everything: the roots are empty, not offline (items go) ({sorted(titles)})")
            code, st = await req(http, "PUT", "/api/settings/skip", json={"skip": []})
            check(len(await H.wait_up(http, 7)) == 7, "skip cleared: 7 again")

            # ---- move a folder's path: ids kept when the files are the same
            shutil.copytree(lib / "extra", lib / "extra2")
            bonus = (await H.library(http))["Bonus"]["id"]
            code, st = await req(http, "PUT", "/api/settings/roots/extra-stuff", json={"path": str(lib / "extra2")})
            lib_now = await H.library(http)
            check(code == 200 and lib_now.get("Bonus", {}).get("id") == bonus, "new path, same files: the id stays")

            # ---- remove a folder: usage shown before, items gone after
            # (the skip-everything round removed every item: they came back with NEW ids, as they must)
            ids_before = {it["title"]: it["id"] for it in lib_now.values()}
            async with http.post(BASE + "/api/playlists", json={"name": "P", "ids": [bonus, ids_before["01 Song1"]]}) as r:
                await r.json()
            remote = await H.connect(http, "role=remote&session=tv")
            await remote.send_json({"type": "cmd", "cmd": "love", "id": bonus, "on": True})
            await asyncio.sleep(0.4)
            code, st = await req(http, "GET", "/api/settings")
            ex = next(r for r in st["roots"] if r["key"] == "extra-stuff")
            check(ex["items"] == 1 and ex["playlist_entries"] == 1 and ex["loved"] == 1,
                  f"usage before removing: 1 item, 1 playlist entry, 1 heart ({ex})")
            code, st = await req(http, "DELETE", "/api/settings/roots/extra-stuff")
            check(code == 200 and "Bonus" not in await H.library(http) and len(await H.library(http)) == 6,
                  "removed: its item is gone after the (waited) rescan")

            # ---- rescan schedule
            code, st = await req(http, "PUT", "/api/settings/rescan", json={"mode": "hours", "hours": 3})
            last = st.get("next_rescan") or 0
            check(code == 200 and st["rescan"]["mode"] == "hours" and 3 * 3600 - 120 < last - time.time() <= 3 * 3600,
                  f"every 3 h: next in ~3 h ({last - time.time():.0f} s)")
            code, _ = await req(http, "PUT", "/api/settings/rescan", json={"mode": "daily", "at": "26:00"})
            check(code == 400, "bad time refused")
            code, st = await req(http, "PUT", "/api/settings/rescan", json={"mode": "off"})
            check(st["next_rescan"] is None, "off: no next rescan")

            # ---- server name
            code, st = await req(http, "PUT", "/api/settings/server", json={"name": "Living room"})
            check((await H.state(http, "tv"))["server_name"] == "Living room", "server name in the state")

            # ---- status + cache
            code, stat = await req(http, "GET", "/api/status")
            check(code == 200 and stat["items"]["songs"] == 6 and stat["server_name"] == "Living room"
                  and {"audio_bytes", "art_bytes"} <= set(stat["cache"]) and stat["rescan"]["mode"] == "off",
                  f"status: {stat.get('items')}, cache fields, rescan")
            (tmp / "cache" / "audio").mkdir(parents=True, exist_ok=True)
            (tmp / "cache" / "audio" / "999.main.mka").write_bytes(b"x" * 1000)
            (tmp / "cache" / "audio" / "998.main.1.2.tmp.mka").write_bytes(b"x")
            code, cl = await req(http, "POST", "/api/cache/clear")
            check(code == 200 and cl["removed"] == 1 and (tmp / "cache" / "audio" / "998.main.1.2.tmp.mka").exists(),
                  "cache clear: copies gone, a copy in progress kept")

            # ---- backup / restore (with ids that change)
            await remote.send_json({"type": "cmd", "cmd": "love", "id": ids_before["02 Song2"], "on": True})
            await asyncio.sleep(0.3)
            code, bk = await req(http, "GET", "/api/backup")
            check(code == 200 and bk["format"] == "atv-media-backup" and str(ids_before["02 Song2"]) in bk["files"]
                  and any(p["name"] == "P" for p in bk["playlists"]), "backup: playlists, hearts, files")
            # wipe: remove the music folder, add it again under another key -> every id changes
            await req(http, "DELETE", "/api/settings/roots/music")
            await req(http, "POST", "/api/settings/roots", json={"path": str(lib / "music"), "name": "Songs"})
            await H.wait_up(http, 6)
            now_ids = {it["title"]: it["id"] for it in (await H.library(http)).values()}
            check(now_ids["02 Song2"] != ids_before["02 Song2"], "library rebuilt: new ids")
            code, res = await req(http, "POST", "/api/backup", data=json.dumps(bk))
            check(code == 200 and res["playlists"] >= 1 and res["loved"] == 1 and res["missing"] == 1,
                  f"restore: mapped through the file paths, Bonus (gone) missing ({res})")
            async with http.get(BASE + "/api/playlists") as r:
                pls = (await r.json())["playlists"]
            p = next(x for x in pls if x["name"] == "P")
            async with http.get(BASE + f"/api/playlists/{p['id']}") as r:
                items = (await r.json())["items"]
            check([i["id"] for i in items] == [now_ids["01 Song1"]], "restored playlist points at the new id")
            async with http.get(BASE + "/api/auto") as r:
                loved = next(a for a in await r.json() if a["key"] == "loved")
            check(loved["count"] == 1, "restored heart on the new id")
            code, _ = await req(http, "POST", "/api/backup", data=b"{\"format\": \"x\"}")
            check(code == 400, "not a backup: refused")

            # ---- atv-audio relay
            sink = await http.ws_connect(f"{H.WS}/ws?role=sink&session=tv")
            await H.drain(sink)
            code, _ = await req(http, "POST", "/api/atv-audio", json={"op": "connect", "address": "AA:BB"})
            msgs = await H.drain(sink)
            check(code == 200 and {"type": "audio_cmd", "op": "connect", "address": "AA:BB"} in msgs, "audio_cmd relayed")
            code, _ = await req(http, "POST", "/api/atv-audio", json={"op": "rm -rf"})
            check(code == 400, "unknown op refused")
            await sink.send_json({"type": "audio_info", "outputs": [{"key": "bluez_output.x", "name": "Headphones",
                                  "kind": "bluetooth", "default": True}], "devices": [{"address": "AA:BB", "name": "HP",
                                  "paired": True, "connected": False, "audio": True}], "can_bluetooth": True,
                                  "can_outputs": True, "junk": "x" * 5000})
            await asyncio.sleep(0.3)
            a = (await H.state(http, "box"))["sink"]["audio"]
            check(a and a["devices"][0]["name"] == "HP" and "junk" not in a, f"audio_info in the box state ({a})")
            await sink.close()
            await asyncio.sleep(0.3)
            code, _ = await req(http, "POST", "/api/atv-audio", json={"op": "refresh"})
            check(code == 503, "no sink: 503")

            # ---- optional admin password
            code, r = await req(http, "POST", "/api/settings/password", json={"password": "abc"})
            check(code == 400, "too short refused")
            code, r = await req(http, "POST", "/api/settings/password", json={"password": "secret123"})
            check(code == 200 and r["admin"] is True, "password set (this browser stays unlocked)")
            code, st = await req(http, "GET", "/api/settings")
            check(st["admin"] and st["unlocked"], "setter is unlocked")
            async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as other:
                async def oreq(method, path, **kw):
                    async with other.request(method, BASE + path, **kw) as rr:
                        return rr.status, await rr.text()
                code, txt = await oreq("GET", "/api/settings")
                check(code == 200 and json.loads(txt)["unlocked"] is False and "hash" not in txt and "salt" not in txt,
                      "other browser: settings readable, locked, no hash in the answer")
                for method, path, kw in (("PUT", "/api/settings/skip", {"json": {"skip": []}}),
                                         ("POST", "/api/cache/clear", {}), ("GET", "/api/backup", {}),
                                         ("GET", "/api/settings/browse", {}), ("POST", "/api/atv-audio", {"json": {"op": "refresh"}}),
                                         ("POST", "/api/settings/password", {"json": {"password": ""}})):
                    code, _ = await oreq(method, path, **kw)
                    check(code == 403, f"locked: {method} {path} -> 403")
                code, _ = await oreq("GET", "/api/status")
                check(code == 200, "status stays readable")
                t0 = time.time()
                code, _ = await oreq("POST", "/api/settings/unlock", json={"password": "wrong"})
                check(code == 403 and time.time() - t0 >= 0.9, "wrong password: 403 after a 1 s delay")
                code, _ = await oreq("POST", "/api/settings/unlock", json={"password": "secret123"})
                code2, _ = await oreq("PUT", "/api/settings/rescan", json={"mode": "daily", "at": "04:15"})
                check(code == 200 and code2 == 200, "unlocked with the right password")
            async with http.get(BASE + "/api/library") as r:
                check(r.status == 200, "playback APIs never need the password")
            code, r = await req(http, "POST", "/api/settings/password", json={"password": ""})
            check(code == 200 and r["admin"] is False, "password removed")
            saved = json.loads((tmp / "data" / "settings.json").read_text())
            check(saved["admin"] is None and saved["server_name"] == "Living room", "settings.json: no password, name kept")
            await remote.close()
    finally:
        proc.terminate()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()
    log = (tmp / "server.log").read_text()
    check("Traceback" not in log, "no tracebacks in the server log")


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="atv-settingstest-"))
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
