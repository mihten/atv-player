"""Sound settings of the "atv-audio" session (the box plays a queue by itself, the web remote sets its sound).

Same rules as the apps' Shared/SoundSettings.swift, so the box sounds like the Apple TV with the same choice:
total gain = preset gain + Boost + loudness gain, capped at +14 dB (equalizer +8 plus limiter makeup +6); loudness
lifts quiet tracks to -14 LUFS, only up, at most +8 dB, in half-dB steps; Equalizer Off + Boost 0 + Loudness Off =
unprocessed. The sink builds gain -> 10 octave-wide equalizer bands -> limiter from the message (sink/atv_audio.py
sound_chain), exactly as for the TV's own `sound` messages.

Presets (owner, 2026-11-13: one list in every player): Off, Momentum 4 (tag "a:momentum4", raw VLC preamp: 12 = 0 dB),
the four tone presets (tag "t:<id>", see TONE_PRESETS) and Custom. libVLC's 18 presets ("p:<index>") and Amplify were
deleted: a stored one reads as Off. `eq_last` = the last preset in use other than Off (default Momentum 4): the
output rule (Headphones = Equalizer on) turns the equalizer back on with it.
"""

FREQS = (31.25, 62.5, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)  # VLC's ISO 10-band table (equalizer.c)
VLC_PREAMP_OFFSET = 12.0
MAX_TOTAL = 14.0  # equalizer +8 + limiter makeup +6
LOUDNESS_TARGET = -14.0
MAX_LOUDNESS = 8.0
BOOST_RANGE = (0, 8)

_Z = 0.0  # VLC stores -1.11022e-15 for "0"
# Momentum 4 (2026-11-11, owner: replaces the old app curve here): 10-band fit of the parametric EqualizerAPO
# correction the owner prefers on his laptop (AutoEQ-style KB501x target, APO preamp -1 dB), fitted to the sink's
# own chain (octave-wide peaking filters at FREQS) with tools/atv-audio/apo_vs_sink_eq.py: rms 0.6 dB, max 2.1 dB
# from 25 Hz to 18 kHz. Raw preamp 9 = -3 dB. The apps send these same values when the sink plays their sound (0.18.45+);
# their own VLC equalizer uses a separate fit of the same APO curve (Shared/SoundSettings.swift).
APP_PRESETS = (
    ("momentum4", "Momentum 4", 9.0, (-7, -2, 0, 1, 2, 0, 7, 1, 3, 0)),
)
# Tone presets (2026-11-10, owner: the VLC presets below sounded dull on the TV Player: measured against Off with the
# same settings, Pop cut 1-16 kHz by 5-9 dB, Rock cut 125-500 Hz by 10-15 dB, Full bass cut everything above 500 Hz by 11-23 dB,
# Classical cut the highs by 7-11 dB; their raw preamps are -5..-7 dB and the sink's cascade of overlapping octave filters
# stacks the boosts). Rules of these four: whole dB (the web remote's Custom editor loads them exactly), NO band is cut, a
# makeup of 0 / -1 dB only for headroom; measured on six library tracks with tools/tv-player-test/eq-measure.py: the boosted
# range rises 3-5 dB, the rest stays within 1.5 dB of Off, loudness +0.2..+1.0 LU.
# The VLC presets were deleted on 2026-11-13 (owner).
# (id, name, makeup dB, 10 band gains at FREQS)
TONE_PRESETS = (
    ("bass", "Bass boost", -1.0, (5, 5, 4, 2, 1, 0, 0, 0, 0, 0)),
    ("vocal", "Vocal", 0.0, (0, 0, 0, 0, 1, 2, 3, 2, 1, 0)),
    ("bright", "Bright", 0.0, (0, 0, 0, 0, 0, 1, 2, 3, 4, 4)),
    ("vshape", "V-shape", -1.0, (4, 4, 3, 0, 0, 0, 0, 3, 4, 4)),
)

