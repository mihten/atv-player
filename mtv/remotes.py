"""Remote profiles of a Flirc receiver: save / switch / delete, and learning a new remote with the steps shown on the TV.

A profile is the receiver's whole button table, saved by `flirc_util saveconfig` as <state>/remotes/<name>.fcfg. Using
one = format + loadconfig; learning one = a fresh table, recorded button by button (the TV names the button, you press
it once), then saved under the new name. Everything is started from the web remote (the TV may have no working buttons
when it is needed); the TV only shows the prompts. The table in place before a switch / a learn is kept
(<state>/remotes/.current.fcfg) and put back when the action fails or is stopped, so a failed run never leaves the box
without a remote.

Pure logic + ASS renderer; programs are started through `spawn` (asyncio.create_subprocess_exec by default).
"""
import asyncio
import contextlib
import json
import os
import re
import time

RES_X, RES_Y = 1920, 1080
# What the player needs, not what a remote has: (flirc key name, function, hint, optional). Whatever button the user
# presses for a function, the Flirc sends the key named here and ir.EVDEV_KEYS turns it into the player's key. Optional
# functions can be skipped from the phone (a small remote has no spare buttons).
KEYS = [("return", "Play / Pause", "also selects in the menu", False),
        ("F4", "Menu", "opens and closes the menu", False),
        ("up", "Up", "menu up; forward 1 minute", False),
        ("down", "Down", "menu down; back 1 minute", False),
        ("left", "Left", "menu back; back 10 seconds", False),
        ("right", "Right", "menu select; forward 10 seconds", False),
        ("pageup", "Next", "next video; page down in the menu", True),
        ("pagedown", "Previous", "previous video; page up in the menu", True)]
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,29}$")
TIMEOUT = 40.0            # s to wait for one press
MAX_REJECTS = 5           # presses refused ("already used") before giving up
FLASH = 1.0               # s the "stored" confirmation stays
RESULT_FOR = 30.0         # s the result screen stays when no button dismisses it
QUIET = 1.0               # s without a signal from the receiver before the next button is listened for: a held button
                          # keeps sending (repeats every ~0.1 s, a burst ~1 s after release) and the receiver would refuse
                          # the tail of the PREVIOUS button as "already used"
SETTLE = 0.25             # s after a stored button before the silence check (the first repeat has not shown up yet)
QUIET_MAX = 12.0          # s to wait for that silence at most
STRAY = 2.0               # s: a refusal this soon after a signal is the previous button's tail, not a real duplicate
MAX_STRAYS = 20
LOADED = "Loaded Successfully"
# Hashes of signals that are no button: the NEC "repeat" frame (9 ms burst, 2.25 ms space) is the same for every key of
# every NEC remote and follows most presses. The receiver stores it as a button when the press starts just before it
# listens; the stored "button" then fires for every key whose own code is not stored (measured 2026-11-08: TotalTV).
NOT_A_BUTTON = {"7C8BCB1E": "the repeat part of a signal"}
ROW = re.compile(r"^\s*(\d+)\s+([0-9A-Fa-f]{8})\s+(\d+)\s+(\d+)\s+(\S+)\s*$", re.M)
MAX_PARTIAL = 8

BG, BG_ALPHA = "&H101010&", "&H18&"
WHITE, GREY, DIM, BLUE, GREEN = "&HFFFFFF&", "&HB4B4B4&", "&H808080&", "&HE0A060&", "&H80D080&"


def esc(text):
    return str(text or "").replace("\\", "\\\u200b").replace("{", "\\{").replace("}", "\\}").replace("\n", " ")


def clean_name(name):
    """The profile name, or None when it is not allowed (it becomes a file name)."""
    name = " ".join(str(name or "").split())
    return name if NAME_RE.match(name) else None


