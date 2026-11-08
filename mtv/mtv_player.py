#!/usr/bin/env python3
"""mtv-player: a video player for a TV screen, played straight from a Linux box's HDMI output.

Joins the media server as the player of session "mtv" (/ws?role=player&kind=mtv) and plays that queue in mpv on the
DRM/KMS output (no desktop, no compositor): video with VAAPI decoding, sound over HDMI. The screen stays in ONE mode
(MTV_DRM_MODE, e.g. 3840x2160@60) for everything; mpv retimes video to it (display-resample) and blends frames of
24/25/50 fps material (interpolation), so the TV never switches modes. The server owns the queue; this program
speaks the player protocol: load / control / sound in, status / ended / error / log out. Sound settings (equalizer,
gain, limiter) come from the server like for the atv-audio box player and use the same filter chain.

mpv keeps running when nothing plays (idle, black screen), so the box's text console never shows on the TV.

Requirements: Python 3.9+, aiohttp, mpv 0.38+ with the drm gpu context; the sink's atv_audio.py (shared code) next
to this file or in ../server/sink.

Environment:
  MTV_SERVER         server base URL, e.g. http://atv-media.local:8080 (required)
  MTV_AUDIO_DEVICE   mpv audio device (default alsa/hdmi: check `mpv --audio-device=help`)
  MTV_DRM_CONNECTOR  DRM connector name as `mpv --drm-connector=help` lists it, e.g. DP-1 (default: mpv picks the
                     first connected one; mpv 0.40 rejected the "0.DP-1" form its mode list prints)
  MTV_DRM_MODE       DRM mode index or WxH@Hz (default 0 = the screen's first mode; list: mpv --drm-mode=help)
  MTV_INTERPOLATION  1 (default) / 0: blend frames when the video rate is not the screen rate
  MTV_HWDEC          mpv hwdec (default vaapi)
  MTV_VOLUME         mpv volume 0-100 at the first start (default 100); later the web remote's volume bar sets it and it
                     is kept in MTV_STATE (default $STATE_DIRECTORY): the TV's own volume is separate
  MTV_MPV            mpv binary (default mpv)
  MTV_MPV_ARGS       extra mpv options, shell-quoted (e.g. tone mapping)
  MTV_IR_DEVICE      raw LIRC receiver for the remote (default /dev/lirc0; "off" = no remote)
  MTV_IR_ADDRESS     the remote's NEC address (default 0xE386: LG Magic Remote, set-top box profile "Vip")
  MTV_IR_KEYS        JSON command -> key name map replacing the default (see ir.py), e.g. {"0x0e": "ok"}
  MTV_KEYS_DEVICE    keyboard-style receiver (Flirc) as a path or glob, e.g. /dev/input/by-id/*flirc*event-kbd; grabbed,
                     key events: Enter=OK, arrows, PgUp/PgDn=CH+/CH-, F1-F4=red/green/yellow/blue, 0-9 (see ir.EVDEV_KEYS)
  MTV_IR_DEBUG       1: log every decoded IR frame with its time (press, repeat, other remotes) to the journal
  MTV_FLIRC_UTIL     flirc_util binary (default: PATH or /usr/local/bin); with MTV_KEYS_DEVICE the web remote
                     (Settings > Audio > TV Player > Remote) can learn, switch and delete remote profiles (remotes.py)

Remote keys (ir.py names), menu closed: OK = play / pause, Blue = open the menu, CH+ / CH- = next / previous, Right / Left = +10 / -10 s,
Up / Down = +60 / -60 s (Left/Right/Up/Down repeat while held; each shows a short text). Menu open (menu.py): Up / Down
move, CH+ / CH- page, OK / Right select, Left back, Blue / Red close. A held key counts as one press (arrows
repeat while held). The menu's first row is Play / Pause (the menu stays open).

Run: python3 mtv_player.py
"""
import asyncio
import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
for _d in (HERE, os.path.join(HERE, "..", "server", "sink")):
    if os.path.isfile(os.path.join(_d, "atv_audio.py")):
        sys.path.insert(0, os.path.abspath(_d))
        break
