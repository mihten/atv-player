"""IR remote input for mtv-player: raw LIRC receiver (/dev/lircN, MODE2) -> NEC / NEC-extended decode -> key names.

The kernel's own decoders and keymaps need sysfs writes on the host (read-only inside a container), so the raw pulse
stream is decoded here. Default key map: an LG TV's Magic Remote driving a "set-top box" through the TV's universal
control (brand Vip: NEC-extended, address 0xE386). Other remotes: MTV_IR_ADDRESS and MTV_IR_KEYS (JSON
{"0x0e": "ok", ...}).

A press is one frame; holding a key sends NEC repeat frames (~108 ms apart). Keys in HOLD_KEYS act again while held
(after HOLD_DELAY, every HOLD_EVERY); the others act once per press.
"""
import json
import os
import struct
import time

PULSE, SPACE, TIMEOUT, OVERFLOW = 0x01000000, 0x00000000, 0x03000000, 0x04000000
GAP_US = 15000           # a space this long ends a frame
REPEAT_WITHIN = 0.3      # s: a repeat frame later than this after the last frame belongs to nothing (lost press)
HOLD_DELAY = 0.5         # s held before a hold key acts again
HOLD_EVERY = 0.25        # s between actions while held

DEFAULT_ADDRESS = 0xE386
DEFAULT_KEYS = {
    0x0E: "ok", 0x40: "up", 0x41: "down", 0x07: "left", 0x06: "right",
    0x00: "ch_up", 0x01: "ch_down",
    0x0B: "red", 0x04: "green", 0x05: "yellow", 0x0C: "blue",
    0x10: "0", 0x11: "1", 0x12: "2", 0x13: "3", 0x14: "4", 0x15: "5", 0x16: "6", 0x17: "7", 0x18: "8", 0x19: "9",
}
HOLD_KEYS = {"left", "right", "up", "down"}


def _near(v, ref, tol):
    return abs(v - ref) <= ref * tol


def decode_nec(d):
    """d = [pulse, space, ..., pulse] in µs. Returns ("key", address, command), ("repeat",) or None.
    Plain NEC (second address byte = inverse of the first) gives the 8-bit address, NEC-extended the 16-bit one with
    the second byte high, as the kernel's scancode prints it (0xE386 = bytes 0x86, 0xE3)."""
    if len(d) < 3 or not _near(d[0], 9000, 0.25):
        return None
    if _near(d[1], 2250, 0.3) and len(d) <= 4:
        return ("repeat",)
    if not _near(d[1], 4500, 0.25) or len(d) < 2 + 64 + 1:
        return None
    v = 0
    for i in range(32):
        p, s = d[2 + 2 * i], d[3 + 2 * i]
        if not _near(p, 560, 0.5):
            return None
        if _near(s, 1690, 0.35):
            v |= 1 << i
        elif not _near(s, 560, 0.5):
            return None
    a, ai, c, ci = v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF
    if c ^ ci != 0xFF:
        return None
    return ("key", a if a ^ ai == 0xFF else (ai << 8) | a, c)


class Mode2:
    """Splits a MODE2 sample stream (4-byte words) into frames."""

    def __init__(self):
        self.frame = []
        self._rest = b""

    def feed(self, data):
        data = self._rest + data
        n = len(data) - len(data) % 4
        self._rest = data[n:]
        out = []
        for (word,) in struct.iter_unpack("I", data[:n]):
            kind, val = word & 0xFF000000, word & 0x00FFFFFF
            end = False
            if kind == PULSE:
                self.frame.append(val)
            elif kind == SPACE:
                if not self.frame:
                    continue
                if val > GAP_US:
                    end = True
                else:
                    self.frame.append(val)
            elif kind in (TIMEOUT, OVERFLOW):
                end = bool(self.frame)
            if end:
                if len(self.frame) % 2 == 0:
                    self.frame.pop()
                out.append(self.frame)
                self.frame = []
        return out


def parse_keys(text):
    """MTV_IR_KEYS JSON {"0x0e": "ok", "14": "ok"} -> {14: "ok"}; None when empty / invalid."""
    if not text:
        return None
    try:
        raw = json.loads(text)
        return {int(str(k), 0): str(v) for k, v in raw.items()}
    except (ValueError, TypeError, AttributeError):
        return None


class Keys:
    """Frames -> key presses. on_key(name, held) is called once per press, and again while a HOLD_KEYS key is held."""

    def __init__(self, on_key, address=DEFAULT_ADDRESS, keys=None, mono=time.monotonic, on_unknown=None):
        self.on_key, self.address, self.mono = on_key, address, mono
        self.keys = keys or DEFAULT_KEYS
        self.on_unknown = on_unknown
        self.last = None          # key name of the last frame (None after an unknown frame)
        self.last_at = -1e9       # time of the last frame or repeat of that key
        self.down_at = 0.0        # time of the press
        self.acted_at = 0.0       # time of the last action

    def frame(self, d):
        r = decode_nec(d)
        now = self.mono()
        if r is None:
            return
        if r[0] == "repeat":
            if self.last is None or now - self.last_at > REPEAT_WITHIN:
                return
            self.last_at = now
            if self.last in HOLD_KEYS and now - self.down_at >= HOLD_DELAY and now - self.acted_at >= HOLD_EVERY:
                self.acted_at = now
                self.on_key(self.last, True)
            return
        _, addr, cmd = r
        name = self.keys.get(cmd) if addr == self.address else None
        if name is None:
            self.last = None
            if self.on_unknown:
                self.on_unknown(addr, cmd)
            return
        self.last, self.last_at, self.down_at, self.acted_at = name, now, now, now
        self.on_key(name, False)


def open_device(path):
    """Non-blocking fd of a LIRC raw receiver, or None (missing, no permission)."""
    try:
        return os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
