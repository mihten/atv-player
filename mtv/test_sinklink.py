#!/usr/bin/env python3
"""sinklink.py through the Player, with a fake mpv, a fake clock and a fake server: the TV Player's sound on the atv-audio
headphones. Choice kept across restarts; mpv muted; route / lip sync / clock messages; the ping / pong server clock; a
start waits for the sink's `ready` (max 3 s); the output missing at a start or lost while playing pauses with a notice
once per loss; a Play with the headphones still off plays silently; changing the sound output while playing pauses with
a notice; HDMI again unmutes; volume messages never unmute while the headphones play.
    python3 test_sinklink.py
"""
import asyncio
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mtv_player as M  # noqa: E402
import sinklink as SL  # noqa: E402
from test_mtv_player import FakeMpv  # noqa: E402

failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


GOOD = {"connected": True, "name": "atv-audio", "route": True, "output": {"kind": "bluetooth", "name": "Headphones X", "codec": "aptx_hd", "ready": True}}
OFF = {"connected": True, "name": "atv-audio", "route": True, "output": {"kind": "bluetooth", "name": "Headphones X", "codec": None, "ready": False}}
GONE = {"connected": False, "name": None, "output": None, "route": True}


def state(sink):
    return {"type": "state", "state": {"sink": sink, "current": {"id": 11}, "queue": [11]}}


def of(sent, t):
    return [m for m in sent if m.get("type") == t]


def cmds(sent, c):
    return [m for m in sent if m.get("type") == "cmd" and m.get("cmd") == c]


def setup(tmp, **env):
    c = Clock()
    sent = []
    cfg = M.Config(dict({"MTV_SERVER": "http://media.test:8080", "MTV_STATE": tmp}, **env))
    p = M.Player(cfg, send=sent.append, mono=c)
    return c, sent, p


async def started(p, c, sent, mpv):
    """A playing item 11: load + file-loaded + a server clock."""
    await p.handle({"type": "load", "item": {"id": 11, "duration": 100}, "position": 0, "autoplay": True})
    return mpv