from atv_audio import MpvError, MpvIpc, gain_text, sound_chain, sound_commands  # noqa: E402
import ir  # noqa: E402
import menu as menu_mod  # noqa: E402
import remotes as remotes_mod  # noqa: E402

VERSION = "0.6.0"
STATUS_EVERY = 1.0       # s between status messages while something is loaded
TICK = 0.25
RECONNECT_MAX = 30.0
# Cheap HDR -> SDR: the default peak detection + tone mapping drops ~15 frames/s of 4K60 HLG on a Gen 9.5 iGPU.
DEFAULT_TONE = ["--hdr-compute-peak=no", "--tone-mapping=clip", "--gamut-mapping-mode=clip", "--dither=no"]


def log(msg):
    print(f"mtv-player: {msg}", file=sys.stderr, flush=True)


def flag(value, default=True):
    if value is None or value == "":
        return default
    return str(value).strip().lower() not in ("0", "no", "off", "false")


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.server = (env.get("MTV_SERVER") or "").rstrip("/")
        self.audio_device = env.get("MTV_AUDIO_DEVICE") or "alsa/hdmi"
        self.connector = env.get("MTV_DRM_CONNECTOR") or ""
        self.mode = env.get("MTV_DRM_MODE") or "0"
        self.interpolation = flag(env.get("MTV_INTERPOLATION"))
        self.hwdec = env.get("MTV_HWDEC") or "vaapi"
        try:
            self.volume = max(0, min(100, int(env.get("MTV_VOLUME") or 100)))
        except ValueError:
            self.volume = 100
        self.mpv = env.get("MTV_MPV") or "mpv"
        self.state_dir = env.get("MTV_STATE") or env.get("STATE_DIRECTORY") or ""
        self.keys_device = env.get("MTV_KEYS_DEVICE") or ""
        self.flirc_util = env.get("MTV_FLIRC_UTIL") or shutil.which("flirc_util") or "/usr/local/bin/flirc_util"
        self.extra = shlex.split(env.get("MTV_MPV_ARGS") or "")
        dev = env.get("MTV_IR_DEVICE")
        self.ir_device = "" if (dev or "").lower() in ("off", "0", "no", "none") else (dev or "/dev/lirc0")
        try:
            self.ir_address = int(env.get("MTV_IR_ADDRESS") or "0", 0) or ir.DEFAULT_ADDRESS
        except ValueError:
            self.ir_address = ir.DEFAULT_ADDRESS
        self.ir_keys = ir.parse_keys(env.get("MTV_IR_KEYS"))
        self.ir_debug = flag(env.get("MTV_IR_DEBUG"), default=False)

    @property
    def can_learn(self):
        """The menu offers "Learn remote" when a Flirc is configured and its tool is installed."""
        return bool(self.keys_device) and os.access(self.flirc_util, os.X_OK)

    @property
    def ws_url(self):
        return re.sub(r"^http", "ws", self.server) + "/ws?role=player&kind=mtv"

    def media_url(self, item_id):
        return f"{self.server}/media/{item_id}"


