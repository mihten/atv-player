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


class _DBusException(Exception):
    def get_dbus_name(self):
        return "org.bluez.Error.Failed"

    def get_dbus_message(self):
        return str(self)


dbus.DBusException = _DBusException
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
        self.codec_calls = []
        self.offered = [{"codec": "aptx_hd", "label": "aptX HD", "profile": "a2dp-sink"},
                        {"codec": "aptx_adaptive", "label": "aptX Adaptive", "profile": "a2dp-sink-aptx_adaptive"}]

    async def codec_choices(self):
        return ("bluez_card.AA", list(self.offered)) if self.offered else None

    async def set_codec(self, codec):
        self.codec_calls.append(codec)
        return any(c["codec"] == codec for c in self.offered)

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



DEV = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"


def bluez(connected=True, blocked=False, uuids=(B.A2DP_SINK,), transport=False, other_transport=False):
    objs = {DEV: {"org.bluez.Device1": {"Connected": connected, "Blocked": blocked, "UUIDs": list(uuids)}}}
    if transport:
        objs[DEV + "/fd0"] = {"org.bluez.MediaTransport1": {}}
    if other_transport:
        objs["/org/bluez/hci0/dev_AA_BB_CC_DD_EE_F0/fd1"] = {"org.bluez.MediaTransport1": {}}
    return objs


class A2dpWatchTest(unittest.TestCase):
    """Headphones connected without an A2DP transport for 10 s -> ConnectProfile(A2DP Sink) once."""

    def setUp(self):
        self.objs = bluez()
        self.timers = []          # (ms, fn)
        self.calls = []           # (uuid, reply, error)
        test = self

        class Dev:
            def ConnectProfile(self, uuid, reply_handler, error_handler, timeout):
                test.calls.append((uuid, reply_handler, error_handler))

        class Bus:
            def get_object(self, name, path):
                test.assertEqual(path, DEV)
                return object()

        dbus.Interface = lambda obj, iface: Dev()
        self.w = B.A2dpWatch(Bus(), lambda ms, fn: self.timers.append((ms, fn)), lambda: self.objs)

    def run_timers(self):
        timers, self.timers = self.timers, []
        out = []
        for ms, fn in timers:
            self.assertEqual(ms, 10000)
            out += outputs(fn)
        return out

    def test_missing_a2dp(self):
        self.assertTrue(B.missing_a2dp(DEV, bluez()))
        self.assertTrue(B.missing_a2dp(DEV, bluez(other_transport=True)))   # another device's transport
        self.assertFalse(B.missing_a2dp(DEV, bluez(transport=True)))
        self.assertFalse(B.missing_a2dp(DEV, bluez(connected=False)))
        self.assertFalse(B.missing_a2dp(DEV, bluez(blocked=True)))         # standby program: refused on purpose
        self.assertFalse(B.missing_a2dp(DEV, bluez(uuids=("0000110e-0000-1000-8000-00805f9b34fb",))))
        self.assertFalse(B.missing_a2dp("/org/bluez/hci0/dev_00", bluez()))

    def test_asks_once_and_reports_success(self):
        self.w.connected(DEV)
        self.assertEqual(self.calls, [])                       # nothing before 10 s
        out = self.run_timers()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], B.A2DP_SINK)
        self.assertIn("without A2DP", out[0]["log"])
        self.calls[0][1]()                                     # ConnectProfile returned
        self.objs = bluez(transport=True)
        out = self.run_timers()
        self.assertEqual(out, [{"log": "a2dp watchdog: dev_AA_BB_CC_DD_EE_FF A2DP is up"}])
        self.assertEqual(len(self.calls), 1)                   # no second attempt

    def test_still_missing_is_logged_not_retried(self):
        self.w.connected(DEV)
        self.run_timers()
        self.calls[0][1]()
        out = self.run_timers()
        self.assertIn("still without A2DP", out[0]["log"])
        self.assertEqual(self.timers, [])
        self.assertEqual(len(self.calls), 1)

    def test_failure_is_logged(self):
        self.w.connected(DEV)
        self.run_timers()
        out = outputs(lambda: self.calls[0][2](_DBusException("Host is down")))
        self.assertIn("ConnectProfile failed: org.bluez.Error.Failed Host is down", out[0]["log"])
        self.assertEqual(self.timers, [])

    def test_transport_present_does_nothing(self):
        self.objs = bluez(transport=True)
        self.w.connected(DEV)
        self.assertEqual(self.run_timers(), [])
        self.assertEqual(self.calls, [])

    def test_disconnect_cancels(self):
        self.w.connected(DEV)
        self.w.disconnected(DEV)
        self.assertEqual(self.run_timers(), [])
        self.assertEqual(self.calls, [])

    def test_reconnect_only_newest_check_acts(self):
        self.w.connected(DEV)
        self.w.disconnected(DEV)
        self.w.connected(DEV)
        self.run_timers()
        self.assertEqual(len(self.calls), 1)

    def test_start_checks_already_connected(self):
        self.w.start()
        self.run_timers()
        self.assertEqual(len(self.calls), 1)
        self.objs = bluez(connected=False)
        self.calls.clear()
        self.w.start()
        self.assertEqual(self.timers, [])


