"""Tests for raising mpv's audio thread to SCHED_FIFO (fake /proc tree, fake scheduler calls)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mtv_player as mp  # noqa: E402

FAILS = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def fake_proc(root, pid, threads):
    for tid, comm in threads.items():
        d = os.path.join(root, str(pid), "task", str(tid))
        os.makedirs(d)
        with open(os.path.join(d, "comm"), "w") as f:
            f.write(comm + "\n")


def main():
    with tempfile.TemporaryDirectory() as root:
        fake_proc(root, 100, {100: "mpv", 101: "vo", 102: "ao", 103: "demux", 104: "ao"})
        policy = {100: os.SCHED_OTHER, 101: os.SCHED_OTHER, 102: os.SCHED_OTHER, 103: os.SCHED_OTHER,
                  104: os.SCHED_FIFO}
        calls = []

        def set_(tid, pol, param):
            calls.append((tid, pol, param.sched_priority))
            policy[tid] = pol

        raised, err = mp.raise_audio_threads(100, 70, proc_root=root, get=policy.get, set_=set_)
        check("only the normal 'ao' thread is raised", raised == [102])
        check("raised with SCHED_FIFO and the given priority", calls == [(102, os.SCHED_FIFO, 70)])
        check("no error", err is None)
        check("video / demux / main threads untouched", all(policy[t] == os.SCHED_OTHER for t in (100, 101, 103)))

        calls.clear()
        raised, err = mp.raise_audio_threads(100, 70, proc_root=root, get=policy.get, set_=set_)
        check("second scan changes nothing", raised == [] and calls == [])

        def denied(tid, pol, param):
            raise PermissionError(1, "Operation not permitted")

        policy[102] = os.SCHED_OTHER
        raised, err = mp.raise_audio_threads(100, 70, proc_root=root, get=policy.get, set_=denied)
        check("missing rtprio limit: error returned, nothing claimed", raised == [] and isinstance(err, PermissionError))

        def gone(tid):
            raise ProcessLookupError(3, "No such process")

        raised, err = mp.raise_audio_threads(100, 70, proc_root=root, get=gone, set_=set_)
        check("vanished thread is skipped quietly", raised == [] and err is None)

        raised, err = mp.raise_audio_threads(999, 70, proc_root=root, get=policy.get, set_=set_)
        check("vanished process: nothing, no error", raised == [] and err is None)

    cfg = mp.Config({"MTV_SERVER": "http://x"})
    check("default priority 70", cfg.audio_rtprio == 70)
    check("0 turns it off", mp.Config({"MTV_AUDIO_RTPRIO": "0"}).audio_rtprio == 0)
    check("clamped to 99", mp.Config({"MTV_AUDIO_RTPRIO": "150"}).audio_rtprio == 99)
    check("garbage = default", mp.Config({"MTV_AUDIO_RTPRIO": "x"}).audio_rtprio == 70)

    print("FAILED: " + ", ".join(FAILS) if FAILS else "all ok")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