def mpv_args(cfg, sock, volume=None, muted=False):
    """mpv for the TV: DRM output in one fixed mode, VAAPI, HDMI sound, idle black screen between items.
    keep-open=yes: at the end of a file mpv stays on its last frame (eof-reached) instead of closing it, so the end
    is reported once and the next load replaces it without a flash of the console. keep-open-pause=no: mpv does not
    pause itself there (its own pause raced with the next load's unpause: the next item stayed paused)."""
    args = [cfg.mpv, "--no-config", "--idle=yes", "--force-window=yes", "--keep-open=yes", "--keep-open-pause=no",
            "--vo=gpu-next", "--gpu-context=drm", f"--drm-mode={cfg.mode}", f"--hwdec={cfg.hwdec}",
            "--video-sync=display-resample", "--ao=alsa", f"--audio-device={cfg.audio_device}",
            f"--volume={cfg.volume if volume is None else volume}", f"--mute={'yes' if muted else 'no'}",
            "--volume-max=100", "--audio-display=embedded-first", "--sid=no",
            "--hr-seek=yes", "--cache=yes", "--cache-secs=60", "--demuxer-readahead-secs=30",
            "--demuxer-max-bytes=512MiB", "--demuxer-max-back-bytes=64MiB", "--network-timeout=15",
            "--ytdl=no", "--no-input-terminal", "--input-default-bindings=no", "--osd-level=1", "--osc=no",
            "--quiet", "--msg-level=all=warn", "--background-color=#000000", f"--input-ipc-server={sock}"]
    if cfg.connector:
        args.append(f"--drm-connector={cfg.connector}")
    if cfg.interpolation:
        args += ["--interpolation", "--tscale=oversample"]
    return args + DEFAULT_TONE + cfg.extra


