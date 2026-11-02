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
  `connected · <headphones> · <codec>`; a USB dongle shows `codec unknown` (it encodes by itself).
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
- **Volume** (click the centre button in the player: the quick panel's second row, only with atv-audio):
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
