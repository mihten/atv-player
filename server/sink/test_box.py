#!/usr/bin/env python3
"""Unit tests for atv-audio's own player ("box" session) and the shared output: Router (TV wins), BoxDriver (clock
follows mpv, ended / error / notice / status), Buttons (Play ignored right after a reconnect, session = the output's
owner). Fake clock + fake mpv from test_sync. Run: python3 test_box.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import atv_audio as A  # noqa: E402
import test_sync as T  # noqa: E402

SONG = {"id": 11, "kind": "audio", "title": "Song", "artist": "Band", "duration": 200.0}
TVITEM = {"id": 22, "kind": "video", "title": "Video", "artist": "Singer", "duration": 240.0}


def make():
    sink, clock, sink_sent = T.make_sink()
    router = A.Router(sink)
    box_sent = []
    box = A.BoxDriver(sink, router, box_sent.append, clock)
    router.box = box
    return sink, clock, router, box, box_sent, sink_sent


async def ticks(sink, box, clock, seconds, tv=None):
    """Advance in ticks like run()'s ticker; tv = (router, item_id, start_t, pos0) sends the TV clock 4x/s."""
    for i in range(int(round(seconds / A.TICK))):
        clock.t += A.TICK
        if tv and i % 2 == 0:
            r, iid, t0, p0 = tv
            await r.from_tv({"type": "clock", "item": iid, "position": p0 + (clock.t - t0), "at": clock.t,
                             "state": "playing"})
        await box.tick()
        await sink.tick()


async def box_play(router, box, sink, item=SONG, position=0.0):
    await box.handle({"type": "sound", "processed": True, "gain": 4.0, "freqs": T.FREQS, "bands": [0] * 10})
    await box.handle({"type": "load", "item": item, "position": position, "autoplay": True})
    await sink.on_file_loaded()


def statuses(sent):
    return [m for m in sent if m.get("type") == "status"]


class BoxTest(unittest.IsolatedAsyncioTestCase):
    async def test_box_plays_and_reports(self):
        sink, clock, router, box, sent, _ = make()
        await box_play(router, box, sink, position=5.0)
        self.assertEqual(router.owner, "box")
        self.assertTrue(sink.free_run)
        self.assertEqual(sink.lip_ms, 0)
        await ticks(sink, box, clock, 3.0)
        mpv = sink.mpv
        self.assertFalse(mpv.props["pause"])
        self.assertEqual(mpv.calls[0][0], "set")  # af before the file
        self.assertTrue(any(c[0] == "loadfile" for c in mpv.calls))
        self.assertAlmostEqual(box.pos, mpv.pos, places=3)
        self.assertGreater(box.pos, 7.5)
        self.assertEqual(mpv.sets("speed"), [], "no speed trims in box mode")
        self.assertLessEqual(len(mpv.seeks()), 1, "at most the start seek")
        st = statuses(sent)
        self.assertTrue(st and st[-1]["playing"] and st[-1]["item"] == 11)
        self.assertNotIn("ready", [m.get("type") for m in sent])

    async def test_pause_then_play_and_seek(self):
        sink, clock, router, box, sent, _ = make()
        await box_play(router, box, sink)
        await ticks(sink, box, clock, 2.0)
        await box.handle({"type": "control", "action": "pause"})
        await ticks(sink, box, clock, 1.0)
        self.assertTrue(sink.mpv.props["pause"])
        held = box.pos
        await ticks(sink, box, clock, 2.0)
        self.assertAlmostEqual(box.pos, held, places=3)
        self.assertFalse(statuses(sent)[-1]["playing"])
        await box.handle({"type": "control", "action": "seek", "position": 60.0})
        self.assertAlmostEqual(sink.mpv.pos, 60.0, places=2)
        await box.handle({"type": "control", "action": "play"})
        await ticks(sink, box, clock, 1.0)
        self.assertFalse(sink.mpv.props["pause"])
        self.assertGreater(box.pos, 60.5)

    async def test_ended_once(self):
        sink, clock, router, box, sent, _ = make()
        await box_play(router, box, sink)
        await ticks(sink, box, clock, 1.0)
        sink.mpv.props["eof-reached"] = True
        await ticks(sink, box, clock, 1.0)
        self.assertEqual([m for m in sent if m.get("type") == "ended"], [{"type": "ended", "item": 11}])

    async def test_error_once(self):
        sink, clock, router, box, sent, _ = make()
        await box_play(router, box, sink)
        sink.failed_id = 11
        await ticks(sink, box, clock, 1.0)
        self.assertEqual(len([m for m in sent if m.get("type") == "error"]), 1)

    async def test_output_lost_pauses_and_stays_paused(self):
        sink, clock, router, box, sent, _ = make()
        await box_play(router, box, sink)
        await ticks(sink, box, clock, 2.0)
        sink.set_output({"kind": "bluetooth", "name": "Headphones", "codec": None, "ready": False})
        await ticks(sink, box, clock, 1.0)
        self.assertFalse(box.want_play)
        self.assertIn({"type": "notice", "text": "Headphones disconnected: paused"}, sent)
        sink.set_output({"kind": "bluetooth", "name": "Headphones", "codec": "aptx_hd", "ready": True})
        await ticks(sink, box, clock, 2.0)
        self.assertTrue(sink.mpv.props["pause"], "headphones back: the box stays paused")
        await box.handle({"type": "control", "action": "play"})
        await ticks(sink, box, clock, 1.0)
        self.assertFalse(sink.mpv.props["pause"], "Play resumes")

    async def test_tv_takes_the_output(self):
        sink, clock, router, box, sent, _ = make()
        await box_play(router, box, sink)
        await ticks(sink, box, clock, 2.0)
        await router.from_tv({"type": "route", "sink": True})
        self.assertEqual(router.owner, "box", "route alone (TV paused) does not take it")
        await router.from_tv({"type": "lip_sync", "ms": -100})
        await router.from_tv({"type": "load", "item": TVITEM, "position": 0, "autoplay": True})
        self.assertEqual(router.owner, "tv")
        self.assertFalse(sink.free_run)
        self.assertFalse(box.want_play)
        self.assertFalse(statuses(sent)[-1]["playing"])
        self.assertEqual(sink.item["id"], 22)
        self.assertEqual(sink.lip_ms, -100)
        await sink.on_file_loaded()
        t0 = clock.t
        await ticks(sink, box, clock, 2.0, tv=(router, 22, t0, 0.0))
        self.assertFalse(sink.mpv.props["pause"])
        # the box may not take it back while the TV plays
        await box.handle({"type": "control", "action": "play"})
        self.assertEqual(router.owner, "tv")
        self.assertFalse(box.want_play)

    async def test_box_takes_it_back_when_tv_stopped(self):
        sink, clock, router, box, sent, _ = make()
        await router.from_tv({"type": "route", "sink": True})
        await router.from_tv({"type": "load", "item": TVITEM, "position": 0, "autoplay": True})
        await sink.on_file_loaded()
        t0 = clock.t
        await ticks(sink, box, clock, 1.0, tv=(router, 22, t0, 0.0))
        await router.from_tv({"type": "clock", "item": 22, "position": 1.0, "at": clock.t, "state": "paused"})
        await ticks(sink, box, clock, 1.0)
        await box_play(router, box, sink)
        self.assertEqual(router.owner, "box")
        self.assertEqual(sink.item["id"], 11)
        await ticks(sink, box, clock, 1.0)
        self.assertFalse(sink.mpv.props["pause"])

    async def test_tv_messages_while_box_owns_are_kept(self):
        sink, clock, router, box, sent, _ = make()
        await box_play(router, box, sink)
        await router.from_tv({"type": "sound", "processed": True, "gain": 9.0, "freqs": T.FREQS, "bands": [1] * 10})
        self.assertEqual(sink.sound["gain"], 4.0, "box sound stays while the box owns the output")
        await router.from_tv({"type": "volume", "percent": 40, "muted": False})
        self.assertEqual(sink.output["volume"], 40, "volume is the output's: always applied")
        await router.from_tv({"type": "route", "sink": True})
        await router.from_tv({"type": "load", "item": TVITEM, "position": 0, "autoplay": True})
        self.assertEqual(sink.sound["gain"], 9.0, "TV's own sound back with the TV")


