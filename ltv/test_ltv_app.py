#!/usr/bin/env python3.13
"""LTVPlayer app checks (phase P3 "engine"): the page in desktop Chromium as the ltv player of a THROWAWAY server.

    SERVER_PY=<python with aiohttp mutagen pillow> PYTHONPATH=<dir with aiohttp> python3.13 ltv/test_ltv_app.py [outdir]

Builds a temp library (VP9/Opus WebM clips, a two-audio-track H.264 MKV, FLAC songs, one WavPack song that desktop
Chromium cannot play), starts server/server.py on its own port with its own data dir, opens /static/ltv/index.html with
Playwright and drives it from a second page acting as the web remote. Checks: joins as the player, start log line, Home
OK shuffles music videos, remote play / pause / seek / next / prev / jump, end of an item advances the queue, an
unplayable item is skipped, a load that never starts fails after 30 s, the default audio track is selected, player keys
send the right commands, Back / Back twice, hidden page pauses, a server restart does not stop the picture and the
session shows playing again, blocked autoplay waits for OK. Screenshots of Home and player go to outdir.
Desktop Chromium cannot play HEVC / most MKV: codec questions belong to the TV probe, not here.
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
sys.path.insert(0, str(REPO / "server"))
import test_atv_audio as H  # noqa: E402  (ffmpeg helpers only)

PORT = int(os.environ.get("LTV_TEST_PORT", "18121"))
BASE = f"http://127.0.0.1:{PORT}"
PY = os.environ.get("SERVER_PY", sys.executable)
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="ltv-shots-"))
OUT.mkdir(parents=True, exist_ok=True)
tmp = Path(tempfile.mkdtemp(prefix="ltv-app-"))
fails = []


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label, flush=True)
    if not cond:
        fails.append(label)


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def webm(path, seconds, freq, title):
    path.parent.mkdir(parents=True, exist_ok=True)
    ff("-f", "lavfi", "-i", f"testsrc=size=320x180:rate=25:duration={seconds}", "-f", "lavfi", "-i",
       f"sine=frequency={freq}:duration={seconds}", "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8",
       "-b:v", "200k", "-c:a", "libopus", "-metadata", f"title={title}", "-metadata", "artist=Clipper", str(path))


def song_with_cover(path, seconds, freq, title):
    path.parent.mkdir(parents=True, exist_ok=True)
    cover = tmp / f"{title}.jpg"
    ff("-f", "lavfi", "-i", "testsrc2=size=600x600:rate=1:duration=1", "-frames:v", "1", str(cover))
    ff("-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}", "-i", str(cover), "-map", "0:a", "-map", "1:v",
       "-c:a", "flac", "-c:v", "mjpeg", "-disposition:v", "attached_pic", "-metadata", f"title={title}",
       "-metadata", "artist=Tester", "-metadata", "album=Test Album", str(path))


lib = tmp / "lib"
webm(lib / "videos" / "Clipper - Clip A.webm", 20, 400, "Clip A")
webm(lib / "videos" / "Clipper - Clip B.webm", 20, 500, "Clip B")
webm(lib / "videos" / "Clipper - Clip C.webm", 7, 600, "Clip C")
H.clip(lib / "videos" / "Clipper - Two Tracks.mkv", 12, second_default=True)
song_with_cover(lib / "songs" / "01 Song One.flac", 12, 330, "Song One")
ff("-f", "lavfi", "-i", "sine=frequency=660:duration=8", "-c:a", "wavpack", "-metadata", "title=Song Wav",
   "-metadata", "artist=Tester", str(lib / "songs" / "02 Song Wav.wv"))
ff("-f", "lavfi", "-i", "sine=frequency=700:duration=10", "-c:a", "flac", "-metadata", "title=Song Hang",
   "-metadata", "artist=Tester", str(lib / "songs" / "03 Song Hang.flac"))

env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(tmp / "data"),
           ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT))
env.pop("ATV_BONJOUR", None)
srvlog = tmp / "server.log"
srv = None


def start_server():
    global srv
    srv = subprocess.Popen([PY, str(REPO / "server" / "server.py")], env=env, stdout=open(srvlog, "a"),
                           stderr=subprocess.STDOUT)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=5) as r:
        return json.loads(r.read())


def state():
    return get("/api/state?session=ltv")


def journal(pattern=""):
    return [l.split("player[ltv]: ", 1)[1] for l in srvlog.read_text(errors="replace").splitlines()
            if "player[ltv]: " in l and re.search(pattern, l)]


def server_up(n_items, secs=60):
    end = time.time() + secs
    while time.time() < end:
        try:
            if len(get("/api/library?all=1")["items"]) >= n_items:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


start_server()
try:
    check(server_up(7), "throwaway library scanned (7 items)")
    items = {it["title"]: it for it in get("/api/library?all=1")["items"]}
    A, B, C = items["Clip A"]["id"], items["Clip B"]["id"], items["Clip C"]["id"]
    M, S, W, HANG = items["Two Tracks"]["id"], items["Song One"]["id"], items["Song Wav"]["id"], items["Song Hang"]["id"]
    check(get(f"/api/item/{M}").get("audio_ord") == 1, "two-track clip: server's default audio track is ord 1")

    with sync_playwright() as p:
        b = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required",
                                    "--enable-blink-features=AudioVideoTracks"])
        page = b.new_page(viewport={"width": 1920, "height": 1080})
        page.add_init_script("window.close = () => { window.__closed = (window.__closed || 0) + 1; };")
        errors, sent, got = [], [], []
        page.on("pageerror", lambda e: errors.append(str(e)))

        def on_ws(ws):
            ws.on("framesent", lambda f: sent.append(f if isinstance(f, str) else ""))
            ws.on("framereceived", lambda f: got.append(f if isinstance(f, str) else ""))

        page.on("websocket", on_ws)

        def cmds(name=None):
            out = []
            for f in sent:
                try:
                    m = json.loads(f)
                except ValueError:
                    continue
                if m.get("type") == "cmd" and (name is None or m.get("cmd") == name):
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
                page.wait_for_timeout(250)
            try:
                return bool(cond())
            except Exception:
                return False

        def js(expr):
            return page.evaluate(expr)

        def vt():
            return js("document.getElementById('v').currentTime")

        def cur():
            st = state()
            return st["current"]["id"] if st.get("current") else None

        remote = b.new_page()
        remote.goto(BASE + "/api/state?session=ltv")

        def open_remote():
            remote.evaluate("""() => new Promise((ok) => {
                window.r = new WebSocket(location.origin.replace('http', 'ws') + '/ws?role=remote&session=ltv');
                window.r.onopen = ok; })""")

        open_remote()

        def rc(name, **args):
            remote.evaluate("(m) => window.r.send(JSON.stringify(m))", {"type": "cmd", "cmd": name, **args})

        # ---- boot: joins as the ltv player, start line, Home with the empty card
        page.goto(BASE + "/static/ltv/index.html?from=test")
        check(wait(lambda: state()["players"] == 1), "the page joins as the ltv player (players 1)")
        check(wait(lambda: any(l.startswith("ltv 0.3.0 ua=") and "from=test" in l for l in journal())),
              "start line 'ltv 0.3.0 ua=… from=test' in the journal")
        check(wait(lambda: js("ltv.screen") == "home" and page.inner_text(".h-conn") == "Connected"),
              "Home shown, pill says Connected")
        check(page.inner_text(".h-etitle") == "Nothing playing yet", "empty card: 'Nothing playing yet'")
        page.screenshot(path=str(OUT / "p3-home-empty.png"))

        # ---- Home OK with nothing loaded: shuffle music videos -> player screen, picture plays
        page.keyboard.press("Enter")
        check(wait(lambda: any(m.get("collection") == "videos" and m.get("shuffle") is True
                               for m in cmds("play_collection"))), "Home OK sends play_collection videos shuffle:true")
        check(wait(lambda: js("ltv.screen") == "player"), "load present=true shows the player")
        check(wait(lambda: state()["playing"] is True), "server state playing after the start")

        # ---- remote play_ids A, B, C: A plays, position grows
        rc("play_ids", ids=[A, B, C], start_index=0)
        check(wait(lambda: cur() == A and state()["playing"]), "remote play_ids: Clip A playing")
        p1 = state()["position"]
        page.wait_for_timeout(2500)
        p2 = state()["position"]
        check(p2 > p1 + 1, f"server position grows from the page's status ({p1} -> {p2})")
        check(js("document.getElementById('v').getAttribute('src')") == f"/media/{A}", "video src is /media/<id>")
        page.mouse.move(500, 500)
        page.wait_for_timeout(300)
        page.screenshot(path=str(OUT / "p3-player-video.png"))
        check(page.inner_text(".p-title") == "Clip A" and "Next: Clip B" in page.inner_text(".p-next"),
              "player overlay: title and 'Next: Clip B'")

        # ---- remote pause / play / seek
        rc("pause")
        check(wait(lambda: js("document.getElementById('v').paused") and state()["playing"] is False),
              "remote pause: element paused, state playing false")
        rc("play")
        check(wait(lambda: not js("document.getElementById('v').paused") and state()["playing"] is True),
              "remote play: playing again")
        rc("seek", position=12.0)
        check(wait(lambda: 12 <= vt() < 15), f"remote seek 12: element at {vt():.1f}")
        check(wait(lambda: any(re.search(rf"seek #{A} server .* -> 12(\.0)?: seeked \d+ms landed 12.*running after \d+ms", l)
                               for l in journal())), "seek line: target, seeked, landed, running")

        # ---- next / prev / jump
        rc("next")
        check(wait(lambda: cur() == B and js("document.getElementById('v').getAttribute('src')") == f"/media/{B}"),
              "remote next: Clip B loaded")
        check(wait(lambda: any(l.startswith(f"end #{A} replaced") for l in journal())), "end line for Clip A (replaced)")
        check(wait(lambda: state()["playing"]), "Clip B playing")
        rc("prev", position=1.0)
        check(wait(lambda: cur() == A), "remote prev under 5 s: previous item (Clip A)")
        rc("jump", index=2)
        check(wait(lambda: cur() == C and state()["playing"]), "remote jump 2: Clip C playing")

        # ---- the end of an item advances the queue (repeat all: back to A)
        check(wait(lambda: cur() == A, 15), "Clip C ended -> server loads the next item")
        check(any(l.startswith(f"end #{C} ended") for l in journal()), "end line for Clip C (ended)")

        # ---- an unplayable item is skipped with the capsule
        rc("play_ids", ids=[W, A], start_index=0)
        check(wait(lambda: any(l.startswith(f"error #{W} 'Song Wav': MEDIA_ERR") for l in journal())),
              "WavPack: error line with MEDIA_ERR")
        check(wait(lambda: "Cannot play Song Wav on this TV" in page.inner_text("#capsule")), "capsule 'Cannot play …'")
        check(wait(lambda: cur() == A and state()["playing"]), "after the error the server plays the next item")
        check(wait(lambda: not js("document.getElementById('capsule').classList.contains('on')"), 7),
              "the capsule goes away after 5 s")

        # ---- default audio track of a multi-track file
        rc("play_ids", ids=[M], start_index=0)
        check(wait(lambda: any(l.startswith(f"meta #{M}") and "audio track 1 selected" in l for l in journal())),
              "two-track MKV: audio track 1 selected (meta line)")
        check(wait(lambda: js("(() => { const t = document.getElementById('v').audioTracks; "
                              "return t && t.length === 2 && t[1].enabled && !t[0].enabled; })()")),
              "audioTracks: only ord 1 enabled")

        # ---- player keys -> commands
        rc("play_ids", ids=[A, B], start_index=0)
        check(wait(lambda: cur() == A and state()["playing"] and vt() > 1), "Clip A playing for the key test")
        n = len(sent)
        page.keyboard.press("Enter")
        check(wait(lambda: any(m["cmd"] == "pause" for m in cmds()[len(cmds()) - 3:])) and
              wait(lambda: state()["playing"] is False), "OK while playing: cmd pause, state paused")
        page.keyboard.press("Enter")
        check(wait(lambda: state()["playing"] is True), "OK while paused: cmd play, playing")
        t0 = vt()
        page.keyboard.press("ArrowRight")
        sk = cmds("seek")
        check(bool(sk) and abs(sk[-1]["position"] - (t0 + 10)) < 1.5, f"▶: cmd seek +10 ({sk[-1]['position'] if sk else None} vs {t0:.1f})")
        check(wait(lambda: vt() >= t0 + 8), "▶: the element jumps forward")
        page.keyboard.press("ArrowLeft")
        page.keyboard.press("ArrowLeft")
        sk = cmds("seek")
        check(len(sk) >= 3 and sk[-1]["position"] < sk[-2]["position"] < sk[-3]["position"],
              "◀ twice: targets add up before the server answers")
        page.keyboard.press("ArrowDown")
        check(wait(lambda: cmds("next") and cur() == B), "▼: cmd next -> Clip B")
        wait(lambda: js("ltv.engine.item && ltv.engine.item.id") == B and state()["playing"])   # the page holds B
        page.keyboard.press("ArrowUp")
        check(wait(lambda: cmds("prev") and cur() == A), "▲: cmd prev (under 5 s) -> Clip A")
        page.keyboard.press("PageUp")
        check(wait(lambda: len(cmds("next")) >= 2), "CH+ (PageUp): cmd next")
        page.keyboard.press("PageDown")
        check(wait(lambda: len(cmds("prev")) >= 2), "CH- (PageDown): cmd prev")
        check(wait(lambda: state()["playing"]), "playing before Back")
        page.keyboard.press("Escape")
        check(wait(lambda: js("ltv.screen") == "home"), "Back on the player: Home")
        page.wait_for_timeout(1200)
        check(not js("document.getElementById('v').paused") and state()["playing"], "leaving the player does not pause")
        page.screenshot(path=str(OUT / "p3-home-playing.png"))
        check(page.inner_text(".h-eyebrow") == "NOW PLAYING", "Home card: NOW PLAYING")
        page.keyboard.press("Escape")
        check(wait(lambda: "Press Back again to close" in page.inner_text("#capsule")), "Back on Home: 'Press Back again to close'")
        check(not js("window.__closed || 0"), "one Back does not close")
        page.keyboard.press("Escape")
        check(wait(lambda: js("window.__closed || 0") == 1), "second Back within 2 s closes (window.close)")
        page.keyboard.press("Enter")
        check(wait(lambda: js("ltv.screen") == "player"), "Home OK with an item: the player")

        # ---- song screen
        rc("play_ids", ids=[S], start_index=0)
        check(wait(lambda: cur() == S and state()["playing"]), "song plays through the same element")
        page.wait_for_timeout(1500)
        page.mouse.move(600, 600)
        page.wait_for_timeout(400)
        check(js("ltv.player.root.classList.contains('song')"), "song: cover screen")
        page.screenshot(path=str(OUT / "p3-player-song.png"))

        # ---- page hidden: pause
        rc("play_ids", ids=[A, B], start_index=0)
        check(wait(lambda: cur() == A and state()["playing"]), "Clip A playing before the hidden test")
        js("""() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
                      document.dispatchEvent(new Event('visibilitychange')); }""")
        check(wait(lambda: js("document.getElementById('v').paused") and state()["playing"] is False),
              "hidden page: element paused, server state paused")
        check(any(l == "hidden: paused" for l in journal()), "journal 'hidden: paused'")
        js("""() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
                      document.dispatchEvent(new Event('visibilitychange')); }""")
        page.keyboard.press("Enter")
        check(wait(lambda: state()["playing"] is True), "playing again after OK")

        # ---- server gone in mid-play (killed: aiohttp's graceful stop waits for open sockets), then back
        srv.kill()
        srv.wait(20)
        t1 = vt()
        page.wait_for_timeout(3000)
        check(vt() > t1 + 2 and not js("document.getElementById('v').paused"), f"server down: picture goes on ({t1:.1f} -> {vt():.1f})")
        check(js("document.getElementById('pill').classList.contains('on')"), "server down: 'Reconnecting…' pill")
        n_got = len(got)
        start_server()
        check(server_up(7), "server back")
        open_remote()   # the web remote's socket died with the server
        check(wait(lambda: state()["players"] == 1 and state()["playing"] is True, 20),
              "after the reconnect the session shows playing again")
        check(any(re.match(rf"load #{A} resync: kept, playing", l) for l in journal()), "resync line: kept, playing")
        check(wait(lambda: any('"type": "control"' in f and '"action": "play"' in f for f in got[n_got:])),
              "after the resync the app's cmd play comes back as control play (the server wants to play again)")
        check(any(l.startswith("socket back after") for l in journal()) and "socket down" in journal(),
              "journal: socket down / socket back after")
        check(not js("document.getElementById('pill').classList.contains('on')"), "pill gone after the reconnect")

        # ---- a load that never starts fails after 30 s, the next item plays
        hung = []
        page.route(f"**/media/{HANG}", lambda route: hung.append(route))   # not answered until the check is done
        rc("play_ids", ids=[HANG, A], start_index=0)
        check(wait(lambda: any(l.startswith(f"error #{HANG} 'Song Hang': no playback 30 s") for l in journal()), 40),
              "a load that never starts: error after 30 s")
        check(wait(lambda: cur() == A and state()["playing"], 10), "then the next item plays")
        for route in hung:
            route.abort()
        page.unroute_all(behavior="ignoreErrors")

        check(not errors, f"no page errors ({errors[:2]})")
        check(not any(l.startswith("JSERR") for l in journal()), "no JSERR lines")
        page.close()
        b.close()

        # ---- autoplay blocked: 'Press OK to play', OK starts it
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1920, "height": 1080})
        remote = b.new_page()
        remote.goto(BASE + "/api/state?session=ltv")

        def open_remote():
            remote.evaluate("""() => new Promise((ok) => {
                window.r = new WebSocket(location.origin.replace('http', 'ws') + '/ws?role=remote&session=ltv');
                window.r.onopen = ok; })""")

        open_remote()
        page.goto(BASE + "/static/ltv/index.html")
        check(wait(lambda: state()["players"] == 1), "second browser joins (the first one closed)")
        rc("play_ids", ids=[B], start_index=0)
        check(wait(lambda: any(l.startswith(f"play #{B}: blocked (NotAllowedError)") for l in journal())),
              "autoplay blocked: logged")
        check(wait(lambda: page.inner_text("#capsule") == "Press OK to play"), "capsule 'Press OK to play'")
        page.keyboard.press("Enter")
        check(wait(lambda: state()["playing"] is True and cur() == B), "OK starts it")
        page.close()
        b.close()
finally:
    if srv and srv.poll() is None:
        srv.terminate()
        try:
            srv.wait(10)
        except subprocess.TimeoutExpired:
            srv.kill()
            srv.wait(10)
    log = srvlog.read_text(errors="replace")
    print("server tracebacks:", log.count("Traceback"))
    shutil.copy(srvlog, OUT / "p3-check-server.log")
    if log.count("Traceback"):
        fails.append("server tracebacks")
print(f"FAILED {len(fails)}" if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
