#!/usr/bin/env python3
"""The info popup inside the player (fake mpv, fake server fetches, fake clock):
  shows for 5 s when a different video starts (waits briefly for the cover, never twice for the same video), Back shows it
  when it is hidden and hides it when it is up (menu closed), the drawing = osd-overlay 6 + the cover as overlay-add 1
  (premultiplied bgra at the screen's pixel size), redraws once a second while playing and not when nothing changed,
  removed after 5 s / by Back / by Stop, never over the lyrics screen, the menu or the screensaver, comes back after an mpv
  restart, an item without a picture gets the tile and no bitmap, Back in an open menu goes up a page (not the popup).
    python3 test_info_player.py   (Pillow for the cover)
"""
import asyncio
import io
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


TRACKS = [{"type": "video", "id": 1, "codec": "hevc", "demux-w": 1920, "demux-h": 1080}, {"type": "audio", "id": 1, "selected": True}]


def item(i, art=True, **kw):
    return dict({"id": i, "kind": "video", "title": f"Video {i}", "artist": "Artist", "duration": 100, "codec": "hevc", "acodec": "opus",
                 "art": art}, **kw)


def ov(mpv, oid):
    return [c for c in mpv.cmds("osd-overlay") if c[1] == oid]


def adds(mpv):
    return mpv.cmds("overlay-add")


async def settle(n=6):
    for _ in range(n):          # real short sleeps: the cover's thumbnail is made in a worker thread
        await asyncio.sleep(0.01)


