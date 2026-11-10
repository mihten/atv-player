#!/usr/bin/env python3
"""mtv-player's lyrics and the new menu rows with a fake mpv and fake server fetches:
  subtitles: the video's lyrics track in the chosen language (sid = ord + 1, "no" for Off or a missing track), the language
  change applies at once and is kept in lyrics.json; the lyrics screen: shader file set in mpv (a new file per palette), the
  video stretched to the screen's aspect, subtitles hidden, the text layer drawn (overlay 4) and redrawn only when needed,
  a plain background (overlay 5) for an item without a picture, lyrics fetched once per item, everything undone when it is
  switched off, on Stop, and re-applied after an mpv restart; the screen choice is kept across a restart of the program;
  menu: Boost (Left / Right, 0..8, a sound command; quick presses add up), Loudness (OK toggles), Lyrics (Left / Right steps
  through Off / Auto / English / Croatian, stops at the ends), Lyrics screen (OK).
    python3 test_lyrics_player.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mtv_player as M  # noqa: E402
from test_mtv_player import FakeMpv  # noqa: E402

failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


SUBS = [{"ord": 0, "lang": "eng", "title": "English", "is_default": True, "forced": False},
        {"ord": 1, "lang": "hrv", "title": "Hrvatski", "is_default": False, "forced": False}]
TRACKS = [{"type": "video", "id": 1, "codec": "hevc", "demux-w": 1920, "demux-h": 1080},
          {"type": "audio", "id": 1, "selected": True}, {"type": "sub", "id": 1}, {"type": "sub", "id": 2}]
CUES = [{"s": 2.0, "e": 4.0, "t": "Line one"}, {"s": 5.0, "e": 7.0, "t": "Line two"}]
LYRICS = {"id": 10, "lyr": 2, "tracks": [{"ord": 0, "lang": "eng", "is_default": True, "forced": False, "synced": True, "cues": CUES},
                                        {"ord": 1, "lang": "hrv", "is_default": False, "forced": False, "synced": True,
                                         "cues": [{"s": 2.0, "e": 4.0, "t": "Prvi stih"}]}]}


def item(i, **kw):
    return dict({"id": i, "kind": "video", "title": f"Song {i}", "artist": "A", "duration": 100, "subs": SUBS, "lyr": 2,
                 "art": True}, **kw)


def overlays(mpv, oid):
    """Formats ("ass-events" / "none") sent for one overlay id; cmds() gives ("osd-overlay", id, format, data, ...)."""
    return [c[2] for c in mpv.cmds("osd-overlay") if c[1] == oid]


def overlay_data(mpv, oid):
    return [c[3] for c in mpv.cmds("osd-overlay") if c[1] == oid]


def last_cmd(sent):
    return [m for m in sent if m.get("type") == "cmd"][-1]


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def main():
    tmp = tempfile.mkdtemp()
    c = Clock()
    sent = []
    fetched = []
    cfg = M.Config({"MTV_SERVER": "http://media.test:8080", "MTV_STATE": tmp})
    p = M.Player(cfg, send=sent.append, mono=c)
    mpv = FakeMpv(TRACKS)
    mpv.props.update({"display-width": 3840, "display-height": 2160, "display-fps": 60, "current-vo": "gpu-next"})

    async def fetch(path):
        fetched.append(path)
        if path.startswith("/api/lyrics/"):
            return dict(LYRICS, id=int(path.rsplit("/", 1)[1]))
        raise OSError("no such path")

    async def fetch_bytes(path):
        fetched.append(path)
        try:
            import io
            from PIL import Image
            buf = io.BytesIO()
            Image.new("RGB", (32, 32), (200, 40, 40) if path.endswith("/10") else (40, 40, 200)).save(buf, "PNG")
            return buf.getvalue()
        except ImportError:
            raise OSError("no Pillow")
    p.fetch, p.fetch_bytes, p.http = fetch, fetch_bytes, object()
    await p.attach(mpv)
    check(p.lyrics_lang == "auto" and not p.lyrics_on, "defaults: language Auto, lyrics screen off")

    # ---- subtitles on the video
    await p.handle({"type": "load", "item": item(10), "position": 0, "autoplay": True})
    await p._on_file_loaded(mpv.entry)
    await settle()
    check(mpv.sets("sid")[-1] == 1 and mpv.sets("sub-visibility")[-1] is True, "Auto: the file's default track (sid 1), visible")
    check("/art/10" in fetched and "/api/lyrics/10" not in fetched, "cover fetched for the colours; lyrics not fetched (screen off)")
    await p.set_lyrics_lang("hrv")
    check(mpv.sets("sid")[-1] == 2, "Croatian: sid 2 at once")
    check(json.load(open(os.path.join(tmp, "lyrics.json"))) == {"lang": "hrv", "screen": False}, "kept in lyrics.json")
    await p.set_lyrics_lang("off")
    check(mpv.sets("sid")[-1] == "no", "Off: no subtitle")
    await p.set_lyrics_lang("klingon")
    check(p.lyrics_lang == "off", "an unknown language is ignored")
    await p.set_lyrics_lang("eng")
    mpv.tracks = TRACKS[:3]   # a file with one subtitle stream: Croatian is not there
    p.item["subs"] = SUBS[:1]
    await p.set_lyrics_lang("hrv")
    check(mpv.sets("sid")[-1] == 1, "a language the file lacks: its default track")
    p.item["subs"] = [{"ord": 3, "lang": "hrv"}]
    await p.apply_subs()
    check(mpv.sets("sid")[-1] == "no", "a track number beyond the file's subtitle streams: none (no mpv error)")
    mpv.tracks = TRACKS
    p.item["subs"] = SUBS
    await p.set_lyrics_lang("eng")

    # ---- lyrics screen on
    n_fetch = len(fetched)
    await p.set_lyrics_on(True)
    await settle()
    shaders = mpv.cmds("change-list")
    check(shaders and shaders[-1][1:3] == ("glsl-shaders", "set") and os.path.isfile(shaders[-1][3])
          and open(shaders[-1][3]).read().startswith("//!HOOK OUTPUT"), "on: the backdrop shader file is set in mpv")
    check(mpv.sets("video-aspect-override")[-1] == "3840:2160" and mpv.sets("sub-visibility")[-1] is False,
          "on: the video fills the screen (aspect of the display), its subtitles hidden")
    check(fetched[n_fetch:].count("/api/lyrics/10") == 1, "on: the lyrics fetched once")
    front = overlays(mpv, 4)
    check(front and front[-1] == "ass-events" and "Line one" in overlay_data(mpv, 4)[-1] and "Song 10" in overlay_data(mpv, 4)[-1],
          "on: the text layer is drawn with the lyrics and the title")
    check(json.load(open(os.path.join(tmp, "lyrics.json")))["screen"] is True, "screen choice kept in lyrics.json")
    n = len(overlays(mpv, 4))
    await p.draw_lyrics()
    check(len(overlays(mpv, 4)) == n, "nothing changed: no redraw")
    mpv.props["time-pos"] = 2.5
    p.file_loaded = True
    await p.draw_lyrics()
    check(len(overlays(mpv, 4)) == n + 1 and p.view.idx == 0, "a new line: redrawn")
    c.t += 0.1
    await p.draw_lyrics()
    check(len(overlays(mpv, 4)) == n + 2, "while the line lights up: redrawn again (animation frame)")
    c.t += 1.0
    k = len(overlays(mpv, 4))
    await p.draw_lyrics()
    check(len(overlays(mpv, 4)) == k, "after the animation, same second: no redraw")
    check(overlays(mpv, 5)[-1] == "none", "an item with a picture: no plain background")

    # ---- next item: new shader (other cover colours), lyrics for it, mpv's sid follows
    first_shader = shaders[-1][3]
    await p.handle({"type": "load", "item": item(11), "position": 0, "autoplay": True})
    await p._on_file_loaded(mpv.entry)
    await settle()
    await settle()
    last = mpv.cmds("change-list")[-1]
    check(last[1:3] == ("glsl-shaders", "set") and last[3] != first_shader and not os.path.exists(first_shader),
          "next item: a new shader file (its colours), the old file removed")
    check("/api/lyrics/11" in fetched and p.view.item["id"] == 11, "next item: its lyrics fetched")
    # an item without any picture: the plain ASS background
    mpv.tracks = [{"type": "audio", "id": 1}]
    await p.handle({"type": "load", "item": item(12, kind="audio", subs=None, art=False), "position": 0, "autoplay": True})
    await p._on_file_loaded(mpv.entry)
    await settle()
    check(not p.has_picture and overlays(mpv, 5)[-1] == "ass-events", "no picture at all: plain background layer")
    mpv.tracks = TRACKS

    # ---- mpv restart: re-applied
    mpv2 = FakeMpv(TRACKS)
    mpv2.props.update({"display-width": 3840, "display-height": 2160, "current-vo": "gpu-next"})
    p.detach()
    await p.attach(mpv2)
    await settle()
    check(mpv2.cmds("change-list") and mpv2.sets("video-aspect-override")[-1] == "3840:2160" and overlays(mpv2, 4),
          "mpv restarted: shader, aspect and text layer again")
    mpv = mpv2

    # ---- Stop: the screen goes away; the next load brings it back
    await p.on_control({"action": "stop"})
    check(mpv.cmds("change-list")[-1][1:3] == ("glsl-shaders", "clr") and mpv.sets("video-aspect-override")[-1] == "no"
          and overlays(mpv, 4)[-1] == "none", "Stop: shader cleared, aspect back, text layer removed")
    await p.handle({"type": "load", "item": item(10), "position": 0, "autoplay": True})
    await settle()
    check(mpv.cmds("change-list")[-1][1:3] == ("glsl-shaders", "set"), "the next load: lyrics screen back (still on)")

    # ---- a new program start keeps the choice
    p2 = M.Player(cfg, send=lambda m: None, mono=c)
    check(p2.lyrics_on is True and p2.lyrics_lang == "eng", "restart of the program: lyrics screen on, English (lyrics.json)")

    # ---- off
    await p.set_lyrics_on(False)
    check(mpv.cmds("change-list")[-1][1:3] == ("glsl-shaders", "clr") and mpv.sets("video-aspect-override")[-1] == "no"
          and mpv.sets("sub-visibility")[-1] is True and overlays(mpv, 4)[-1] == "none",
          "off: shader cleared, aspect back, subtitles visible, text layer removed")

    # ---- menu rows: Boost, Loudness, Lyrics, Lyrics screen
    p.state = {"current": item(10), "playing": True, "queue": [10], "index": 0,
               "sound": {"preset": "off", "boost": 2, "loudness": "raise"},
               "sound_presets": [["off", "Off"], ["a:momentum4", "Momentum 4"], ["p:11", "Pop"], ["p:13", "Rock"],
                                 ["p:4", "Full bass"], ["p:1", "Classical"], ["p:17", "Techno"], ["custom", "Custom"]]}
    await p.menu.show()
    labels = [r.label for r in p.menu.page.rows]
    check(labels[:8] == ["Lyrics screen", "Lyrics", "Volume", "Boost", "Loudness", "Equalizer", "Shuffle", "Repeat"]
          and labels[8:] == ["Queue", "Playlists", "For you", "Music videos"],
          f"menu: Lyrics screen first, settings, then Queue / Playlists / For you / Music videos ({labels})")
    check(len(labels) <= M.menu_mod.ROWS, f"the first page fits on the screen ({len(labels)} rows)")
    sent.clear()
    p.menu.page.sel = 3
    await M.on_key(p, "right", False)
    await M.on_key(p, "right", False)
    snd = [m for m in sent if m.get("cmd") == "sound"]
    check([m.get("boost") for m in snd] == [3, 4] and p.menu.page.rows[3].sub == "+4 dB",
          f"Boost Right x2: quick presses add up (3, 4) before the server answers ({[m.get('boost') for m in snd]})")
    p.state["sound"]["boost"] = 4
    c.t += 5
    p.menu.refresh_root()
    for _ in range(6):
        await M.on_key(p, "right", False)
    check([m.get("boost") for m in sent if m.get("cmd") == "sound"][-1] == 8 and p.menu.page.rows[3].sub == "+8 dB",
          "Boost stops at +8 dB")
    p.state["sound"]["boost"] = 0
    c.t += 5
    p.menu.refresh_root()
    sent.clear()
    await M.on_key(p, "left", False)
    check(not [m for m in sent if m.get("cmd") == "sound"] and p.menu.page.rows[3].sub == "off",
          f"Boost at 0: Left sends nothing ({[m for m in sent if m.get('cmd') == 'sound']}, {p.menu.page.rows[3].sub})")
    p.menu.page.sel = 4
    await M.on_key(p, "ok", False)
    check(last_cmd(sent) == {"type": "cmd", "cmd": "sound", "loudness": "off"} and p.menu.open, "Loudness OK: off (menu stays open)")
    p.state["sound"]["loudness"] = "off"
    c.t += 5
    p.menu.refresh_root()
    await M.on_key(p, "ok", False)
    check(last_cmd(sent) == {"type": "cmd", "cmd": "sound", "loudness": "raise"}, "Loudness OK again: raise quiet tracks")
    p.menu.page.sel = 5
    sent.clear()
    await M.on_key(p, "right", False)
    check(last_cmd(sent) == {"type": "cmd", "cmd": "sound", "preset": "a:momentum4"} and p.menu.page.rows[5].sub == "Momentum 4",
          "Equalizer Right: Momentum 4 sent through the real player, the row shows it")
    await M.on_key(p, "ok", False)
    check(len([m for m in sent if m.get("cmd") == "sound"]) == 1, "OK on the Equalizer row sends nothing")
    p.menu.page.sel = 1
    check(p.menu.page.rows[1].sub == "English", "Lyrics row shows the language")
    await M.on_key(p, "right", False)
    check(p.lyrics_lang == "hrv" and p.menu.page.rows[1].sub == "Croatian", "Lyrics Right: Croatian")
    await M.on_key(p, "right", False)
    check(p.lyrics_lang == "hrv", "Lyrics Right at the end: stays (no wrap)")
    for _ in range(5):
        await M.on_key(p, "left", False)
    check(p.lyrics_lang == "off" and p.menu.page.rows[1].sub == "Off" and len(p.menu.pages) == 1,
          "Lyrics Left x5: Off, stops there, Left does not leave the page")
    await M.on_key(p, "ok", False)
    check(p.lyrics_lang == "off", "OK on the Lyrics row does nothing")
    p.menu.page.sel = 0
    await M.on_key(p, "ok", False)
    check(p.lyrics_on and p.menu.page.rows[0].value is True and mpv.cmds("change-list")[-1][1:3] == ("glsl-shaders", "set"),
          "Lyrics screen OK: on, the screen comes up under the menu")
    await M.on_key(p, "ok", False)
    check(not p.lyrics_on, "Lyrics screen OK again: off")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
    print("ALL PASSED" if not failed else f"FAILED {failed}")
    sys.exit(1 if failed else 0)
