#!/usr/bin/env python3
"""Sennheiser BTD 700 USB Bluetooth dongle for atv-audio: headphone state, codec, connect / disconnect and the
headphone buttons, read from the dongle's HID interface (a Linux hidraw node). The dongle pairs and encodes Bluetooth
by itself and Linux only sees a USB sound card, so this channel is the only way to know whether the headphones are
there and which codec they use.

Protocol (reverse-engineered by https://github.com/sobalap/btd700ctl, LGPL-2.1; the few messages used here are
reimplemented in Python, no C library needed): HID report 0x34, 64-byte packets [0x34, marker, id, length, data...].
Marker 0xFE = command from the host, 0xFF = the dongle's answer (same id), 0xFC = an event the dongle sends by itself
(headphones connected / gone, codec changed). The same HID interface carries report 0x01, the consumer-control keys
(two bytes of bits) through which the headphone buttons arrive.

Node: interface 0 of USB 3542:3001 (the one with report 0x34; interface 1 is a different vendor channel). Pass its
hidraw node to atv-audio as ATV_AUDIO_DONGLE=/dev/hidrawN (a udev rule can give it a fixed name).
"""
import asyncio
import contextlib
import errno
import os

REPORT, KEYS_REPORT = 0x34, 0x01
HOST_CMD, DONGLE_RSP, DONGLE_EVT = 0xFE, 0xFF, 0xFC
CMD_CODEC_IN_USE, CMD_STATE, CMD_FIRMWARE, CMD_BT_CONNECT = 0x05, 0x06, 0x12, 0x14
EVT_CODEC, EVT_STATE = 0x04, 0x0F
PACKET = 64

STATES = {1: "disconnected", 2: "connected", 3: "streaming", 4: "voice"}
READY_STATES = ("connected", "streaming", "voice")
# bit i of the dongle's codec mask -> codec id (the ids PipeWire / the apps already know: aptx_adaptive -> "aptX Adaptive")
CODECS = ("sbc", "aptx", "aptx_adaptive", "aptx_lossless", "aptx_lite", "lc3")
# report 0x01 bits in the HID descriptor's order: usages CD B5 B6 B7 B0 B1 B3 B4 (byte 1), E9 EA E2 (byte 2)
KEYS = ("playpause", "next", "prev", "stop", "play", "pause", "ff", "rew", "volup", "voldown", "mute")

NAME = "BTD 700"
RETRY = 5.0      # s between attempts to open the node (dongle unplugged / not passed in)
POLL = 15.0      # s between state checks while open (events carry the changes; this only covers a missed one)
TIMEOUT = 1.0    # s for an answer


def open_node(path):
    return os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)


def codec_name(mask):
    """Lowest set bit of the codec mask -> codec id; None when no codec is in use (headphones idle / gone)."""
    for i, name in enumerate(CODECS):
        if mask & (1 << i):
            return name
    return None


