"""The TV Player's sound on the atv-audio headphones (the sink program on the media server), the way the Apple TV does it.

With "Headphones" chosen the player keeps its picture but mutes mpv, tells the server `route {sink: true}` and sends the
sink a `clock` (item, position, server time, state) four times a second; the sink opens the same file on the headphones
and follows that clock. Rules (owner decisions, same as the Apple TV app):
  - the sound output changes ONLY by a deliberate choice, never by itself (no fall back to HDMI sound);
  - a start waits up to 3 s for the sink's `ready` so picture and sound begin together;
  - the output missing at a start, or lost while playing, pauses playback with a notice that stays until Play; once per
    loss: a Play while the headphones are still off plays without sound;
  - the headphones back while it plays: the sink resumes by itself at the player's position (nothing to do here).
The model is pure: it uses the Player's mpv / send / mono and its own tick(), so a fake clock drives the tests.
"""
import contextlib
import json
import os

LIP_DEFAULT = -100       # ms the sink plays earlier (the Apple TV's default; the headphones' latency is the sink's job)
READY_WAIT = 3.0         # s to wait for the sink's `ready` before the picture starts without it
PING_FAST, PING_SLOW, FAST_PINGS = 2.0, 15.0, 8
SEEK_SETTLE = 0.4        # s after a seek the clock says "stalled" (mpv shows the target before it plays from it)


def usable(sink):
    """The sink program is connected and its output (the headphones) is ready."""
    return bool(sink and sink.get("connected") and (sink.get("output") or {}).get("ready"))


