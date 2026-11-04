#!/usr/bin/env python3
"""atv-audio headphone buttons (started by atv_audio.py, not on its own).

Registers a media player with BlueZ (org.bluez.Media1.RegisterPlayer = an AVRCP target). Bluetooth headphones then
send their buttons to it: Play / Pause (tap), Next / Previous (swipe), and the wear sensor's Pause (taken off) /
Play (put on). Without a registered player BlueZ only turns Play / Pause into input-device keys, the rest is lost.

Fast forward / rewind (held) never reach a registered player: BlueZ 5.82's AVRCP target handles only play, stop,
pause, forward and backward (profiles/audio/avrcp.c passthrough_handlers) and hands the rest to its input device
"<headphones> (AVRCP)" as KEY_FASTFORWARD / KEY_REWIND (avctp.c key_map; press, kernel autorepeat while held,
release). Those keys are read from /dev/input/event* here (the user needs group `input`): the first press seeks
SEEK_STEP s, every SEEK_EVERY s of holding one more step.

It also manages the headphones for the web remote (Settings -> atv-audio): list paired / found devices, scan, pair
(with an agent that accepts only a pairing started here, within PAIR_WINDOW s), connect, disconnect, forget.

  stdin:  JSON lines from atv_audio.py  {"status": "Playing"|"Paused"|"Stopped", "title", "artist", "length" (s)}
          or {"op": "bt", "action": "refresh"|"scan"|"pair"|"connect"|"disconnect"|"forget", "address": ...}
  stdout: JSON lines to atv_audio.py    {"cmd": "play"|"pause"|"next"|"prev"}, {"cmd": "seek", "delta": s},
          {"bt": {"devices": [...], "scanning", "busy", "error"}} and {"log": "..."}

A Play within PLAY_GUARD s after a headphone (re)connects is dropped here (headphones that are switched on while
worn send Play by themselves: no automatic start). Needs python3-dbus and GLib (libglib-2.0); no PyGObject.
Exit code 3 = python3-dbus missing (atv_audio.py then stops trying).
"""
import ctypes
import glob
import json
import os
import struct
import sys
import time

try:
    import dbus
    import dbus.service
    import dbus.mainloop.glib
except ImportError:
    sys.exit(3)

PATH = "/eu/atvplayer/atvaudio/player"
MPRIS = "org.mpris.MediaPlayer2.Player"
PLAY_GUARD = 10.0
G_IO_IN, G_IO_ERR, G_IO_HUP = 1, 8, 16
EV_KEY, KEY_REWIND, KEY_FASTFORWARD = 1, 168, 208
SEEK_STEP = 10.0   # s per step
SEEK_EVERY = 0.6   # s of holding per further step
EVENT = struct.Struct("llHHi")  # struct input_event on 64-bit Linux
AGENT_PATH = "/eu/atvplayer/atvaudio/agent"
PAIR_WINDOW = 60.0   # s the agent accepts a pairing after the web remote asked for one
SCAN_SECONDS = 25
# Audio Sink, A2DP, Headset, Handsfree: headphones / speakers
AUDIO_UUIDS = ("0000110b", "0000110d", "00001108", "0000111e")