CUSTOM_RANGE = 12  # dB, preamp and every band (the TV's custom editor), 1 dB steps
FLAT = {"preamp": 0, "bands": [0] * len(FREQS)}
EQ_ON = "a:momentum4"  # the preset the output rule turns on when nothing else was used yet
DEFAULT = {"preset": "off", "boost": 0, "loudness": "raise", "custom": FLAT, "eq_last": EQ_ON}  # the apps' defaults


def choices():
    """[[value, label], ...] in the apps' display order. Custom = this queue's own curve, kept on the server."""
    return ([["off", "Off"]] + [[f"a:{i}", name] for i, name, _, _ in APP_PRESETS]
            + [[f"t:{i}", name] for i, name, _, _ in TONE_PRESETS] + [["custom", "Custom"]])


def _db(value):
    try:
        return max(-CUSTOM_RANGE, min(CUSTOM_RANGE, int(round(float(value)))))
    except (TypeError, ValueError, OverflowError):
        return 0


def clean_custom(custom):
    """{preamp, bands[10]} in whole dB within ±12; anything missing is flat."""
    if not isinstance(custom, dict):
        return dict(FLAT, bands=list(FLAT["bands"]))
    bands = custom.get("bands") if isinstance(custom.get("bands"), list) else []
    bands = [_db(b) for b in bands[:len(FREQS)]] + [0] * max(0, len(FREQS) - len(bands))
    return {"preamp": _db(custom.get("preamp", 0)), "bands": bands}


def valid_preset(value):
    return any(v == value for v, _ in choices())


def clean(settings):
    """Stored / requested settings -> {preset, boost, loudness} with every value in range."""
    s = dict(DEFAULT)
    s["custom"] = clean_custom(settings.get("custom") if isinstance(settings, dict) else None)
    if isinstance(settings, dict):
        if valid_preset(settings.get("preset")):
            s["preset"] = settings["preset"]
        try:
            s["boost"] = max(BOOST_RANGE[0], min(BOOST_RANGE[1], int(settings.get("boost", s["boost"]))))
        except (TypeError, ValueError, OverflowError):
            pass
        if settings.get("loudness") in ("off", "raise"):
            s["loudness"] = settings["loudness"]
        if settings.get("eq_last") != "off" and valid_preset(settings.get("eq_last")):
            s["eq_last"] = settings["eq_last"]
    if s["preset"] != "off":
        s["eq_last"] = s["preset"]
    return s


def for_output(settings, headphones):
    """The output rule (owner, 2026-11-13): HDMI = Equalizer Off and Boost 0, Headphones = Equalizer on (the last preset
    in use, else Momentum 4); Loudness stays as it is either way. Applied when the output is CHANGED, so a choice made
    afterwards on that output stands."""
    s = clean(settings)
    if headphones:
        if s["preset"] == "off":
            s["preset"] = s["eq_last"]
    else:
        s["preset"], s["boost"] = "off", 0
    return s


def curve(preset, settings=None):
    """(preset gain in dB relative to 'equalizer off', 10 band gains). Custom reads settings["custom"]."""
    if preset == "custom":
        c = clean_custom((settings or {}).get("custom"))
        return float(c["preamp"]), [float(b) for b in c["bands"]]
    if preset.startswith("a:"):
        for i, _, pre, bands in APP_PRESETS:
            if preset == f"a:{i}":
                return pre - VLC_PREAMP_OFFSET, list(bands)
    elif preset.startswith("t:"):
        for i, _, pre, bands in TONE_PRESETS:
            if preset == f"t:{i}":
                return float(pre), [float(b) for b in bands]
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
    pre, bands = curve(settings["preset"], settings)
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
    pre, _ = curve(settings["preset"], settings)
    lift = level_gain(settings, (item or {}).get("lufs"))
    total = min(MAX_TOTAL, pre + settings["boost"] + lift)
    return f"{name} {pre:+.0f}, boost +{settings['boost']}, loudness {lift:+.1f} = {total:+.1f} dB"
