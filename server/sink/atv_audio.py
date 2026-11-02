#!/usr/bin/env python3
"""atv-audio: plays the Apple TV's sound on a Linux box, in sync with the TV's video.

Connects to the media server as the sink of session "tv" (/ws?role=sink&session=tv), opens the item the TV
plays over HTTP (/media/<id>?audio=main) in mpv, and follows the TV's clock messages: seek when it is more than
150 ms off, otherwise trim mpv's speed by at most 0.2 % until aligned. `volume` messages set the output's own
volume (PipeWire / Pulse sink volume = a Bluetooth headset's absolute volume; ALSA device: mpv's volume).

Requirements: Python 3.9+, aiohttp, mpv on PATH (optional: pactl for PipeWire/PulseAudio output status).

Environment (see env.example):
  ATV_AUDIO_SERVER  server base URL, e.g. http://atv-media.local:8080 (required)
  ATV_AUDIO_OUTPUT  pipewire | pulse | pulse:<server> | alsa:<device>   (default pipewire)
  ATV_AUDIO_NAME    name shown on the TV (default atv-audio)
  ATV_AUDIO_MPV     mpv binary (default mpv)
  ATV_AUDIO_HOLD    seconds the output stays open after pause / stop / route off (default 300)

Run: python3 atv_audio.py
"""
import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import time
from collections import deque

# Sync controller (contract): seek above 150 ms, else speed trim within +-0.2 %, 1.0 again below 10 ms.
SEEK_ABOVE = 0.150
NEAR = 0.010
MAX_TRIM = 0.002
GAIN = 0.1             # speed change per second of error (20 ms -> full 0.2 % trim; ~10 s time constant)
SETTLE = 0.5           # s without corrections after a seek / start / buffering (audio-pts settles)
START_SEEK_ABOVE = 0.030  # at the start mpv is paused: a seek costs nothing audible
CONFIRM = 2            # consecutive ticks above SEEK_ABOVE before a seek (one noisy sample does not seek)
STALE_CLOCK = 3.0      # s without a fresh "playing" clock: the TV is gone, pause
TICK = 0.1
OUTPUT_POLL = 3.0
READY_CACHE = 1.0      # s of demuxer cache that counts as buffered
LIMIT = 0.8913         # -1 dBFS
AO_RETRY = 30.0        # s an output mpv could not open is reported not ready
SEEK_WAIT = 2.0        # s at most a reopen seek waits for mpv's playback-restart
PENDING = object()     # loadfile sent, its reply (playlist entry id) not read yet
PING_FAST, PING_SLOW, PING_FAST_COUNT = 2.0, 15.0, 8
VOLUME_KEEP = 2.0      # s the output poll keeps a volume just set (pactl may still report the old one)
PLAY_EDGE_KEEP = 2 * OUTPUT_POLL + 2  # s a Play counts for an output the poll reports back only afterwards


def log(msg):
    print(f"atv-audio: {msg}", file=sys.stderr, flush=True)


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.server = (env.get("ATV_AUDIO_SERVER") or "").rstrip("/")
        self.output = env.get("ATV_AUDIO_OUTPUT") or "pipewire"
        self.name = env.get("ATV_AUDIO_NAME") or "atv-audio"
        self.mpv = env.get("ATV_AUDIO_MPV") or "mpv"
        try:
            self.hold = float(env.get("ATV_AUDIO_HOLD") or 300)
        except ValueError:
            self.hold = 300.0

    @property
    def ws_url(self):
        base = re.sub(r"^http", "ws", self.server)
        return f"{base}/ws?role=sink&session=tv"

    def media_url(self, item_id):
        return f"{self.server}/media/{item_id}?audio=main"


def output_args(output):
    """mpv options for ATV_AUDIO_OUTPUT."""
    kind, _, arg = output.partition(":")
    if kind == "pipewire":
        return ["--ao=pipewire"]
    if kind == "pulse":
        return ["--ao=pulse"] + ([f"--pulse-host={arg}"] if arg else [])
    if kind == "alsa":
        return ["--ao=alsa", f"--audio-device=alsa/{arg or 'default'}"]
    raise ValueError(f"unknown ATV_AUDIO_OUTPUT {output!r}")


def mpv_args(cfg, sock):
    # keep-open=always: at the end of a file mpv pauses on it instead of closing it. audio-stream-silence:
    # while paused (and around seeks) mpv keeps the audio output open and writes silence instead of pausing
    # the stream. The PipeWire stream therefore stays active and the sink node is not suspended (PipeWire
    # suspends idle nodes after ~5 s; for Bluetooth that can release the A2DP transport and cut the first
    # second of the next song). Released only by 'stop' after ATV_AUDIO_HOLD. Unverified on real hardware.
    # gapless-audio=weak keeps the output open across 'loadfile replace' while the format stays the same
    # (with =no mpv 0.37 reopens it at every file).
    return [cfg.mpv, "--no-config", "--idle=yes", "--no-video", "--audio-display=no", "--keep-open=always",
            "--audio-pitch-correction=no", "--audio-stream-silence=yes", f"--input-ipc-server={sock}",
            "--no-input-terminal", "--quiet", "--msg-level=all=warn", "--ytdl=no", "--gapless-audio=weak",
            "--hr-seek=yes", "--cache=yes", "--cache-secs=60", "--demuxer-readahead-secs=30",
            "--demuxer-max-bytes=256MiB", "--demuxer-max-back-bytes=32MiB", "--network-timeout=15",
            f"--audio-client-name={cfg.name}"] + output_args(cfg.output)


# ---------------------------------------------------------------- mpv JSON IPC

class MpvError(Exception):
    pass