def out(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def note(text):
    out({"log": text})


class Player(dbus.service.Object):
    def __init__(self, bus):
        super().__init__(bus, PATH)
        self.connected_at = -1e9
        self.props = {
            "PlaybackStatus": dbus.String("Stopped"), "LoopStatus": dbus.String("None"),
            "Shuffle": dbus.Boolean(False), "Rate": dbus.Double(1.0), "Volume": dbus.Double(1.0),
            "Position": dbus.Int64(0), "Metadata": self.metadata("", "", 0),
            "CanGoNext": dbus.Boolean(True), "CanGoPrevious": dbus.Boolean(True), "CanPlay": dbus.Boolean(True),
            "CanPause": dbus.Boolean(True), "CanSeek": dbus.Boolean(False), "CanControl": dbus.Boolean(True),
        }

    @staticmethod
    def metadata(title, artist, length):
        return dbus.Dictionary({"xesam:title": dbus.String(title or " "),
                                "xesam:artist": dbus.Array([dbus.String(artist or " ")], signature="s"),
                                "mpris:length": dbus.Int64(int(max(0.0, length) * 1e6))}, signature="sv")

    def update(self, st):
        changed = {}
        status = st.get("status") if st.get("status") in ("Playing", "Paused", "Stopped") else "Stopped"
        if status != self.props["PlaybackStatus"]:
            changed["PlaybackStatus"] = self.props["PlaybackStatus"] = dbus.String(status)
        meta = self.metadata(st.get("title"), st.get("artist"), float(st.get("length") or 0))
        if meta != self.props["Metadata"]:
            changed["Metadata"] = self.props["Metadata"] = meta
        if changed:
            self.PropertiesChanged(MPRIS, changed, [])

    def resend(self):
        """The headphones may think the state changed (a dropped Play): tell them ours again."""
        self.PropertiesChanged(MPRIS, {"PlaybackStatus": self.props["PlaybackStatus"]}, [])

    def command(self, cmd, source):
        if cmd == "play" and time.monotonic() - self.connected_at < PLAY_GUARD:
            note(f"Play {time.monotonic() - self.connected_at:.1f} s after the headphones connected: ignored")
            self.resend()
            return
        out({"cmd": cmd})

    @dbus.service.method(MPRIS)
    def Play(self):
        self.command("play", "Play")

    @dbus.service.method(MPRIS)
    def Pause(self):
        self.command("pause", "Pause")

    @dbus.service.method(MPRIS)
    def PlayPause(self):
        self.command("pause" if self.props["PlaybackStatus"] == "Playing" else "play", "PlayPause")

    @dbus.service.method(MPRIS)
    def Stop(self):
        self.command("pause", "Stop")

    @dbus.service.method(MPRIS)
    def Next(self):
        self.command("next", "Next")

    @dbus.service.method(MPRIS)
    def Previous(self):
        self.command("prev", "Previous")

    @dbus.service.method(MPRIS, in_signature="x")
    def Seek(self, offset):
        pass

    @dbus.service.method(MPRIS, in_signature="ox")
    def SetPosition(self, track, position):
        pass

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
    def Get(self, iface, name):
        return self.props[name]

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        return self.props

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ssv")
    def Set(self, iface, name, value):
        pass  # volume / shuffle / repeat from the headphones: not used (volume is the transport's)

    @dbus.service.signal(dbus.PROPERTIES_IFACE, signature="sa{sv}as")
    def PropertiesChanged(self, iface, changed, invalidated):
        pass


class SeekKeys:
    """KEY_FASTFORWARD / KEY_REWIND from BlueZ's "(AVRCP)" input devices -> seek commands."""

    def __init__(self):
        self.open = {}         # path -> (fd, watch id)
        self.denied = set()    # paths already reported as not readable
        self.last = {}         # key -> monotonic time of its last step

    @staticmethod
    def devices():
        found = []
        for sysdir in glob.glob("/sys/class/input/event*"):
            try:
                with open(os.path.join(sysdir, "device", "name")) as f:
                    name = f.read().strip()
            except OSError:
                continue
            if name.endswith("(AVRCP)"):
                found.append(("/dev/input/" + os.path.basename(sysdir), name))
        return found

    def scan(self, watch):
        """Open new AVRCP input devices (called at start, on connects and every few seconds)."""
        for path, name in self.devices():
            if path in self.open:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError as e:
                if path not in self.denied:
                    self.denied.add(path)
                    note(f"seek keys: cannot read {path} ({name}): {e.strerror}; the user needs group 'input'")
                continue
            self.denied.discard(path)
            self.open[path] = (fd, watch(fd, path))
            note(f"seek keys: reading {path} ({name})")

    def closed(self, path):
        fd, _ = self.open.pop(path, (None, None))
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def read(self, fd):
        """False when the device went away."""
        while True:
            try:
                data = os.read(fd, EVENT.size * 64)
            except BlockingIOError:
                return True
            except OSError:
                return False
            if not data:
                return False
            for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
                _, _, typ, code, value = EVENT.unpack_from(data, off)
                if typ == EV_KEY and code in (KEY_FASTFORWARD, KEY_REWIND):
                    self.key(code, value)

    def key(self, code, value):
        if code not in (KEY_FASTFORWARD, KEY_REWIND):
            return
        now = time.monotonic()
        if value == 1 or (value == 2 and now - self.last.get(code, 0) >= SEEK_EVERY):
            self.last[code] = now
            out({"cmd": "seek", "delta": SEEK_STEP if code == KEY_FASTFORWARD else -SEEK_STEP})


def is_audio(dev):
    uuids = [str(u).lower() for u in dev.get("UUIDs", [])]
    if any(u.startswith(a) for u in uuids for a in AUDIO_UUIDS):
        return True
    cls = int(dev.get("Class", 0) or 0)
    return (cls >> 8) & 0x1F == 0x04  # major device class Audio/Video


def device_view(dev, scanning):
    """One device for the web remote, or None when it does not belong in the list."""
    paired = bool(dev.get("Paired"))
    audio = is_audio(dev)
    if not paired and not (audio and scanning and (dev.get("Name") or dev.get("Alias"))):
        return None
    return {"address": str(dev.get("Address") or ""), "name": str(dev.get("Alias") or dev.get("Name") or ""),
            "paired": paired, "connected": bool(dev.get("Connected")), "audio": audio}


class Agent(dbus.service.Object):
    """Pairing agent (no display, no keyboard): headphones pair with "just works". Accepts ONLY while a pairing the web
    remote asked for is running, so nothing nearby can pair with the box on its own."""

    def __init__(self, bus, manager):
        super().__init__(bus, AGENT_PATH)
        self.manager = manager

    def _check(self):
        if time.monotonic() > self.manager.pair_until:
            raise dbus.DBusException("not pairing now", name="org.bluez.Error.Rejected")

    @dbus.service.method("org.bluez.Agent1")
    def Release(self):
        pass

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        self._check()
        return "0000"

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        self._check()
        return dbus.UInt32(0)

    @dbus.service.method("org.bluez.Agent1", in_signature="ouq")
    def DisplayPasskey(self, device, passkey, entered):
        pass

    @dbus.service.method("org.bluez.Agent1", in_signature="os")
    def DisplayPinCode(self, device, pincode):
        pass

    @dbus.service.method("org.bluez.Agent1", in_signature="ou")
    def RequestConfirmation(self, device, passkey):
        self._check()

    @dbus.service.method("org.bluez.Agent1", in_signature="o")
    def RequestAuthorization(self, device):
        self._check()

    @dbus.service.method("org.bluez.Agent1", in_signature="os")
    def AuthorizeService(self, device, uuid):
        # an already paired (trusted) device never gets here; a new one only while pairing
        self._check()

    @dbus.service.method("org.bluez.Agent1")
    def Cancel(self):
        pass


class Headphones:
    """Bluetooth devices for the web remote, through BlueZ's D-Bus API (async calls: the main loop never blocks)."""

    def __init__(self, bus, later):
        self.bus = bus
        self.later = later          # later(ms, fn): run fn on the main loop after ms
        self.adapter = None
        self.scanning = False
        self.busy = None
        self.error = None
        self.pair_until = 0.0
        self.emit_pending = False
        self.agent_ok = False
        self.last = None

    # -- plumbing
    def objects(self):
        return dbus.Interface(self.bus.get_object("org.bluez", "/"),
                              "org.freedesktop.DBus.ObjectManager").GetManagedObjects()

    def find(self, address=None):
        """(adapter path, {device address: device path}) of the first adapter."""
        adapter, devices = None, {}
        for path, ifaces in self.objects().items():
            if "org.bluez.Adapter1" in ifaces and adapter is None:
                adapter = str(path)
            if "org.bluez.Device1" in ifaces:
                devices[str(ifaces["org.bluez.Device1"].get("Address"))] = str(path)
        self.adapter = adapter
        return adapter, devices

    def setup_agent(self):
        try:
            Agent(self.bus, self)
            mgr = dbus.Interface(self.bus.get_object("org.bluez", "/org/bluez"), "org.bluez.AgentManager1")
            mgr.RegisterAgent(dbus.ObjectPath(AGENT_PATH), "NoInputNoOutput")
            mgr.RequestDefaultAgent(dbus.ObjectPath(AGENT_PATH))
            self.agent_ok = True
            note("pairing agent registered")
        except dbus.DBusException as e:
            note(f"pairing agent: {e.get_dbus_name()} {e.get_dbus_message()}")

    def emit(self, force=False):
        """Device list to atv_audio.py, at most every 0.3 s (BlueZ sends bursts of property changes)."""
        if self.emit_pending and not force:
            return
        self.emit_pending = True

        def go():
            self.emit_pending = False
            try:
                objs = self.objects()
            except dbus.DBusException as e:
                info = {"devices": [], "scanning": False, "busy": None, "error": f"BlueZ: {e.get_dbus_name()}"}
            else:
                devs = []
                for path, ifaces in objs.items():
                    d = ifaces.get("org.bluez.Device1")
                    v = device_view(d, self.scanning) if d is not None else None
                    if v:
                        devs.append(v)
                devs.sort(key=lambda d: (not d["connected"], not d["paired"], d["name"].lower()))
                info = {"devices": devs, "scanning": self.scanning, "busy": self.busy, "error": self.error}
            if info != self.last or force:
                self.last = info
                out({"bt": info})
        self.later(0 if force else 300, go)

    def done(self, error=None):
        self.busy = None
        self.error = error
        self.emit(force=True)

    def fail(self, what):
        def handler(e):
            name = e.get_dbus_name() if isinstance(e, dbus.DBusException) else type(e).__name__
            msg = e.get_dbus_message() if isinstance(e, dbus.DBusException) else str(e)
            note(f"bluetooth {what}: {name} {msg}")
            self.done(f"{what} failed: {msg or name}")
        return handler

    def device(self, address):
        adapter, devices = self.find()
        path = devices.get(address or "")
        if path is None:
            raise ValueError("that device is no longer known: scan again")
        return path, dbus.Interface(self.bus.get_object("org.bluez", path), "org.bluez.Device1")

    # -- actions
    def action(self, action, address=None):
        self.error = None
        try:
            getattr(self, "do_" + action)(address)
        except (ValueError, dbus.DBusException) as e:
            msg = e.get_dbus_message() if isinstance(e, dbus.DBusException) else str(e)
            self.done(msg)

    def do_refresh(self, _address):
        self.emit(force=True)

    def do_scan(self, _address):
        adapter, _ = self.find()
        if adapter is None:
            raise ValueError("no Bluetooth adapter")
        a = dbus.Interface(self.bus.get_object("org.bluez", adapter), "org.bluez.Adapter1")
        try:
            a.SetDiscoveryFilter({"Transport": dbus.String("bredr")})
        except dbus.DBusException:
            pass
        self.scanning = True
        self.busy = "Looking for headphones in pairing mode…"
        self.emit(force=True)

        def stop():
            try:
                a.StopDiscovery()
            except dbus.DBusException:
                pass
            self.scanning = False
            self.done(self.error)
        a.StartDiscovery(reply_handler=lambda: None, error_handler=self.fail("scan"))
        self.later(SCAN_SECONDS * 1000, stop)

    def do_pair(self, address):
        path, dev = self.device(address)
        props = dbus.Interface(self.bus.get_object("org.bluez", path), dbus.PROPERTIES_IFACE)
        name = str(props.Get("org.bluez.Device1", "Alias"))
        if not self.agent_ok:
            self.setup_agent()
        self.pair_until = time.monotonic() + PAIR_WINDOW
        self.busy = f"Pairing {name}…"
        self.emit(force=True)

        def paired():
            self.pair_until = 0.0
            try:
                props.Set("org.bluez.Device1", "Trusted", dbus.Boolean(True))
            except dbus.DBusException as e:
                note(f"trust {name}: {e.get_dbus_message()}")
            note(f"paired {name}, connecting")
            self.busy = f"Connecting {name}…"
            self.emit(force=True)
            dev.Connect(reply_handler=lambda: self.done(), error_handler=self.fail("connect"), timeout=30)

        def pair_failed(e):
            self.pair_until = 0.0
            self.fail("pairing")(e)
        dev.Pair(reply_handler=paired, error_handler=pair_failed, timeout=60)

    def do_connect(self, address):
        path, dev = self.device(address)
        self.busy = "Connecting…"
        self.emit(force=True)
        dev.Connect(reply_handler=lambda: self.done(), error_handler=self.fail("connect"), timeout=30)

    def do_disconnect(self, address):
        path, dev = self.device(address)
        self.busy = "Disconnecting…"
        self.emit(force=True)
        dev.Disconnect(reply_handler=lambda: self.done(), error_handler=self.fail("disconnect"), timeout=20)

    def do_forget(self, address):
        path, _ = self.device(address)
        a = dbus.Interface(self.bus.get_object("org.bluez", self.adapter), "org.bluez.Adapter1")
        a.RemoveDevice(dbus.ObjectPath(path), reply_handler=lambda: self.done(), error_handler=self.fail("forget"))


def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    player = Player(bus)
    registered = set()

    def register_all():
        try:
            objs = dbus.Interface(bus.get_object("org.bluez", "/"),
                                  "org.freedesktop.DBus.ObjectManager").GetManagedObjects()
        except dbus.DBusException as e:
            note(f"BlueZ not reachable: {e.get_dbus_name()}")
            return
        for path, ifaces in objs.items():
            if "org.bluez.Media1" in ifaces and path not in registered:
                try:
                    dbus.Interface(bus.get_object("org.bluez", path), "org.bluez.Media1").RegisterPlayer(
                        dbus.ObjectPath(PATH), player.props)
                    registered.add(path)
                    note(f"player registered on {path}")
                except dbus.DBusException as e:
                    note(f"RegisterPlayer on {path} failed: {e.get_dbus_name()} {e.get_dbus_message()}")

    def bluez_owner(name):
        registered.clear()
        if name:
            register_all()

    def interfaces_added(path, ifaces):
        if "org.bluez.Media1" in ifaces:
            register_all()
        if "org.bluez.Device1" in ifaces and HP:
            HP[0].emit()

    def interfaces_removed(path, ifaces):
        if "org.bluez.Device1" in ifaces and HP:
            HP[0].emit()

    HP = []  # the Headphones manager, once the main loop helpers exist

    def props_changed(iface, changed, invalidated, path=None):
        if iface in ("org.bluez.Device1", "org.bluez.Adapter1") and HP:
            HP[0].emit()
        if iface == "org.bluez.Device1" and changed.get("Connected") is not None:
            if bool(changed["Connected"]):
                player.connected_at = time.monotonic()
                note(f"headphones connected ({str(path).rsplit('/', 1)[-1]})")
            else:
                note(f"headphones disconnected ({str(path).rsplit('/', 1)[-1]})")

    bus.watch_name_owner("org.bluez", bluez_owner)
    bus.add_signal_receiver(interfaces_added, dbus_interface="org.freedesktop.DBus.ObjectManager",
                            signal_name="InterfacesAdded", bus_name="org.bluez")
    bus.add_signal_receiver(interfaces_removed, dbus_interface="org.freedesktop.DBus.ObjectManager",
                            signal_name="InterfacesRemoved", bus_name="org.bluez")
    bus.add_signal_receiver(props_changed, dbus_interface=dbus.PROPERTIES_IFACE, signal_name="PropertiesChanged",
                            bus_name="org.bluez", path_keyword="path")

    glib = ctypes.CDLL("libglib-2.0.so.0")
    glib.g_main_loop_new.restype = ctypes.c_void_p
    glib.g_main_loop_new.argtypes = [ctypes.c_void_p, ctypes.c_int]
    glib.g_main_loop_run.argtypes = [ctypes.c_void_p]
    glib.g_main_loop_quit.argtypes = [ctypes.c_void_p]
    glib.g_io_channel_unix_new.restype = ctypes.c_void_p
    glib.g_io_channel_unix_new.argtypes = [ctypes.c_int]
    io_func = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)
    glib.g_io_add_watch.restype = ctypes.c_uint
    glib.g_io_add_watch.argtypes = [ctypes.c_void_p, ctypes.c_int, io_func, ctypes.c_void_p]
    loop = glib.g_main_loop_new(None, 0)
    buf = bytearray()

    def on_stdin(_channel, condition, _data):
        try:
            data = os.read(0, 65536) if condition & G_IO_IN else b""
            if not data:
                glib.g_main_loop_quit(loop)  # atv_audio.py went away
                return 0
            buf.extend(data)
            while b"\n" in buf:
                line, _, rest = bytes(buf).partition(b"\n")
                buf[:] = rest
                try:
                    st = json.loads(line)
                except ValueError:
                    continue
                if isinstance(st, dict) and st.get("op") == "bt":
                    if HP:
                        HP[0].action(str(st.get("action")), st.get("address"))
                elif isinstance(st, dict):
                    player.update(st)
            return 1
        except Exception as e:  # never let the watch die silently
            note(f"stdin: {e!r}")
            return 1

    callback = io_func(on_stdin)  # keep a reference: GLib calls it for the life of the loop
    glib.g_io_add_watch(glib.g_io_channel_unix_new(0), G_IO_IN | G_IO_ERR | G_IO_HUP, callback, None)

    seeks = SeekKeys()
    keepalive = []  # ctypes callbacks GLib still holds

    def watch_device(fd, path):
        def on_event(_channel, condition, _data):
            try:
                if condition & (G_IO_ERR | G_IO_HUP) or not seeks.read(fd):
                    seeks.closed(path)
                    return 0
                return 1
            except Exception as e:
                note(f"seek keys: {e!r}")
                return 1
        cb = io_func(on_event)
        keepalive.append(cb)
        return glib.g_io_add_watch(glib.g_io_channel_unix_new(fd), G_IO_IN | G_IO_ERR | G_IO_HUP, cb, None)

    timeout_func = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)
    glib.g_timeout_add_seconds.restype = ctypes.c_uint
    glib.g_timeout_add_seconds.argtypes = [ctypes.c_uint, timeout_func, ctypes.c_void_p]

    def rescan(_data):
        try:
            seeks.scan(watch_device)
        except Exception as e:
            note(f"seek keys: {e!r}")
        return 1

    rescan_cb = timeout_func(rescan)  # the AVRCP input device appears a moment after each connect
    glib.g_timeout_add_seconds(3, rescan_cb, None)
    rescan(None)

    glib.g_timeout_add.restype = ctypes.c_uint
    glib.g_timeout_add.argtypes = [ctypes.c_uint, timeout_func, ctypes.c_void_p]
    pending_calls = []  # ctypes callbacks alive until GLib ran them

    def later(ms, fn):
        def once(_data):
            try:
                fn()
            except Exception as e:
                note(f"bluetooth: {e!r}")
            finally:
                if cb in pending_calls:
                    pending_calls.remove(cb)
            return 0
        cb = timeout_func(once)
        pending_calls.append(cb)
        glib.g_timeout_add(max(0, int(ms)), cb, None)

    HP.append(Headphones(bus, later))
    HP[0].setup_agent()
    HP[0].emit(force=True)
    glib.g_main_loop_run(loop)


if __name__ == "__main__":
    main()
