#!/usr/bin/env python3
"""Codec switch aptX HD <-> aptX Adaptive (server 0.28.3): POST /api/atv-audio {op: set_codec, output: <codec>}.
Imports the server with a temp library / data dir and calls the handler with a fake request and a fake sink socket:
only the two codec ids are accepted, the command goes to the sink, and it is REFUSED (409, nothing sent) while sound
plays through the headphones (the switch cuts the sound for a few seconds): the Web Player playing or wanting to play,
or the Apple TV / TV Player playing with its sound on atv-audio.
    python3 test_codec_switch.py
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
from aiohttp import web  # noqa: E402

failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


class Req:
    def __init__(self, body):
        self._b = body
        self.headers, self.cookies, self.query = {}, {}, {}

    async def json(self):
        return self._b


async def call(body):
    try:
        r = await server.api_atv_audio(Req(body))
        return r.status
    except web.HTTPException as e:
        return e.status


async def main():
    sent = []

    async def fake_send(ws, msg):
        sent.append(msg)
    server.send = fake_send
    server.admin_ok = lambda request: True
    server.SINKS["tv"] = object()
    server.ABTEST["running"] = False
    box, tv = server.SESSIONS["box"], server.SESSIONS["tv"]
    box.playing = box.want_play = tv.playing = tv.want_play = False

    check(await call({"op": "set_codec", "output": "aptx_adaptive"}) == 200
          and sent == [{"type": "audio_cmd", "op": "set_codec", "output": "aptx_adaptive"}], f"aptX Adaptive relayed ({sent})")
    sent.clear()
    check(await call({"op": "set_codec", "output": "aptx_hd"}) == 200 and sent[0]["output"] == "aptx_hd", "aptX HD relayed")
    for bad in ("sbc", "aptx_adaptive; rm -rf /", "", None, 5):
        sent.clear()
        check(await call({"op": "set_codec", "output": bad}) == 400 and not sent, f"codec {bad!r} refused, nothing relayed")
    sent.clear()
    check(await call({"op": "set_codec"}) == 400 and not sent, "no codec refused")

    for label, setter in (("Web Player playing", lambda: setattr(box, "playing", True)),
                          ("Web Player wants to play", lambda: setattr(box, "want_play", True))):
        box.playing = box.want_play = False
        setter()
        sent.clear()
        check(await call({"op": "set_codec", "output": "aptx_hd"}) == 409 and not sent, f"{label}: 409, nothing relayed")
    box.playing = box.want_play = False

    # the Apple TV playing with its sound on atv-audio
    server.tv_owns_output = lambda: True
    sent.clear()
    check(await call({"op": "set_codec", "output": "aptx_hd"}) == 409 and not sent, "Apple TV / TV Player playing on atv-audio: 409")
    server.tv_owns_output = lambda: False
    sent.clear()
    check(await call({"op": "set_codec", "output": "aptx_hd"}) == 200 and sent, "nothing plays on atv-audio: allowed again")

    server.SINKS.pop("tv", None)
    check(await call({"op": "set_codec", "output": "aptx_hd"}) == 503, "no sink: 503")
    server.ABTEST["running"] = True
    check(await call({"op": "set_codec", "output": "aptx_hd"}) == 409, "blind test running: 409")
    server.ABTEST["running"] = False


asyncio.run(main())
print("FAILED" if failed else "ALL OK")
sys.exit(1 if failed else 0)