class MpvIpc:
    """Async client for mpv's JSON IPC. props holds the observed properties; on_event gets every event.
    Tests replace it with a fake that has the same command / get_property / set_property / props."""

    OBSERVE = ("pause", "paused-for-cache", "seeking", "idle-active", "eof-reached", "duration",
               "demuxer-cache-duration", "demuxer-cache-idle", "speed", "aid")

    def __init__(self, on_event=None):
        self.on_event = on_event
        self.props = {}
        self._reader = self._writer = None
        self._next = 0
        self._pending = {}
        self._task = None
        self.closed = asyncio.Event()

    async def connect(self, path):
        self._reader, self._writer = await asyncio.open_unix_connection(path, limit=1 << 22)
        self._task = asyncio.create_task(self._read())
        for i, name in enumerate(self.OBSERVE, 1):
            await self.command("observe_property", i, name)

    async def _read(self):
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "request_id" in msg and "event" not in msg:
                    fut = self._pending.pop(msg["request_id"], None)
                    if fut and not fut.done():
                        if msg.get("error") == "success":
                            fut.set_result(msg.get("data"))
                        else:
                            fut.set_exception(MpvError(msg.get("error")))
                    continue
                if msg.get("event") == "property-change":
                    self.props[msg.get("name")] = msg.get("data")
                if self.on_event:
                    try:
                        self.on_event(msg)
                    except Exception as e:  # never let a handler kill the reader
                        log(f"event handler: {e!r}")
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(MpvError("ipc closed"))
            self._pending.clear()
            self.closed.set()

    async def command(self, *args, timeout=5.0):
        if self.closed.is_set() or not self._writer:
            raise MpvError("ipc closed")
        self._next += 1
        rid = self._next
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._writer.write((json.dumps({"command": list(args), "request_id": rid}) + "\n").encode())
        try:
            await self._writer.drain()
            return await asyncio.wait_for(fut, timeout)
        except (OSError, asyncio.TimeoutError) as e:
            self._pending.pop(rid, None)
            raise MpvError(repr(e))

    async def get_property(self, name, default=None):
        try:
            return await self.command("get_property", name)
        except MpvError:
            return default

    async def set_property(self, name, value):
        return await self.command("set_property", name, value)

    async def close(self):
        if self._writer:
            self._writer.close()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task