class Player:
    """The player of session "mtv". Gets server messages (handle) and mpv events (on_mpv_event); tick() reports
    status / ended / error. Holds no queue: every item comes from a server `load`."""

    def __init__(self, cfg, send=None, mono=time.monotonic):
        self.cfg = cfg
        self._send = send
        self.mono = mono
        self.mpv = None
        self.item = None
        self.want_play = False
        self.pos = 0.0
        self.loads = 0               # +1 per load from the server
        self.entry = None            # mpv playlist entry id of the current load (None: unknown / nothing loaded)
        self.file_loaded = False
        self.failed_for = self.ended_for = None
        self.sound = None            # last sound message
        self.applied = None          # sound message the running filters were built from / changed to
        self.af_text = None          # last chain written to mpv's 'af' property
        self.last_status, self.status_key = -1e9, None
        self.volume, self.muted = cfg.volume, False
        self.screen = None           # "3840x2160 @ 60 Hz", read from mpv once the window is up
        self.load_volume()
        self.state = None            # last session state from the server (queue, current, playing, shuffle, ...)
        self.http = None             # aiohttp session for the menu's lists (set while connected)
        self.menu = menu_mod.Menu(self.fetch, self.send, lambda: self.state)
        self.remotes = remotes_mod.Remotes(cfg.flirc_util, cfg.state_dir or tempfile.gettempdir(), self.remotes_changed,
                                           self.log)

    # -- volume: mpv's own (software) volume; the TV / soundbar volume is separate
    def _volume_file(self):
        return os.path.join(self.cfg.state_dir, "volume.json") if self.cfg.state_dir else None

    def load_volume(self):
        path = self._volume_file()
        if not path:
            return
        try:
            with open(path) as f:
                data = json.load(f)
            self.volume = max(0, min(100, int(data.get("volume"))))
            self.muted = bool(data.get("muted"))
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def save_volume(self):
        path = self._volume_file()
        if not path:
            return
        with contextlib.suppress(OSError):
            os.makedirs(self.cfg.state_dir, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"volume": self.volume, "muted": self.muted}, f)
            os.replace(tmp, path)

    def report_output(self):
        """Screen, sound device, volume and remote profiles to the server (web remote: volume bar, Settings -> Audio)."""
        msg = {"type": "output", "name": "TV Player", "screen": self.screen, "device": self.cfg.audio_device,
               "volume": self.volume, "muted": self.muted, "version": VERSION}
        if self.cfg.can_learn:  # a receiver whose buttons can be learned (Flirc + flirc_util)
            msg["remote"] = dict(self.remotes.summary(), can=True)
        self.send(msg)

    async def remotes_changed(self):
        """A remote action moved on: the TV screen and the web remote follow."""
        await self.draw_menu()
        self.report_output()

    async def remote_command(self, msg):
        if not self.cfg.can_learn:
            return
        err = await self.remotes.command(msg.get("op"), msg.get("name"))
        if err:
            self.remotes.text = err
            self.log(f"remotes: {err}")
            self.report_output()

    async def read_screen(self):
        mpv = self.mpv
        if not mpv:
            return
        w = await mpv.get_property("display-width")
        h = await mpv.get_property("display-height")
        fps = await mpv.get_property("display-fps")
        if w and h:
            self.screen = f"{w}x{h}" + (f" @ {fps:g} Hz" if fps else "")

    async def set_volume(self, percent, muted):
        self.volume, self.muted = max(0, min(100, int(percent))), bool(muted)
        self.save_volume()
        if self.mpv:
            with contextlib.suppress(MpvError):
                await self.mpv.set_property("volume", self.volume)
                await self.mpv.set_property("mute", self.muted)
        self.report_output()

    async def fetch(self, path):
        if self.http is None:
            raise RuntimeError("not connected")
        import aiohttp
        async with self.http.get(self.cfg.server + path, timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            return await r.json()

    async def draw_menu(self):
        """The menu on the screen (osd-overlay 1), or nothing when it is closed."""
        if not self.mpv:
            return
        try:
            if self.remotes.active_screen:
                await self.mpv.command("osd-overlay", 1, "ass-events", self.remotes.render(), remotes_mod.RES_X,
                                       remotes_mod.RES_Y, 10)
            elif self.menu.open:
                await self.mpv.command("osd-overlay", 1, "ass-events", self.menu.render(), menu_mod.RES_X,
                                       menu_mod.RES_Y, 10)
            else:
                await self.mpv.command("osd-overlay", 1, "none", "")
        except MpvError as e:
            log(f"menu draw: {e}")

    # -- outgoing
    def send(self, msg):
        if self._send:
            with contextlib.suppress(Exception):
                self._send(msg)

    def log(self, message):
        log(message)
        self.send({"type": "log", "message": f"mtv: {message}"})

    # -- mpv
    async def attach(self, mpv):
        """A (re)started mpv: replay the current item paused or playing as it was."""
        self.mpv = mpv
        self.af_text = self.applied = None
        if self.sound is not None:
            await self._set_af(sound_chain(self.sound))
            self.applied = self.sound
        if self.item:
            await self._load(self.pos)
        await self.read_screen()
        self.report_output()
        if self.menu.open:
            await self.draw_menu()

    def detach(self):
        self.mpv = None
        self.file_loaded = False
        self.entry = None

    def on_mpv_event(self, ev):
        name = ev.get("event")
        if name == "file-loaded":
            asyncio.ensure_future(self._on_file_loaded(ev.get("playlist_entry_id")))
        elif name == "end-file" and ev.get("reason") == "error":
            if self.item and (ev.get("playlist_entry_id") in (None, self.entry)):
                self.log(f"cannot play {self.item.get('id')}: {ev.get('file_error') or 'error'}")
                self._fail()

    def _fail(self):
        if self.item and self.failed_for != self.loads:
            self.failed_for = self.loads
            self.file_loaded = False
            self.send({"type": "error", "item": self.item.get("id"), "message": "mtv-player could not play it"})

    async def _on_file_loaded(self, entry):
        mpv, it = self.mpv, self.item
        if not mpv or not it or (entry is not None and self.entry is not None and entry != self.entry):
            return  # an older file's event
        tracks = await mpv.get_property("track-list", []) or []
        audio = [t for t in tracks if t.get("type") == "audio"]
        aid = int(it.get("audio_ord") or 0) + 1 if len(audio) > 1 else 1
        if aid > len(audio):
            aid = 1
        cur = next((t.get("id") for t in audio if t.get("selected")), None)
        if audio and cur != aid:
            with contextlib.suppress(MpvError):
                await mpv.set_property("aid", aid)
        if self.item is not it or self.mpv is not mpv:
            return
        if mpv.props.get("pause") is not None and mpv.props.get("pause") == self.want_play:  # belt and braces
            with contextlib.suppress(MpvError):
                await mpv.set_property("pause", not self.want_play)
        self.file_loaded = True
        video = next((t for t in tracks if t.get("type") == "video" and not t.get("albumart")), None)
        what = f"{video.get('codec')} {video.get('demux-w')}x{video.get('demux-h')}" if video else "audio only"
        self.log(f"opened {it.get('id')} ({what}, {'playing' if self.want_play else 'paused'}), audio track "
                 f"{aid}/{len(audio)}, sound {gain_text(self.sound)}")
        self.report(force=True)

    async def _set_af(self, chain):
        await self.mpv.set_property("af", chain)
        self.af_text = chain

    async def apply_sound(self):
        """Live on a playing file (af-command on the labelled filters: no gap); else the 'af' property, which the
        next file builds its chain from."""
        mpv = self.mpv
        if not mpv or self.sound is None:
            return
        live = self.file_loaded
        cmds = sound_commands(self.applied, self.sound) if live else None
        try:
            if cmds is None:
                chain = sound_chain(self.sound)
                if chain != self.af_text or live:
                    await self._set_af(chain)
            else:
                for c in cmds:
                    await mpv.command("af-command", *c)
            self.applied = self.sound
            self.log(f"sound {gain_text(self.sound)} ({'live' if cmds is not None else 'chain set'})")
        except MpvError as e:
            self.log(f"sound: {e}, rebuilding the filter chain")
            with contextlib.suppress(MpvError):
                await self._set_af(sound_chain(self.sound))
                self.applied = self.sound

    async def _load(self, position):
        mpv, it = self.mpv, self.item
        self.file_loaded = False
        self.entry = None
        if not mpv or not it:
            return
        try:
            if self.sound is not None and sound_chain(self.sound) != self.af_text:
                await self._set_af(sound_chain(self.sound))
                self.applied = self.sound
            # start / pause as per-file options of this load (mpv 0.38+: flags, index, options): nothing global to race
            opts = f"start={max(0.0, position):.3f},pause={'no' if self.want_play else 'yes'}"
            reply = await mpv.command("loadfile", self.cfg.media_url(it["id"]), "replace", -1, opts)
        except MpvError as e:
            self.log(f"load {it.get('id')} failed: {e}")
            self._fail()
            return
        self.entry = reply.get("playlist_entry_id") if isinstance(reply, dict) else None
        mpv.props["eof-reached"] = False

    # -- server messages
    async def handle(self, msg):
        t = msg.get("type")
        if t == "load":
            self.item = msg.get("item")
            self.pos = float(msg.get("position") or 0)
            self.want_play = bool(msg.get("autoplay")) and bool(self.item)
            self.loads += 1
            if self.item:
                await self._load(self.pos)
            elif self.mpv:
                with contextlib.suppress(MpvError):
                    await self.mpv.command("stop")
            self.report(force=True)
        elif t == "control":
            await self.on_control(msg)
        elif t == "sound":
            self.sound = msg
            await self.apply_sound()
        elif t == "remote":
            await self.remote_command(msg)
        elif t == "volume":
            try:
                await self.set_volume(float(msg.get("percent")), msg.get("muted"))
            except (TypeError, ValueError):
                pass
        elif t == "state" and isinstance(msg.get("state"), dict):
            self.state = msg["state"]
            if self.menu.open and len(self.menu.pages) == 1:
                self.menu.refresh_root()
                await self.draw_menu()

    async def on_control(self, msg):
        action, mpv = msg.get("action"), self.mpv
        if action == "toggle":
            action = "pause" if self.want_play else "play"
        try:
            if action == "play" and self.item:
                self.want_play = True
                if mpv and mpv.props.get("eof-reached"):  # Play on an ended item: from the start
                    self.ended_for = None
                    await mpv.command("seek", 0, "absolute")
                if mpv:
                    await mpv.set_property("pause", False)
            elif action == "pause":
                self.want_play = False
                if mpv:
                    await mpv.set_property("pause", True)
            elif action == "stop":
                self.want_play = False
                self.item = None
                if mpv:
                    await mpv.command("stop")
            elif action == "seek" and self.item:
                self.pos = max(0.0, float(msg.get("position") or 0))
                self.ended_for = None
                if mpv and self.file_loaded:
                    await mpv.command("seek", self.pos, "absolute")
                elif mpv:
                    await self._load(self.pos)
        except MpvError as e:
            self.log(f"{action}: {e}")
        self.report(force=True)

    # -- periodic
    async def tick(self):
        mpv, it = self.mpv, self.item
        if not it:
            return
        if mpv and self.file_loaded:
            p = mpv.props.get("time-pos")
            if p is None:
                p = await mpv.get_property("time-pos")
            if isinstance(p, (int, float)):
                self.pos = max(0.0, float(p))
            if mpv.props.get("eof-reached") and self.ended_for != self.loads:
                self.ended_for = self.loads
                self.want_play = False
                self.send({"type": "ended", "item": it.get("id")})
        self.report()

    def playing(self):
        mpv = self.mpv
        return bool(self.item and self.want_play and mpv and self.file_loaded and mpv.props.get("pause") is False
                    and not mpv.props.get("eof-reached"))

    def report(self, force=False):
        it = self.item
        if not it:
            return
        mpv = self.mpv
        dur = (mpv.props.get("duration") if mpv and self.file_loaded else None) or it.get("duration") or 0
        playing = self.playing()
        key = (it.get("id"), playing, self.loads)
        now = self.mono()
        if not force and key == self.status_key and now - self.last_status < STATUS_EVERY:
            return
        self.status_key, self.last_status = key, now
        self.send({"type": "status", "item": it.get("id"), "playing": playing, "position": round(self.pos, 3),
                   "duration": dur})


SEEK_KEYS = {"right": 10.0, "left": -10.0, "up": 60.0, "down": -60.0}


def clock(sec):
    sec = max(0, int(sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


async def osd(player, text, ms=1500):
    if player.mpv:
        with contextlib.suppress(MpvError):
            await player.mpv.command("show-text", text, ms)


async def on_key(player, name, held):
    """A remote key: the menu's while it is open; else a server command (the server owns the queue and sends the
    control back to this player)."""
    if player.remotes.active_screen:   # learning a remote: the Flirc takes the presses; other keys do not count
        if await player.remotes.key(name):
            await player.draw_menu()
        return
    if player.menu.open:
        if await player.menu.key(name):
            await player.draw_menu()
        if not held:
            player.log(f"menu key {name}")
        return
    if name == "blue":
        await player.menu.show()
        await player.draw_menu()
        player.log("menu opened")
        return
    if not player._send:
        await osd(player, "Not connected to the server")
        return
    it = player.item
    if name == "ok":
        if not it:
            await osd(player, "Nothing to play")
            return
        playing = player.want_play
        player.send({"type": "cmd", "cmd": "toggle"})
        await osd(player, "Pause" if playing else "Play")
    elif name in ("ch_up", "ch_down"):
        player.send({"type": "cmd", "cmd": "next" if name == "ch_up" else "prev"})
        await osd(player, "Next" if name == "ch_up" else "Previous")
    elif name in SEEK_KEYS:
        if not it:
            return
        dur = (player.mpv.props.get("duration") if player.mpv and player.file_loaded else None) or it.get("duration") or 0
        target = max(0.0, player.pos + SEEK_KEYS[name])
        if dur:
            target = min(target, max(0.0, dur - 2))
        player.pos = target  # held keys add up before the server's seek comes back
        player.send({"type": "cmd", "cmd": "seek", "position": round(target, 3)})
        step = SEEK_KEYS[name]
        await osd(player, f"{'+' if step > 0 else '-'}{abs(step):.0f} s   {clock(target)} / {clock(dur)}")
    else:
        log(f"key {name}: not assigned")
        return
    if not held:
        player.log(f"key {name}")


async def run_ir(cfg, player, stop):
    """Reads the remote from a raw LIRC receiver while the program runs."""
    if not cfg.ir_device:
        return
    fd = ir.open_device(cfg.ir_device)
    if fd is None:
        log(f"remote: cannot open {cfg.ir_device}, no remote")
        return
    loop = asyncio.get_running_loop()
    mode2 = ir.Mode2()
    keys = ir.Keys(lambda name, held: asyncio.ensure_future(on_key(player, name, held)),
                   address=cfg.ir_address, keys=cfg.ir_keys, later=loop.call_later,
                   on_unknown=lambda a, c: log(f"remote: unknown key address 0x{a:x} command 0x{c:02x}"))

    def readable():
        try:
            data = os.read(fd, 4096)
        except BlockingIOError:
            return
        except OSError as e:
            log(f"remote: {e}")
            loop.remove_reader(fd)
            return
        for frame in mode2.feed(data):
            if cfg.ir_debug:
                r = ir.decode_nec(frame)
                what = "noise" if r is None else ("repeat" if r[0] == "repeat" else f"0x{r[1]:x}/0x{r[2]:02x}")
                log(f"ir: {time.monotonic():.3f} {what} ({len(frame)} edges)")
            keys.frame(frame)

    loop.add_reader(fd, readable)
    log(f"remote: reading {cfg.ir_device} (NEC address 0x{cfg.ir_address:x})")
    try:
        await stop.wait()
    finally:
        loop.remove_reader(fd)
        os.close(fd)


async def run_evdev(cfg, player, stop):
    """A keyboard-style receiver (Flirc): found by path / glob, grabbed, plugged in or out at any time."""
    if not cfg.keys_device:
        return
    loop = asyncio.get_running_loop()
    keys = ir.EvdevKeys(lambda name, held: asyncio.ensure_future(on_key(player, name, held)))
    announced = False
    while not stop.is_set():
        found = ir.open_evdev(cfg.keys_device)
        if not found:
            if not announced:
                log(f"remote: no device matches {cfg.keys_device} (waiting)")
                announced = True
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), 5)
            continue
        fd, path = found
        announced = False
        gone = asyncio.Event()

        def readable():
            try:
                data = os.read(fd, EVENT_BATCH)
            except BlockingIOError:
                return
            except OSError:  # unplugged
                loop.remove_reader(fd)
                gone.set()
                return
            if cfg.ir_debug:
                for _s, _us, etype, code, value in ir.EVENT.iter_unpack(data[:len(data) - len(data) % ir.EVENT.size]):
                    if etype == ir.EV_KEY:
                        log(f"key event: {time.monotonic():.3f} code {code} value {value}")
            keys.feed(data)

        loop.add_reader(fd, readable)
        log(f"remote: reading {path} (keyboard receiver)")
        try:
            await asyncio.wait([asyncio.ensure_future(gone.wait()), asyncio.ensure_future(stop.wait())],
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            with contextlib.suppress(Exception):
                loop.remove_reader(fd)
            os.close(fd)
        if not stop.is_set():
            log("remote: keyboard receiver unplugged")


EVENT_BATCH = ir.EVENT.size * 64


class MpvObserver(MpvIpc):
    OBSERVE = MpvIpc.OBSERVE + ("time-pos",)


async def run_mpv(cfg, player, stop):
    """Runs mpv, attaches it to the player, restarts it when it dies (backoff 1 ... 30 s)."""
    backoff = 1.0
    sock_dir = tempfile.mkdtemp(prefix="mtv-")
    sock = os.path.join(sock_dir, "mpv.sock")
    try:
        await _mpv_loop(cfg, player, stop, sock)
    finally:  # also on cancel (SIGTERM)
        shutil.rmtree(sock_dir, ignore_errors=True)


async def _mpv_loop(cfg, player, stop, sock):
    backoff = 1.0
    while not stop.is_set():
        with contextlib.suppress(FileNotFoundError):
            os.unlink(sock)
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(*mpv_args(cfg, sock, player.volume, player.muted))
        ipc = MpvObserver(on_event=player.on_mpv_event)
        try:
            for _ in range(100):
                if os.path.exists(sock) or proc.returncode is not None:
                    break
                await asyncio.sleep(0.1)
            await ipc.connect(sock)
            for _ in range(30):  # the IPC socket can be up before the window is
                if await ipc.get_property("current-vo") or proc.returncode is not None:
                    break
                await asyncio.sleep(0.1)
            if not await ipc.get_property("current-vo"):  # no screen (connector / mode / DRM busy): sound only
                raise MpvError("no picture output (check the connector, the mode and that nothing else holds DRM)")
            log(f"mpv {proc.pid} up on {await ipc.get_property('current-vo')}")
            await player.attach(ipc)
            done, _ = await asyncio.wait([asyncio.ensure_future(proc.wait()), asyncio.ensure_future(stop.wait())],
                                         return_when=asyncio.FIRST_COMPLETED)
        except (OSError, MpvError) as e:
            log(f"mpv: {e!r}")
        finally:
            player.detach()
            await ipc.close()
            if proc.returncode is None:
                proc.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), 5)
                if proc.returncode is None:
                    proc.kill()
        if stop.is_set():
            break
        if time.monotonic() - started > 60:
            backoff = 1.0
        log(f"mpv exited ({proc.returncode}), restarting in {backoff:.0f} s")
        await asyncio.sleep(backoff)
        backoff = min(RECONNECT_MAX, backoff * 2)


