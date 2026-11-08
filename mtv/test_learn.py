#!/usr/bin/env python3
"""learn.py without a Flirc: a fake flirc_util (stored table, refused duplicates, a press that never comes), the
full run, refusing a used button, stopping on a timeout with the old table restored, a failed restore, key handling
and rendering, plus the menu row.
    python3 test_learn.py
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import learn as L  # noqa: E402
import menu as Mn  # noqa: E402

failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


class FakeProc:
    def __init__(self, out, rc=0, hang=False):
        self.out, self.returncode, self.hang, self.killed = out.encode(), rc, hang, False

    async def communicate(self):
        if self.hang:
            await asyncio.sleep(3600)
        return self.out, None

    def kill(self):
        self.killed = True

    async def wait(self):
        return 0


class FakeUtil:
    """Behaves like flirc_util: `record` stores the next scripted press, saveconfig writes <base>.fcfg."""
    def __init__(self, presses, restore_ok=True):
        self.presses = list(presses)     # per record call: "ok" | "dup" | "none" (never comes)
        self.table = ["old1", "old2"]
        self.calls, self.restore_ok, self.n = [], restore_ok, 0
        self.saved = None

    async def __call__(self, util, *args, **kw):
        self.calls.append(args)
        cmd = args[0]
        if cmd == "saveconfig":
            self.saved = list(self.table)
            open(args[1] + ".fcfg", "w").write("x")
            return FakeProc("Configuration File saved")
        if cmd == "format":
            self.table = []
            return FakeProc("Formatting Device, please wait...  Done!")
        if cmd == "loadconfig":
            if not self.restore_ok:
                return FakeProc("[E] cannot load", rc=1)
            self.table = list(self.saved)
            return FakeProc("Configuration File loaded")
        if cmd == "record":
            what = self.presses.pop(0) if self.presses else "ok"
            if what == "none":
                return FakeProc("", hang=True)
            if what == "dup":
                return FakeProc("[E] button already exists")
            self.table.append(args[1])
            return FakeProc(f"Press any button on the remote to link it with '{args[1]}'\n Succesfully recorded button")
        raise AssertionError(args)


async def make(presses, restore_ok=True, **kw):
    drawn, logs = [], []
    util = FakeUtil(presses, restore_ok)
    tmp = tempfile.mkdtemp()

    async def redraw():
        drawn.append((L_.stage, L_.index, L_.note))

    L_ = L.Learn("flirc_util", tmp, redraw, logs.append, spawn=util, timeout=0.2, flash=0, result_for=0.3, **kw)
    return L_, util, drawn, logs, tmp


async def main():
    # ---- the full run: confirm -> 21 presses in order -> done, table = the 21 keys
    l, u, drawn, logs, tmp = await make([])
    check(not l.active and l.render() == "", "idle: nothing drawn")
    await l.open()
    check(l.stage == "confirm" and "Learn remote" in l.render() and "OK  start" in l.render(), "confirm screen")
    check(await l.key("blue") is True and not l.active, "Blue on the confirm screen cancels, nothing started")
    check(u.calls == [], "cancel runs no program")
    await l.open()
    await l.key("ok")
    await l.task
    keys = [c[1] for c in u.calls if c[0] == "record"]
    check(keys == [k for k, _ in L.KEYS] and len(keys) == 21, f"recorded {len(keys)} keys in order")
    check(l.stage == "result" and l.ok and "All 21" in l.result, "result: all learned")
    check(u.table == [k for k, _ in L.KEYS], "stored table = the 21 buttons only (old ones cleared)")
    check(u.calls[0][0] == "saveconfig" and u.calls[1] == ("format",), "old table saved before the format")
    check(any(os.path.isfile(os.path.join(tmp, f)) for f in os.listdir(tmp)), "backup file exists")
    check([d[1] for d in drawn if d[0] == "run"][:3] == [0, 1, 2], "screen redrawn per button")
    check("Done" in l.render() and "Press any button" in l.render(), "done screen")
    check(await l.key("up") is True and not l.active, "any key closes the result")

    # ---- a refused press (button already used): same key again, then through
    l, u, drawn, logs, tmp = await make(["ok", "dup", "ok"])
    await l.open(); await l.key("ok"); await l.task
    keys = [c[1] for c in u.calls if c[0] == "record"]
    check(keys[:3] == ["return", "up", "up"] and l.ok, "refused press -> same button asked again, run completes")
    check(any("already used" in d[2] for d in drawn), "screen says the button is already used")

    # ---- too many refusals -> stop + restore
    l, u, drawn, logs, tmp = await make(["dup"] * 10)
    await l.open(); await l.key("ok"); await l.task
    check(not l.ok and u.table == ["old1", "old2"] and "restored" in l.result, "5 refusals -> stopped, old table back")

    # ---- timeout in the middle -> kill, restore
    l, u, drawn, logs, tmp = await make(["ok", "ok", "ok", "none"])
    await l.open(); await l.key("ok"); await l.task
    check(not l.ok and "No button pressed" in l.result, "no press -> stopped")
    check(u.table == ["old1", "old2"], "timeout: old buttons are back (partial table wiped, then loadconfig)")
    check(l.restored is True and "Press any button (the old buttons work again)" in l.render(), "result says old buttons work")
    # the result screen leaves by itself
    await asyncio.sleep(0.5)
    check(not l.active, "result screen closes on its own")

    # ---- restore fails -> says so and names the file
    l, u, drawn, logs, tmp = await make(["none"], restore_ok=False)
    await l.open(); await l.key("ok"); await l.task
    check(l.restored is False and "RESTORE FAILED" in l.result and l.backup in l.render(), "failed restore is reported with the file")

    # ---- keys while running are ignored
    l, u, drawn, logs, tmp = await make(["none"])
    await l.open(); await l.key("ok")
    await asyncio.sleep(0)
    check(await l.key("ok") is False and l.stage == "run", "keys while running change nothing")
    await l.task

    # ---- a failing save never formats
    class NoSave(FakeUtil):
        async def __call__(self, util, *args, **kw):
            self.calls.append(args)
            return FakeProc("[E] fail", rc=1)
    l, u, drawn, logs, tmp = await make([])
    l.spawn = NoSave([])
    await l.open(); await l.key("ok"); await l.task
    check(not l.ok and ("format",) not in l.spawn.calls and "nothing was changed" in l.result, "save fails -> no format, nothing changed")

    # ---- ASS: escaping and labels
    check(L.esc("a{b}\\c") == "a\\{b\\}\\\u200bc", "escape")
    l, *_ = await make([])
    await l.open(); l.stage = "run"; l.index = 7
    r = l.render()
    check("RED" in r and "8 / 21" in r, "run screen names the button and the count")

    # ---- the menu row
    async def nothing(): ...
    called = []

    async def start():
        called.append(1)
    m = Mn.Menu(None, lambda m: None, lambda: {}, learn=start)
    await m.show()
    check(m.page.rows[-1].label == "Learn remote", "menu: Learn remote is the last row when the receiver can learn")
    m.page.sel = len(m.page.rows) - 1
    await m.key("right")
    check(not called, "Right does not start it")
    check(await m.key("ok") is True and called == [1] and not m.open, "OK closes the menu and starts it")
    m2 = Mn.Menu(None, lambda m: None, lambda: {})
    await m2.show()
    check("Learn remote" not in [r.label for r in m2.page.rows], "no row without a Flirc")

    print("FAILED" if failed else "ALL PASS", failed)
    return failed

sys.exit(1 if asyncio.run(main()) else 0)
