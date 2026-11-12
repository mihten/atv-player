#!/usr/bin/env python3
"""Volume keys and the menu's Volume row of mtv-player (fake mpv, fake clock, fake server):
  HDMI sound: CH+ / CH- stay next / previous; the menu's Volume row changes mpv's volume (Left / Right 5 %, OK mutes);
  headphones: CH+ / CH- are the headphones' volume (a `sink_volume` message for the server, mpv untouched), repeated presses
  add up before the sink reports back, the menu row shows and changes the headphones' volume, nothing is sent while the
  headphones are away; the volume keys never move the screensaver's or the menu's paging.
    python3 test_volume.py
"""
import asyncio
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mtv_player as M  # noqa: E402
from test_mtv_player import FakeMpv  # noqa: E402

failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


class Clock:
    def __init__(self):
        self.t = 500.0

    def __call__(self):
        return self.t


GOOD = {"connected": True, "name": "atv-audio", "route": True,
        "output": {"kind": "bluetooth", "name": "Headphones X", "codec": "aptx_hd", "ready": True, "volume": 40, "muted": False}}
AWAY = {"connected": True, "name": "atv-audio", "route": True,
        "output": {"kind": "bluetooth", "name": "Headphones X", "codec": None, "ready": False}}


def cmds(sent):
    return [m.get("cmd") for m in sent if m.get("type") == "cmd"]


def volumes(sent):
    return [m for m in sent if m.get("type") == "sink_volume"]