class MpvProcess:
    """Runs mpv, connects the IPC, restarts mpv when it dies (backoff 1 ... 30 s)."""

    def __init__(self, cfg, sink):
        self.cfg, self.sink = cfg, sink
        self.dir = tempfile.mkdtemp(prefix="atv-audio-")
        self.sock = os.path.join(self.dir, "mpv.sock")
        self.proc = None

    async def _spawn(self):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.sock)
        self.proc = await asyncio.create_subprocess_exec(*mpv_args(self.cfg, self.sock),
                                                         stdin=asyncio.subprocess.DEVNULL)
        for _ in range(100):
            if self.proc.returncode is not None:
                raise MpvError(f"mpv exited at start (code {self.proc.returncode})")
            if os.path.exists(self.sock):
                try:
                    ipc = MpvIpc(self.sink.on_mpv_event)
                    await ipc.connect(self.sock)
                    return ipc
                except OSError:
                    pass
            await asyncio.sleep(0.1)
        raise MpvError("mpv IPC socket did not appear")

    async def supervise(self):
        delay, first = 1.0, True
        while True:
            started = time.monotonic()
            ipc = None
            try:
                ipc = await self._spawn()
                await self.sink.on_mpv(ipc, restarted=not first)
                first = False
                waiter = asyncio.create_task(self.proc.wait())
                closed = asyncio.create_task(ipc.closed.wait())
                await asyncio.wait({waiter, closed}, return_when=asyncio.FIRST_COMPLETED)
                closed.cancel()
                waiter.cancel()
            except FileNotFoundError:
                self.sink.log(f"mpv: {self.cfg.mpv} not found")
            except (MpvError, OSError) as e:
                self.sink.log(f"mpv: {e}")
            except Exception as e:
                self.sink.log(f"mpv: unexpected {e!r}")
            self.sink.on_mpv_gone()
            if ipc:
                await ipc.close()
            await self.kill()
            self.sink.log(f"mpv stopped (code {self.proc.returncode if self.proc else None}), restarting")
            if time.monotonic() - started > 60:
                delay = 1.0
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def kill(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


# ---------------------------------------------------------------- clock + sync

class ClockOffset:
    """server time = local monotonic + offset; offset from the ping with the lowest round trip of the last 8."""

    def __init__(self, keep=8):
        self.samples = deque(maxlen=keep)

    def add(self, t_send, t_recv, t_server):
        rtt = t_recv - t_send
        if rtt < 0:
            return
        self.samples.append((rtt, t_server - (t_send + t_recv) / 2))

    def reset(self):
        self.samples.clear()

    @property
    def best(self):
        return min(self.samples) if self.samples else None

    @property
    def offset(self):
        b = self.best
        return b[1] if b else None


class SyncController:
    """Decides seek / speed from target (where the TV says the sink should be) and mpv's position."""

    def __init__(self):
        # learned: how far a seek while playing lands behind its target (with audio-stream-silence about the
        # audio buffer, ~0.2 s: the seek target is heard after the silence already queued)
        self.lead = 0.0
        self.reset(0.0)

    def reset(self, now):
        self.settle_until = now + SETTLE
        self.over = 0
        self.speed = 1.0
        self.learn = False
        self.last_error = None

    def defer(self, now):
        """Seeking / buffering: no decisions until SETTLE after it ends."""
        self.settle_until = max(self.settle_until, now + SETTLE)
        self.over = 0

    def step(self, now, target, pos):
        """Returns ("seek", position), ("speed", value) or None."""
        if pos is None or now < self.settle_until:
            return None
        err = target - pos
        self.last_error = err
        if self.learn:  # first settled sample after our seek: how far behind its target it landed
            self.lead = min(max(self.lead + err, 0.0), 0.5)
            self.learn = False
        if abs(err) > SEEK_ABOVE:
            self.over += 1
            if self.over < CONFIRM:
                return None
            self.reset(now)
            self.learn = True
            self.last_error = err
            return ("seek", max(0.0, target + self.lead))
        self.over = 0
        speed = 1.0 if abs(err) < NEAR else 1.0 + min(max(GAIN * err, -MAX_TRIM), MAX_TRIM)
        if speed != self.speed and (speed == 1.0 or abs(speed - self.speed) >= 0.0001):
            self.speed = speed
            return ("speed", speed)
        return None


# ---------------------------------------------------------------- sound (lavfi)

def _num(x):
    s = f"{float(x):.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _bands(sound):
    freqs = list(sound.get("freqs") or [])
    bands = list(sound.get("bands") or [])
    n = min(len(freqs), len(bands))
    return freqs[:n], bands[:n]


def sound_chain(sound):
    """mpv --af / 'af set' value for a sound message; "" = no filters."""
    if not sound or not sound.get("processed"):
        return ""
    freqs, bands = _bands(sound)
    parts = [f"@vol:lavfi-volume=volume={_num(sound.get('gain') or 0)}dB"]
    parts += [f"@eq{i}:lavfi-equalizer=f={_num(f)}:t=o:w=1:g={_num(g)}"
              for i, (f, g) in enumerate(zip(freqs, bands))]
    parts.append(f"@lim:lavfi-alimiter=limit={LIMIT}:attack=1.5:release=100:level=disabled")
    return ",".join(parts)


def sound_commands(old, new):
    """af-command argument lists turning chain `old` into `new` live, or None when the chain must be set anew
    (processed switched, band count changed, nothing applied yet). The last argument names the libavfilter
    filter inside mpv's wrapper graph: without it mpv 0.37 applies the command but reports an error."""
    if not old or not new or not old.get("processed") or not new.get("processed"):
        return None
    of, ob = _bands(old)
    nf, nb = _bands(new)
    if len(of) != len(nf):
        return None
    cmds = []
    if _num(old.get("gain") or 0) != _num(new.get("gain") or 0):
        cmds.append(["vol", "volume", f"{_num(new.get('gain') or 0)}dB", "volume"])
    for i in range(len(nf)):
        if _num(of[i]) != _num(nf[i]):
            cmds.append([f"eq{i}", "f", _num(nf[i]), "equalizer"])
        if _num(ob[i]) != _num(nb[i]):
            cmds.append([f"eq{i}", "g", _num(nb[i]), "equalizer"])
    return cmds


# ---------------------------------------------------------------- output status

def _sink_output(name, desc, props, vol=None):
    props = props or {}
    display = props.get("device.description") or desc or name
    if (props.get("device.api") == "bluez5" or props.get("device.bus") == "bluetooth"
            or name.startswith("bluez")):
        out = {"kind": "bluetooth", "name": display, "codec": props.get("api.bluez5.codec") or None,
               "ready": True}
    else:
        usb = (props.get("device.bus") == "usb" or "usb" in (props.get("device.bus-path") or "")
               or "usb" in (props.get("device.bus_path") or "") or props.get("alsa.driver_name") == "snd_usb_audio")
        out = {"kind": "usb" if usb else "other", "name": display, "codec": None, "ready": True}
    if vol and vol.get("volume") is not None:
        out["volume"], out["muted"] = vol["volume"], bool(vol.get("muted"))
    return out


def _percent(values):
    """Sink volume in % (the loudest channel), never above 100 for the status."""
    nums = []
    for v in values:
        m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*%", str(v))
        if m:
            nums.append(float(m.group(1)))
    return min(100, int(round(max(nums)))) if nums else None


def _json_volume(s):
    vol = s.get("volume")
    if not isinstance(vol, dict):
        return {}
    pct = _percent(ch.get("value_percent") for ch in vol.values() if isinstance(ch, dict))
    return {} if pct is None else {"volume": pct, "muted": bool(s.get("mute"))}


def parse_pactl_json(text):
    """[(name, description, properties, {volume, muted})] from 'pactl -f json list sinks'."""
    out = []
    for s in json.loads(text or "[]"):
        if isinstance(s, dict) and s.get("name"):
            out.append((s["name"], s.get("description") or "", s.get("properties") or {}, _json_volume(s)))
    return out


def parse_pactl_text(text):
    """The same from plain 'pactl list sinks' (LC_ALL=C), for pactl without -f json."""
    out, cur, in_props = [], None, False
    for line in (text or "").splitlines():
        if line.startswith("Sink #"):
            cur = {"name": "", "desc": "", "props": {}, "vol": {}}
            out.append(cur)
            in_props = False
            continue
        if cur is None:
            continue
        s = line.strip()
        if line.startswith("\t") and not line.startswith("\t\t"):
            in_props = s == "Properties:"
            if s.startswith("Name:"):
                cur["name"] = s[5:].strip()
            elif s.startswith("Description:"):
                cur["desc"] = s[12:].strip()
            elif s.startswith("Mute:"):
                cur["vol"]["muted"] = s[5:].strip().lower() == "yes"
            elif s.startswith("Volume:"):
                pct = _percent(re.findall(r"(\d+(?:\.\d+)?%)", s))
                if pct is not None:
                    cur["vol"]["volume"] = pct
        elif in_props and " = " in s:
            k, _, v = s.partition(" = ")
            cur["props"][k.strip()] = v.strip().strip('"')
    return [(c["name"], c["desc"], c["props"], c["vol"]) for c in out if c["name"]]


def choose_sink(sinks, default):
    """The sink mpv plays on: the default one, else the first (never PipeWire's dummy)."""
    sinks = [s for s in sinks if not s[0].startswith("auto_null")]
    return next((s for s in sinks if s[0] == default), None) or (sinks[0] if sinks else None)


def pick_output(sinks, default, preferred):
    """(output, preferred) for the sinks list and the default sink name. preferred = the last Bluetooth / USB
    sink seen ({"key", "output"}): when it is gone, it is reported with ready false even if a fallback exists."""
    sinks = [s for s in sinks if not s[0].startswith("auto_null")]
    by_name = {s[0]: s for s in sinks}
    chosen = choose_sink(sinks, default)
    if chosen:
        out = _sink_output(*chosen)
        if out["kind"] in ("bluetooth", "usb"):
            return out, {"key": chosen[0], "output": out}
        if preferred and preferred["key"] not in by_name:
            return dict(preferred["output"], ready=False), preferred
        return out, preferred
    if preferred:
        return dict(preferred["output"], ready=False), preferred
    return None, None


def parse_asound_cards(text):
    """[{index, id, driver, name, usb}] from /proc/asound/cards."""
    cards = []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*(\d+)\s+\[(\S+)\s*\]:\s*(\S+)\s+-\s+(.*)$", line)
        if m:
            cards.append({"index": int(m.group(1)), "id": m.group(2), "driver": m.group(3),
                          "name": m.group(4).strip(), "usb": m.group(3) == "USB-Audio"})
        elif cards and "usb-" in line:
            cards[-1]["usb"] = True
    return cards


def alsa_output(device, cards_text):
    """Output status for ALSA device `device` (hw:1,0 / plughw:CARD=X,DEV=0 / default ...)."""
    cards = parse_asound_cards(cards_text)
    ref = None
    m = re.search(r"CARD=([^,]+)", device) or re.match(r"^(?:plug)?hw:([^,]+)", device)
    if m:
        ref = m.group(1)
    card = None
    if ref is None:
        card = cards[0] if cards else None
    else:
        for c in cards:
            if (ref.isdigit() and c["index"] == int(ref)) or c["id"] == ref:
                card = c
    if not card:
        return {"kind": "other", "name": ref or device, "codec": None, "ready": False} if ref else None
    return {"kind": "usb" if card["usb"] else "other", "name": card["name"], "codec": None, "ready": True}


async def _run(*args, env_extra=None, timeout=3.0):
    env = dict(os.environ, LC_ALL="C", **(env_extra or {}))
    try:
        p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.DEVNULL, env=env)
    except (FileNotFoundError, PermissionError):
        return None
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return None
    return out.decode(errors="replace") if p.returncode == 0 else None