class FakeDevice:
    """One BlueZ device object: Device1 (Connect) + Properties (Get / Set Blocked)."""

    def __init__(self, blocked=False):
        self.blocked, self.calls = blocked, []

    def GetManagedObjects(self):
        return {"/org/bluez/hci0": {"org.bluez.Adapter1": {}},
                "/org/bluez/hci0/dev_AA": {"org.bluez.Device1": {"Address": "AA", "Paired": True, "Connected": True}}}

    def Get(self, iface, name):
        return {"Blocked": self.blocked, "Alias": "Headphones X"}[name]

    def Set(self, iface, name, value):
        assert name == "Blocked"
        self.blocked = bool(value)
        self.calls.append(("blocked", self.blocked))

    def Connect(self, reply_handler=None, error_handler=None, timeout=None):
        self.calls.append(("connect", self.blocked))
        reply_handler()

    def Disconnect(self, reply_handler=None, error_handler=None, timeout=None):
        self.calls.append(("disconnect", self.blocked))
        reply_handler()


class FakeBus:
    def __init__(self, dev):
        self.dev = dev

    def get_object(self, service, path):
        return self.dev


def _card(name="bluez_card.AA_BB", active="a2dp-sink-aptx_adaptive", **profiles):
    """'pactl -f json list cards' shape (profiles: name -> description), as seen on the live box 2026-11-12."""
    base = {"off": "Off", "a2dp-sink-sbc": "High Fidelity Playback (A2DP Sink, codec SBC)",
            "a2dp-sink-sbc_xq": "High Fidelity Playback (A2DP Sink, codec SBC-XQ)",
            "a2dp-sink-aptx_adaptive_ll": "High Fidelity Playback (A2DP Sink, codec aptX Adaptive (Low Latency))",
            "a2dp-sink-aptx": "High Fidelity Playback (A2DP Sink, codec aptX)",
            "a2dp-sink": "High Fidelity Playback (A2DP Sink, codec aptX HD)",
            "a2dp-sink-aptx_adaptive": "High Fidelity Playback (A2DP Sink, codec aptX Adaptive)"}
    base.update(profiles)
    return {"name": name, "active_profile": active,
            "profiles": {k: ({"description": v, "available": True} if isinstance(v, str) else v) for k, v in base.items()}}