async def main():
    tmp = tempfile.mkdtemp()
    c = Clock()
    sent = []
    cfg = M.Config({"MTV_SERVER": "http://media.test:8080", "MTV_STATE": tmp})
    p = M.Player(cfg, send=sent.append, mono=c)
    mpv = FakeMpv()
    await p.attach(mpv)
    p.state = {"current": {"id": 5, "title": "T", "artist": "A"}, "playing": True, "queue": [5], "index": 0}
    p.volume = 60
    texts = []
    real_osd = M.osd

    async def osd(player, text, ms=1500):
        texts.append(text)
    M.osd = osd

    # ---- HDMI sound: the CH keys are mpv's volume (0.12.0; they were next / previous before)
    await M.on_key(p, "ch_up", False)
    check(p.volume == 65 and mpv.sets("volume")[-1] == 65 and not cmds(sent) and texts[-1] == "Volume 65 %",
          f"HDMI: CH+ = volume +5 % (65), no queue command, the screen says so ({texts[-1]})")
    await M.on_key(p, "ch_down", False)
    await M.on_key(p, "ch_down", False)
    check(p.volume == 55 and not cmds(sent) and not volumes(sent), "HDMI: CH- x2 = 55 %, nothing sent to the headphones")
    await M.on_key(p, "ch_up", False)

    # ---- the menu's Volume row (HDMI): mpv's volume
    await p.menu.show()
    labels = [r.label for r in p.menu.page.rows]
    check(labels[:4] == ["Lyrics screen", "Subtitles", "Audio output", "Volume"] and "Pause" not in labels and "Stop" not in labels,
          f"menu: Volume after Lyrics screen, Subtitles, Audio output, no Pause / Stop row ({labels[:4]})")
    vrow = p.menu.page.rows[3]
    check(vrow.sub == "60 %" and vrow.level, "the row shows 60 % and is a level row")
    p.menu.page.sel = 3
    drawn = p.menu.render()
    check("}◀ ▶" in drawn and "}adjust" in drawn and "}OK" in drawn and "}mute" in drawn,
          "on the row the hint chips say: ◀ ▶ adjust, OK mute")
    await M.on_key(p, "right", False)
    check(p.volume == 65 and mpv.sets("volume")[-1] == 65 and p.menu.page.rows[3].sub == "65 %", "Right: +5 %, mpv volume set, the row follows")
    check(p.menu.open and not cmds(sent)[2:], "the menu stays open and no queue command is sent")
    await M.on_key(p, "left", False)
    await M.on_key(p, "left", False)
    check(p.volume == 55 and p.menu.open and len(p.menu.pages) == 1, "Left x2: 55 % (Left does not go back on this row)")
    outs = [m for m in sent if m.get("type") == "output"]
    check(outs and outs[-1]["volume"] == 55, "the new volume is reported to the server (web remote follows)")
    await M.on_key(p, "ok", False)
    check(p.muted and mpv.sets("mute")[-1] is True and p.menu.page.rows[3].sub == "muted", "OK on the row: mute, shown as muted")
    await M.on_key(p, "right", False)
    check(not p.muted and p.volume == 60 and mpv.sets("mute")[-1] is False, "a step unmutes")
    await M.on_key(p, "ok", False)
    await M.on_key(p, "ok", False)
    check(not p.muted and p.menu.page.rows[3].sub == "60 %", "OK twice: back to 60 %")
    p.volume = 98
    await M.on_key(p, "right", False)
    await M.on_key(p, "right", False)
    check(p.volume == 100, "clamped at 100")
    p.volume = 3
    await M.on_key(p, "left", False)
    await M.on_key(p, "left", False)
    check(p.volume == 0, "clamped at 0")
    p.menu.page.sel = 0
    await M.on_key(p, "right", False)
    check(p.volume == 0 and p.menu.open, "Right on another row does not change the volume")
    p.menu.close()

    # ---- headphones: the CH keys are their volume
    sent.clear()
    p.link.on = True
    mpv_volume_sets = len(mpv.sets("volume"))
    await p.handle({"type": "state", "state": {"sink": GOOD, "current": {"id": 5}, "queue": [5]}})
    await M.on_key(p, "ch_up", False)
    check(volumes(sent) == [{"type": "sink_volume", "percent": 45, "muted": False}] and not cmds(sent), f"headphones: CH+ -> sink_volume 45, no 'next' ({volumes(sent)})")
    check(texts[-1] == "Headphones volume 45 %", f"the screen says so ({texts[-1]})")
    check(p.volume == 0 and len(mpv.sets("volume")) == mpv_volume_sets, "mpv's own volume is not touched")
    c.t += 0.3
    await M.on_key(p, "ch_up", False)
    await M.on_key(p, "ch_down", False)
    await M.on_key(p, "ch_down", False)
    check([v["percent"] for v in volumes(sent)] == [45, 50, 45, 40], f"quick presses add up before the sink reports ({[v['percent'] for v in volumes(sent)]})")
    c.t += 3
    await M.on_key(p, "ch_down", False)
    check(volumes(sent)[-1]["percent"] == 35, "after 2 s the sink's reported volume is the base again (40 -> 35)")
    # the menu row (the value just sent is shown for 2 s, then the sink's report counts again)
    c.t += 3
    sent.clear()
    await p.menu.show()
    row = p.menu.page.rows[3]
    check(row.label == "Headphones volume" and row.sub == "40 %", f"menu row: headphones' volume ({row.label}, {row.sub})")
    p.menu.page.sel = 3
    await M.on_key(p, "right", False)
    check(volumes(sent) == [{"type": "sink_volume", "percent": 45, "muted": False}], "menu Right on the row -> sink_volume 45")
    await M.on_key(p, "ok", False)
    check(volumes(sent)[-1] == {"type": "sink_volume", "percent": 45, "muted": True}, "menu OK on the row -> the headphones muted")
    # CH keys with the menu open page the menu (not the volume)
    n = len(volumes(sent))
    p.menu.page.sel = 0
    await M.on_key(p, "ch_down", False)
    check(len(volumes(sent)) == n and p.menu.open, "menu open: CH keys page the menu, not the volume")
    p.menu.close()

    # ---- headphones away: nothing is sent
    sent.clear()
    await p.handle({"type": "state", "state": {"sink": AWAY, "current": {"id": 5}, "queue": [5]}})
    await M.on_key(p, "ch_up", False)
    check(not volumes(sent) and texts[-1] == "Headphones not connected", "headphones away: CH+ says so, sends nothing")
    await p.menu.show()
    row = p.menu.page.rows[3]
    check(row.sub == "not connected", f"menu row: not connected ({row.sub})")
    p.menu.page.sel = 3
    await M.on_key(p, "right", False)
    await M.on_key(p, "ok", False)
    check(not volumes(sent), "menu: nothing sent while they are away")
    p.menu.close()

    # ---- back to HDMI: the CH keys are mpv's volume again
    p.link.on = False
    sent.clear()
    before = p.volume
    await M.on_key(p, "ch_up", False)
    check(not cmds(sent) and not volumes(sent) and p.volume == min(100, before + 5), "HDMI again: CH+ = mpv volume, no 'next'")

    # ---- the screensaver: a volume key only wakes it
    p.link.on = True
    await p.handle({"type": "state", "state": {"sink": GOOD, "current": {"id": 5}, "queue": [5]}})
    p.saver.on = True
    sent.clear()
    await M.on_key(p, "ch_up", False)
    check(not volumes(sent) and not p.saver.on, "a volume key while the screensaver is on only closes it")
    M.osd = real_osd
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
    print("ALL PASSED" if not failed else f"FAILED {failed}")
    sys.exit(1 if failed else 0)