class OutputWatch:
    def __init__(self, cfg):
        self.cfg = cfg
        self.preferred = None
        self.no_tool = False
        self.last = None
        self.fails = 0
        self.key = None          # pactl name of the sink the reported output is (None: not ready / unknown)

    def _pactl(self):
        kind, _, arg = self.cfg.output.partition(":")
        return ["pactl"] + (["-s", arg] if kind == "pulse" and arg else [])

    @property
    def has_pactl(self):
        return not self.cfg.output.startswith("alsa") and shutil.which("pactl") is not None

    async def set_volume(self, percent, muted):
        """The output's own volume (a Bluetooth sink's = the headset's absolute volume). True when pactl took it."""
        if not self.key:
            return False
        pa = self._pactl()
        vol = await _run(*pa, "set-sink-volume", self.key, f"{int(percent)}%")
        mute = await _run(*pa, "set-sink-mute", self.key, "1" if muted else "0")
        return vol is not None and mute is not None

    async def detect(self):
        kind, _, arg = self.cfg.output.partition(":")
        if kind == "alsa":
            try:
                with open("/proc/asound/cards") as f:
                    text = f.read()
            except OSError:
                return {"kind": "other", "name": arg or "ALSA", "codec": None, "ready": True}
            return alsa_output(arg or "default", text)
        if shutil.which("pactl") is None:
            self.no_tool = True  # status unknown: assume the output works
            return {"kind": "other", "name": self.cfg.output, "codec": None, "ready": True}
        pa = self._pactl()
        default = (await _run(*pa, "get-default-sink") or "").strip()
        text = await _run(*pa, "-f", "json", "list", "sinks")
        sinks = None
        if text:
            with contextlib.suppress(ValueError):
                sinks = parse_pactl_json(text)
        if sinks is None:
            text = await _run(*pa, "list", "sinks")
            if text is None:
                return self._no_answer(kind)
            sinks = parse_pactl_text(text)
        self.fails = 0
        out, self.preferred = pick_output(sinks, default, self.preferred)
        chosen = choose_sink(sinks, default)
        self.key = chosen[0] if chosen and out and out.get("ready") else None
        self.last = out
        return out

    def _no_answer(self, kind):
        """pactl is installed but no Pulse / PipeWire server answers (mpv cannot open the output either).
        One miss keeps the last result (a busy box), from the second one the output is not ready."""
        self.fails += 1
        if self.last is not None and self.fails < 2:
            return self.last
        self.key = None
        if kind == "pipewire" and pipewire_socket():
            return {"kind": "other", "name": "PipeWire", "codec": None, "ready": True}  # no Pulse layer
        return dict(self.last or {"kind": "other", "name": self.cfg.output, "codec": None}, ready=False)


def pipewire_socket():
    run_dir = os.environ.get("PIPEWIRE_RUNTIME_DIR") or os.environ.get("XDG_RUNTIME_DIR")
    return bool(run_dir) and os.path.exists(os.path.join(run_dir, os.environ.get("PIPEWIRE_REMOTE") or "pipewire-0"))


# ---------------------------------------------------------------- sink logic

