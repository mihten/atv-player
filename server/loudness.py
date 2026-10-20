"""Per-file loudness (EBU R128 integrated loudness + true peak), measured once with ffmpeg and stored
in the library DB (items.lufs / items.tpeak, in LUFS and dBFS). The apps use it to raise quiet tracks
(or level all tracks) with a gain that cannot clip.

Runs as a low-priority background job after the scan; a changed file is measured again because the
scanner clears its values. Files that cannot be measured get loud_fail = 1 (not retried until changed).
"""
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time

log = logging.getLogger("atv-media")

MAX_SECONDS = 900  # measure at most the first 15 minutes (long concert videos)
SUMMARY = re.compile(r"Summary:(.*)", re.S)
LUFS = re.compile(r"\bI:\s+(-?\d+(?:\.\d+)?)\s+LUFS")
PEAK = re.compile(r"True peak:\s+Peak:\s+(-?\d+(?:\.\d+)?)\s+dBFS")
STOP = threading.Event()
LOW_PRIORITY = ["nice", "-n", "19"] + (["ionice", "-c", "3"] if shutil.which("ionice") else [])


def measure(path, audio_ord=None):
    """-> (lufs, true_peak_dbfs) or None when ffmpeg cannot read the audio."""
    cmd = [*LOW_PRIORITY, "ffmpeg", "-nostdin", "-nostats", "-hide_banner", "-v", "info",
           "-t", str(MAX_SECONDS), "-i", path, "-map", f"0:a:{audio_ord or 0}",
           "-af", "ebur128=peak=true", "-f", "null", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=600).stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("loudness: %s: %s", path, exc)
        return None
    summary = SUMMARY.search(out)
    if not summary:
        return None
    lufs, peak = LUFS.search(summary.group(1)), PEAK.search(summary.group(1))
    if not lufs or not peak:
        return None
    value = float(lufs.group(1))
    if value < -69:  # silence / "-inf"
        return None
    return round(value, 1), round(float(peak.group(1)), 1)


def pending(con):
    return con.execute(
        "SELECT id, root, relpath, audio_ord FROM items WHERE lufs IS NULL AND loud_fail = 0 "
        "ORDER BY hidden, kind DESC, id").fetchall()  # visible first, videos first: they have the big level differences


def counts(db_path):
    con = sqlite3.connect(db_path)
    try:
        done = con.execute("SELECT COUNT(*) FROM items WHERE lufs IS NOT NULL").fetchone()[0]
        todo = con.execute("SELECT COUNT(*) FROM items WHERE lufs IS NULL AND loud_fail = 0").fetchone()[0]
        failed = con.execute("SELECT COUNT(*) FROM items WHERE loud_fail = 1").fetchone()[0]
    finally:
        con.close()
    return {"done": done, "pending": todo, "failed": failed}


def run(db_path, roots, on_result=None, limit=None):
    """Measure every item without a value (oldest ids first). on_result(id, lufs, tpeak) is called
    for each success so the server can update its in-memory library. Returns the number measured."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    done = 0
    started = time.time()
    try:
        for row in pending(con):
            if STOP.is_set() or (limit is not None and done >= limit):
                break
            base = roots.get(row["root"])
            if not base:
                continue
            result = measure(os.path.join(base, row["relpath"]), row["audio_ord"])
            if result is None:
                con.execute("UPDATE items SET loud_fail = 1 WHERE id = ?", (row["id"],))
            else:
                con.execute("UPDATE items SET lufs = ?, tpeak = ? WHERE id = ?", (*result, row["id"]))
                if on_result:
                    on_result(row["id"], *result)
            con.commit()
            done += 1
            if done % 200 == 0:
                log.info("loudness: %d measured so far (%.0f s)", done, time.time() - started)
    finally:
        con.close()
    if done:
        log.info("loudness: %d files measured in %.0f s", done, time.time() - started)
    return done
