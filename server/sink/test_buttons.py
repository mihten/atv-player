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


class DeviceListTest(unittest.TestCase):
    def test_audio_detection(self):
        self.assertTrue(B.is_audio({"UUIDs": ["0000110b-0000-1000-8000-00805f9b34fb"]}))
        self.assertTrue(B.is_audio({"Class": 0x240404}))           # major class audio/video
        self.assertFalse(B.is_audio({"UUIDs": ["00001124-0000-1000-8000-00805f9b34fb"], "Class": 0x2540}))  # keyboard

    def test_which_devices_show(self):
        hp = {"Address": "AA", "Alias": "Headphones X", "Paired": True, "Connected": True, "UUIDs": ["0000110b-x"]}
        new = {"Address": "BB", "Name": "Speaker", "Paired": False, "UUIDs": ["0000110b-x"]}
        kb = {"Address": "CC", "Name": "Keyboard", "Paired": False, "Class": 0x2540}
        self.assertEqual(B.device_view(hp, False), {"address": "AA", "name": "Headphones X", "paired": True,
                                                    "connected": True, "audio": True})
        self.assertIsNone(B.device_view(new, False), "unpaired: only while scanning")
        self.assertEqual(B.device_view(new, True)["name"], "Speaker")
        self.assertIsNone(B.device_view(kb, True), "not audio: never offered for pairing")
        self.assertIsNone(B.device_view({"Address": "DD", "Paired": False, "UUIDs": ["0000110b"]}, True),
                          "no name yet: hidden")


class FakeOutputs:
    has_pactl = True

    def __init__(self):
        self.sinks = [("auto_null", "Dummy", {}, {}),
                      ("bluez_output.AA.1", "Headphones X", {"device.api": "bluez5", "api.bluez5.codec": "aptx_hd"}, {}),
                      ("alsa_output.usb", "USB DAC", {"device.bus": "usb"}, {})]
        self.default = "bluez_output.AA.1"
        self.set_calls = []

    async def set_default(self, key):
        self.set_calls.append(key)
        if key not in {s[0] for s in self.sinks}:
            return False
        self.default = key
        return True


class FakeButtons:
    def __init__(self, running=True):
        self.calls, self.running = [], running

    def bt(self, action, address=None):
        self.calls.append((action, address))
        return self.running


class AudioControlTest(unittest.IsolatedAsyncioTestCase):
    async def test_outputs_and_switch(self):
        sent, outs = [], FakeOutputs()
        redetect = []

        async def again():
            redetect.append(1)
        a = A.AudioControl(outs, sent.append, FakeButtons(), again)
        a.update()
        info = sent[-1]
        self.assertEqual([o["key"] for o in info["outputs"]], ["bluez_output.AA.1", "alsa_output.usb"], "no dummy")
        self.assertEqual(info["outputs"][0], {"key": "bluez_output.AA.1", "name": "Headphones X", "kind": "bluetooth",
                                              "codec": "aptx_hd", "default": True})
        self.assertFalse(info["can_bluetooth"], "no helper answer yet")
        n = len(sent)
        a.update()
        self.assertEqual(len(sent), n, "unchanged: not sent again")
        await a.handle({"type": "audio_cmd", "op": "set_output", "output": "alsa_output.usb"})
        self.assertEqual(outs.default, "alsa_output.usb")
        self.assertTrue(redetect and sent[-1]["outputs"][1]["default"])
        await a.handle({"type": "audio_cmd", "op": "set_output", "output": "evil; rm"})
        self.assertIn("not available", sent[-1]["error"])

    async def test_bluetooth_ops(self):
        sent, b = [], FakeButtons()
        a = A.AudioControl(FakeOutputs(), sent.append, b)
        await a.handle({"type": "audio_cmd", "op": "connect", "address": "AA"})
        self.assertEqual(b.calls[-1], ("connect", "AA"))
        a.on_bt({"devices": [{"address": "AA", "name": "M4", "paired": True, "connected": True, "audio": True}],
                 "scanning": False, "busy": None, "error": None})
        self.assertTrue(sent[-1]["can_bluetooth"])
        self.assertEqual(sent[-1]["devices"][0]["name"], "M4")
        a.on_bt(None)
        self.assertFalse(sent[-1]["can_bluetooth"], "helper gone")
        dead = A.AudioControl(FakeOutputs(), sent.append, FakeButtons(running=False))
        await dead.handle({"type": "audio_cmd", "op": "scan"})
        self.assertIn("not available", sent[-1]["error"])

    async def test_router_passes_audio_cmd(self):
        sink, clock, _ = T.make_sink()
        router = A.Router(sink)
        got = []

        class Audio(A.AudioControl):
            def __init__(self):
                pass

            async def handle(self, msg):
                got.append(msg)
        router.audio = Audio()
        await router.from_tv({"type": "audio_cmd", "op": "refresh"})
        self.assertEqual(got, [], "runs in its own task, not inside the message loop")
        await router.audio._last
        self.assertEqual(got, [{"type": "audio_cmd", "op": "refresh"}])
        self.assertIsNone(sink.item, "not treated as a TV message")


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