async def main():
    tmp = tempfile.mkdtemp()
    c = Clock()
    sent = []
    cfg = M.Config({"MTV_SERVER": "http://media.test:8080", "MTV_STATE": tmp})
    p = M.Player(cfg, send=sent.append, mono=c)
    mpv = FakeMpv(TRACKS)
    mpv.props.update({"display-width": 3840, "display-height": 2160, "display-fps": 60, "current-vo": "gpu-next"})
    slow = {"on": False}

    async def fetch(path):
        raise OSError("no such path")

    async def fetch_bytes(path):
        if slow["on"]:
            await asyncio.sleep(0.05)
        try:
            from PIL import Image
            buf = io.BytesIO()
            Image.new("RGB", (64, 36), (200, 40, 40)).save(buf, "PNG")
            return buf.getvalue()
        except ImportError:
            raise OSError("no Pillow")
    p.fetch, p.fetch_bytes, p.http = fetch, fetch_bytes, object()
    await p.attach(mpv)
    p.state = {"upcoming": [{"id": 11, "title": "Next one"}], "shuffle": True, "repeat_mode": "all", "queue": [10, 11], "index": 0}
    check(not p.info.visible and not ov(mpv, 6), "nothing on the screen before a video")

    # ---- a different video: the popup for 5 s, with the cover
    await p.handle({"type": "load", "item": item(10), "position": 0, "autoplay": True})
    await p._on_file_loaded(mpv.entry)
    await settle()
    shown = ov(mpv, 6)
    check(shown and shown[-1][2] == "ass-events" and "Video 10" in shown[-1][3] and "Next one" in shown[-1][3]
          and shown[-1][4:] == (1920, 1080, 8), "a new video: the card (osd-overlay 6, 1920 x 1080, z 8) with title and next")
    a = adds(mpv)
    check(len(a) == 1 and a[0][1] == 1 and a[0][2:4] == (188, 1680) and a[0][5:] == (0, "bgra", 480, 270, 1920),
          f"the cover: overlay-add 1 at the 4K pixel position, 480 x 270 bgra ({a[-1][1:] if a else None})")
    check(a and os.path.getsize(a[0][4]) == 480 * 270 * 4, "... from a raw file of w x h x 4 bytes")
    check(p.info.visible, "popup is up")

    # ---- redraws: once a second while playing, not when nothing changed
    n = len(ov(mpv, 6))
    await p.tick()
    check(len(ov(mpv, 6)) == n, "no redraw when nothing changed")
    p.pos = 1.2
    c.t += 1.3
    await p.tick()
    check(len(ov(mpv, 6)) == n + 1 and "0:01" in ov(mpv, 6)[-1][3], "the next second: redrawn with the new time")
    check(len(adds(mpv)) == 1, "... the cover bitmap is not sent again")
    p.pos = 1.7
    await p.tick()
    check(len(ov(mpv, 6)) == n + 1, "the same second again: no redraw")
    await p.on_control({"action": "pause"})
    await p.tick()
    check("PAUSED" in ov(mpv, 6)[-1][3], "paused: redrawn as PAUSED")
    await p.on_control({"action": "play"})

    # ---- after 5 s it goes
    c.t += 5.0
    await p.tick()
    check(ov(mpv, 6)[-1][2] == "none" and mpv.cmds("overlay-remove")[-1][1] == 1, "after 5 s: card and cover removed")
    check(not p.info.visible, "popup is down")
    k = len(mpv.calls)
    await p.tick()
    check(len(mpv.calls) == k, "nothing is sent while it is hidden")

    # ---- Back: shows, Back again hides at once, again shows and ends after 5 s
    await M.on_key(p, "back", False)
    check(p.info.visible and ov(mpv, 6)[-1][2] == "ass-events", "Back (no menu): the popup shows")
    c.t += 2
    await M.on_key(p, "back", False)
    check(not p.info.visible and ov(mpv, 6)[-1][2] == "none" and mpv.cmds("overlay-remove")[-1][1] == 1,
          "Back while it is up: hidden at once, cover removed")
    await M.on_key(p, "back", False)
    check(p.info.visible, "Back again: shows again")
    c.t += 4.9
    await p.tick()
    check(p.info.visible and ov(mpv, 6)[-1][2] == "ass-events", "still up after 4.9 s")
    c.t += 0.2
    await p.tick()
    check(not p.info.visible and ov(mpv, 6)[-1][2] == "none", "gone after 5 s (max)")
    check(not [m for m in sent if m.get("cmd") in ("next", "prev", "toggle", "stop")], "Back sends no player command")

    # ---- the same video again (Stop, Play) does not announce itself; another one does
    await p.handle({"type": "load", "item": item(10), "position": 0, "autoplay": True})
    await settle()
    check(not p.info.visible, "the same video reloaded: no popup")
    await p.handle({"type": "load", "item": item(11), "position": 0, "autoplay": True})
    await settle()
    check(p.info.visible and "Video 11" in ov(mpv, 6)[-1][3], "the next video: the popup again")
    c.t += 3
    await p.handle({"type": "load", "item": item(10), "position": 0, "autoplay": True})
    await settle()
    c.t += 3
    await p.tick()
    check(p.info.visible, "a switch restarts the 5 s (3 s + 3 s later it is still up)")
    c.t += 2.1
    await p.tick()
    check(not p.info.visible, "... and ends 5 s after the last switch")

    # ---- the cover is on its way: no flash of the empty tile
    mpv.calls.clear()
    slow["on"] = True
    await p.handle({"type": "load", "item": item(12), "position": 0, "autoplay": True})
    check(not ov(mpv, 6), "cover still loading: nothing drawn yet")
    await asyncio.sleep(0.1)
    await settle()
    check(ov(mpv, 6) and len(adds(mpv)) == 1 and "Video 12" in ov(mpv, 6)[-1][3], "cover arrived: one drawing with the cover")
    slow["on"] = False
    p.info.hide()
    await p.draw_info()
    # a cover that never comes: after 1 s the card is drawn with the tile
    mpv.calls.clear()
    p.fetching.add(("art", 13))
    await p.handle({"type": "load", "item": item(13), "position": 0, "autoplay": True})
    p.fetching.add(("art", 13))
    await p.tick()
    check(not ov(mpv, 6), "cover pending < 1 s: not yet")
    c.t += 1.1
    await p.tick()
    check(ov(mpv, 6) and ov(mpv, 6)[-1][2] == "ass-events" and not adds(mpv), "pending > 1 s: the card with the tile, no bitmap")
    p.fetching.discard(("art", 13))

    # ---- an item without a picture: the tile at once, no bitmap
    p.info.hide()
    await p.draw_info()
    mpv.calls.clear()
    await p.handle({"type": "load", "item": item(14, art=False), "position": 0, "autoplay": True})
    await settle()
    check(ov(mpv, 6) and ov(mpv, 6)[-1][2] == "ass-events" and not adds(mpv), "no picture: the card at once, no cover bitmap")
    p.info.hide()
    await p.tick()

    # ---- Back inside the menu goes up a page; the popup is not involved
    await p.handle({"type": "load", "item": item(10), "position": 0, "autoplay": True})
    await settle()
    p.info.hide()
    await p.tick()
    await M.on_key(p, "blue", False)
    check(p.menu.open and not p.info.visible, "menu open")
    mpv.calls.clear()
    await M.on_key(p, "back", False)
    check(not p.menu.open and not ov(mpv, 6) or ov(mpv, 6)[-1][2] == "none", "Back on the menu's first page closes the menu ...")
    check(not p.info.visible and not [c_ for c_ in ov(mpv, 6) if c_[2] == "ass-events"], "... and does not show the popup")

    # ---- the menu takes the screen: the popup goes and does not come back
    await M.on_key(p, "back", False)
    check(p.info.visible, "popup up")
    await M.on_key(p, "blue", False)
    check(not p.info.visible and ov(mpv, 6)[-1][2] == "none" and mpv.cmds("overlay-remove")[-1][1] == 1, "opening the menu removes the popup")
    await M.on_key(p, "blue", False)
    c.t += 1
    await p.tick()
    check(not p.info.visible, "closing the menu does not bring it back")

    # ---- the lyrics screen has its own title / progress: no popup
    await p.set_lyrics_on(True)
    await settle()
    n = len(ov(mpv, 6))
    await M.on_key(p, "back", False)
    check(not p.info.visible and len(ov(mpv, 6)) == n, "lyrics screen on: Back shows nothing")
    await p.handle({"type": "load", "item": item(15), "position": 0, "autoplay": True})
    await settle()
    check(not p.info.visible, "lyrics screen on: a new video shows no popup")
    await p.set_lyrics_on(False)
    await settle()
    p.info.show()
    await p.set_lyrics_on(True)
    await p.tick()
    check(not p.info.visible, "turning the lyrics screen on removes a popup that is up")
    await p.set_lyrics_on(False)
    await settle()

    # ---- the screensaver
    await M.on_key(p, "back", False)
    check(p.info.visible, "popup up again")
    p.saver.on = True
    await p.draw_info()
    check(not p.info.visible and ov(mpv, 6)[-1][2] == "none", "screensaver on: popup removed")
    p.saver.on = False

    # ---- Stop and nothing playing
    await M.on_key(p, "back", False)
    check(p.info.visible, "popup up")
    await p.on_control({"action": "stop"})
    check(not p.info.visible and ov(mpv, 6)[-1][2] == "none", "Stop: the popup goes")
    texts = []
    orig = M.osd

    async def spy(player, text, ms=1500):
        texts.append(text)
    M.osd = spy
    try:
        await M.on_key(p, "back", False)
    finally:
        M.osd = orig
    check(texts == ["Nothing playing"] and not p.info.visible, "Back with nothing playing: a short text, no popup")

    # ---- mpv restarted: the popup is drawn again (new mpv has no overlays)
    await p.handle({"type": "load", "item": item(16), "position": 0, "autoplay": True})
    await settle()
    c.t += 1
    check(p.info.visible and p.info_drawn is not None, "popup up before the restart")
    mpv2 = FakeMpv(TRACKS)
    mpv2.props.update(mpv.props)
    await p.attach(mpv2)
    await p.tick()
    await settle()
    check(ov(mpv2, 6) and ov(mpv2, 6)[-1][2] == "ass-events" and len(adds(mpv2)) == 1, "after an mpv restart: card and cover drawn again")

    # ---- a different mpv error never kills the loop
    class Broken(FakeMpv):
        async def command(self, *args, timeout=5.0):
            if args[0] in ("osd-overlay", "overlay-add"):
                raise M.MpvError("gone")
            return await super().command(*args, timeout=timeout)
    p.info_drawn = p.info_thumb = None
    p.mpv = Broken(TRACKS)
    p.info.show()
    await p.tick()
    check(True, "a failing overlay command is logged, the tick goes on")

    shutil.rmtree(tmp, ignore_errors=True)
    if p.info_dir:
        shutil.rmtree(p.info_dir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
    print("ALL PASSED" if not failed else f"FAILED {failed}")
    sys.exit(1 if failed else 0)
