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
  MTV_SAVER_AFTER    s paused / stopped / idle before the screensaver (black, the name moving around; default 300)
  MTV_SAVER_MOVE     s between two jumps of the screensaver text (default 180)
  MTV_LIP_SYNC       ms the headphones play earlier than the picture (default -100, the Apple TV's default)
  MTV_FONT_DIR       directory of the Inter font files for text measuring (default /usr/share/fonts/opentype/inter, Debian's
                     fonts-inter); mpv draws with the Inter family through fontconfig. Pillow (python3-pil) reads the cover
                     colours and measures text; without it the colours come from the item id and widths are estimated

Sound: HDMI (default) or the atv-audio headphones, chosen on the web remote (Settings > Audio): mpv is muted, the
server's atv-audio sink plays the same file and follows this player's clock (sinklink.py).

Remote keys (ir.py names), menu closed: OK = play / pause, Stop = stop (the screen goes black, "MTV Player stopped"; Play starts
the item again), Blue = open the menu, Up / Down = previous / next video, Right / Left = +10 / -10 s (Left/Right repeat while
held), CH+ / CH- = volume up / down (5 %; mpv's volume on HDMI, the headphones' own when the sound goes there); each shows a
short text. Back with no menu open shows the info popup (cover, title, progress, next; info.py) for 5 s, Back again hides it; it also
shows for 5 s whenever a different video starts (not over the lyrics screen, the menu or the screensaver).
Menu open (menu.py): Up / Down move, CH+ / CH- page, OK / Right select, Left and Back go back one page (Back closes on the
first page), Blue / Red close. A held key counts as one press (arrows repeat while held in the menu). The menu's first page:
Lyrics screen, Subtitles ›, Audio output (HDMI / Headphones: the same switch as the web remote; Headphones that are not
ready are asked for during one minute, then never again by themselves), Volume, Boost, Loudness (the server's sound
settings of this session), Equalizer (a short preset picker: Off, Momentum 4, Bass boost, Vocal, Bright, V-shape, Custom),
Shuffle, Repeat, then a divider and the lists: Queue, Playlists, For you (its last row: Music videos). No Play / Pause
or Stop row (the remote has those buttons). While the screensaver is on, any key only closes it.

Lyrics: the video's own lyrics (subtitle) track in the chosen language is shown on the picture (Auto = the file's default
track); a second language can be shown at the top at the same time, and font / size / box / bold are chosen in the menu's
Subtitles page (subs.py; kept in lyrics.json with the language). Both lines are hidden while the info popup is up. The lyrics screen (lyricsview.py) replaces the picture with the Apple TV's lyrics view: time-synced lyrics only,
scrolling by themselves, on a backdrop in the cover's colours that the GPU draws as a shader; the video keeps playing under it.
Language and screen are kept in MTV_STATE/lyrics.json.

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
import info as info_mod  # noqa: E402
import ir  # noqa: E402
import lyricsview  # noqa: E402
import menu as menu_mod  # noqa: E402
import palette as palette_mod  # noqa: E402
import remotes as remotes_mod  # noqa: E402
import saver as saver_mod  # noqa: E402
import sinklink  # noqa: E402
import subs as subs_mod  # noqa: E402

