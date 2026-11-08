#!/usr/bin/env python3
"""remotes.py without a Flirc: a fake flirc_util (a stored table, refused duplicates, a press that never comes,
saveconfig / loadconfig on real files), learning a profile, switching, deleting, stopping, restoring, name checks, the
summary for the web remote and the TV screens.
    python3 test_remotes.py
"""
import asyncio
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import remotes as R  # noqa: E402

failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


class FakeProc:
    def __init__(self, out, rc=0, hang=False):
        self.out, self.returncode, self.hang, self.killed = out.encode(), rc, hang, False
        self.ev = asyncio.Event()

    async def communicate(self):
        if self.hang:
            await self.ev.wait()          # until killed
            self.returncode = -9
            return b"", None
        return self.out, None

    def kill(self):
        self.killed = True
        self.ev.set()

    async def wait(self):
        return 0


class FakeUtil:
    """Like flirc_util: `record` stores the next scripted press, saveconfig / loadconfig use real files."""
    def __init__(self, presses=(), table=("old1", "old2"), load_ok=True):
        self.presses, self.table, self.calls, self.load_ok = list(presses), list(table), [], load_ok

    async def __call__(self, util, *args, **kw):
        self.calls.append(args)
        cmd = args[0]
        if cmd == "saveconfig":
            json.dump(self.table, open(args[1] + ".fcfg", "w"))
            return FakeProc("Configuration File saved")
        if cmd == "format":
            self.table = []
            return FakeProc("Formatting Device, please wait...  Done!")
        if cmd == "loadconfig":
            if not self.load_ok:
                return FakeProc("[E] cannot load", rc=1)
            self.table = json.load(open(args[1]))
            return FakeProc("Configuration File Loaded Successfully")
        if cmd == "record":
            what = self.presses.pop(0) if self.presses else "ok"
            if what == "none":
                return FakeProc("", hang=True)
            if what == "dup":
                return FakeProc("[E] button already exists")
            self.table.append(args[1] + ("@REP" if what == "rep" else ""))   # "rep": only the repeat part was caught
            return FakeProc(f"Press any button on the remote to link it with '{args[1]}'\n Succesfully recorded button")
        if cmd == "settings":
            rows = "".join(f"{i:5d}  {'7C8BCB1E' if '@REP' in e else '%08X' % (0xA0000000 + i)}   130  01  {e.split('@')[0]}\n"
                           for i, e in enumerate(self.table))
            return FakeProc("FW Version: v4.7.0\n\nRecorded Keys:\nIndex  hash       IK   ID  key\n-----  --------   ---  --  ------------\n" + rows)
        if cmd == "delete_index":
            self.table.pop(int(args[1]))
            return FakeProc("deleted")
        raise AssertionError(args)


async def make(util, **kw):
    drawn = []
    tmp = tempfile.mkdtemp()

    async def changed():
        drawn.append((r.stage, r.index, r.note))

    r = R.Remotes("flirc_util", tmp, changed, lambda m: None, spawn=util, timeout=0.2, flash=0, result_for=0.3, **kw)
    return r, drawn, tmp


async def seed(r, name, table):
    os.makedirs(r.dir, exist_ok=True)
    json.dump(table, open(os.path.join(r.dir, name + ".fcfg"), "w"))