class SinkLink:
    def __init__(self, player):
        self.p = player
        self.on = False                # Sound output = headphones
        self.waiting = False           # a load waits for the sink's `ready`
        self.wait_t0 = 0.0
        self.ready_item = None         # last item the sink reported ready
        self.had_item = False          # the sink already had the item when its load came in
        self.loss_armed = True         # the next loss shows its notice (re-armed when the sink is usable again)
        self.sent_state = ""
        self.sink = None               # last state.sink from the server
        self.notice = None             # text on the TV until Play
        self.samples = []              # (round trip, server time - our time) of the last pings
        self.sent = {}
        self.counter = 0
        self.pings = 0
        self.next_ping = 0.0
        self.seek_until = 0.0
        self.lip_ms = LIP_DEFAULT
        self.load_state()

    # -- the choice, kept across restarts
    def _file(self):
        d = self.p.cfg.state_dir
        return os.path.join(d, "output.json") if d else None

    def load_state(self):
        path = self._file()
        if not path:
            return
        try:
            with open(path) as f:
                self.on = bool(json.load(f).get("headphones"))
        except (OSError, ValueError, AttributeError):
            pass

    def save_state(self):
        path = self._file()
        if not path:
            return
        with contextlib.suppress(OSError):
            os.makedirs(self.p.cfg.state_dir, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"headphones": self.on}, f)
            os.replace(tmp, path)

    # -- server clock (ping / pong, offset from the sample with the lowest round trip, as NTP does)
    def reset_link(self):
        """A new server connection: its monotonic clock may be new."""
        self.samples, self.sent, self.pings, self.next_ping = [], {}, 0, 0.0
        self.sent_state = ""

    def pong(self, msg):
        t0 = self.sent.pop(msg.get("n"), None)
        t = msg.get("t")
        if t0 is None or not isinstance(t, (int, float)):
            return
        now = self.p.mono()
        self.samples.append((now - t0, float(t) - (t0 + now) / 2))
        del self.samples[:-8]

    def server_now(self):
        if not self.samples:
            return None
        return self.p.mono() + min(self.samples)[1]

    def _ping_due(self):
        now = self.p.mono()
        if now < self.next_ping:
            return
        self.counter += 1
        self.sent[self.counter] = now
        for old in [n for n in self.sent if n <= self.counter - 16]:
            del self.sent[old]
        self.pings += 1
        self.next_ping = now + (PING_FAST if self.pings < FAST_PINGS else PING_SLOW)
        self.p.send({"type": "ping", "n": self.counter})

    # -- what is missing, for the notice and the log
    @property
    def usable(self):
        return self.p.connected and usable(self.sink)

    @property
    def target_text(self):
        if not self.p.connected:
            return "atv-audio: no connection to the server"
        sink = self.sink
        if not sink or not sink.get("connected"):
            return "atv-audio is not connected"
        out = sink.get("output")
        if not out:
            return "atv-audio: no output found"
        return f"atv-audio: {out.get('name') or 'headphones'} not connected"

    def status_text(self):
        sink = self.sink
        if not sink or not sink.get("connected"):
            return "atv-audio: not connected"
        out = sink.get("output")
        if not out:
            return "atv-audio: connected, no output found"
        return f"atv-audio: {out.get('name') or 'headphones'}{'' if out.get('ready') else ' (not available)'}"

    # -- messages to the server
    def send_config(self):
        """On every (re)connect and when the sink connects: route, lip sync, the clock."""
        p = self.p
        self.clock(force=True)
        p.send({"type": "lip_sync", "ms": self.lip_ms})
        p.send({"type": "route", "sink": self.on})

    def clock_state(self):
        p, mpv = self.p, self.p.mpv
        if p.item is None:
            return None
        if p.ended_for == p.loads or (mpv and mpv.props.get("eof-reached")):
            return "ended"
        if not p.want_play:
            return "paused"
        props = mpv.props if mpv else {}
        if (self.waiting or not mpv or not p.file_loaded or props.get("pause") or props.get("paused-for-cache")
                or props.get("seeking") or p.mono() < self.seek_until):
            return "stalled"
        return "playing"

    def clock(self, force=False):
        """clock {item, position, at (server time), state}: every tick while playing, at once on a change."""
        p = self.p
        if not self.on or p.item is None:
            return
        at = self.server_now()
        state = self.clock_state()
        if at is None or state is None:
            return
        if not force and state != "playing" and state == self.sent_state:
            return
        pos = p.pos
        if state == "playing":
            tp = p.mpv.props.get("time-pos") if p.mpv else None
            if isinstance(tp, (int, float)):
                pos = float(tp)
        self.sent_state = state
        p.send({"type": "clock", "item": p.item.get("id"), "position": round(max(0.0, pos), 3), "at": at,
                "state": state})

    def note_seek(self):
        self.seek_until = self.p.mono() + SEEK_SETTLE
        self.clock(force=True)

    async def tick(self):
        if self.waiting and self.p.mono() - self.wait_t0 >= READY_WAIT:
            if self.had_item:
                self.p.log(f"atv-audio: no ready within {READY_WAIT:g} s, the sink already has the item open -> starting")
            else:
                self.p.log("atv-audio: timeout, starting muted")
            await self.end_wait()
        if self.on or self.samples:
            self._ping_due()
        self.clock()

    # -- the choice changes
    async def set_on(self, on):
        """Web remote: Sound output = headphones / HDMI, at once. While it plays: pause with a notice until Play."""
        p = self.p
        if on == self.on:
            p.report_output()
            return
        playing = p.want_play and p.item is not None
        if playing:
            await p.local_pause("sound output")
        self.on = on
        self.save_state()
        self.waiting = False
        self.sent_state = ""
        await p.apply_mute()
        if on:
            self.clock(force=True)  # before the route: a sink that opens the item then opens it at our position
        p.send({"type": "route", "sink": on})
        p.log(f"atv-audio: sound output -> {'headphones, mpv muted' if on else 'HDMI, mpv sound on'}"
              f"{' at ' + str((p.item or {}).get('id')) if p.item else ''}; {self.status_text()}")
        self.loss_armed = self.usable if playing else True
        if playing:
            await self.show_notice(f"Sound output changed to {'headphones' if on else 'HDMI'} — playback paused")
        p.report_output()

    # -- a start
    async def begin_item(self, item, autoplay, from_server):
        """Item start. Headphones chosen: mpv is muted for every item. A server load with autoplay waits for the sink's
        ready (max 3 s) when the sink and its output are there; else it starts muted, or holds with the notice once."""
        self.waiting = False
        self.sent_state = ""
        self.seek_until = 0.0
        if not self.on:
            return
        await self.p.apply_mute()
        if from_server:
            self.had_item = self.ready_item == item.get("id")
            self.ready_item = None
        if not autoplay:
            return
        await self.acknowledge("play")  # an item that starts playing ends a notice
        if not self.usable:
            if not await self.check_target(f"not available at the start of {item.get('id')}"):
                self.p.log(f"atv-audio: {item.get('id')} starts muted without atv-audio; {self.target_text}")
            return
        if not from_server:
            return  # the player's own reopen: the sink follows the clock
        self.waiting, self.wait_t0 = True, self.p.mono()

    async def end_wait(self):
        if not self.waiting:
            return
        self.waiting = False
        p = self.p
        if p.want_play and p.mpv and p.item:
            with contextlib.suppress(Exception):
                await p.mpv.set_property("pause", False)

    async def ready(self, item_id):
        """Server: the sink has the item open and buffered."""
        self.ready_item = item_id
        p = self.p
        if self.waiting and p.item and p.item.get("id") == item_id:
            p.log(f"atv-audio: ready after {int((p.mono() - self.wait_t0) * 1000)} ms")
            await self.end_wait()

    # -- loss
    async def state_changed(self, old, new):
        """state.sink changed (or the first state on a new socket)."""
        if new and new.get("connected") and not (old and old.get("connected")):
            self.send_config()
        if not self.on:
            return
        was, now = usable(old), usable(new)
        if was and not now:
            self.p.log(f"atv-audio: {'output gone' if new and new.get('connected') else 'disconnected'}; mpv stays muted; "
                       f"{self.status_text()}")
        elif not was and now:
            self.p.log(f"atv-audio: available again; {self.status_text()}")
        await self.check_target("output lost" if new and new.get("connected") else "sink disconnected")
        if self.waiting and not self.usable:
            self.p.log("atv-audio: lost while waiting for ready, starting muted")
            await self.end_wait()

    async def check_target(self, why):
        """The target is missing while playback is wanted: pause and a notice that stays until Play. Once per loss: it
        is re-armed only when the target is usable again. True when it paused."""
        if not self.on:
            return False
        if self.usable:
            self.loss_armed = True
            return False
        if not (self.loss_armed and self.p.want_play):
            return False
        self.loss_armed = False
        await self.p.local_pause("atv-audio")
        await self.show_notice(f"{self.target_text} — playback paused")
        self.p.log(f"atv-audio: {why} -> paused (notice); {self.status_text()}")
        return True

    async def hold_play(self):
        """Play pressed while atv-audio is missing and its loss was not shown yet: the notice instead of starting."""
        if not (self.on and not self.usable and self.loss_armed):
            return False
        self.loss_armed = False
        await self.p.local_pause("atv-audio")
        await self.show_notice(f"{self.target_text} — playback paused")
        self.p.log(f"atv-audio: not available at play -> paused (notice); {self.status_text()}")
        return True

    async def connection_lost(self):
        await self.check_target("server connection lost")
        await self.end_wait()

    # -- the notice on the TV
    async def show_notice(self, text):
        self.notice = text
        self.p.send({"type": "notice", "text": text})
        await self.p.draw_notice()

    async def acknowledge(self, how):
        if self.notice is None:
            return False
        self.notice = None
        await self.p.draw_notice()
        self.p.log(f"atv-audio: notice acknowledged ({how})")
        self.p.send({"type": "cmd", "cmd": "dismiss"})
        return True