class ButtonsTest(unittest.IsolatedAsyncioTestCase):
    def setup(self):
        sink, clock, router, box, sent, sink_sent = make()
        out = []
        b = A.Buttons(router, out.append)
        return sink, clock, router, box, b, out

    async def test_play_ignored_right_after_connect(self):
        sink, clock, router, box, b, out = self.setup()
        sink.set_output({"kind": "bluetooth", "name": "Headphones", "codec": None, "ready": False})
        sink.set_output({"kind": "bluetooth", "name": "Headphones", "codec": "aptx_hd", "ready": True})
        clock.t += 3
        b.on_cmd("play")
        self.assertEqual(out, [])
        b.on_cmd("pause")
        self.assertEqual(out, [{"type": "button", "cmd": "pause", "session": "box"}])
        clock.t += A.PLAY_GUARD
        b.on_cmd("play")
        self.assertEqual(out[-1], {"type": "button", "cmd": "play", "session": "box"})

    async def test_play_ignored_without_output(self):
        sink, clock, router, box, b, out = self.setup()
        clock.t += 60
        sink.set_output({"kind": "bluetooth", "name": "Headphones", "codec": None, "ready": False})
        b.on_cmd("play")
        self.assertEqual(out, [])

    async def test_session_follows_the_owner(self):
        sink, clock, router, box, b, out = self.setup()
        clock.t += 60
        await router.from_tv({"type": "route", "sink": True})
        await router.from_tv({"type": "load", "item": TVITEM, "position": 0, "autoplay": True})
        b.on_cmd("next")
        self.assertEqual(out[-1]["session"], "tv")
        self.assertEqual(b.state()["title"], "Video")
        await router.from_tv({"type": "route", "sink": False})
        b.on_cmd("next")
        self.assertEqual(out[-1]["session"], "box", "TV plays its own sound: the buttons drive the box")
        await box_play(router, box, sink)
        self.assertEqual(b.state(), {"status": "Playing", "title": "Song", "artist": "Band", "length": 200.0})


if __name__ == "__main__":
    unittest.main(verbosity=1)
