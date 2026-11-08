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


if __name__ == "__main__":
    unit()
    asyncio.run(keys())
    print("ALL PASSED" if not failed else f"FAILED {failed}")
    sys.exit(1 if failed else 0)
