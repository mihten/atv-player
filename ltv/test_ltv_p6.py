#!/usr/bin/env python3.13
"""LTVPlayer P6 (app 0.8.2): the Aurora song player, OLED care and the song player's FPS lines in desktop Chromium as
the ltv player of a THROWAWAY server.

    SERVER_PY=<python with aiohttp mutagen pillow> PYTHONPATH=<dir with aiohttp> python3.13 ltv/test_ltv_p6.py [outdir]

Checks: Aurora song player (AuroraPlayer: cover left at 640, eyebrow NOW PLAYING / PAUSED, title, artist, album line,
chips, glow progress that runs, Up next; no bottom overlay, no centred cover; the cover 0.9 and the backdrop still
while paused, drifting while playing); one `fps song <look>` line per song and look (Aurora backdrop drifting, Noir,
Vinyl record spinning); OLED care with ?oled=2500 (a still screen dims after the time without a key and moves a few
px; any key wakes it AND still acts; a new item wakes it; a playing video never dims; a paused video and the lyrics
screen do; leaving the player wakes it); journal lines `key: next (▼)`, `key: prev (CH-)`, `quick menu: closed`.
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
PORT = int(os.environ.get("LTV_TEST_PORT", "18127"))
BASE = f"http://127.0.0.1:{PORT}"
PY = os.environ.get("SERVER_PY", sys.executable)
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="ltv-p6-shots-"))
OUT.mkdir(parents=True, exist_ok=True)
tmp = Path(tempfile.mkdtemp(prefix="ltv-p6-"))
fails = []
OLED_MS = 2500


def check(cond, label):
    print(time.strftime("%H:%M:%S ") + ("PASS " if cond else "FAIL ") + label, flush=True)
    if not cond:
        fails.append(label)


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def webm(path, seconds, freq, title):
    path.parent.mkdir(parents=True, exist_ok=True)
    ff("-f", "lavfi", "-i", f"testsrc=size=320x180:rate=25:duration={seconds}", "-f", "lavfi", "-i",
       f"sine=frequency={freq}:duration={seconds}", "-map", "0:v", "-map", "1:a", "-c:v", "libvpx-vp9", "-deadline",
       "realtime", "-cpu-used", "8", "-b:v", "200k", "-c:a", "libopus", "-metadata", f"title={title}",
       "-metadata", "artist=Clipper", str(path))


def song(path, seconds, freq, title, track=None, year=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    cover = tmp / f"{title}.jpg"
    ff("-f", "lavfi", "-i", "testsrc2=size=600x600:rate=1:duration=1", "-frames:v", "1", str(cover))
    ff("-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}", "-i", str(cover), "-map", "0:a", "-map", "1:v",
       "-c:a", "flac", "-c:v", "mjpeg", "-disposition:v", "attached_pic", "-metadata", f"title={title}",
       *(["-metadata", f"track={track}"] if track else []), *(["-metadata", f"date={year}"] if year else []),
       "-metadata", "artist=Tester", "-metadata", "album=Test Album", "-metadata", "lyrics=Line one\nLine two",
       str(path))


lib = tmp / "lib"
webm(lib / "videos" / "Oled Clip.webm", 40, 440, "Oled Clip")
song(lib / "songs" / "03 Long Song.flac", 90, 330, "Long Song", track=3, year=1999)
song(lib / "songs" / "Next Song.flac", 90, 350, "Next Song")
N_ITEMS = 3

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
    items = {it["title"]: it for it in get("/api/library?all=1")["items"]}
    V1, S1, S2 = items["Oled Clip"]["id"], items["Long Song"]["id"], items["Next Song"]["id"]

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
                page.wait_for_timeout(200)
            try:
                return bool(cond())
            except Exception:
                return False

        def js(expr, arg=None):
            return page.evaluate(expr) if arg is None else page.evaluate(expr, arg)

        def css(sel, prop):
            return js("([s, p]) => { const e = document.querySelector(s); return e ? getComputedStyle(e).getPropertyValue(p).trim() : null; }", [sel, prop])

        def text(sel):
            return js("(s) => { const e = document.querySelector(s); return e ? e.textContent.trim() : null; }", sel)

        def cur():
            st = state()
            return st["current"]["id"] if st.get("current") else None

        def tvkey(code):
            js("""(code) => { for (const t of ['keydown', 'keyup']) { const e = new KeyboardEvent(t, {bubbles: true, cancelable: true});
                Object.defineProperty(e, 'keyCode', {get: () => code}); document.dispatchEvent(e); } }""", code)

        def dimmed():
            return js("ltv.player.root.classList.contains('dim')")

        def scale(sel):
            m = css(sel, "transform")
            return round(float(m.split("(")[1].split(",")[0]), 3) if m and m.startswith("matrix(") else 1.0

        def n_lines(pattern):
            return len(journal(pattern))

        remote = b.new_page()
        remote.goto(BASE + "/api/state?session=ltv")
        remote.evaluate("""() => new Promise((ok) => {
            window.r = new WebSocket(location.origin.replace('http', 'ws') + '/ws?role=remote&session=ltv'); window.r.onopen = ok; })""")

        def rc(name, **args):
            remote.evaluate("(m) => window.r.send(JSON.stringify(m))", {"type": "cmd", "cmd": name, **args})

        page.goto(BASE + f"/static/ltv/index.html?from=test&oled={OLED_MS}")
        check(wait(lambda: state()["players"] == 1 and js("ltv.screen") == "home"), "the page joins as the ltv player, Home")
        check(wait(lambda: any(re.match(r"ltv 0\.8\.2 ", l) for l in journal())), "start line: ltv 0.8.2")
        check(js("document.body.dataset.look") == "aurora", "default look: Aurora")
        check(js("ltv.settings.get('dimAfter')") == 0, "default: Dim the player when idle = Off (his choice)")
        js("ltv.lists.open('set-theme')")
        check(wait(lambda: "Dim the player when idle" in js("document.getElementById('lists').textContent")
                   and "Off" in js("document.getElementById('lists').textContent")), "Settings → Theme: 'Dim the player when idle  Off'")
        js("ltv.lists.back()")

        # ---------------------------------------------------------------- Aurora song player
        rc("play_ids", ids=[S1, S2], start_index=0)
        check(wait(lambda: cur() == S1 and js("ltv.screen") == "player" and state()["playing"]), "a song plays (Aurora)")
        check(wait(lambda: css(".pa", "display") == "block"), "Aurora: the song player is shown")
        check(css(".p-overlay", "display") == "none" and css(".p-cover", "display") == "none",
              "... no bottom overlay, no centred cover (the P3 placeholder is gone)")
        check(text(".pa-title") == "Long Song" and text(".pa-artist") == "Tester", "... title + artist")
        check(text(".pa-album") == "Test Album · 1999", f"... album line 'Test Album · 1999' ({text('.pa-album')})")
        check(f"/art/{S1}?s=640" in (js("document.querySelector('.pa-art img').src") or ""), "... the cover (640 px size)")
        box = js("(() => { const r = document.querySelector('.pa-art').getBoundingClientRect(); return [r.left, r.width]; })()")
        check(box[0] < 400 and 570 <= box[1] <= 650, f"... the cover on the left, about 640 wide ({box})")
        check(wait(lambda: text(".pa-state") == "NOW PLAYING"), "... eyebrow NOW PLAYING")
        check(wait(lambda: js("document.querySelectorAll('.pa-next .p-lyr-nrow').length") == 1
                   and "Next Song" in text(".pa-next")), "... UP NEXT: Next Song")
        el1 = text(".pa-el")
        check(wait(lambda: text(".pa-el") not in (el1, "", "0:00"), 4), f"... the time runs ({el1} -> {text('.pa-el')})")
        check(css(".pa-fill", "box-shadow") not in (None, "none"), "... glow progress")
        check(wait(lambda: scale(".pa-art") == 1.0, 3), "... the cover at full size while playing")
        check(not js("ltv.player.songBackdrop.root.classList.contains('still')")
              and css(".pa-bd .bd-f0", "animation-play-state") == "running", "... the backdrop drifts while playing")
        page.screenshot(path=str(OUT / "p6-aurora-song.png"))

        # ---------------------------------------------------------------- the song player's FPS line
        check(wait(lambda: n_lines(rf"fps song aurora #{S1}: [\d.]+ fps, max frame gap \d+ ms, backdrop drifting") == 1, 12),
              "journal: one 'fps song aurora #<id>: … backdrop drifting' line")

        # ---------------------------------------------------------------- OLED care on the song player
        # the default (dim off): quiet for longer than the test time = no dim, but the content still moves
        check(not dimmed(), "dim Off (default): the song player does not dim")
        check(wait(lambda: css(".pa", "translate") not in ("none", "0px", "0px 0px"), 6),
              f"... but it still moves a few px (drift is always on: {css('.pa', 'translate')})")
        check(not dimmed() and n_lines(r"oled: dimmed") == 0, "... still not dimmed, no 'oled: dimmed' line")
        js("ltv.settings.set('dimAfter', 1)")   # 'After 1 minute'; ?oled= shortens it to OLED_MS
        check(wait(lambda: dimmed(), 6), f"dim on: no key for {OLED_MS / 1000} s: the song player dims")
        check(n_lines(r"oled: dimmed after \d+ s without a key \(song\)") >= 1, "journal: 'oled: dimmed after … (song)'")
        check(wait(lambda: css(".p-dim", "opacity") == "0.65", 4), "... to 35 % (a black layer at 0.65)")
        check(wait(lambda: css(".pa", "translate") not in ("none", "0px", "0px 0px"), 6), f"... the content moved a few px ({css('.pa', 'translate')})")
        page.screenshot(path=str(OUT / "p6-aurora-song-dimmed.png"))
        n_seek = len(cmds("seek"))
        page.keyboard.press("ArrowLeft")
        check(wait(lambda: not dimmed(), 2), "a key (◀): full brightness at once")
        check(wait(lambda: len(cmds("seek")) == n_seek + 1), "... and the key still did its job (seek sent)")
        check(wait(lambda: n_lines(r"oled: awake \(key\)") >= 1), "journal: 'oled: awake (key)'")
        check(wait(lambda: dimmed(), 6), "dims again after the next quiet time")
        n_item = n_lines(r"oled: awake \(item\)")
        rc("next")
        check(wait(lambda: cur() == S2 and not dimmed()), "a new song (from the remote): awake again")
        check(wait(lambda: n_lines(r"oled: awake \(item\)") == n_item + 1), "journal: 'oled: awake (item)'")

        # ---------------------------------------------------------------- paused: still cover, still backdrop
        n_pause, n_play = len(cmds("pause")), len(cmds("play"))
        js("ltv.engine.v.pause(); ltv.engine.wantPlay = true")   # a start in progress: play() not resolved yet (TV: up to 1.4 s)
        tvkey(404)
        check(wait(lambda: len(cmds("pause")) == n_pause + 1, 3) and len(cmds("play")) == n_play,
              "Green while a song is still starting: sends Pause, not Play")
        check(wait(lambda: state()["playing"] is False), "Green: paused")
        check(wait(lambda: text(".pa-state") == "PAUSED"), "... eyebrow PAUSED")
        check(wait(lambda: scale(".pa-art") == 0.9, 3), f"... the cover at 0.9 ({scale('.pa-art')})")
        check(wait(lambda: js("ltv.player.songBackdrop.root.classList.contains('still')")), "... the backdrop holds still")
        page.screenshot(path=str(OUT / "p6-aurora-song-paused.png"))
        tvkey(404)
        check(wait(lambda: state()["playing"] is True), "Green: playing again")

        # ---------------------------------------------------------------- the keys in the journal
        page.keyboard.press("ArrowUp")
        check(wait(lambda: cur() == S1), "▲: previous")
        check(wait(lambda: n_lines(r": key: prev \(▲\)$") == 1), "journal: 'key: prev (▲)'")
        page.keyboard.press("ArrowDown")
        check(wait(lambda: cur() == S2), "▼: next")
        check(wait(lambda: n_lines(r": key: next \(▼\)$") == 1), "journal: 'key: next (▼)'")
        tvkey(34)
        check(wait(lambda: n_lines(r": key: prev \(CH-\)$") == 1), "journal: 'key: prev (CH-)'")
        tvkey(406)
        check(wait(lambda: js("ltv.quick.isOpen")), "Blue: quick menu open")
        tvkey(406)
        check(wait(lambda: not js("ltv.quick.isOpen")), "Blue again: closed")
        check(wait(lambda: n_lines(r": quick menu: closed$") == 1), "journal: 'quick menu: closed'")

        # ---------------------------------------------------------------- Noir / Vinyl keep their players, own FPS lines
        js("ltv.settings.set('look', 'noir')")
        check(wait(lambda: css(".pn", "display") == "block" and css(".pa", "display") == "none"), "Noir: its song player, not Aurora's")
        check(wait(lambda: n_lines(r"fps song noir #\d+: [\d.]+ fps, max frame gap \d+ ms, nothing moving") == 1, 12),
              "journal: one 'fps song noir' line")
        js("ltv.settings.set('look', 'vinyl')")
        check(wait(lambda: css(".pv", "display") == "block" and css(".pa", "display") == "none"), "Vinyl: the turntable, not Aurora's")
        check(wait(lambda: n_lines(r"fps song vinyl #\d+: [\d.]+ fps, max frame gap \d+ ms, record spinning") == 1, 12),
              "journal: one 'fps song vinyl: … record spinning' line")
        js("ltv.settings.set('look', 'aurora')")
        check(wait(lambda: css(".pa", "display") == "block"), "Aurora again: its song player")
        page.wait_for_timeout(7000)
        aur = [re.match(r"fps song aurora #(\d+):", l).group(1) for l in journal(r"fps song aurora #")]
        check(len(aur) == len(set(aur)) >= 1, f"one Aurora line per song, never twice ({aur})")

        # ---------------------------------------------------------------- the lyrics screen dims too
        tvkey(405)
        check(wait(lambda: css(".p-lyr", "display") == "block"), "Yellow: the lyrics screen")
        check(wait(lambda: dimmed(), 6) and wait(lambda: n_lines(r"oled: dimmed .*\(lyrics screen\)") >= 1),
              "the lyrics screen dims after the quiet time")
        tvkey(405)
        check(wait(lambda: not dimmed(), 2), "Yellow (a key): awake")

        # ---------------------------------------------------------------- videos: playing = never, paused = dims
        rc("play_ids", ids=[V1], start_index=0)
        check(wait(lambda: cur() == V1 and js("ltv.engine.item && ltv.engine.item.kind") == "video" and state()["playing"]),
              "a video plays")
        check(wait(lambda: js("ltv.engine.position()") > 0.5), "... it is running")
        page.wait_for_timeout(OLED_MS + 2500)
        check(not dimmed(), "a playing video never dims")
        check(css("#player .p-overlay", "translate") in ("none", "0px", "0px 0px"), "... and does not move")
        tvkey(404)
        check(wait(lambda: state()["playing"] is False), "Green: video paused")
        check(wait(lambda: dimmed(), 6) and wait(lambda: n_lines(r"oled: dimmed .*\(paused video\)") >= 1),
              "a paused video dims after the quiet time (the overlay stays up while paused)")
        wait(lambda: css(".p-dim", "opacity") == "0.65", 4)   # the 2 s fade, then the screenshot
        page.screenshot(path=str(OUT / "p6-video-paused-dimmed.png"))
        page.keyboard.press("Escape")
        check(wait(lambda: js("ltv.screen") == "home"), "Back: Home")
        check(wait(lambda: not dimmed(), 3), "left the player: no dim left behind")
        # the setting back to Off while dimmed: awake at once
        rc("play_ids", ids=[S1], start_index=0)
        check(wait(lambda: cur() == S1 and js("ltv.screen") == "player"), "a song again")
        check(wait(lambda: dimmed(), 6), "... dims (setting still on)")
        js("ltv.settings.set('dimAfter', 0)")
        check(wait(lambda: not dimmed(), 3) and wait(lambda: n_lines(r"oled: awake \(setting off\)") == 1),
              "Dim set to Off while dimmed: full brightness at once ('oled: awake (setting off)')")

        check(not errors, f"no page errors ({errors[:2]})")
        check(not any(l.startswith("JSERR") for l in journal()), "no JSERR lines")
        (OUT / "sent.jsonl").write_text("\n".join(f for f in sent if f))
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
        shutil.copy(srvlog, OUT / "server.log")   # the journal of this run, next to the screenshots
    shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASSED" if not fails else f"FAILED {len(fails)}: {fails}")
sys.exit(1 if fails else 0)
