"""Learn remote: teaches a Flirc receiver the buttons of the remote, with every step shown on the TV.

Flow (menu row "Learn remote"): a confirm screen, then the buttons one after another: the screen names the button, the
user presses it once on the remote, `flirc_util record <key>` stores it, the next one follows. The old button table is
saved first (`flirc_util saveconfig`) and put back when the learning stops (no press for TIMEOUT seconds), so a failed
run never leaves the box without a working remote. While it runs the player ignores the remote's keys.

Pure logic + ASS renderer; the program is started through `spawn` (asyncio.create_subprocess_exec by default).
"""
import asyncio
import contextlib
import os
import time

RES_X, RES_Y = 1920, 1080
# (flirc key name, text on the screen): the Flirc sends the matching keyboard key, ir.EVDEV_KEYS turns it into the key
KEYS = [("return", "OK"), ("up", "UP"), ("down", "DOWN"), ("left", "LEFT"), ("right", "RIGHT"),
        ("pageup", "CH +"), ("pagedown", "CH -"), ("F1", "RED"), ("F2", "GREEN"), ("F3", "YELLOW"), ("F4", "BLUE")] + \
       [(str(n), str(n)) for n in (1, 2, 3, 4, 5, 6, 7, 8, 9, 0)]
TIMEOUT = 40.0            # s to wait for one press
MAX_REJECTS = 5           # presses refused ("already used") before giving up
FLASH = 1.0               # s the "stored" confirmation stays
RESULT_FOR = 30.0         # s the result screen stays when no button dismisses it

BG, BG_ALPHA = "&H101010&", "&H18&"
WHITE, GREY, DIM, BLUE, GREEN = "&HFFFFFF&", "&HB4B4B4&", "&H808080&", "&HE0A060&", "&H80D080&"


def esc(text):
    return str(text or "").replace("\\", "\\\u200b").replace("{", "\\{").replace("}", "\\}").replace("\n", " ")


