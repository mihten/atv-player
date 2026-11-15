"""Badges of a library item: the short facts every app and the web remote show (one list, made here, so the players
cannot disagree). Pure functions of the item's stored fields.

  song          audio, channels                       FLAC 24/96            MP3 320           FLAC 24/96 · 5.1
  music video   codec, picture, HDR, fps, audio, ch.  HEVC · 4K · HLG · 60fps · FLAC 24/48 · 5.1

`badges` is the full list; `abadges` is the audio part only (audio-only streaming of a video away from home).
The year, "Lossless" and the version chip are not badges."""

# ffmpeg / mutagen codec names -> display label
LABELS = {
    "av1": "AV1", "vp9": "VP9", "vp8": "VP8", "h264": "H.264", "avc1": "H.264", "hevc": "HEVC", "h265": "HEVC",
    "mpeg4": "MPEG-4", "mpeg2video": "MPEG-2", "vc1": "VC-1", "wmv3": "WMV",
    "opus": "Opus", "flac": "FLAC", "aac": "AAC", "mp3": "MP3", "vorbis": "Vorbis", "alac": "ALAC",
    "ac3": "AC3", "eac3": "E-AC3", "dts": "DTS", "wavpack": "WavPack", "wv": "WavPack", "ape": "APE",
    "wma": "WMA", "wmav2": "WMA", "truehd": "TrueHD", "mp2": "MP2",
}
# the usual lossy bitrates (kbit/s); an average within 6 % of one is shown as that one
STEPS = (64, 96, 112, 128, 160, 192, 224, 256, 320)


def codec_label(name):
    n = (name or "").strip().lower()
    if not n:
        return None
    if n.startswith("pcm_") or n in ("wav", "aiff", "aif"):
        return "PCM"
    if n.startswith("mp4a"):
        return "AAC"  # MP4 audio as mutagen reports it ("mp4a.40.2")
    return LABELS.get(n, n.upper())


def kbps(bitrate):
    """Lossy bitrate (bit/s) -> the number shown, or None."""
    try:
        k = float(bitrate) / 1000
    except (TypeError, ValueError):
        return None
    if k < 32:
        return None
    near = min(STEPS, key=lambda s: abs(s - k))
    return near if abs(near - k) <= 0.06 * near else int(round(k / 10.0) * 10)


def audio_badge(codec, lossless, bits, rate, bitrate, with_bitrate=True):
    label = codec_label(codec)
    if not label:
        return None
    if lossless:
        if bits and rate:  # 16/44 (44.1 kHz), 24/96, 24/192
            return f"{label} {int(bits)}/{int(int(rate) / 1000)}"
        return label
    k = kbps(bitrate) if with_bitrate else None
    return f"{label} {k}" if k else label


def picture_badge(width, height):
    w, h = width or 0, height or 0
    if not w and not h:
        return None
    if w >= 3000 or h >= 2000:
        return "4K"
    if w >= 2400 or h >= 1300:
        return "1440p"
    if w >= 1800 or h >= 1000:
        return "1080p"
    if w >= 1200 or h >= 700:
        return "720p"
    return "SD"


def channel_badge(channels):
    return {6: "5.1", 8: "7.1"}.get(channels or 0)


def item_badges(it):
    """(badges, abadges) of a library row (dict with the scanner's columns)."""
    video = it.get("kind") == "video"
    acodec = it.get("acodec")
    if acodec is None and not video:
        acodec = it.get("codec")
    audio = []
    if acodec:  # "" / None = a video without sound
        a = audio_badge(acodec, it.get("lossless"), it.get("bdepth"), it.get("srate"), it.get("bitrate"),
                        with_bitrate=not video)  # a video's file bitrate is not its audio track's
        if a:
            audio.append(a)
        ch = channel_badge(it.get("channels"))
        if ch:
            audio.append(ch)
    if not video:
        return audio, audio
    head = []
    vc = codec_label(it.get("codec"))
    if vc:
        head.append(vc)
    pic = picture_badge(it.get("width"), it.get("height"))
    if pic:
        head.append(pic)
    hdr = {"pq": "HDR10", "hlg": "HLG", "dv": "DV"}.get(it.get("dyn") or "")
    if hdr:
        head.append(hdr)
    fps = it.get("fps") or 0
    if fps >= 49:
        head.append("50fps" if fps < 55 else "60fps")
    return head + audio, audio