class Btd700:
    def __init__(self, path, on_change=None, on_key=None, log=print, opener=open_node):
        self.path = path
        self.on_change = on_change   # (): state, codec or presence changed
        self.on_key = on_key         # (name): a headphone button was pressed (KEYS)
        self.log = log
        self.opener = opener
        self.fd = None
        self.state = None            # STATES value; None = no dongle (not open yet / unplugged)
        self.codec = None            # last codec seen in use (kept while the headphones idle)
        self.firmware = None
        self.keys = 0
        self.pending = {}
        self.lock = None
        self.closed = None
        self.first = None            # set after the first open attempt and state read (atv-audio waits for it)
        self._refreshing = None

    # -- what atv-audio reports
    @property
    def present(self):
        return self.fd is not None and self.state is not None

    @property
    def ready(self):
        return self.state in READY_STATES

    def output(self):
        """The sink's output dict, or None without a dongle (atv-audio then reports the plain ALSA card)."""
        if not self.present:
            return None
        return {"kind": "bluetooth", "name": NAME, "codec": self.codec, "ready": self.ready}

    def info(self):
        return {"name": NAME, "state": self.state if self.present else None, "codec": self.codec,
                "firmware": self.firmware}

    # -- packets
    def feed(self, data):
        """One HID report from the node."""
        if len(data) >= 3 and data[0] == KEYS_REPORT:
            bits = data[1] | (data[2] << 8)
            pressed, self.keys = bits & ~self.keys, bits
            for i, name in enumerate(KEYS):
                if pressed & (1 << i) and self.on_key:
                    self.on_key(name)
            return
        if len(data) < 4 or data[0] != REPORT:
            return
        marker, ident = data[1], data[2]
        if marker == DONGLE_RSP:
            fut = self.pending.pop(ident, None)
            if fut and not fut.done():
                fut.set_result(bytes(data[4:]))
        elif marker == DONGLE_EVT:
            payload = bytes(data[4:4 + data[3]])
            if ident == EVT_STATE and payload:
                self._set(state=STATES.get(payload[0], "disconnected"))
                self._schedule_refresh()
            elif ident == EVT_CODEC:
                self._schedule_refresh()

    async def command(self, ident, args=b""):
        if self.fd is None:
            raise OSError(errno.ENODEV, "dongle not open")
        if self.lock is None:
            self.lock = asyncio.Lock()
        async with self.lock:
            fut = asyncio.get_running_loop().create_future()
            self.pending[ident] = fut
            packet = bytes([REPORT, HOST_CMD, ident, len(args)]) + bytes(args)
            try:
                os.write(self.fd, packet.ljust(PACKET, b"\0"))
                return await asyncio.wait_for(fut, TIMEOUT)
            finally:
                if self.pending.get(ident) is fut:
                    del self.pending[ident]

    async def refresh(self):
        """Read state and codec now (after an event, on a poll, from Settings -> atv-audio)."""
        st = await self.command(CMD_STATE)
        codec = await self.command(CMD_CODEC_IN_USE)
        mask = (codec[0] | (codec[1] << 8)) if len(codec) >= 2 else (codec[0] if codec else 0)
        self._set(state=STATES.get(st[0], "disconnected") if st else "disconnected", codec=codec_name(mask))

    async def connect(self, on=True):
        """Settings -> atv-audio: connect to / disconnect from the paired headphones. True when the dongle answered."""
        try:
            await self.command(CMD_BT_CONNECT, bytes([1 if on else 0]))
        except (OSError, asyncio.TimeoutError) as e:
            self.log(f"dongle: {'connect' if on else 'disconnect'}: no answer ({type(e).__name__})")
            return False
        self.log(f"dongle: {'connect' if on else 'disconnect'} sent")
        self._schedule_refresh()
        return True

    def _set(self, state=None, codec=None):
        changed = False
        if state is not None and state != self.state:
            self.log(f"dongle: headphones {state}" + (f" ({self.codec})" if state == "streaming" and self.codec else ""))
            self.state, changed = state, True
        if codec is not None and codec != self.codec:
            self.log(f"dongle: codec {codec}")
            self.codec, changed = codec, True
        if changed and self.on_change:
            self.on_change()

    def _schedule_refresh(self):
        if self._refreshing is None or self._refreshing.done():
            self._refreshing = asyncio.ensure_future(self._refresh_quietly())

    async def _refresh_quietly(self):
        await asyncio.sleep(0.2)  # the dongle sends several events in a row; read once after them
        with contextlib.suppress(OSError, asyncio.TimeoutError):
            await self.refresh()

    # -- node
    def _readable(self):
        if self.fd is None:
            return
        try:
            data = os.read(self.fd, PACKET)
        except BlockingIOError:
            return
        except OSError as e:
            self._lost(e)
            return
        if not data:
            self._lost(OSError(errno.ENODEV, "end of file"))
            return
        self.feed(data)

    def _lost(self, err):
        if self.closed and not self.closed.done():
            self.closed.set_exception(err)

    def _close(self):
        if self.fd is not None:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(self.fd)
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = None
        for fut in self.pending.values():
            if not fut.done():
                fut.cancel()
        self.pending.clear()
        self.keys = 0
        if self.state is not None:
            self.log("dongle: gone")
            self.state = None
            if self.on_change:
                self.on_change()

    async def run(self):
        loop = asyncio.get_running_loop()
        self.first = self.first or asyncio.Event()
        said = None
        while True:
            try:
                self.fd = self.opener(self.path)
            except OSError as e:
                if said != e.errno:
                    self.log(f"dongle: {self.path}: {e.strerror or e} (retrying every {int(RETRY)} s)")
                    said = e.errno
                self.first.set()
                await asyncio.sleep(RETRY)
                continue
            said = None
            self.closed = loop.create_future()
            loop.add_reader(self.fd, self._readable)
            try:
                fw = await self.command(CMD_FIRMWARE)
                if len(fw) >= 4:
                    self.firmware = f"{fw[0]}.{fw[1]}.{fw[2] | (fw[3] << 8)}"
                await self.refresh()
                self.log(f"dongle: {NAME} firmware {self.firmware or '?'}, headphones {self.state}")
                self.first.set()
                while True:
                    done, _ = await asyncio.wait([self.closed], timeout=POLL)
                    if done:
                        self.closed.result()  # raises the read error
                    await self.refresh()
            except (OSError, asyncio.TimeoutError) as e:
                self.log(f"dongle: {self.path}: {e.strerror if isinstance(e, OSError) and e.strerror else type(e).__name__}"
                         f", reopening in {int(RETRY)} s")
            finally:
                self._close()
                self.first.set()
            await asyncio.sleep(RETRY)