class Remotes:
    def __init__(self, util, state_dir, changed, log=lambda m: None, spawn=None, timeout=TIMEOUT, flash=FLASH,
                 result_for=RESULT_FOR, quiet_for=None, quiet=QUIET, quiet_max=QUIET_MAX, settle=SETTLE):
        self.util, self.dir, self.changed, self.log = util, os.path.join(state_dir, "remotes"), changed, log
        self.quiet_for = quiet_for   # callable: seconds since the receiver last sent a key event (None: unknown)
        self.quiet, self.quiet_max, self.settle = quiet, quiet_max, settle
        self.spawn = spawn or asyncio.create_subprocess_exec
        self.timeout, self.flash, self.result_for = timeout, flash, result_for
        self.stage = None          # TV screen: None | "run" | "result"
        self.mode = None           # what runs: None | "learn" | "use"
        self.name = ""             # profile being learned / switched to
        self.index = 0             # button being learned
        self.note = ""             # one line under the big text
        self.text = ""             # last result, for the web remote and the result screen
        self.ok = False
        self.restored = None       # after an abort: were the old buttons put back
        self.task = self.closer = self.proc = None
        self.cancelled = False
        self.skip_flag = False       # "Skip" pressed for an optional function

    # -- profiles on disk
    def _path(self, name):
        return os.path.join(self.dir, name + ".fcfg")

    def profiles(self):
        try:
            return sorted((f[:-5] for f in os.listdir(self.dir) if f.endswith(".fcfg") and not f.startswith(".")),
                          key=str.lower)
        except OSError:
            return []

    @property
    def active(self):
        try:
            with open(os.path.join(self.dir, "active.json")) as f:
                name = json.load(f).get("active")
            return name if name in self.profiles() else None
        except (OSError, ValueError, AttributeError):
            return None

    def _set_active(self, name):
        os.makedirs(self.dir, exist_ok=True)
        tmp = os.path.join(self.dir, "active.json.tmp")
        with open(tmp, "w") as f:
            json.dump({"active": name}, f)
        os.replace(tmp, os.path.join(self.dir, "active.json"))

    # -- a marker while the table is being changed: a crash or restart in between is repaired at the next start
    def _mark(self, on):
        path = os.path.join(self.dir, ".pending")
        with contextlib.suppress(OSError):
            if on:
                os.makedirs(self.dir, exist_ok=True)
                open(path, "w").close()
            else:
                os.unlink(path)

    async def recover(self):
        """At start: a learn / switch that never finished left the receiver cleared or half-filled: put the table back
        that was in it before."""
        if not os.path.exists(os.path.join(self.dir, ".pending")):
            return None
        if not os.path.isfile(os.path.join(self.dir, ".current.fcfg")):
            self._mark(False)
            return None
        ok = await self._load(os.path.join(self.dir, ".current.fcfg"))
        self.text = "An unfinished remote change was undone: the previous buttons are back" if ok \
            else "An unfinished remote change could not be undone: choose a profile on the phone"
        self.log(f"remotes: {self.text}")
        if ok:
            self._mark(False)
        return ok

    async def shutdown(self):
        """The player stops: a running learn is stopped and undone, a switch is finished first."""
        if not self.busy:
            return
        await self.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(self.task), 60)

    @property
    def busy(self):
        return self.task is not None and not self.task.done()

    @property
    def active_screen(self):
        return self.stage is not None

    def summary(self):
        """For the web remote (inside the player's `output` message)."""
        state = "learning" if self.mode == "learn" and self.busy else "switching" if self.mode == "use" and self.busy \
            else "idle"
        prog = {"state": state, "name": self.name if state != "idle" else "", "text": self.text}
        if state == "learning":
            fn = KEYS[min(self.index, len(KEYS) - 1)]
            prog.update(index=self.index, total=len(KEYS), label=fn[1], optional=fn[3], note=self.note)
        return {"profiles": self.profiles(), "active": self.active, "progress": prog}

    # -- the flirc_util program
    async def _util(self, *args, timeout=15.0):
        """(returncode or None on timeout / stop, output)."""
        proc = self.proc = await self.spawn(self.util, *args, stdout=asyncio.subprocess.PIPE,
                                            stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            await self._kill(proc)
            return None, "timeout"
        finally:
            self.proc = None
        if self.cancelled:
            return None, "stopped"
        return proc.returncode, (out or b"").decode(errors="replace")

    @staticmethod
    async def _kill(proc):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()

    async def _load(self, path):
        """Table of a file into the (cleared) receiver."""
        await self._util("format", timeout=30)
        rc, out = await self._util("loadconfig", path, timeout=30)
        return rc == 0 and LOADED in out

    async def _stored(self, key):
        """(index, hash) of the newest table row for a flirc key name, or None."""
        rc, out = await self._util("settings", timeout=15)
        if rc != 0:
            return None
        rows = [(int(i), h.upper()) for i, h, _ik, _id, k in ROW.findall(out) if k.lower() == key.lower()]
        return rows[-1] if rows else None

    async def _bad_rows(self):
        """Names (flirc keys) of table rows that are no real button (see NOT_A_BUTTON); [] when none or unreadable."""
        rc, out = await self._util("settings", timeout=15)
        if rc != 0:
            return []
        return [k for _i, h, _ik, _id, k in ROW.findall(out) if h.upper() in NOT_A_BUTTON]

    async def _keep_current(self):
        """The table in the receiver now, saved so that it can be put back."""
        os.makedirs(self.dir, exist_ok=True)
        base = os.path.join(self.dir, ".current")
        with contextlib.suppress(OSError):
            os.unlink(base + ".fcfg")
        rc, _ = await self._util("saveconfig", base)
        return rc == 0 and os.path.isfile(base + ".fcfg")

    async def _restore(self):
        self.cancelled = False   # a stop must not block the restore
        ok = await self._load(os.path.join(self.dir, ".current.fcfg"))
        self.restored = ok
        return "old buttons restored" if ok else "RESTORE FAILED - the receiver needs a profile from the web remote"

    # -- web remote commands (return an error text at once, or None when started)
    async def command(self, op, name=None):
        if op == "cancel":
            return await self.cancel()
        if op == "skip":
            return await self.skip()
        if self.busy:
            return "busy"
        clean = clean_name(name)
        if op in ("learn", "use", "delete") and clean is None:
            return "bad name"
        if op == "learn":
            if clean in self.profiles():
                return "a profile with that name already exists"
            self.mode, self.name, self.index, self.note, self.cancelled = "learn", clean, 0, "", False
            self.stage, self.restored, self.ok, self.text = "run", None, False, ""
            self._mark(True)
            self.task = asyncio.ensure_future(self._learn())
        elif op == "use":
            if clean not in self.profiles():
                return "no such profile"
            self.mode, self.name, self.cancelled, self.text = "use", clean, False, ""
            self._mark(True)
            self.task = asyncio.ensure_future(self._use())
        elif op == "delete":
            if clean not in self.profiles():
                return "no such profile"
            if clean == self.active:
                return "that profile is in use - switch to another one first"
            with contextlib.suppress(OSError):
                os.unlink(self._path(clean))
            self.text = f"{clean} deleted"
            self.log(f"remotes: {self.text}")
        else:
            return "unknown op"
        await self.changed()
        return None

    async def skip(self):
        """Leave out the function being asked for (only the optional ones: Next / Previous)."""
        if not self.busy or self.mode != "learn" or self.index >= len(KEYS) or not KEYS[self.index][3]:
            return "this function cannot be skipped"
        self.skip_flag = True
        if self.proc:
            await self._kill(self.proc)
        return None

    async def cancel(self):
        if not self.busy or self.mode != "learn":   # a switch is short and always ends with a working table
            return None
        self.cancelled = True
        if self.proc:
            await self._kill(self.proc)
        return None

    # -- switching
    async def _use(self):
        name = self.name
        try:
            if not await self._keep_current():
                return await self._done_use(f"Could not save the current buttons - nothing changed")
            if await self._load(self._path(name)):
                self._set_active(name)
                bad = await self._bad_rows()
                if bad:
                    self.log(f"remotes: {name} holds {len(bad)} row(s) that are no button: {bad}")
                    return await self._done_use(f"Now using {name} - but its {', '.join(bad)} button caught only the repeat part "
                                                f"of a signal and fires for other keys too: delete the remote and learn it again")
                return await self._done_use(f"Now using {name}")
            await self._restore()
            return await self._done_use(f"Could not load {name} - the previous buttons are back" if self.restored
                                        else f"Could not load {name} - RESTORE FAILED")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return await self._done_use(f"Error: {e!r:.60}")

    async def _done_use(self, text):
        self._mark(False)
        self.text, self.mode = text, None
        self.log(f"remotes: {text}")
        await self.changed()

    # -- learning
    async def _wait_quiet(self):
        """Until the receiver has been silent for `quiet` s (the previous button is let go). Shows a hint meanwhile."""
        if self.quiet_for is None:
            return
        await asyncio.sleep(self.settle)
        loop = asyncio.get_running_loop()
        deadline, hinted = loop.time() + self.quiet_max, False
        while self.quiet_for() < self.quiet and not self.cancelled and loop.time() < deadline:
            if not hinted:
                hinted, self.note = True, "Release the button"
                await self.changed()
            await asyncio.sleep(0.1)
        if hinted:
            self.note = ""
            await self.changed()

    async def _learn(self):
        try:
            if not await self._keep_current():
                return await self._finish(False, "Could not save the old buttons - nothing was changed")
            rc, out = await self._util("format", timeout=30)
            if rc != 0:
                return await self._finish(False, f"Could not clear the receiver: {' '.join(out.split())[:60]}")
            rejects = strays = learned = partial = 0
            while self.index < len(KEYS):
                key, label, _hint, optional = KEYS[self.index]
                await self.changed()
                await self._wait_quiet()
                if self.cancelled:
                    return await self._finish(False, "Stopped, " + await self._restore())
                if self.skip_flag:          # "Skip" arrived before the receiver was listening
                    self.skip_flag = False
                    self.log(f"remotes: {label} skipped")
                    self.index += 1
                    continue
                rc, out = await self._util("record", key, timeout=self.timeout)
                if self.skip_flag:          # "Skip" on the phone killed the wait
                    self.skip_flag = False
                    self.log(f"remotes: {label} skipped")
                    self.index += 1
                    rejects = 0
                    continue
                if rc is None:
                    why = "Stopped" if self.cancelled else "No button pressed - stopped"
                    return await self._finish(False, why + ", " + await self._restore())
                if "recorded button" in out:
                    row = await self._stored(key)
                    if row and row[1] in NOT_A_BUTTON:
                        partial += 1   # only the repeat part was caught: take it out and listen again
                        await self._util("delete_index", str(row[0]), timeout=15)
                        self.log(f"remotes: {label}: only {NOT_A_BUTTON[row[1]]} was caught (hash {row[1]}), removed, asking again")
                        if partial >= MAX_PARTIAL:
                            return await self._finish(False, "The receiver keeps catching only part of the signal - stopped, "
                                                      + await self._restore())
                        self.note = "Only part of the signal was caught - press the button again"
                        continue
                    self.log(f"remotes: {label} stored" + (f" (hash {row[1]})" if row else ""))
                    self.note = f"{label} stored"
                    self.index += 1
                    learned += 1
                    rejects = 0
                    if self.flash and self.index < len(KEYS):
                        await self.changed()
                        await asyncio.sleep(self.flash)
                    self.note = ""
                    continue
                self.log(f"remotes: {label} refused: {' '.join(out.split())[-100:]} (signal {self._since()} s ago)")
                if "already exists" in out and self.quiet_for is not None and self.quiet_for() < STRAY and strays < MAX_STRAYS:
                    strays += 1      # the tail of the previous button, not a duplicate: wait for silence, listen again
                    continue
                rejects += 1
                self.note = "That button is already used - press a different one" if "already exists" in out \
                    else "Not stored - press the button again"
                if rejects >= MAX_REJECTS:
                    return await self._finish(False, "Too many refused presses - stopped, " + await self._restore())
            rc, _ = await self._util("saveconfig", os.path.join(self.dir, self.name))
            if rc != 0 or not os.path.isfile(self._path(self.name)):
                return await self._finish(False, "Learned, but could not save the profile, " + await self._restore())
            self._set_active(self.name)
            skipped = len(KEYS) - learned
            await self._finish(True, f"{self.name}: {learned} functions learned" + (f", {skipped} skipped" if skipped else ""))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never leave the screen stuck
            await self._finish(False, f"Error: {e!r:.60} - " + await self._restore())

    def _since(self):
        return "?" if self.quiet_for is None else f"{self.quiet_for():.1f}"

    async def _finish(self, ok, text):
        if ok or self.restored or "nothing was changed" in text:   # a failed restore keeps the marker: next start retries
            self._mark(False)
        self.ok, self.text, self.stage, self.mode = ok, text, "result", None
        self.log(f"remotes: {text}")
        await self.changed()
        self.closer = asyncio.ensure_future(self._auto_close())

    async def _auto_close(self):
        """The result screen leaves by itself (a remote that lost its buttons cannot dismiss it)."""
        await asyncio.sleep(self.result_for)
        if self.stage == "result":
            self.stage = None
            await self.changed()

    async def key(self, name):
        """A remote key while the TV shows the learn screen. Returns True when the screen changed."""
        if self.stage == "result":
            self.stage = None
            if self.closer:
                self.closer.cancel()
                self.closer = None
            return True
        return False  # running: the receiver takes the presses, nothing else counts

    # -- drawing (TV)
    def render(self):
        if self.stage is None:
            return ""
        ev = [f"{{\\an7\\pos(0,0)\\bord0\\shad0\\1c{BG}\\1a{BG_ALPHA}\\p1}}m 0 0 l {RES_X} 0 {RES_X} {RES_Y} 0 {RES_Y}{{\\p0}}"]

        def text(x, y, s, size, color=WHITE, an=7, bold=False):
            ev.append(f"{{\\an{an}\\pos({x},{y})\\fs{size}\\bord0\\shad0\\1c{color}\\b{1 if bold else 0}}}{esc(s)}")

        mid = RES_X // 2
        text(120, 90, f"Learn remote: {self.name}", 56, WHITE, bold=True)
        if self.stage == "run":
            key, label, hint, optional = KEYS[min(self.index, len(KEYS) - 1)]
            text(mid, 250, f"{self.index + 1} / {len(KEYS)}", 44, GREY, an=8)
            text(mid, 340, "Press the button you want for", 56, GREY, an=8)
            text(mid, 440, label, 170, WHITE, an=8, bold=True)
            text(mid, 650, hint, 40, GREY, an=8)
            text(mid, 740, self.note or ("once, on the remote" + ("  (or Skip on the phone)" if optional else "")), 44,
                 GREEN if "stored" in self.note else GREY, an=8)
            parts = []
            for n, (_k, lab, _h, _o) in enumerate(KEYS):
                col = GREEN if n < self.index else (BLUE if n == self.index else DIM)
                parts.append(f"{{\\1c{col}}}{esc(lab)}")
            ev.append(f"{{\\an8\\pos({mid},900)\\fs40\\bord0\\shad0\\b0}}" + "  ".join(parts))
            text(mid, 990, f"No press for {int(self.timeout)} s, or Cancel on the phone, stops and restores the old buttons",
                 32, DIM, an=8)
        else:
            text(mid, 400, "Done" if self.ok else "Stopped", 120, GREEN if self.ok else BLUE, an=8, bold=True)
            text(mid, 580, self.text, 44, WHITE, an=8)
            hint = "Press any button" if self.ok or self.restored is None else (
                "Press any button (the old buttons work again)" if self.restored
                else "Choose a profile on the phone: Settings > Audio > TV Player > Remote")
            text(mid, 760, hint, 36, DIM, an=8)
        return "\n".join(ev)
