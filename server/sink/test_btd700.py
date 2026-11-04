#!/usr/bin/env python3
"""Unit tests for btd700.py (Sennheiser BTD 700 dongle) and its use in atv_audio.py: a fake dongle answers on a
SOCK_SEQPACKET socket pair (one packet per read, like a hidraw node). Run: python3 test_btd700.py"""
import asyncio
import os
import socket
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import btd700 as D  # noqa: E402
import atv_audio as A  # noqa: E402

D.RETRY = 0.05
D.TIMEOUT = 0.3


class FakeDongle:
    """The dongle's side of the socket pair: answers state / codec / firmware / connect commands."""

    def __init__(self, sock):
        self.sock = sock
        self.state = 2          # connected
        self.codec = 0          # mask; 0 = none in use
        self.commands = []
        self.answer = True
        sock.setblocking(False)

    def packet(self, marker, ident, data=b""):
        return (bytes([D.REPORT, marker, ident, len(data)]) + bytes(data)).ljust(D.PACKET, b"\0")

    def event(self, ident, data):
        self.sock.send(self.packet(D.DONGLE_EVT, ident, data))

    def keys(self, bits):
        self.sock.send(bytes([D.KEYS_REPORT, bits & 0xFF, bits >> 8]))

    def on_readable(self):
        try:
            data = self.sock.recv(D.PACKET)
        except BlockingIOError:
            return
        if not data or data[0] != D.REPORT or data[1] != D.HOST_CMD:
            return
        ident, n = data[2], data[3]
        args = data[4:4 + n]
        self.commands.append((ident, bytes(args)))
        if not self.answer:
            return
        if ident == D.CMD_STATE:
            out = bytes([self.state])
        elif ident == D.CMD_CODEC_IN_USE:
            out = bytes([self.codec & 0xFF, self.codec >> 8])
        elif ident == D.CMD_FIRMWARE:
            out = bytes([3, 11, 0, 0])
        elif ident == D.CMD_BT_CONNECT:
            self.state = 2 if args and args[0] else 1
            out = b""
        else:
            out = b""
        self.sock.send(self.packet(D.DONGLE_RSP, ident, out))


class Rig:
    """A Btd700 opened on one end of a socket pair, the FakeDongle on the other."""

    def __init__(self, test):
        self.test = test
        self.logs, self.keys, self.changes = [], [], 0
        self.present = True
        self.fake = None

        def opener(_path):
            if not self.present:
                raise OSError(2, "No such file or directory")
            a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            self.fake = FakeDongle(b)
            self.fake.state, self.fake.codec = self.state, self.codec
            asyncio.get_running_loop().add_reader(b.fileno(), self.fake.on_readable)
            self._keep = (a, b)
            return os.dup(a.fileno())

        self.state, self.codec = 2, 0
        self.d = D.Btd700("/dev/fake", on_change=self._changed, on_key=self.keys.append, log=self.logs.append,
                          opener=opener)

    def _changed(self):
        self.changes += 1

    async def start(self):
        self.d.first = asyncio.Event()
        self.task = asyncio.create_task(self.d.run())
        await asyncio.wait_for(self.d.first.wait(), 2)

    async def stop(self):
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass

    def unplug(self):
        self.present = False
        asyncio.get_running_loop().remove_reader(self.fake.sock.fileno())
        self.fake.sock.close()


async def until(cond, timeout=2.0):
    t = asyncio.get_running_loop().time()
    while not cond():
        if asyncio.get_running_loop().time() - t > timeout:
            raise AssertionError("condition not met")
        await asyncio.sleep(0.01)


