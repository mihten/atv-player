# atv-audio sink (atv_audio.py): plays the Apple TV's sound in sync, reading it over HTTP from the media server.
# Built by compose.yaml (profile "audio"); output = the host's PipeWire/Pulse socket or /dev/snd (USB dongle).
FROM debian:bookworm-slim

# mpv plays; pactl reports the Pulse/PipeWire output (Bluetooth codec); aplay -L lists ALSA devices.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-aiohttp mpv pulseaudio-utils alsa-utils ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --system --uid 10002 --user-group --groups audio --home-dir /tmp --no-create-home atvaudio

WORKDIR /opt/atv-audio
COPY atv_audio.py ./

# HOME=/tmp: mpv/pactl also run under the host user's uid (Pulse socket variant), which has no home in the image.
ENV HOME=/tmp \
    PYTHONUNBUFFERED=1

USER atvaudio
CMD ["python3", "/opt/atv-audio/atv_audio.py"]
