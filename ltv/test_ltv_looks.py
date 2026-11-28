#!/usr/bin/env python3.13
"""LTVPlayer looks (app 0.7.2): Settings -> Theme -> Look = Aurora / Noir / Vinyl in desktop Chromium as the ltv player
of a THROWAWAY server.

    SERVER_PY=<python with aiohttp mutagen pillow> PYTHONPATH=<dir with aiohttp> python3.13 ltv/test_ltv_looks.py [outdir]

Checks: the default is Aurora; the Look row and its choice page; <body data-look>; per look the corner, the accent
(Noir = the calm Aurora accent, Vinyl = amber), the ink, the backdrop (no moving fields in Noir / Vinyl), the focus
border, the condensed titles; the stored setting survives a reload; the Noir song player (track number, artist,
title, facts, clock, NEXT, hairline progress, black and white while paused); the Vinyl song player (record with the
cover label, SIDE A / TRACK n, title / artist / album, needle progress, numbered Up next), the record turns only while
a song plays on the visible player (not under a menu, not with Theme effects off, not on the lyrics screen), spins
down on pause; videos keep the full picture in every look; Home's Now Playing card (Vinyl = a still record).
Screenshots go to outdir.
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
PORT = int(os.environ.get("LTV_TEST_PORT", "18123"))
BASE = f"http://127.0.0.1:{PORT}"
PY = os.environ.get("SERVER_PY", sys.executable)
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="ltv-looks-shots-"))
OUT.mkdir(parents=True, exist_ok=True)
tmp = Path(tempfile.mkdtemp(prefix="ltv-looks-"))
fails = []
READY = {"kind": "bluetooth", "name": "Headphones X", "codec": "aptx_hd", "ready": True, "volume": 40, "muted": False}
CODECS = [{"codec": "aptx_hd", "label": "aptX HD"}, {"codec": "aptx_adaptive", "label": "aptX Adaptive"}]


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label, flush=True)
    if not cond:
        fails.append(label)


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def vtt(path, cues):
    path.write_text("WEBVTT\n\n" + "\n".join(f"00:00:{a:06.3f} --> 00:00:{b:06.3f}\n{t}\n" for a, b, t in cues))


def webm(path, seconds, freq, title, subs=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["-f", "lavfi", "-i", f"testsrc=size=320x180:rate=25:duration={seconds}", "-f", "lavfi", "-i",
            f"sine=frequency={freq}:duration={seconds}"]
    maps = ["-map", "0:v", "-map", "1:a"]
    meta = []
    for n, (lang, cues) in enumerate(subs):
        f = tmp / f"{title}-{lang}.vtt"
        vtt(f, cues)
        args += ["-i", str(f)]
        maps += ["-map", f"{n + 2}:s"]
        meta += [f"-metadata:s:s:{n}", f"language={lang}", f"-disposition:s:{n}", "default" if n == 0 else "0"]
    ff(*args, *maps, "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "200k", "-c:a", "libopus",
       *(["-c:s", "webvtt"] if subs else []), *meta, "-metadata", f"title={title}", "-metadata", "artist=Clipper", str(path))


def song(path, seconds, freq, title, lyrics=None, track=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    cover = tmp / f"{title}.jpg"
    ff("-f", "lavfi", "-i", "testsrc2=size=600x600:rate=1:duration=1", "-frames:v", "1", str(cover))
    ff("-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}", "-i", str(cover), "-map", "0:a", "-map", "1:v",
       "-c:a", "flac", "-c:v", "mjpeg", "-disposition:v", "attached_pic", "-metadata", f"title={title}",
       *(["-metadata", f"lyrics={lyrics}"] if lyrics else []), *(["-metadata", f"track={track}"] if track else []),
       "-metadata", "artist=Tester", "-metadata", "album=Test Album", str(path))


ENG = [(0.5, 2.5, "Hello world"), (2.5, 4.5, "Second line"), (4.5, 6.5, "Third line"), (6.5, 30, "Last line")]
HRV = [(0.5, 2.5, "Bok svijete"), (2.5, 4.5, "Drugi redak"), (4.5, 6.5, "Treci redak"), (6.5, 30, "Zadnji redak")]
lib = tmp / "lib"
webm(lib / "videos" / "Look Clip.webm", 30, 440, "Look Clip")
song(lib / "songs" / "07 Night Song.flac", 60, 330, "Night Song", track=7)
song(lib / "songs" / "Day Song.flac", 60, 350, "Day Song")
N_ITEMS = 3

env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(tmp / "data"), ATV_CACHE=str(tmp / "cache"),
           ATV_PORT=str(PORT))
env.pop("ATV_BONJOUR", None)
srvlog = tmp / "server.log"
srv = subprocess.Popen([PY, str(REPO / "server" / "server.py")], env=env, stdout=open(srvlog, "a"), stderr=subprocess.STDOUT)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=5) as r:
        return json.loads(r.read())


def send_json(path, body, method="POST"):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read() or b"{}")


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
    items = {it["title"]: it for it in get("/api/library?all=1")["items"]}
    V1, S1, S2 = items["Look Clip"]["id"], items["Night Song"]["id"], items["Day Song"]["id"]
    check(items["Night Song"].get("track") == 7, f"fixture: Night Song is track 7 ({items['Night Song'].get('track')})")

    with sync_playwright() as p:
        b = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = b.new_page(viewport={"width": 1920, "height": 1080})
        errors, sent, got = [], [], []
        page.on("pageerror", lambda e: errors.append(str(e)))

        def on_ws(ws):
            ws.on("framesent", lambda f: sent.append(f if isinstance(f, str) else ""))
            ws.on("framereceived", lambda f: got.append(f if isinstance(f, str) else ""))

        page.on("websocket", on_ws)

        def frames(kind, src=None):
            out = []
            for f in (sent if src is None else src):
                try:
                    m = json.loads(f)
                except ValueError:
                    continue
                if m.get("type") == kind:
                    out.append(m)
            return out

        def cmds(name=None):
            return [m for m in frames("cmd") if name is None or m.get("cmd") == name]

        def wait(cond, secs=10.0):
            end = time.time() + secs
            while time.time() < end:
                try:
                    if cond():
                        return True
                except Exception:
                    pass
                page.wait_for_timeout(200)
            try:
                return bool(cond())
            except Exception:
                return False

        def js(expr, arg=None):
            return page.evaluate(expr) if arg is None else page.evaluate(expr, arg)

        def cur():
            st = state()
            return st["current"]["id"] if st.get("current") else None

        def tvkey(code, kind="press", hold_ms=0):
            """A TV key by its code (Red 403, Green 404, Blue 406 ...: Playwright has no names for them)."""
            js("""([code, kind]) => { for (const t of kind === 'press' ? ['keydown', 'keyup'] : [kind]) {
                const e = new KeyboardEvent(t, {bubbles: true, cancelable: true});
                Object.defineProperty(e, 'keyCode', {get: () => code}); document.dispatchEvent(e); } }""", [code, kind])

        def hold_ok(ms=750):
            tvkey(13, "keydown")
            page.wait_for_timeout(ms)
            tvkey(13, "keyup")

        def top():
            return js("ltv.lists.top() ? ltv.lists.top().kind : null")

        def focused_key():
            return js("(() => { const t = ltv.lists.top(); const e = t && t.list && t.list.focused(); return e ? String(e.key) : null; })()")

        def go_row(key, tries=40):
            for _ in range(tries):
                if focused_key() == key:
                    return True
                keys = js("ltv.lists.top().list.entries.filter(e => e.focusable).map(e => String(e.key))")
                fk = focused_key()
                if key not in keys:
                    return False
                page.keyboard.press("ArrowDown" if keys.index(key) > keys.index(fk) else "ArrowUp")
            return focused_key() == key

        def ok():
            page.keyboard.press("Enter")
            page.wait_for_timeout(250)

        def back():
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)

        def home_settings():
            """Home -> Browse / Settings -> the Settings page."""
            while js("ltv.screen") != "home":
                back()
            for _ in range(8):
                page.keyboard.press("ArrowUp")
            page.keyboard.press("ArrowDown")
            for _ in range(6):
                page.keyboard.press("ArrowRight")
            page.wait_for_timeout(300)
            if js("ltv.home.focus.el && ltv.home.focus.el.dataset.focus") != "browse:settings":
                return False
            ok()
            return wait(lambda: top() == "settings")

        def row_text():
            return js("(() => { const t = ltv.lists.top(); const n = t.list.nodes.get(t.list.fi); return n ? n.innerText : ''; })()")

        remote = b.new_page()
        remote.goto(BASE + "/api/state?session=ltv")
        remote.evaluate("""() => new Promise((ok) => {
            window.r = new WebSocket(location.origin.replace('http', 'ws') + '/ws?role=remote&session=ltv'); window.r.onopen = ok; })""")

        def rc(name, **args):
            remote.evaluate("(m) => window.r.send(JSON.stringify(m))", {"type": "cmd", "cmd": name, **args})

        page.goto(BASE + "/static/ltv/index.html?from=test")
        check(wait(lambda: state()["players"] == 1 and js("ltv.screen") == "home"), "the page joins as the ltv player, Home")
        check(wait(lambda: any(re.match(r"ltv 0\.7\.2 ", l) for l in journal())), "start line: ltv 0.7.2")
        check(js("document.body.dataset.look") == "aurora", "default look: Aurora")

        def css(sel, prop):
            return js("([s, p]) => { const e = document.querySelector(s); return e ? getComputedStyle(e).getPropertyValue(p).trim() : null; }", [sel, prop])

        def pick_look(name, label):
            if not home_settings():
                return False
            go_row("theme"); ok()
            if not wait(lambda: top() == "set-theme"):
                return False
            go_row("p:look")
            ok()
            if not wait(lambda: top() == "set-choice"):
                return False
            go_row(f"c:{name}"); ok()
            return wait(lambda: js("document.body.dataset.look") == name) and wait(lambda: label in row_text())

        def spin_rate():
            return js("ltv.player.spin ? ltv.player.spin.playbackRate * (ltv.player.spin.playState === 'running' ? 1 : 0) : 0")

        def spin_angle():
            return js("""(() => { const m = getComputedStyle(document.querySelector('.pv-disc')).transform;
                if (!m || m === 'none') return 0; const v = m.match(/matrix\\(([^)]+)\\)/)[1].split(',').map(Number);
                return Math.round(Math.atan2(v[1], v[0]) * 180 / Math.PI); })()""")

        # ---------------------------------------------------------------- Aurora stays as it was
        check(css(".bd-field", "display") == "block" and css(".bd-field", "animation-name") == "drift0", "Aurora: drifting fields")
        check(css(".tile", "border-radius") == "22px", "Aurora: corners 22 px")
        page.screenshot(path=str(OUT / "looks-aurora-home.png"))

        # ---------------------------------------------------------------- Noir
        check(pick_look("noir", "Noir"), "Settings -> Theme -> Look -> Noir (row says Noir)")
        check(js("JSON.parse(localStorage.getItem('ltv.settings')).look") == "noir", "... stored on the TV")
        check(css(".bd-field", "display") == "none", "Noir: no moving fields")
        check("radial-gradient" in css("#home .backdrop", "background-image"), "Noir: black + two faint glows")
        check(css(".tile", "border-radius") == "4px", "Noir: corners 4 px")
        check(js("getComputedStyle(document.body).getPropertyValue('--accent').trim()")
              == js("getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()"),
              "Noir: the calm Aurora accent from the cover, no override (his choice)")
        check("ATV Condensed" in css(".l-title", "font-family") and css(".l-h", "text-transform") == "uppercase"
              if js("document.querySelector('.l-h')") else "ATV Condensed" in css(".l-title", "font-family"),
              "Noir: condensed titles, uppercase section titles")
        back(); back()
        check(wait(lambda: js("ltv.screen") == "home"), "back on Home")
        page.wait_for_timeout(400)
        check(css(".tile.focused", "transform") in ("matrix(1.04, 0, 0, 1.04, 0, 0)",) or
              js("!!document.querySelector('.hero.focused')"), "Noir: half the focus lift")
        check(css(".shelf-title", "text-transform") == "uppercase", "Noir: shelf titles uppercase")
        page.screenshot(path=str(OUT / "looks-noir-home.png"))

        rc("play_ids", ids=[S1, S2], start_index=0)
        check(wait(lambda: cur() == S1 and js("ltv.screen") == "player" and state()["playing"]), "a song plays")
        check(wait(lambda: css(".pn", "display") == "block" and css(".p-overlay", "display") == "none"),
              "Noir: its song player, no bottom overlay")
        check(js("document.querySelector('.pn-num').textContent") == "07", "... the track number 07, huge and faint")
        check(js("document.querySelector('.pn-title').textContent") == "Night Song"
              and js("document.querySelector('.pn-artist').textContent") == "Tester", "... title + artist")
        check("Test Album" in js("document.querySelector('.pn-facts').textContent"), "... the facts line")
        check(wait(lambda: js("document.querySelector('.pn-nt').textContent") == "Day Song"), "... NEXT: Day Song")
        check(wait(lambda: js("document.querySelector('.pn-el').textContent") not in ("", "0:00")), "... the clock runs")
        check(css(".pn-art", "filter") == "none", "... the cover in colour while playing")
        page.screenshot(path=str(OUT / "looks-noir-song.png"))
        tvkey(404)
        check(wait(lambda: state()["playing"] is False), "Green: paused")
        check(wait(lambda: css(".pn-art", "filter") == "grayscale(1)"), "Noir: paused = black and white")
        tvkey(404)
        check(wait(lambda: state()["playing"] is True), "... playing again")
        tvkey(406)
        check(wait(lambda: css(".pn", "display") == "none" and css(".p-lyr", "display") == "block"), "Blue: the lyrics screen over Noir")
        page.screenshot(path=str(OUT / "looks-noir-lyrics.png"))
        tvkey(406)
        rc("play_ids", ids=[V1], start_index=0)
        check(wait(lambda: cur() == V1 and js("ltv.engine.item && ltv.engine.item.kind") == "video"), "a video in Noir")
        check(wait(lambda: css(".pn", "display") == "none" and css(".p-overlay", "display") == "block"),
              "Noir video: the full picture + the overlay, no song player")
        page.screenshot(path=str(OUT / "looks-noir-video.png"))

        # ---------------------------------------------------------------- reload keeps the look
        page.reload()
        check(wait(lambda: js("ltv.screen") == "home" and js("document.body.dataset.look") == "noir"), "after a reload: still Noir")

        # ---------------------------------------------------------------- Vinyl
        check(pick_look("vinyl", "Vinyl"), "Settings -> Theme -> Look -> Vinyl")
        check(js("getComputedStyle(document.body).getPropertyValue('--accent').trim()") == "rgb(237, 168, 74)", "Vinyl: amber accent")
        check(css("body", "color") == "rgb(250, 237, 217)", "Vinyl: cream text")
        check(css(".tile", "border-radius") == "10px", "Vinyl: corners 10 px")
        check(css(".bd-field", "display") == "none" and "linear-gradient" in css("#lists .backdrop", "background-image"),
              "Vinyl: warm still backdrop")
        check("svg" in css("#lists .backdrop", "background-image") or "svg" in js(
              "getComputedStyle(document.querySelector('#lists .backdrop'), '::after').backgroundImage"), "Vinyl: the grain tile")
        back(); back()
        check(wait(lambda: js("ltv.screen") == "home"), "back on Home")
        rc("play_ids", ids=[S1, S2], start_index=0)
        check(wait(lambda: cur() == S1 and js("ltv.screen") == "player" and state()["playing"]), "a song plays (Vinyl)")
        check(wait(lambda: css(".pv", "display") == "block" and css(".pn", "display") == "none"), "Vinyl: the turntable screen")
        check(js("document.querySelector('.pv-side').textContent").replace("  ", " ") == "SIDE A · TRACK 7", "... SIDE A · TRACK 7")
        check(js("document.querySelector('.pv-label').src").find(f"/art/{S1}") >= 0, "... the cover is the record's label")
        check(wait(lambda: js("document.querySelectorAll('.pv-nrow').length") == 1), "... numbered Up next (1)")
        check(wait(lambda: spin_rate() == 1, 6), "... the record turns at full speed (33 rpm) while playing")
        a1 = spin_angle(); page.wait_for_timeout(450); a2 = spin_angle()
        check(a1 != a2, f"... the angle changes ({a1} -> {a2})")
        def arm_b():
            m = css(".pv-arm", "transform")
            return float(m.split("(")[1].split(",")[1]) if m and m.startswith("matrix(") else 0.0
        check(wait(lambda: arm_b() > 0.2, 3), f"... the tonearm swung onto the record ({arm_b():.2f})")
        page.screenshot(path=str(OUT / "looks-vinyl-song.png"))
        tvkey(404)
        check(wait(lambda: state()["playing"] is False), "Green: paused")
        page.wait_for_timeout(250)
        check(wait(lambda: arm_b() < 0, 3), "... the tonearm lifts off")
        r_mid = spin_rate()
        check(0 < r_mid < 1, f"paused: the record spins down ({r_mid:.2f})")
        check(wait(lambda: spin_rate() == 0, 5), "... and stops")
        tvkey(404)
        check(wait(lambda: spin_rate() == 1, 6), "playing again: up to speed")
        tvkey(406)
        check(wait(lambda: spin_rate() == 0, 5) and css(".pv", "display") == "none", "lyrics screen: the record is hidden and stops")
        tvkey(406)
        check(wait(lambda: spin_rate() == 1, 6), "lyrics screen off: turns again")
        back()
        check(wait(lambda: js("ltv.screen") == "home" and spin_rate() == 0), "Back to Home: the record stops (no spin under a menu)")
        check(css(".hero.song .hero-art", "border-radius") == "50%", "Home Now Playing card: the song as a record")
        check(js("document.getAnimations().filter(a => !(a instanceof CSSTransition) && a.playState === 'running' && a.effect && a.effect.target && a.effect.target.closest && a.effect.target.closest('#home')).map(a => a.animationName || 'waapi').filter(n => n !== 'eq')").__len__() == 0,
              "Home in Vinyl: no running animation except the playing bars")
        page.screenshot(path=str(OUT / "looks-vinyl-home.png"))
        # effects off: no spin
        js("ltv.settings.set('effects', false)")
        rc("play_ids", ids=[S2], start_index=0)
        check(wait(lambda: cur() == S2 and js("ltv.screen") == "player" and state()["playing"]), "a song plays (effects off)")
        page.wait_for_timeout(800)
        check(spin_rate() == 0, "Theme effects off: the record stays still")
        check(js("document.querySelector('.pv-side').textContent") == "NOW SPINNING", "no track number: NOW SPINNING")
        js("ltv.settings.set('effects', true)")
        check(wait(lambda: spin_rate() == 1, 6), "effects on again: it turns")
        rc("play_ids", ids=[V1], start_index=0)
        check(wait(lambda: cur() == V1 and css(".pv", "display") == "none" and css(".p-overlay", "display") == "block"),
              "Vinyl video: the full picture + the overlay")
        check(wait(lambda: spin_rate() == 0, 5), "... no record turning under a video")
        page.screenshot(path=str(OUT / "looks-vinyl-video.png"))

        # ---------------------------------------------------------------- back to Aurora
        check(pick_look("aurora", "Aurora"), "Look -> Aurora again")
        check(css(".bd-field", "display") == "block" and css(".tile", "border-radius") == "22px", "Aurora restored")
        check(js("getComputedStyle(document.body).getPropertyValue('--accent').trim()") != "rgb(237, 168, 74)", "... its cover accent, not amber")

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
    shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASSED" if not fails else f"FAILED {len(fails)}: {fails}")
sys.exit(1 if fails else 0)
