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
import struct
import textwrap
import time

RES_X, RES_Y = 1920, 1080
# What the player needs, not what a remote has: (flirc key name, function, hint, optional). Whatever button the user
# presses for a function, the Flirc sends the key named here and ir.EVDEV_KEYS turns it into the player's key. Play / Pause
# comes first and is the only one that must be learned: every later function is skipped ("not set") by pressing the
# Play / Pause button again, or by Skip on the phone (a small remote has no spare buttons). `optional` = shown as optional.
KEYS = [("return", "Play / Pause", "also selects in the menu", False),
        ("F4", "Menu", "opens and closes the menu", False),
        ("up", "Up", "menu up; previous video", False),
        ("down", "Down", "menu down; next video", False),
        ("left", "Left", "menu back; back 10 seconds", False),
        ("right", "Right", "menu select; forward 10 seconds", False),
        ("pageup", "CH+ button", "the channel up button: volume up in the player, page up in the menu", True),
        ("pagedown", "CH- button", "the channel down button: volume down in the player, page down in the menu", True),
        ("escape", "Back", "closes the menu", True),
        ("F1", "Stop", "stops playback; the screen goes black", True)]
FUNCTIONS = [k[1] for k in KEYS]
# Older names of the CH+ / CH- functions (same Flirc keys): Next / Previous until 0.11.0, Volume up / down in 0.12.0
RENAMED = {"Next": "CH+ button", "Previous": "CH- button", "Volume up": "CH+ button", "Volume down": "CH- button"}
LABEL_MAX = 30            # characters of a button name typed on the phone
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
# The first call of a fresh flirc_util process sometimes fails (seen 2026-11-09 in the TV Player CT); a read of the table or a
# save of it changes nothing, so those two are tried again instead of aborting a learn.
RETRY_CMDS = ("settings", "saveconfig")
RETRIES, RETRY_WAIT = 3, 1.0
# The receiver holds ONE button table and `loadconfig` replaces it (measured 2026-11-08), so several remotes at once =
# one combined table written by us: a .fcfg file is a 28-byte header (magic, version, row count, checksum the device does
# not verify when loading, ...) and 16-byte rows (hash LE u32, IK, ..., HID key code in the last byte).
FCFG_MAGIC = b"\xef\xbe\xad\xde"
FCFG_HEAD, FCFG_ROW = 28, 16
# The player's key name each learned function arrives as (ir.EVDEV_KEYS: Enter -> ok, F4 -> blue, PgUp -> ch_up ...)
HID = {"return": 0x28, "F4": 0x3d, "up": 0x52, "down": 0x51, "left": 0x50, "right": 0x4f, "pageup": 0x4b, "pagedown": 0x4e,
       "escape": 0x29, "F1": 0x3a}
PLAYER_KEY = {"return": "ok", "F4": "blue", "up": "up", "down": "down", "left": "left", "right": "right",
              "pageup": "ch_up", "pagedown": "ch_down", "escape": "back", "F1": "stop"}
TEST_IDLE = 60.0          # s without any key before a test step gives up
REVIEW_FOR = 120.0        # s the phone has to choose Keep / Discard after a failed test (then: discard)
WARN = "&H4080F0&"        # orange (BGR)

BG, BG_ALPHA = "&H101010&", "&H18&"
WHITE, GREY, DIM, BLUE, GREEN = "&HFFFFFF&", "&HB4B4B4&", "&H808080&", "&HE0A060&", "&H80D080&"


def esc(text):
    return str(text or "").replace("\\", "\\\u200b").replace("{", "\\{").replace("}", "\\}").replace("\n", " ")


def clean_name(name):
    """The profile name, or None when it is not allowed (it becomes a file name)."""
    name = " ".join(str(name or "").split())
    return name if NAME_RE.match(name) else None


def clean_label(text):
    """A button name typed on the phone: printable, single spaces, at most LABEL_MAX characters ('' = no name)."""
    text = "".join(c for c in str(text or "") if c.isprintable())
    return " ".join(text.split())[:LABEL_MAX].strip()


