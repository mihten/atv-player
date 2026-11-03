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

  stdin:  JSON lines from atv_audio.py  {"status": "Playing"|"Paused"|"Stopped", "title", "artist", "length" (s)}
  stdout: JSON lines to atv_audio.py    {"cmd": "play"|"pause"|"next"|"prev"}, {"cmd": "seek", "delta": s}
          and {"log": "..."}

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

    def props_changed(iface, changed, invalidated, path=None):
        if iface == "org.bluez.Device1" and changed.get("Connected") is not None:
            if bool(changed["Connected"]):
                player.connected_at = time.monotonic()
                note(f"headphones connected ({str(path).rsplit('/', 1)[-1]})")
            else:
                note(f"headphones disconnected ({str(path).rsplit('/', 1)[-1]})")

    bus.watch_name_owner("org.bluez", bluez_owner)
    bus.add_signal_receiver(interfaces_added, dbus_interface="org.freedesktop.DBus.ObjectManager",
                            signal_name="InterfacesAdded", bus_name="org.bluez")
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
                if isinstance(st, dict):
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
    glib.g_main_loop_run(loop)


if __name__ == "__main__":
    main()
