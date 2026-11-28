#!/usr/bin/env python3.13
"""LTVPlayer 0.8.2: the video decoder is given back (his choice 2026-11-28) on leaving the app and after a video was paused
for 10 minutes inside the app (?release=<ms> shortens it), in desktop Chromium as the ltv player of a THROWAWAY server.

    SERVER_PY=<python with aiohttp mutagen pillow> PYTHONPATH=<dir with aiohttp> python3.13 ltv/test_ltv_release.py [outdir]

Why: the LG screen saver (Aerial) plays its scenes only while no app holds a video decoder, else a clock on black.
Checks: hidden -> `release #id at t (left the app)`, the element empty, the item + position kept (status / server
state); visible on the player -> `restore`, same item, paused, same position; paused N ms -> release; a key in the
player -> restore + the key still acts (◀ = jump back from the kept position); Green on a released item -> plays from
there; released on Home -> opening the player restores; Stop while released; songs are never released; a playing video
is never released; a reconnect resync keeps it released.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("LTV_TEST_PORT", "18129"))
BASE = f"http://127.0.0.1:{PORT}"
PY = os.environ.get("SERVER_PY", sys.executable)
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="ltv-release-shots-"))
OUT.mkdir(parents=True, exist_ok=True)
tmp = Path(tempfile.mkdtemp(prefix="ltv-release-"))
fails = []
REL_MS = 3000


def check(cond, label):
    print(time.strftime("%H:%M:%S ") + ("PASS " if cond else "FAIL ") + label, flush=True)
    if not cond:
        fails.append(label)


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


lib = tmp / "lib"
(lib / "videos").mkdir(parents=True)
(lib / "songs").mkdir()
ff("-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=60", "-f", "lavfi", "-i", "sine=frequency=440:duration=60",
   "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "200k", "-g", "25", "-c:a", "libopus",
   "-metadata", "title=Keep Clip", "-metadata", "artist=Clipper", str(lib / "videos" / "Keep Clip.webm"))
ff("-f", "lavfi", "-i", "sine=frequency=330:duration=60", "-c:a", "flac", "-metadata", "title=Plain Song",
   "-metadata", "artist=Tester", str(lib / "songs" / "Plain Song.flac"))
N_ITEMS = 2

env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(tmp / "data"), ATV_CACHE=str(tmp / "cache"),
           ATV_PORT=str(PORT))
env.pop("ATV_BONJOUR", None)
srvlog = tmp / "server.log"
srv = subprocess.Popen([PY, str(REPO / "server" / "server.py")], env=env, stdout=open(srvlog, "a"), stderr=subprocess.STDOUT)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=5) as r:
        return json.loads(r.read())


def state():
    return get("/api/state?session=ltv")


def journal(pattern=""):
    return [l.split("player[ltv]: ", 1)[1] for l in srvlog.read_text(errors="replace").splitlines()
            if "player[ltv]: " in l and re.search(pattern, l)]


def server_up(n, secs=60):
    end = time.time() + secs
    while time.time() < end:
        try:
            if len(get("/api/library?all=1")["items"]) >= n:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


try:
    check(server_up(N_ITEMS), f"throwaway library scanned ({N_ITEMS} items)")
    items = {it["title"]: it["id"] for it in get("/api/library?all=1")["items"]}
    V, S = items["Keep Clip"], items["Plain Song"]

    with sync_playwright() as p:
        b = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = b.new_page(viewport={"width": 1920, "height": 1080})
        errors, sent = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("websocket", lambda ws: ws.on("framesent", lambda f: sent.append(f if isinstance(f, str) else "")))

        def cmds(name):
            out = []
            for f in sent:
                try:
                    m = json.loads(f)
                except ValueError:
                    continue
                if m.get("type") == "cmd" and m.get("cmd") == name:
                    out.append(m)
            return out

        def wait(cond, secs=10.0):
            end = time.time() + secs
            while time.time() < end:
                try:
                    if cond():
                        return True
                except Exception:
                    pass
                page.wait_for_timeout(150)
            try:
                return bool(cond())
            except Exception:
                return False

        def js(expr, arg=None):
            return page.evaluate(expr) if arg is None else page.evaluate(expr, arg)

        def tvkey(code):
            js("""(code) => { for (const t of ['keydown', 'keyup']) { const e = new KeyboardEvent(t, {bubbles: true, cancelable: true});
                Object.defineProperty(e, 'keyCode', {get: () => code}); document.dispatchEvent(e); } }""", code)

        def set_hidden(h):
            js("(h) => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => h});"
               " Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => h ? 'hidden' : 'visible'});"
               " document.dispatchEvent(new Event('visibilitychange')); }", h)

        def src():
            return js("ltv.engine.v.getAttribute('src')")

        def released():
            return js("!!ltv.engine.released")

        def cur():
            st = state()
            return st["current"]["id"] if st.get("current") else None

        def n(pattern):
            return len(journal(pattern))

        remote = b.new_page()
        remote.goto(BASE + "/api/state?session=ltv")
        remote.evaluate("""() => new Promise((ok) => {
            window.r = new WebSocket(location.origin.replace('http', 'ws') + '/ws?role=remote&session=ltv'); window.r.onopen = ok; })""")

        def rc(name, **args):
            remote.evaluate("(m) => window.r.send(JSON.stringify(m))", {"type": "cmd", "cmd": name, **args})

        page.goto(BASE + f"/static/ltv/index.html?from=test&release={REL_MS}")
        check(wait(lambda: state()["players"] == 1 and js("ltv.screen") == "home"), "the page joins as the ltv player, Home")
        check(wait(lambda: any(re.match(r"ltv 0\.8\.2 ", l) for l in journal())), "start line: ltv 0.8.2")

        # ---------------------------------------------------------------- leaving the app
        rc("play_ids", ids=[V], start_index=0)
        check(wait(lambda: cur() == V and js("ltv.screen") == "player" and state()["playing"]), "a video plays")
        check(wait(lambda: js("ltv.engine.v.currentTime") > 14, 20), "... past 14 s")
        set_hidden(True)
        check(wait(lambda: n(rf": release #{V} at [\d.]+ \(left the app\): video decoder free$") == 1),
              "hidden: 'release #id at t (left the app): video decoder free'")
        at = js("ltv.engine.released && ltv.engine.released.at")
        check(src() is None and js("ltv.engine.v.readyState") == 0, "... the element is empty (no src, readyState 0)")
        check(at is not None and at > 14, f"... the position is kept ({at})")
        check(wait(lambda: state()["playing"] is False and abs(state()["position"] - at) < 0.6),
              f"... the server: paused at the kept position ({state()['position']})")
        page.wait_for_timeout(1500)
        check(abs(state()["position"] - at) < 0.6, "... still there a second later (status reports the kept position)")
        set_hidden(False)
        check(wait(lambda: n(rf": restore #{V} at [\d.]+ \(back in the app\)$") == 1), "visible on the player: 'restore … (back in the app)'")
        check(wait(lambda: src() == f"/media/{V}" and js("ltv.engine.v.readyState") >= 1), "... the same item is open again")
        check(wait(lambda: abs(js("ltv.engine.v.currentTime") - at) < 1.0), f"... at the same place ({js('ltv.engine.v.currentTime'):.1f} vs {at:.1f})")
        check(js("ltv.engine.v.paused") and state()["playing"] is False, "... paused")

        # ---------------------------------------------------------------- paused inside the app
        check(wait(lambda: n(rf": release #{V} at [\d.]+ \(paused \d+ s\): video decoder free$") == 1, REL_MS / 1000 + 8),
              f"paused {REL_MS / 1000} s inside the app: 'release … (paused N s)'")
        at2 = js("ltv.engine.released && ltv.engine.released.at")
        check(src() is None and abs(at2 - at) < 1.0, f"... element empty, position kept ({at2})")
        page.screenshot(path=str(OUT / "release-player-released.png"))
        n_seek = len(cmds("seek"))
        page.keyboard.press("ArrowLeft")
        check(wait(lambda: n(rf": restore #{V} at [\d.]+ \(key\)$") == 1), "a key (◀) in the player: 'restore … (key)'")
        check(wait(lambda: len(cmds("seek")) == n_seek + 1 and abs(cmds("seek")[-1]["position"] - (at2 - 10)) < 1.0),
              f"... and the key still acts: seek to the kept position - 10 s ({cmds('seek')[-1]['position'] if cmds('seek') else None})")
        check(wait(lambda: js("ltv.engine.v.readyState") >= 1 and abs(js("ltv.engine.v.currentTime") - (at2 - 10)) < 1.2),
              f"... the picture opens there, not at the old place ({js('ltv.engine.v.currentTime'):.1f})")
        check(js("ltv.engine.v.paused"), "... still paused")

        # ---------------------------------------------------------------- Green on a released item
        check(wait(lambda: released(), REL_MS / 1000 + 8), "released again after the pause time")
        at3 = js("ltv.engine.released.at")
        tvkey(404)
        check(wait(lambda: state()["playing"] is True and not js("ltv.engine.v.paused")), "Green: plays again")
        check(wait(lambda: js("ltv.engine.v.currentTime") > at3 - 0.5) and js("ltv.engine.v.currentTime") < at3 + 4,
              f"... from the kept position ({js('ltv.engine.v.currentTime'):.1f} vs {at3:.1f})")
        page.wait_for_timeout(REL_MS + 1500)
        check(not released() and src() is not None, "a playing video is never released")

        # ---------------------------------------------------------------- released on Home: opening the player restores
        tvkey(404)
        check(wait(lambda: state()["playing"] is False), "Green: paused")
        page.keyboard.press("Escape")
        check(wait(lambda: js("ltv.screen") == "home"), "Back: Home")
        check(wait(lambda: released(), REL_MS / 1000 + 8), "paused behind Home: released after the pause time")
        n_r = n(r"\(player shown\)$")
        for _ in range(8):
            page.keyboard.press("ArrowUp")
        page.wait_for_timeout(300)
        page.keyboard.press("Enter")
        check(wait(lambda: js("ltv.screen") == "player"), "OK on the Now Playing card: the player")
        check(wait(lambda: n(r"\(player shown\)$") == n_r + 1 and src() == f"/media/{V}"), "... 'restore … (player shown)', the item open")

        # ---------------------------------------------------------------- reconnect while released: stays released
        check(wait(lambda: released(), REL_MS / 1000 + 8), "released again")
        js("ltv.sock.ws && ltv.sock.ws.close()")
        check(wait(lambda: n(rf": load #{V} resync: kept released at [\d.]+$") >= 1, 20) and released() and src() is None,
              "socket reconnect while released: 'resync: kept released', element still empty")

        # ---------------------------------------------------------------- Stop while released
        tvkey(403)
        check(wait(lambda: js("ltv.engine.stopped") and not released()), "Red (Stop) while released: stopped, nothing released")
        check(wait(lambda: js("ltv.screen") != "player"), "... back to the menu")

        # ---------------------------------------------------------------- songs are never released
        rc("play_ids", ids=[S], start_index=0)
        check(wait(lambda: cur() == S and state()["playing"]), "a song plays")
        tvkey(404)
        check(wait(lambda: state()["playing"] is False), "... paused")
        set_hidden(True)
        page.wait_for_timeout(REL_MS + 2000)
        check(n(rf": release #{S} ") == 0 and src() == f"/media/{S}", "a song is never released (no video decoder)")
        set_hidden(False)

        # ---------------------------------------------------------------- Play from the web remote while released (Home)
        rc("play_ids", ids=[V], start_index=0)
        check(wait(lambda: cur() == V and js("ltv.screen") == "player" and state()["playing"]), "the video again")
        tvkey(404)
        check(wait(lambda: state()["playing"] is False), "... paused")
        page.keyboard.press("Escape")
        check(wait(lambda: js("ltv.screen") == "home") and wait(lambda: released(), REL_MS / 1000 + 8),
              "on Home: released after the pause time")
        n_play = n(r": restore #\d+ at [\d.]+ \(play\)$")
        rc("play")
        check(wait(lambda: n(r": restore #\d+ at [\d.]+ \(play\)$") == n_play + 1), "web remote Play while released: 'restore … (play)'")
        check(wait(lambda: state()["playing"] is True and not js("ltv.engine.v.paused")), "... and it plays")

        check(not errors, f"no page errors ({errors[:2]})")
        check(not any(l.startswith("JSERR") for l in journal()), "no JSERR lines")
        page.close()
        b.close()
    tb = srvlog.read_text(errors="replace").count("Traceback")
    print(f"server tracebacks: {tb}")
    check(tb == 0, "no server tracebacks")
finally:
    srv.terminate()
    try:
        srv.wait(10)
    except subprocess.TimeoutExpired:
        srv.kill()
    if srvlog.exists():
        shutil.copy(srvlog, OUT / "server.log")
    shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASSED" if not fails else f"FAILED {len(fails)}: {fails}")
sys.exit(1 if fails else 0)
