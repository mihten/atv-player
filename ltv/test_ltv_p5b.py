#!/usr/bin/env python3.13
"""LTVPlayer P5b checks (app 0.7.2 + server 0.33.0): his requests of 2026-11-26 in desktop Chromium as the ltv player of a
THROWAWAY server with a fake atv-audio sink.

    SERVER_PY=<python with aiohttp mutagen pillow> PYTHONPATH=<dir with aiohttp> python3.13 ltv/test_ltv_p5b.py [outdir]

Checks: Red = Stop on the player / Home / a list (control stop, the picture goes away, the menu shows, Home says STOPPED)
and Green = Play after it (the item loads again from 0:00) / Play-Pause on every screen; the Settings tile and pages
(Theme text size + effects, Playback jump steps + Prefer videos + keep music, Lyrics language / second language / style,
Mixes add / remove / rename, Sound with the headphones: equalizer, Boost, Loudness, custom equalizer, codec, lip sync,
About); lyrics on the video (timed tracks, the chosen language, a second language on top, hidden for songs); the lyrics
screen (Blue: synced lines light up and scroll, plain lyrics, "no lyrics" card; a short OK still pauses); the quick
menu (hold OK: rows, love, shuffle, lyrics step, queue and back to the player, the sound rows only with the headphones);
the headphones (route, muted picture, clock with the server's time, a start waits for the sink's ready, the output lost
= pause + notice once, the web remote's choice); sized covers (?s=). Screenshots go to outdir.
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
PORT = int(os.environ.get("LTV_TEST_PORT", "18122"))
BASE = f"http://127.0.0.1:{PORT}"
PY = os.environ.get("SERVER_PY", sys.executable)
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="ltv-p5b-shots-"))
OUT.mkdir(parents=True, exist_ok=True)
tmp = Path(tempfile.mkdtemp(prefix="ltv-p5b-"))
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


def song(path, seconds, freq, title, lyrics=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    cover = tmp / f"{title}.jpg"
    ff("-f", "lavfi", "-i", "testsrc2=size=600x600:rate=1:duration=1", "-frames:v", "1", str(cover))
    ff("-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}", "-i", str(cover), "-map", "0:a", "-map", "1:v",
       "-c:a", "flac", "-c:v", "mjpeg", "-disposition:v", "attached_pic", "-metadata", f"title={title}",
       *(["-metadata", f"lyrics={lyrics}"] if lyrics else []),
       "-metadata", "artist=Tester", "-metadata", "album=Test Album", str(path))


ENG = [(0.5, 2.5, "Hello world"), (2.5, 4.5, "Second line"), (4.5, 6.5, "Third line"), (6.5, 30, "Last line")]
HRV = [(0.5, 2.5, "Bok svijete"), (2.5, 4.5, "Drugi redak"), (4.5, 6.5, "Treci redak"), (6.5, 30, "Zadnji redak")]
lib = tmp / "lib"
webm(lib / "videos" / "Lyric Clip.webm", 30, 440, "Lyric Clip", subs=(("eng", ENG), ("hrv", HRV)))
webm(lib / "videos" / "Plain Clip.webm", 30, 550, "Plain Clip")
webm(lib / "videos" / "Third Clip.webm", 30, 660, "Third Clip")
song(lib / "songs" / "01 Song Words.flac", 30, 330, "Song Words", lyrics="First verse line\nSecond verse line\n\nChorus line")
song(lib / "songs" / "02 Song Bare.flac", 30, 350, "Song Bare")
N_ITEMS = 5

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
    LC, PC, TC = items["Lyric Clip"]["id"], items["Plain Clip"]["id"], items["Third Clip"]["id"]
    SW, SB = items["Song Words"]["id"], items["Song Bare"]["id"]
    lyr = get(f"/api/lyrics/{LC}")
    check(lyr["lyr"] == 2 and sorted(t["lang"] for t in lyr["tracks"]) == ["eng", "hrv"],
          f"fixture: Lyric Clip has timed eng + hrv lyrics ({lyr['lyr']}, {[t['lang'] for t in lyr['tracks']]})")
    check(get(f"/api/lyrics/{SW}")["lyr"] == 1, "fixture: Song Words has plain lyrics")
    pl = send_json("/api/playlists", {"name": "Evening List", "ids": [PC, TC]})["id"]

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
        check(wait(lambda: any(re.match(r"ltv 0\.7\.2 .* settings=0 sound=tv", l) for l in journal())),
              "start line: ltv 0.7.2, no stored settings, sound=tv")
        check(wait(lambda: frames("route") and frames("route")[-1] == {"type": "route", "sink": False}
                   and frames("output") and frames("output")[-1]["sound"] == "hdmi"),
              "on connect: route false + output report (sound hdmi)")
        check(wait(lambda: js("ltv.sock.serverNow() !== null")), "the server clock is known after the first pongs")

        # ---------------------------------------------------------------- sized covers (R1)
        check(wait(lambda: js("[...document.querySelectorAll('#home img[data-src], #home img[src*=\"/art/\"]')]"
                              ".every(i => /\\?s=\\d+/.test(i.dataset.src || i.getAttribute('src')))")),
              "every cover on Home is asked for in its size (/art/<id>?s=)")
        check(js("getComputedStyle(document.querySelector('.tile.card, .tile.poster')).boxShadow") == "none",
              "R1: an unfocused tile has no shadow")
        check(js("getComputedStyle(document.querySelector('.h-page')).willChange") == "transform",
              "R1: the Home page layer is promoted (will-change: transform)")

        # ---------------------------------------------------------------- Stop / Play (Red / Green) from the player
        rc("play_ids", ids=[LC, PC, TC], start_index=0)
        check(wait(lambda: cur() == LC and state()["playing"] and js("ltv.screen") == "player"), "Lyric Clip plays (player)")
        page.wait_for_timeout(1200)
        tvkey(403)
        check(wait(lambda: cmds("stop")), "Red in the player: cmd stop")
        check(wait(lambda: js("ltv.engine.stopped") and js("document.getElementById('v').getAttribute('src')") is None),
              "control stop: the picture goes away (no src), the item stays")
        check(wait(lambda: js("ltv.screen") == "home"), "after Stop: back on the menu (Home)")
        check(wait(lambda: page.inner_text(".hero-state") == "STOPPED"), "Home card: STOPPED")
        st = state()
        check(st["playing"] is False and st["current"]["id"] == LC and st["position"] == 0, "server: stopped, item kept, position 0")
        check(any(l.startswith(f"stop #{LC} at ") for l in journal()), "journal: stop line")
        n_load = len(frames("load", got))
        tvkey(404)
        check(wait(lambda: cmds("play")), "Green after Stop on Home: cmd play")
        check(wait(lambda: len(frames("load", got)) > n_load and frames("load", got)[-1]["item"]["id"] == LC
                   and frames("load", got)[-1]["position"] == 0), "... the item loads again from 0:00")
        check(wait(lambda: state()["playing"] is True and not js("ltv.engine.stopped")), "... and plays")
        check(js("ltv.screen") == "home", "Green on Home plays without leaving the menu")
        tvkey(404)
        check(wait(lambda: state()["playing"] is False), "Green on Home again: pause")
        tvkey(404)
        check(wait(lambda: state()["playing"] is True), "... and Green once more: playing")
        # 0.7.1: a held OK in the menus does nothing (it used to fire the focused tile with every key repeat)
        n_cmd, n_load = len(cmds()), len(frames("load", got))
        tvkey(13, "keydown")
        for _ in range(16):
            page.wait_for_timeout(50)
            tvkey(13, "keydown")
        tvkey(13, "keyup")
        page.wait_for_timeout(600)
        check(len(cmds()) == n_cmd and len(frames("load", got)) == n_load and js("ltv.screen") == "home",
              "hold OK on Home: nothing happens (no command, no new item, still Home)")
        check(any(re.match(r"key: OK held \d+ ms on home: ignored", l) for l in journal()), "... journal: OK held … ignored")
        check(js("getComputedStyle(document.getElementById('home')).transitionDuration") == "0s"
              and js("getComputedStyle(document.getElementById('lists')).transitionDuration") == "0s",
              "menus switch without a cross-fade (no video showing through)")

        # ---------------------------------------------------------------- Settings tile + root page
        check(home_settings(), "Browse / Settings opens the Settings page")
        keys = js("ltv.lists.top().list.entries.map(e => String(e.key))")
        check(keys == ["theme", "mixes", "sound", "playback", "lyrics", "about"], f"Settings rows ({keys})")
        page.screenshot(path=str(OUT / "p5b-settings.png"))
        tvkey(404)
        check(wait(lambda: state()["playing"] is False), "Green in a list: pause")
        tvkey(404)
        check(wait(lambda: state()["playing"] is True), "Green in a list: play")
        tvkey(403)
        check(wait(lambda: js("ltv.engine.stopped") and top() == "settings" and js("ltv.screen") == "lists"),
              "Red in a list: Stop, the list stays")
        tvkey(404)
        check(wait(lambda: state()["playing"] is True and not js("ltv.engine.stopped")), "Green in a list after Stop: plays again")

        # Theme: text size + effects
        go_row("theme"); ok()
        check(wait(lambda: top() == "set-theme"), "Theme page")
        go_row("p:textSize"); ok()
        check(wait(lambda: top() == "set-choice"), "Text size: its choice page")
        go_row("c:compact"); ok()
        check(wait(lambda: top() == "set-theme" and js("document.body.classList.contains('ts-compact')")),
              "Compact: back on Theme, body.ts-compact")
        check(js("JSON.parse(localStorage.getItem('ltv.settings')).textSize") == "compact", "... stored on the TV")
        check(js("getComputedStyle(document.querySelector('#lists .l-screen:not(.hidden) .row')).height") == "68px",
              "... rows are 68 px high")
        go_row("p:textSize"); ok(); go_row("c:normal"); ok()
        check(wait(lambda: not js("document.body.classList.contains('ts-compact')")), "Normal again")
        go_row("effects"); ok()
        check(wait(lambda: js("document.body.classList.contains('no-fx')")), "Theme effects off: body.no-fx")
        ok()
        check(wait(lambda: not js("document.body.classList.contains('no-fx')")), "... on again")
        back()

        # Playback: jump step + Prefer videos + keep music
        go_row("playback"); ok()
        check(wait(lambda: top() == "set-playback"), "Playback page")
        go_row("p:seekForward"); ok(); go_row("c:15"); ok()
        check(wait(lambda: js("ltv.settings.get('seekForward')") == 15), "Right button jumps forward: 15 s")
        go_row("prefer")
        before = state()["prefer_videos"]
        ok()
        check(wait(lambda: cmds("prefer_videos") and state()["prefer_videos"] is (not before)), "Prefer videos toggles (cmd + state)")
        ok()
        check(wait(lambda: state()["prefer_videos"] is before), "... and back")
        back()

        # About
        go_row("about"); ok()
        check(wait(lambda: top() == "set-about"), "About page")
        about = js("ltv.lists.top().list.entries.map(e => e.html ? e.html() : '').join(' ')")
        check("0.7.2" in about and "server 0.33.0" in about and "Red" in about and "Blue" in about, "About: versions + key help")
        back()

        # ---------------------------------------------------------------- Mixes: add / remove / rename
        go_row("mixes"); ok()
        check(wait(lambda: top() == "set-mixes" and len(js("ltv.lists.top().list.entries")) == 2), "Mixes page: Mix 1, Mix 2")
        go_row("m:2"); ok()
        check(wait(lambda: top() == "set-mix" and js("ltv.lists.top().list.entries.some(e => e.key === 'addp')")), "Mix 2 page")
        go_row("addp"); ok()
        check(wait(lambda: top() == "set-mix-playlist" and js("ltv.lists.top().list.entries.length") >= 1), "Add playlist: the list")
        go_row(f"p:{pl}"); ok()
        check(wait(lambda: top() == "set-mix" and state()["mixes"][1]["playlists"] == 1), "playlist added to Mix 2 (server)")
        go_row("addf"); ok()
        check(wait(lambda: top() == "set-mix-folder" and js("ltv.lists.top().list.entries.some(e => e.key === 'r:music')")),
              "Add folder: the roots")
        go_row("r:music"); ok()
        check(wait(lambda: top() == "set-mix-folder" and js("ltv.lists.top().list.entries.some(e => String(e.key) === 'f:songs')")),
              "... the root's folders")
        go_row("f:songs"); ok()
        check(wait(lambda: js("ltv.lists.top().list.entries.some(e => e.key === 'add')")), "... a folder: Add this folder")
        go_row("rec"); ok()
        check(js("ltv.settings.get('mixSubfolders')") is False, "Include subfolders off")
        go_row("add"); ok()
        check(wait(lambda: top() == "set-mix" and state()["mixes"][1]["folders"] == 1), "folder added, back on the mix page (3 pages up)")
        src = [s for s in get("/api/mixes")["mixes"][1]["sources"] if s["type"] == "folder"]
        check(src and src[0]["folder"] == "songs" and src[0]["recursive"] is False, f"... as songs, without subfolders ({src})")
        go_row("s:0"); ok()
        check(wait(lambda: state()["mixes"][1]["playlists"] == 0 and state()["mixes"][1]["folders"] == 1), "a click removes (the playlist)")
        go_row("name"); ok()
        check(wait(lambda: js("document.getElementById('prompt').classList.contains('on')")), "Name: the text prompt")
        page.keyboard.press("Control+A")
        page.keyboard.type("Night")
        page.keyboard.press("Enter")
        check(wait(lambda: state()["mixes"][1]["name"] == "Night"), "the mix is renamed (server)")
        back(); back()

        # ---------------------------------------------------------------- lyrics on the video (Lyric Clip, eng default)
        while js("ltv.screen") != "home":
            back()
        rc("play_ids", ids=[LC, PC], start_index=0)
        check(wait(lambda: cur() == LC and js("ltv.screen") == "player" and js("ltv.engine.isPlaying()")), "Lyric Clip in the player")
        rc("seek", position=1.0)
        check(wait(lambda: page.inner_text(".p-sub1") == "Hello world"), "lyrics on the video: the eng line at 1 s")
        check(page.inner_text(".p-sub2") == "", "no second language by default")
        page.screenshot(path=str(OUT / "p5b-subs.png"))
        js("ltv.settings.set('subLang', 'hrv')")
        check(wait(lambda: page.inner_text(".p-sub1") in ("Bok svijete", "Drugi redak")), "Language Croatian: the hrv line")
        js("ltv.settings.set('subLang2', 'eng')")
        check(wait(lambda: page.inner_text(".p-sub2") in ("Hello world", "Second line")), "Second language English on top")
        js("ltv.settings.set('subColor', '#ffff00'); ltv.settings.set('subBox', 176)")
        check(wait(lambda: js("getComputedStyle(document.querySelector('.p-sub1')).color") == "rgb(255, 255, 0)"
                   and "0.69" in js("getComputedStyle(document.querySelector('.p-sub1')).backgroundColor")),
              "style: yellow text on a dark box")
        js("ltv.settings.set('subsOn', false)")
        check(wait(lambda: page.inner_text(".p-sub1") == ""), "Show lyrics off: nothing on the video")
        js("ltv.settings.set('subsOn', true); ltv.settings.set('subLang', ''); ltv.settings.set('subLang2', '');"
           "ltv.settings.set('subColor', '#ffffff'); ltv.settings.set('subBox', 0)")
        t0 = js("document.getElementById('v').currentTime")
        n_seek = len(cmds("seek"))
        page.keyboard.press("ArrowRight")
        check(wait(lambda: len(cmds("seek")) > n_seek and abs(cmds("seek")[-1]["position"] - (t0 + 15)) < 1.5),
              "▶ in the player jumps by the setting (+15 s)")

        # ---------------------------------------------------------------- the lyrics screen (Blue)
        tvkey(406)
        check(wait(lambda: js("ltv.player.root.classList.contains('lyr')")), "Blue: the lyrics screen")
        check(not js("ltv.quick.isOpen"), "... Blue no longer opens the quick menu")
        check(wait(lambda: js("document.querySelectorAll('.p-lyr-line').length") == 4), "... the synced lines (4)")
        rc("seek", position=3.0)
        check(wait(lambda: js("(document.querySelector('.p-lyr-line.on') || {}).textContent") == "Second line"),
              "... the line at 3 s lights up")
        y1 = js("document.querySelector('.p-lyr-col').style.transform")
        rc("seek", position=7.0)
        check(wait(lambda: js("(document.querySelector('.p-lyr-line.on') || {}).textContent") == "Last line"
                   and js("document.querySelector('.p-lyr-col').style.transform") != y1), "... later: the next line, the column scrolls")
        check(state()["playing"] is True, "Blue did not pause")
        page.screenshot(path=str(OUT / "p5b-lyrics-screen.png"))
        page.keyboard.press("Enter")
        check(wait(lambda: state()["playing"] is False), "a short OK on the lyrics screen: pause")
        page.keyboard.press("Enter")
        check(wait(lambda: state()["playing"] is True), "... and play")
        check(js("ltv.settings.get('lyricsScreen')") is True, "the lyrics screen is remembered")
        rc("play_ids", ids=[SW], start_index=0)
        check(wait(lambda: cur() == SW and js("document.querySelector('.p-lyr-col.plain .p-lyr-cap')") is not None),
              "a song with plain lyrics: LYRICS · NOT SYNCED")
        check(js("document.querySelectorAll('.p-lyr-pline').length") == 3, "... its 3 lines")
        rc("play_ids", ids=[SB], start_index=0)
        check(wait(lambda: cur() == SB and "NO LYRICS IN THIS FILE" in page.inner_text(".p-lyr-col")), "no lyrics: the info card")
        check("Song Bare" in page.inner_text(".p-lyr-col") and "Test Album" in page.inner_text(".p-lyr-col"), "... title and album")
        tvkey(406)
        check(wait(lambda: not js("ltv.player.root.classList.contains('lyr')")), "Blue again: the lyrics screen goes")

        # ---------------------------------------------------------------- the quick menu (hold OK)
        rc("play_ids", ids=[LC, PC, TC], start_index=0)
        check(wait(lambda: cur() == LC and js("ltv.screen") == "player"), "Lyric Clip again")
        n_love, n_pp = len(cmds("love")), len(cmds("pause")) + len(cmds("play"))
        tvkey(13, "keydown")   # held like on the TV: keydown repeats every ~50 ms, the first ones without e.repeat
        for _ in range(16):
            page.wait_for_timeout(50)
            tvkey(13, "keydown")
        tvkey(13, "keyup")
        check(wait(lambda: js("ltv.quick.isOpen")), "hold OK: the quick menu opens")
        page.wait_for_timeout(400)
        check(js("ltv.quick.isOpen") and js("(document.querySelector('.qm-row.focused') || {}).dataset?.qm") == "close"
              and len(cmds("love")) == n_love and state()["playing"] is True
              and len(cmds("pause")) + len(cmds("play")) == n_pp,
              "... the held key's repeats and its release do nothing in the menu (still open on Close, no pause)")
        tvkey(406)
        check(wait(lambda: js("ltv.player.root.classList.contains('lyr')")) and js("ltv.quick.isOpen"),
              "Blue with the menu open: the lyrics screen, the menu stays")
        check(wait(lambda: js("(document.querySelector('.qm-row[data-qm=lyrscreen] .qm-v') || {}).textContent") == "On"),
              "... its Lyrics screen row says On")
        tvkey(406)
        check(wait(lambda: not js("ltv.player.root.classList.contains('lyr')")), "... Blue again: off")
        qrows = js("[...document.querySelectorAll('.qm-row')].map(r => r.dataset.qm)")
        check(qrows == ["close", "love", "shuffle", "queue", "subs", "lyrscreen", "output", "version"],
              f"rows without the headphones ({qrows})")
        page.screenshot(path=str(OUT / "p5b-quick.png"))

        def qm_go(row):
            for _ in range(20):
                rows = js("[...document.querySelectorAll('.qm-row')].map(r => r.dataset.qm)")
                at = js("(document.querySelector('.qm-row.focused') || {}).dataset?.qm")
                if at == row:
                    return True
                if row not in rows or at not in rows:
                    return False
                page.keyboard.press("ArrowDown" if rows.index(row) > rows.index(at) else "ArrowUp")
            return False

        qm_go("love"); page.keyboard.press("Enter")
        check(wait(lambda: any(m.get("id") == LC and m.get("on") is True for m in cmds("love"))), "Love: cmd love on")
        check(wait(lambda: get(f"/api/item/{LC}").get("loved") is True), "... the server has the heart")
        qm_go("shuffle"); page.keyboard.press("Enter")
        check(wait(lambda: cmds("shuffle") and state()["shuffle"] is True), "Shuffle: on")
        qm_go("subs"); page.keyboard.press("ArrowRight")
        check(wait(lambda: js("ltv.settings.get('subLang')") == "eng"), "Lyrics ▶: English")
        page.keyboard.press("ArrowRight"); page.keyboard.press("ArrowRight")
        check(wait(lambda: js("ltv.settings.get('subsOn')") is False), "▶ ▶: Off")
        page.keyboard.press("ArrowLeft"); page.keyboard.press("ArrowLeft"); page.keyboard.press("ArrowLeft")
        check(wait(lambda: js("ltv.settings.get('subsOn')") is True and js("ltv.settings.get('subLang')") == ""), "◀ x3: File default")
        tvkey(404)
        check(wait(lambda: state()["playing"] is False) and js("ltv.quick.isOpen"), "Green with the menu open: pause, the menu stays")
        tvkey(404)
        check(wait(lambda: state()["playing"] is True), "... play")
        qm_go("queue"); page.keyboard.press("Enter")
        check(wait(lambda: js("ltv.screen") == "lists" and top() == "queue"), "Queue: the queue screen")
        back()
        check(wait(lambda: js("ltv.screen") == "player"), "Back from that queue: the player again")
        hold_ok()
        check(wait(lambda: js("ltv.quick.isOpen")), "hold OK after the queue: the menu again")
        page.keyboard.press("Escape")
        check(wait(lambda: not js("ltv.quick.isOpen") and js("ltv.screen") == "player"), "Back closes the menu, the player stays")

        # ---------------------------------------------------------------- the headphones (a fake atv-audio sink)
        sinkpage = b.new_page()
        sinkpage.goto(BASE + "/api/state?session=ltv")
        sinkpage.evaluate("""(ready) => new Promise((ok) => {
            window.msgs = []; window.s = new WebSocket(location.origin.replace('http', 'ws') + '/ws?role=sink&id=atv-audio');
            window.s.onmessage = (e) => window.msgs.push(JSON.parse(e.data));
            window.s.onopen = () => { window.s.send(JSON.stringify({type: 'sink', name: 'atv-audio', output: ready}));
                                      window.s.send(JSON.stringify({type: 'audio_info', codecs: %s})); ok(); }; })""" % json.dumps(CODECS),
                          READY)

        def sink_msgs(kind=None):
            return [m for m in sinkpage.evaluate("window.msgs") if kind is None or m.get("type") == kind]

        def sink_send(m):
            sinkpage.evaluate("(m) => window.s.send(JSON.stringify(m))", m)

        check(wait(lambda: js("ltv.audio.sink && ltv.audio.sink.connected") and js("ltv.audio.usable")),
              "the app sees the sink and its ready headphones (state.sink)")
        check(wait(lambda: js("ltv.audio.sink.codecs.length") == 2), "... with their codecs")
        n_route = len(frames("route"))
        check(home_settings(), "Settings again")
        go_row("sound"); ok()
        check(wait(lambda: top() == "set-sound"), "Sound page")
        k1 = js("ltv.lists.top().list.entries.map(e => String(e.key))")
        check("eq" not in k1 and "out" in k1, f"TV sound: no equalizer rows ({k1})")
        go_row("out"); ok(); go_row("c:true"); ok()
        check(wait(lambda: js("ltv.settings.get('headphones')") is True and frames("route")[n_route:]
                   and frames("route")[-1] == {"type": "route", "sink": True}), "Headphones chosen: route true")
        check(wait(lambda: js("document.getElementById('v').muted")), "... the picture is muted")
        check(wait(lambda: state()["sink"]["route"] is True), "... the server: ltv holds the headphones")
        check(any("Sound output changed to the headphones — playback paused" in m.get("text", "") for m in frames("notice")),
              "... playing at the choice: paused with the notice")
        k2 = js("ltv.lists.top().list.entries.map(e => String(e.key))")
        check(all(k in k2 for k in ("lip", "codec", "eq", "eqedit", "boost", "loud")), f"with the headphones: lip sync, codec, sound rows ({k2})")
        page.screenshot(path=str(OUT / "p5b-sound.png"))
        go_row("boost"); ok(); go_row("c:3"); ok()
        check(wait(lambda: state()["sound"]["boost"] == 3), "Boost +3 (server)")
        check(wait(lambda: any(m.get("gain") is not None for m in sink_msgs("sound"))), "... the sink gets the sound message")
        go_row("loud"); ok()
        check(wait(lambda: state()["sound"]["loudness"] == "off"), "Loudness off")
        go_row("eq"); ok(); go_row("c:t:vocal"); ok()
        check(wait(lambda: state()["sound"]["preset"] == "t:vocal"), "Equalizer Vocal")
        go_row("lip"); ok(); go_row("c:-60"); ok()
        check(wait(lambda: any(m.get("ms") == -60 for m in sink_msgs("lip_sync"))), "Lip sync -60 ms reaches the sink")
        go_row("eqedit"); ok()
        check(wait(lambda: top() == "set-eq" and state()["sound"]["preset"] == "custom"), "Edit custom equalizer: custom in use")
        go_row("b3"); page.keyboard.press("ArrowRight"); page.keyboard.press("ArrowRight")
        check(wait(lambda: state()["sound"]["custom"]["bands"][3] == 2), "◀ ▶ on 250 Hz: +2 dB (server)")
        back()
        go_row("codec"); ok(); go_row("c:aptx_adaptive"); ok()
        check(wait(lambda: any(m.get("op") == "set_codec" and m.get("output") == "aptx_adaptive" for m in sink_msgs("audio_cmd")), 8),
              "Codec aptX Adaptive: set_codec reaches the sink")
        sink_send({"type": "sink", "name": "atv-audio", "output": dict(READY, codec="aptx_adaptive")})
        back()

        # a start waits for the sink's ready; the clock carries the server time
        while js("ltv.screen") != "home":
            back()
        rc("play_ids", ids=[PC, TC], start_index=0)
        check(wait(lambda: js("ltv.engine.item && ltv.engine.item.id") == PC and js("ltv.audio.waiting")),
              "headphones on, a server start: the picture waits for the sink's ready")
        page.wait_for_timeout(800)
        check(js("document.getElementById('v').paused"), "... still paused before ready")
        sink_send({"type": "ready", "item": PC})
        check(wait(lambda: js("!document.getElementById('v').paused") and not js("ltv.audio.waiting")), "the sink's ready: it starts")
        check(any(re.match(r"atv-audio: ready after \d+ ms", l) for l in journal()), "journal: ready after N ms")
        playing_clocks = lambda: [c for c in sink_msgs("clock") if c["item"] == PC and c["state"] == "playing"]  # noqa: E731
        check(wait(lambda: len(playing_clocks()) >= 2 and playing_clocks()[-1]["at"] - playing_clocks()[0]["at"] > 1.5),
              "the sink gets playing clocks (4 a second)")
        cl = playing_clocks()
        check(len(cl) >= 2 and cl[-1]["at"] > cl[0]["at"] and isinstance(cl[-1]["position"], (int, float)),
              "... with server time (at) and position")
        rate = None
        if len(cl) >= 2 and cl[-1]["at"] - cl[0]["at"] > 1:
            rate = (cl[-1]["position"] - cl[0]["position"]) / (cl[-1]["at"] - cl[0]["at"])
        check(rate is not None and 0.8 < rate < 1.2, f"... position advances with the server clock (rate {rate})")
        sink_send({"type": "sink", "name": "atv-audio", "output": dict(READY, ready=False)})
        check(wait(lambda: js("document.getElementById('v').paused") and state()["playing"] is False),
              "the headphones go away while playing: paused")
        check(wait(lambda: "not connected — playback paused" in page.inner_text("#capsule")), "... with the notice")
        page.keyboard.press("Enter")
        check(wait(lambda: state()["playing"] is True), "Play after the notice: plays (muted, once per loss)")
        sink_send({"type": "sink", "name": "atv-audio", "output": READY})
        page.wait_for_timeout(500)

        # the quick menu now has the sound rows; volume = the headphones'
        hold_ok()
        check(wait(lambda: js("ltv.quick.isOpen")), "hold OK: the quick menu (headphones)")
        qrows = js("[...document.querySelectorAll('.qm-row')].map(r => r.dataset.qm)")
        check(all(r in qrows for r in ("volume", "eq", "loud", "boost")), f"quick menu with the headphones: volume + sound rows ({qrows})")
        check(qrows.index("volume") == qrows.index("output") + 1, f"... Volume right below Audio output ({qrows})")
        qm_go("volume"); page.keyboard.press("ArrowRight")
        check(wait(lambda: any(m.get("percent") == 45 for m in sink_msgs("volume"))), "Volume ▶: 40 -> 45 % on the sink")
        qfocus = lambda: js("(document.querySelector('.qm-row.focused') || {}).dataset?.qm")  # noqa: E731
        qlist = lambda: js("[...document.querySelectorAll('.qm-row')].map(r => r.dataset.qm)")  # noqa: E731
        qm_go("output"); page.keyboard.press("Enter")
        check(wait(lambda: frames("route")[-1] == {"type": "route", "sink": False} and not js("document.getElementById('v').muted")),
              "Audio output -> TV: route false, sound on")
        check(wait(lambda: "volume" not in qlist()) and qfocus() == "output", "... the focus stays on Audio output")
        page.keyboard.press("Enter")
        check(wait(lambda: frames("route")[-1].get("sink") is True), "Audio output -> Headphones again")
        check(wait(lambda: "volume" in qlist()) and qfocus() == "output", "... Volume comes back below, the focus stays on Audio output")
        page.keyboard.press("Escape")
        check(wait(lambda: not js("ltv.quick.isOpen")), "menu closed")
        # 0.7.1: the wheel in the player = the headphones' volume
        page.wait_for_timeout(1700)   # the menu's own pending value is gone: the sink's 40 % counts
        n_vol = len(sink_msgs("volume"))
        page.mouse.move(960, 540)
        page.mouse.wheel(0, -120)
        check(wait(lambda: len(sink_msgs("volume")) > n_vol and sink_msgs("volume")[-1]["percent"] == 45),
              "wheel up in the player: headphones 40 -> 45 %")
        check(wait(lambda: "Headphones 45 %" in page.inner_text("#capsule")), "... the notice shows it")
        page.wait_for_timeout(1700)
        page.mouse.wheel(0, 120)
        check(wait(lambda: sink_msgs("volume")[-1]["percent"] == 35), "wheel down: 40 -> 35 %")
        hold_ok()
        check(wait(lambda: js("ltv.quick.isOpen")), "hold OK: the quick menu")
        qm_go("output"); page.keyboard.press("Enter")
        check(wait(lambda: frames("route")[-1] == {"type": "route", "sink": False}), "Audio output -> TV for the rest")
        page.keyboard.press("Escape")
        n_vol = len(sink_msgs("volume"))
        page.mouse.wheel(0, -120)
        page.wait_for_timeout(600)
        check(len(sink_msgs("volume")) == n_vol, "wheel with the TV's own sound: nothing sent")

        # the web remote's choice
        r = send_json("/api/mtv-sound", {"session": "ltv", "output": "headphones"})
        check(r.get("ok") and wait(lambda: js("ltv.settings.get('headphones')") is True), "web remote: Headphones for LTVPlayer")
        send_json("/api/mtv-sound", {"session": "ltv", "output": "hdmi"})
        check(wait(lambda: js("ltv.settings.get('headphones')") is False), "web remote: back to the TV")

        # ---------------------------------------------------------------- keep music playing (songs), videos pause
        rc("play_ids", ids=[SW], start_index=0)
        check(wait(lambda: cur() == SW and state()["playing"]), "a song plays")
        js("""() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
                      document.dispatchEvent(new Event('visibilitychange')); }""")
        page.wait_for_timeout(800)
        check(state()["playing"] is True and any(l == "hidden: music keeps playing" for l in journal()),
              "leaving the app with music: it keeps playing")
        js("""() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
                      document.dispatchEvent(new Event('visibilitychange')); }""")
        rc("play_ids", ids=[PC], start_index=0)
        check(wait(lambda: cur() == PC and state()["playing"]), "a video plays")
        js("""() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
                      document.dispatchEvent(new Event('visibilitychange')); }""")
        check(wait(lambda: state()["playing"] is False), "leaving the app with a video: paused")

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
