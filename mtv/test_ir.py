#!/usr/bin/env python3
"""ir.py + the key actions of mtv_player, without hardware: synthesized MODE2 sample streams (split anywhere), NEC
and NEC-extended decode, repeats, hold timing, unknown remotes, key -> server command and on-screen text.
    python3 test_ir.py
"""
import asyncio
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ir  # noqa: E402
import mtv_player as M  # noqa: E402

failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def nec_frame(addr16, cmd, ext=True):
    a, ai = (addr16 & 0xFF, addr16 >> 8) if ext else (addr16, addr16 ^ 0xFF)
    d = [9000, 4500]
    for b in (a, ai, cmd, cmd ^ 0xFF):
        for i in range(8):
            d += [560, 1690 if (b >> i) & 1 else 560]
    return d + [560]


REPEAT = [9000, 2250, 560]


def words(frame, gap=40000):
    out = b""
    for i, v in enumerate(frame):
        out += struct.pack("I", (ir.PULSE if i % 2 == 0 else ir.SPACE) | v)
    return out + struct.pack("I", ir.SPACE | gap)


def unit():
    check(ir.decode_nec(nec_frame(0xE386, 0x0E)) == ("key", 0xE386, 0x0E), "NEC-ext E386 / 0x0e decoded")
    check(ir.decode_nec(nec_frame(0x04, 0x08, ext=False)) == ("key", 0x04, 0x08), "plain NEC 8-bit address")
    check(ir.decode_nec(REPEAT) == ("repeat",), "repeat frame")
    bad = nec_frame(0xE386, 0x0E)
    bad[2 + 2 * 16 + 1] = 1690 if bad[2 + 2 * 16 + 1] == 560 else 560
    check(ir.decode_nec(bad) is None, "command check failure -> None")
    check(ir.decode_nec([2666, 889, 444, 444, 444]) is None and ir.decode_nec([889, 889, 889]) is None,
          "RC6 / RC5 frames -> None")
    jit = [int(v * 1.12) for v in nec_frame(0xE386, 0x40)]
    check(ir.decode_nec(jit) == ("key", 0xE386, 0x40), "12 % slow timing still decodes")

    m = ir.Mode2()
    stream = words(nec_frame(0xE386, 0x06)) + words(REPEAT) + struct.pack("I", ir.TIMEOUT | 0)
    frames = []
    for i in range(0, len(stream), 7):  # split mid-word
        frames += m.feed(stream[i:i + 7])
    check(len(frames) == 2 and ir.decode_nec(frames[0]) == ("key", 0xE386, 0x06) and ir.decode_nec(frames[1]) == ("repeat",),
          f"MODE2 stream split every 7 bytes -> 2 frames ({len(frames)})")

    check(ir.parse_keys('{"0x0e": "ok", "64": "up"}') == {14: "ok", 64: "up"}, "MTV_IR_KEYS parsed (hex + decimal)")
    check(ir.parse_keys("nope") is None and ir.parse_keys("") is None, "bad MTV_IR_KEYS -> default map")

    # ---- press / repeat / hold
    t = [0.0]
    got, unknown = [], []
    k = ir.Keys(lambda n, h: got.append((n, h, round(t[0], 2))), mono=lambda: t[0],
                on_unknown=lambda a, c: unknown.append((a, c)))
    k.frame(nec_frame(0xE386, 0x0E))
    t[0] += 0.05
    k.frame(REPEAT)  # the remote's single repeat after every press
    check(got == [("ok", False, 0.0)], f"OK press + its repeat -> one action ({got})")
    got.clear()
    t[0] = 10.0
    k.frame(nec_frame(0xE386, 0x06))
    while t[0] < 11.2:  # right held 1.2 s, repeats every 108 ms
        t[0] = round(t[0] + 0.108, 3)
        k.frame(REPEAT)
    holds = [g for g in got if g[1]]
    check(got[0] == ("right", False, 10.0) and 3 <= len(holds) <= 4 and holds[0][2] >= 10.5,
          f"right held 1.2 s: press + {len(holds)} repeats from 0.5 s ({got})")
    got.clear()
    t[0] = 20.0
    k.frame(nec_frame(0xE386, 0x00))
    for _ in range(10):
        t[0] += 0.108
        k.frame(REPEAT)
    check(got == [("ch_up", False, 20.0)], "CH+ held: acts once (not a hold key)")
    got.clear()
    t[0] = 30.0
    k.frame(REPEAT)
    check(got == [], "a stray repeat with no recent press does nothing")
    k.frame(nec_frame(0xE386, 0x07))  # left (a hold key), then a repeat 5 s later = not a hold
    got.clear()
    t[0] = 35.0
    k.frame(REPEAT)
    check(got == [], "a repeat 5 s after a hold-key press does nothing")
    k.frame(nec_frame(0x0707, 0x0E))  # a command number we map, from another remote
    check(got == [] and unknown == [(0x0707, 0x0E)], "another remote's key (same command number) -> unknown")
    t[0] += 0.1
    k.frame(REPEAT)
    check(got == [], "... and its repeats do nothing")
    t[0] += 1.0
    k.frame(nec_frame(0xE386, 0x99))
    check(unknown[-1] == (0xE386, 0x99) and got == [], "our address, unmapped command -> unknown")
    k2 = ir.Keys(lambda n, h: got.append(n), address=0x04, keys={0x08: "ok"}, mono=lambda: t[0])
    k2.frame(nec_frame(0x04, 0x08, ext=False))
    check(got == ["ok"], "custom address + key map")