VERSION = "0.16.0"
STATUS_EVERY = 1.0       # s between status messages while something is loaded
VOLUME_STEP = 5          # % per press of a volume key / Left / Right on the menu's Volume row
HP_HOLD = 2.0            # s the headphones' volume just sent is shown as it is (the sink's report comes a moment later)
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

        def num(name, default, lo, hi):
            try:
                return max(lo, min(hi, float(env.get(name) or default)))
            except ValueError:
                return default
        self.saver_after = num("MTV_SAVER_AFTER", saver_mod.AFTER, 5.0, 86400.0)
        self.saver_move = num("MTV_SAVER_MOVE", saver_mod.MOVE, 5.0, 86400.0)
        self.lip_sync = int(num("MTV_LIP_SYNC", sinklink.LIP_DEFAULT, -200, 400))

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
            "--sub-font=Inter", "--sub-bold=yes", "--sub-font-size=40", "--sub-outline-size=2", "--sub-shadow-offset=1",
            "--sub-margin-y=44", "--osd-font=Inter", f"--secondary-sub-pos={subs_mod.SECOND_POS}", "--secondary-sid=no",
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
        self.menu = menu_mod.Menu(self.fetch, self.send, lambda: self.state, volume=self, mono=mono)
        self.last_key_at = -1e9      # monotonic time of the last key event from the keyboard receiver (Flirc)
        self.remotes = remotes_mod.Remotes(cfg.flirc_util, cfg.state_dir or tempfile.gettempdir(), self.remotes_changed,
                                           self.log, quiet_for=lambda: self.mono() - self.last_key_at)
        self.stopped = False         # playback was stopped (Stop): the screensaver says so; cleared by the next load
        self.hp_set = None           # (percent, muted, time) of the headphones' volume just sent
        self.link = sinklink.SinkLink(self)   # the sound on the atv-audio headphones (chosen on the web remote)
        self.link.lip_ms = cfg.lip_sync
        self.saver = saver_mod.Saver(mono, after=cfg.saver_after, move=cfg.saver_move)
        # lyrics: the language (subtitles on the video and the lyrics screen) and the lyrics screen itself, kept in
        # lyrics.json; the screen's model, the cover colours per item and the shader file mpv draws the backdrop from
        self.lyrics_lang, self.lyrics_on = "auto", False
        self.sub_style = dict(subs_mod.DEFAULT)   # second language (top line) + look of the subtitles (subs.py)
        self.sub_vis = None          # subtitle visibility last set in mpv (None: not set in this mpv yet)
        self.load_lyrics_settings()
        self.view = lyricsview.LyricsView()
        self.view.set_lang(self.lyrics_lang)
        self.palettes = {}           # item id -> Palette (small cache)
        self.has_picture = True      # the loaded file has a video track or a cover (the shader has something to draw on)
        self.shader_path = None      # backdrop shader in mpv now (None: none)
        self.shader_text = None
        self.shader_dir = None
        self.shader_n = 0
        self.display = None          # (width, height) of the screen, read from mpv
        self.lyrics_drawn = None     # signature of the lyrics screen on the TV now (None: draw it again)
        self.lyrics_layer = False    # its overlays (4, 5) are on the screen
        self.aspect_set = False      # video-aspect-override set for the lyrics screen
        self.fetching = set()        # ("lyrics" | "art", id) in flight
        # info popup (info.py): shown for 5 s on a different video and on Back; osd-overlay 6 + the cover bitmap overlay 1
        self.info = info_mod.Info(mono)
        self.arts = {}               # item id -> cover bytes (small cache; the popup's bitmap is made from it at the screen's scale)
        self.thumbs = {}             # item id -> (scale, bitmap or None, w, h, x, y) the bitmap made for that scale
        self.info_drawn = None       # signature of the popup on the screen now (None: nothing drawn)
        self.info_thumb = None       # (item id, scale) of the cover bitmap on the screen
        self.info_item = None        # id of the video the popup last announced (a different one shows it again)

    @property
    def connected(self):
        """The server socket is open."""
        return self._send is not None

    @property
    def mpv_muted(self):
        """mpv's own sound is off when the user muted it or the headphones play the sound."""
        return bool(self.muted or self.link.on)

    async def apply_mute(self):
        if self.mpv:
            with contextlib.suppress(MpvError):
                await self.mpv.set_property("mute", self.mpv_muted)

    async def local_pause(self, why):
        """Pause here and tell the server (its play state follows; the sink pauses with it)."""
        self.want_play = False
        if self.mpv:
            with contextlib.suppress(MpvError):
                await self.mpv.set_property("pause", True)
        self.send({"type": "cmd", "cmd": "pause"})
        self.report(force=True)

    async def draw_notice(self):
        """The notice that stays until Play (osd-overlay 2), or nothing."""
        mpv = self.mpv
        if not mpv:
            return
        try:
            if self.link.notice:
                await mpv.command("osd-overlay", 2, "ass-events", notice_events(self.link.notice), 1920, 1080, 30)
            else:
                await mpv.command("osd-overlay", 2, "none", "")
        except MpvError as e:
            log(f"notice draw: {e}")

    # -- screensaver
    def busy(self):
        """Something is going on: playing (or starting), a menu or a learn screen open."""
        return bool(self.menu.open or self.remotes.active_screen or (self.item and self.want_play))

    async def saver_tick(self):
        what = self.saver.tick(self.busy(), self.stopped)
        if what is None:
            return
        if what != "move":
            log(f"screensaver {'on' if what == 'show' else 'off (activity)'}")
        await self.draw_saver(what != "hide")

    async def draw_saver(self, on):
        mpv = self.mpv
        if not mpv:
            return
        try:
            if on:
                await mpv.command("osd-overlay", 3, "ass-events", self.saver.render(), saver_mod.RES_X, saver_mod.RES_Y, 40)
            else:
                await mpv.command("osd-overlay", 3, "none", "")
        except MpvError as e:
            log(f"screensaver draw: {e}")

    async def wake(self):
        """A key: closes the screensaver and does nothing else. True when it was on."""
        if self.saver.wake():
            log("screensaver off (key)")
            await self.draw_saver(False)
            return True
        return False

    # -- volume keys / the menu's Volume row: the headphones' own volume while the sound goes there, else mpv's volume
    def volume_now(self):
        """(label, percent or None, muted): the volume the keys and the menu act on."""
        if not self.link.on:
            return "Volume", self.volume, self.muted
        out = (self.link.sink or {}).get("output") or {}
        pct, muted = out.get("volume"), bool(out.get("muted"))
        if self.hp_set and self.mono() - self.hp_set[2] < HP_HOLD:
            pct, muted = self.hp_set[0], self.hp_set[1]
        if not self.link.usable:
            return "Headphones volume", None, False
        return "Headphones volume", pct, muted

    async def set_level(self, percent, muted):
        """Set that volume (HDMI: mpv, kept in the state file; headphones: the sink's output volume, through the server)."""
        percent = max(0, min(100, int(percent)))
        if self.link.on:
            self.hp_set = (percent, bool(muted), self.mono())
            self.send({"type": "sink_volume", "percent": percent, "muted": bool(muted)})
        else:
            await self.set_volume(percent, muted)

    async def volume_step(self, delta):
        """A step up / down (unmutes). Returns the text for the screen."""
        label, pct, _muted = self.volume_now()
        if pct is None:
            return "Headphones not connected"
        await self.set_level(pct + delta, False)
        return f"{label} {max(0, min(100, pct + delta))} %"

    async def volume_mute(self):
        label, pct, muted = self.volume_now()
        if pct is None:
            return "Headphones not connected"
        await self.set_level(pct, not muted)
        return f"{label} {'muted' if not muted else f'{pct} %'}"

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

    # -- lyrics: the language (subtitles on the video + lyrics screen) and the lyrics screen, kept in lyrics.json
    def _lyrics_file(self):
        return os.path.join(self.cfg.state_dir, "lyrics.json") if self.cfg.state_dir else None

    def load_lyrics_settings(self):
        path = self._lyrics_file()
        if not path:
            return
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get("lang") in dict(lyricsview.LANGS):
                self.lyrics_lang = data["lang"]
            self.lyrics_on = bool(data.get("screen"))
            self.sub_style = subs_mod.clean(data.get("style"))
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def save_lyrics_settings(self):
        path = self._lyrics_file()
        if not path:
            return
        with contextlib.suppress(OSError):
            os.makedirs(self.cfg.state_dir, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"lang": self.lyrics_lang, "screen": self.lyrics_on, "style": self.sub_style}, f)
            os.replace(tmp, path)

    async def set_lyrics_lang(self, lang):
        """Menu: Lyrics Off / Auto / English / Croatian (the subtitles on the video and the lyrics screen follow)."""
        if lang not in dict(lyricsview.LANGS) or lang == self.lyrics_lang:
            return
        self.lyrics_lang = lang
        self.save_lyrics_settings()
        self.view.set_lang(lang)
        self.log(f"lyrics language {lang}")
        await self.apply_subs()
        self.lyrics_drawn = None
        await self.draw_lyrics()

    async def set_sub_style(self, key, value):
        """Menu Subtitles page: second language, font, size, box, bold (kept in lyrics.json, applied at once)."""
        new = subs_mod.clean(dict(self.sub_style, **{key: value}))
        if new == self.sub_style:
            return
        self.sub_style = new
        self.save_lyrics_settings()
        self.log(f"subtitles {key} {new.get(key)}")
        if key == "second":
            await self.apply_subs()
        else:
            await self.apply_sub_style()

    async def apply_sub_style(self):
        if not self.mpv:
            return
        for name, value in subs_mod.props(self.sub_style):
            try:
                await self.mpv.set_property(name, value)
            except MpvError as e:
                log(f"subtitle style {name}: {e}")

    async def apply_sub_visibility(self):
        """Both subtitle lines are hidden under the lyrics screen and while the info popup is up (owner: the card is
        partly transparent like the menu, the lines would show through); set in mpv only when it changes."""
        vis = not self.lyrics_on and not self.info.visible
        if not self.mpv or vis == self.sub_vis:
            return
        try:
            await self.mpv.set_property("sub-visibility", vis)
            await self.mpv.set_property("secondary-sub-visibility", vis)
            self.sub_vis = vis
        except MpvError as e:
            log(f"subtitle visibility: {e}")

    async def set_lyrics_on(self, on):
        """Menu: the lyrics screen on / off (the video keeps playing underneath)."""
        on = bool(on)
        if on == self.lyrics_on:
            return
        self.lyrics_on = on
        self.save_lyrics_settings()
        self.log(f"lyrics screen {'on' if on else 'off'}")
        self.want_data(self.item)
        await self.apply_lyrics_screen()

    def current_palette(self):
        it = self.item or (self.state or {}).get("current") or {}
        return self.palettes.get(it.get("id")) or palette_mod.for_item(it.get("id"))

    # -- the menu's Audio output row (same switch as the web remote's Settings > Audio > TV Player > Sound goes to)
    def audio_out(self):
        return self.link.out_status()

    async def set_audio_out(self, mode):
        await self.link.set_on(mode == "headphones")

    async def refresh_menu(self):
        """Something the open settings page shows changed outside a key press (headphones connect window ended)."""
        if self.menu.open and self.menu.page is not None and self.menu.page.build is not None:
            self.menu.refresh_root()
            await self.draw_menu()

    def accent(self):
        """The cover's accent colour (menu, lyrics screen)."""
        return self.current_palette().vibrant

    def backdrop_base(self):
        return self.current_palette().base

    async def apply_subs(self, tracks=None):
        """The video's lyrics (subtitle) track in the chosen language, hidden while the lyrics screen is on."""
        mpv, it = self.mpv, self.item
        if not mpv or not it or not self.file_loaded:
            return
        try:
            if tracks is None:
                tracks = await mpv.get_property("track-list", []) or []
            count = sum(1 for t in tracks if t.get("type") == "sub")
            sid = lyricsview.sub_id(it, self.lyrics_lang)
            if sid is not None and sid > count:
                sid = None
            await mpv.set_property("sid", sid if sid is not None else "no")
            second = subs_mod.exact_sub_id(it, self.sub_style["second"]) if self.sub_style["second"] != "off" else None
            if second is not None and (second > count or second == sid):
                second = None            # not in the file, or the same track as the bottom line
            await mpv.set_property("secondary-sid", second if second is not None else "no")
        except MpvError as e:
            log(f"subtitles: {e}")
        await self.apply_sub_visibility()

    def want_data(self, item):
        """Cover colours for every item (menu accent, lyrics backdrop); the lyrics while the lyrics screen is on."""
        if not item:
            return
        iid = item.get("id")
        if iid not in self.palettes or (item.get("art") and iid not in self.arts):
            if not item.get("art"):
                self.palettes[iid] = palette_mod.for_item(iid)
            elif self.http is not None and ("art", iid) not in self.fetching:
                self.fetching.add(("art", iid))
                asyncio.ensure_future(self._fetch_art(iid))
        v = self.view
        if self.lyrics_on and v.item is not None and v.item.get("id") == iid and v.tracks is None:
            if not (item.get("lyr") or 0):
                v.set_lyrics(iid, [])
            elif self.http is not None and ("lyrics", iid) not in self.fetching:
                self.fetching.add(("lyrics", iid))
                asyncio.ensure_future(self._fetch_lyrics(iid))

    async def _fetch_art(self, iid):
        pal, data = None, None
        try:
            data = await self.fetch_bytes(f"/art/{iid}")
            pal = await asyncio.get_running_loop().run_in_executor(None, palette_mod.extract, data)
        except Exception as e:  # no art / server away: the art-free colours
            log(f"cover colours {iid}: {e!r}")
        finally:
            self.fetching.discard(("art", iid))
        self.palettes[iid] = pal or palette_mod.for_item(iid)
        while len(self.palettes) > 40:
            self.palettes.pop(next(iter(self.palettes)))
        if data is not None:   # the info popup's cover (its bitmap is made when it is drawn, at mpv's OSD scale)
            self.arts[iid] = data
            while len(self.arts) > 8:
                old = next(iter(self.arts))
                self.arts.pop(old)
                self.thumbs.pop(old, None)
            await self.draw_info()
        if (self.item or {}).get("id") == iid:
            if self.view.item is not None and self.view.item.get("id") == iid:
                self.view.palette = self.palettes[iid]
            if self.lyrics_on:
                await self.apply_lyrics_screen()
            if self.menu.open:
                await self.draw_menu()

    async def _fetch_lyrics(self, iid):
        tracks = []
        try:
            data = await self.fetch(f"/api/lyrics/{iid}")
            tracks = data.get("tracks") or []
        except Exception as e:
            log(f"lyrics {iid}: {e!r}")
        finally:
            self.fetching.discard(("lyrics", iid))
        if self.view.set_lyrics(iid, tracks):
            self.lyrics_drawn = None
            await self.draw_lyrics()

    async def _set_shader(self, text):
        """The backdrop shader in mpv (a new file name for new contents, so mpv compiles it), or none."""
        mpv = self.mpv
        if text is None:
            if self.shader_path is not None:
                with contextlib.suppress(MpvError):
                    await mpv.command("change-list", "glsl-shaders", "clr", "")
                with contextlib.suppress(OSError):
                    os.unlink(self.shader_path)
                self.shader_path = self.shader_text = None
            return
        if self.shader_path is not None and self.shader_text == text:
            return
        if not self.shader_dir:
            self.shader_dir = tempfile.mkdtemp(prefix="mtv-shader-")
        self.shader_n += 1
        path = os.path.join(self.shader_dir, f"lyrics-{self.shader_n}.glsl")
        with open(path, "w") as f:
            f.write(text)
        await mpv.command("change-list", "glsl-shaders", "set", path)
        old, self.shader_path, self.shader_text = self.shader_path, path, text
        if old:
            with contextlib.suppress(OSError):
                os.unlink(old)

    async def apply_lyrics_screen(self):
        """Lyrics screen on: the shader backdrop, the video stretched over the whole screen (the shader draws on the video's
        area; the picture is covered anyway), its subtitles hidden, the text layers. Off (or nothing loaded): all undone."""
        mpv = self.mpv
        if not mpv:
            return
        on = self.lyrics_on and self.item is not None
        try:
            if on:
                if self.view.item is None or self.view.item.get("id") != self.item.get("id"):
                    self.view.set_item(self.item)
                self.view.palette = self.palettes.get(self.item.get("id")) or self.view.palette
                await self._set_shader(self.view.shader())
                if not self.aspect_set and self.display:
                    await mpv.set_property("video-aspect-override", f"{self.display[0]}:{self.display[1]}")
                    self.aspect_set = True
            else:
                await self._set_shader(None)
                if self.aspect_set:
                    await mpv.set_property("video-aspect-override", "no")
                    self.aspect_set = False
        except (MpvError, OSError) as e:
            log(f"lyrics screen: {e}")
        await self.apply_sub_visibility()
        self.lyrics_drawn = None
        await self.draw_lyrics()

    async def draw_lyrics(self):
        """The lyrics screen's text layer (osd-overlay 4, under the menu) and, for an item with no picture, a plain
        background (overlay 5); redrawn when it looks different or an animation runs."""
        mpv = self.mpv
        if not mpv:
            return
        if not (self.lyrics_on and self.item is not None):
            if self.lyrics_layer:
                with contextlib.suppress(MpvError):
                    await mpv.command("osd-overlay", 4, "none", "")
                    await mpv.command("osd-overlay", 5, "none", "")
                self.lyrics_layer = False
            self.lyrics_drawn = None
            return
        now = self.mono()
        p = mpv.props.get("time-pos") if self.file_loaded else None
        pos = float(p) if isinstance(p, (int, float)) else self.pos
        self.view.update(pos, now)
        up = (self.state or {}).get("upcoming") or []
        sig = (self.view.signature(pos, self.want_play, up), self.has_picture)
        if sig == self.lyrics_drawn and not self.view.animating(now):
            return
        dur = (mpv.props.get("duration") if self.file_loaded else None) or self.item.get("duration") or 0
        try:
            await mpv.command("osd-overlay", 4, "ass-events", self.view.front(pos, dur, self.want_play, up, now),
                              lyricsview.d.RES_X, lyricsview.d.RES_Y, 5)
            if self.lyrics_drawn is None or self.lyrics_drawn[1] != self.has_picture:
                if self.has_picture:
                    await mpv.command("osd-overlay", 5, "none", "")
                else:
                    await mpv.command("osd-overlay", 5, "ass-events", self.view.solid(), lyricsview.d.RES_X,
                                      lyricsview.d.RES_Y, 4)
        except MpvError as e:
            log(f"lyrics draw: {e}")
        self.lyrics_drawn = sig
        self.lyrics_layer = True

    def report_output(self):
        """Screen, sound device, volume and remote profiles to the server (web remote: volume bar, Settings -> Audio)."""
        msg = {"type": "output", "name": "TV Player", "screen": self.screen, "device": self.cfg.audio_device,
               "volume": self.volume, "muted": self.muted, "version": VERSION,
               "sound": "headphones" if self.link.on else "hdmi"}
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
        err = await self.remotes.command(msg.get("op"), msg.get("name"), msg.get("function"), msg.get("text"))
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
            self.display = (int(w), int(h))

    async def set_volume(self, percent, muted):
        self.volume, self.muted = max(0, min(100, int(percent))), bool(muted)
        self.save_volume()
        if self.mpv:
            with contextlib.suppress(MpvError):
                await self.mpv.set_property("volume", self.volume)
            await self.apply_mute()
        self.report_output()

    async def fetch(self, path):
        if self.http is None:
            raise RuntimeError("not connected")
        import aiohttp
        async with self.http.get(self.cfg.server + path, timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            return await r.json()

    async def fetch_bytes(self, path):
        if self.http is None:
            raise RuntimeError("not connected")
        import aiohttp
        async with self.http.get(self.cfg.server + path, timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            return await r.read()

    # -- info popup
    def info_blocked(self):
        """Nothing to show it for, or something else owns the screen: the lyrics screen has its own title and progress."""
        return bool(not self.item or self.lyrics_on or self.menu.open or self.remotes.active_screen or self.saver.on)

    def info_data(self):
        it = self.item or {}
        mpv = self.mpv
        dur = (mpv.props.get("duration") if mpv and self.file_loaded else None) or it.get("duration") or 0
        st = self.state or {}
        up = st.get("upcoming") or []
        return {"item": it, "pos": self.pos, "dur": dur, "playing": self.want_play, "next": up[0] if up else None,
                "shuffle": bool(st.get("shuffle")), "repeat": st.get("repeat_mode") or "all",
                "accent": self.accent(), "base": self.backdrop_base(), "thumb": False}

    async def info_thumb_for(self, iid):
        """The popup's cover bitmap for this item at mpv's OSD scale ((scale, bitmap, w, h, x, y); bitmap None when there
        is none to make). overlay-add draws in screen pixels: the scale is osd-width / 1920 read from mpv now, not a guess."""
        art = self.arts.get(iid)
        if art is None:
            return None
        ow = None
        with contextlib.suppress(MpvError):
            ow = await self.mpv.get_property("osd-width")
        k = (ow if isinstance(ow, (int, float)) and ow > 0 else (self.display[0] if self.display else info_mod.RES_X)) / info_mod.RES_X
        th = self.thumbs.get(iid)
        if th is not None and abs(th[0] - k) < 1e-6:
            return th
        x, y, w, h = (round(v * k) for v in info_mod.THUMB)
        buf = await asyncio.get_running_loop().run_in_executor(None, info_mod.thumb_bgra, art, w, h, round(info_mod.THUMB_R * k))
        th = self.thumbs[iid] = (k, buf, w, h, x, y)
        return th

    async def draw_info(self):
        """The popup (osd-overlay 6 + the cover as overlay-add 1) while it is up, redrawn only when it looks different
        (once a second while playing); removed when it ends or something else takes the screen. A failed step never
        leaves stale state behind: removing always runs and marks what is on the screen only after mpv took it."""
        mpv = self.mpv
        if not mpv:
            return
        if self.info.visible and self.info_blocked():
            self.info.hide()
        await self.apply_sub_visibility()   # the subtitle lines go while the card is up and come back with it gone
        if not self.info.visible:
            if self.info_drawn is not None or self.info_thumb is not None:
                await self.remove_info()
            return
        try:
            it = self.item or {}
            iid = it.get("id")
            if it.get("art") and iid not in self.arts and ("art", iid) in self.fetching and self.info.age < 1.0:
                return   # the cover is on its way: do not flash the empty tile first
            th = await self.info_thumb_for(iid)
            if th is not None and th[1] is None:
                th = None            # no bitmap could be made (no Pillow, a bad picture): the tile
            data = dict(self.info_data(), thumb=th is not None)
            sig = self.info.signature(data) + ((th[0], th[2]) if th else ())
            if sig == self.info_drawn:
                return
            if th is not None and self.info_thumb != (iid, th[0]):
                try:
                    await self.add_cover(th)
                    self.info_thumb = (iid, th[0])
                except (MpvError, OSError) as e:   # no cover this time: the card with the tile, the next draw retries
                    log(f"info popup cover: {e}")
                    th = None
                    data["thumb"] = False
                    sig = None
            elif th is None and self.info_thumb is not None:
                with contextlib.suppress(MpvError):
                    await mpv.command("overlay-remove", 1)
                self.info_thumb = None
            await mpv.command("osd-overlay", 6, "ass-events", self.info.render(data), info_mod.RES_X, info_mod.RES_Y,
                              info_mod.Z)
            self.info_drawn = sig if sig is not None else ("retry",)
        except MpvError as e:
            log(f"info popup draw: {e}")

    async def add_cover(self, th):
        """The cover bitmap into mpv: a fresh private file every time (nothing outside can have removed it), read by mpv
        into memory before the command returns, then deleted."""
        _k, buf, w, h, x, y = th
        fd, path = tempfile.mkstemp(prefix="mtv-cover-", suffix=".bgra")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(buf)
            await self.mpv.command("overlay-add", 1, x, y, path, 0, "bgra", w, h, w * 4)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)

    async def remove_info(self):
        """Card and cover off the screen; each removal on its own, so one failing never keeps the other."""
        mpv = self.mpv
        for args in (("osd-overlay", 6, "none", ""), ("overlay-remove", 1)):
            try:
                await mpv.command(*args)
            except MpvError as e:
                log(f"info popup remove: {e}")
        self.info_drawn = self.info_thumb = None

    async def info_back(self):
        """Back with no menu: shows the popup for 5 s, or hides it when it is up."""
        if not self.item:
            await osd(self, "Nothing playing")
            return
        if self.lyrics_on:
            self.log("key back: the lyrics screen shows the info already")
            return
        on = self.info.toggle()
        await self.draw_info()
        self.log(f"key back: info {'shown' if on else 'hidden'}")

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
        self.shader_path = self.shader_text = None   # a new mpv: no shader, no aspect override, no overlays
        self.aspect_set = False
        self.lyrics_drawn = None
        self.lyrics_layer = False
        self.info_drawn = self.info_thumb = None   # a new mpv has none of the popup's overlays
        self.sub_vis = None                        # ... and its own subtitle visibility / style
        await self.apply_sub_style()
        if self.sound is not None:
            await self._set_af(sound_chain(self.sound))
            self.applied = self.sound
        if self.item:
            await self.link.begin_item(self.item, self.want_play, from_server=False)
            await self._load(self.pos)
        await self.apply_mute()
        await self.read_screen()
        self.report_output()
        if self.menu.open:
            await self.draw_menu()
        if self.link.notice:
            await self.draw_notice()
        if self.saver.on:
            await self.draw_saver(True)
        if self.lyrics_on:
            await self.apply_lyrics_screen()

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
        if mpv.props.get("pause") is not None and mpv.props.get("pause") == self.want_play \
                and not self.link.waiting:  # belt and braces (not while the start waits for the headphones)
            with contextlib.suppress(MpvError):
                await mpv.set_property("pause", not self.want_play)
        self.file_loaded = True
        self.has_picture = any(t.get("type") == "video" for t in tracks)   # a video or a cover (album art)
        await self.apply_subs(tracks)
        if self.lyrics_on:
            self.lyrics_drawn = None
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
            opts = f"start={max(0.0, position):.3f},pause={'no' if self.want_play and not self.link.waiting else 'yes'}"
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
            self.stopped = False
            if self.saver.on and self.want_play:
                await self.wake()
            if self.item and self.item.get("id") != self.info_item:   # a different video: the info popup for 5 s
                self.info_item = self.item.get("id")
                self.info.show()
            if self.item:
                self.view.set_item(self.item)
                self.view.palette = self.palettes.get(self.item.get("id"))
                self.want_data(self.item)
                await self.link.begin_item(self.item, self.want_play, from_server=True)
                await self._load(self.pos)
                if self.lyrics_on:
                    await self.apply_lyrics_screen()
            else:
                self.link.waiting = False
                if self.mpv:
                    with contextlib.suppress(MpvError):
                        await self.mpv.command("stop")
                await self.apply_lyrics_screen()
            await self.draw_info()
            self.report(force=True)
        elif t == "control":
            await self.on_control(msg)
        elif t == "sound":
            self.sound = msg
            await self.apply_sound()
        elif t == "remote":
            await self.remote_command(msg)
        elif t == "sound_out":      # web remote: HDMI or the atv-audio headphones
            await self.link.set_on(msg.get("mode") == "headphones")
            await self.refresh_menu()
        elif t == "sink_ready":
            await self.link.ready(msg.get("id"))
        elif t == "pong":
            self.link.pong(msg)
        elif t == "volume":
            try:
                await self.set_volume(float(msg.get("percent")), msg.get("muted"))
            except (TypeError, ValueError):
                pass
        elif t == "state" and isinstance(msg.get("state"), dict):
            self.state = msg["state"]
            old, self.link.sink = self.link.sink, self.state.get("sink")
            await self.link.state_changed(old, self.link.sink)
            if self.menu.open and len(self.menu.pages) == 1:
                self.menu.refresh_root()
                await self.draw_menu()

    async def on_control(self, msg):
        action, mpv = msg.get("action"), self.mpv
        if action == "toggle":
            action = "pause" if self.want_play else "play"
        try:
            if action == "play" and self.item:
                if await self.link.hold_play():   # the headphones are missing: the notice instead of starting
                    return
                await self.link.acknowledge("play")
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
                self.stopped = True
                self.want_play = False
                self.item = None
                self.pos = 0.0
                self.file_loaded = False
                self.link.waiting = False
                await self.link.acknowledge("stop")
                if mpv:
                    await mpv.command("stop")
                await self.apply_lyrics_screen()   # nothing loaded: the lyrics screen goes away (the stopped screen shows)
                await self.draw_info()             # ... and so does the info popup
            elif action == "seek" and self.item:
                self.pos = max(0.0, float(msg.get("position") or 0))
                self.ended_for = None
                self.link.note_seek()
                if mpv and self.file_loaded:
                    await mpv.command("seek", self.pos, "absolute")
                elif mpv:
                    await self._load(self.pos)
        except MpvError as e:
            self.log(f"{action}: {e}")
        self.report(force=True)

    # -- periodic
    async def tick(self):
        await self.link.tick()
        await self.saver_tick()
        await self.draw_info()
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


SEEK_KEYS = {"right": 10.0, "left": -10.0}


def clock(sec):
    sec = max(0, int(sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def notice_events(text):
    """ASS events of the notice (bottom of the screen): a dark box and the text, wrapped at ~60 characters."""
    import textwrap
    lines = textwrap.wrap(str(text), 60)[:3] or [""]
    h = 40 + 56 * len(lines)
    y0 = 960 - h
    ev = [f"{{\\an7\\pos(0,0)\\bord0\\shad0\\1c&H101010&\\1a&H10&\\p1}}m 300 {y0} l 1620 {y0} 1620 960 300 960{{\\p0}}"]
    for n, line in enumerate(lines):
        ev.append(f"{{\\an5\\pos(960,{y0 + 20 + 28 + 56 * n})\\fs40\\bord0\\shad0\\1c&H80C0FF&}}{menu_mod.esc(line)}")
    return "\n".join(ev)


async def osd(player, text, ms=1500):
    if player.mpv:
        with contextlib.suppress(MpvError):
            await player.mpv.command("show-text", text, ms)


async def on_key(player, name, held):
    """A remote key: the menu's while it is open; else a server command (the server owns the queue and sends the
    control back to this player). While the screensaver is on a key only closes it."""
    if await player.wake():
        player.log(f"key {name}: woke the screensaver")
        return
    if held and player.saver.recently_woke():   # the rest of the held key that woke it
        return
    if player.remotes.active_screen:   # learning a remote: the Flirc takes the presses; other keys do not count
        if await player.remotes.key(name):
            await player.draw_menu()
        return
    if name == "stop":                 # Stop works with the menu open too (and closes it)
        if not player._send:
            await osd(player, "Not connected to the server")
            return
        if player.menu.open:
            player.menu.close()
            await player.draw_menu()
        if player.item:
            player.send({"type": "cmd", "cmd": "stop"})
            player.log("key stop")
        else:
            await osd(player, "Nothing to stop")
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
        await player.draw_info()      # the menu owns the screen: the popup goes
        player.log("menu opened")
        return
    if name == "back":                # no menu: the info popup (shows 5 s, or hides when it is up)
        await player.info_back()
        return
    if not player._send:
        await osd(player, "Not connected to the server")
        return
    it = player.item
    if name == "ok":
        if not it and not (player.state or {}).get("current"):
            await osd(player, "Nothing to play")
            return
        playing = player.want_play    # after Stop the player holds no item: the server restarts the current one
        player.send({"type": "cmd", "cmd": "toggle"})
        await osd(player, "Pause" if playing else "Play")
    elif name in ("up", "down"):       # previous / next video (a held key is one press)
        if held:
            return
        player.send({"type": "cmd", "cmd": "prev" if name == "up" else "next"})
        await osd(player, "Previous" if name == "up" else "Next")
    elif name in ("ch_up", "ch_down"):  # volume up / down: mpv's on HDMI, the headphones' own when the sound goes there
        await osd(player, await player.volume_step(VOLUME_STEP if name == "ch_up" else -VOLUME_STEP), 1200)
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
            for _s, _us, etype, code, value in ir.EVENT.iter_unpack(data[:len(data) - len(data) % ir.EVENT.size]):
                if etype == ir.EV_KEY:
                    player.last_key_at = player.mono()
                    if cfg.ir_debug:
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
        proc = await asyncio.create_subprocess_exec(*mpv_args(cfg, sock, player.volume, player.mpv_muted))
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
                    player.want_data(player.item)
                    player.log(f"mtv-player {VERSION} up (mode {cfg.mode}, interpolation "
                               f"{'on' if cfg.interpolation else 'off'}, audio {cfg.audio_device}, volume {player.volume} %)")
                    player.report_output()
                    player.link.reset_link()
                    player.link.send_config()

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
                        with contextlib.suppress(Exception):
                            await player.link.connection_lost()
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
                log(f"server: {e!r}")
            if stop.is_set():
                break
            log(f"disconnected, retrying in {backoff:.0f} s")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), backoff)
            backoff = min(RECONNECT_MAX, backoff * 2)


LYRICS_TICK, LYRICS_FAST = 0.1, 1 / 24   # s between lyrics-screen checks; while a line scrolls / lights up


async def run_lyrics(player, stop):
    """Keeps the lyrics screen in step with the playback position (redraws only when it looks different, ~24 frames/s
    while a line scrolls or lights up)."""
    while not stop.is_set():
        fast = False
        try:
            await player.draw_lyrics()
            fast = player.lyrics_on and player.view.animating(player.mono())
        except MpvError:
            pass
        except Exception as e:  # never let a drawing bug stop the loop
            log(f"lyrics loop: {e!r}")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), LYRICS_FAST if fast else LYRICS_TICK)


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
                                              run_ticks(player, stop), run_lyrics(player, stop), run_ir(cfg, player, stop),
                                              run_evdev(cfg, player, stop))]
    await stop.wait()  # SIGTERM / SIGINT: the server loop sits in a ws read, so cancel instead of waiting for it
    await player.remotes.shutdown()  # a learn in progress is stopped and undone first
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)  # run_mpv's finally terminates mpv
    if player.shader_dir:
        shutil.rmtree(player.shader_dir, ignore_errors=True)
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