async def main():
    ALL = [k[0] for k in R.KEYS]
    N = len(R.KEYS)

    # ---- empty state
    u = FakeUtil()
    r, drawn, tmp = await make(u)
    s = r.summary()
    check(s["profiles"] == [] and s["active"] is None and s["progress"]["state"] == "idle", "start: no profiles, idle")
    check(r.render() == "" and not r.active_screen, "nothing drawn")

    # ---- name rules
    for bad in ("", " ", "../x", "a/b", "x" * 31, "-a", "a.b", None, "a;rm"):
        check(await r.command("learn", bad) == "bad name", f"bad name refused: {bad!r}")
    check(clean := R.clean_name("  My   Remote  ") == "My Remote", "spaces collapsed")
    check(u.calls == [], "bad names ran nothing")

    # ---- learn a profile: current table kept, one press per player function in order, saved, becomes active
    check(await r.command("learn", "Cambridge") is None, "learn starts")
    check(r.busy and r.summary()["progress"]["state"] == "learning", "summary: learning")
    await r.task
    keys = [c[1] for c in u.calls if c[0] == "record"]
    check(keys == ALL and N == 8, f"recorded {len(keys)} functions in order")
    check([k[1] for k in R.KEYS] == ["Play / Pause", "Menu", "Up", "Down", "Left", "Right", "Next", "Previous"], "the functions asked for: no digits, no colours")
    check(not any(len(k[1]) > 12 for k in R.KEYS), "labels fit the server's 12 characters")
    check(u.calls[0][0] == "saveconfig" and u.calls[1] == ("format",), "current table saved before the format")
    check(r.profiles() == ["Cambridge"] and r.active == "Cambridge", "profile saved and active")
    check(json.load(open(os.path.join(r.dir, "Cambridge.fcfg"))) == ALL, "the saved file holds the learned functions only")
    check(r.stage == "result" and r.ok and "8 functions learned" in r.text, "result: all learned")
    check("Done" in r.render() and "Learn remote: Cambridge" in r.render(), "done screen")
    check(await r.key("up") is True and not r.active_screen, "a key closes the result screen")
    check(r.summary()["progress"]["state"] == "idle" and "learned" in r.summary()["progress"]["text"], "summary idle + last result")

    # ---- the name exists
    check(await r.command("learn", "Cambridge") == "a profile with that name already exists", "duplicate name refused")

    # ---- a second profile from a seeded table; switching
    await seed(r, "Vip STB", ["a", "b", "c"])
    check(r.profiles() == ["Cambridge", "Vip STB"], "profiles sorted")
    u.table = list(ALL)
    check(await r.command("use", "Vip STB") is None, "use starts")
    await r.task
    check(u.table == ["a", "b", "c"] and r.active == "Vip STB", "switch: table loaded, active set")
    check("Now using Vip STB" in r.text and r.summary()["progress"]["state"] == "idle", "switch text, idle again")
    check(await r.command("use", "Nope") == "no such profile", "unknown profile refused")

    # ---- a switch that fails puts the previous table back
    u.load_ok = False
    u.table = ["keep1", "keep2"]
    await r.command("use", "Cambridge")
    await r.task
    u.load_ok = True  # the restore uses loadconfig too: with it failing both, the receiver would be empty (separate test)
    check(r.active == "Vip STB", "failed switch: active unchanged")
    check("Could not load" in r.text, f"failed switch reported ({r.text})")

    class FailOnce(FakeUtil):
        n = 0

        async def __call__(self, util, *args, **kw):
            if args[0] == "loadconfig":
                FailOnce.n += 1
                if FailOnce.n == 1:
                    self.calls.append(args)
                    return FakeProc("[E] bad file", rc=1)
            return await super().__call__(util, *args, **kw)
    u2 = FailOnce(table=["keep1", "keep2"])
    r2, d2, t2 = await make(u2)
    await seed(r2, "Broken", ["x"])
    await r2.command("use", "Broken")
    await r2.task
    check(u2.table == ["keep1", "keep2"] and "previous buttons are back" in r2.text, "failed switch: previous table restored")
    check(r2.active is None, "... and no profile became active")

    # ---- delete
    check(await r.command("delete", "Vip STB") == "that profile is in use - switch to another one first", "active profile cannot be deleted")
    check(await r.command("delete", "Cambridge") is None and r.profiles() == ["Vip STB"], "other profile deleted")
    check(await r.command("delete", "Cambridge") == "no such profile", "deleting twice refused")

    # ---- refused press -> same button again; five refusals -> stop + restore
    u = FakeUtil(["ok", "dup", "ok"])
    r, drawn, tmp = await make(u)
    await r.command("learn", "T")
    await r.task
    keys = [c[1] for c in u.calls if c[0] == "record"]
    check(keys[:3] == ["return", "F4", "F4"] and r.ok, "refused press -> same button asked again, run completes")
    check(any("already used" in d[2] for d in drawn), "screen says the button is already used")
    u = FakeUtil(["dup"] * 10)
    r, drawn, tmp = await make(u)
    await r.command("learn", "T")
    await r.task
    check(not r.ok and u.table == ["old1", "old2"] and r.profiles() == [] and r.active is None, "5 refusals: stopped, old table back, no profile")

    # ---- timeout in the middle: killed, old table back, nothing saved
    u = FakeUtil(["ok", "ok", "ok", "none"])
    r, drawn, tmp = await make(u)
    await r.command("learn", "T")
    await r.task
    check(not r.ok and "No button pressed" in r.text and u.table == ["old1", "old2"], "no press: stopped, old buttons back")
    check(r.profiles() == [] and r.restored is True and "(the old buttons work again)" in r.render(), "nothing saved, screen says so")
    await asyncio.sleep(0.5)
    check(not r.active_screen, "the result screen closes on its own")

    # ---- Cancel from the phone while a press is awaited
    u = FakeUtil(["ok", "none"])
    r, drawn, tmp = await make(u)
    r.timeout = 30
    await r.command("learn", "T")
    await asyncio.sleep(0.1)
    check(r.index == 1 and r.busy, "waiting for the 2nd press")
    await r.command("cancel")
    await asyncio.wait_for(r.task, 5)
    check(not r.ok and r.text.startswith("Stopped") and u.table == ["old1", "old2"], f"cancel: stopped, old table back ({r.text})")
    check(r.profiles() == [] and r.summary()["progress"]["state"] == "idle", "cancel: nothing saved, idle")
    check(r.restored is True and "old buttons restored" in r.text, f"cancel: the restore itself succeeded ({r.restored}, {r.text})")

    # ---- busy: other commands refused while learning
    u = FakeUtil(["none"])
    r, drawn, tmp = await make(u)
    r.timeout = 30
    await r.command("learn", "A")
    await asyncio.sleep(0.05)
    check(await r.command("learn", "B") == "busy" and await r.command("use", "A") == "busy", "learn / use refused while busy")
    await r.command("cancel")
    await asyncio.wait_for(r.task, 5)

    # ---- restore fails too -> says so
    u = FakeUtil(["none"], load_ok=False)
    r, drawn, tmp = await make(u)
    await r.command("learn", "T")
    await r.task
    check(r.restored is False and "RESTORE FAILED" in r.text and "Choose a profile on the phone" in r.render(), "failed restore reported, screen points to the phone")

    # ---- the old table cannot be saved -> nothing touched
    class NoSave(FakeUtil):
        async def __call__(self, util, *args, **kw):
            self.calls.append(args)
            return FakeProc("[E] fail", rc=1)
    u = NoSave()
    r, drawn, tmp = await make(u)
    await r.command("learn", "T")
    await r.task
    check(not r.ok and ("format",) not in u.calls and "nothing was changed" in r.text, "save fails -> no format, nothing changed")

    # ---- the profile file cannot be written at the end
    class NoFinalSave(FakeUtil):
        async def __call__(self, util, *args, **kw):
            if args[0] == "saveconfig" and not args[1].endswith(".current"):
                self.calls.append(args)
                return FakeProc("[E] fail", rc=1)
            return await super().__call__(util, *args, **kw)
    u = NoFinalSave()
    r, drawn, tmp = await make(u)
    await r.command("learn", "T")
    await r.task
    check(not r.ok and r.profiles() == [] and u.table == ["old1", "old2"], "final save fails -> old table back, no profile")

    # ---- a crash in the middle of a learn is repaired at the next start
    pend = lambda rr: os.path.exists(os.path.join(rr.dir, ".pending"))
    u = FakeUtil(["ok", "ok", "none"])
    r, drawn, tmp = await make(u)
    r.timeout = 30
    await r.command("learn", "T")
    await asyncio.sleep(0.1)
    check(pend(r) and u.table == ["return", "F4"], "learn running: marker set, table half filled")
    r.task.cancel()                      # the process dies here (no restore runs)
    await asyncio.sleep(0.05)
    check(pend(r), "after the crash the marker is still there")
    r2 = R.Remotes("flirc_util", tmp, lambda: asyncio.sleep(0), lambda m: None, spawn=u, timeout=0.2, flash=0)
    check(await r2.recover() is True, "next start: recover() runs")
    check(u.table == ["old1", "old2"] and not pend(r2), "table back, marker gone")
    check("undone" in r2.text, f"text says so ({r2.text})")
    u.calls.clear()
    check(await r2.recover() is None and u.calls == [], "recover() with no marker does nothing")

    # ---- recover whose load fails keeps the marker
    u = FakeUtil(["ok", "none"])
    r, drawn, tmp = await make(u)
    r.timeout = 30
    await r.command("learn", "T")
    await asyncio.sleep(0.1)
    r.task.cancel()
    await asyncio.sleep(0.05)
    u.load_ok = False
    r2 = R.Remotes("flirc_util", tmp, lambda: asyncio.sleep(0), lambda m: None, spawn=u, timeout=0.2, flash=0)
    check(await r2.recover() is False and pend(r2) and "could not be undone" in r2.text, "failed recover keeps the marker and says so")

    # ---- the marker is cleared after a normal finish, kept after a failed restore
    u = FakeUtil()
    r, drawn, tmp = await make(u)
    await r.command("learn", "T")
    await r.task
    check(not pend(r), "marker cleared after a finished learn")
    u = FakeUtil(["none"], load_ok=False)
    r, drawn, tmp = await make(u)
    await r.command("learn", "T")
    await r.task
    check(pend(r), "failed restore: marker kept for the next start")
    u = FakeUtil(["none"])
    r, drawn, tmp = await make(u)
    await r.command("learn", "T")
    await r.task
    check(not pend(r), "stopped learn with a working restore: marker cleared")

    # ---- shutdown during a learn: stopped and undone
    u = FakeUtil(["ok", "none"])
    r, drawn, tmp = await make(u)
    r.timeout = 30
    await r.command("learn", "T")
    await asyncio.sleep(0.1)
    await asyncio.wait_for(r.shutdown(), 10)
    check(u.table == ["old1", "old2"] and not pend(r) and not r.busy, "shutdown: learn stopped, old table back, marker gone")
    await r.shutdown()
    check(True, "shutdown when idle returns")

    # ---- a held button: the next one is not listened for until the receiver is silent (its tail would be refused)
    import time as _t
    last = {"t": -1e9}
    stamps = []

    class Holder(FakeUtil):
        async def __call__(self, util, *args, **kw):
            if args[0] == "record":
                stamps.append(("start", _t.monotonic()))
            proc = await super().__call__(util, *args, **kw)
            if args[0] == "record" and b"recorded" in proc.out:
                stamps.append(("stored", _t.monotonic()))

                async def hold():             # the remote keeps sending for 0.5 s after the first signal
                    end = _t.monotonic() + 0.5
                    while _t.monotonic() < end:
                        last["t"] = _t.monotonic()
                        await asyncio.sleep(0.05)
                asyncio.ensure_future(hold())
            return proc
    u = Holder()
    drawn = []
    tmp = tempfile.mkdtemp()

    async def ch():
        drawn.append(r.note)
    r = R.Remotes("flirc_util", tmp, ch, lambda m: None, spawn=u, timeout=2, flash=0, result_for=0.3,
                  quiet_for=lambda: _t.monotonic() - last["t"], quiet=0.3, quiet_max=3, settle=0.1)
    await r.command("learn", "Held")
    await asyncio.wait_for(r.task, 60)
    gaps = [b2[1] - a2[1] for a2, b2 in zip(stamps, stamps[1:]) if a2[0] == "stored" and b2[0] == "start"]
    check(r.ok and len(gaps) == N - 1, f"held buttons: all learned ({len(gaps)} gaps)")
    check(min(gaps) >= 0.75, f"next button listened for only after silence (shortest gap {min(gaps):.2f} s >= 0.5 hold + 0.3 quiet)")
    check("Release the button" in drawn, "the screen asks to release the button while waiting")
    check(r.note == "" or r.stage == "result", "hint cleared afterwards")

    # ---- a refusal right after a signal = the previous button's tail: not counted, not shown, retried
    u = FakeUtil(["ok", "dup", "dup", "dup", "dup", "dup", "dup", "dup", "ok"])
    r, drawn, tmp = await make(u, quiet_for=lambda: 0.5, quiet=0.3, settle=0)
    await r.command("learn", "S")
    await r.task
    check(r.ok and not any("already used" in d[2] for d in drawn), "7 stray refusals: run completes, never told 'already used'")
    u = FakeUtil(["ok", "dup", "dup", "dup", "dup", "dup", "ok"])
    r, drawn, tmp = await make(u, quiet_for=lambda: 100.0, quiet=0.3, settle=0)
    await r.command("learn", "S")
    await r.task
    check(not r.ok and "Too many refused" in r.text, "refusals long after any signal are real duplicates and count")
    u = FakeUtil(["ok", "dup", "dup", "dup", "dup", "dup", "ok"])
    r, drawn, tmp = await make(u, quiet_for=lambda: 0.5, quiet=0.3, settle=0)
    await r.command("learn", "S")
    await r.task
    check(r.ok, "the same 5 refusals right after a signal are strays: completes")

    # ---- skipping the optional functions (a small remote)
    u = FakeUtil(["ok"] * 6 + ["none", "none"])
    r, drawn, tmp = await make(u)
    r.timeout = 30
    await r.command("learn", "Small")
    await asyncio.sleep(0.15)
    check(r.index == 6 and r.busy and r.summary()["progress"]["optional"] is True and r.summary()["progress"]["label"] == "Next",
          "at Next: optional, reported to the phone")
    check(await r.command("skip") is None, "skip accepted for an optional function")
    await asyncio.sleep(0.2)
    check(r.index == 7 and r.summary()["progress"]["label"] == "Previous", "skipped: now asking for Previous")
    check(await r.command("skip") is None, "Previous can be skipped too")
    await asyncio.wait_for(r.task, 10)
    check(r.ok and "6 functions learned, 2 skipped" in r.text, f"done with two skipped ({r.text})")
    check(u.table == ALL[:6] and r.profiles() == ["Small"], "the profile holds the 6 learned functions")
    u = FakeUtil(["none"])
    r, drawn, tmp = await make(u)
    r.timeout = 30
    await r.command("learn", "X")
    await asyncio.sleep(0.1)
    check(await r.command("skip") == "this function cannot be skipped" and r.index == 0, "a required function cannot be skipped")
    await r.command("cancel")
    await asyncio.wait_for(r.task, 5)
    check(await r.command("skip") == "this function cannot be skipped", "skip when nothing is learning is refused")

    # ---- only the repeat part of a signal was caught (the TotalTV "Down" that fired for every unknown key)
    u = FakeUtil(["ok", "ok", "ok", "rep", "ok"])
    r, drawn, tmp = await make(u)
    await r.command("learn", "Rep")
    await r.task
    keys = [c[1] for c in u.calls if c[0] == "record"]
    check(r.ok and keys[3:5] == ["down", "down"], f"a repeat-only capture is asked for again ({keys[2:6]})")
    check(("delete_index", "3") in u.calls, "... and removed from the receiver")
    check(u.table == ALL and not any("@REP" in e for e in u.table), "the saved table holds only real buttons")
    check(any("Only part of the signal" in d[2] for d in drawn), "the screen says only part of the signal was caught")
    u = FakeUtil(["rep"] * 20)
    r, drawn, tmp = await make(u)
    await r.command("learn", "Rep2")
    await r.task
    check(not r.ok and "only part of the signal" in r.text, f"endless repeat-only captures end the run ({r.text})")
    check(u.table == ["old1", "old2"] and r.profiles() == [], "... old buttons back, nothing saved, no bad entry kept")

    # ---- switching to a profile that holds a repeat-only row warns about it
    u = FakeUtil()
    r, drawn, tmp = await make(u)
    await seed(r, "Bad", ["return", "down@REP", "up"])
    await seed(r, "Good", ["return", "down", "up"])
    await r.command("use", "Bad")
    await r.task
    check("down button caught only the repeat part" in r.text and r.active == "Bad", f"warning on a bad profile ({r.text[:80]})")
    await r.command("use", "Good")
    await r.task
    check(r.text == "Now using Good", "no warning on a good one")

    # ---- ASS
    check(R.esc("a{b}\\c") == "a\\{b\\}\\\u200bc", "escape")
    u = FakeUtil()
    r, drawn, tmp = await make(u)
    r.stage, r.index, r.name = "run", 7, "X"
    ass = r.render()
    check("Previous" in ass and "8 / 8" in ass and "Press the button you want for" in ass, "run screen names the function and the count")
    r.index = 6
    ass = r.render()
    check("Next" in ass and "Skip on the phone" in ass, "an optional function mentions Skip")
    r.index = 1
    check("Skip on the phone" not in r.render() and "opens and closes the menu" in r.render(), "a required one does not; the hint says what it does")

    print("FAILED" if failed else "ALL PASS", failed)
    return failed

sys.exit(1 if asyncio.run(main()) else 0)
