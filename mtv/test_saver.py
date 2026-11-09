#!/usr/bin/env python3
"""saver.py (the model) and its use in the Player: the screensaver comes up after the idle time, the text jumps to a
new spot every move interval and always stays on the screen, activity (playing, a menu) removes it, ANY key only closes
it (it never does its normal function), the rest of a held key does nothing, Stop key / Stop row, keys after Stop.
    python3 test_saver.py
"""
import asyncio
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mtv_player as M  # noqa: E402
import saver as S  # noqa: E402
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


def model():
    c = Clock()
    s = S.Saver(c, rng=random.Random(7), after=300, move=180)
    check(not s.on and s.tick(False) is None, "model: starts off")
    c.t += 299
    check(s.tick(False) is None and not s.on, "model: not before the idle time")
    c.t += 2
    check(s.tick(False) == "show" and s.on, "model: comes up after 300 s idle")
    x, y = s.pos
    check(S.X_MIN <= x <= S.X_MAX and S.Y_MIN <= y <= S.Y_MAX, f"model: first spot on the screen ({s.pos})")
    c.t += 179
    check(s.tick(False) is None, "model: stays put for 179 s")
    spots = [s.pos]
    for _ in range(40):
        c.t += 181
        check_move = s.tick(False)
        assert check_move == "move"
        spots.append(s.pos)
    far = all((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 >= S.MIN_JUMP ** 2 for a, b in zip(spots, spots[1:]))
    inside = all(S.X_MIN <= p[0] <= S.X_MAX and S.Y_MIN <= p[1] <= S.Y_MAX for p in spots)
    check(far and inside and len(set(spots)) > 30, "model: 40 jumps: always far from the last spot, always on the screen, different spots")
    c.t += 5
    check(s.tick(True) == "hide" and not s.on, "model: activity removes it")
    c.t += 299
    check(s.tick(False) is None, "model: the idle timer restarted at the activity")
    ass = s.render()
    check("MTVPlayer" in ass and ass.count("\\p1") == 1 and f"\\pos({s.pos[0]},{s.pos[1]})" in ass
          and f"{S.RES_X} {S.RES_Y}" in ass, "model: render = black screen + the name at the spot")
    c.t += 10
    s.tick(False)
    check(s.on, "model: on again")
    check(s.wake() is True and not s.on, "model: a key closes it and says so")
    check(s.wake() is False, "model: a key with it off is a normal key")
    s2 = S.Saver(c, after=300, move=180)
    s2.wake()
    check(not s2.recently_woke(), "model: a key that woke nothing starts no 'held key' window")
    s.on = True
    s.wake()
    check(s.recently_woke() and not (c.__setattr__("t", c.t + 2.5) or s.recently_woke()), "model: held-key window is 2 s")


def config(tmp):
    return M.Config({"MTV_SERVER": "http://media.test:8080", "MTV_STATE": tmp, "MTV_SAVER_AFTER": "300", "MTV_SAVER_MOVE": "180"})


def overlays(mpv, ident):
    return [c[3:] for c in mpv.calls if c[0] == "cmd" and c[1] == "osd-overlay" and c[2] == ident]


async def player_part():
    tmp = tempfile.mkdtemp()
    c = Clock()
    sent = []
    p = M.Player(config(tmp), send=sent.append, mono=c)
    mpv = FakeMpv()
    await p.attach(mpv)
    p.state = {"current": {"id": 5}, "queue": [5]}
    check(M.Config({"MTV_SERVER": "x"}).saver_after == 300 and M.Config({"MTV_SERVER": "x"}).saver_move == 180,
          "defaults: 5 minutes, jump every 3 minutes")
    check(M.Config({"MTV_SERVER": "x", "MTV_SAVER_AFTER": "x"}).saver_after == 300, "garbage env -> default")

    # nothing loaded, 300 s: the screensaver
    for _ in range(3):
        c.t += 100
        await p.tick()
    ov = overlays(mpv, 3)
    check(len(ov) == 1 and ov[0][0] == "ass-events" and "MTVPlayer" in ov[0][1] and ov[0][2:4] == (1920, 1080),
          f"idle 300 s -> overlay 3 with the name ({len(ov)} draws)")
        # a key only closes it
    await M.on_key(p, "ok", False)
    check(not p.saver.on and overlays(mpv, 3)[-1][0] == "none", "a key removes the overlay")
    check(not [m for m in sent if m.get("type") == "cmd"], "... and the key did nothing else (no toggle sent)")
    await M.on_key(p, "ok", False)
    check([m for m in sent if m.get("type") == "cmd"] == [{"type": "cmd", "cmd": "toggle"}],
          "the next OK is a normal OK (the server restarts the current item after a Stop)")
    sent.clear()

    # held arrow: the repeats after the waking press do nothing
    p.item, p.want_play, p.file_loaded = {"id": 5, "duration": 100}, False, True
    c.t += 400
    await p.tick()
    check(p.saver.on, "paused for 400 s: screensaver")
    await M.on_key(p, "right", False)
    await M.on_key(p, "right", True)
    c.t += 1
    await M.on_key(p, "right", True)
    check(not [m for m in sent if m.get("type") == "cmd"], "a held key: the waking press and its repeats seek nothing")
    c.t += 3
    await M.on_key(p, "right", False)
    check([m.get("cmd") for m in sent if m.get("type") == "cmd"] == ["seek"], "a new press 3 s later is a normal press")
    sent.clear()

    # it moves every 180 s while idle
    c.t += 400
    await p.tick()
    n = len(overlays(mpv, 3))
    c.t += 179
    await p.tick()
    check(len(overlays(mpv, 3)) == n, "no redraw before 180 s")
    c.t += 2
    await p.tick()
    check(len(overlays(mpv, 3)) == n + 1 and overlays(mpv, 3)[-1][1] != overlays(mpv, 3)[-2][1], "after 180 s the text is drawn at a new spot")

    # playing removes it; a paused-then-playing player stays clear
    p.want_play = True
    await p.tick()
    check(not p.saver.on and overlays(mpv, 3)[-1][0] == "none", "playing (from the phone) removes the screensaver")
    c.t += 1000
    mpv.props["pause"] = False
    await p.tick()
    check(not p.saver.on, "playing for 1000 s: never")
    p.want_play = False
    c.t += 299
    await p.tick()
    check(not p.saver.on, "after the pause: not yet at 299 s")
    c.t += 2
    await p.tick()
    check(p.saver.on, "... and at 300 s")
    # a load that plays removes it
    await p.handle({"type": "load", "item": {"id": 6}, "position": 0, "autoplay": True})
    check(not p.saver.on and overlays(mpv, 3)[-1][0] == "none", "a load with autoplay removes it at once")
    # the menu counts as activity
    p.want_play = False
    p.item = None
    await p.menu.show()
    c.t += 1000
    await p.tick()
    check(not p.saver.on, "menu open: no screensaver")
    p.menu.close()
    # mpv restart: drawn again
    c.t += 400
    await p.tick()
    check(p.saver.on, "idle again: on")
    n = len(overlays(mpv, 3))
    mpv2 = FakeMpv()
    p.detach()
    await p.attach(mpv2)
    check(len(overlays(mpv2, 3)) == 1 and "MTVPlayer" in overlays(mpv2, 3)[0][1], "a restarted mpv gets the screensaver again")

    # Stop key
    sent.clear()
    await M.on_key(p, "ok", False)         # wakes it
    p.item = {"id": 7}
    await p.menu.show()
    await M.on_key(p, "stop", False)
    check({"type": "cmd", "cmd": "stop"} in sent and not p.menu.open, "Stop key: stop sent, the menu closes")
    sent.clear()
    p.item = None
    await M.on_key(p, "stop", False)
    check(not [m for m in sent if m.get("cmd") == "stop"], "Stop with nothing loaded: nothing sent")
    # control stop
    p.item, p.want_play, p.pos = {"id": 8}, True, 55.0
    await p.handle({"type": "control", "action": "stop"})
    check(p.item is None and not p.want_play and p.pos == 0.0 and mpv2.cmds("stop"), "control stop: nothing loaded, mpv stopped, position 0")


async def main():
    model()
    await player_part()


if __name__ == "__main__":
    asyncio.run(main())
    print("ALL PASSED" if not failed else f"FAILED {failed}")
    sys.exit(1 if failed else 0)
