#!/usr/bin/env python3
"""Fit VLC 3's 10-band equalizer to an EqualizerAPO GraphicEQ curve.

Reproduces modules/audio_filter/equalizer.c (VLC 3.0.x): per band a band-pass biquad
y = a(x - x[n-2]) + g*y[n-1] - b*y[n-2]; output = gamp * (0.25*x + sum(amp_j * y_j)),
amp_j = 0.25*(10^(dB_j/20) - 1), gamp = 10^(preamp/20). The bands overlap, so a curve copied
band by band boosts too much; this solves for the band gains whose combined response matches.

usage: eq_fit.py "<GraphicEQ: f g; f g; ...>" <apo_preamp_dB>
"""
import cmath
import math
import sys

import os

import numpy as np
from scipy.optimize import least_squares

RATE = 48000.0
# libVLC media players create "equalizer-vlcfreqs" as a plain bool (= false, lib/media_player.c:725), so
# the equalizer filter in the app uses the ISO table (modules/audio_filter/equalizer_presets.h), the same
# frequencies VLCAudioEqualizer().bands reports. The VLC table (60 Hz ... 16 kHz) only applies to the
# desktop VLC GUI. EQ_TABLE=vlc reproduces the old (wrong) assumption for comparison.
ISO_FREQS = [31.25, 62.5, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
VLC_FREQS = [60, 170, 310, 600, 1000, 3000, 6000, 12000, 14000, 16000]
FREQS = VLC_FREQS if os.environ.get("EQ_TABLE") == "vlc" else ISO_FREQS
IN = 0.25


def coeffs(f, rate=RATE, octave=1.0):
    of = 2 ** (0.5 * octave)
    of1, of2 = 0.5 * (of + 1), 0.5 * (of - 1)
    t1 = 2 * math.pi * f / rate
    t2 = t1 / of
    s = math.sin(t2)
    sp = math.sin(t2 * of1) * math.sin(t2 * of2)
    den = 0.5 * s + sp
    return sp / den, (0.5 * s - sp) / den, s * math.cos(t1) / den  # alpha, beta, gamma


BANDS = [coeffs(f) for f in FREQS]


def band_h(f, a, b, g):
    """Transfer function of y[n] = a(x[n]-x[n-2]) + g y[n-1] - b y[n-2] at frequency f."""
    z1 = cmath.exp(-1j * 2 * math.pi * f / RATE)
    return a * (1 - z1 * z1) / (1 - g * z1 + b * z1 * z1)


def response_db(f, band_db, preamp_db):
    h = IN + sum(IN * (10 ** (d / 20) - 1) * band_h(f, *c) for d, c in zip(band_db, BANDS))
    return 20 * math.log10(abs(h)) + preamp_db


def parse_graphic(text):
    text = text.split(":", 1)[-1]
    pts = []
    for part in text.split(";"):
        part = part.strip()
        if part:
            f, g = part.split()
            pts.append((float(f), float(g)))
    return pts


def main():
    pts = parse_graphic(sys.argv[1])
    apo_pre = float(sys.argv[2])
    # target = what EqualizerAPO plays (graphic gain + its preamp), interpolated on a log grid
    fa = np.array([p[0] for p in pts])
    ga = np.array([p[1] for p in pts]) + apo_pre
    grid = np.geomspace(25, 18000, 240)
    target = np.interp(np.log10(grid), np.log10(fa), ga)

    def resid(x):
        bands, pre = x[:10], x[10]
        return np.array([response_db(f, bands, pre) for f in grid]) - target

    x0 = np.zeros(11)
    x0[10] = 12.0
    fit = least_squares(resid, x0, bounds=([-20] * 10 + [-20], [20] * 10 + [20]))
    bands, pre = fit.x[:10], fit.x[10]
    err = resid(fit.x)
    print("fitted VLC bands (dB):", ", ".join(f"{b:.1f}" for b in bands))
    print(f"fitted VLC preamp: {pre:.2f} dB  (app-relative: {pre - 12:.2f} dB)")
    print(f"fit error: max {np.max(np.abs(err)):.2f} dB, rms {np.sqrt(np.mean(err ** 2)):.2f} dB")
    # rounded to 0.5 dB as stored in the app
    rb = [round(b * 2) / 2 for b in bands]
    rp = round(pre * 2) / 2
    err_r = np.array([response_db(f, rb, rp) for f in grid]) - target
    print("rounded bands:", rb, "preamp", rp, f"-> max {np.max(np.abs(err_r)):.2f} dB, rms {np.sqrt(np.mean(err_r ** 2)):.2f} dB")
    naive = [float(np.interp(math.log10(f), np.log10(fa), ga - apo_pre)) for f in FREQS]
    err_n = np.array([response_db(f, naive, 12 + apo_pre) for f in grid]) - target
    print("naive copy (APO gain at each VLC freq):", [round(v, 1) for v in naive],
          f"-> max {np.max(np.abs(err_n)):.2f} dB")
    print("check points  f: target / fitted")
    if len(sys.argv) > 3:  # existing preset "b0,...,b9" raw_preamp: what it really does on this table
        cur = [float(v) for v in sys.argv[3].split(",")]
        cur_pre = float(sys.argv[4])
        err_c = np.array([response_db(f, cur, cur_pre) for f in grid]) - target
        print(f"given preset on this table: max err {np.max(np.abs(err_c)):.2f} dB, rms {np.sqrt(np.mean(err_c ** 2)):.2f} dB,",
              f"peak level {max(response_db(f, cur, cur_pre) for f in grid):+.2f} dB")
        for f in (30, 60, 125, 250, 500, 1000, 2000, 3150, 5000, 8000, 12500, 16000):
            t = float(np.interp(math.log10(f), np.log10(fa), ga))
            print(f"  {f:>6} Hz: target {t:+.2f} / given {response_db(f, cur, cur_pre):+.2f}")
    for f in (30, 60, 100, 250, 500, 1000, 2000, 3150, 5000, 8000, 12500, 16000):
        t = float(np.interp(math.log10(f), np.log10(fa), ga))
        print(f"  {f:>6} Hz: {t:+.2f} / {response_db(f, rb, rp):+.2f}")
    print("peak level of fitted curve (dB):", f"{max(response_db(f, rb, rp) for f in grid):+.2f}")


main()
