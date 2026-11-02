#!/usr/bin/env python3
"""follow_playlist() with shuffle: a playlist edit rebuilds the queue with each entry once, also when the playlist held
an entry twice and both copies were heard before the edit (the case that made test_follow.py fail ~1 run in 8).
Deterministic: calls follow_playlist() on a hand-built session; no server process, no network, temp dirs only.
    python3 test_follow_dedup.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="atv-follow-dedup-"))
os.environ.update(ATV_ROOTS=json.dumps({"music": str(TMP / "lib")}), ATV_DATA=str(TMP / "data"),
                  ATV_CACHE=str(TMP / "cache"))
sys.path.insert(0, str(HERE))
import server  # noqa: E402

failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def session(queue, index, playlist):
    server.LIB.clear()
    for i in range(1, 10):
        server.LIB[i] = {"id": i, "kind": "audio", "title": f"t{i}", "hidden": False}
    server.PLAYLISTS.clear()
    server.PLAYLISTS[1] = {"id": 1, "name": "p", "items": list(playlist)}
    s = server.Session("tv")
    s.shuffle = True
    s.queue, s.index = list(queue), index
    s.source, s.source_items = 1, [1, 2, 3, 4, 5, 6, 1, 2, 3]
    return s


def once_each(q, want):
    return sorted(q) == sorted(want) and len(q) == len(set(q))


# The captured failure: playlist 1 2 3 4 5 6 1 2 3, shuffled; heard 3, 6, now the second 3; edit -> 3 4 5 6 7 8.
for run in range(50):  # the rest is shuffled at random: the result must hold for every order
    s = session([3, 6, 3, 5, 1, 2, 4, 1, 2], 2, [3, 4, 5, 6, 7, 8])
    changed = server.follow_playlist(s, 1)
    q = s.queue
    if run == 0:
        check(changed, "edit is followed")
        check(q[s.index] == 3, f"current 3 kept at index {s.index}: {q}")
        check(q[:s.index] == [6], f"played part = [6] (the current 3 is not behind itself): {q[:s.index]}")
    if not once_each(q, [3, 4, 5, 6, 7, 8]):
        check(False, f"run {run}: every playlist entry exactly once: {q}")
        break
else:
    check(True, "50 shuffles: every playlist entry exactly once")

# A played entry heard twice (not the current one) stays behind once.
s = session([2, 5, 2, 4, 1, 3], 3, [1, 2, 3, 4, 5, 6])
server.follow_playlist(s, 1)
check(s.queue[s.index] == 4 and s.queue[:s.index] == [2, 5], f"played twice: kept behind once, first-heard order: {s.queue}")
check(once_each(s.queue, [1, 2, 3, 4, 5, 6]), f"played twice: every entry once: {s.queue}")

# The current item removed from the playlist keeps playing; the rest is the playlist, once each.
s = session([3, 6, 3, 5, 1], 2, [4, 5, 6, 7])
server.follow_playlist(s, 1)
check(s.queue[s.index] == 3 and s.queue[:s.index] == [6], f"current removed: still playing, played [6] behind: {s.queue}")
check(once_each(s.queue, [3, 4, 5, 6, 7]), f"current removed: playlist entries once + current: {s.queue}")

# The playlist itself holds an entry twice after the edit: both copies stay (a playlist may repeat an entry).
s = session([1, 2, 3], 1, [1, 2, 3, 2])
server.follow_playlist(s, 1)
check(s.queue[s.index] == 2 and s.queue[:s.index] == [1], f"repeat in the new playlist: current 2, played [1]: {s.queue}")
check(sorted(s.queue) == [1, 2, 2, 3], f"repeat in the new playlist: its second copy stays: {s.queue}")

print("ALL PASS" if not failed else f"{failed} FAILED")
sys.exit(1 if failed else 0)