class Btd700Test(unittest.IsolatedAsyncioTestCase):
    async def test_open_reads_firmware_state_codec(self):
        r = Rig(self)
        r.state, r.codec = 3, 0x04
        await r.start()
        self.assertEqual(r.d.firmware, "3.11.0")
        self.assertEqual(r.d.state, "streaming")
        self.assertEqual(r.d.codec, "aptx_adaptive")
        self.assertEqual(r.d.output(), {"kind": "bluetooth", "name": "BTD 700", "codec": "aptx_adaptive", "ready": True})
        await r.stop()

    async def test_state_event_marks_headphones_gone_at_once(self):
        r = Rig(self)
        r.state, r.codec = 3, 0x04
        await r.start()
        r.fake.state = 1
        r.fake.answer = False   # only the event itself may change the state (no answer to the follow-up read)
        r.fake.event(D.EVT_STATE, bytes([1]))
        await until(lambda: r.d.state == "disconnected", timeout=0.15)
        self.assertFalse(r.d.output()["ready"])
        self.assertEqual(r.d.codec, "aptx_adaptive", "the last codec is kept while the headphones are away")
        await asyncio.sleep(0.6)  # the follow-up read times out: still open, state from the event
        r.fake.answer = True
        r.fake.state, r.fake.codec = 3, 0x04
        r.fake.event(D.EVT_STATE, bytes([3]))
        await until(lambda: r.d.ready)
        await r.stop()

    async def test_codec_event_triggers_a_read(self):
        r = Rig(self)
        await r.start()
        self.assertIsNone(r.d.codec)
        r.fake.codec = 0x02
        r.fake.event(D.EVT_CODEC, bytes([0x02]))
        await until(lambda: r.d.codec == "aptx")
        await r.stop()

    async def test_unplug_reports_gone_and_reopens(self):
        r = Rig(self)
        await r.start()
        before = r.changes
        r.unplug()
        await until(lambda: r.d.state is None)
        self.assertIsNone(r.d.output())
        self.assertGreater(r.changes, before)
        r.present = True
        await until(lambda: r.d.state == "connected")
        await r.stop()

    async def test_missing_node_retries_quietly(self):
        r = Rig(self)
        r.present = False
        await r.start()
        await asyncio.sleep(0.2)
        self.assertIsNone(r.d.output())
        self.assertEqual(sum("No such file" in m for m in r.logs), 1, r.logs)
        r.present = True
        await until(lambda: r.d.present)
        await r.stop()

    async def test_keys_edges_only(self):
        r = Rig(self)
        await r.start()
        r.fake.keys(0x0001)   # play/pause down
        r.fake.keys(0x0000)   # up
        r.fake.keys(0x0002)   # next
        r.fake.keys(0x0002)   # still held: no second press
        r.fake.keys(0x0000)
        r.fake.keys(0x0100)   # volume up
        r.fake.keys(0x0000)
        await until(lambda: len(r.keys) >= 3)
        self.assertEqual(r.keys, ["playpause", "next", "volup"])
        await r.stop()

    async def test_connect_and_disconnect(self):
        r = Rig(self)
        await r.start()
        self.assertTrue(await r.d.connect(False))
        self.assertIn((D.CMD_BT_CONNECT, b"\x00"), r.fake.commands)
        await until(lambda: r.d.state == "disconnected")
        self.assertTrue(await r.d.connect(True))
        await until(lambda: r.d.state == "connected")
        await r.stop()

    async def test_no_answer_reopens(self):
        r = Rig(self)
        await r.start()
        r.fake.answer = False
        self.assertFalse(await r.d.connect(True))
        await r.stop()


class FakeRouter:
    def __init__(self, playing):
        self._playing = playing
        self.box = None
        self.owner = "tv"
        self.tv_last = {"load": {"item": {"id": 1, "title": "t", "duration": 100}}}
        self.sink = type("S", (), {"output_ok": True, "output_ready_at": None, "mono": lambda s: 1000.0,
                                   "log": lambda s, m: None})()

    def button_session(self):
        return "tv"

    def tv_playing(self):
        return self._playing


class SinkWiringTest(unittest.IsolatedAsyncioTestCase):
    def test_playpause_toggles_against_the_queue_state(self):
        for playing, want in ((True, "pause"), (False, "play")):
            sent = []
            b = A.Buttons(FakeRouter(playing), sent.append, helper=False)
            b.on_key("playpause")
            self.assertEqual(sent, [{"type": "button", "cmd": want, "session": "tv"}])

    def test_keys_map(self):
        sent = []
        b = A.Buttons(FakeRouter(True), sent.append, helper=False)
        for k in ("next", "prev", "stop", "volup", "voldown", "mute", "ff"):
            b.on_key(k)
        self.assertEqual([m["cmd"] for m in sent], ["next", "prev", "pause", "seek"])
        self.assertEqual(sent[-1]["delta"], 10.0)

    async def test_no_helper_without_bluez(self):
        b = A.Buttons(FakeRouter(True), lambda m: None, helper=False)
        await asyncio.wait_for(b.run(), 1)   # returns at once, no atv_buttons.py
        self.assertIsNone(b.proc)

    async def test_output_watch_uses_the_dongle_state(self):
        cfg = A.Config({"ATV_AUDIO_SERVER": "http://x", "ATV_AUDIO_OUTPUT": "alsa:plughw:CARD=NOSUCHCARD,DEV=0"})
        w = A.OutputWatch(cfg)
        d = D.Btd700("/dev/fake")
        d.fd, d.state, d.codec = 99, "connected", "aptx_adaptive"
        w.dongle = d
        out = await w.detect()
        self.assertEqual(out, {"kind": "bluetooth", "name": "BTD 700", "codec": "aptx_adaptive", "ready": False},
                         "card missing -> not ready even with the dongle answering")
        d.fd, d.state = None, None
        out = await w.detect()
        self.assertEqual(out["ready"], False)
        self.assertEqual(out["kind"], "other")  # plain ALSA status again

    async def test_dongle_volume_is_mpvs_and_survives_a_restart(self):
        import tempfile
        d = tempfile.mkdtemp()
        cfg = A.Config({"ATV_AUDIO_SERVER": "http://x", "ATV_AUDIO_OUTPUT": "alsa:default", "ATV_AUDIO_STATE": d})
        s = A.Sink(cfg, lambda m: None)
        s.set_output({"kind": "bluetooth", "name": "BTD 700", "codec": None, "ready": True})
        self.assertEqual((s.output["volume"], s.output["muted"]), (100, False))
        await s.on_volume({"percent": 35, "muted": False})
        self.assertEqual(s.output["volume"], 35)
        s2 = A.Sink(cfg, lambda m: None)
        s2.load_volume()
        self.assertEqual(s2.mpv_volume, (35, False), "a restarted sink keeps the volume")
        s3 = A.Sink(A.Config({"ATV_AUDIO_SERVER": "http://x", "ATV_AUDIO_OUTPUT": "alsa:default"}), lambda m: None)
        s3.load_volume()
        self.assertEqual(s3.mpv_volume, (100, False), "no state dir: default")

    def test_audio_info_carries_the_dongle(self):
        cfg = A.Config({"ATV_AUDIO_SERVER": "http://x", "ATV_AUDIO_OUTPUT": "alsa:default"})
        ac = A.AudioControl(A.OutputWatch(cfg), lambda m: None)
        d = D.Btd700("/dev/fake")
        d.fd, d.state, d.codec, d.firmware = 99, "streaming", "aptx_adaptive", "3.11.0"
        ac.dongle = d
        info = ac.info()
        self.assertEqual(info["dongle"], {"name": "BTD 700", "state": "streaming", "codec": "aptx_adaptive",
                                          "firmware": "3.11.0"})
        self.assertFalse(info["can_bluetooth"])
        self.assertIsNone(A.AudioControl(A.OutputWatch(cfg), lambda m: None).info()["dongle"])

    def test_alsa_card_parsing_for_the_mixer(self):
        self.assertEqual(asyncio.run(A.alsa_full_volume("pipewire", lambda m: None)), False)


