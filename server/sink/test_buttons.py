#!/usr/bin/env python3
"""Unit tests for atv_buttons.py's seek keys (KEY_FASTFORWARD / KEY_REWIND from BlueZ's "(AVRCP)" input device) and
the sink's seek relay. dbus is replaced by a stub, so this runs without python3-dbus. Run: python3 test_buttons.py"""
import io
import json
import os
import sys
import types
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# --- dbus stub: just enough for the module to import (decorators that return the function, a base class)
dbus = types.ModuleType("dbus")
service = types.ModuleType("dbus.service")
mainloop = types.ModuleType("dbus.mainloop")
glibmod = types.ModuleType("dbus.mainloop.glib")
service.Object = object
service.method = lambda *a, **k: (lambda f: f)
service.signal = lambda *a, **k: (lambda f: f)
dbus.service, dbus.mainloop, mainloop.glib = service, mainloop, glibmod
dbus.PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
sys.modules.update({"dbus": dbus, "dbus.service": service, "dbus.mainloop": mainloop, "dbus.mainloop.glib": glibmod})

import atv_buttons as B  # noqa: E402
import atv_audio as A  # noqa: E402
import test_sync as T  # noqa: E402


def outputs(fn):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


class SeekKeysTest(unittest.TestCase):
    def setUp(self):
        self.t = 100.0
        B.time.monotonic = lambda: self.t
        self.k = B.SeekKeys()

    def test_press_seeks_once(self):
        out = outputs(lambda: (self.k.key(B.KEY_FASTFORWARD, 1), self.k.key(B.KEY_FASTFORWARD, 0)))
        self.assertEqual(out, [{"cmd": "seek", "delta": 10.0}])

    def test_rewind_negative(self):
        self.assertEqual(outputs(lambda: self.k.key(B.KEY_REWIND, 1)), [{"cmd": "seek", "delta": -10.0}])

    def test_hold_steps_every_0_6_s(self):
        def hold():
            self.k.key(B.KEY_FASTFORWARD, 1)
            for _ in range(60):            # kernel autorepeat ~30/s for 2 s
                self.t += 1 / 30
                self.k.key(B.KEY_FASTFORWARD, 2)
            self.k.key(B.KEY_FASTFORWARD, 0)
        out = outputs(hold)
        self.assertEqual(len(out), 1 + 3, out)    # press + one step per 0.6 s of 2 s
        self.assertTrue(all(o == {"cmd": "seek", "delta": 10.0} for o in out))

    def test_other_keys_ignored(self):
        self.assertEqual(outputs(lambda: [self.k.key(c, 1) for c in (200, 201, 163, 165, 115)]), [])

    def test_read_parses_input_events(self):
        r, w = os.pipe()
        os.set_blocking(r, False)
        os.write(w, B.EVENT.pack(0, 0, 4, 4, 9) + B.EVENT.pack(0, 0, B.EV_KEY, B.KEY_REWIND, 1)
                 + B.EVENT.pack(0, 0, 0, 0, 0) + B.EVENT.pack(0, 0, B.EV_KEY, B.KEY_REWIND, 0))
        res = []
        out = outputs(lambda: res.append(self.k.read(r)))
        self.assertEqual(out, [{"cmd": "seek", "delta": -10.0}])
        self.assertTrue(res[0], "still open")
        os.close(w)
        self.assertFalse(self.k.read(r), "EOF = device gone")
        os.close(r)


class SinkSeekRelayTest(unittest.IsolatedAsyncioTestCase):
    async def test_seek_goes_to_the_owner(self):
        sink, clock, _ = T.make_sink()
        router = A.Router(sink)
        box = A.BoxDriver(sink, router, [].append, clock)
        router.box = box
        sent = []
        b = A.Buttons(router, sent.append)
        b.on_seek(10.0)
        self.assertEqual(sent[-1], {"type": "button", "cmd": "seek", "delta": 10.0, "session": "box"})
        await router.from_tv({"type": "route", "sink": True})
        await router.from_tv({"type": "load", "item": {"id": 5, "kind": "video"}, "position": 0, "autoplay": True})
        b.on_seek(-10.0)
        self.assertEqual(sent[-1]["session"], "tv")


if __name__ == "__main__":
    unittest.main(verbosity=1)
