"""Sound settings of the "atv-audio" session (the box plays a queue by itself, the web remote sets its sound).

Same rules as the apps' Shared/SoundSettings.swift, so the box sounds like the Apple TV with the same choice:
total gain = preset gain + Boost + loudness gain, capped at +14 dB (equalizer +8 plus limiter makeup +6); loudness
lifts quiet tracks to -14 LUFS, only up, at most +8 dB, in half-dB steps; Equalizer Off + Boost 0 + Loudness Off =
unprocessed. The sink builds gain -> 10 octave-wide equalizer bands -> limiter from the message (sink/atv_audio.py
sound_chain), exactly as for the TV's own `sound` messages.

Presets: the apps' own presets (tag "a:<id>") and libVLC 3.0.x's 18 (tag "p:<index>", modules/audio_filter/
equalizer_presets.h, same order and values as VLCAudioEqualizer.presets). Preamps are VLC's raw values: 12 = 0 dB.
"""

FREQS = (31.25, 62.5, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)  # VLC's ISO 10-band table (equalizer.c)
VLC_PREAMP_OFFSET = 12.0
MAX_TOTAL = 14.0  # equalizer +8 + limiter makeup +6
LOUDNESS_TARGET = -14.0
MAX_LOUDNESS = 8.0
BOOST_RANGE = (0, 8)

_Z = 0.0  # VLC stores -1.11022e-15 for "0"
APP_PRESETS = (
    ("momentum4", "Momentum 4", 16.0, (-1, -1, -0.5, 0.5, 1, 2, 3.5, 2, -0.5, -1.5)),
    ("amplify", "Amplify", 17.0, (0, 0, 0, 0, 0, 0, 0, 0, 0, 0)),
)
VLC_PRESETS = (
    ("Flat", 12.0, (0, 0, 0, 0, 0, 0, 0, 0, 0, 0)),
    ("Classical", 12.0, (_Z, _Z, _Z, _Z, _Z, _Z, -7.2, -7.2, -7.2, -9.6)),
    ("Club", 6.0, (_Z, _Z, 8.0, 5.6, 5.6, 5.6, 3.2, _Z, _Z, _Z)),
    ("Dance", 5.0, (9.6, 7.2, 2.4, _Z, _Z, -5.6, -7.2, -7.2, _Z, _Z)),
    ("Full bass", 5.0, (-8.0, 9.6, 9.6, 5.6, 1.6, -4.0, -8.0, -10.4, -11.2, -11.2)),
    ("Full bass and treble", 4.0, (7.2, 5.6, _Z, -7.2, -4.8, 1.6, 8.0, 11.2, 12.0, 12.0)),
    ("Full treble", 3.0, (-9.6, -9.6, -9.6, -4.0, 2.4, 11.2, 16.0, 16.0, 16.0, 16.8)),
    ("Headphones", 4.0, (4.8, 11.2, 5.6, -3.2, -2.4, 1.6, 4.8, 9.6, 12.8, 14.4)),
    ("Large Hall", 5.0, (10.4, 10.4, 5.6, 5.6, _Z, -4.8, -4.8, -4.8, _Z, _Z)),
    ("Live", 7.0, (-4.8, _Z, 4.0, 5.6, 5.6, 5.6, 4.0, 2.4, 2.4, 2.4)),
    ("Party", 6.0, (7.2, 7.2, _Z, _Z, _Z, _Z, _Z, _Z, 7.2, 7.2)),
    ("Pop", 6.0, (-1.6, 4.8, 7.2, 8.0, 5.6, _Z, -2.4, -2.4, -1.6, -1.6)),
    ("Reggae", 8.0, (_Z, _Z, _Z, -5.6, _Z, 6.4, 6.4, _Z, _Z, _Z)),
    ("Rock", 5.0, (8.0, 4.8, -5.6, -8.0, -3.2, 4.0, 8.8, 11.2, 11.2, 11.2)),
    ("Ska", 6.0, (-2.4, -4.8, -4.0, _Z, 4.0, 5.6, 8.8, 9.6, 11.2, 9.6)),
    ("Soft", 5.0, (4.8, 1.6, _Z, -2.4, _Z, 4.0, 8.0, 9.6, 11.2, 12.0)),
    ("Soft rock", 7.0, (4.0, 4.0, 2.4, _Z, -4.0, -5.6, -3.2, _Z, 2.4, 8.8)),
    ("Techno", 5.0, (8.0, 5.6, _Z, -5.6, -4.8, _Z, 8.0, 9.6, 9.6, 8.8)),
)

