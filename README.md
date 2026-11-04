# ATV Player

Self-hosted Apple TV player for a home music + music-video library: one queue mixing FLAC
songs and music videos, with next / previous / shuffle, controllable from other devices.

## Status

Personal project, in daily use. Two parts: a tvOS app (SwiftUI + TVVLCKit, plays original files)
and a small media server for a LAN host (`server/`).

## How it is built (no Mac needed)

- `project.yml` is an [XcodeGen](https://github.com/yonaskolb/XcodeGen) spec; the Xcode
  project is generated on the runner, not committed.
- `.github/workflows/build.yml` runs on GitHub's `macos-26` runner (Xcode 26.6, tvOS 26.5
  SDK), builds an **unsigned** Release app for `appletvos`, zips it as `AtvPlayer.ipa` and
  publishes it as a GitHub release `build-<n>`.
- Signing and installation happen on the LAN with
  [ATVLoadly](https://github.com/bitxeno/atvloadly), which signs with a free Apple ID and
  re-signs before the 7-day expiry.

Deployment target: tvOS 17.0.

## ATVLoadly image

`infra/atvloadly/Dockerfile` builds a local ATVLoadly image: upstream `bitxeno/atvloadly:v0.7.1`
with `plumesign` rebuilt from `bitxeno/PlumeImpactor@v2.7.0-patch.1` plus the one-line Windows iTunes
Client-Info change from `claration/impactor#290` (Impactor v2.7.1). Apple blocked the previous
client info on 2026-10-14 (`bitxeno/atvloadly#215`). The binary itself is not committed; rebuild it
on Ubuntu 22.04 with `cargo build --release -p plumesign`. Return to the upstream image once it ships the fix.

## Media server

`server/` runs on a LAN host: scans read-only library folders into SQLite (duplicates hidden,
lossless preferred), streams original files with HTTP Range (`/media/<id>`), serves cover art /
video frame grabs (`/art/<id>`) and owns the play queues: one for the Apple TV app
(`role=player&kind=tv`) and a separate one for the iPhone app (`kind=phone`), so both play
independently. The web remote at `/` (`role=remote`) controls either queue; all changes are
pushed over WebSocket. The server advertises itself over Bonjour (`_atvmedia._tcp`), so the
apps need no configured address.

Site-specific settings are not in this repo: copy `server/site.example/` to `server/site/` and
put the deploy target / ATVLoadly address in `local.env` at the repo root (both git-ignored),
then deploy with `server/deploy.sh`. `server/test_e2e.py` runs an end-to-end test with a
simulated player; the other `test_*.py` start their own throwaway instance.

## iPhone in the car

The iPhone app has no CarPlay app of its own: that needs Apple's CarPlay entitlement, which a free
Apple ID cannot get. CarPlay's built-in **Now Playing** screen shows the app instead, like the lock screen
and Control Center:

- **Previous / Play-Pause / Next** and the position (no ±15 s buttons, on purpose).
- **Shuffle** and **Repeat** (off / all / one) where iOS shows them (CarPlay; the lock screen does not):
  they show the queue's current state, also after a change in the app or the web remote, and change it.
- **Love**: hearts the current item (the same ♥ as in the player and the web remote).

## Offline (iPhone downloads)

For trips without the home server, the iPhone app downloads lists for offline listening:

- **What:** turn on **Download for offline** in a playlist, in **Loved** or in **Most played** (Media server mode).
  The size is shown before anything starts; a download that would leave less than 1 GB free is refused.
- **Audio only, best version:** for each item the server picks the version of the song with the best audio
  (lossless before lossy, then bit depth, sample rate; the song or one of its music videos) and sends it unchanged:
  a song's original file (FLAC stays FLAC), a music video's default audio track as a stream copy. Never a video
  file, never a transcode (`/media/<id>?audio=best`, list sizes from `/api/offline/<list>`).
- **Where to switch:** Settings → Server → Library → **Offline (downloads)**. This is the only way in and out:
  the app never switches by itself (no fallback when the server is unreachable). In Offline the app makes no
  server connection at all; it plays the downloaded lists (shuffled, repeat all) with the normal sound chain,
  loudness levelling (the server's measurement is stored), lyrics and covers. A ♥ set offline is sent to the
  server the next time the app connects in Media server mode.
- **Downloads** run in the background (also with the phone locked), one or two at a time, resumed after a failure;
  **Only on Wi-Fi** (Settings → Downloads, default on).
- **Staying current:** while the app is connected in Media server mode, a downloaded list that changed on the
  server is updated: new items are fetched, removed ones deleted (unless another downloaded list still has them),
  re-scanned files fetched again.
- **Space:** Settings → Downloads shows the space used and free, each list with its size and state, Remove per
  list and Remove all. The files live in the app's own storage (not in the Files app, not in the iCloud backup).

## Sound through atv-audio (optional)

The Apple TV keeps showing the video; a small Linux machine plays the sound in sync. Why: the Apple TV
sends Bluetooth audio as AAC only, while Linux (PipeWire + BlueZ) can send aptX, aptX HD or LDAC when the
headphones support it. A USB transmitter dongle (e.g. an aptX Adaptive dongle) is just a USB sound card to
Linux: it pairs and encodes by itself. With it off (the default) nothing changes.

The sink (`server/sink/atv_audio.py`, Python 3 + aiohttp + [mpv](https://mpv.io)) connects to the media
server as `/ws?role=sink&session=tv`, reads the sound of the current item over HTTP
(`/media/<id>?audio=main`, never from file mounts) and follows the TV's playback clock: more than 150 ms
off → seek, less → mpv speed trimmed by at most 0.2 % (inaudible) until aligned. It applies the TV's
equalizer / gain / limiter itself (Settings → Sound), and keeps the output open while the queue plays and
for 5 minutes after a pause or stop, then releases it so the headphones can switch off or connect elsewhere.

### Where it runs

The program is the same everywhere; only its env file differs.

1. **Media server alone (LXC / bare metal):** the sink as a second service on the same machine. Output =
   a USB dongle (pass the ALSA device into the container, `ATV_AUDIO_OUTPUT=alsa:<device>`), or the host's
   PipeWire/PulseAudio socket if the host runs an audio system with built-in Bluetooth.
2. **Media server + a separate audio machine or VM:** PipeWire + BlueZ + the sink on that machine (built-in
   Bluetooth or a dongle); it connects to the server over the LAN.
3. **Docker (Linux hosts only):** `Dockerfile` + `compose.yaml` at the repo root. Create `.env` next to
   `compose.yaml` (the format is in its header: host library folders, `ATV_ROOTS` with the container paths
   `/media/music` / `/media/videos`; keep it out of git), then `docker compose up -d` for the server, or
   `docker compose --profile audio up -d` with the sink. The server uses host networking for Bonjour. Pick
   one of the two output variants commented in the `atv-audio` service: (a) the host's PipeWire/Pulse socket
   (`ATV_AUDIO_OUTPUT=pulse`, runs as the host user that owns the audio session, `ATV_AUDIO_UID` /
   `ATV_AUDIO_GID` in `.env`) or (b) a USB dongle (`/dev/snd` + group `audio`, `ATV_AUDIO_OUTPUT=alsa:<device>`).

### Installing the sink (systemd)

Needs `python3`, `python3-aiohttp`, `mpv`, and `pactl` (`pulseaudio-utils`, for the output / codec status).
PipeWire runs per user, so the sink is a user unit of the account that owns the audio session:

```sh
sudo install -D -m 755 server/sink/atv_audio.py /opt/atv-audio/atv_audio.py   # the only root step
install -D server/sink/atv-audio.service ~/.config/systemd/user/atv-audio.service
install -D server/sink/env.example ~/.config/atv-audio/env    # then set ATV_AUDIO_SERVER etc.
loginctl enable-linger $USER                                   # start at boot without a login
systemctl --user enable --now atv-audio
```

With an ALSA output it can also run as a system unit (see the comments in `atv-audio.service`).

`ATV_AUDIO_OUTPUT` chooses where the sound goes:

| Value | Output |
| --- | --- |
| `pipewire` (default) | the user's PipeWire: built-in Bluetooth through BlueZ, or a dongle |
| `pulse` | PulseAudio or pipewire-pulse (`PULSE_SERVER` is honoured) |
| `pulse:<server>` | a PulseAudio server elsewhere, e.g. `pulse:unix:/run/user/<uid>/pulse/native` |
| `alsa:<device>` | an ALSA device directly, e.g. `alsa:plughw:CARD=<card>,DEV=0` (list: `aplay -L`) |

`ATV_AUDIO_NAME` is the name shown on the TV (default `atv-audio`).

### Two atv-audio programs (e.g. a dongle and built-in Bluetooth)

Several sink programs may connect to one server, each with its own `ATV_AUDIO_ID`. One is active; the others wait
in standby (they close their output and hand the headphones over). Settings → atv-audio → **Sound goes to**
switches: the active one frees the headphones first, then the chosen one connects them and continues where the
queue or the Apple TV is (a few seconds of silence). The choice is kept across restarts. Use
`ATV_AUDIO_VOLUME=mpv` on a PipeWire/BlueZ program so both have the same volume control (output pinned at 100 %,
the slider is mpv's volume, as with a dongle).

**Blind test** (Settings → atv-audio, needs exactly two programs): build a list from the songs you choose (play,
pause where the excerpt should start, "Add current song"), set excerpt length and trials per song, Start. Each
trial plays the same excerpt through A and B (the two programs in random order, the same fixed switch time
before each, `ATV_ABTEST_GAP` seconds, default 10); vote A better / B better / Same, or replay A / B. The status
line and settings hide which program plays until the end; the result lists every trial, the totals and an exact
binomial p-value, and is saved in the server's data folder (`abtest-<time>.json`).

### Sennheiser BTD 700 dongle

A plain USB dongle tells Linux nothing about the headphones. The BTD 700 does, over its HID interface
(`server/sink/btd700.py`, protocol after [btd700ctl](https://github.com/sobalap/btd700ctl); no extra
packages). Set `ATV_AUDIO_OUTPUT=alsa:plughw:CARD=B700,DEV=0` and `ATV_AUDIO_DONGLE=<hidraw node>` (interface 0 of
USB `3542:3001`, the one with report 0x34). The sink then:

- knows when the headphones go and come back (the dongle's own events): the same pause / resume rules as with
  built-in Bluetooth;
- shows the codec the dongle uses (e.g. aptX Adaptive) and offers Connect / Disconnect in the web remote
  (Settings → atv-audio and the player card); new headphones are paired with the dongle's button. Disconnect
  pauses what plays, closes the sound device (an open stream makes the dongle connect the headphones again) and
  opens nothing until Connect or the next Play, which connects the headphones again;
- takes the headphone buttons the dongle forwards: tap and the wear sensor = play / pause (toggled against the
  queue's state), swipes = next / previous; fast forward / rewind arrive only if the dongle sends them;
- sets the dongle's own volume to 0 dB (needs `amixer` from `alsa-utils`); the volume control (web remote slider,
  Apple TV quick menu) is mpv's own volume, so set the headphones to their maximum once. It is kept across restarts
  in `$ATV_AUDIO_STATE` / systemd's `StateDirectory`.

In an unprivileged container, give it the dongle's `/dev/snd` nodes and the hidraw node with read/write for the
container's `audio` group. Bind them from a host directory with `optional` mounts (a udev rule can create the
nodes there), so the container still starts when the dongle is unplugged; per-device passthrough entries that
must exist at start would stop it from booting.

### Pairing headphones

A dongle pairs by its own button; Linux sees a USB sound card. Built-in Bluetooth is paired once with
`bluetoothctl`. On a headless machine BlueZ only accepts pairing with `AlwaysPairable = true` in the
`[General]` section of `/etc/bluetooth/main.conf` (restart `bluetooth` after the change). Put the headphones
into pairing mode, then:

```text
bluetoothctl
power on
agent on
default-agent
scan on              # wait for the headphones' address
pair <address>
trust <address>      # do not skip: without it they do not reconnect by themselves
connect <address>
```

PipeWire then picks the best codec both sides support (aptX HD / LDAC need PipeWire's Bluetooth codec
plugins, e.g. Debian's `libspa-0.2-bluetooth`).

### On the Apple TV

- **Settings → Sound → Sound output = atv-audio** (selectable while the sink is connected; the choice is
  kept when it is not). The status line under it shows `atv-audio: not connected`, or
  `connected · <headphones> · <codec>`; a USB dongle shows `codec unknown` (it encodes by itself), except a
  BTD 700 with `ATV_AUDIO_DONGLE`.
- **Lip sync** (only with atv-audio): how much earlier the sink plays, −200 … +400 ms, default −100
  (Bluetooth adds a delay that cannot be measured). Set it by ear on a video with clear drum hits or speech:
  raise it if the sound comes late, lower it if it comes early. Changes apply live.
- With atv-audio chosen the Apple TV **never** plays its own sound (VLC stays muted), whatever the sink's
  state. Only Settings → Sound output changes it, and the change applies at once.
- Each item starts after a short pause while the sink buffers (at most 3 s; then it starts anyway).
- **Lost or changed:** if, during playback, the sink disconnects, its output goes away (headphones off), the
  server connection drops, or Sound output is changed, playback **pauses** with a notice that stays until ⏯ /
  Play or Back. Play then plays normally (without sound while the headphones are still off, no second
  notice); headphones back on while it plays = sound again by itself within a few seconds at the right place
  (while paused: from the next Play).
- **Volume** (click the centre button in the player: the quick menu's first row, only with atv-audio):
  ◀ ▶ −/+5 %, click = mute. It is the output's own volume (with Bluetooth through PipeWire: the headphones'
  volume, no extra digital attenuation); the web remote shows the same as a slider.

### Web remote

The sink plays only the audio track the TV plays. For videos the server serves a copy of that one track
(`<id>.main.mka` in the cache); while a copy is missing it serves the original file and builds the copy in
the background, for the current item and the next 3 in the queue while a sink is connected. **Extract
missing audio** (status bar, shown while copies are missing) builds every missing copy in the background,
with progress (n / total). The **atv-audio** badge on a music video means its copy exists. Copies are only
made by the button or for the queue, never after a library scan.

While the Apple TV plays its sound through atv-audio and the sink has its output, the player card shows a
**volume slider** and a mute button (the same volume as the TV's quick panel row; it follows changes made on
the headphones).

### atv-audio as a player of its own (web remote only)

The sink is also a player: the web remote's queue selector lists **atv-audio** (while the sink runs), and that
queue plays on the sink's output with no Apple TV or iPhone involved. Browse, play, queue, playlists and the volume
slider work as for the apps. The player card adds **EQ** (the apps' presets: Off, Momentum 4, Amplify and VLC's 18),
**Boost** (0 … +8 dB) and **Loudness** (raises quiet recordings, never turns anything down), saved on the server
for this queue only; the Apple TV keeps its own settings.

One output, the Apple TV wins: while the TV plays with Sound output = atv-audio, the atv-audio queue is paused and
shows "atv-audio is in use by the Apple TV"; a Play there is refused until the TV pauses or stops. It never resumes
by itself. If the headphones go away while it plays, it pauses with a notice; it stays paused when they come back.

**Headphone buttons**: the sink registers a media player with BlueZ (`server/sink/atv_buttons.py`, needs
`python3-dbus`), so Bluetooth headphones send Play / Pause / Next / Previous, and their wear sensor's pause (taken
off) and play (put on), to whichever queue holds the output: the Apple TV's while its sound goes to atv-audio,
otherwise atv-audio's own. A Play in the first 10 s after the headphones (re)connect is ignored, so switching them
on never starts playback. Holding fast forward / rewind seeks 10 s, and 10 s more for every 0.6 s held: BlueZ
sends those two keys only to its `<headphones> (AVRCP)` input device, so install `server/sink/70-atv-audio-avrcp.rules`
(lets group `audio` read just those devices; see the comments in the file). `ATV_AUDIO_BOX=0` turns the own queue off,
`ATV_AUDIO_BUTTONS=0` the buttons.

The EQ list includes **Custom**: **Edit** opens a preamp + 10-band editor (±12 dB, 1 dB steps, changes are heard at once,
"Start from a preset" copies a preset's curve). The curve is kept on the server with the queue's other sound settings.

## Settings (web remote)

The ⚙ button opens **Settings**:

- **Look**: per browser.
- **Library**: library folders with a display name per folder (add one with a folder picker, rename, remove; a
  removed folder's items leave the library and their playlist entries show as missing), a **skip list** (one folder or
  file name per line, `*` and `?` work, case does not matter; a path with `/` counts from the library folder), and the
  **rescan schedule** (off, every 1–12 hours, or daily at a time; default daily 04:15). The server schedules rescans
  itself: the old `atv-media-rescan.timer` is removed by `deploy.sh`.
  The folder picker only shows folders under `ATV_LIBRARY_BASES` (default: the parent folders of the first-start
  `ATV_ROOTS`). A network share has to be mounted on the host / container first.
- **atv-audio**: the sound output (PipeWire / Pulse sinks) and the headphones: find, pair (headphones in pairing mode),
  connect, disconnect, forget. Pairing is only accepted while a pairing started here runs (60 s). The player card also
  shows a Connect / Disconnect button for the headphones.
- **Server**: status (version, library, last and next scan, loudness, audio copies with **Delete audio copies**,
  playlists / hearts / plays), the server's name (shown in the page title and the Bonjour name), and an **optional
  admin password**. Without one (the default) anyone on the network can change the settings; with one, changes need an
  unlocked browser (cookie, 30 days). Playback, queues and playlists never ask for it. Lost password: delete the
  `admin` entry in `<ATV_DATA>/settings.json` and restart the server.
- **Backup**: one JSON file with playlists, Loved, the play history and atv-audio's sound settings. A restore replaces
  them; ids are matched through the stored file paths, so it also works on a rebuilt library.