# Real Magic Remote timings (CT log 2026-11-08 11:00:53-11:01:09, MTV_IR_DEBUG): F = OK frame, R = repeat.
SHORT = [(36040.768, "F"), (36040.820, "R")]
HOLD1 = [(36046.271, "F"), (36046.320, "R"), (36046.767, "F"), (36046.822, "R"), (36046.931, "R"), (36047.035, "R"),
         (36047.144, "R"), (36047.364, "F"), (36047.412, "R"), (36047.620, "F"), (36047.675, "R"), (36047.885, "F"),
         (36047.933, "R"), (36048.140, "F"), (36048.193, "R"), (36048.401, "F"), (36048.452, "R")]
HOLD3 = [(36052.489, "F"), (36052.541, "R"), (36052.989, "F")] + \
    [(round(36053.039 + 0.1085 * i, 3), "R") for i in range(25)] + \
    [(36056.002, "F"), (36056.053, "R"), (36056.264, "F"), (36056.313, "R"), (36056.522, "F"), (36056.573, "R")]


class Clock:
    """Fake time + call_later: due callbacks run before each signal (as the event loop would)."""

    def __init__(self):
        self.t, self.jobs = 0.0, []

    def later(self, delay, fn):
        self.jobs.append((self.t + delay, fn))

    def advance(self, to):
        for due, fn in sorted(self.jobs, key=lambda j: j[0]):
            if due <= to:
                self.t = due
                self.jobs.remove((due, fn))
                fn()
        self.t = to


def play(keys, clock, seq, cmd=0x0E, shift=0.0):
    for t, kind in seq:
        clock.advance(t + shift)
        keys.frame(nec_frame(0xE386, cmd) if kind == "F" else REPEAT)
    clock.advance(seq[-1][0] + shift + 2.0)