async def main():
    tmp = tempfile.mkdtemp()
    c, sent, p = setup(tmp)
    mpv = FakeMpv()
    await p.attach(mpv)
    check(not p.link.on and not p.mpv_muted, "default: HDMI, mpv sound on")
    check(M.Config({"MTV_SERVER": "x"}).lip_sync == -100 and M.Config({"MTV_SERVER": "x", "MTV_LIP_SYNC": "-40"}).lip_sync == -40
          and M.Config({"MTV_SERVER": "x", "MTV_LIP_SYNC": "9999"}).lip_sync == 400, "lip sync: -100 default, env, clamped")

    # ---- the server connects: config
    p.link.reset_link()
    p.link.send_config()
    check(of(sent, "lip_sync") == [{"type": "lip_sync", "ms": -100}] and of(sent, "route") == [{"type": "route", "sink": False}],
          "connect: lip sync + route (HDMI) sent")
    check(not of(sent, "clock"), "... no clock while on HDMI")

    # ---- the choice: headphones while nothing plays
    sent.clear()
    await p.handle(state(GOOD))
    await p.handle({"type": "sound_out", "mode": "headphones"})
    check(p.link.on and p.mpv_muted and mpv.sets("mute")[-1] is True, "headphones: mpv muted")
    check(of(sent, "route")[-1] == {"type": "route", "sink": True}, "route true sent (after the sink connect's config: route false)")
    outs = of(sent, "output")
    check(outs and outs[-1]["sound"] == "headphones", "the output report says headphones")
    check(not of(sent, "notice") and not cmds(sent, "pause"), "nothing played: no notice, no pause")
    c2, sent2, p2 = setup(tmp)
    check(p2.link.on, "the choice is kept across a restart (output.json)")
    check(os.path.exists(os.path.join(tmp, "output.json")), "state file written")

    # ---- the server clock: ping / pong
    sent.clear()
    await p.tick()
    pings = of(sent, "ping")
    check(len(pings) == 1 and pings[0]["n"] == 1, f"first tick pings ({pings})")
    c.t += 0.1
    p.link.pong({"type": "pong", "n": 1, "t": 5000.05})
    now = p.link.server_now()
    assert now is not None
    check(abs(now - (c.t + (5000.05 - 100.05))) < 1e-6, "offset = server - (sent + received) / 2")
    c.t += 0.1
    p.link.pong({"type": "pong", "n": 99, "t": 1.0})
    p.link.pong({"type": "pong", "n": 1, "t": "x"})
    now = p.link.server_now()
    assert now is not None
    check(abs(now - (c.t + 4900.0)) < 1e-6, "an unknown / garbage pong changes nothing")
    for _ in range(12):   # pings: fast at first, slow later
        c.t += 2.0
        await p.tick()
    n_fast = len(of(sent, "ping"))
    sent.clear()
    for _ in range(10):
        c.t += 2.0
        await p.tick()
    check(n_fast >= 8 and len(of(sent, "ping")) <= 2, f"pings: 2 s apart at first ({n_fast}), every 15 s later ({len(of(sent, 'ping'))} in 20 s)")

    # ---- a start waits for ready
    sent.clear()
    mpv.calls.clear()
    await p.handle({"type": "load", "item": {"id": 11, "duration": 100}, "position": 0, "autoplay": True})
    lf = [c_ for c_ in mpv.calls if c_[0] == "cmd" and c_[1] == "loadfile"]
    check(lf and "pause=yes" in lf[0][-1], f"load with autoplay + sink ready: mpv opens PAUSED ({lf[0][-1] if lf else None})")
    check(p.link.waiting and p.want_play, "waiting for the sink's ready, playback still wanted")
    await p._on_file_loaded(p.entry)
    check(mpv.sets("pause") == [], "file-loaded does not start it while waiting")
    mpv.props["pause"] = True
    await p.handle({"type": "sink_ready", "id": 11})
    check(not p.link.waiting and mpv.sets("pause")[-1] is False, "ready -> mpv starts")
    mpv.props.update({"pause": False, "time-pos": 12.5})
    sent.clear()
    await p.tick()
    ck = of(sent, "clock")
    check(len(ck) == 1 and ck[0]["state"] == "playing" and ck[0]["item"] == 11 and ck[0]["position"] == 12.5
          and abs(ck[0]["at"] - p.link.server_now()) < 1e-6, f"playing: clock with mpv's time ({ck})")
    c.t += 0.25
    mpv.props["time-pos"] = 12.75
    sent.clear()
    await p.tick()
    check(len(of(sent, "clock")) == 1 and of(sent, "clock")[0]["position"] == 12.75, "a clock every tick while playing")

    # ---- pause / seek / buffering states
    await p.handle({"type": "control", "action": "pause"})
    sent.clear()
    await p.tick()
    await p.tick()
    check([m["state"] for m in of(sent, "clock")] == ["paused"], "paused: ONE clock (not every tick)")
    await p.handle({"type": "control", "action": "play"})
    mpv.props["pause"] = False
    sent.clear()
    await p.handle({"type": "control", "action": "seek", "position": 40})
    check(of(sent, "clock") and of(sent, "clock")[-1]["state"] == "stalled" and of(sent, "clock")[-1]["position"] == 40.0,
          "seek: stalled at the target at once")
    c.t += 0.5
    mpv.props["time-pos"] = 40.2
    sent.clear()
    await p.tick()
    check(of(sent, "clock") and of(sent, "clock")[-1]["state"] == "playing", "0.4 s after the seek: playing")
    mpv.props["paused-for-cache"] = True
    sent.clear()
    await p.tick()
    check(of(sent, "clock")[-1]["state"] == "stalled", "buffering: stalled")
    mpv.props["paused-for-cache"] = False
    mpv.props["eof-reached"] = True
    sent.clear()
    await p.tick()
    await p.tick()
    check([m["state"] for m in of(sent, "clock")] == ["ended"] and len(of(sent, "ended")) == 1, "end of file: one ended + clock ended")
    mpv.props["eof-reached"] = False
    p.ended_for = None
    p.want_play = True

    # ---- no ready within 3 s: starts anyway
    mpv.calls.clear()
    await p.handle({"type": "load", "item": {"id": 12, "duration": 100}, "position": 0, "autoplay": True})
    check(p.link.waiting, "next item waits")
    c.t += 2.9
    await p.tick()
    check(p.link.waiting, "... still waiting at 2.9 s")
    c.t += 0.2
    await p.tick()
    check(not p.link.waiting and mpv.sets("pause")[-1] is False, "3 s without ready: starts muted anyway")
    # ready for another item does not end the wait
    await p.handle({"type": "load", "item": {"id": 13, "duration": 100}, "position": 0, "autoplay": True})
    await p.handle({"type": "sink_ready", "id": 12})
    check(p.link.waiting, "ready of another item is ignored")
    await p.handle({"type": "sink_ready", "id": 13})
    check(not p.link.waiting, "ready of this item ends the wait")

    # ---- the headphones are lost while playing: pause + notice once
    mpv.props["pause"] = False
    sent.clear()
    mpv.calls.clear()
    await p.handle(state(OFF))
    check(not p.want_play and mpv.sets("pause")[-1] is True and len(cmds(sent, "pause")) == 1, "output lost: paused here and on the server")
    nt = of(sent, "notice")
    check(len(nt) == 1 and nt[0]["text"] == "atv-audio: Headphones X not connected — playback paused", f"one notice ({nt})")
    ov = [c_ for c_ in mpv.calls if c_[0] == "cmd" and c_[1] == "osd-overlay" and c_[2] == 2]
    check(ov and ov[-1][3] == "ass-events" and "Headphones X not connected" in ov[-1][4], "the notice is on the TV (overlay 2)")
    await p.handle(state(OFF))
    await p.handle(state(GONE))
    check(len(of(sent, "notice")) == 1, "more state messages while still lost: no second notice")
    # Play while still lost: plays silently, no new notice
    await p.handle({"type": "control", "action": "play"})
    check(p.want_play and mpv.sets("pause")[-1] is False and len(of(sent, "notice")) == 1 and p.link.notice is None,
          "Play with the headphones still off: plays (muted), the notice is gone, no second one")
    check(cmds(sent, "dismiss"), "... and the server's notice is dismissed")
    await p.handle(state(OFF))
    check(p.want_play and len(of(sent, "notice")) == 1, "the same loss does not pause again")
    # back, then lost again: a new notice
    await p.handle(state(GOOD))
    await p.handle(state(GONE))
    check(not p.want_play and len(of(sent, "notice")) == 2 and "atv-audio is not connected" in of(sent, "notice")[-1]["text"],
          "usable again re-arms: the next loss pauses with a (different) notice")

    # ---- Play while paused and the loss was never shown (it happened while paused): the notice instead
    await p.handle({"type": "control", "action": "play"})   # acknowledge + play (loss already shown)
    await p.handle({"type": "control", "action": "pause"})
    await p.handle(state(GOOD))
    await p.handle({"type": "control", "action": "pause"})
    await p.handle(state(OFF))                              # lost while paused: nothing yet
    n = len(of(sent, "notice"))
    check(n == 2, "lost while paused: no notice yet")
    await p.handle({"type": "control", "action": "play"})
    check(not p.want_play and len(of(sent, "notice")) == n + 1 and mpv.sets("pause")[-1] is True,
          "Play then: the notice instead of starting")

    # ---- a start with the output missing: starts paused with the notice
    await p.handle({"type": "control", "action": "play"})   # plays muted
    sent.clear()
    mpv.calls.clear()
    await p.handle(state(GOOD))
    await p.handle(state(OFF))
    await p.handle({"type": "control", "action": "pause"})
    await p.handle(state(GOOD))
    await p.handle(state(OFF))
    sent.clear()
    mpv.calls.clear()
    await p.handle({"type": "load", "item": {"id": 14, "duration": 100}, "position": 0, "autoplay": True})
    lf = [c_ for c_ in mpv.calls if c_[0] == "cmd" and c_[1] == "loadfile"]
    check(lf and "pause=yes" in lf[0][-1] and not p.want_play and len(of(sent, "notice")) == 1 and not p.link.waiting,
          "load with autoplay and the headphones off: opens paused + notice, no waiting")

    # ---- lost while waiting for ready: starts (muted), pause + notice follow
    await p.handle(state(GOOD))
    await p.handle({"type": "load", "item": {"id": 15, "duration": 100}, "position": 0, "autoplay": True})
    check(p.link.waiting, "waiting again")
    await p.handle(state(GONE))
    check(not p.link.waiting and not p.want_play, "lost while waiting: no waiting, paused with the notice")

    # ---- changing the sound output while playing: pause + notice until Play
    await p.handle(state(GOOD))
    await p.handle({"type": "control", "action": "play"})
    mpv.props["pause"] = False
    sent.clear()
    await p.handle({"type": "sound_out", "mode": "hdmi"})
    nt = of(sent, "notice")
    check(not p.link.on and not p.mpv_muted and mpv.sets("mute")[-1] is False, "HDMI again: mpv unmuted")
    check(of(sent, "route") == [{"type": "route", "sink": False}], "route false sent")
    check(not p.want_play and nt and nt[0]["text"] == "Sound output changed to HDMI — playback paused", f"playing: paused with a notice ({nt})")
    await p.handle({"type": "control", "action": "play"})
    check(p.want_play and p.link.notice is None, "Play ends the notice and plays")
    sent.clear()
    await p.tick()
    check(not of(sent, "clock") and not of(sent, "ping"), "on HDMI: no clock, no ping")
    await p.handle({"type": "sound_out", "mode": "hdmi"})
    check(not of(sent, "notice"), "the same choice again: nothing")

    # ---- headphones again, user mute, volume messages
    await p.handle({"type": "sound_out", "mode": "headphones"})
    await p.handle({"type": "volume", "percent": 70, "muted": False})
    check(mpv.sets("mute")[-1] is True and p.volume == 70, "a volume message does not unmute mpv while the headphones play")
    await p.handle({"type": "sound_out", "mode": "hdmi"})
    check(mpv.sets("mute")[-1] is False, "HDMI: unmuted (the user did not mute)")
    await p.handle({"type": "volume", "percent": 70, "muted": True})
    await p.handle({"type": "sound_out", "mode": "headphones"})
    await p.handle({"type": "sound_out", "mode": "hdmi"})
    check(mpv.sets("mute")[-1] is True, "the user's own mute survives the round trip")
    await p.handle({"type": "volume", "percent": 70, "muted": False})

    # ---- stop clears the notice and the wait
    await p.handle({"type": "sound_out", "mode": "headphones"})
    await p.handle(state(GOOD))
    await p.handle({"type": "load", "item": {"id": 16, "duration": 100}, "position": 0, "autoplay": True})
    check(p.link.waiting, "waiting")
    await p.handle({"type": "control", "action": "stop"})
    check(not p.link.waiting and p.item is None, "stop ends the wait")

    # ---- mpv restart: muted again, no wait for ready
    mpv3 = FakeMpv()
    p.item, p.want_play = {"id": 17, "duration": 50}, True
    p.detach()
    await p.attach(mpv3)
    lf = [c_ for c_ in mpv3.calls if c_[0] == "cmd" and c_[1] == "loadfile"]
    check(mpv3.sets("mute")[-1] is True and lf and "pause=no" in lf[0][-1] and not p.link.waiting,
          "mpv restarted: muted, reopens playing at once (the sink follows the clock)")
    check(M.mpv_args(p.cfg, "/s", 50, p.mpv_muted).count("--mute=yes") == 1, "a new mpv process starts muted")

    # ---- Headphones chosen while they are not ready: ask to connect for one minute, then stop (owner 2026-11-12)
    await p.handle({"type": "control", "action": "stop"})
    await p.handle({"type": "sound_out", "mode": "hdmi"})
    await p.handle(state(OFF))
    sent.clear()
    await p.handle({"type": "sound_out", "mode": "headphones"})
    check(p.link.connecting and p.audio_out() == ("headphones", "connecting…"), "Headphones while off: connecting…")
    await p.tick()
    check(len(of(sent, "sink_connect")) == 1, "first tick: one sink_connect")
    for _ in range(20):          # 10 s of ticks
        c.t += 0.5
        await p.tick()
    check(len(of(sent, "sink_connect")) == 1, "no repeat within 30 s")
    c.t += 20.5
    await p.tick()
    check(len(of(sent, "sink_connect")) == 2, "a second request after 30 s")
    c.t += 29.6
    await p.tick()
    check(len(of(sent, "sink_connect")) == 2 and not p.link.connecting and p.audio_out() == ("headphones", "not connected"),
          f"after 60 s: stopped, 'not connected' ({len(of(sent, 'sink_connect'))} requests)")
    for _ in range(10):
        c.t += 30
        await p.tick()
    check(len(of(sent, "sink_connect")) == 2 and p.link.on, "never asks again by itself; Headphones stays chosen")
    await p.handle({"type": "sound_out", "mode": "headphones"})
    await p.tick()
    check(p.link.connecting and len(of(sent, "sink_connect")) == 3, "choosing Headphones again: one more minute")
    await p.handle(state(GOOD))
    await p.tick()
    check(not p.link.connecting and p.audio_out() == ("headphones", ""), "the headphones arrive: stops, status empty")
    await p.handle(state(OFF))
    for _ in range(4):
        c.t += 31
        await p.tick()
    check(len(of(sent, "sink_connect")) == 3, "lost later: no reconnect attempts (only the notice rules apply)")
    await p.handle({"type": "sound_out", "mode": "headphones"})
    await p.handle({"type": "sound_out", "mode": "hdmi"})
    check(not p.link.connecting, "HDMI ends the window in the same step (no tick needed)")
    c.t += 31
    await p.tick()
    check(not p.link.connecting and len(of(sent, "sink_connect")) == 3 and p.audio_out() == ("hdmi", ""),
          "HDMI ends the window at once")
    await p.set_audio_out("headphones")
    check(p.link.on and p.link.connecting, "the menu's set_audio_out uses the same switch")
    await p.set_audio_out("hdmi")

    # ---- the server link goes away
    p._send = None
    await p.link.connection_lost()
    check(p.link.target_text == "atv-audio: no connection to the server", "no server: the text says so")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
    print("ALL PASSED" if not failed else f"FAILED {failed}")
    sys.exit(1 if failed else 0)