class Learn:
    def __init__(self, util, backup_dir, redraw, log=lambda m: None, spawn=None, timeout=TIMEOUT, flash=FLASH,
                 result_for=RESULT_FOR):
        self.util, self.backup_dir, self.redraw, self.log = util, backup_dir, redraw, log
        self.spawn = spawn or asyncio.create_subprocess_exec
        self.timeout, self.flash, self.result_for = timeout, flash, result_for
        self.closer = None
        self.stage = None          # None | "confirm" | "run" | "result"
        self.index = 0             # button being learned
        self.note = ""             # one line under the big text
        self.result = ""           # result screen text
        self.ok = False
        self.restored = None       # after an abort: were the old buttons put back
        self.task = None
        self.backup = None

    @property
    def active(self):
        return self.stage is not None

    # -- control
    async def open(self):
        self.stage, self.index, self.note, self.result, self.ok = "confirm", 0, "", "", False
        self.restored = None

    def close(self):
        self.stage = None
        if self.closer:
            self.closer.cancel()
            self.closer = None

    async def key(self, name):
        """A remote key while the screen is up. Returns True when the screen changed."""
        if self.stage == "confirm":
            if name == "ok":
                self.stage = "run"
                self.task = asyncio.ensure_future(self._run())
            else:
                self.close()
            return True
        if self.stage == "result":
            self.close()
            return True
        return False  # running: the Flirc takes the presses, nothing else counts

    async def _util(self, *args, timeout=15.0):
        """(returncode or None on timeout, output)."""
        proc = await self.spawn(self.util, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return None, "timeout"
        return proc.returncode, (out or b"").decode(errors="replace")

    async def _finish(self, ok, text):
        self.ok, self.result, self.stage = ok, text, "result"
        self.log(f"learn: {text}")
        await self.redraw()
        self.closer = asyncio.ensure_future(self._auto_close())

    async def _auto_close(self):
        """The result screen leaves by itself (a remote that lost its buttons cannot dismiss it)."""
        await asyncio.sleep(self.result_for)
        if self.stage == "result":
            self.close()
            await self.redraw()

    async def _restore(self):
        """Old button table back: wipe the partial one first (loadconfig adds to what is stored)."""
        if not self.backup:
            return "no backup to restore"
        await self._util("format", timeout=30)
        rc, out = await self._util("loadconfig", self.backup, timeout=30)
        self.restored = rc == 0 and "rror" not in out
        return "old buttons restored" if self.restored else f"RESTORE FAILED: {' '.join(out.split())[:80]}"

    async def _run(self):
        try:
            os.makedirs(self.backup_dir, exist_ok=True)
            base = os.path.join(self.backup_dir, "flirc-before-" + time.strftime("%Y%m%d-%H%M%S"))
            rc, out = await self._util("saveconfig", base)       # the tool appends ".fcfg"
            self.backup = base + ".fcfg"
            if rc != 0 or not os.path.isfile(self.backup):
                return await self._finish(False, "Could not save the old buttons - nothing was changed")
            rc, out = await self._util("format", timeout=30)
            if rc != 0:
                return await self._finish(False, f"Could not clear the receiver: {' '.join(out.split())[:60]}")
            rejects = 0
            while self.index < len(KEYS):
                key, label = KEYS[self.index]
                await self.redraw()
                rc, out = await self._util("record", key, timeout=self.timeout)
                if rc is None:
                    return await self._finish(False, "No button pressed - stopped, " + await self._restore())
                if "recorded button" in out:
                    self.log(f"learn: {label} stored")
                    self.note = f"{label} stored"
                    self.index += 1
                    rejects = 0
                    if self.flash and self.index < len(KEYS):
                        await self.redraw()
                        await asyncio.sleep(self.flash)
                    self.note = ""
                    continue
                rejects += 1
                self.note = "That button is already used - press a different one" if "already exists" in out \
                    else "Not stored - press the button again"
                if rejects >= MAX_REJECTS:
                    return await self._finish(False, "Too many refused presses - stopped, " + await self._restore())
            await self._finish(True, f"All {len(KEYS)} buttons learned")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never leave the screen stuck
            await self._finish(False, f"Error: {e!r:.60} - " + await self._restore())

    # -- drawing
    def render(self):
        if self.stage is None:
            return ""
        ev = [f"{{\\an7\\pos(0,0)\\bord0\\shad0\\1c{BG}\\1a{BG_ALPHA}\\p1}}m 0 0 l {RES_X} 0 {RES_X} {RES_Y} 0 {RES_Y}{{\\p0}}"]

        def text(x, y, s, size, color=WHITE, an=7, bold=False):
            ev.append(f"{{\\an{an}\\pos({x},{y})\\fs{size}\\bord0\\shad0\\1c{color}\\b{1 if bold else 0}}}{esc(s)}")

        text(120, 90, "Learn remote", 56, WHITE, bold=True)
        mid = RES_X // 2
        if self.stage == "confirm":
            lines = ["Teach the receiver the buttons of your remote.",
                     f"The screen names {len(KEYS)} buttons one after another: press each one once, aimed at the receiver.",
                     "The old buttons are saved first and come back if you stop",
                     f"(no press for {int(self.timeout)} seconds)."]
            for n, s in enumerate(lines):
                text(120, 260 + n * 64, s, 40, GREY)
            text(120, 640, "OK  start", 52, GREEN, bold=True)
            text(120, 720, "Left / Red / Blue  cancel", 40, DIM)
        elif self.stage == "run":
            key, label = KEYS[self.index]
            text(mid, 270, f"{self.index + 1} / {len(KEYS)}", 44, GREY, an=8)
            text(mid, 380, "Press", 56, GREY, an=8)
            text(mid, 460, label, 190, WHITE, an=8, bold=True)
            text(mid, 720, self.note or "once, on the remote", 44, GREEN if "stored" in self.note else GREY, an=8)
            parts = []
            for n, (_k, lab) in enumerate(KEYS):
                col = GREEN if n < self.index else (BLUE if n == self.index else DIM)
                parts.append(f"{{\\1c{col}}}{esc(lab)}")
            ev.append(f"{{\\an8\\pos({mid},900)\\fs40\\bord0\\shad0\\b0}}" + "  ".join(parts))
            text(mid, 990, f"No press for {int(self.timeout)} s stops and restores the old buttons", 32, DIM, an=8)
        else:
            text(mid, 400, "Done" if self.ok else "Stopped", 120, GREEN if self.ok else BLUE, an=8, bold=True)
            text(mid, 580, self.result, 44, WHITE, an=8)
            hint = "Press any button" if self.ok or self.restored is None else (
                "Press any button (the old buttons work again)" if self.restored
                else f"Restore by hand: flirc_util loadconfig {self.backup}")
            text(mid, 760, hint, 36, DIM, an=8)
        return "\n".join(ev)