class ReleaseTest(unittest.IsolatedAsyncioTestCase):
    """Disconnect from the web remote: the stream closes and stays closed until a real Play."""

    async def asyncSetUp(self):
        import test_sync as T
        self.T = T
        self.sink, self.clock, self.sent = T.make_sink(lip=0)
        self.woken = []
        self.sink.on_wake = lambda: self.woken.append(self.clock.t)
        await T.start_item(self.sink, self.clock)
        await T.run_for(self.sink, self.clock, 2.0, self.clock.t, 7, 0.0)
        self.assertIs(self.sink.mpv.props["pause"], False, "playing before the disconnect")

    async def test_release_stops_and_ignores_the_tvs_late_playing_clocks(self):
        s, c, T = self.sink, self.clock, self.T
        await s.release("test")
        self.assertIn(("stop",), s.mpv.calls)
        self.assertIsNone(s.loaded_id)
        loads = len([x for x in s.mpv.calls if x[0] == "loadfile"])
        await T.run_for(s, c, 1.0, c.t, 7, 2.0)          # the TV still says playing for a moment
        await s.handle({"type": "load", "item": {"id": 8, "kind": "audio"}, "position": 0, "autoplay": False})
        await T.play(s, c, 8, 0.0, state="paused")      # next item queued paused
        for _ in range(5):
            c.t += A.TICK
            await s.tick()
        self.assertEqual(len([x for x in s.mpv.calls if x[0] == "loadfile"]), loads, "nothing opened while released")
        self.assertEqual(self.woken, [])
        self.assertTrue(s.released)

    async def test_play_after_pause_wakes_and_loads(self):
        s, c, T = self.sink, self.clock, self.T
        await s.release("test")
        await T.play(s, c, 7, 2.0, state="paused")
        c.t += A.TICK
        await s.tick()
        self.assertEqual(self.woken, [])
        await T.run_for(s, c, 0.5, c.t, 7, 2.0)            # Play
        self.assertEqual(len(self.woken), 1, "the Play reconnects the dongle")
        self.assertFalse(s.released)
        self.assertTrue(any(x[0] == "loadfile" for x in s.mpv.calls[-10:]), "and opens the stream again")

    async def test_connect_wakes_without_play(self):
        s = self.sink
        await s.release("test")
        s.wake("connect")
        self.assertFalse(s.released)


class DisconnectOpTest(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_closes_the_stream_before_the_dongle_is_told(self):
        order = []
        cfg = A.Config({"ATV_AUDIO_SERVER": "http://x", "ATV_AUDIO_OUTPUT": "alsa:default"})
        ac = A.AudioControl(A.OutputWatch(cfg), lambda m: None)
        d = D.Btd700("/dev/fake")
        d.fd, d.state = 99, "streaming"

        async def connect(on=True):
            order.append("dongle " + ("on" if on else "off"))
            return True

        async def before():
            order.append("release")
        d.connect = connect
        ac.dongle, ac.on_disconnect, ac.on_connect = d, before, lambda: order.append("wake")
        await ac.handle({"op": "disconnect"})
        await ac.handle({"op": "connect"})
        self.assertEqual(order, ["release", "dongle off", "dongle on", "wake"])


if __name__ == "__main__":
    unittest.main()