class Sink:
    def __init__(self, cfg, send=None, mono=time.monotonic):
        self.cfg = cfg
        self._send = send
        self.mono = mono
        self.mpv = None
        self.clock_offset = ClockOffset()
        self.ctl = SyncController()
        self.lock = asyncio.Lock()
        self.routed = False
        self.item = None             # item of the last load
        self.load_position = 0.0
        self.clock = None
        self.clock_seq = 0
        self.block_seq = -1          # pause/seek/stop control: ignore clocks received before it ...
        self.block_at = None         # ... and clocks the TV sent before it (server time)
        self.lip_ms = -100           # the TV sends its own on connect (default -100 ms)
        self.sound = None
        self.applied_sound = None
        self.af_text = None          # the 'af' property as last set (af-command changes do not update it)
        self.output = None
        self.output_ok = False
        self.lost_item = None        # output gone during this item: silent until the TV's next play / seek / load
        self.play_edge_at = None     # when the TV's clock last went to "playing" while the output was missing
        self.volume_backend = None   # async (percent, muted) -> bool (pactl); None: mpv's own volume (ALSA output)
        self.want_volume = None
        self.volume_task = None
        self.volume_at = -1e9        # last volume set: the output poll keeps it for a moment (pactl may lag)
        self.mpv_volume = (100, False)
        self.failed_id = None        # mpv could not play this item: no reload until the next load / route on
        self.ao_error_until = 0.0    # mpv could not open the output: reported not ready until then
        self.want_loaded = False
        self.loaded_id = None        # what mpv has opened (or is opening)
        self.file_loaded = False
        self.load_gen = 0            # +1 per loadfile; a file-loaded handler of an older one drops out
        self.expect_entry = None     # mpv playlist entry id of the last loadfile (None: mpv gave none)
        self.cur_entry = None        # entry id of mpv's last start-file event
        self.seek_wait = None        # time of a reopen seek not finished yet (playback-restart pending)
        self.load_t = 0.0
        self.pending_ready = None
        self.hold_since = None
        self.speed = 1.0
        self._seek_logged = 0.0
        self._seeks_unlogged = 0
        self._err_logged = -1e9
        self._pings = {}
        self.req_pos = None          # position of the last load / seek while paused (None once playing)
        self.paused_lead = 0.0

    # -- plumbing
    def log(self, message):
        log(message)
        self.send({"type": "log", "message": message[:1500]})

    def send(self, msg):
        if self._send:
            try:
                self._send(msg)
            except Exception:
                pass

    def warn(self, message):
        """Error line at most every 10 s (a lasting fault would otherwise log at the tick rate)."""
        now = self.mono()
        if now - self._err_logged >= 10:
            self._err_logged = now
            self.log(message)

    def server_now(self):
        off = self.clock_offset.offset
        return None if off is None else self.mono() + off

    def status_message(self):
        return {"type": "sink", "name": self.cfg.name, "output": self.output}

    async def on_mpv(self, ipc, restarted=False):
        self.mpv = ipc
        self.loaded_id, self.file_loaded, self.pending_ready = None, False, None
        self.expect_entry, self.failed_id, self.seek_wait = None, None, None
        self.load_gen += 1
        self.applied_sound, self.af_text, self.speed = None, None, 1.0
        self.ctl.reset(self.mono())
        if restarted:
            self.log("mpv restarted")
        if self.volume_backend is None and self.mpv_volume != (100, False):
            await self._mpv_set_volume(*self.mpv_volume)  # a new mpv starts at 100 %
        await self.apply_sound()
        await self.tick()

    def on_mpv_gone(self):
        self.mpv = None
        self.loaded_id, self.file_loaded, self.expect_entry = None, False, None
        self.load_gen += 1

    def _is_current(self, entry):
        """Whether an mpv event with playlist_entry_id `entry` belongs to the last loadfile."""
        if self.expect_entry is PENDING:
            return False
        return entry is None or self.expect_entry is None or entry == self.expect_entry

    def on_mpv_event(self, ev):
        name = ev.get("event")
        if name == "start-file":
            self.cur_entry = ev.get("playlist_entry_id")
        elif name == "file-loaded":
            asyncio.ensure_future(self.on_file_loaded(self.cur_entry))
        elif name == "playback-restart":
            self.seek_wait = None
        elif name == "end-file" and ev.get("reason") == "error" and self._is_current(ev.get("playlist_entry_id")):
            err = str(ev.get("file_error") or "")
            self.log(f"mpv could not play {self.loaded_id}: {err}")
            self.failed_id = self.loaded_id
            self.loaded_id, self.file_loaded, self.pending_ready = None, False, None
            self.load_gen += 1
            if "audio output" in err:
                self.ao_error_until = self.mono() + AO_RETRY
                self.set_output(self.output or {"kind": "other", "name": self.cfg.output, "codec": None,
                                                "ready": True})

    async def on_file_loaded(self, entry=None):
        mpv, lid, gen = self.mpv, self.loaded_id, self.load_gen
        if not mpv or lid is None or self.expect_entry is PENDING:
            return  # the loadfile reply is not in yet: this event is the previous file's
        if entry is None or self.expect_entry is None:  # mpv without entry ids: compare the path
            if await mpv.get_property("path") != self.cfg.media_url(lid):
                return
        elif entry != self.expect_entry:
            return
        item = self.item if self.item and self.item.get("id") == lid else {}
        tracks = await mpv.get_property("track-list", []) or []
        audio = [t for t in tracks if t.get("type") == "audio"]
        aid = (int(item.get("audio_ord") or 0) + 1) if len(audio) > 1 else 1
        if aid > len(audio):
            aid = 1
        cur = next((t.get("id") for t in audio if t.get("selected")), None)
        if audio and cur != aid:
            with contextlib.suppress(MpvError):
                await mpv.set_property("aid", aid)
        if gen != self.load_gen or self.loaded_id != lid or self.mpv is not mpv:
            return  # another load, an end-file error or an mpv restart during the awaits
        self.file_loaded = True
        await self.tick()

    # -- messages from the server
    async def handle(self, msg):
        t = msg.get("type")
        if t == "pong":
            self.on_pong(msg)
            return
        if t == "load":
            self.item = msg.get("item")
            self.load_position = float(msg.get("position") or 0)
            self.failed_id = None
            # output missing at the start: this item stays silent until the TV's next play / seek
            self.lost_item = None if self.output_ok else (self.item or {}).get("id")
            self.want_loaded = bool(self.item) and self.routed
            if self.want_loaded and self.loaded_id is not None and self.loaded_id == self.item.get("id"):
                await self._reopen()
        elif t == "route":
            routed = bool(msg.get("sink"))
            if routed != self.routed:
                self.routed = routed
                self.log(f"route: {'sink' if routed else 'TV'}")
                if routed:
                    self.failed_id = None
            if routed and self.item:
                self.want_loaded = True
                if self.loaded_id is not None and self.loaded_id == self.item.get("id") and self.pending_ready is None:
                    # the TV may wait for 'ready' (it reconnected and got the load again; this sink did not)
                    self.pending_ready, self.load_t = self.loaded_id, self.mono()
        elif t == "clock":
            prev, self.clock = self.clock, msg
            self.clock_seq += 1
            if msg.get("state") == "playing" and (not prev or prev.get("state") != "playing"
                                                 or prev.get("item") != msg.get("item")):
                if self.lost_item is not None and self.output_ok:
                    self._sound_back()  # Play / seek on the TV after the output came back
                elif not self.output_ok:
                    self.play_edge_at = self.mono()  # Play / seek while the output is still reported missing
        elif t == "control":
            await self.on_control(msg)
        elif t == "lip_sync":
            with contextlib.suppress(TypeError, ValueError):
                self.lip_ms = max(-200, min(400, int(msg.get("ms"))))
                self.ctl.reset(self.mono())
        elif t == "sound":
            self.sound = msg
            await self.apply_sound()
            return
        elif t == "volume":
            self.on_volume(msg)
            return
        else:
            return
        await self.tick()

    def _block(self):
        self.block_seq = self.clock_seq
        self.block_at = self.server_now()

    async def on_control(self, msg):
        action = msg.get("action")
        mpv = self.mpv
        playing = mpv and self.loaded_id is not None and mpv.props.get("pause") is False
        if action in ("pause", "stop") or (action == "toggle" and playing):
            self._block()
            await self._pause()
        elif action == "seek" and mpv and self.file_loaded:
            self._block()
            pos = max(0.0, float(msg.get("position") or 0) + self.lip_ms / 1000)
            if mpv.props.get("pause") is True:
                pos += self.paused_lead
                self.req_pos = pos
            with contextlib.suppress(MpvError):
                await mpv.command("seek", pos, "absolute+exact")
            await self._reset_sync()

    def on_pong(self, msg):
        sent = self._pings.pop(msg.get("n"), None)
        if sent is not None and isinstance(msg.get("t"), (int, float)):
            self.clock_offset.add(sent, self.mono(), float(msg["t"]))

    def reset_clock(self):
        """New server connection: the server may have restarted (new monotonic base)."""
        self.clock_offset.reset()
        self._pings.clear()
        self.block_at = None

    def make_ping(self, n):
        self._pings[n] = self.mono()
        if len(self._pings) > 16:
            self._pings.pop(min(self._pings))
        return {"type": "ping", "n": n}

    # -- output
    def set_output(self, out, volume_set=False):
        if out and out.get("ready") and self.mono() < self.ao_error_until:
            out = dict(out, ready=False)  # mpv could not open it a moment ago
        if out and self.volume_backend is None:
            out = dict(out, volume=self.mpv_volume[0], muted=self.mpv_volume[1])  # mpv's own volume
        elif (out and not volume_set and self.output and "volume" in self.output and out.get("name") == self.output.get("name")
              and self.mono() - self.volume_at < VOLUME_KEEP):
            out = dict(out, volume=self.output["volume"], muted=self.output["muted"])  # just set, pactl may lag
        if out == self.output:
            return False
        old_ok = self.output_ok
        old = self.output or {}
        self.output = out
        self.output_ok = bool(out and out.get("ready"))
        same = {k: v for k, v in old.items() if k not in ("volume", "muted")}
        if out and same == {k: v for k, v in out.items() if k not in ("volume", "muted")}:
            pass  # only the volume moved: no log line (a slider sends 10 per second)
        elif out:
            self.log(f"output: {out['kind']} {out['name']} codec {out['codec'] or 'unknown'}"
                     f"{'' if out['ready'] else ' (not ready)'}"
                     f"{'' if out.get('volume') is None else ' volume %d %%%s' % (out['volume'], ' muted' if out.get('muted') else '')}")
        else:
            self.log("output: none found")
        if old_ok and not self.output_ok and self.item:
            self.lost_item = self.item.get("id")
            self.play_edge_at = None
        elif (not old_ok and self.output_ok and self.lost_item is not None and self.play_edge_at is not None
              and self.mono() - self.play_edge_at < PLAY_EDGE_KEEP and (self.clock or {}).get("state") == "playing"
              and (self.clock or {}).get("item") == self.lost_item):
            self._sound_back()  # Play pressed just before the output poll saw the headphones again
        self.send(self.status_message())
        return True

    def _sound_back(self):
        """Output usable again and the TV just played / seeked: this item has sound again."""
        if self.failed_id == self.lost_item:
            self.failed_id = None  # mpv dropped the file when the output went: open it again
        self.lost_item = None

    # -- volume
    def on_volume(self, msg):
        """volume {percent 0..100, muted}: the output's own volume. Nothing while the output is missing (the
        headphones keep their own volume when they come back). The latest value wins while one is being set."""
        try:
            percent = max(0, min(100, int(round(float(msg.get("percent"))))))
        except (TypeError, ValueError, OverflowError):
            return None
        muted = bool(msg.get("muted"))
        if not self.output_ok and self.volume_backend is not None:
            return None  # pactl: the device keeps its own volume
        self.want_volume = (percent, muted)
        if self.volume_backend is None:
            self.mpv_volume = (percent, muted)  # mpv's own volume: kept (and applied) without an output too
        self.volume_at = self.mono()
        if self.output:
            self.set_output(dict(self.output, volume=percent, muted=muted), volume_set=True)  # status now, poll confirms
        if self.volume_task is None or self.volume_task.done():
            self.volume_task = asyncio.ensure_future(self._apply_volume())
        return self.volume_task

    async def _apply_volume(self):
        while self.want_volume is not None:
            (percent, muted), self.want_volume = self.want_volume, None
            try:
                if self.volume_backend is None:
                    ok = await self._mpv_set_volume(percent, muted)
                else:
                    ok = await self.volume_backend(percent, muted)
            except Exception as e:
                ok = False
                self.warn(f"volume: {e!r}")
            if not ok:
                self.warn(f"volume {percent} %{' muted' if muted else ''}: not set")

    async def _mpv_set_volume(self, percent, muted):
        mpv = self.mpv
        if not mpv:
            return True  # set when mpv (re)starts
        try:
            await mpv.set_property("volume", percent)
            await mpv.set_property("mute", muted)
        except MpvError:
            return False
        return True

    # -- sound
    def _chain_live(self):
        """mpv has a file open with its filters built: values can change live with af-command."""
        return self.file_loaded and self.loaded_id is not None and self.expect_entry is not PENDING

    async def _set_af(self, mpv, chain):
        await mpv.set_property("af", chain)
        self.af_text = chain

    async def apply_sound(self):
        """Live when a file plays (af-command on the labelled filters, no gap). With no file open (idle, or a
        load in progress: the TV sends the next track's gain right with its load) the 'af' property is set: the
        new file builds its chain from it, nothing plays, so nothing is rebuilt audibly."""
        mpv = self.mpv
        if not mpv or self.sound is None:
            return
        cmds = sound_commands(self.applied_sound, self.sound) if self._chain_live() else None
        try:
            if cmds is None:
                chain = sound_chain(self.sound)
                if chain != self.af_text or self._chain_live():
                    await self._set_af(mpv, chain)
            else:
                for c in cmds:
                    await mpv.command("af-command", *c)
            self.applied_sound = self.sound
        except MpvError as e:
            playing = mpv.props.get("pause") is False
            self.log(f"sound: {e}, rebuilding the filter chain ({'playing: a short gap' if playing else 'paused: inaudible'})")
            with contextlib.suppress(MpvError):
                await self._set_af(mpv, sound_chain(self.sound))
                self.applied_sound = self.sound

    async def _af_for_next_file(self, mpv):
        """Before a loadfile: a new file builds its filters from the 'af' property, which af-command does not
        update. Paused here, so setting it is inaudible."""
        if self.sound is None:
            return
        chain = sound_chain(self.sound)
        if chain != self.af_text:
            with contextlib.suppress(MpvError):
                await self._set_af(mpv, chain)
                self.applied_sound = self.sound

    # -- playback
    def target(self, snow):
        """Where mpv should be now (TV clock + lip sync), or None without a clock for the current item."""
        c, it = self.clock, self.item
        if not c or not it or c.get("item") != it.get("id"):
            return None
        pos = float(c.get("position") or 0)
        if c.get("state") == "playing" and snow is not None:
            pos += snow - float(c.get("at") or snow)
        return pos + self.lip_ms / 1000

    def desired_playing(self, snow):
        c, it = self.clock, self.item
        return bool(self.routed and self.output_ok and it and c and snow is not None
                    and c.get("item") == it.get("id") and c.get("state") == "playing"
                    and self.clock_seq > self.block_seq and self.lost_item != it.get("id")
                    and (self.block_at is None or float(c.get("at") or 0) > self.block_at)
                    and snow - float(c.get("at") or 0) < STALE_CLOCK)

    @staticmethod
    async def position(mpv):
        pos = await mpv.get_property("audio-pts")
        if pos is None:
            pos = await mpv.get_property("time-pos")
        return pos

    async def _pause(self):
        mpv = self.mpv
        if mpv and mpv.props.get("pause") is not True:
            with contextlib.suppress(MpvError):
                await mpv.set_property("pause", True)
            mpv.props["pause"] = True
        await self._reset_sync()

    async def _reset_sync(self, controller=True):
        if controller:
            self.ctl.reset(self.mono())
        mpv = self.mpv
        if mpv and self.speed != 1.0:
            with contextlib.suppress(MpvError):
                await mpv.set_property("speed", 1.0)
            self.speed = 1.0

    async def _load(self, position):
        mpv, it = self.mpv, self.item
        self.loaded_id, self.file_loaded = it["id"], False
        self.pending_ready, self.load_t = it["id"], self.mono()
        self.hold_since = self.seek_wait = None
        self.load_gen += 1
        gen = self.load_gen
        self.expect_entry = PENDING
        await self._pause()
        await self._af_for_next_file(mpv)
        try:
            self.req_pos = max(0.0, position + self.paused_lead)
            await mpv.set_property("start", f"{self.req_pos:.3f}")
            reply = await mpv.command("loadfile", self.cfg.media_url(it["id"]), "replace")
        except MpvError as e:
            self.log(f"load {it['id']} failed: {e}")
            if gen == self.load_gen:
                self.loaded_id, self.pending_ready, self.expect_entry = None, None, None
                self.failed_id = it["id"]
            return
        if gen == self.load_gen:
            self.expect_entry = reply.get("playlist_entry_id") if isinstance(reply, dict) else None
        mpv.props["eof-reached"] = False
        self.log(f"load {it['id']} at {position:.2f}")

    async def _reopen(self):
        """load of the item mpv already has open (repeat one, jump to the current item, server replay): back to
        the start position, paused, and 'ready' again once buffered (the TV waits for it)."""
        mpv, it = self.mpv, self.item
        if not mpv or not self.file_loaded:
            await self._load(self.load_position + self.lip_ms / 1000)  # still opening: open it again
            return
        self._block()  # clocks of the previous lap
        self.pending_ready, self.load_t = it["id"], self.mono()
        self.hold_since = None
        await self._pause()
        self.req_pos = max(0.0, self.load_position + self.lip_ms / 1000 + self.paused_lead)
        mpv.props["eof-reached"] = False
        self.seek_wait = self.mono()
        try:
            await mpv.command("seek", self.req_pos, "absolute+exact")
        except MpvError as e:
            self.seek_wait = None
            self.log(f"reopen {it['id']} seek failed: {e}")
        self.log(f"load {it['id']} again at {self.load_position:.2f}")

    def _buffered(self, mpv):
        p = mpv.props
        if self.seek_wait is not None and self.mono() - self.seek_wait < SEEK_WAIT:
            return False
        return (self.file_loaded and not p.get("seeking") and not p.get("paused-for-cache")
                and ((p.get("demuxer-cache-duration") or 0) >= READY_CACHE or p.get("demuxer-cache-idle")
                     or p.get("eof-reached")))

    async def tick(self):
        if not self.mpv:
            return
        async with self.lock:
            try:
                await self._tick()
            except MpvError as e:
                self.warn(f"tick: {e}")
            except Exception as e:  # never let one race stop the sync loop or the connection
                self.warn(f"tick: unexpected {e!r}")

    async def _tick(self):
        mpv = self.mpv
        if mpv is None:
            return
        now, snow = self.mono(), self.server_now()
        p = mpv.props
        it = self.item
        desired = self.desired_playing(snow)
        if desired:
            self.want_loaded = True
        if (self.want_loaded and it and self.routed and self.loaded_id != it.get("id")
                and self.lost_item != it["id"] and self.failed_id != it["id"]):
            t = self.target(snow)
            await self._load(self.load_position + self.lip_ms / 1000 if t is None else t)
            return
        if self.pending_ready is not None and self.pending_ready == self.loaded_id and self._buffered(mpv):
            self.send({"type": "ready", "item": self.pending_ready})
            self.log(f"ready {self.pending_ready} after {int((now - self.load_t) * 1000)} ms"
                     f" (cache {p.get('demuxer-cache-duration') or 0:.1f} s)")
            self.pending_ready = None
        if self.loaded_id is None:
            self.hold_since = None
            return
        if not desired or self.loaded_id != (it or {}).get("id"):
            if p.get("pause") is not True:
                await self._pause()
            if self.hold_since is None:
                self.hold_since = now
            elif now - self.hold_since >= self.cfg.hold:
                await mpv.command("stop")
                self.log(f"output released after {int(self.cfg.hold)} s idle")
                self.loaded_id, self.file_loaded, self.pending_ready = None, False, None
                self.want_loaded, self.hold_since = False, None
            return
        self.hold_since = None
        if not self.file_loaded or p.get("seeking") or p.get("paused-for-cache"):
            self.ctl.defer(now)
            return
        target = self.target(snow)
        if p.get("pause") is True:
            await self._start(mpv, target)
            return
        act = self.ctl.step(now, target, await self.position(mpv))
        if act and act[0] == "seek":
            await mpv.command("seek", act[1], "absolute+exact")
            await self._reset_sync(controller=False)  # step() reset itself and learns from this seek
            self._log_seek(self.ctl.last_error)
        elif act and act[0] == "speed":
            await mpv.set_property("speed", act[1])
            self.speed = act[1]

    async def _start(self, mpv, target):
        dur = mpv.props.get("duration")
        if target < 0 or (dur and target >= dur - (1.0 if mpv.props.get("eof-reached") else 0.05)):
            return  # lip sync behind the start (wait), or at the end (keep-open paused there: no replay)
        pos = await self.position(mpv)
        if pos is not None and self.req_pos is not None:
            # with audio-stream-silence a paused mpv reports the requested position minus the audio buffered
            # behind the silence (~0.2 s): seek that much further, and load that much further next time
            self.paused_lead = min(max(self.req_pos - pos, 0.0), 0.5)
            if not self.ctl.lead:
                self.ctl.lead = self.paused_lead  # first guess for seeks while playing
        if pos is None or abs(target - pos) > START_SEEK_ABOVE:
            await mpv.command("seek", target + self.paused_lead, "absolute+exact")
        self.req_pos = None
        await mpv.set_property("pause", False)
        mpv.props["pause"] = False
        await self._reset_sync()

    def _log_seek(self, err):
        self._seeks_unlogged += 1
        now = self.mono()
        if now - self._seek_logged >= 10:
            self.log(f"sync seek, error {int((err or 0) * 1000)} ms ({self._seeks_unlogged} in the last"
                     f" {int(min(now - self._seek_logged, 999))} s)")
            self._seek_logged, self._seeks_unlogged = now, 0


