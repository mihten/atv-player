#!/usr/bin/env python3
"""sink_connect_headphones (server 0.28.2): the TV Player chose Headphones while they are not ready, the server asks the
atv-audio sink to connect them. Imports the server with a temp library / data dir and calls the function directly with a
fake sink socket: which command goes out for a USB dongle, for BlueZ (connected headphones first, then the output's name,
then the first paired one), and that nothing goes out without a sink, during a blind test, with the output ready or
without paired headphones; and that a `sink_connect` from the TV Player's player reaches it (on_message), from no one else.
    python3 test_sink_connect.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = tempfile.mkdtemp()
os.environ.update(ATV_ROOTS='{"music": "%s"}' % os.path.join(TMP, "lib"), ATV_DATA=os.path.join(TMP, "data"),
                  ATV_CACHE=os.path.join(TMP, "cache"))
os.makedirs(os.path.join(TMP, "lib"), exist_ok=True)
sys.path.insert(0, str(HERE))
import server  # noqa: E402

failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


HP = {"address": "AA:AA", "name": "Headphones X", "paired": True, "connected": False, "audio": True}
PHONE = {"address": "BB:BB", "name": "Phone", "paired": True, "connected": True, "audio": False}
OTHER = {"address": "CC:CC", "name": "Other buds", "paired": True, "connected": False, "audio": True}


async def main():
    sent = []

    async def fake_send(ws, msg):
        sent.append((ws, msg))
    server.send = fake_send
    sink = object()

    def setup(devices=None, dongle=None, ready=False, name="Headphones X", connected=True):
        sent.clear()
        server.SINKS.pop("tv", None)
        if connected:
            server.SINKS["tv"] = sink
        server.SINK_INFO["tv"] = {"name": "atv-audio", "output": {"kind": "bluetooth", "name": name, "ready": ready}}
        server.SINK_AUDIO["tv"] = {"devices": devices or [], "dongle": dongle}
        server.ABTEST["running"] = False

    setup([PHONE, OTHER, HP])
    r = await server.sink_connect_headphones()
    check(r == "AA:AA" and sent == [(sink, {"type": "audio_cmd", "op": "connect", "address": "AA:AA"})],
          f"BlueZ: the paired headphones with the output's name ({sent})")
    setup([PHONE, OTHER, HP], name=None)
    r = await server.sink_connect_headphones()
    check(r == "CC:CC", "no output name: the first paired audio device (never the phone)")
    setup([PHONE, dict(OTHER, connected=True), HP])
    r = await server.sink_connect_headphones()
    check(r == "CC:CC", "headphones connected without A2DP: that one first (before the output's name)")
    setup([PHONE, OTHER, HP], dongle={"name": "BTD 700", "state": "idle"})
    r = await server.sink_connect_headphones()
    check(r == "dongle" and sent == [(sink, {"type": "audio_cmd", "op": "connect"})], "USB dongle: connect without address")
    for label, kw, extra in (("output ready", {"ready": True}, None), ("no sink", {"connected": False}, None),
                             ("no paired headphones", {"devices": [PHONE]}, None), ("blind test", {}, "abtest")):
        setup(**({"devices": [HP]} | kw))
        if extra == "abtest":
            server.ABTEST["running"] = True
        r = await server.sink_connect_headphones()
        check(r is None and sent == [], f"{label}: nothing sent")
    server.ABTEST["running"] = False

    # the message path: only the TV Player's player
    setup([HP])
    await server.on_message(server.SESSIONS["mtv"], {"type": "sink_connect"}, "player")
    check(len(sent) == 1 and sent[0][1]["op"] == "connect", "on_message: sink_connect from the TV Player's player")
    for kind, role in (("mtv", "remote"), ("tv", "player"), ("box", "player")):
        setup([HP])
        await server.on_message(server.SESSIONS[kind], {"type": "sink_connect"}, role)
        check(sent == [], f"on_message: ignored from {kind} {role}")


if __name__ == "__main__":
    asyncio.run(main())
    print("ALL PASSED" if not failed else f"FAILED {failed}")
    sys.exit(1 if failed else 0)
