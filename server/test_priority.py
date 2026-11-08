#!/usr/bin/env python3
"""Every non-playback ffmpeg / ffprobe run of the media server starts with scanner.LOW_PRIORITY (nice 19 + idle I/O).
Calls each function with subprocess.run replaced by a recorder (no real ffmpeg, no server, no network).
    python3 test_priority.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
tmp = Path(tempfile.mkdtemp(prefix="atv-prio-"))
os.environ.update(ATV_ROOTS=json.dumps({"music": str(tmp)}), ATV_DATA=str(tmp / "data"), ATV_CACHE=str(tmp / "cache"))
sys.path.insert(0, str(HERE))
import scanner  # noqa: E402
import server  # noqa: E402

failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


calls = []


class Done:
    returncode, stdout, stderr = 1, "{}", ""


def fake_run(cmd, *a, **kw):
    calls.append(list(cmd))
    return Done()


subprocess.run = fake_run          # scanner and server both call subprocess.run through the module
LOW = scanner.LOW_PRIORITY
check(LOW[:3] == ["nice", "-n", "19"], f"the shared prefix is nice 19 ({LOW})")

(tmp / "a.mkv").write_bytes(b"x")
it = {"id": 1, "root": "music", "relpath": "a.mkv", "kind": "video", "audio_ord": 0, "duration": 100, "folder": ""}
path = server.item_path(it)

jobs = {
    "scanner.video_pictures (ffprobe)": lambda: scanner.video_pictures(path),
    "scanner.video_picture_bytes, picture (ffmpeg)": lambda: scanner.video_picture_bytes(path, "picture", 1),
    "scanner.video_picture_bytes, attachment (ffmpeg)": lambda: scanner.video_picture_bytes(path, "attachment", 1),
    "scanner.probe_video (ffprobe)": lambda: scanner.probe_video(path),
    "server.make_main_copy (ffmpeg)": lambda: server.make_main_copy(it, tmp / "main.mka"),
    "server.make_audio_only (ffmpeg)": lambda: server.make_audio_only(it, tmp / "all.mka"),
    "server.video_keyframes (ffprobe)": lambda: server.video_keyframes(path),
    "server.probe_audio (ffprobe)": lambda: server.probe_audio(it),
    "server.video_art frame grab (ffmpeg)": lambda: server.video_art(path),
}
for name, fn in jobs.items():
    calls.clear()
    try:
        fn()
    except Exception:
        pass                       # the fake ffmpeg produces nothing; only the command line matters
    ff = [c for c in calls if any(x in ("ffmpeg", "ffprobe") for x in c)]
    check(bool(ff), f"{name}: runs ffmpeg / ffprobe")
    check(all(c[:len(LOW)] == LOW for c in ff), f"{name}: starts with the low-priority prefix ({ff[0][:6] if ff else '-'})")

# nothing in the sources builds an ffmpeg / ffprobe command without the prefix (a new call must be added above)
src = (HERE / "server.py").read_text() + (HERE / "scanner.py").read_text()
raw = [m.group(0) for m in re.finditer(r'(?<!\+ )\[\s*"(?:ffmpeg|ffprobe)"', src)]   # "PREFIX + [..." is fine
check(not raw, f"no ffmpeg / ffprobe command list without the prefix in server.py / scanner.py ({raw[:3]})")

print("FAILED" if failed else "ALL PASSED", failed)
sys.exit(1 if failed else 0)