def read_fcfg(path):
    """(header bytes, [16-byte rows]) of a saved table; ValueError when it is no table."""
    with open(path, "rb") as f:
        b = f.read()
    if len(b) < FCFG_HEAD or b[:4] != FCFG_MAGIC:
        raise ValueError("not a button table")
    n = struct.unpack_from("<I", b, 8)[0]
    if FCFG_HEAD + n * FCFG_ROW != len(b):
        raise ValueError("table size does not match its row count")
    return b[:FCFG_HEAD], [b[FCFG_HEAD + i * FCFG_ROW:FCFG_HEAD + (i + 1) * FCFG_ROW] for i in range(n)]


def row_hash(row):
    return f"{struct.unpack_from('<I', row, 0)[0]:08X}"


def row_name(row):
    hid = row[FCFG_ROW - 1]
    for flirc, label, _h, _o in KEYS:
        if HID.get(flirc) == hid:
            return label
    return f"key 0x{hid:02x}"


def table_functions(rows):
    """The functions (KEYS order) that have a real button in a table's rows (a stored repeat frame does not count)."""
    hids = {r[FCFG_ROW - 1] for r in rows if row_hash(r) not in NOT_A_BUTTON}
    return [label for flirc, label, _h, _o in KEYS if HID[flirc] in hids]


def merge_tables(tables):
    """tables: [(name, header, rows)] in priority order. Returns (header, rows, notes): one row per signal. The same signal
    with the same key twice is one row; the same signal with another key stays with the first profile (a note names it);
    a stored NEC repeat frame is dropped (it would fire for every unknown key)."""
    seen, out, notes = {}, [], []
    head = tables[0][1]
    for name, _h, rows in tables:
        for row in rows:
            h = row_hash(row)
            if h in NOT_A_BUTTON:
                notes.append(f"{name}: {row_name(row)} is only {NOT_A_BUTTON[h]} - left out")
            elif h not in seen:
                seen[h] = (name, row)
                out.append(row)
            elif seen[h][1][FCFG_ROW - 1] != row[FCFG_ROW - 1]:
                notes.append(f"{name}: {row_name(row)} has the same signal as {seen[h][0]}'s {row_name(seen[h][1])} - "
                             f"{seen[h][0]} wins")
    hdr = bytearray(head)
    struct.pack_into("<I", hdr, 8, len(out))
    return bytes(hdr), out, notes