def real_holds():
    clock = Clock()
    got = []
    k = ir.Keys(lambda n, h: got.append((n, h, round(clock.t, 3))), mono=lambda: clock.t, later=clock.later)
    play(k, clock, SHORT)
    check([g[0] for g in got] == ["ok"], f"real short OK -> one press ({got})")
    got.clear()
    play(k, clock, HOLD1)
    check([g[:2] for g in got] == [("ok", False)], f"real 1 s OK hold + release burst -> one press ({got})")
    got.clear()
    play(k, clock, HOLD3)
    check([g[:2] for g in got] == [("ok", False)], f"real 3 s OK hold + release burst -> one press ({got})")
    # the same 3 s hold on Right: press + repeats while held, nothing after the last continuous repeat
    got.clear()
    play(k, clock, HOLD3, cmd=0x06, shift=100)
    holds = [g for g in got if g[1]]
    last_r = 36053.039 + 0.1085 * 24 + 100
    check(got[0][:2] == ("right", False) and 8 <= len(holds) <= 11 and all(g[2] <= last_r + 0.001 for g in holds)
          and len([g for g in got if not g[1]]) == 1, f"real 3 s Right hold: 1 press + {len(holds)} repeats, none after release")
    # fast deliberate taps 0.35 s apart (as in the menu test): each acts at once
    got.clear()
    base = 300.0
    for i in range(4):
        play(k, clock, [(base + 0.35 * i, "F"), (base + 0.35 * i + 0.05, "R")], cmd=0x41)
        clock.t = base + 0.35 * i + 0.06  # play() jumped 2 s ahead: back to the tap rhythm for the next one
    check([g[0] for g in got] == ["down"] * 4, f"4 taps 0.35 s apart -> 4 presses ({len(got)})")
    # taps 0.5 s apart (the ambiguous spacing): the second waits HOLD_CONFIRM, then counts
    got.clear()
    clock = Clock()
    k = ir.Keys(lambda n, h: got.append((n, round(clock.t, 3))), mono=lambda: clock.t, later=clock.later)
    for t, kind in [(10.0, "F"), (10.05, "R"), (10.5, "F"), (10.55, "R")]:
        clock.advance(t)
        k.frame(nec_frame(0xE386, 0x41) if kind == "F" else REPEAT)
    check(got == [("down", 10.0)], "second tap at +0.5 s: not yet (waiting for repeats)")
    clock.advance(11.0)
    check(got == [("down", 10.0), ("down", 10.7)], f"... counted 0.2 s later ({got})")
    # third tap while the second waits: both count, in order
    got.clear()
    for t, kind in [(20.0, "F"), (20.05, "R"), (20.5, "F"), (20.55, "R"), (20.62, "F")]:
        clock.advance(t)
        k.frame(nec_frame(0xE386, 0x41) if kind == "F" else REPEAT)
    check([g[0] for g in got] == ["down"] * 3, f"tap during a pending tap: all 3 count ({got})")
    # no scheduler: the waiting tap is decided by the next signal
    got.clear()
    k2 = ir.Keys(lambda n, h: got.append(n), mono=lambda: clock.t)
    for t, kind, c in [(30.0, "F", 0x41), (30.05, "R", 0), (30.5, "F", 0x41), (30.55, "R", 0), (32.0, "F", 0x0E)]:
        clock.t = t
        k2.frame(nec_frame(0xE386, c) if kind == "F" else REPEAT)
    check(got == ["down", "down", "ok"], f"without later(): pending tap counted when the next key comes ({got})")


class FakeMpv:
    def __init__(self):
        self.props = {"duration": 200.0, "pause": False}
        self.texts = []
        self.overlays = []

    async def command(self, *args, timeout=5.0):
        if args[0] == "show-text":
            self.texts.append(args[1])
        elif args[0] == "osd-overlay":
            self.overlays.append(args[2])


async def keys():
    sent = []
    cfg = M.Config({"MTV_SERVER": "http://x"})
    p = M.Player(cfg, send=sent.append)
    p.mpv = FakeMpv()
    await M.on_key(p, "ok", False)
    check(not [m for m in sent if m.get("type") == "cmd"] and p.mpv.texts[-1] == "Nothing to play",
          "OK with nothing loaded: no command, text shown")
    p.item, p.file_loaded, p.want_play, p.pos = {"id": 5, "duration": 200}, True, True, 50.0
    await M.on_key(p, "ok", False)
    check(sent[-2:] == [{"type": "cmd", "cmd": "toggle"}, {"type": "log", "message": "mtv: key ok"}]
          and p.mpv.texts[-1] == "Pause" and not p.menu.open, "OK while playing -> toggle, 'Pause', no menu")
    await M.on_key(p, "blue", False)
    check(p.menu.open and p.mpv.overlays == ["ass-events"] and sent[-1] == {"type": "log", "message": "mtv: menu opened"},
          "Blue (menu closed) opens the menu, sends no command")
    await M.on_key(p, "right", False)
    check(not [m for m in sent if m.get("cmd") == "seek"], "menu open: Right is the menu's, no seek")
    await M.on_key(p, "red", False)
    check(not p.menu.open and p.mpv.overlays[-1] == "none", "Red closes the menu, overlay removed")
    await M.on_key(p, "blue", False)
    await M.on_key(p, "blue", False)
    check(not p.menu.open and p.mpv.overlays[-2:] == ["ass-events", "none"], "Blue opens and Blue closes the menu")
    await M.on_key(p, "ch_up", False)
    await M.on_key(p, "ch_down", False)
    cmds = [m["cmd"] for m in sent if m.get("type") == "cmd"]
    check(cmds[-2:] == ["next", "prev"], "CH+ / CH- -> next / prev")
    await M.on_key(p, "right", False)
    await M.on_key(p, "right", True)
    seeks = [m["position"] for m in sent if m.get("cmd") == "seek"]
    check(seeks == [60.0, 70.0] and p.mpv.texts[-1] == "+10 s   1:10 / 3:20", f"right twice adds up ({seeks})")
    held_logs = [m for m in sent if m.get("message") == "mtv: key right"]
    check(len(held_logs) == 1, "held repeats are not logged")
    await M.on_key(p, "down", False)
    await M.on_key(p, "down", False)
    check([m["position"] for m in sent if m.get("cmd") == "seek"][-2:] == [10.0, 0.0], "down -60 twice, clamped at 0")
    p.pos = 190.0
    await M.on_key(p, "up", False)
    check([m["position"] for m in sent if m.get("cmd") == "seek"][-1] == 198.0, "up near the end stops 2 s before it")
    n = len(sent)
    await M.on_key(p, "red", False)
    check(len(sent) == n, "Red with the menu closed: nothing sent")
    p._send = None
    await M.on_key(p, "ch_up", False)
    check(p.mpv.texts[-1] == "Not connected to the server", "no server: text instead of a command")
    check(M.Config({"MTV_IR_DEVICE": "off"}).ir_device == "" and M.Config({}).ir_device == "/dev/lirc0"
          and M.Config({"MTV_IR_ADDRESS": "0x04"}).ir_address == 4 and M.Config({}).ir_address == 0xE386,
          "IR config: off / default device / address")


