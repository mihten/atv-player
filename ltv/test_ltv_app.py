#!/usr/bin/env python3.13
"""LTVPlayer app checks (P3 "engine" + P4 "navigation"): the page in desktop Chromium as the ltv player of a THROWAWAY server.

    SERVER_PY=<python with aiohttp mutagen pillow> PYTHONPATH=<dir with aiohttp> python3.13 ltv/test_ltv_app.py [outdir]

Builds a temp library (VP9/Opus WebM clips, a two-audio-track H.264 MKV, FLAC songs, one WavPack song that desktop
Chromium cannot play), starts server/server.py on its own port with its own data dir, opens /static/ltv/index.html with
Playwright and drives it from a second page acting as the web remote. Checks: joins as the player, start log line, Home
OK shuffles music videos, remote play / pause / seek / next / prev / jump, end of an item advances the queue, an
unplayable item is skipped, a load that never starts fails after 30 s, the default audio track is selected, player keys
send the right commands, Back / Back twice, hidden page pauses, a server restart does not stop the picture and the
session shows playing again, blocked autoplay waits for OK. Navigation (P4, seeded playlists / mix / loved items, long
titles with diacritics): Home sections and tiles, key sequences end on the expected tiles, Up / Down remember the tile
per section, no wrap, the focused tile stays in view (shelf / page transforms), lazy covers, Back from the player keeps
the focus, pointer focus + chevron + wheel, arrow keys leave pointer mode, OK on each kind of tile sends the exact
command, font + accent colour, the palette port against a Python port of Shared/Artwork.swift, FPS log lines.
List screens (P5): Playlists (For you + playlist card grids), a playlist (Play, entries by position, a deleted file dimmed and
skipped, start_index), the open playlist reloads on a change / shows "deleted", an auto-list (Play / start_id), Music
videos (Play + 4-column poster grid, the page follows the focus), Folders (roots, "including subfolders" / "only this
folder", files with start_id), Queue (opens centred on the playing entry, virtualised rows, jump + log line, the focus
stays on track changes, pointer / wheel), Back from the player returns to the list with its focus, Back to Home.
Screenshots of Home, lists and player go to outdir.
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



# ---------------------------------------------------------------- Python port of Palette.extract (Shared/Artwork.swift)
def _hsb(c):
    mx, mn = max(c["r"], c["g"], c["b"]), min(c["r"], c["g"], c["b"])
    d, h = mx - mn, 0.0
    if d > 0:
        if mx == c["r"]:
            h = ((c["g"] - c["b"]) / d) % 6 if (c["g"] - c["b"]) >= 0 else -((-(c["g"] - c["b"]) / d) % 6)
        elif mx == c["g"]:
            h = (c["b"] - c["r"]) / d + 2
        else:
            h = (c["r"] - c["g"]) / d + 4
        h /= 6
        if h < 0:
            h += 1
    return h, (0 if mx == 0 else d / mx), mx


def _rgb(h, s, v):
    import math
    h6 = (h - math.floor(h)) * 6
    i, f = int(h6) % 6, h6 - math.floor(h6)
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    r, g, b = [(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)][i]
    return {"r": r, "g": g, "b": b}


def _bright(c, v, s=None):
    h, s0, _ = _hsb(c)
    return _rgb(h, s0 if s is None else s, v)


def py_extract(px):
    bins = [[0.0, 0.0, 0.0, 0.0] for _ in range(12)]
    dark, total = [0.0, 0.0, 0.0, 0.0], 0.0
    for i in range(0, len(px), 4):
        c = {"r": px[i] / 255, "g": px[i + 1] / 255, "b": px[i + 2] / 255}
        h, s, v = _hsb(c)
        if v < 0.5:
            w = 0.2 + s
            dark[0] += c["r"] * w; dark[1] += c["g"] * w; dark[2] += c["b"] * w; dark[3] += w
        if not (s > 0.18 and v > 0.14):
            continue
        w = s * v * v
        k = min(11, int(h * 12))
        bins[k][0] += c["r"] * w; bins[k][1] += c["g"] * w; bins[k][2] += c["b"] * w; bins[k][3] += w
        total += w
    ranked = [{"r": b[0] / b[3], "g": b[1] / b[3], "b": b[2] / b[3]}
              for b in sorted([b for b in bins if b[3] > 0 and b[3] > total * 0.04], key=lambda b: -b[3])]
    shadow = {"r": dark[0] / dark[3], "g": dark[1] / dark[3], "b": dark[2] / dark[3]} if dark[3] > 0 else None
    if not ranked:
        h = _hsb(shadow)[0] if shadow else 0.6
        return {"base": _rgb(h, 0.15, 0.08), "tones": [_rgb(h, 0.12, 0.32), _rgb(h, 0.1, 0.22), _rgb(h + 0.5, 0.1, 0.18),
                _rgb(h, 0.05, 0.4)], "vibrant": _rgb(0.58, 0.25, 0.95)}
    top = ranked[0]
    th, ts, tv = _hsb(top)
    if shadow:
        sh, ss, _ = _hsb(shadow)
        depth = _rgb(sh, min(0.8, max(0.35, ss * 1.4)), 0.3)
    else:
        depth = _rgb(th, ts * 0.8, 0.28)
    lift = lambda c: _bright(c, min(0.62, max(0.38, _hsb(c)[2] * 0.7)))  # noqa: E731
    t0 = lift(top)
    t1 = lift(ranked[1]) if len(ranked) > 1 else _rgb(th, ts, 0.3)
    t3 = lift(ranked[2]) if len(ranked) > 2 else {k: (t0[k] + depth[k]) / 2 for k in "rgb"}
    dh, ds, _ = _hsb(depth)
    return {"base": _rgb(dh, min(0.7, ds), 0.09), "tones": [t0, t1, depth, t3],
            "vibrant": _rgb(th, max(ts, 0.55), 1)}


def pal_close(a, b, tol=1e-6):
    if isinstance(a, dict) and "r" in a:
        return all(abs(a[k] - b[k]) <= tol for k in "rgb")
    return pal_close(a["base"], b["base"]) and pal_close(a["vibrant"], b["vibrant"]) and \
        all(pal_close(x, y) for x, y in zip(a["tones"], b["tones"]))


def synthetic():
    def fill(*colours):   # equal stripes of the given RGB colours over 32 x 32 pixels
        out = []
        for i in range(1024):
            out += list(colours[i * len(colours) // 1024]) + [255]
        return out
    return {"red": fill((255, 0, 0)), "two colours": fill((20, 90, 200), (230, 120, 30)),
            "greyscale": fill((10, 10, 10), (128, 128, 128), (240, 240, 240)),
            "three hues + shadow": fill((200, 30, 60), (40, 160, 70), (60, 40, 180), (15, 10, 30))}


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
# P5: a song that is deleted later (a missing playlist entry) and one in a subfolder (a folder with files + subfolders)
GONE_PATH = lib / "songs" / "04 Song Gone.flac"
ff("-f", "lavfi", "-i", "sine=frequency=740:duration=6", "-c:a", "flac", "-metadata", "title=Song Gone",
   "-metadata", "artist=Tester", str(GONE_PATH))
(lib / "songs" / "bonus").mkdir(parents=True, exist_ok=True)
ff("-f", "lavfi", "-i", "sine=frequency=780:duration=6", "-c:a", "flac", "-metadata", "title=Song Bonus",
   "-metadata", "artist=Tester", str(lib / "songs" / "bonus" / "05 Song Bonus.flac"))
# P4: enough music videos for a scrolling shelf; three carry titles as long as the longest in a real library (76-80
# characters) and a 60-character artist, with č ć š ž đ (the font must have them)
LONG = ["Ljubičasta šuma u ožujku (Đurđevdanski koncert uživo, proširena verzija s gostima)",
        "Čežnja za žutim šljivama i đurđicama (akustična verzija snimljena kod kuće, 2026)",
        "Šapat noći nad Dravom - posljednja pjesma večeri (cijeli nastup bez prekida, HD)"]
LONG_ARTIST = "Đurđa Šimunović-Čačić & Orkestar Žarko Ćosić i Prijatelji"
for n in range(16):   # 20 music videos in all: the 4-column grid of P5 is taller than one screen
    title = LONG[n] if n < 3 else f"Extra {n}"
    path = lib / "more" / f"extra{n}.webm"
    path.parent.mkdir(parents=True, exist_ok=True)
    ff("-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=2", "-f", "lavfi", "-i", "sine=frequency=500:duration=2",
       "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "50k", "-c:a", "libopus",
       "-metadata", f"title={title}", "-metadata", f"artist={LONG_ARTIST if n < 3 else 'Clipper'}", str(path))
N_ITEMS = 25

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
    check(server_up(N_ITEMS), f"throwaway library scanned ({N_ITEMS} items)")
    items = {it["title"]: it for it in get("/api/library?all=1")["items"]}
    A, B, C = items["Clip A"]["id"], items["Clip B"]["id"], items["Clip C"]["id"]
    M, S, W, HANG = items["Two Tracks"]["id"], items["Song One"]["id"], items["Song Wav"]["id"], items["Song Hang"]["id"]
    GONE, BONUS = items["Song Gone"]["id"], items["Song Bonus"]["id"]
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

        def js(expr, arg=None):
            return page.evaluate(expr) if arg is None else page.evaluate(expr, arg)

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
        check(wait(lambda: any(re.match(r"ltv \d+\.\d+\.\d+ ua=", l) and "from=test" in l for l in journal())),
              "start line 'ltv <version> ua=… from=test' in the journal")
        check(wait(lambda: js("ltv.screen") == "home" and page.inner_text(".h-conn") == "Connected"),
              "Home shown, pill says Connected")
        check(page.inner_text(".hero-etitle") == "Nothing playing yet", "empty card: 'Nothing playing yet'")
        page.screenshot(path=str(OUT / "p4-home-empty.png"))

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
        wait(lambda: cmds("seek"), 3)
        sk = cmds("seek")
        check(bool(sk) and abs(sk[-1]["position"] - (t0 + 10)) < 1.5, f"▶: cmd seek +10 ({sk[-1]['position'] if sk else None} vs {t0:.1f})")
        check(wait(lambda: vt() >= t0 + 8), "▶: the element jumps forward")
        page.keyboard.press("ArrowLeft")
        page.keyboard.press("ArrowLeft")
        wait(lambda: len(cmds("seek")) >= 3, 3)
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
        page.screenshot(path=str(OUT / "p4-home-playing.png"))
        check(page.inner_text(".hero-state") == "NOW PLAYING", "Home card: NOW PLAYING")
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
        check(server_up(N_ITEMS), "server back")
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

        # ======================================================== P4: Home navigation on a seeded library
        def send_json(path, body, method="POST"):
            req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method=method,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read() or b"{}")

        lib_items = get("/api/library?all=1")["items"]
        vids = [i["id"] for i in get("/api/collection/videos")["items"]]
        long_ids = [i["id"] for i in lib_items if i["title"] in LONG]
        p_road = send_json("/api/playlists", {"name": "Road Trip", "ids": vids[:6]})["id"]
        p_long = send_json("/api/playlists", {"name": LONG[0], "ids": vids[2:5] + [S]})["id"]
        send_json("/api/mixes/1", {"name": "Evening", "sources": [{"type": "playlist", "id": p_road}]}, "PUT")
        check([m.get("playlists") for m in state()["mixes"]][:1] == [1], f"seed: mix 1 holds a playlist ({state()['mixes']})")

        b = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = b.new_page(viewport={"width": 1920, "height": 1080})
        errors, sent = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("websocket", lambda ws: ws.on("framesent", lambda f: sent.append(f if isinstance(f, str) else "")))
        remote = b.new_page()
        remote.goto(BASE + "/api/state?session=ltv")
        open_remote()
        for i in vids[:5]:
            rc("love", id=i, on=True)
        page.goto(BASE + "/static/ltv/index.html")
        check(wait(lambda: state()["players"] == 1), "P4: the page joins")

        def focused():
            return js("document.querySelector('#home .focused')?.dataset.focus || null")

        def keys(*names):
            for n in names:
                page.keyboard.press(n)
                page.wait_for_timeout(120)

        def rect(sel):
            return js(f"(() => {{ const r = document.querySelector({json.dumps(sel)}).getBoundingClientRect(); "
                      "return {l: r.left, t: r.top, r: r.right, b: r.bottom, w: r.width, h: r.height}; })()")

        def ty():
            return js("ltv.home.focus.ty")

        def tx(section):
            return js(f"ltv.home.focus.tx.get(document.querySelector('[data-section=\"{section}\"] [data-track]')) || 0")

        want = ["hero", "browse", "foryou", "playlists", "play", "videos"]
        check(wait(lambda: js("[...document.querySelectorAll('#home [data-section]')].map(s => s.dataset.section)") == want, 15),
              f"Home sections in order {want}")
        check(js("[...document.querySelectorAll('[data-section=browse] [data-focus]')].map(e => e.dataset.focus)") ==
              ["browse:playlists", "browse:folders", "browse:videos", "browse:queue", "browse:settings"],
              "Browse: Playlists, Folders, Music videos, Queue, Settings")
        check(wait(lambda: "♥ Loved" in page.inner_text("[data-focus='auto:loved']")) and "5 items" in page.inner_text("[data-focus='auto:loved']"),
              "For you: '♥ Loved' card with '5 items'")
        road = page.inner_text(f"[data-focus='playlist:{p_road}']")
        check("Road Trip" in road and re.search(r"6 items · 6 videos · \d+:\d\d", road),
              f"Playlists: 'Road Trip' with '6 items · 6 videos · m:ss' ({road!r})")
        check(js(f"document.querySelectorAll('[data-section=playlists] .tile').length") == 2, "Playlists: 2 cards")
        mix = page.inner_text("[data-focus='mix:1']")
        check("Evening" in mix and "1 playlist" in mix, f"Play: mix tile 'Evening' / '1 playlist' ({mix!r})")
        check(js("document.querySelector('[data-focus=\"mix:2\"]')") is None, "Play: the empty mix 2 has no tile")
        check(abs(rect("[data-focus='shuffle-videos']")["w"] - 1300) < 2 and abs(rect("[data-focus='mix:1']")["w"] - 420) < 2,
              "Play: Mix 1 420 px, 'Shuffle music videos' fills the rest (1300 px)")
        check(js("document.querySelectorAll('[data-section=videos] .tile.poster').length") == len(vids),
              f"Music videos: {len(vids)} posters")
        last_poster = f"[data-focus='video:{vids[-1]}'] img"
        check(js(f"document.querySelector({json.dumps(last_poster)})?.hasAttribute('data-src')") is True,
              "lazy covers: the last poster (off screen) is not loaded yet")
        check(js("document.fonts.check('800 58px Nunito', 'čćšžđ ČĆŠŽĐ')") and
              wait(lambda: js("[...document.fonts].some(f => f.family.includes('Nunito') && f.status === 'loaded')")),
              "font: Nunito loaded, covers č ć š ž đ")
        page.screenshot(path=str(OUT / "p4-home-top.png"))

        # ---- keys: focus path, no wrap, Up / Down remember per section
        check(focused() == "hero", "focus starts on the hero")
        keys("ArrowDown")
        check(focused() == "browse:videos", f"▼ from the hero: the Browse tile nearest its centre ({focused()})")
        keys("ArrowRight", "ArrowRight", "ArrowRight")
        check(focused() == "browse:settings", "▶ x3: Browse / Settings (from Music videos, the tile under the hero)")
        keys("ArrowRight")
        check(focused() == "browse:settings", "▶ at the end: no wrap")
        keys("ArrowDown")
        check((focused() or "").startswith("auto:"), f"▼: a For you card ({focused()})")
        keys("ArrowUp")
        check(focused() == "browse:settings", "▲: back on the remembered Browse tile")
        keys("ArrowLeft", "ArrowLeft", "ArrowLeft", "ArrowLeft")
        check(focused() == "browse:playlists", "◀ x4: first tile, no wrap")
        keys("ArrowDown", "ArrowDown")
        check(focused() == f"playlist:{p_road}", f"▼ x2: first playlist card ({focused()})")
        keys("ArrowDown")
        check(focused() in ("mix:1", "shuffle-videos"), f"▼: a Play tile ({focused()})")
        keys("ArrowDown")
        check((focused() or "").startswith("video:"), f"▼: a music video poster ({focused()})")
        page.wait_for_timeout(500)
        check(ty() < 0, f"page moved up to show the poster shelf (translateY {ty()})")
        r = rect("#home .focused")
        check(r["t"] >= 0 and r["b"] <= 1080, f"focused poster fully on screen ({r})")
        keys(*["ArrowRight"] * (len(vids) + 2))
        page.wait_for_timeout(600)
        check(focused() == f"video:{vids[-1]}", "▶ to the end of the poster shelf: last poster")
        check(tx("videos") < 0, f"poster shelf moved left (translateX {tx('videos')})")
        r = rect("#home .focused")
        check(r["r"] <= 1920 and r["l"] >= 0, f"last poster on screen ({r})")
        check(js(f"document.querySelector({json.dumps(last_poster)})?.hasAttribute('data-src')") is False,
              "lazy covers: the last poster loaded once near the view")
        page.screenshot(path=str(OUT / "p4-home-videos.png"))
        check(wait(lambda: any(l.startswith("fps shelf videos:") for l in journal())), "FPS line for a shelf move in the journal")
        keys("ArrowUp", "ArrowUp", "ArrowUp", "ArrowUp", "ArrowUp")
        page.wait_for_timeout(500)
        check(focused() == "hero" and ty() == 0, "▲ x5: back on the hero, page at the top")

        # ---- OK on each kind of tile
        def last_cmd():
            c = cmds("play_collection")
            return c[-1] if c else None

        keys("ArrowDown")
        b_tile = focused()
        keys("Enter")
        check(wait(lambda: js("ltv.screen") == "lists"), f"OK on a Browse tile ({b_tile}): its list screen")
        keys("Escape")
        check(wait(lambda: js("ltv.screen") == "home") and focused() == b_tile, "Back from the list: Home with the focus on that tile")
        keys("ArrowDown", "Enter")
        check(wait(lambda: js("ltv.screen") == "lists") and wait(lambda: any(l.startswith("list: open auto ") for l in journal())),
              "OK on a For you card: its auto-list screen (log line)")
        keys("Escape")
        keys("ArrowDown", "Enter")
        check(wait(lambda: js("ltv.screen") == "lists") and wait(lambda: any(l.startswith("list: open playlist ") for l in journal())),
              "OK on a playlist card: the playlist screen (log line)")
        keys("Escape")
        check(wait(lambda: js("ltv.screen") == "home") and (focused() or "").startswith("playlist:"), "Back: Home on the playlist card")
        keys("ArrowDown", "ArrowLeft", "ArrowLeft")
        check(focused() == "mix:1", "Play row: ◀ to Mix 1")
        n0 = len(cmds("play_collection"))
        keys("Enter")
        check(wait(lambda: len(cmds("play_collection")) > n0) and
              {k: v for k, v in last_cmd().items() if k != "type"} == {"cmd": "play_collection", "collection": "mix", "mix": 1, "shuffle": True},
              f"OK on Mix 1: play_collection mix 1 shuffled ({last_cmd()})")
        check(wait(lambda: js("ltv.screen") == "player"), "a start opens the player")
        keys("Escape")
        check(wait(lambda: js("ltv.screen") == "home") and focused() == "mix:1", "Back: Home with the focus on Mix 1")
        keys("ArrowRight")
        n0 = len(cmds("play_collection"))
        keys("Enter")
        check(wait(lambda: len(cmds("play_collection")) > n0) and
              {k: v for k, v in last_cmd().items() if k != "type"} == {"cmd": "play_collection", "collection": "videos", "shuffle": True},
              f"OK on Shuffle music videos: play_collection videos shuffled ({last_cmd()})")
        keys("Escape")
        keys("ArrowDown", "ArrowRight", "ArrowRight")
        pick = focused()
        n0 = len(cmds("play_collection"))
        keys("Enter")
        check(wait(lambda: len(cmds("play_collection")) > n0) and
              {k: v for k, v in last_cmd().items() if k != "type"} ==
              {"cmd": "play_collection", "collection": "videos", "start_id": int(pick.split(":")[1]), "shuffle": True},
              f"OK on a poster: play_collection videos start_id {pick} shuffled ({last_cmd()})")
        check(wait(lambda: js("ltv.screen") == "player") and wait(lambda: cur() == int(pick.split(":")[1])),
              "the picked video plays first")
        keys("Escape")
        check(wait(lambda: js("ltv.screen") == "home") and focused() == pick, "Back from the player keeps the poster focused")

        # ---- pointer: hover focuses, chevron, wheel; an arrow key leaves pointer mode
        keys(*["ArrowLeft"] * (len(vids) + 1))
        page.wait_for_timeout(500)
        check(focused() == f"video:{vids[0]}" and tx("videos") == 0, "◀ to the first poster: shelf back at the start")
        box = rect("#home .focused")
        page.mouse.move(box["l"] + box["w"] / 2, box["t"] + box["h"] / 2)
        page.wait_for_timeout(300)
        hovered = focused()
        check(js("document.body.classList.contains('pointer')") and (hovered or "").startswith("video:"),
              f"pointer: mode on, the poster under it is focused ({hovered})")
        x0 = tx("videos")
        chev = rect("[data-section=videos] .chev-r")
        check(chev["w"] > 0, "pointer: right chevron shown on a shelf that can scroll")
        page.mouse.click(chev["l"] + chev["w"] / 2, chev["t"] + chev["h"] / 2)
        page.wait_for_timeout(500)
        check(tx("videos") < x0, f"chevron click moved the shelf ({x0} -> {tx('videos')})")
        x1 = tx("videos")
        vis = js("""(() => { const p = [...document.querySelectorAll('[data-section=videos] .tile.poster')].find(e => {
            const r = e.getBoundingClientRect(); return r.left >= 100 && r.right <= 1800; });
            const r = p.getBoundingClientRect(); return {x: r.left + r.width / 2, y: r.top + r.height / 2, id: p.dataset.focus}; })()""")
        page.mouse.move(vis["x"], vis["y"])
        page.wait_for_timeout(300)
        check(focused() == vis["id"], f"pointer on a visible poster focuses it ({vis['id']})")
        page.mouse.wheel(0, -120)
        page.wait_for_timeout(500)
        check(tx("videos") > x1, f"wheel over the shelf moved it back ({x1} -> {tx('videos')})")
        under = js("document.elementFromPoint(%d, %d)?.closest('[data-focus]')?.dataset.focus || null" % (vis["x"], vis["y"]))
        check(under is not None and under != vis["id"] and focused() == under,
              f"after the shelf moved under the pointer, the tile now under it is focused ({focused()} / {under})")
        page.screenshot(path=str(OUT / "p4-home-pointer.png"))
        keys("ArrowLeft")
        check(not js("document.body.classList.contains('pointer')"), "an arrow key leaves pointer mode")

        # ---- accent from the cover, palette port, long titles
        rc("play_ids", ids=[S], start_index=0)
        check(wait(lambda: cur() == S), "song with a cover loaded")
        page.wait_for_timeout(2000)
        acc = js("getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()")
        want_acc = js(f"import('/static/ltv/palette.js').then(m => m.paletteFor({{id: {S}, art: true}}).then(p => m.css(m.uiAccent(p.vibrant))))")
        check(acc == want_acc and acc != "rgb(167, 139, 250)", f"accent = the cover's calmed vibrant colour ({acc} vs {want_acc})")
        # UI accent = option C: same hue, saturation clamped to 0.30-0.42, brightness 0.92 (values worked out by hand)
        ua = js("""import('/static/ltv/palette.js').then(m => [m.css(m.uiAccent({r: 0.04, g: 0.02, b: 1})),
                   m.css(m.uiAccent({r: 1, g: 0, b: 0})), m.css(m.uiAccent({r: 0.75, g: 0.8, b: 0.95}))])""")
        check(ua == ["rgb(138, 136, 235)", "rgb(235, 136, 136)", "rgb(164, 182, 235)"],
              f"UI accent: vivid blue / pure red capped at s 0.42 v 0.92, pale blue lifted to s 0.30 ({ua})")
        res = js(f"""import('/static/ltv/palette.js').then(async (m) => {{
            const img = new Image(); img.src = '/art/{S}'; await img.decode();
            const c = document.createElement('canvas'); c.width = c.height = 32;
            const x = c.getContext('2d'); x.imageSmoothingQuality = 'medium'; x.drawImage(img, 0, 0, 32, 32);
            const px = x.getImageData(0, 0, 32, 32).data;
            return {{px: Array.from(px), pal: m.extractPixels(px)}}; }})""")
        check(pal_close(res["pal"], py_extract(res["px"])), "palette of a real cover = Python port of Artwork.swift")
        for name, px in synthetic().items():
            got = js("(a) => import('/static/ltv/palette.js').then(m => m.extractPixels(Uint8ClampedArray.from(a)))", px)
            check(pal_close(got, py_extract(px)), f"palette port, synthetic '{name}' = Python port")
        check(pal_close(js("(a) => import('/static/ltv/palette.js').then(m => m.extractPixels(Uint8ClampedArray.from(a)))",
                           synthetic()["red"])["vibrant"], {"r": 1, "g": 0, "b": 0}) and
              pal_close(js("(a) => import('/static/ltv/palette.js').then(m => m.extractPixels(Uint8ClampedArray.from(a)))",
                           synthetic()["red"])["tones"][0], {"r": 0.62, "g": 0, "b": 0}),
              "palette: pure red -> vibrant (1,0,0), tone0 (0.62,0,0) (worked out by hand from the Swift rules)")
        rc("play_ids", ids=[long_ids[0]], start_index=0)
        check(wait(lambda: cur() == long_ids[0]), "long-title video loaded")
        keys("Escape", "ArrowUp", "ArrowUp", "ArrowUp", "ArrowUp", "ArrowUp")
        check(wait(lambda: focused() == "hero" and ty() == 0), "back at the hero for the long-title look")
        t = rect(".hero-title")
        check(t["h"] <= 2 * 58 * 1.12 + 6, f"hero title at most 2 lines ({t['h']:.0f} px)")
        if js("document.querySelector('.hero-next')"):
            check(rect(".hero-text")["r"] <= rect(".hero-next")["l"] + 1, "hero text does not run into UP NEXT")
        bad = js("""[...document.querySelectorAll('#home .tile')].filter(t => {
            const r = t.getBoundingClientRect();
            return [...t.querySelectorAll('.cap, .cap-t, .cap-s, span')].some(c => {
              const q = c.getBoundingClientRect(); return q.width && (q.left < r.left - 1 || q.right > r.right + 1 || q.bottom > r.bottom + 1); });
          }).map(t => t.dataset.focus)""")
        check(not bad, f"no caption runs out of its tile ({bad})")
        page.screenshot(path=str(OUT / "p4-home-longtitle.png"))
        check(not errors, f"P4: no page errors ({errors[:2]})")
        check(not any(l.startswith("JSERR") for l in journal()), "P4: no JSERR lines")
        page.close()
        b.close()

        # ======================================================== P5: list screens + Queue
        b = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = b.new_page(viewport={"width": 1920, "height": 1080})
        errors, sent = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("websocket", lambda ws: ws.on("framesent", lambda f: sent.append(f if isinstance(f, str) else "")))
        remote = b.new_page()
        remote.goto(BASE + "/api/state?session=ltv")
        open_remote()
        p_gaps = send_json("/api/playlists", {"name": "Gaps", "ids": [S, GONE, A]})["id"]
        GONE_PATH.unlink()
        rc("rescan")
        check(wait(lambda: GONE not in [i["id"] for i in get("/api/library?all=1")["items"]] and not state()["scanning"], 40),
              "seed: 'Song Gone' deleted + rescanned (a missing playlist entry)")
        page.goto(BASE + "/static/ltv/index.html")
        check(wait(lambda: state()["players"] == 1), "P5: the page joins")
        check(wait(lambda: js("[...document.querySelectorAll('#home [data-section]')].map(s => s.dataset.section)").count("playlists") == 1, 15),
              "P5: Home loaded")

        def lf():
            return js("document.querySelector('#lists .l-screen:not(.hidden) .focused')?.dataset.focus || null")

        def ltext(sel):
            return js("(s) => document.querySelector('#lists .l-screen:not(.hidden) ' + s)?.textContent || null", sel)

        def lall(sel):
            return js("(s) => [...document.querySelectorAll('#lists .l-screen:not(.hidden) ' + s)].map(e => e.textContent.trim())", sel)

        def strip(m):
            return {k: v for k, v in (m or {}).items() if k != "type"}

        def count_items(n):
            return "1 item" if n == 1 else f"{n} items"

        def new_cmd(n0, name="play_collection"):
            return strip(cmds(name)[-1]) if wait(lambda: len(cmds(name)) > n0) else None

        def home_tile(focus_id):   # from anywhere on Home: hero, ▼ to Browse, ◀ to the start, then ▶ to the tile
            keys("ArrowUp", "ArrowUp", "ArrowUp", "ArrowUp", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowLeft", "ArrowLeft", "ArrowLeft")
            for _ in range(4):
                if focused() == focus_id:
                    break
                keys("ArrowRight")
            return focused() == focus_id

        def leave_player():   # a start opens the player when the server's load arrives: wait for it, then Back
            wait(lambda: js("ltv.screen") == "player")
            keys("Escape")
            wait(lambda: js("ltv.screen") != "player")

        def to_menu():   # Back until Home
            for _ in range(6):
                if js("ltv.screen") == "home":
                    return
                keys("Escape")
                page.wait_for_timeout(150)

        # ---- Playlists: For you + playlists as card grids
        check(home_tile("browse:playlists"), "Home: Browse / Playlists focused")
        keys("Enter")
        check(wait(lambda: js("ltv.screen") == "lists" and ltext(".l-title") == "Playlists"), "Playlists screen: title")
        check(wait(lambda: ltext(".l-count") == "3 playlists"), f"Playlists: count line '3 playlists' ({ltext('.l-count')})")
        check(js("[...document.querySelectorAll('#lists .l-screen:not(.hidden) [data-section]')].map(s => s.dataset.section)") == ["foryou", "playlists"],
              "Playlists: sections For you, Playlists")
        check(len(lall("[data-section=playlists] .tile.card")) == 3 and "♥ Loved" in (ltext("[data-focus='auto:loved']") or ""),
              "Playlists: 3 playlist cards, '♥ Loved' card")
        check(lf() == "auto:loved", f"Playlists: focus starts on the first card ({lf()})")
        page.wait_for_timeout(400)
        page.screenshot(path=str(OUT / "p5-playlists.png"))
        keys("ArrowDown")
        check((lf() or "").startswith("playlist:"), f"▼: the playlist grid ({lf()})")
        for _ in range(3):
            if lf() == f"playlist:{p_road}":
                break
            keys("ArrowRight")
        check(lf() == f"playlist:{p_road}", "▶: the Road Trip card")
        keys("Enter")

        # ---- one playlist: Play, rows by position; Back from the player returns here with the focus
        check(wait(lambda: ltext(".l-title") == "Road Trip"), "playlist screen: title 'Road Trip'")
        check(wait(lambda: re.fullmatch(r"6 items · 6 videos · \d+:\d\d", ltext(".l-count") or "")), f"playlist: count line ({ltext('.l-count')})")
        check(wait(lambda: lf() == "play") and "6 items" in (ltext(".row.play") or ""), "playlist: focus on Play, '6 items'")
        check(lall(".l-header") == ["Songs and videos"] and len(lall(".l-rows .row")) == 7, "playlist: header + Play + 6 rows")
        check(wait(lambda: any(l.startswith(f"playlist {p_road} shown: 6 entries") for l in journal())), "journal: 'playlist <id> shown: 6 entries'")
        n0 = len(cmds("play_collection"))
        keys("Enter")
        check(new_cmd(n0) == {"cmd": "play_collection", "collection": "playlist", "playlist": p_road, "shuffle": True},
              f"OK on Play: play_collection playlist shuffled ({strip(last_cmd())})")
        check(wait(lambda: js("ltv.screen") == "player"), "the start opens the player")
        keys("Escape")
        check(wait(lambda: js("ltv.screen") == "lists") and lf() == "play", "Back from the player: the playlist screen, focus kept")
        keys("ArrowDown", "ArrowDown", "ArrowDown")
        check(lf() == "p:2", f"▼ x3: the third entry ({lf()})")
        n0 = len(cmds("play_collection"))
        keys("Enter")
        check(new_cmd(n0) == {"cmd": "play_collection", "collection": "playlist", "playlist": p_road, "start_index": 2, "shuffle": True},
              f"OK on an entry: start_index = its position ({strip(last_cmd())})")
        check(wait(lambda: cur() == vids[2]), "the picked entry plays first")
        leave_player()
        check(wait(lambda: js("ltv.screen") == "lists") and lf() == "p:2", "Back: entry still focused")
        check(wait(lambda: js("!!document.querySelector('#lists .l-screen:not(.hidden) [data-focus=\"p:2\"] .r-now')")),
              "the playing entry shows the bars on its thumb")
        page.screenshot(path=str(OUT / "p5-playlist.png"))
        send_json(f"/api/playlists/{p_road}", {"name": "Road Trip 2"}, "PUT")
        check(wait(lambda: ltext(".l-title") == "Road Trip 2") and lf() == "p:2", "playlists message: the open playlist reloads, focus kept")
        keys("Escape")
        check(wait(lambda: ltext(".l-title") == "Playlists") and lf() == f"playlist:{p_road}", "Back: Playlists with the card focused")

        # ---- a playlist with a deleted file: dimmed, not focusable, Play counts the playable ones
        for _ in range(3):
            if lf() == f"playlist:{p_gaps}":
                break
            keys("ArrowLeft")
        keys("Enter")
        check(wait(lambda: ltext(".l-title") == "Gaps"), "playlist 'Gaps' opened")
        check(wait(lambda: "1 missing" in (ltext(".l-count") or "")) and "2 items" in (ltext(".row.play") or ""),
              f"Gaps: '1 missing' in the count line, Play '2 items' ({ltext('.l-count')})")
        check(js("(() => { const r = document.querySelector('#lists .l-screen:not(.hidden) .row.dim'); "
                 "return !!r && !r.hasAttribute('data-focus') && r.textContent.includes('file was deleted or moved'); })()"),
              "missing entry: dimmed, not focusable, 'file was deleted or moved'")
        keys("ArrowDown", "ArrowDown")
        check(lf() == "p:2", f"▼ x2: skips the missing entry ({lf()})")
        n0 = len(cmds("play_collection"))
        keys("Enter")
        check(new_cmd(n0) == {"cmd": "play_collection", "collection": "playlist", "playlist": p_gaps, "start_index": 2, "shuffle": True},
              f"OK past a missing entry: start_index 2 ({strip(last_cmd())})")
        check(wait(lambda: cur() == A), "the server maps the position past the missing entry (Clip A plays)")
        leave_player()
        send_json(f"/api/playlists/{p_gaps}", {}, "DELETE")
        check(wait(lambda: ltext(".l-text") == "This playlist was deleted."), "deleted while open: 'This playlist was deleted.'")
        to_menu()
        check(js("ltv.screen") == "home" and focused() == "browse:playlists", "Back x2: Home, Browse / Playlists focused")

        # ---- auto-list (For you card on Home)
        keys("ArrowDown")
        check(focused() == "auto:loved" or wait(lambda: (focused() or "").startswith("auto:")), f"▼: a For you card ({focused()})")
        while focused() != "auto:loved" and js("!!document.querySelector('[data-focus=\"auto:loved\"]')"):
            before = focused()
            keys("ArrowLeft")
            if focused() == before:
                break
        keys("Enter")
        check(wait(lambda: ltext(".l-title") == "Loved" and ltext(".l-count") == "5 items"), "Loved: title + '5 items'")
        check(wait(lambda: len(lall(".r-love")) == 5), "Loved: every row shows ♥")
        n0 = len(cmds("play_collection"))
        keys("Enter")
        check(new_cmd(n0) == {"cmd": "play_collection", "collection": "loved", "shuffle": True}, f"Loved / Play: shuffled ({strip(last_cmd())})")
        leave_player()
        keys("ArrowDown")
        first = lf()
        n0 = len(cmds("play_collection"))
        keys("Enter")
        check(first and first.startswith("i:") and
              new_cmd(n0) == {"cmd": "play_collection", "collection": "loved", "start_id": int(first[2:]), "shuffle": True},
              f"Loved / an item: start_id ({first}, {strip(last_cmd())})")
        check(wait(lambda: cur() == int(first[2:])), "the picked item plays first")
        leave_player()
        to_menu()

        # ---- Music videos: Play row + 4-column poster grid
        check(home_tile("browse:videos"), "Home: Browse / Music videos")
        keys("Enter")
        check(wait(lambda: ltext(".l-title") == "Music videos" and ltext(".l-count") == count_items(len(vids))),
              f"Music videos: title + '{len(vids)} items'")
        check(len(lall(".tile.poster.grid")) == len(vids), f"Music videos: {len(vids)} posters")
        check(abs(rect("#lists .l-screen:not(.hidden) .tile.poster.grid")["w"] - 410) < 2, "posters 410 px wide")
        tops = js("[...document.querySelectorAll('#lists .l-screen:not(.hidden) .tile.poster.grid')].slice(0, 5).map(e => Math.round(e.getBoundingClientRect().top))")
        check(len(set(tops[:4])) == 1 and tops[4] > tops[0], f"4 posters per row ({tops})")
        n0 = len(cmds("play_collection"))
        keys("Enter")
        check(new_cmd(n0) == {"cmd": "play_collection", "collection": "videos", "shuffle": True}, "Music videos / Play: shuffled")
        leave_player()
        keys("ArrowDown", "ArrowDown", "ArrowDown", "ArrowDown", "ArrowDown", "ArrowRight")   # the last grid row
        page.wait_for_timeout(600)
        pick = lf()
        r = rect("#lists .l-screen:not(.hidden) .focused")
        check((pick or "").startswith("video:") and js("ltv.lists.top().focus.ty") < 0 and r["t"] >= 0 and r["b"] <= 1080,
              f"▼ x5 to the last grid row: the page moved, the poster is on screen ({pick}, {r})")
        page.screenshot(path=str(OUT / "p5-videos.png"))
        n0 = len(cmds("play_collection"))
        keys("Enter")
        check(pick and new_cmd(n0) == {"cmd": "play_collection", "collection": "videos", "start_id": int(pick.split(":")[1]), "shuffle": True},
              f"OK on a poster: start_id ({strip(last_cmd())})")
        leave_player()
        keys("ArrowUp", "ArrowUp", "ArrowUp", "ArrowUp", "ArrowUp", "ArrowUp")
        check(wait(lambda: lf() == "play" and js("ltv.lists.top().focus.ty") == 0), "▲ back to Play: page at the top")
        to_menu()

        # ---- Folders: roots -> folder (subfolders only) -> folder (files + subfolder)
        check(home_tile("browse:folders"), "Home: Browse / Folders")
        keys("Enter")
        check(wait(lambda: ltext(".l-title") == "Folders" and lf() == "r:music"), "Folders: the root row focused")
        keys("Enter")
        check(wait(lambda: ltext(".l-title") == "music" and lf() == "play"), "root folder opened, Play focused")
        check(lall(".row.play .r-title") == ["Play (including subfolders)"], f"root has only subfolders: one Play row ({lall('.row.play .r-title')})")
        check(lall(".l-header") == ["Folders"], "root: header Folders")
        n0 = len(cmds("play_collection"))
        keys("Enter")
        check(new_cmd(n0) == {"cmd": "play_collection", "collection": "folder", "root": "music", "folder": "", "recursive": True, "shuffle": True},
              f"Play (including subfolders): recursive ({strip(last_cmd())})")
        leave_player()
        for _ in range(4):
            if lf() == "f:songs":
                break
            keys("ArrowDown")
        keys("Enter")
        check(wait(lambda: ltext(".l-title") == "songs"), "folder 'songs' opened")
        check(wait(lambda: lall(".row.play .r-title") == ["Play (including subfolders)", "Play (only this folder)"]),
              f"songs: both Play rows ({lall('.row.play .r-title')})")
        check(lall(".l-header") == ["Folders", "Files"], "songs: headers Folders, Files")
        keys("ArrowDown")
        n0 = len(cmds("play_collection"))
        keys("Enter")
        check(new_cmd(n0) == {"cmd": "play_collection", "collection": "folder", "root": "music", "folder": "songs", "recursive": False, "shuffle": True},
              f"Play (only this folder): recursive false ({strip(last_cmd())})")
        leave_player()
        page.screenshot(path=str(OUT / "p5-folder.png"))
        for _ in range(6):
            if lf() == f"i:{S}":
                break
            keys("ArrowDown")
        n0 = len(cmds("play_collection"))
        keys("Enter")
        check(new_cmd(n0) == {"cmd": "play_collection", "collection": "folder", "root": "music", "folder": "songs", "recursive": False,
                              "start_id": S, "shuffle": True}, f"OK on a file: only this folder, start_id ({strip(last_cmd())})")
        leave_player()
        to_menu()

        # ---- Queue: 60 entries, opens centred on the playing one, virtualised, jump, focus stays on track changes
        all_ids = [i["id"] for i in get("/api/library?all=1")["items"]]
        rc("play_ids", ids=all_ids * 4, start_index=40)
        check(wait(lambda: len(state()["queue"]) == len(all_ids) * 4 and state()["index"] == 40), f"queue of {len(all_ids) * 4}, playing #41")
        nq = len(all_ids) * 4
        leave_player()   # the remote's start opened the player
        check(wait(lambda: js("ltv.screen") == "home"), "Home")
        check(home_tile("browse:queue"), "Home: Browse / Queue")
        keys("Enter")
        check(wait(lambda: ltext(".l-title") == f"Queue · {nq}"), f"Queue: title 'Queue · {nq}'")
        check(wait(lambda: lf() == "q:40"), f"Queue opens focused on the playing entry ({lf()})")
        r = rect("#lists .l-screen:not(.hidden) .focused")
        check(abs((r["t"] + r["b"]) / 2 - 540) < 60, f"... centred on the screen ({r['t']:.0f}-{r['b']:.0f})")
        check("Now playing" in (ltext("[data-focus='q:40']") or "") and js("document.querySelector('#lists [data-focus=\"q:40\"]').classList.contains('now')")
              and (ltext("[data-focus='q:40'] .r-num") or "").strip() == "41", "playing entry: number 41, 'Now playing', marked")
        nrows = len(lall(".l-rows .row"))
        check(0 < nrows < nq - 10, f"virtualised: {nrows} of {nq} rows in the page")
        check(wait(lambda: not any(t.startswith("Item #") for t in lall(".l-rows .r-title"))), "queue titles known (library loaded once)")
        page.screenshot(path=str(OUT / "p5-queue.png"))
        keys("ArrowDown", "ArrowDown", "ArrowDown")
        check(lf() == "q:43", f"▼ x3: entry 44 ({lf()})")
        n0 = len(cmds("jump"))
        keys("Enter")
        check(new_cmd(n0, "jump") == {"cmd": "jump", "index": 43}, f"OK: cmd jump index 43 ({strip(cmds('jump')[-1] if cmds('jump') else None)})")
        check(wait(lambda: any(re.match(rf"queue: jump to 44 of {nq} \(item \d+\)", l) for l in journal())), "journal: 'queue: jump to 44 of …'")
        check(wait(lambda: state()["index"] == 43) and wait(lambda: js("ltv.screen") == "player"), "the server plays entry 44, the player opens")
        keys("Escape")
        check(wait(lambda: js("ltv.screen") == "lists") and lf() == "q:43", "Back: the queue, focus on 44")
        check(wait(lambda: js("document.querySelector('#lists [data-focus=\"q:43\"]')?.classList.contains('now')")), "the marker moved to 44")
        rc("next")
        check(wait(lambda: state()["index"] == 44) and wait(lambda: js("document.querySelector('#lists [data-focus=\"q:44\"]')?.classList.contains('now')")),
              "track change from the remote: marker on 45")
        check(lf() == "q:43", f"... the focus does not move ({lf()})")
        keys(*["ArrowUp"] * 46)
        page.wait_for_timeout(600)
        check(lf() == "q:0" and js("ltv.lists.top().list.ty") == 0, f"▲ to the first entry: the title is back ({lf()})")
        check(wait(lambda: any(l.startswith("fps list queue:") for l in journal())), "FPS line for a list move")
        box = rect("#lists .l-screen:not(.hidden) [data-focus='q:3']")
        page.mouse.move(box["l"] + 300, box["t"] + box["h"] / 2)
        page.wait_for_timeout(300)
        check(lf() == "q:3", f"pointer over a row focuses it ({lf()})")
        page.mouse.wheel(0, 120)
        page.wait_for_timeout(500)
        check(js("ltv.lists.top().list.ty") < 0, "wheel scrolls the list")
        keys("ArrowDown")
        check(not js("document.body.classList.contains('pointer')"), "an arrow key leaves pointer mode")
        to_menu()
        check(js("ltv.screen") == "home" and focused() == "browse:queue", "Back: Home on Browse / Queue")
        check(not errors, f"P5: no page errors ({errors[:2]})")
        check(not any(l.startswith("JSERR") for l in journal()), "P5: no JSERR lines")
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
    shutil.copy(srvlog, OUT / "ltv-check-server.log")
    if log.count("Traceback"):
        fails.append("server tracebacks")
print(f"FAILED {len(fails)}" if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