class Remotes:
    def __init__(self, util, state_dir, changed, log=lambda m: None, spawn=None, timeout=TIMEOUT, flash=FLASH,
                 result_for=RESULT_FOR, quiet_for=None, quiet=QUIET, quiet_max=QUIET_MAX, settle=SETTLE,
                 test_idle=TEST_IDLE, review_for=REVIEW_FOR, retry_wait=RETRY_WAIT):
        self.retry_wait = retry_wait
        self.util, self.dir, self.changed, self.log = util, os.path.join(state_dir, "remotes"), changed, log
        self.quiet_for = quiet_for   # callable: seconds since the receiver last sent a key event (None: unknown)
        self.quiet, self.quiet_max, self.settle = quiet, quiet_max, settle
        self.test_idle, self.review_for = test_idle, review_for
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
        self.learned = []            # KEYS indexes stored in this learn
        self.phase = None            # None | "test1" (press what you assigned) | "test2" (press what you did not) | "review"
        self.hits = {}               # function label -> presses seen in the test
        self.strays = {}             # function label (or key name) -> times it fired without being asked for
        self.problems = []
        self.decision = None         # "keep" | "discard" from the phone after a failed test
        self._wake = asyncio.Event()
        self._menu_pressed = False
        self._want = None            # (op, names) of a running switch
        self._before = []            # profiles in use before a learn
        self.skipped = []            # KEYS indexes left "not set" in this learn
        self.ok_row = self.ok_hash = None   # the Play / Pause row, out of the receiver while the other functions are learned

    # -- profiles on disk
    def _path(self, name):
        return os.path.join(self.dir, name + ".fcfg")

    def _labels_path(self, name):
        return os.path.join(self.dir, name + ".labels.json")

    def labels(self, name):
        """{function: button name} typed on the phone for a profile."""
        try:
            with open(self._labels_path(name)) as f:
                val = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(val, dict):
            return {}
        val = {RENAMED.get(k, k): v for k, v in val.items()}
        return {k: v for k, v in val.items() if k in FUNCTIONS and isinstance(v, str) and v}

    def details(self):
        """Per profile: the functions that have a button, and the names given to them (web remote overview)."""
        out = {}
        for n in self.profiles():
            try:
                _h, rows = read_fcfg(self._path(n))
                have = table_functions(rows)
            except (OSError, ValueError):
                have = []
            out[n] = {"set": have, "names": self.labels(n)}
        return out

    def set_label(self, name, function, text):
        """Name the button of one function of a saved profile ('' removes the name). Returns an error text or None."""
        if name not in self.profiles():
            return "no such profile"
        if function not in FUNCTIONS:
            return "unknown function"
        labels = self.labels(name)
        text = clean_label(text)
        if text:
            labels[function] = text
        else:
            labels.pop(function, None)
        tmp = self._labels_path(name) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(labels, f)
        os.replace(tmp, self._labels_path(name))
        self.log(f"remotes: {name}: {function} button " + (f"named {text!r}" if text else "name removed"))
        return None

    def profiles(self):
        try:
            return sorted((f[:-5] for f in os.listdir(self.dir) if f.endswith(".fcfg") and not f.startswith(".")),
                          key=str.lower)
        except OSError:
            return []

    @property
    def active_names(self):
        """The profiles in use together, in priority order (active.json: a name, or a list of names)."""
        try:
            with open(os.path.join(self.dir, "active.json")) as f:
                val = json.load(f).get("active")
        except (OSError, ValueError, AttributeError):
            return []
        names = val if isinstance(val, list) else [val]
        have = self.profiles()
        return [n for n in names if isinstance(n, str) and n in have]

    @property
    def active(self):
        """The first profile in use (None when none)."""
        names = self.active_names
        return names[0] if names else None

    def _set_active(self, names):
        names = [names] if isinstance(names, str) else list(names)
        os.makedirs(self.dir, exist_ok=True)
        tmp = os.path.join(self.dir, "active.json.tmp")
        with open(tmp, "w") as f:
            json.dump({"active": names}, f)
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
        learn = self.mode == "learn" and self.busy
        state = ("review" if self.phase == "review" else "testing" if self.phase in ("test1", "test2") else "learning") \
            if learn else "switching" if self.mode == "use" and self.busy else "idle"
        prog = {"state": state, "name": self.name if state != "idle" else "", "text": self.text}
        if state == "learning":
            fn = KEYS[min(self.index, len(KEYS) - 1)]
            prog.update(index=self.index, total=len(KEYS), label=fn[1], optional=self.index > 0, note=self.note)
        elif state in ("testing", "review"):
            prog.update(phase=self.phase, expected=[KEYS[i][1] for i in self.learned],
                        lit=[l for l, n in self.hits.items() if n], strays=self._stray_list(), problems=list(self.problems),
                        end=self._end_key()[1])
        return {"profiles": self.profiles(), "active": self.active_names, "progress": prog,
                "functions": FUNCTIONS, "details": self.details()}

    # -- the flirc_util program
    async def _util(self, *args, timeout=15.0):
        """(returncode or None on timeout / stop, output). settings / saveconfig are tried again when they fail."""
        for attempt in range(RETRIES if args and args[0] in RETRY_CMDS else 1):
            if attempt:
                await asyncio.sleep(self.retry_wait)
            rc, out = await self._util_once(*args, timeout=timeout)
            if rc == 0 or rc is None or self.cancelled:
                break
        return rc, out

    async def _util_once(self, *args, timeout=15.0):
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
    async def command(self, op, name=None, function=None, text=None):
        if op == "cancel":
            return await self.cancel()
        if op == "skip":
            return await self.skip()
        if op == "label":    # only a file next to the profile: allowed while something else runs
            err = self.set_label(clean_name(name), function, text)
            if err is None:
                await self.changed()
            return err
        if op in ("keep", "discard"):
            if self.phase != "review":
                return "nothing to decide"
            self.decision = op
            self._wake.set()
            return None
        if self.busy:
            return "busy"
        clean = clean_name(name)
        if op in ("learn", "use", "add", "remove", "delete") and clean is None:
            return "bad name"
        if op == "learn":
            if clean in self.profiles():
                return "a profile with that name already exists"
            self.mode, self.name, self.index, self.note, self.cancelled = "learn", clean, 0, "", False
            self.stage, self.restored, self.ok, self.text = "run", None, False, ""
            self.learned, self.phase, self.hits, self.strays, self.problems, self.decision = [], None, {}, {}, [], None
            self.skipped, self.ok_row, self.ok_hash = [], None, None
            self._before = self.active_names
            self._mark(True)
            self.task = asyncio.ensure_future(self._learn())
        elif op in ("use", "add", "remove"):
            if clean not in self.profiles():
                return "no such profile"
            now = self.active_names
            names = [clean] if op == "use" else now + [clean] if clean not in now else now
            if op == "remove":
                if clean not in now:
                    return "that profile is not in use"
                names = [n for n in now if n != clean]
                if not names:
                    return "at least one remote must stay in use"
            self.mode, self.name, self.cancelled, self.text = "use", clean, False, ""
            self._want = (op, names)
            self._mark(True)
            self.task = asyncio.ensure_future(self._use())
        elif op == "delete":
            if clean not in self.profiles():
                return "no such profile"
            if clean in self.active_names:
                return "that profile is in use - turn it off first"
            with contextlib.suppress(OSError):
                os.unlink(self._path(clean))
            with contextlib.suppress(OSError):
                os.unlink(self._labels_path(clean))
            self.text = f"{clean} deleted"
            self.log(f"remotes: {self.text}")
        else:
            return "unknown op"
        await self.changed()
        return None

    async def skip(self):
        """Leave out the function being asked for ("not set"); every function after Play / Pause can be skipped."""
        if not self.busy or self.mode != "learn" or self.phase is not None or not 0 < self.index < len(KEYS):
            return "this function cannot be skipped"
        self.skip_flag = True
        if self.proc:
            await self._kill(self.proc)
        return None

    async def cancel(self):
        if not self.busy or self.mode != "learn":   # a switch is short and always ends with a working table
            return None
        self.cancelled = True
        self._wake.set()          # a test step waiting for keys
        if self.proc:
            await self._kill(self.proc)
        return None

    # -- switching
    async def _apply(self, names):
        """Make the receiver's table = the profiles in `names` (one: its file as saved; several: one combined table).
        Returns (ok, notes). The caller keeps / restores the previous table."""
        if len(names) == 1:
            ok = await self._load(self._path(names[0]))
            notes = []
            if ok:
                notes = [f"{names[0]}: {k} is only the repeat part of a signal and fires for other keys too - learn it again"
                         for k in await self._bad_rows()]
            return ok, notes
        tables = []
        for n in names:
            try:
                head, rows = read_fcfg(self._path(n))
            except (OSError, ValueError) as e:
                return False, [f"{n}: {e}"]
            tables.append((n, head, rows))
        head, rows, notes = merge_tables(tables)
        path = os.path.join(self.dir, ".merged.fcfg")
        with open(path, "wb") as f:
            f.write(head + b"".join(rows))
        if not await self._load(path):
            return False, ["the receiver did not accept the combined table"]
        got = await self._table_hashes()
        if got is not None and got != sorted(row_hash(r) for r in rows):
            return False, [f"the receiver holds {len(got)} buttons instead of the expected {len(rows)}"]
        return True, notes

    async def _table_hashes(self):
        rc, out = await self._util("settings", timeout=15)
        if rc != 0:
            return None
        return sorted(h.upper() for _i, h, _ik, _id, _k in ROW.findall(out))

    async def _use(self):
        op, names = self._want
        name = self.name
        try:
            if not await self._keep_current():
                return await self._done_use("Could not save the current buttons - nothing changed")
            ok, notes = await self._apply(names)
            if ok:
                self._set_active(names)
                joined = " + ".join(names)
                text = {"remove": f"{name} turned off - now using {joined}", "add": f"{name} added - now using {joined}"
                        }.get(op, f"Now using {joined}")
                if notes:
                    self.log(f"remotes: {text}: {notes}")
                    text += " - " + "; ".join(notes)
                return await self._done_use(text)
            await self._restore()
            why = notes[0] if notes else "it did not load"
            return await self._done_use(f"Could not switch ({why}) - the previous buttons are back" if self.restored
                                        else f"Could not switch ({why}) - RESTORE FAILED")
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

    def _skip_current(self, why):
        """The function being asked for stays "not set"."""
        label = KEYS[self.index][1]
        self.skipped.append(self.index)
        self.log(f"remotes: {label} not set ({why})")
        self.note = f"{label} not set"
        self.index += 1

    async def _park_ok(self, row):
        """Take the Play / Pause row out of the receiver (kept in memory) while the other functions are learned: the
        receiver only says "button exists" for a known button, never which one, so with the row out a Play / Pause press
        is recorded like any button and its signal (hash) tells the player "leave this function out". True when done."""
        if not row:
            return False
        base = os.path.join(self.dir, ".learn")
        with contextlib.suppress(OSError):
            os.unlink(base + ".fcfg")
        rc, _ = await self._util("saveconfig", base)
        try:
            _h, rows = read_fcfg(base + ".fcfg")
        except (OSError, ValueError):
            return False
        mine = [r for r in rows if row_hash(r) == row[1]]
        if rc != 0 or not mine:
            return False
        rc, _ = await self._util("delete_index", str(row[0]), timeout=15)
        got = await self._table_hashes()
        if rc != 0 or got is None or row[1] in got:
            return False
        self.ok_row, self.ok_hash = mine[0], row[1]
        return True

    async def _unpark_ok(self):
        """Put the Play / Pause row back in front of what was learned (before the test). True when the receiver holds
        exactly the expected buttons (or nothing was parked)."""
        if self.ok_row is None:
            return True
        base = os.path.join(self.dir, ".learn")
        with contextlib.suppress(OSError):
            os.unlink(base + ".fcfg")
        rc, _ = await self._util("saveconfig", base)
        try:
            head, rows = read_fcfg(base + ".fcfg")
        except (OSError, ValueError):
            return False
        if rc != 0:
            return False
        rows = [self.ok_row] + [r for r in rows if row_hash(r) != self.ok_hash]
        hdr = bytearray(head)
        struct.pack_into("<I", hdr, 8, len(rows))
        path = base + "-full.fcfg"
        with open(path, "wb") as f:
            f.write(bytes(hdr) + b"".join(rows))
        if not await self._load(path):
            return False
        got = await self._table_hashes()
        if got is not None and got != sorted(row_hash(r) for r in rows):
            return False
        self.ok_row = None   # back in the receiver: a restore from here on uses the files only
        return True

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
                    self._skip_current("Skip on the phone")
                    continue
                rc, out = await self._util("record", key, timeout=self.timeout)
                if self.skip_flag:          # "Skip" on the phone killed the wait
                    self.skip_flag = False
                    self._skip_current("Skip on the phone")
                    rejects = 0
                    continue
                if rc is None:
                    why = "Stopped" if self.cancelled else "No button pressed - stopped"
                    return await self._finish(False, why + ", " + await self._restore())
                if "recorded button" in out:
                    row = await self._stored(key)
                    if row and self.ok_hash and row[1] == self.ok_hash:
                        # the Play / Pause button again = leave this function out (its row is out of the receiver, so the
                        # press was recorded like a new button; take that row out again)
                        await self._util("delete_index", str(row[0]), timeout=15)
                        self._skip_current("Play / Pause pressed")
                        rejects = 0
                        continue
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
                    self.learned.append(self.index)
                    if self.index == 0 and not await self._park_ok(row):
                        self.log("remotes: could not take Play / Pause out while learning - pressing it will not skip")
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
            if not await self._unpark_ok():
                return await self._finish(False, "Could not put Play / Pause back - stopped, " + await self._restore())
            self.problems = await self._verify()
            if self.cancelled:
                return await self._finish(False, "Stopped, " + await self._restore())
            kept_with_problems = False
            if self.problems:
                decision = await self._review()
                if decision != "keep":
                    why = "Stopped" if self.cancelled else "Discarded" if decision == "discard" else "No answer from the phone"
                    return await self._finish(False, f"Test failed ({'; '.join(self.problems)}). {why}, " + await self._restore())
                kept_with_problems = True
            self.phase = None
            rc, _ = await self._util("saveconfig", os.path.join(self.dir, self.name))
            if rc != 0 or not os.path.isfile(self._path(self.name)):
                return await self._finish(False, "Learned, but could not save the profile, " + await self._restore())
            skipped = len(self.skipped)
            text = (f"{self.name}: {learned} functions learned" + (f", {skipped} not set" if skipped else "")
                    + (" - kept although the test found problems" if kept_with_problems else ", test passed"))
            before = [n for n in self._before if n != self.name]
            if before:   # the new remote joins the ones in use: one combined table
                ok, notes = await self._apply(before + [self.name])
                if ok:
                    self._set_active(before + [self.name])
                    text += f"; now using {' + '.join(before + [self.name])}" + (" - " + "; ".join(notes) if notes else "")
                else:
                    await self._restore()
                    self._set_active(before)
                    text += f"; could not combine it with {' + '.join(before)} ({notes[0] if notes else 'not accepted'}), " \
                            f"{'those are' if len(before) > 1 else 'that one is'} back - use Add on the phone to try again"
            else:
                self._set_active([self.name])
            await self._finish(True, text)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never leave the screen stuck
            await self._finish(False, f"Error: {e!r:.60} - " + await self._restore())

    def _label_of(self, name):
        """Function label for a player key name (ok -> Play / Pause), or the key name itself when no function has it."""
        for flirc, label, _h, _o in KEYS:
            if PLAYER_KEY.get(flirc) == name:
                return label
        return name

    def _stray_list(self):
        return [f"{k} x{v}" if v > 1 else k for k, v in self.strays.items()]

    def _end_key(self):
        """(player key, function) that ends test step 2: Menu, or Play / Pause when Menu was left out."""
        return (PLAYER_KEY["F4"], "Menu") if 1 in self.learned else (PLAYER_KEY["return"], "Play / Pause")

    async def _wait_key(self, idle):
        """True when a key (or a stop) arrived within `idle` s, False on silence."""
        try:
            await asyncio.wait_for(self._wake.wait(), idle)
        except asyncio.TimeoutError:
            return False
        self._wake.clear()
        return True

    async def _verify(self):
        """Test what was learned, live, on the TV. Step 1: press every button you assigned (each lights up when the player
        gets its key). Step 2: press buttons you did NOT assign: nothing may happen (a stored repeat frame, a wrong or
        doubled button shows up as a function that fires). Returns a list of problems ([] = passed)."""
        fns = list(self.learned)
        expected = [KEYS[i][1] for i in fns]
        self.hits, self.strays, self.stage = {l: 0 for l in expected}, {}, "test"
        self.phase = "test1"
        await self.changed()
        self._wake.clear()
        while not self.cancelled and not all(self.hits.values()):
            got = await self._wait_key(self.test_idle)
            await self.changed()
            if not got:
                break
        if not self.cancelled:
            self.phase = "test2"
            self._wake.clear()
            await self.changed()
            while not self.cancelled:
                got = await self._wait_key(self.test_idle)
                await self.changed()
                if not got or self._menu_pressed:
                    break
        self._menu_pressed = False
        problems = []
        missing = [l for l, n in self.hits.items() if not n]
        if missing:
            problems.append("no answer from " + ", ".join(missing))
        if self.strays:
            problems.append("fired without being assigned: " + ", ".join(self._stray_list()))
        self.log(f"remotes: test {'passed' if not problems else 'FAILED: ' + '; '.join(problems)}")
        return problems

    async def _review(self):
        """Failed test: the phone decides (Keep / Discard). Returns 'keep', 'discard', or None (stopped / no answer)."""
        self.phase, self.decision = "review", None
        self._wake.clear()
        await self.changed()
        end = asyncio.get_running_loop().time() + self.review_for
        while not self.cancelled and self.decision is None:
            left = end - asyncio.get_running_loop().time()
            if left <= 0 or not await self._wait_key(left):
                break
        return None if self.cancelled else self.decision

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
        if self.stage == "test" and self.phase in ("test1", "test2"):
            label = self._label_of(name)
            end_key, end_label = self._end_key()
            if self.phase == "test1" and label in self.hits:
                self.hits[label] += 1
            elif self.phase == "test2" and name == end_key and end_label in self.hits:
                self._menu_pressed = True          # the Menu button (Play / Pause when Menu is not set) ends step 2
                self.hits[end_label] += 1
            else:
                self.strays[label] = self.strays.get(label, 0) + 1
            self._wake.set()
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
        text(120, 90, f"{'Test' if self.stage == 'test' else 'Learn'} remote: {self.name}", 56, WHITE, bold=True)
        if self.stage == "run":
            key, label, hint, optional = KEYS[min(self.index, len(KEYS) - 1)]
            text(mid, 250, f"{self.index + 1} / {len(KEYS)}", 44, GREY, an=8)
            text(mid, 340, "Press the button you want for", 56, GREY, an=8)
            text(mid, 440, label, 170, WHITE, an=8, bold=True)
            text(mid, 650, hint, 40, GREY, an=8)
            how = "once, on the remote" if self.index == 0 else \
                "once, on the remote  ·  your Play / Pause button = not set"
            text(mid, 740, self.note or how, 44, GREEN if "stored" in self.note else GREY, an=8)
            parts = []
            for n, (_k, lab, _h, _o) in enumerate(KEYS):
                if n in self.skipped:
                    parts.append(f"{{\\1c{DIM}\\s1}}{esc(lab)}{{\\s0}}")   # struck through: not set
                    continue
                col = GREEN if n < self.index else (BLUE if n == self.index else DIM)
                parts.append(f"{{\\1c{col}}}{esc(lab)}")
            ev.append(f"{{\\an8\\pos({mid},900)\\fs38\\bord0\\shad0\\b0}}" + "  ".join(parts))
            text(mid, 990, f"No press for {int(self.timeout)} s, or Cancel on the phone, stops and restores the old buttons",
                 32, DIM, an=8)
        elif self.stage == "test":
            if self.phase == "test1":
                text(mid, 220, "Test 1 / 2", 40, GREY, an=8)
                text(mid, 290, "Press each button you assigned", 62, WHITE, an=8, bold=True)
                text(mid, 380, "once. Each function lights up when its button works.", 38, GREY, an=8)
            elif self.phase == "test2":
                text(mid, 220, "Test 2 / 2", 40, GREY, an=8)
                text(mid, 290, "Now press buttons you did NOT assign", 62, WHITE, an=8, bold=True)
                text(mid, 380, "Colours, digits, anything else. Nothing should happen.", 38, GREY, an=8)
            else:
                text(mid, 220, "Test failed", 80, WARN, an=8, bold=True)
            y = 480
            for lab in [KEYS[i][1] for i in self.learned]:
                n = self.hits.get(lab, 0)
                text(mid - 220, y, ("✓  " if n else "·  ") + lab, 46, GREEN if n else DIM, an=7)
                y += 62 if len(self.learned) <= 8 else 52
            col = mid + 220         # right column: what fired by mistake, or (review) what the test found
            if self.phase == "review":
                y2 = 480
                for prob in self.problems[:3]:
                    for line in textwrap.wrap(prob, 30) or [""]:
                        text(col, y2, line, 36, WARN, an=7)
                        y2 += 46
                    y2 += 14
            elif self.strays:
                text(col, 480, "Fired without being assigned:", 34, WARN, an=7, bold=True)
                for k, line in enumerate(self._stray_list()[:6]):
                    text(col, 536 + k * 52, line, 44, WARN, an=7)
            if self.phase == "test2":
                text(mid, 990, f"When done, press your {self._end_key()[1]} button.", 36, GREY, an=8)
            elif self.phase == "review":
                text(mid, 990, "Choose on the phone: Keep or Discard (Discard puts the old buttons back)", 34, GREY, an=8)
            else:
                text(mid, 990, f"No key for {int(self.test_idle)} s, or Cancel on the phone, stops and restores the old buttons", 32, DIM, an=8)
        else:
            text(mid, 400, "Done" if self.ok else "Stopped", 120, GREEN if self.ok else BLUE, an=8, bold=True)
            text(mid, 580, self.text, 44, WHITE, an=8)
            hint = "Press any button" if self.ok or self.restored is None else (
                "Press any button (the old buttons work again)" if self.restored
                else "Choose a profile on the phone: Settings > Audio > TV Player > Remote")
            text(mid, 760, hint, 36, DIM, an=8)
        return "\n".join(ev)