DEFAULT = {"preset": "off", "boost": 0, "loudness": "raise"}  # the apps' defaults


def choices():
    """[[value, label], ...] in the apps' display order (Custom is TV-only: its curve lives on the TV)."""
    return ([["off", "Off"]] + [[f"a:{i}", name] for i, name, _, _ in APP_PRESETS]
            + [[f"p:{n}", p[0]] for n, p in enumerate(VLC_PRESETS)])


def valid_preset(value):
    return any(v == value for v, _ in choices())


def clean(settings):
    """Stored / requested settings -> {preset, boost, loudness} with every value in range."""
    s = dict(DEFAULT)
    if isinstance(settings, dict):
        if valid_preset(settings.get("preset")):
            s["preset"] = settings["preset"]
        try:
            s["boost"] = max(BOOST_RANGE[0], min(BOOST_RANGE[1], int(settings.get("boost", s["boost"]))))
        except (TypeError, ValueError, OverflowError):
            pass
        if settings.get("loudness") in ("off", "raise"):
            s["loudness"] = settings["loudness"]
    return s


def curve(preset):
    """(preset gain in dB relative to 'equalizer off', 10 band gains)."""
    if preset.startswith("a:"):
        for i, _, pre, bands in APP_PRESETS:
            if preset == f"a:{i}":
                return pre - VLC_PREAMP_OFFSET, list(bands)
    elif preset.startswith("p:"):
        try:
            _, pre, bands = VLC_PRESETS[int(preset[2:])]
            return pre - VLC_PREAMP_OFFSET, list(bands)
        except (ValueError, IndexError):
            pass
    return 0.0, [0.0] * len(FREQS)


def processed(settings):
    return settings["preset"] != "off" or settings["boost"] > 0 or settings["loudness"] != "off"


def level_gain(settings, lufs):
    """Loudness lift for a track (SoundSettings.levelGain)."""
    if lufs is None or settings["loudness"] == "off":
        return 0.0
    gain = min(MAX_LOUDNESS, max(0.0, LOUDNESS_TARGET - float(lufs)))
    stepped = round(gain * 2) / 2
    return 0.0 if stepped == 0 else stepped


def _r(value):
    v = round(float(value) * 100) / 100
    return 0.0 if v == 0 else v


def message(settings, item):
    """The `sound` message for the sink (same fields as the TV sends): {processed, gain, freqs, bands}."""
    settings = clean(settings)
    on = processed(settings)
    pre, bands = curve(settings["preset"])
    lift = level_gain(settings, (item or {}).get("lufs"))
    total = min(MAX_TOTAL, pre + settings["boost"] + lift) if on else 0.0
    return {"type": "sound", "processed": on, "gain": _r(total), "freqs": [_r(f) for f in FREQS],
            "bands": [_r(b) for b in bands] if on else [0.0] * len(FREQS)}


def label(settings, item):
    """e.g. 'Momentum 4, boost +2, loudness +3.5 = +9.5 dB' for the log / the web remote."""
    settings = clean(settings)
    if not processed(settings):
        return "unprocessed"
    name = dict((v, n) for v, n in choices()).get(settings["preset"], "Off")
    pre, _ = curve(settings["preset"])
    lift = level_gain(settings, (item or {}).get("lufs"))
    total = min(MAX_TOTAL, pre + settings["boost"] + lift)
    return f"{name} {pre:+.0f}, boost +{settings['boost']}, loudness {lift:+.1f} = {total:+.1f} dB"