def evdev():
    t = [0.0]
    got = []
    k = ir.EvdevKeys(lambda n, h: got.append((n, h, round(t[0], 2))), mono=lambda: t[0])

    def ev(code, value, etype=1):
        return ir.EVENT.pack(0, 0, etype, code, value)

    k.feed(ev(28, 1) + ev(28, 0))
    check(got == [("ok", False, 0.0)], "Enter press -> ok once")
    got.clear()
    t[0] = 5.0
    k.feed(ev(105, 1))
    for i in range(1, 40):  # kernel autorepeat of a held Left, every 33 ms after 250 ms
        t[0] = 5.0 + 0.25 + 0.033 * i
        k.feed(ev(105, 2))
    k.feed(ev(105, 0))
    holds = [g for g in got if g[1]]
    check(got[0] == ("left", False, 5.0) and 3 <= len(holds) <= 5 and holds[0][2] >= 5.5,
          f"held Left: press + {len(holds)} repeats from 0.5 s, every 0.25 s ({[g[2] for g in holds]})")
    got.clear()
    t[0] = 10.0
    k.feed(ev(104, 1))
    for i in range(1, 30):
        t[0] = 10.0 + 0.033 * i + 0.25
        k.feed(ev(104, 2))
    k.feed(ev(104, 0))
    check([g[0] for g in got] == ["ch_up"], "held PageUp (CH+): acts once")
    got.clear()
    k.feed(ev(0, 0, etype=0) + ev(59, 1) + ev(59, 0) + ev(61, 1) + ev(999, 1) + ev(999, 0))
    check([g[0] for g in got] == ["red", "yellow"], "sync events and unmapped codes ignored; F1 = red, F3 = yellow")
    got.clear()
    data = ev(108, 1) + ev(108, 0)
    for i in range(0, len(data), 5):  # split mid-event
        k.feed(data[i:i + 5])
    check([g[0] for g in got] == ["down"], "events split across reads")
    got.clear()
    k.feed(ev(2, 1) + ev(2, 0) + ev(11, 1) + ev(11, 0))
    check([g[0] for g in got] == ["1", "0"], "digits 1 and 0")
    got.clear()
    k.feed(ev(1, 1) + ev(1, 0) + ev(14, 1) + ev(14, 0))
    check([g[0] for g in got] == ["back", "back"], "Esc / Backspace = back (the Back function)")
    check(ir.EVENT.size == 24, "struct input_event is 24 bytes (64-bit)")


if __name__ == "__main__":
    unit()
    evdev()
    real_holds()
    asyncio.run(keys())
    print("ALL PASSED" if not failed else f"FAILED {failed}")
    sys.exit(1 if failed else 0)
