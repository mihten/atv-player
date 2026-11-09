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


async def autotest(r):
    """The user at the remote for the test stage: step 1 presses every learned function, step 2 presses Menu."""
    d1 = d2 = False
    while True:
        await asyncio.sleep(0.01)
        if r.phase == "test1" and not d1:
            d1 = True
            for i in list(r.learned):
                await r.key(R.PLAYER_KEY[R.KEYS[i][0]])
        elif r.phase == "test2" and not d2:
            d2 = True
            await r.key("blue" if 1 in r.learned else "ok")    # Menu ends step 2; Play / Pause when Menu is not set
        elif r.phase is None:
            d1 = d2 = False


async def make(util, auto=True, **kw):
    drawn = []
    tmp = tempfile.mkdtemp()

    async def changed():
        drawn.append((r.stage, r.index, r.note))

    kw.setdefault("test_idle", 0.3)
    kw.setdefault("retry_wait", 0.0)
    kw.setdefault("review_for", 0.5)
    r = R.Remotes("flirc_util", tmp, changed, lambda m: None, spawn=util, timeout=0.2, flash=0, result_for=0.3, **kw)
    if auto:
        r._auto = asyncio.ensure_future(autotest(r))
    return r, drawn, tmp


async def until(cond, t=5.0):
    end = asyncio.get_running_loop().time() + t
    while not cond():
        if asyncio.get_running_loop().time() > end:
            return False
        await asyncio.sleep(0.01)
    return True


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
    check(s["profiles"] == [] and s["active"] == [] and s["progress"]["state"] == "idle", "start: no profiles, none in use, idle")
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
    check(keys == ALL and N == 10, f"recorded {len(keys)} functions in order")
    check([k[1] for k in R.KEYS] == ["Play / Pause", "Menu", "Up", "Down", "Left", "Right", "Next", "Previous", "Back", "Stop"], "the functions asked for: no digits, no colours")
    check(not any(len(k[1]) > 12 for k in R.KEYS), "labels fit the server's 12 characters")
    check(u.calls[0][0] == "saveconfig" and u.calls[1] == ("format",), "current table saved before the format")
    check(r.profiles() == ["Cambridge"] and r.active == "Cambridge", "profile saved and active")
    check(json.load(open(os.path.join(r.dir, "Cambridge.fcfg"))) == ALL, "the saved file holds the learned functions only")
    check(r.stage == "result" and r.ok and "10 functions learned" in r.text, "result: all learned")
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
    check("Could not switch" in r.text, f"failed switch reported ({r.text})")

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
    check(await r.command("delete", "Vip STB") == "that profile is in use - turn it off first", "active profile cannot be deleted")
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
    held_notes = []
    tmp = tempfile.mkdtemp()

    async def ch():
        held_notes.append(r.note)
    r = R.Remotes("flirc_util", tmp, ch, lambda m: None, spawn=u, timeout=2, flash=0, result_for=0.3,
                  quiet_for=lambda: _t.monotonic() - last["t"], quiet=0.3, quiet_max=3, settle=0.1, test_idle=0.3)
    r._auto = asyncio.ensure_future(autotest(r))
    await r.command("learn", "Held")
    await asyncio.wait_for(r.task, 60)
    gaps = [b2[1] - a2[1] for a2, b2 in zip(stamps, stamps[1:]) if a2[0] == "stored" and b2[0] == "start"]
    check(r.ok and len(gaps) == N - 1, f"held buttons: all learned ({len(gaps)} gaps)")
    check(min(gaps) >= 0.75, f"next button listened for only after silence (shortest gap {min(gaps):.2f} s >= 0.5 hold + 0.3 quiet)")
    check("Release the button" in held_notes, "the screen asks to release the button while waiting")
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

    # ---- the first settings / saveconfig call of a fresh flirc_util process fails once: tried again, the learn goes through
    class FlakyFirst(FakeUtil):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.failed = set()

        async def __call__(self, util, *args, **kw):
            if args[0] in ("settings", "saveconfig") and args[0] not in self.failed:
                self.failed.add(args[0])
                self.calls.append(args)
                return FakeProc("[E] could not open the device", rc=1)
            return await super().__call__(util, *args, **kw)
    u = FlakyFirst()
    r, drawn, tmp = await make(u)
    check(await r.command("learn", "Flaky") is None, "learn starts")
    await asyncio.wait_for(r.task, 15)
    check(r.ok and r.profiles() == ["Flaky"] and u.failed == {"settings", "saveconfig"},
          f"a failing first saveconfig / settings is tried again; the learn completes ({r.text})")
    class AlwaysFails(FakeUtil):
        async def __call__(self, util, *args, **kw):
            if args[0] == "saveconfig":
                self.calls.append(args)
                return FakeProc("[E] no device", rc=1)
            return await super().__call__(util, *args, **kw)
    u = AlwaysFails()
    r, drawn, tmp = await make(u)
    await r.command("learn", "Never")
    await asyncio.wait_for(r.task, 15)
    check(not r.ok and len([c for c in u.calls if c[0] == "saveconfig"]) == R.RETRIES and not any(c[0] == "format" for c in u.calls),
          f"a save that keeps failing: {R.RETRIES} tries, then the learn stops BEFORE touching the receiver ({r.text})")
    u = FakeUtil()
    check(await FakeUtil.__call__(u, "x", "record", "a") is not None, "fixture ok")
    class FlakyFormat(FakeUtil):
        async def __call__(self, util, *args, **kw):
            if args[0] == "format":
                self.calls.append(args)
                return FakeProc("[E] x", rc=1)
            return await super().__call__(util, *args, **kw)
    u = FlakyFormat()
    r, drawn, tmp = await make(u)
    await r._util("format")
    check(len([c for c in u.calls if c[0] == "format"]) == 1, "format is never repeated (not idempotent for the table)")

    # ---- skipping functions from the phone (a small remote)
    u = FakeUtil(["ok"] * 6 + ["none", "none", "none", "none"])
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
    await asyncio.sleep(0.2)
    check(r.index == 8 and r.summary()["progress"]["label"] == "Back", "now asking for Back")
    check(await r.command("skip") is None, "Back can be skipped too")
    await asyncio.sleep(0.2)
    check(r.index == 9 and r.summary()["progress"]["label"] == "Stop" and r.summary()["progress"]["optional"] is True,
          "now asking for Stop (optional)")
    check(await r.command("skip") is None, "Stop can be skipped too")
    await asyncio.wait_for(r.task, 10)
    check(r.ok and "6 functions learned, 4 not set" in r.text, f"done with four not set ({r.text})")
    check(u.table == ALL[:6] and r.profiles() == ["Small"], "the profile holds the 6 learned functions")
    u = FakeUtil(["none"])
    r, drawn, tmp = await make(u)
    r.timeout = 30
    await r.command("learn", "X")
    await asyncio.sleep(0.1)
    check(await r.command("skip") == "this function cannot be skipped" and r.index == 0, "Play / Pause cannot be skipped")
    await r.command("cancel")
    await asyncio.wait_for(r.task, 5)
    u = FakeUtil(["ok", "none", "ok"])
    r, drawn, tmp = await make(u)
    r.timeout = 30
    await r.command("learn", "Y")
    await asyncio.sleep(0.1)
    check(r.index == 1 and r.summary()["progress"]["optional"] is True, "Menu: skippable, reported to the phone")
    check(await r.command("skip") is None, "every function after Play / Pause can be skipped from the phone")
    await asyncio.sleep(0.1)
    check(1 in r.skipped and 1 not in r.learned and r.index >= 2, "Menu left out, the run goes on")
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
    check("down is only the repeat part of a signal" in r.text and r.active == "Bad", f"warning on a bad profile ({r.text[:80]})")
    await r.command("use", "Good")
    await r.task
    check(r.text == "Now using Good", "no warning on a good one")

    # ---- the test stage: pass
    u = FakeUtil()
    r, drawn, tmp = await make(u, auto=False, test_idle=3)
    shots = {}
    await r.command("learn", "Pass")
    check(await until(lambda: r.phase == "test1"), "after the last button the test starts")
    check(r.stage == "test" and r.summary()["progress"]["state"] == "testing" and r.summary()["progress"]["phase"] == "test1",
          "summary: testing, step 1")
    shots[1] = r.render()
    check("Test remote: Pass" in shots[1] and "Test 1 / 2" in shots[1] and "Press each button you assigned" in shots[1], "TV: step 1 text")
    check(r.summary()["progress"]["expected"] == [k[1] for k in R.KEYS] and r.summary()["progress"]["lit"] == [], "nothing lit yet")
    await r.key("ok"); await r.key("up")
    check(set(r.summary()["progress"]["lit"]) == {"Play / Pause", "Up"} and "✓  Play / Pause" in r.render(), "pressed functions light up")
    for k in ("blue", "down", "left", "right", "ch_up", "ch_down", "back", "stop"):
        await r.key(k)
    check(await until(lambda: r.phase == "test2"), "all lit -> step 2")
    shots[2] = r.render()
    check("Test 2 / 2" in shots[2] and "did NOT assign" in shots[2] and "press your Menu button" in shots[2], "TV: step 2 text")
    t0 = asyncio.get_running_loop().time()
    await r.key("blue")
    await asyncio.wait_for(r.task, 5)
    check(asyncio.get_running_loop().time() - t0 < 1.5, "the Menu button ends step 2 at once (not after the idle time)")
    check(r.ok and r.text.endswith("test passed") and r.profiles() == ["Pass"] and r.active == "Pass", f"passed -> saved and active ({r.text})")
    check(r.summary()["progress"]["state"] == "idle", "idle after")

    # ---- a function never answers: step 1 times out, review, Discard restores
    u = FakeUtil()
    r, drawn, tmp = await make(u, auto=False)
    await r.command("learn", "Miss")
    await until(lambda: r.phase == "test1")
    for k in ("ok", "blue", "down", "left", "right", "ch_up", "ch_down", "back", "stop"):   # Up is never pressed (or never arrives)
        await r.key(k)
    check(await until(lambda: r.phase == "test2", 3), "silence in step 1 ends it after the idle time")
    await r.key("blue")
    check(await until(lambda: r.phase == "review"), "a missing function -> review")
    p = r.summary()["progress"]
    check(p["state"] == "review" and p["problems"] == ["no answer from Up"], f"problem named ({p['problems']})")
    check("Test failed" in r.render() and "no answer from Up" in r.render() and "Keep or Discard" in r.render(), "TV: review screen")
    check(await r.command("discard") is None, "Discard accepted")
    await asyncio.wait_for(r.task, 5)
    check(not r.ok and "Discarded" in r.text and u.table == ["old1", "old2"] and r.profiles() == [] and r.active is None,
          f"discard: old buttons back, nothing saved ({r.text[:90]})")

    # ---- a button fires a function it was not assigned (step 2): review, Keep saves it anyway
    u = FakeUtil()
    r, drawn, tmp = await make(u, auto=False)
    await r.command("learn", "Stray")
    await until(lambda: r.phase == "test1")
    for k in ("ok", "blue", "up", "down", "left", "right", "ch_up", "ch_down", "back", "stop"):
        await r.key(k)
    await until(lambda: r.phase == "test2")
    await r.key("down"); await r.key("down"); await r.key("green")   # Green and Yellow acting as Down (the TotalTV case)
    check(r.summary()["progress"]["strays"] == ["Down x2", "green"] and "Fired without being assigned" in r.render(), "strays listed live")
    await r.key("blue")
    check(await until(lambda: r.phase == "review"), "strays -> review")
    check("fired without being assigned: Down x2, green" in r.summary()["progress"]["problems"][0], "problem names what fired")
    check(await r.command("keep") is None, "Keep accepted")
    await asyncio.wait_for(r.task, 5)
    check(r.ok and "kept although the test found problems" in r.text and r.profiles() == ["Stray"], f"keep: saved with the warning ({r.text[:90]})")

    # ---- nobody answers on the phone: discard after the review time
    u = FakeUtil()
    r, drawn, tmp = await make(u, auto=False, review_for=0.3)
    await r.command("learn", "Quiet")
    await asyncio.wait_for(r.task, 10)
    check(not r.ok and "No answer from the phone" in r.text and u.table == ["old1", "old2"], f"no decision -> discarded ({r.text[-70:]})")

    # ---- Cancel during the test
    u = FakeUtil()
    r, drawn, tmp = await make(u, auto=False, test_idle=30)
    await r.command("learn", "Cx")
    await until(lambda: r.phase == "test1")
    await r.command("cancel")
    await asyncio.wait_for(r.task, 5)
    check(not r.ok and r.text.startswith("Stopped") and u.table == ["old1", "old2"] and r.profiles() == [], "cancel in the test: stopped, old table back")

    # ---- keep / discard outside a review
    check(await r.command("keep") == "nothing to decide" and await r.command("discard") == "nothing to decide", "keep / discard only in a review")

    # ---- a skipped function's key firing counts as a stray in step 1
    u = FakeUtil(["ok"] * 6 + ["none", "none", "none", "none"])
    r, drawn, tmp = await make(u, auto=False, test_idle=0.4)
    r.timeout = 30
    await r.command("learn", "Skp")
    await asyncio.sleep(0.15)
    for _ in range(4):
        await r.command("skip")
        await asyncio.sleep(0.2)
    await until(lambda: r.phase == "test1")
    check(r.summary()["progress"]["expected"] == [k[1] for k in R.KEYS[:6]], "only the learned functions are expected")
    await r.key("ch_up")
    check(r.summary()["progress"]["strays"] == ["Next"], "Next was skipped but its key fired: a stray")
    await r.command("cancel")
    await asyncio.wait_for(r.task, 5)

    # ---- several remotes at once: one combined table (binary tables, like the real receiver's files)
    import struct as _st

    def mkrow(h, hid, ik=130):
        return _st.pack("<I", int(h, 16)) + bytes([ik]) + b"\x00" * 8 + b"\x01\x00" + bytes([hid])

    def mkfile(path, rows):
        head = bytearray(b"\xef\xbe\xad\xde\x01\x00\x00\x00" + _st.pack("<I", len(rows)) + b"\x11\x22\x33\x44" + b"\xff\xff\xff\xff\x0f" + b"\x00" * 7)
        open(path, "wb").write(bytes(head) + b"".join(rows))

    HIDS = R.HID

    NAMES = {v: k for k, v in R.HID.items()}

    class BinFake:
        """flirc_util with real binary tables: loadconfig REPLACES the table (measured), record adds one row; a signal
        the table already holds is refused like the real receiver ("Error, button exists"); `settings` names the keys.
        Press scripts: "ok" = the button meant for the function, "okbtn" = the remote's Play / Pause button again,
        "none" = no press."""
        def __init__(self, salt, presses=(), accept=True, drop=False):
            self.salt, self.presses, self.table, self.calls, self.accept, self.drop = salt, list(presses), [], [], accept, drop
            self.only_merged_refused = self.refuse_full = False
            self.at_record = []
            self.table = [mkrow("0000%04X" % i, 1 + i) for i in range(2)]      # the "old" buttons

        async def __call__(self, util, *args, **kw):
            self.calls.append(args)
            c = args[0]
            if c == "saveconfig":
                mkfile(args[1] + ".fcfg", self.table)
                return FakeProc("Configuration File saved")
            if c == "format":
                self.table = []
                return FakeProc("Done")
            if c == "loadconfig":
                if not self.accept or (self.only_merged_refused and args[1].endswith(".merged.fcfg")) or \
                        (self.refuse_full and args[1].endswith("-full.fcfg")):
                    return FakeProc("[E] bad", rc=1)
                _h, rows = R.read_fcfg(args[1])
                lose = self.drop and args[1].endswith(".merged.fcfg")   # the receiver silently loses a row of a combined table
                self.table = rows[:-1] if lose else rows
                return FakeProc("Configuration File Loaded Successfully")
            if c == "record":
                what = self.presses.pop(0) if self.presses else "ok"
                if what == "none":
                    return FakeProc("", hang=True)
                self.at_record.append([R.row_hash(x) for x in self.table])
                h = "%08X" % (zlib.crc32((self.salt + ("return" if what == "okbtn" else args[1])).encode()) & 0xffffffff)
                if h in [R.row_hash(x) for x in self.table]:
                    return FakeProc("Error, button exists")
                self.table.append(mkrow(h, HIDS[args[1]]))
                return FakeProc("Succesfully recorded button")
            if c == "settings":
                rows = "".join(f"{i:5d}  {R.row_hash(r)}   130  01  {NAMES.get(r[15], 'k%02x' % r[15])}\n" for i, r in enumerate(self.table))
                return FakeProc("Recorded Keys:\nIndex  hash       IK   ID  key\n-----  --------   ---  --  ------------\n" + rows)
            if c == "delete_index":
                self.table.pop(int(args[1]))
                return FakeProc("deleted")
            raise AssertionError(args)

    import zlib
    A_ROWS = [mkrow("AAAA0001", 0x28), mkrow("AAAA0002", 0x3d), mkrow("AAAA0003", 0x52)]
    B_ROWS = [mkrow("BBBB0001", 0x28), mkrow("BBBB0002", 0x51), mkrow("BBBB0003", 0x50)]

    # merge_tables itself
    hd = b"\xef\xbe\xad\xde" + b"\x00" * 24
    head, rows, notes = R.merge_tables([("A", hd, A_ROWS), ("B", hd, B_ROWS)])
    check(len(rows) == 6 and not notes and _st.unpack_from("<I", head, 8)[0] == 6, "merge: all rows kept, count in the header fixed")
    head, rows, notes = R.merge_tables([("A", hd, A_ROWS), ("B", hd, A_ROWS)])
    check(len(rows) == 3 and not notes, "merge: the same signal and key twice = one row")
    head, rows, notes = R.merge_tables([("A", hd, A_ROWS), ("B", hd, [mkrow("AAAA0001", 0x51)])])
    check(len(rows) == 3 and len(notes) == 1 and "Play / Pause" in notes[0] and "A wins" in notes[0], f"merge: conflicting key, first wins, noted ({notes})")
    head, rows, notes = R.merge_tables([("A", hd, A_ROWS), ("B", hd, [mkrow("7C8BCB1E", 0x51), mkrow("BBBB0009", 0x4f)])])
    check(len(rows) == 4 and any("repeat" in n for n in notes) and all(R.row_hash(r) != "7C8BCB1E" for r in rows), "merge: the NEC repeat frame is left out")
    try:
        open(tmp := tempfile.mktemp(), "wb").write(b"junk")
        R.read_fcfg(tmp)
        check(False, "read_fcfg refuses a non-table")
    except ValueError:
        check(True, "read_fcfg refuses a non-table")
    open(tmp, "wb").write(open(tmp, "rb").read() + b"")
    mkfile(tmp, A_ROWS)
    open(tmp, "ab").write(b"x")
    try:
        R.read_fcfg(tmp)
        check(False, "read_fcfg refuses a size that does not match the row count")
    except ValueError:
        check(True, "read_fcfg refuses a size that does not match the row count")

    # switching with several profiles
    u = BinFake("x")
    r, drawn, tmp = await make(u, auto=False)
    os.makedirs(r.dir, exist_ok=True)
    mkfile(r._path("A"), A_ROWS)
    mkfile(r._path("B"), B_ROWS)
    check(await r.command("use", "A") is None, "use A")
    await r.task
    check(r.active_names == ["A"] and u.table == A_ROWS, "A alone: its table as saved")
    check(await r.command("add", "B") is None, "add B")
    await r.task
    check(r.active_names == ["A", "B"] and sorted(R.row_hash(x) for x in u.table) == sorted(R.row_hash(x) for x in A_ROWS + B_ROWS),
          f"A + B: the receiver holds the combined table ({len(u.table)} rows)")
    check("B added - now using A + B" in r.text and r.summary()["active"] == ["A", "B"], f"text + summary list both ({r.text})")
    check(r.active == "A", "the first one in use is the primary")
    check(await r.command("delete", "B") == "that profile is in use - turn it off first", "a profile in use cannot be deleted")
    check(await r.command("add", "B") is None, "adding one already in use is accepted (no change)")
    await r.task
    check(r.active_names == ["A", "B"] and len(u.table) == 6, "... and changes nothing")
    check(await r.command("remove", "A") is None, "remove A")
    await r.task
    check(r.active_names == ["B"] and u.table == B_ROWS and "A turned off" in r.text, f"back to B alone ({r.text})")
    check(await r.command("remove", "B") == "at least one remote must stay in use", "the last one cannot be turned off")
    check(await r.command("remove", "A") == "that profile is not in use", "turning off one that is not in use is refused")
    check(await r.command("use", "A") is None, "use A only")
    await r.task
    check(r.active_names == ["A"] and u.table == A_ROWS, "'use' = only this one")

    # a receiver that refuses / loses a row: the previous table comes back
    u = BinFake("x", accept=True)
    r, drawn, tmp = await make(u, auto=False)
    os.makedirs(r.dir, exist_ok=True)
    mkfile(r._path("A"), A_ROWS); mkfile(r._path("B"), B_ROWS)
    await r.command("use", "A"); await r.task
    u.drop = True
    await r.command("add", "B"); await r.task
    check(r.active_names == ["A"] and "Could not switch" in r.text and "expected 6" in r.text, f"wrong row count after loading: refused ({r.text[:100]})")
    u.drop = False
    check(u.table == A_ROWS, "... and the previous table is back")
    u.only_merged_refused = True
    await r.command("add", "B"); await r.task
    check(r.active_names == ["A"] and "Could not switch" in r.text and "did not accept" in r.text and u.table == A_ROWS,
          f"a refused combined table: previous profiles and table kept ({r.text[:90]})")
    u.only_merged_refused = False
    open(r._path("C"), "wb").write(b"not a table")
    await r.command("add", "C"); await r.task
    check(r.active_names == ["A"] and "not a button table" in r.text and u.table == A_ROWS, f"a broken profile file is named, nothing changed ({r.text[:90]})")

    # old active.json (one name) still reads
    open(os.path.join(r.dir, "active.json"), "w").write(json.dumps({"active": "B"}))
    check(r.active_names == ["B"] and r.active == "B", "an old active.json with one name still works")
    open(os.path.join(r.dir, "active.json"), "w").write(json.dumps({"active": ["A", "gone", "B"]}))
    check(r.active_names == ["A", "B"], "names of deleted profiles drop out of the list")

    # learning while other remotes are in use: the new one joins them
    u = BinFake("learn")
    r, drawn, tmp = await make(u, auto=True)
    os.makedirs(r.dir, exist_ok=True)
    mkfile(r._path("A"), A_ROWS)
    r._set_active(["A"])
    await r.command("learn", "New")
    await asyncio.wait_for(r.task, 15)
    check(r.ok and r.active_names == ["A", "New"], f"learned: now using A + New ({r.text})")
    check("now using A + New" in r.text, "the result says so")
    hs = sorted(R.row_hash(x) for x in u.table)
    check(len(hs) == 3 + 10 and set(R.row_hash(x) for x in A_ROWS) <= set(hs), f"the receiver holds A's 3 + the new remote's 10 buttons ({len(hs)})")
    u = BinFake("learn2")
    u.only_merged_refused = True          # the combined table is refused after the new remote was learned
    r, drawn, tmp = await make(u, auto=True)
    os.makedirs(r.dir, exist_ok=True)
    mkfile(r._path("A"), A_ROWS)
    r._set_active(["A"])
    u.table = list(A_ROWS)
    await r.command("learn", "New2")
    await asyncio.wait_for(r.task, 15)
    check(r.ok and r.profiles() == ["A", "New2"] and r.active_names == ["A"], f"combine failed: the new remote is saved, A stays in use ({r.text[-110:]})")
    check(u.table == A_ROWS and "back - use Add" in r.text, "... and A's table is back in the receiver")
    u.only_merged_refused = False
    await r.command("add", "New2"); await r.task
    check(r.active_names == ["A", "New2"] and len(u.table) == 3 + 10, "Add afterwards works")

    # ---- the remote's Play / Pause button pressed for a later function = that function stays "not set"
    crc = lambda salt, k: "%08X" % (zlib.crc32((salt + k).encode()) & 0xffffffff)
    u = BinFake("skp", presses=["ok", "okbtn", "ok", "ok", "ok", "ok", "ok", "ok", "okbtn", "ok"])
    old = [R.row_hash(x) for x in u.table]
    r, drawn, tmp = await make(u, auto=True)
    await r.command("learn", "Skips")
    await asyncio.wait_for(r.task, 15)
    ok_h = crc("skp", "return")
    hs = [R.row_hash(x) for x in u.table]
    want = [f for f in R.FUNCTIONS if f not in ("Menu", "Back")]
    check(r.ok and "8 functions learned, 2 not set" in r.text, f"Play / Pause for Menu and Back: 8 learned, 2 not set ({r.text})")
    check([R.KEYS[i][1] for i in r.skipped] == ["Menu", "Back"], "Menu and Back are the ones left out")
    check(all(ok_h not in snap for snap in u.at_record[1:]), "while the later functions were learned, Play / Pause was out of the receiver")
    check(hs.count(ok_h) == 1 and len(hs) == 8, f"afterwards Play / Pause is back, once, with the 7 others ({len(hs)} rows)")
    check(R.table_functions(u.table) == want, "the receiver: no Menu, no Back")
    check(R.table_functions(R.read_fcfg(r._path("Skips"))[1]) == want, "the saved profile: the same 8 functions")
    check(r.details()["Skips"]["set"] == want, "details: the 8 functions with a button")
    check(any("not set" in d[2] for d in drawn), "the TV says the function is not set")
    check([R.KEYS[i][1] for i in r.learned] == want, "the test asked only for the learned functions; Play / Pause ended step 2")

    # the end of step 2 when Menu is not set
    r.learned, r.stage, r.phase = [0, 2], "test", "test2"
    r.hits = {"Play / Pause": 0, "Up": 0}
    check("press your Play / Pause button" in r.render() and r.summary is not None, "TV: step 2 ends with Play / Pause when Menu is not set")
    r.learned = [0, 1]
    check("press your Menu button" in r.render(), "... and with Menu when it is set")
    r.stage = r.phase = None

    # a stop while Play / Pause is out of the receiver: the old buttons come back (not a table without Play / Pause)
    u = BinFake("stp", presses=["ok", "ok", "none"])
    old = list(u.table)
    r, drawn, tmp = await make(u, auto=False)
    r.timeout = 30
    await r.command("learn", "Stop")
    await until(lambda: r.index == 2)
    await asyncio.sleep(0.05)
    check(r.ok_row is not None and crc("stp", "return") not in [R.row_hash(x) for x in u.table], "learning: Play / Pause out")
    await r.command("cancel")
    await asyncio.wait_for(r.task, 5)
    check(u.table == old and r.profiles() == [], "cancel then: old buttons back, nothing saved")

    # a crash while Play / Pause is out: the next start puts the old buttons back
    u = BinFake("crs", presses=["ok", "none"])
    old = list(u.table)
    r, drawn, tmp = await make(u, auto=False)
    r.timeout = 30
    await r.command("learn", "Crash")
    await until(lambda: r.index == 1)
    await asyncio.sleep(0.05)
    r.task.cancel()
    await asyncio.sleep(0.05)
    r2 = R.Remotes("flirc_util", tmp, lambda: asyncio.sleep(0), lambda m: None, spawn=u, timeout=0.2, flash=0)
    check(await r2.recover() is True and u.table == old, "crash with Play / Pause out: recover() restores the old buttons")

    # Play / Pause cannot be put back: stopped, old buttons back
    u = BinFake("unp", presses=["ok"] + ["okbtn"] * 9)
    u.refuse_full = True
    old = list(u.table)
    r, drawn, tmp = await make(u, auto=True)
    await r.command("learn", "Unpark")
    await asyncio.wait_for(r.task, 15)
    check(not r.ok and "Could not put Play / Pause back" in r.text and u.table == old and r.profiles() == [],
          f"Play / Pause not accepted back: stopped, old buttons back ({r.text[:80]})")

    # all later functions left out with the Play / Pause button: a one-button remote
    u = BinFake("one", presses=["ok"] + ["okbtn"] * 9)
    r, drawn, tmp = await make(u, auto=True)
    await r.command("learn", "One")
    await asyncio.wait_for(r.task, 15)
    check(r.ok and "1 functions learned, 9 not set" in r.text and R.table_functions(u.table) == ["Play / Pause"],
          f"only Play / Pause learned ({r.text})")

    # a receiver where taking Play / Pause out fails: the learn still works (its button is then refused as 'already used')
    class NoDelete(BinFake):
        async def __call__(self, util, *args, **kw):
            if args[0] == "delete_index":
                self.calls.append(args)
                return FakeProc("[E] fail", rc=1)
            return await super().__call__(util, *args, **kw)
    u = NoDelete("nod")
    r, drawn, tmp = await make(u, auto=True)
    await r.command("learn", "NoDel")
    await asyncio.wait_for(r.task, 15)
    check(r.ok and len(u.table) == 10 and r.ok_row is None, f"Play / Pause could not be taken out: the learn completes as before ({r.text})")

    # ---- the overview on the phone: which functions have a button, and the names typed for them
    u = BinFake("lab")
    r, drawn, tmp = await make(u, auto=False)
    os.makedirs(r.dir, exist_ok=True)
    mkfile(r._path("A"), A_ROWS)                           # Play / Pause, Menu, Up
    mkfile(r._path("Rep"), [mkrow("7C8BCB1E", 0x51), mkrow("CCCC0001", 0x29)])
    s = r.summary()
    check(s["functions"] == R.FUNCTIONS and s["details"]["A"] == {"set": ["Play / Pause", "Menu", "Up"], "names": {}},
          "summary: the function list and A's functions with a button")
    check(s["details"]["Rep"]["set"] == ["Back"], "a stored repeat frame does not count as a button")
    check(await r.command("label", "A", "Play / Pause", "  OK   key ") is None and r.labels("A") == {"Play / Pause": "OK key"},
          "a button name is saved under the profile (spaces tidied)")
    check(json.load(open(os.path.join(r.dir, "A.labels.json"))) == {"Play / Pause": "OK key"}, "... in A.labels.json")
    await r.command("label", "A", "Menu", "x" * 50 + "\x00")
    check(r.labels("A")["Menu"] == "x" * 30, "names are cut to 30 characters, control characters dropped")
    check(r.summary()["details"]["A"]["names"] == {"Play / Pause": "OK key", "Menu": "x" * 30}, "summary carries the names")
    check(await r.command("label", "A", "Menu", "   ") is None and "Menu" not in r.labels("A"), "an empty name removes it")
    check(await r.command("label", "A", "Volume", "x") == "unknown function", "unknown function refused")
    check(await r.command("label", "Nope", "Menu", "x") == "no such profile", "unknown profile refused")
    check(await r.command("label", "../A", "Menu", "x") == "no such profile", "a bad profile name is refused")
    check(r.profiles() == ["A", "Rep"], "the names file is not a profile")
    u.presses = ["none"]
    r.timeout = 30
    await r.command("learn", "Busy")
    await asyncio.sleep(0.05)
    check(r.busy and await r.command("label", "A", "Up", "Arrow up") is None and r.labels("A")["Up"] == "Arrow up",
          "naming works while a learn runs (only a file, the receiver is not touched)")
    await r.command("cancel")
    await asyncio.wait_for(r.task, 5)
    check(await r.command("delete", "A") is None and not os.path.exists(os.path.join(r.dir, "A.labels.json")),
          "deleting a profile deletes its names")
    mkfile(r._path("A"), A_ROWS)
    check(r.labels("A") == {}, "a new profile with the old name starts without names")

    # ---- ASS
    check(R.esc("a{b}\\c") == "a\\{b\\}\\\u200bc", "escape")
    u = FakeUtil()
    r, drawn, tmp = await make(u)
    r.stage, r.index, r.name = "run", 9, "X"
    ass = r.render()
    check("Stop" in ass and "10 / 10" in ass and "Press the button you want for" in ass, "run screen names the function and the count")
    r.index = 1
    ass = r.render()
    check("Play / Pause button = not set" in ass and "opens and closes the menu" in ass, "after Play / Pause: the screen says its button leaves a function out")
    r.index = 0
    check("= not set" not in r.render(), "Play / Pause itself: no 'not set' hint (it must be learned)")
    r.index, r.skipped = 3, [1]
    check("\\s1}Menu{\\s0}" in r.render() and "\\s1}Up" not in r.render(), "a function left out is struck through in the strip")

    print("FAILED" if failed else "ALL PASS", failed)
    return failed

sys.exit(1 if asyncio.run(main()) else 0)
