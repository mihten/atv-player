#!/usr/bin/env python3
"""Unit tests for the atv-audio sink: sync controller (fake clock + fake mpv), clock offset, sound chain,
output detection parsing. Run: python3 test_sync.py"""
import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import atv_audio as A  # noqa: E402

A.log = lambda msg: None  # quiet: log lines still reach the fake server through Sink.send

FREQS = [31.25, 62.5, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeMpv:
    """Plays in real time on the fake clock while unpaused; a seek lands `lag` s behind its target."""

    def __init__(self, clock, tracks=None):
        self.clock = clock
        self.props = {"pause": True, "seeking": False, "paused-for-cache": False, "eof-reached": False,
                      "demuxer-cache-duration": 10.0, "duration": 300.0}
        self.base, self.base_t = 0.0, clock.t
        self.lag = 0.0
        self.calls = []
        self.tracks = tracks or [{"id": 1, "type": "audio", "selected": True}]
        self.entry = 0
        self.fail = None   # exception the next command raises

    @property
    def pos(self):
        if self.props.get("pause"):
            return self.base
        return self.base + (self.clock.t - self.base_t) * self.props.get("speed", 1.0)

    def set_pos(self, p):
        self.base, self.base_t = p, self.clock.t

    async def command(self, *args):
        if self.fail:
            e, self.fail = self.fail, None
            raise e
        self.calls.append(args)
        if args[0] == "seek":
            self.set_pos(args[1] - self.lag)
        if args[0] == "loadfile":
            self.set_pos(float(self.props.get("start", 0)))
            self.props["path"] = args[1]
            self.entry += 1
            return {"playlist_entry_id": self.entry}

    async def get_property(self, name, default=None):
        if name in ("audio-pts", "time-pos"):
            return self.pos
        if name == "track-list":
            return self.tracks
        return self.props.get(name, default)

    async def set_property(self, name, value):
        if self.fail:
            e, self.fail = self.fail, None
            raise e
        if name in ("pause", "speed"):
            self.set_pos(self.pos)  # keep the position continuous across the change
        self.calls.append(("set", name, value))
        self.props[name] = value

    def seeks(self):
        return [c for c in self.calls if c[0] == "seek"]

    def sets(self, name):
        return [c[2] for c in self.calls if c[0] == "set" and c[1] == name]


class ControllerTest(unittest.TestCase):
    def ready(self, now=0.0):
        c = A.SyncController()
        c.reset(now - 10)  # settled
        return c

    def test_seek_above_150ms(self):
        c = self.ready()
        self.assertIsNone(c.step(0.0, 10.2, 10.0))      # first sample above: confirm once more
        act = c.step(0.1, 10.3, 10.1)
        self.assertEqual(act[0], "seek")
        self.assertAlmostEqual(act[1], 10.3, places=3)

    def test_no_seek_at_149ms(self):
        c = self.ready()
        for i in range(5):
            act = c.step(i * 0.1, 10.149, 10.0)
            self.assertNotEqual(act and act[0], "seek")

    def test_speed_trim_below(self):
        c = self.ready()
        act = c.step(0.0, 10.015, 10.0)                                    # behind 15 ms: faster
        self.assertEqual(act[0], "speed")
        self.assertAlmostEqual(act[1], 1.0015, places=6)
        act = c.step(0.1, 10.0, 10.015)                                    # ahead 15 ms: slower
        self.assertAlmostEqual(act[1], 0.9985, places=6)

    def test_clamp(self):
        c = self.ready()
        self.assertEqual(c.step(0.0, 10.1, 10.0), ("speed", 1.002))
        c = self.ready()
        self.assertEqual(c.step(0.0, 10.0, 10.12), ("speed", 0.998))

    def test_back_to_one_near_zero(self):
        c = self.ready()
        self.assertEqual(c.step(0.0, 10.05, 10.0)[1], 1.002)
        self.assertEqual(c.step(0.1, 10.005, 10.0), ("speed", 1.0))
        self.assertIsNone(c.step(0.2, 10.004, 10.0))                       # unchanged: no command

    def test_reset_after_seek(self):
        c = self.ready()
        c.step(0.0, 11.0, 10.0)
        self.assertEqual(c.step(0.1, 11.1, 10.1)[0], "seek")
        self.assertEqual(c.speed, 1.0)
        self.assertIsNone(c.step(0.2, 11.2, 10.2))                         # settling after the seek
        self.assertIsNone(c.step(0.1 + A.SETTLE - 0.01, 11.2, 10.2))

    def test_lead_learned_from_seek_residual(self):
        c = self.ready()
        c.step(0.0, 11.0, 10.0)
        c.step(0.1, 11.0, 10.0)                                            # seek
        c.step(1.0, 10.05, 10.0)                                           # landed 50 ms behind
        self.assertAlmostEqual(c.lead, 0.05, places=3)


def make_sink(lip=None, tracks=None, hold=300):
    clock = FakeClock()
    sent = []
    cfg = A.Config({"ATV_AUDIO_SERVER": "http://media.example:8080", "ATV_AUDIO_HOLD": str(hold)})
    sink = A.Sink(cfg, sent.append, clock)
    sink.clock_offset.add(clock.t, clock.t, clock.t)  # offset 0
    sink.mpv = FakeMpv(clock, tracks)
    sink.set_output({"kind": "bluetooth", "name": "Headphones", "codec": "aptx_hd", "ready": True})
    if lip is not None:
        sink.lip_ms = lip
    return sink, clock, sent


async def start_item(sink, clock, item=None, position=0.0):
    item = item or {"id": 7, "kind": "audio"}
    await sink.handle({"type": "route", "sink": True})
    await sink.handle({"type": "load", "item": item, "position": position, "autoplay": True, "present": False})
    await sink.on_file_loaded()
    return item


async def play(sink, clock, item_id, position, state="playing"):
    await sink.handle({"type": "clock", "item": item_id, "position": position, "at": clock.t, "state": state})


async def run_for(sink, clock, seconds, tv_start, item_id, tv_pos0):
    """Advance the fake clock in ticks, sending the TV clock 4x/s like the app does."""
    steps = int(round(seconds / A.TICK))
    for i in range(steps):
        clock.t += A.TICK
        if i % 2 == 0:
            await play(sink, clock, item_id, tv_pos0 + (clock.t - tv_start))
        await sink.tick()


class SinkTest(unittest.IsolatedAsyncioTestCase):
    async def test_load_paused_at_position_and_ready(self):
        sink, clock, sent = make_sink(lip=0)
        await start_item(sink, clock, position=12.5)
        mpv = sink.mpv
        self.assertIn(("loadfile", "http://media.example:8080/media/7?audio=main", "replace"), mpv.calls)
        self.assertEqual(mpv.sets("start"), ["12.500"])
        self.assertIs(mpv.props["pause"], True)
        self.assertIn({"type": "ready", "item": 7}, sent)

    async def test_not_ready_while_buffering(self):
        sink, clock, sent = make_sink(lip=0)
        sink.mpv.props["demuxer-cache-duration"] = 0.2
        sink.mpv.props["demuxer-cache-idle"] = False
        await start_item(sink, clock)
        self.assertNotIn({"type": "ready", "item": 7}, sent)
        sink.mpv.props["demuxer-cache-duration"] = 3.0
        await sink.tick()
        self.assertIn({"type": "ready", "item": 7}, sent)

    async def test_aid_per_contract(self):
        two = [{"id": 1, "type": "audio", "selected": True}, {"id": 2, "type": "audio", "selected": False}]
        sink, clock, _ = make_sink(tracks=two)
        await start_item(sink, clock, item={"id": 9, "kind": "video", "audio_ord": 1})
        self.assertEqual(sink.mpv.sets("aid"), [2])
        sink, clock, _ = make_sink()  # the main copy: one track -> aid 1 (already selected)
        await start_item(sink, clock, item={"id": 9, "kind": "video", "audio_ord": 1})
        self.assertEqual(sink.mpv.sets("aid"), [])

    async def test_lip_sync_applied_at_start(self):
        sink, clock, _ = make_sink(lip=200)
        item = await start_item(sink, clock, position=10.0)
        self.assertEqual(sink.mpv.sets("start"), ["10.200"])      # loaded 200 ms ahead already
        await sink.handle({"type": "lip_sync", "ms": 400})
        clock.t += 0.5
        await play(sink, clock, item["id"], 10.0)
        seeks = sink.mpv.seeks()
        self.assertEqual(len(seeks), 1)
        self.assertAlmostEqual(seeks[0][1], 10.4, places=3)
        self.assertIs(sink.mpv.props["pause"], False)

    async def test_paused_lead_compensated(self):
        # with audio-stream-silence a paused mpv reports ~0.2 s less than it was asked to open at
        sink, clock, _ = make_sink(lip=0)
        item = await start_item(sink, clock, position=10.0)
        sink.mpv.set_pos(9.8)
        await play(sink, clock, item["id"], 10.0)
        self.assertAlmostEqual(sink.mpv.seeks()[0][1], 10.2, places=3)
        self.assertAlmostEqual(sink.paused_lead, 0.2, places=3)
        await sink.handle({"type": "load", "item": {"id": 8, "kind": "audio"}, "position": 0})
        self.assertEqual(sink.mpv.sets("start")[-1], "0.200")     # the next load opens that much further

    async def test_start_without_seek_when_aligned(self):
        sink, clock, _ = make_sink(lip=200)
        item = await start_item(sink, clock)
        await play(sink, clock, item["id"], 0.0)
        self.assertEqual(sink.mpv.seeks(), [])
        self.assertIs(sink.mpv.props["pause"], False)

    async def test_controller_seeks_and_trims_while_playing(self):
        sink, clock, _ = make_sink(lip=0)
        item = await start_item(sink, clock)
        t0 = clock.t
        await play(sink, clock, item["id"], 0.0)
        await run_for(sink, clock, 1.0, t0, item["id"], 0.0)
        self.assertEqual(sink.mpv.seeks(), [])
        sink.mpv.set_pos(sink.mpv.pos - 0.5)                      # mpv falls 500 ms behind
        await run_for(sink, clock, 0.3, t0, item["id"], 0.0)
        self.assertEqual(len(sink.mpv.seeks()), 1)
        sink.mpv.set_pos(sink.mpv.pos - 0.03)                     # then 30 ms behind: trim, no seek
        await run_for(sink, clock, 1.0, t0, item["id"], 0.0)
        self.assertEqual(len(sink.mpv.seeks()), 1)
        speeds = sink.mpv.sets("speed")
        self.assertTrue(speeds and max(speeds) > 1.0 and max(speeds) <= 1.002)
        await run_for(sink, clock, 30.0, t0, item["id"], 0.0)
        self.assertEqual(sink.mpv.props["speed"], 1.0)            # aligned again
        self.assertLess(abs(sink.target(clock.t) - sink.mpv.pos), A.NEAR)

    async def test_lip_sync_while_playing(self):
        sink, clock, _ = make_sink(lip=0)
        item = await start_item(sink, clock)
        t0 = clock.t
        await play(sink, clock, item["id"], 0.0)
        await run_for(sink, clock, 1.0, t0, item["id"], 0.0)
        await sink.handle({"type": "lip_sync", "ms": 300})
        await run_for(sink, clock, 1.0, t0, item["id"], 0.0)
        self.assertEqual(len(sink.mpv.seeks()), 1)
        self.assertLess(abs((clock.t - t0) + 0.3 - sink.mpv.pos), 0.01)

    async def test_reset_on_load(self):
        sink, clock, _ = make_sink(lip=0)
        item = await start_item(sink, clock)
        t0 = clock.t
        await play(sink, clock, item["id"], 0.0)
        await run_for(sink, clock, 1.0, t0, item["id"], 0.0)
        sink.mpv.set_pos(sink.mpv.pos - 0.05)
        await run_for(sink, clock, 1.0, t0, item["id"], 0.0)
        self.assertGreater(sink.mpv.props["speed"], 1.0)
        await sink.handle({"type": "load", "item": {"id": 8, "kind": "audio"}, "position": 0, "autoplay": True})
        self.assertEqual(sink.mpv.props["speed"], 1.0)
        self.assertIs(sink.mpv.props["pause"], True)
        self.assertEqual(sink.loaded_id, 8)

    async def test_reset_on_seek_control(self):
        sink, clock, _ = make_sink(lip=0)
        item = await start_item(sink, clock)
        t0 = clock.t
        await play(sink, clock, item["id"], 0.0)
        await run_for(sink, clock, 1.0, t0, item["id"], 0.0)
        sink.mpv.set_pos(sink.mpv.pos - 0.05)
        await run_for(sink, clock, 1.0, t0, item["id"], 0.0)
        await sink.handle({"type": "control", "action": "seek", "position": 60.0})
        self.assertEqual(sink.mpv.seeks()[-1][1], 60.0)
        self.assertEqual(sink.mpv.props["speed"], 1.0)
        await sink.tick()                                          # old clock (before the seek) is ignored
        self.assertEqual(len(sink.mpv.seeks()), 1)

    async def test_paused_clock_no_seeking(self):
        sink, clock, _ = make_sink(lip=0)
        item = await start_item(sink, clock)
        await play(sink, clock, item["id"], 0.0)
        self.assertIs(sink.mpv.props["pause"], False)
        clock.t += 1
        await play(sink, clock, item["id"], 30.0, state="paused")
        for _ in range(20):
            clock.t += A.TICK
            await sink.tick()
        self.assertIs(sink.mpv.props["pause"], True)
        self.assertEqual(sink.mpv.seeks(), [])

    async def test_control_pause_at_once(self):
        sink, clock, _ = make_sink(lip=0)
        item = await start_item(sink, clock)
        await play(sink, clock, item["id"], 0.0)
        await sink.handle({"type": "control", "action": "pause"})
        self.assertIs(sink.mpv.props["pause"], True)
        clock.t += A.TICK
        await sink.tick()                                          # the last clock still says playing
        self.assertIs(sink.mpv.props["pause"], True)

    async def test_route_off_never_plays(self):
        sink, clock, _ = make_sink(lip=0)
        item = await start_item(sink, clock)
        await sink.handle({"type": "route", "sink": False})
        await play(sink, clock, item["id"], 0.0)
        self.assertIs(sink.mpv.props["pause"], True)

    async def test_stale_clock_pauses(self):
        sink, clock, _ = make_sink(lip=0)
        item = await start_item(sink, clock)
        await play(sink, clock, item["id"], 0.0)
        clock.t += A.STALE_CLOCK + 0.1
        await sink.tick()
        self.assertIs(sink.mpv.props["pause"], True)

    async def test_hold_then_release(self):
        sink, clock, sent = make_sink(lip=0, hold=300)
        item = await start_item(sink, clock)
        await play(sink, clock, item["id"], 0.0)
        await play(sink, clock, item["id"], 0.0, state="paused")
        clock.t += 299
        await sink.tick()
        self.assertNotIn(("stop",), sink.mpv.calls)
        clock.t += 2
        await sink.tick()
        self.assertIn(("stop",), sink.mpv.calls)
        self.assertIsNone(sink.loaded_id)
        await sink.tick()                                          # stays released while paused
        self.assertEqual(sink.mpv.calls.count(("stop",)), 1)
        await play(sink, clock, item["id"], 40.0)                  # playing again: reopened at the TV's position
        self.assertEqual(sink.loaded_id, item["id"])
        self.assertEqual(sink.mpv.sets("start")[-1], "40.000")

    async def test_output_gone_silent_until_next_play(self):
        sink, clock, sent = make_sink(lip=0)
        item = await start_item(sink, clock)
        await play(sink, clock, item["id"], 0.0)
        sink.set_output({"kind": "bluetooth", "name": "Headphones", "codec": "aptx_hd", "ready": False})
        await sink.tick()
        self.assertIs(sink.mpv.props["pause"], True)
        self.assertIn({"type": "sink", "name": "atv-audio",
                       "output": {"kind": "bluetooth", "name": "Headphones", "codec": "aptx_hd", "ready": False,
                                  "volume": 100, "muted": False}}, sent)
        clock.t += 0.5
        await play(sink, clock, item["id"], 0.5)                   # the TV plays on without sound
        sink.set_output({"kind": "bluetooth", "name": "Headphones", "codec": "aptx_hd", "ready": True})
        clock.t += 0.5
        await play(sink, clock, item["id"], 1.0)
        self.assertIs(sink.mpv.props["pause"], True)               # back, but not mid-play
        clock.t += 0.5
        await play(sink, clock, item["id"], 1.5, state="paused")   # the TV paused (or seeks: "stalled")
        clock.t += 0.5
        await play(sink, clock, item["id"], 1.5)                   # Play: sound again
        self.assertIs(sink.mpv.props["pause"], False)

    async def test_output_gone_next_item_plays(self):
        sink, clock, _ = make_sink(lip=0)
        item = await start_item(sink, clock)
        await play(sink, clock, item["id"], 0.0)
        sink.set_output({"kind": "bluetooth", "name": "Headphones", "codec": "aptx_hd", "ready": False})
        await sink.tick()
        sink.set_output({"kind": "bluetooth", "name": "Headphones", "codec": "aptx_hd", "ready": True})
        await sink.handle({"type": "load", "item": {"id": 8, "kind": "audio"}, "position": 0})
        await sink.on_file_loaded()
        await play(sink, clock, 8, 0.0)
        self.assertIs(sink.mpv.props["pause"], False)

    async def test_output_missing_at_load(self):
        sink, clock, _ = make_sink(lip=0)
        sink.set_output({"kind": "bluetooth", "name": "Headphones", "codec": "aptx_hd", "ready": False})
        item = await start_item(sink, clock)
        await play(sink, clock, item["id"], 0.0)
        self.assertIs(sink.mpv.props["pause"], True)
        clock.t += A.PLAY_EDGE_KEEP
        await play(sink, clock, item["id"], A.PLAY_EDGE_KEEP)
        sink.set_output({"kind": "bluetooth", "name": "Headphones", "codec": "aptx_hd", "ready": True})
        clock.t += 0.5
        await play(sink, clock, item["id"], 0.5)
        self.assertIs(sink.mpv.props["pause"], True)               # came back mid-play: wait for Play / seek
        clock.t += 0.1
        await play(sink, clock, item["id"], 9.0, state="stalled")  # the TV seeks
        clock.t += 0.1
        await play(sink, clock, item["id"], 9.0)
        self.assertEqual(sink.mpv.sets("start")[-1], "9.000")      # opened only now, at the TV's position
        await sink.on_file_loaded()
        await play(sink, clock, item["id"], 9.0)
        self.assertIs(sink.mpv.props["pause"], False)

    async def test_same_item_load_ready_again(self):
        # repeat one / jump to the current item: the TV waits for 'ready' again
        sink, clock, sent = make_sink(lip=200)
        item = await start_item(sink, clock)
        t0 = clock.t
        await play(sink, clock, item["id"], 0.0)
        await run_for(sink, clock, 2.0, t0, item["id"], 0.0)
        await play(sink, clock, item["id"], 2.0, state="ended")
        sent.clear()
        loads = sum(1 for c in sink.mpv.calls if c[0] == "loadfile")
        await sink.handle({"type": "load", "item": item, "position": 0, "autoplay": True, "present": False})
        self.assertEqual(sum(1 for c in sink.mpv.calls if c[0] == "loadfile"), loads)  # no reload
        self.assertAlmostEqual(sink.mpv.seeks()[-1][1], 0.2, places=3)
        self.assertIs(sink.mpv.props["pause"], True)
        self.assertNotIn({"type": "ready", "item": 7}, sent)      # the seek is not finished yet
        sink.on_mpv_event({"event": "playback-restart"})
        await sink.tick()
        self.assertIn({"type": "ready", "item": 7}, sent)
        clock.t += A.TICK                                          # a late 'playing' clock of the last lap
        await sink.handle({"type": "clock", "item": 7, "position": 1.9, "at": clock.t - 0.3, "state": "playing"})
        self.assertIs(sink.mpv.props["pause"], True)
        await play(sink, clock, item["id"], 0.0)                   # the new lap starts
        self.assertIs(sink.mpv.props["pause"], False)
        self.assertLess(abs(sink.mpv.pos - 0.2), 0.01)

    async def test_route_on_again_sends_ready(self):
        sink, clock, sent = make_sink(lip=0)
        item = await start_item(sink, clock)
        self.assertEqual(sent.count({"type": "ready", "item": 7}), 1)
        await sink.handle({"type": "route", "sink": False})
        await sink.handle({"type": "route", "sink": True})        # e.g. the TV reconnected and waits
        self.assertEqual(sent.count({"type": "ready", "item": 7}), 2)
        self.assertEqual(sum(1 for c in sink.mpv.calls if c[0] == "loadfile"), 1)
        await play(sink, clock, item["id"], 0.0)
        self.assertIs(sink.mpv.props["pause"], False)

    async def test_late_playing_clock_after_pause_ignored(self):
        sink, clock, _ = make_sink(lip=0)
        item = await start_item(sink, clock)
        await play(sink, clock, item["id"], 0.0)
        clock.t += 1.0
        await sink.handle({"type": "control", "action": "pause"})
        clock.t += 0.05                                            # sent by the TV before it paused
        await sink.handle({"type": "clock", "item": 7, "position": 0.9, "at": clock.t - 0.1, "state": "playing"})
        self.assertIs(sink.mpv.props["pause"], True)
        clock.t += 0.5
        await play(sink, clock, item["id"], 1.0)                   # sent after the control: playing again
        self.assertIs(sink.mpv.props["pause"], False)

    async def test_stale_file_loaded_ignored(self):
        sink, clock, sent = make_sink(lip=0)
        await sink.handle({"type": "route", "sink": True})
        await sink.handle({"type": "load", "item": {"id": 7, "kind": "audio"}, "position": 0})
        first = sink.mpv.entry
        await sink.handle({"type": "load", "item": {"id": 8, "kind": "audio"}, "position": 0})
        await sink.on_file_loaded(first)                           # item 7's file-loaded, read late
        self.assertFalse(sink.file_loaded)
        self.assertNotIn({"type": "ready", "item": 8}, sent)
        await sink.on_file_loaded(sink.mpv.entry)
        self.assertIn({"type": "ready", "item": 8}, sent)

    async def test_audio_output_error(self):
        sink, clock, sent = make_sink(lip=0)
        await sink.handle({"type": "route", "sink": True})
        await sink.handle({"type": "load", "item": {"id": 7, "kind": "audio"}, "position": 0})
        entry = sink.mpv.entry
        task = asyncio.ensure_future(sink.on_file_loaded(entry))   # file-loaded comes before the error
        sink.on_mpv_event({"event": "end-file", "reason": "error", "playlist_entry_id": entry,
                           "file_error": "audio output initialization failed"})
        await task
        self.assertFalse(sink.file_loaded)
        self.assertIsNone(sink.loaded_id)
        self.assertEqual(sent[-1]["type"], "sink")
        self.assertIs(sent[-1]["output"]["ready"], False)
        for _ in range(10):
            clock.t += A.TICK
            await sink.tick()
        self.assertEqual(sum(1 for c in sink.mpv.calls if c[0] == "loadfile"), 1)  # no reload loop
        self.assertNotIn({"type": "ready", "item": 7}, sent)
        ok = {"kind": "bluetooth", "name": "Headphones", "codec": "aptx_hd", "ready": True}
        self.assertFalse(sink.set_output(ok))                      # still reported not ready
        clock.t += A.AO_RETRY
        self.assertTrue(sink.set_output(ok))
        self.assertIs(sent[-1]["output"]["ready"], True)
        await sink.handle({"type": "load", "item": {"id": 8, "kind": "audio"}, "position": 0})
        self.assertEqual(sink.loaded_id, 8)

    async def test_other_file_error_ignored(self):
        sink, clock, _ = make_sink(lip=0)
        await start_item(sink, clock)
        sink.on_mpv_event({"event": "end-file", "reason": "error", "playlist_entry_id": 99, "file_error": "x"})
        self.assertEqual(sink.loaded_id, 7)

    async def test_unexpected_error_does_not_escape(self):
        sink, clock, sent = make_sink(lip=0)
        item = await start_item(sink, clock)
        sink.mpv.fail = AttributeError("'NoneType' object has no attribute 'command'")
        await play(sink, clock, item["id"], 0.0)                   # must not raise
        self.assertTrue(any(m.get("type") == "log" and "unexpected" in m["message"] for m in sent))
        clock.t += A.TICK
        await sink.tick()
        self.assertIs(sink.mpv.props["pause"], False)              # the next tick works again

    async def test_mpv_gone_during_tick(self):
        sink, clock, _ = make_sink(lip=0)
        item = await start_item(sink, clock)
        mpv = sink.mpv
        get = mpv.get_property

        async def gone(name, default=None):
            sink.on_mpv_gone()
            return await get(name, default)
        mpv.get_property = gone
        await play(sink, clock, item["id"], 5.0)                   # must not raise
        await sink.tick()

    async def test_sound_live_commands(self):
        sink, clock, _ = make_sink()
        await start_item(sink, clock)
        s1 = {"type": "sound", "processed": True, "gain": -2.0, "freqs": FREQS, "bands": [0] * 10}
        await sink.handle(s1)
        self.assertEqual(sink.mpv.sets("af"), [A.sound_chain(s1)])
        await sink.handle(dict(s1, gain=1.5, bands=[0, 0, 3, 0, 0, 0, 0, 0, 0, 0]))
        self.assertIn(("af-command", "vol", "volume", "1.5dB", "volume"), sink.mpv.calls)
        self.assertIn(("af-command", "eq2", "g", "3", "equalizer"), sink.mpv.calls)
        self.assertEqual(len(sink.mpv.sets("af")), 1)              # no rebuild
        await sink.handle(dict(s1, processed=False))
        self.assertEqual(sink.mpv.sets("af")[-1], "")

    async def test_sound_during_load_no_rebuild(self):
        # the TV sends the next track's gain right with its load: no af-command to a chain mpv has not built yet
        sink, clock, sent = make_sink()
        await start_item(sink, clock)
        s1 = {"type": "sound", "processed": True, "gain": -2.0, "freqs": FREQS, "bands": [0] * 10}
        await sink.handle(s1)
        await sink.handle({"type": "load", "item": {"id": 8, "kind": "audio"}, "position": 0})
        self.assertFalse(sink.file_loaded)                         # loadfile sent, file not loaded yet
        command = sink.mpv.command

        async def no_chain(*args):
            if args[0] == "af-command":                            # what mpv answers while no chain is built
                raise A.MpvError("error running command")
            return await command(*args)
        sink.mpv.command = no_chain
        await sink.handle(dict(s1, gain=-4.0))
        self.assertFalse(any(c[0] == "af-command" for c in sink.mpv.calls))
        self.assertEqual(sink.mpv.sets("af")[-1], A.sound_chain(dict(s1, gain=-4.0)))
        self.assertFalse(any("rebuilding" in m.get("message", "") for m in sent))

    async def test_af_property_current_for_the_next_file(self):
        # af-command changes only the running filters: the next file must not start with the old values
        sink, clock, _ = make_sink()
        await start_item(sink, clock)
        s1 = {"type": "sound", "processed": True, "gain": -2.0, "freqs": FREQS, "bands": [0] * 10}
        await sink.handle(s1)
        s2 = dict(s1, bands=[0, 0, 0, 0, 0, 0, 0, 0, 0, 5])
        await sink.handle(s2)
        self.assertEqual(len(sink.mpv.sets("af")), 1)              # live
        await sink.handle({"type": "load", "item": {"id": 8, "kind": "audio"}, "position": 0})
        self.assertEqual(sink.mpv.sets("af")[-1], A.sound_chain(s2))
        i_af = max(i for i, c in enumerate(sink.mpv.calls) if c[:2] == ("set", "af"))
        i_load = max(i for i, c in enumerate(sink.mpv.calls) if c[0] == "loadfile")
        self.assertLess(i_af, i_load)                              # set before the new file opens (paused)
        await sink.on_file_loaded()
        await sink.handle({"type": "load", "item": {"id": 9, "kind": "audio"}, "position": 0})
        self.assertEqual(len(sink.mpv.sets("af")), 2)              # unchanged: not set again

    async def test_volume_mpv_output(self):
        # ALSA device output (no pactl): mpv's own volume, reported in the status
        sink, clock, sent = make_sink()
        sent.clear()
        task = sink.on_volume({"type": "volume", "percent": 62, "muted": False})
        await task
        self.assertEqual(sink.mpv.sets("volume"), [62])
        self.assertEqual(sink.mpv.sets("mute"), [False])
        self.assertEqual(sent[-1]["output"]["volume"], 62)
        self.assertIs(sent[-1]["output"]["muted"], False)
        self.assertFalse(any(m["type"] == "log" for m in sent))   # a volume change logs nothing
        await sink.handle({"type": "volume", "percent": 140, "muted": True})
        await sink.volume_task
        self.assertEqual(sink.mpv.sets("volume")[-1], 100)        # never above 100 %
        self.assertEqual(sink.mpv.sets("mute")[-1], True)
        self.assertEqual((sent[-1]["output"]["volume"], sent[-1]["output"]["muted"]), (100, True))
        await sink.handle({"type": "volume", "percent": "x"})     # ignored
        # a new mpv starts at 100 %: the last volume is set again
        mpv = FakeMpv(clock)
        await sink.on_mpv(mpv)
        self.assertEqual(mpv.sets("volume"), [100])
        self.assertEqual(mpv.sets("mute"), [True])

    async def test_volume_without_output(self):
        # pactl: nothing to set while the device is missing (it keeps its own volume)
        sink, clock, sent = make_sink()
        calls = []

        async def backend(percent, muted):
            calls.append((percent, muted))
            return True
        sink.volume_backend = backend
        sink.set_output({"kind": "bluetooth", "name": "Headphones", "codec": "aptx_hd", "ready": False})
        self.assertIsNone(sink.on_volume({"type": "volume", "percent": 30, "muted": False}))
        self.assertEqual(calls, [])
        self.assertIsNone(sink.on_volume({"type": "volume", "percent": float("inf")}))  # not a number we can use
        # mpv's own volume (ALSA): kept, so a device plugged in later does not play at 100 %
        sink, clock, sent = make_sink()
        sink.set_output({"kind": "usb", "name": "DAC", "codec": None, "ready": False})
        await sink.on_volume({"type": "volume", "percent": 30, "muted": False})
        self.assertEqual(sink.mpv.sets("volume"), [30])
        sink.set_output({"kind": "usb", "name": "DAC", "codec": None, "ready": True})
        self.assertEqual((sent[-1]["output"]["volume"], sent[-1]["output"]["ready"]), (30, True))

    async def test_play_just_before_the_poll_sees_the_output(self):
        sink, clock, _ = make_sink(lip=0)
        item = await start_item(sink, clock)
        await play(sink, clock, item["id"], 0.0)
        sink.set_output({"kind": "bluetooth", "name": "Headphones", "codec": "aptx_hd", "ready": False})
        clock.t += 0.5
        await play(sink, clock, item["id"], 0.5, state="paused")
        clock.t += 1.0
        await play(sink, clock, item["id"], 0.5)                   # Play while the poll still says not ready
        clock.t += 2.0
        sink.set_output({"kind": "bluetooth", "name": "Headphones", "codec": "aptx_hd", "ready": True})
        clock.t += 0.1
        await play(sink, clock, item["id"], 2.6)
        self.assertIs(sink.mpv.props["pause"], False)

    async def test_reopen_after_audio_output_error_on_play(self):
        sink, clock, _ = make_sink(lip=0)
        item = await start_item(sink, clock)
        await play(sink, clock, item["id"], 0.0)
        entry = sink.mpv.entry
        sink.on_mpv_event({"event": "end-file", "reason": "error", "playlist_entry_id": entry,
                           "file_error": "audio output device lost"})
        self.assertEqual((sink.failed_id, sink.lost_item), (7, 7))
        clock.t += A.AO_RETRY + 1
        sink.set_output({"kind": "bluetooth", "name": "Headphones", "codec": "aptx_hd", "ready": True})
        await play(sink, clock, item["id"], 30.0, state="paused")
        clock.t += 0.2
        await play(sink, clock, item["id"], 30.0)                  # Play: opened again at the TV's position
        self.assertEqual(sum(1 for c in sink.mpv.calls if c[0] == "loadfile"), 2)
        await sink.on_file_loaded()
        await play(sink, clock, item["id"], 30.0)
        self.assertIs(sink.mpv.props["pause"], False)

    async def test_volume_pactl_backend(self):
        sink, clock, sent = make_sink()
        calls = []

        async def backend(percent, muted):
            calls.append((percent, muted))
            await asyncio.sleep(0)
            return True
        sink.volume_backend = backend
        out = {"kind": "bluetooth", "name": "MOMENTUM 4", "codec": "aptx_hd", "ready": True, "volume": 81,
               "muted": False}
        sink.set_output(out)
        self.assertEqual(sent[-1]["output"]["volume"], 81)
        t = sink.on_volume({"type": "volume", "percent": 50, "muted": False})
        for p in (55, 60, 65):                                     # a slider: the latest wins, none queued up
            sink.on_volume({"type": "volume", "percent": p, "muted": False})
        await t
        self.assertEqual(calls[-1], (65, False))
        self.assertLessEqual(len(calls), 2)
        self.assertEqual(sent[-1]["output"]["volume"], 65)
        self.assertFalse(sink.set_output(out))                     # a poll right after: pactl may lag, keep 65
        clock.t += A.VOLUME_KEEP + 0.1
        self.assertTrue(sink.set_output(dict(out, volume=40)))     # changed on the headphones
        self.assertEqual(sent[-1]["output"]["volume"], 40)


class ClockOffsetTest(unittest.TestCase):
    def test_lowest_rtt_wins(self):
        c = A.ClockOffset()
        c.add(100.0, 100.30, 600.5)     # rtt 300 ms, offset 500.35
        c.add(110.0, 110.02, 610.01)    # rtt 20 ms, offset 500.0
        c.add(120.0, 120.10, 620.10)    # rtt 100 ms, offset 500.05
        self.assertAlmostEqual(c.offset, 500.0, places=6)

    def test_keeps_last_8(self):
        c = A.ClockOffset()
        c.add(0.0, 0.001, 50.0)          # best, but pushed out by 8 newer samples
        for i in range(8):
            c.add(10.0 + i, 10.05 + i, 70.025 + i)
        self.assertEqual(len(c.samples), 8)
        self.assertAlmostEqual(c.offset, 60.0, places=6)

    def test_sink_ping_pong(self):
        clock = FakeClock(10.0)
        sink = A.Sink(A.Config({}), None, clock)
        self.assertIsNone(sink.server_now())
        ping = sink.make_ping(1)
        self.assertEqual(ping, {"type": "ping", "n": 1})
        clock.t = 10.04
        sink.on_pong({"type": "pong", "n": 1, "t": 5000.02})
        self.assertAlmostEqual(sink.server_now(), 5000.04, places=6)


class SoundTest(unittest.TestCase):
    def test_chain(self):
        s = {"processed": True, "gain": -3.5, "freqs": FREQS, "bands": [1, 0, -2, 0, 0, 0, 3.25, 0, 0, 0]}
        chain = A.sound_chain(s)
        parts = chain.split(",")
        self.assertEqual(len(parts), 12)
        self.assertEqual(parts[0], "@vol:lavfi-volume=volume=-3.5dB")
        self.assertEqual(parts[1], "@eq0:lavfi-equalizer=f=31.25:t=o:w=1:g=1")
        self.assertEqual(parts[7], "@eq6:lavfi-equalizer=f=2000:t=o:w=1:g=3.25")
        self.assertEqual(parts[10], "@eq9:lavfi-equalizer=f=16000:t=o:w=1:g=0")
        self.assertEqual(parts[11], "@lim:lavfi-alimiter=limit=0.8913:attack=1.5:release=100:level=disabled")

    def test_unprocessed_is_empty(self):
        self.assertEqual(A.sound_chain({"processed": False, "gain": 3, "freqs": FREQS, "bands": [1] * 10}), "")
        self.assertEqual(A.sound_chain(None), "")

    def test_commands(self):
        old = {"processed": True, "gain": 0.0, "freqs": FREQS, "bands": [0] * 10}
        new = dict(old, gain=2.25, bands=[0, 0, 0, 0, 0, 0, 0, 0, 0, -4])
        self.assertEqual(A.sound_commands(old, new), [["vol", "volume", "2.25dB", "volume"],
                                                      ["eq9", "g", "-4", "equalizer"]])
        self.assertEqual(A.sound_commands(old, dict(old)), [])
        moved = dict(old, freqs=[32] + FREQS[1:])
        self.assertEqual(A.sound_commands(old, moved), [["eq0", "f", "32", "equalizer"]])
        self.assertIsNone(A.sound_commands(None, new))                         # nothing applied yet
        self.assertIsNone(A.sound_commands(old, dict(new, processed=False)))   # af cleared
        self.assertIsNone(A.sound_commands(dict(old, processed=False), new))   # chain built


PACTL_JSON = json.dumps([
    {"index": 47, "state": "SUSPENDED", "name": "alsa_output.pci-0000_00_1f.3.analog-stereo",
     "description": "Built-in Audio Analog Stereo", "driver": "PipeWire",
     "properties": {"device.api": "alsa", "device.bus": "pci", "device.description": "Built-in Audio",
                    "node.name": "alsa_output.pci-0000_00_1f.3.analog-stereo"}},
    {"index": 63, "state": "RUNNING", "name": "bluez_output.00_1B_66_00_00_01.1",
     "description": "MOMENTUM 4", "driver": "PipeWire", "mute": False,
     "volume": {"front-left": {"value": 53080, "value_percent": "81%", "db": "-5.49 dB"},
                "front-right": {"value": 53080, "value_percent": "81%", "db": "-5.49 dB"}},
     "properties": {"api.bluez5.address": "00:1B:66:00:00:01", "api.bluez5.codec": "aptx_hd",
                    "api.bluez5.profile": "a2dp-sink", "device.api": "bluez5", "device.bus": "bluetooth",
                    "device.description": "MOMENTUM 4", "media.class": "Audio/Sink"}},
    {"index": 71, "state": "IDLE", "name": "alsa_output.usb-Sennheiser_BTD_700-00.analog-stereo",
     "description": "BTD 700 Analog Stereo", "driver": "PipeWire",
     "properties": {"alsa.driver_name": "snd_usb_audio", "device.api": "alsa", "device.bus": "usb",
                    "device.description": "BTD 700", "media.class": "Audio/Sink"}},
])

PACTL_TEXT = """Sink #63
\tState: RUNNING
\tName: bluez_output.00_1B_66_00_00_01.1
\tDescription: MOMENTUM 4
\tDriver: PipeWire
\tSample Specification: s24le 2ch 48000Hz
\tMute: yes
\tVolume: front-left: 40632 /  62% / -12.46 dB,   front-right: 40632 /  62% / -12.46 dB
\t        balance 0.00
\tProperties:
\t\tapi.bluez5.codec = "aptx_hd"
\t\tdevice.api = "bluez5"
\t\tdevice.bus = "bluetooth"
\t\tdevice.description = "MOMENTUM 4"
\tFormats:
\t\tpcm

Sink #71
\tState: IDLE
\tName: alsa_output.usb-Sennheiser_BTD_700-00.analog-stereo
\tDescription: BTD 700 Analog Stereo
\tDriver: PipeWire
\tProperties:
\t\tdevice.bus = "usb"
\t\tdevice.description = "BTD 700"
"""

ASOUND = """ 0 [PCH            ]: HDA-Intel - HDA Intel PCH
                      HDA Intel PCH at 0xf7f10000 irq 32
 1 [BTD700         ]: USB-Audio - BTD 700
                      Sennheiser BTD 700 at usb-0000:00:14.0-2, full speed
"""


class OutputTest(unittest.TestCase):
    def test_pactl_json_bluetooth_default(self):
        sinks = A.parse_pactl_json(PACTL_JSON)
        out, pref = A.pick_output(sinks, "bluez_output.00_1B_66_00_00_01.1", None)
        self.assertEqual(out, {"kind": "bluetooth", "name": "MOMENTUM 4", "codec": "aptx_hd", "ready": True,
                               "volume": 81, "muted": False})
        self.assertEqual(pref["key"], "bluez_output.00_1B_66_00_00_01.1")

    def test_pactl_json_usb_dongle(self):
        sinks = A.parse_pactl_json(PACTL_JSON)
        out, _ = A.pick_output(sinks, "alsa_output.usb-Sennheiser_BTD_700-00.analog-stereo", None)
        self.assertEqual(out, {"kind": "usb", "name": "BTD 700", "codec": None, "ready": True})

    def test_pactl_json_other(self):
        sinks = A.parse_pactl_json(PACTL_JSON)
        out, _ = A.pick_output(sinks, "alsa_output.pci-0000_00_1f.3.analog-stereo", None)
        self.assertEqual(out, {"kind": "other", "name": "Built-in Audio", "codec": None, "ready": True})

    def test_headphones_off(self):
        sinks = A.parse_pactl_json(PACTL_JSON)
        _, pref = A.pick_output(sinks, "bluez_output.00_1B_66_00_00_01.1", None)
        rest = [s for s in sinks if not s[0].startswith("bluez")]
        # the default falls back to the built-in output: still report the headphones, not ready
        out, pref = A.pick_output(rest, "alsa_output.pci-0000_00_1f.3.analog-stereo", pref)
        self.assertEqual(out, {"kind": "bluetooth", "name": "MOMENTUM 4", "codec": "aptx_hd", "ready": False,
                               "volume": 81, "muted": False})
        out, _ = A.pick_output([], "auto_null", pref)
        self.assertFalse(out["ready"])
        out, _ = A.pick_output(sinks, "bluez_output.00_1B_66_00_00_01.1", pref)  # back
        self.assertTrue(out["ready"])

    def test_nothing_found(self):
        self.assertEqual(A.pick_output([], "", None), (None, None))
        self.assertEqual(A.pick_output([("auto_null", "Dummy Output", {})], "auto_null", None), (None, None))

    def test_pactl_text(self):
        sinks = A.parse_pactl_text(PACTL_TEXT)
        self.assertEqual([s[0] for s in sinks], ["bluez_output.00_1B_66_00_00_01.1",
                                                 "alsa_output.usb-Sennheiser_BTD_700-00.analog-stereo"])
        out, _ = A.pick_output(sinks, "bluez_output.00_1B_66_00_00_01.1", None)
        self.assertEqual(out["codec"], "aptx_hd")
        self.assertEqual((out["volume"], out["muted"]), (62, True))
        out, _ = A.pick_output(sinks, "alsa_output.usb-Sennheiser_BTD_700-00.analog-stereo", None)
        self.assertEqual(out, {"kind": "usb", "name": "BTD 700", "codec": None, "ready": True})

    def test_asound_cards(self):
        cards = A.parse_asound_cards(ASOUND)
        self.assertEqual([(c["index"], c["id"], c["usb"]) for c in cards], [(0, "PCH", False), (1, "BTD700", True)])
        usb = {"kind": "usb", "name": "BTD 700", "codec": None, "ready": True}
        self.assertEqual(A.alsa_output("plughw:CARD=BTD700,DEV=0", ASOUND), usb)
        self.assertEqual(A.alsa_output("hw:1,0", ASOUND), usb)
        self.assertEqual(A.alsa_output("hw:BTD700", ASOUND), usb)
        self.assertEqual(A.alsa_output("hw:0", ASOUND)["kind"], "other")
        gone = A.alsa_output("plughw:CARD=BTD700,DEV=0", ASOUND.split(" 1 [")[0])
        self.assertEqual(gone["ready"], False)


class DetectTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._which, self._run = A.shutil.which, A._run

    def tearDown(self):
        A.shutil.which, A._run = self._which, self._run

    async def test_no_pactl_assumes_ready(self):
        A.shutil.which = lambda name: None
        w = A.OutputWatch(A.Config({}))
        self.assertEqual((await w.detect())["ready"], True)
        self.assertTrue(w.no_tool)

    async def test_pactl_without_server_not_ready(self):
        A.shutil.which = lambda name: "/usr/bin/pactl"
        answers = {"on": True}

        async def run(*args, **kw):
            if not answers["on"]:
                return None
            return "" if "get-default-sink" in args else PACTL_JSON if "json" in args else None
        A._run = run
        os.environ.pop("XDG_RUNTIME_DIR", None)
        os.environ.pop("PIPEWIRE_RUNTIME_DIR", None)
        w = A.OutputWatch(A.Config({"ATV_AUDIO_OUTPUT": "pulse"}))
        self.assertTrue((await w.detect())["ready"])
        answers["on"] = False
        self.assertTrue((await w.detect())["ready"])               # one miss: last result
        out = await w.detect()
        self.assertIs(out["ready"], False)
        self.assertFalse(w.no_tool)
        w = A.OutputWatch(A.Config({}))                            # never answered
        self.assertIs((await w.detect())["ready"], False)


    async def test_volume_through_pactl(self):
        A.shutil.which = lambda name: "/usr/bin/pactl"
        runs = []

        async def run(*args, **kw):
            runs.append(args)
            if "get-default-sink" in args:
                return "bluez_output.00_1B_66_00_00_01.1\n"
            if "list" in args:
                return PACTL_JSON
            return ""
        A._run = run
        w = A.OutputWatch(A.Config({"ATV_AUDIO_OUTPUT": "pipewire"}))
        self.assertTrue(w.has_pactl)
        out = await w.detect()
        self.assertEqual((out["volume"], out["muted"]), (81, False))
        self.assertTrue(await w.set_volume(62, True))
        self.assertIn(("pactl", "set-sink-volume", "bluez_output.00_1B_66_00_00_01.1", "62%"), runs)
        self.assertIn(("pactl", "set-sink-mute", "bluez_output.00_1B_66_00_00_01.1", "1"), runs)
        w = A.OutputWatch(A.Config({"ATV_AUDIO_OUTPUT": "pulse:tcp:box:4713"}))
        await w.detect()
        await w.set_volume(10, False)
        self.assertIn(("pactl", "-s", "tcp:box:4713", "set-sink-mute", "bluez_output.00_1B_66_00_00_01.1", "0"), runs)
        self.assertFalse(A.OutputWatch(A.Config({"ATV_AUDIO_OUTPUT": "alsa:hw:1"})).has_pactl)
        w = A.OutputWatch(A.Config({}))
        self.assertFalse(await w.set_volume(10, False))           # nothing detected yet: no sink to set


class ArgsTest(unittest.TestCase):
    def test_outputs(self):
        self.assertEqual(A.output_args("pipewire"), ["--ao=pipewire"])
        self.assertEqual(A.output_args("pulse"), ["--ao=pulse"])
        self.assertEqual(A.output_args("pulse:tcp:box:4713"), ["--ao=pulse", "--pulse-host=tcp:box:4713"])
        self.assertEqual(A.output_args("alsa:plughw:CARD=X,DEV=0"),
                         ["--ao=alsa", "--audio-device=alsa/plughw:CARD=X,DEV=0"])
        with self.assertRaises(ValueError):
            A.output_args("jack")

    def test_mpv_args_and_urls(self):
        cfg = A.Config({"ATV_AUDIO_SERVER": "https://media.example:8443/"})
        self.assertEqual(cfg.ws_url, "wss://media.example:8443/ws?role=sink&session=tv")
        self.assertEqual(cfg.media_url(5), "https://media.example:8443/media/5?audio=main")
        args = A.mpv_args(cfg, "/run/x.sock")
        for a in ("--no-video", "--idle=yes", "--audio-pitch-correction=no", "--keep-open=always",
                  "--audio-stream-silence=yes", "--input-ipc-server=/run/x.sock", "--ao=pipewire",
                  "--gapless-audio=weak"):
            self.assertIn(a, args)
        self.assertEqual(A.Config({}).hold, 300.0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