# ---------------------------------------------------------------- main

async def run(cfg):
    import aiohttp

    ws_box = {"ws": None}

    def send(msg):
        ws = ws_box["ws"]
        if ws is not None and not ws.closed:
            asyncio.ensure_future(ws.send_str(json.dumps(msg)))

    sink = Sink(cfg, send)
    outputs = OutputWatch(cfg)
    if outputs.has_pactl:
        sink.volume_backend = outputs.set_volume
    sink.set_output(await outputs.detect())
    if outputs.no_tool:
        log("pactl not available: output status not detected")
    proc = MpvProcess(cfg, sink)

    async def poll_outputs():
        while True:
            await asyncio.sleep(OUTPUT_POLL)
            try:
                if sink.set_output(await outputs.detect()):
                    await sink.tick()
            except Exception as e:
                log(f"output check: {e!r}")

    async def ticker():
        while True:
            await asyncio.sleep(TICK)
            try:
                await sink.tick()
            except Exception as e:
                sink.warn(f"ticker: {e!r}")

    async def pinger(ws):
        n = 0
        while not ws.closed:
            n += 1
            await ws.send_str(json.dumps(sink.make_ping(n)))
            await asyncio.sleep(PING_FAST if n < PING_FAST_COUNT else PING_SLOW)

    tasks = [asyncio.create_task(proc.supervise()), asyncio.create_task(poll_outputs()),
             asyncio.create_task(ticker())]
    backoff = 1.0
    try:
        async with aiohttp.ClientSession() as http:
            while True:
                ping_task = None
                try:
                    async with http.ws_connect(cfg.ws_url, heartbeat=20, max_msg_size=0) as ws:
                        backoff = 1.0
                        ws_box["ws"] = ws
                        sink.reset_clock()
                        ping_task = asyncio.create_task(pinger(ws))
                        send(sink.status_message())
                        sink.log(f"connected ({cfg.output})")
                        async for m in ws:
                            if m.type != aiohttp.WSMsgType.TEXT:
                                break
                            try:
                                msg = json.loads(m.data)
                            except ValueError:
                                continue
                            if isinstance(msg, dict):
                                try:
                                    await sink.handle(msg)
                                except Exception as e:  # one bad message must not stop the sink
                                    sink.warn(f"message {msg.get('type')}: {e!r}")
                except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
                    log(f"server: {type(e).__name__}: {e}")
                finally:
                    ws_box["ws"] = None
                    if ping_task:
                        ping_task.cancel()
                log(f"no server connection, retrying in {int(backoff)} s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
    finally:
        for t in tasks:
            t.cancel()
        await proc.kill()
        proc.cleanup()


def main():
    cfg = Config()
    if not cfg.server:
        sys.exit("atv-audio: set ATV_AUDIO_SERVER (e.g. http://<server>:8080)")
    try:
        output_args(cfg.output)
    except ValueError as e:
        sys.exit(f"atv-audio: {e}")
    loop = asyncio.new_event_loop()
    task = loop.create_task(run(cfg))
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    with contextlib.suppress(asyncio.CancelledError):
        loop.run_until_complete(task)
    loop.close()


if __name__ == "__main__":
    main()