async def run_server(cfg, player, stop):
    import aiohttp
    backoff = 1.0
    async with aiohttp.ClientSession() as http:
        while not stop.is_set():
            try:
                async with http.ws_connect(cfg.ws_url, heartbeat=30) as ws:
                    log(f"connected to {cfg.server}")
                    backoff = 1.0
                    queue = asyncio.Queue()
                    player._send = queue.put_nowait
                    player.http = http
                    player.log(f"mtv-player {VERSION} up (mode {cfg.mode}, interpolation "
                               f"{'on' if cfg.interpolation else 'off'}, audio {cfg.audio_device}, volume {player.volume} %)")
                    player.report_output()

                    async def writer():
                        while True:
                            await ws.send_json(await queue.get())

                    wtask = asyncio.create_task(writer())
                    try:
                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                continue
                            with contextlib.suppress(ValueError):
                                await player.handle(json.loads(msg.data))
                    finally:
                        wtask.cancel()
                        player._send = None
                        player.http = None
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
                log(f"server: {e!r}")
            if stop.is_set():
                break
            log(f"disconnected, retrying in {backoff:.0f} s")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), backoff)
            backoff = min(RECONNECT_MAX, backoff * 2)


async def run_ticks(player, stop):
    while not stop.is_set():
        with contextlib.suppress(MpvError):
            await player.tick()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), TICK)


async def main():
    cfg = Config()
    if not cfg.server:
        log("MTV_SERVER is not set")
        return 2
    player = Player(cfg)
    if cfg.can_learn:
        await player.remotes.recover()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    tasks = [asyncio.create_task(c) for c in (run_mpv(cfg, player, stop), run_server(cfg, player, stop),
                                              run_ticks(player, stop), run_ir(cfg, player, stop),
                                              run_evdev(cfg, player, stop))]
    await stop.wait()  # SIGTERM / SIGINT: the server loop sits in a ws read, so cancel instead of waiting for it
    await player.remotes.shutdown()  # a learn in progress is stopped and undone first
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)  # run_mpv's finally terminates mpv
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
