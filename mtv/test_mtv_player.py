#!/usr/bin/env python3
"""mtv_player.Player against a fake mpv (no mpv, no server): load -> loadfile with start/pause and the sound chain
first; file-loaded -> the item's audio track; play / pause / toggle / seek / stop; end of file -> one `ended`;
load error -> one `error`; sound live (af-command) vs for the next file; status reports; mpv restart replays.
    python3 test_mtv_player.py
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mtv_player as M  # noqa: E402

failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


class FakeMpv:
    def __init__(self, tracks=None):
        self.props: dict = {"pause": True}
        self.calls = []
        self.tracks = tracks or []
        self.entry = 0
        self.fail_load = False

    async def command(self, *args, timeout=5.0):
        self.calls.append(("cmd",) + args)
        if args[0] == "loadfile":
            if self.fail_load:
                raise M.MpvError("loading failed")
            self.entry += 1
            return {"playlist_entry_id": self.entry}
        if args[0] == "seek":
            self.props["time-pos"] = float(args[1])
            self.props["eof-reached"] = False
        return None

    async def get_property(self, name, default=None):
        if name == "track-list":
            return self.tracks
        return self.props.get(name, default)

    async def set_property(self, name, value):
        self.calls.append(("set", name, value))
        self.props[name] = value

    def sets(self, name):
        return [c[2] for c in self.calls if c[0] == "set" and c[1] == name]

    def cmds(self, name):
        return [c[1:] for c in self.calls if c[0] == "cmd" and c[1] == name]


def sound(gain, bands=None, processed=True):
    return {"type": "sound", "processed": processed, "gain": gain, "freqs": [31.25, 62.5, 125],
            "bands": bands or [0.0, 0.0, 0.0]}


AUDIO3 = [{"type": "video", "id": 1, "codec": "hevc", "demux-w": 3840, "demux-h": 2160},
          {"type": "audio", "id": 1, "selected": True}, {"type": "audio", "id": 2}, {"type": "audio", "id": 3}]


async def main():
    cfg = M.Config({"MTV_SERVER": "http://media.test:8080/", "MTV_DRM_MODE": "0", "MTV_DRM_CONNECTOR": "0.DP-1"})
    check(cfg.ws_url == "ws://media.test:8080/ws?role=player&kind=mtv", f"ws url ({cfg.ws_url})")
    check(cfg.media_url(7) == "http://media.test:8080/media/7", "media url = the original file")
    a = M.mpv_args(cfg, "/tmp/s")
    check("--gpu-context=drm" in a and "--drm-mode=0" in a and "--drm-connector=0.DP-1" in a
          and "--video-sync=display-resample" in a and "--interpolation" in a and "--idle=yes" in a
          and "--force-window=yes" in a and "--keep-open=yes" in a, "mpv args: drm, fixed mode, resample, interp, idle")
    check("--interpolation" not in M.mpv_args(M.Config({"MTV_SERVER": "x", "MTV_INTERPOLATION": "0"}), "/s"),
          "MTV_INTERPOLATION=0 drops interpolation")
    check(M.Config({"MTV_VOLUME": "250"}).volume == 100 and M.Config({"MTV_VOLUME": "x"}).volume == 100,
          "volume clamped / default 100")

    sent = []
    t = [0.0]
    p = M.Player(cfg, send=sent.append, mono=lambda: t[0])
    mpv = FakeMpv(AUDIO3)
    await p.attach(mpv)
    of = lambda kind: [m for m in sent if m.get("type") == kind]  # noqa: E731

    # ---- sound before the first load: written to 'af' (no file open)
    await p.handle(sound(4.0))
    check(mpv.sets("af") and "lavfi-volume=volume=4dB" in mpv.sets("af")[-1], "sound with no file: af chain set")

    # ---- load autoplay
    item = {"id": 7, "duration": 100, "audio_ord": 1}
    await p.handle({"type": "load", "item": item, "position": 12.5, "autoplay": True})
    check(mpv.cmds("loadfile")[-1][:4] == ("loadfile", "http://media.test:8080/media/7", "replace", -1), "loadfile url")
    check(mpv.cmds("loadfile")[-1][4] == "start=12.500,pause=no" and not mpv.sets("start"),
          f"start 12.5 + pause off as per-file options ({mpv.cmds('loadfile')[-1][4:]})")
    check(of("status")[-1] == {"type": "status", "item": 7, "playing": False, "position": 12.5, "duration": 100},
          "status right after load: not playing yet")
    await p._on_file_loaded(1)
    check(mpv.sets("aid")[-1:] == [2], f"audio_ord 1 -> aid 2 (of 3): {mpv.sets('aid')}")
    check(of("status")[-1]["playing"] is True, "file loaded + unpaused -> playing")
    check(any("hevc 3840x2160" in m.get("message", "") for m in of("log")), "log names codec and size")

    # ---- mpv paused by itself around the load (the race seen live): file-loaded unpauses when it should play
    mpv.calls.clear()
    mpv.props["pause"] = True
    await p._on_file_loaded(mpv.entry)
    check(mpv.sets("pause")[-1:] == [False], "file-loaded: mpv paused while the load wants play -> unpaused")
    mpv.calls.clear()
    await p._on_file_loaded(mpv.entry)
    check(not mpv.sets("pause"), "file-loaded: already playing -> pause untouched")

    # ---- an old file's file-loaded is ignored
    mpv.calls.clear()
    await p._on_file_loaded(99)
    check(not mpv.sets("aid"), "file-loaded of another entry ignored")

    # ---- live sound change: af-command, no chain rebuild
    mpv.calls.clear()
    await p.handle(sound(6.0, [0.0, 2.0, 0.0]))
    check(len(mpv.cmds("af-command")) == 2 and not mpv.sets("af"), "sound on a playing file: 2 af-commands, no af set")

    # ---- status throttling and position
    n = len(of("status"))
    mpv.props["time-pos"] = 20.0
    t[0] += 0.3
    await p.tick()
    check(len(of("status")) == n, "no status within 1 s when nothing changed")
    t[0] += 1.0
    await p.tick()
    check(of("status")[-1]["position"] == 20.0, "status every second with mpv's position")

    # ---- pause / toggle / play
    await p.handle({"type": "control", "action": "pause"})
    check(mpv.sets("pause")[-1] is True and of("status")[-1]["playing"] is False, "pause")
    await p.handle({"type": "control", "action": "toggle"})
    check(mpv.sets("pause")[-1] is False and of("status")[-1]["playing"] is True, "toggle -> play")

    # ---- seek
    await p.handle({"type": "control", "action": "seek", "position": 42})
    check(mpv.cmds("seek")[-1] == ("seek", 42.0, "absolute") and p.pos == 42.0, "seek absolute 42")

    # ---- end of file: exactly one ended
    mpv.props["eof-reached"] = True
    mpv.props["pause"] = True
    await p.tick()
    await p.tick()
    check([m["item"] for m in of("ended")] == [7], "eof -> one ended")
    check(of("status")[-1]["playing"] is False, "not playing after the end")
    # Play on the ended item: from the start
    await p.handle({"type": "control", "action": "play"})
    check(mpv.cmds("seek")[-1] == ("seek", 0, "absolute") and mpv.props["pause"] is False, "Play after the end -> from 0")

    # ---- next load paused (autoplay false) on a single-audio file
    mpv.tracks = [{"type": "audio", "id": 1, "selected": True}]
    mpv.calls.clear()
    await p.handle({"type": "load", "item": {"id": 8, "duration": 50}, "position": 0, "autoplay": False})
    await p._on_file_loaded(mpv.entry)
    check(mpv.cmds("loadfile")[-1][4].endswith("pause=yes") and not mpv.sets("aid"),
          "load paused (per-file option), single audio track left alone")
    check(of("status")[-1]["playing"] is False and of("status")[-1]["item"] == 8, "status: 8 paused")
    check(any("audio only" in m.get("message", "") for m in of("log")), "song: logged as audio only")
    mpv.props["eof-reached"] = True
    await p.tick()
    check([m["item"] for m in of("ended")] == [7, 8], "a new load can end again")

    # ---- load error: one error per load (loadfile fails, and an end-file error)
    mpv.fail_load = True
    await p.handle({"type": "load", "item": {"id": 9}, "position": 0, "autoplay": True})
    check([m["item"] for m in of("error")] == [9], "loadfile failure -> error")
    p.on_mpv_event({"event": "end-file", "reason": "error", "playlist_entry_id": None})
    check(len(of("error")) == 1, "same load: no second error")
    mpv.fail_load = False
    await p.handle({"type": "load", "item": {"id": 10}, "position": 0, "autoplay": True})
    p.on_mpv_event({"event": "end-file", "reason": "error", "playlist_entry_id": mpv.entry, "file_error": "404"})
    check([m["item"] for m in of("error")] == [9, 10], "end-file error of the current entry -> error")
    p.on_mpv_event({"event": "end-file", "reason": "error", "playlist_entry_id": mpv.entry - 1})
    check(len(of("error")) == 2, "end-file error of an old entry ignored")

    # ---- load with no item: stop
    await p.handle({"type": "load", "item": None, "position": 0, "autoplay": False})
    check(mpv.cmds("stop"), "load without item -> mpv stop")
    n = len(of("status"))
    await p.tick()
    check(len(of("status")) == n, "nothing loaded: no status")

    # ---- mpv restart replays the current item and the sound chain
    await p.handle({"type": "load", "item": {"id": 11, "duration": 9}, "position": 3, "autoplay": True})
    p.detach()
    mpv2 = FakeMpv()
    await p.attach(mpv2)
    check(mpv2.sets("af") and "volume=6dB" in mpv2.sets("af")[0], "restart: sound chain set first")
    check(mpv2.cmds("loadfile")[-1][1].endswith("/media/11") and mpv2.cmds("loadfile")[-1][4].endswith("pause=no"),
          "restart: current item reloaded, playing")

    # ---- volume: the web remote's slider sets mpv's volume + mute, kept in the state file, reported to the server
    import tempfile
    sd = tempfile.mkdtemp(prefix="mtvstate-")
    cfg2 = M.Config({"MTV_SERVER": "http://x", "MTV_STATE": sd, "MTV_VOLUME": "60"})
    sent2 = []
    p2 = M.Player(cfg2, send=sent2.append)
    check(p2.volume == 60 and not p2.muted, "no state file: MTV_VOLUME is the first volume")
    check("--volume=60" in M.mpv_args(cfg2, "/s", p2.volume, p2.muted) and "--mute=no" in M.mpv_args(cfg2, "/s", 60, False)
          and "--volume=25" in M.mpv_args(cfg2, "/s", 25, True) and "--mute=yes" in M.mpv_args(cfg2, "/s", 25, True),
          "mpv starts with the player's volume / mute")
    check("--volume=100" in M.mpv_args(M.Config({"MTV_SERVER": "x"}), "/s"), "default volume is 100 (no attenuation)")
    m2 = FakeMpv(AUDIO3)
    m2.props.update({"display-width": 3840, "display-height": 2160, "display-fps": 60.0})
    await p2.attach(m2)
    outs = [m for m in sent2 if m.get("type") == "output"]
    check(outs and outs[-1] == {"type": "output", "name": "TV Player", "screen": "3840x2160 @ 60 Hz",
                                "device": "alsa/hdmi", "volume": 60, "muted": False, "version": M.VERSION, "sound": "hdmi"},
          f"attach reports the output ({outs[-1:] })")
    await p2.handle({"type": "volume", "percent": 35.4, "muted": False})
    check(m2.sets("volume")[-1] == 35 and m2.sets("mute")[-1] is False, "volume message -> mpv volume 35")
    check([m for m in sent2 if m.get("type") == "output"][-1]["volume"] == 35, "... and the new volume is reported")
    await p2.handle({"type": "volume", "percent": 35, "muted": True})
    check(m2.sets("mute")[-1] is True and p2.muted, "mute -> mpv mute")
    await p2.handle({"type": "volume", "percent": 250, "muted": False})
    check(p2.volume == 100, "volume clamped to 100")
    await p2.handle({"type": "volume", "percent": "x"})
    check(p2.volume == 100, "garbage volume ignored")
    await p2.handle({"type": "volume", "percent": 0, "muted": False})
    p3 = M.Player(cfg2, send=sent2.append)
    check(p3.volume == 0 and not p3.muted, "volume kept across a restart (state file)")
    await p2.handle({"type": "volume", "percent": 40, "muted": True})
    p4 = M.Player(cfg2)
    check(p4.volume == 40 and p4.muted, "mute kept too")
    import shutil
    shutil.rmtree(sd, ignore_errors=True)

    # ---- sound off: empty chain
    await p.handle(sound(0, processed=False))
    check(mpv2.sets("af")[-1] == "", "unprocessed sound -> no filters")


if __name__ == "__main__":
    asyncio.run(main())
    print("ALL PASSED" if not failed else f"FAILED {failed}")
    sys.exit(1 if failed else 0)