class CodecChoicesTest(unittest.IsolatedAsyncioTestCase):
    def test_hd_and_adaptive_found_by_description(self):
        card, ch = A.parse_card_codecs(json.dumps([{"name": "alsa_card.pci", "profiles": {}}, _card()]))
        self.assertEqual(card, "bluez_card.AA_BB")
        self.assertEqual(ch, [{"codec": "aptx_hd", "label": "aptX HD", "profile": "a2dp-sink"},
                              {"codec": "aptx_adaptive", "label": "aptX Adaptive", "profile": "a2dp-sink-aptx_adaptive"}],
                         "plain a2dp-sink = aptX HD by its description; LL, aptX, SBC, SBC-XQ and Off are not offered")

    def test_unavailable_or_missing_codec_not_offered(self):
        _, ch = A.parse_card_codecs(json.dumps([_card(**{"a2dp-sink-aptx_adaptive": {"description": "x (A2DP Sink, codec aptX Adaptive)", "available": False}})]))
        self.assertEqual([c["codec"] for c in ch], ["aptx_hd"], "an unavailable profile is not offered")
        # the plain a2dp-sink is the BEST codec, not always aptX HD: here it is LDAC -> no aptX HD choice
        _, ch = A.parse_card_codecs(json.dumps([_card(**{"a2dp-sink": "High Fidelity Playback (A2DP Sink, codec LDAC)"})]))
        self.assertEqual([c["codec"] for c in ch], ["aptx_adaptive"])

    def test_no_bluetooth_card_or_bad_json(self):
        self.assertIsNone(A.parse_card_codecs(json.dumps([{"name": "alsa_card.pci", "profiles": {}}])))
        self.assertIsNone(A.parse_card_codecs("not json"))
        self.assertIsNone(A.parse_card_codecs(""))
        self.assertIsNone(A.parse_card_codecs("{}"))

    async def test_set_codec_uses_the_cards_own_profile_name(self):
        calls = []

        async def fake_run(*args, **kw):
            calls.append(args)
            return json.dumps([_card()]) if args[-3:] == ("-f", "json", "list", "cards")[-3:] or "cards" in args else ""
        old = A._run
        A._run = fake_run
        try:
            w = A.OutputWatch.__new__(A.OutputWatch)
            w.cfg = types.SimpleNamespace(output="pipewire")
            A.shutil.which = lambda n: "/usr/bin/pactl"
            self.assertTrue(await w.set_codec("aptx_hd"))
            self.assertEqual(calls[-1][-3:], ("set-card-profile", "bluez_card.AA_BB", "a2dp-sink"))
            self.assertTrue(await w.set_codec("aptx_adaptive"))
            self.assertEqual(calls[-1][-1], "a2dp-sink-aptx_adaptive")
            n = len(calls)
            for bad in ("sbc", "a2dp-sink; reboot", "", "off"):
                self.assertFalse(await w.set_codec(bad), bad)
            self.assertFalse([c for c in calls[n:] if "set-card-profile" in c], "nothing but the two codecs ever reaches pactl")
        finally:
            A._run = old

    async def test_audio_control_set_codec_and_info(self):
        sent, outs, again = [], FakeOutputs(), []

        async def redetect():
            again.append(1)
        a = A.AudioControl(outs, sent.append, FakeButtons(), redetect)
        await a.handle({"type": "audio_cmd", "op": "refresh"})
        self.assertEqual(sent[-1]["codecs"], [{"codec": "aptx_hd", "label": "aptX HD"}, {"codec": "aptx_adaptive", "label": "aptX Adaptive"}],
                         "the choices (without the profile name) go to the server in audio_info")
        await a.handle({"type": "audio_cmd", "op": "set_codec", "output": "aptx_adaptive"})
        self.assertEqual(outs.codec_calls, ["aptx_adaptive"])
        self.assertTrue(again, "output re-detected after the switch")
        self.assertIsNone(sent[-1]["error"])
        await a.handle({"type": "audio_cmd", "op": "set_codec", "output": "ldac"})
        self.assertIn("do not offer", sent[-1]["error"])
        outs.offered = []
        await a.handle({"type": "audio_cmd", "op": "refresh"})
        self.assertEqual(sent[-1]["codecs"], [], "no Bluetooth card: no choices")


class DisconnectSticksTest(unittest.TestCase):
    """Owner 2026-11-12: Disconnect in the web remote must not be undone (trusted headphones reconnect ~1.5 s after a
    plain Disconnect): Disconnect blocks, Connect / Pair unblock first."""

    def setUp(self):
        dbus.Interface = lambda obj, iface: obj
        dbus.Boolean, dbus.ObjectPath = bool, str
        self.dev = FakeDevice()
        self.h = B.Headphones(FakeBus(self.dev), lambda ms, fn: None)

    def test_disconnect_blocks_and_connect_unblocks(self):
        with redirect_stdout(io.StringIO()):
            self.h.do_disconnect("AA")
            self.assertTrue(self.dev.blocked, "disconnect = blocked (no auto reconnect)")
            self.assertNotIn(("disconnect", False), self.dev.calls, "not a plain Disconnect")
            self.h.do_connect("AA")
        self.assertFalse(self.dev.blocked)
        self.assertEqual(self.dev.calls[-1], ("connect", False), "unblocked BEFORE connecting")


class UserOffTest(unittest.IsolatedAsyncioTestCase):
    def test_remembered_across_restart(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            u = A.UserOff(d)
            u.on_op("disconnect", "aa:bb")
            self.assertIn("AA:BB", u)
            self.assertIn("AA:BB", A.UserOff(d), "a sink restart remembers it")
            u.on_op("scan", "AA:BB")
            self.assertIn("AA:BB", A.UserOff(d))
            for op in ("connect", "pair", "forget"):
                u.on_op("disconnect", "AA:BB")
                u.on_op(op, "AA:BB")
                self.assertNotIn("AA:BB", A.UserOff(d), f"{op} clears it")
            self.assertNotIn(None, A.UserOff(None))

    async def test_web_remote_ops_reach_it(self):
        sent, b, seen = [], FakeButtons(), []
        a = A.AudioControl(FakeOutputs(), sent.append, b)
        a.on_bt_op = lambda op, addr: seen.append((op, addr))
        await a.handle({"type": "audio_cmd", "op": "disconnect", "address": "AA"})
        self.assertEqual(seen, [("disconnect", "AA")])
        self.assertEqual(b.calls[-1], ("disconnect", "AA"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
