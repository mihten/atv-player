#!/usr/bin/env python3
"""The iPhone away from home never plays on the headphones (server 0.30.2).

THROWAWAY server with a fake sink, a fake Apple TV player and fake iPhone players connecting from different places.
Settings of the test: home = 10.9.0.0/16 + 127.0.0.0/8, away hosts 10.9.0.11 (a VPN router that NATs its clients) and
127.0.0.2, trusted proxy 127.0.0.1 (the client is then the LAST X-Forwarded-For entry).
Checks: an iPhone from home (through the proxy) takes the headphones as before; the same iPhone connecting again from
outside (its home socket still open) loses them at once, its state says away, its route / volume / Play are refused, its
volume is not replayed later; the VPN router's address counts as away; X-Forwarded-For from a non-proxy is ignored; a
proxy request without X-Forwarded-For counts as away; a direct home connection is home; the Apple TV is never affected.
    python3 test_phone_away.py   (needs ffmpeg + the server's deps)
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_atv_audio as H  # noqa: E402
from test_box import until, of_type  # noqa: E402

PORT = 18115
H.PORT, H.BASE = PORT, f"http://127.0.0.1:{PORT}"
H.WS = H.BASE.replace("http", "ws")
failed = 0

READY = {"kind": "bluetooth", "name": "Headphones X", "codec": "aptx_hd", "ready": True, "volume": 40, "muted": False}


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def sessions_of(msgs):
    return [m["state"]["session"] for m in msgs if m["type"] == "state"]


async def phone_from(http, xff=None):
    """An iPhone player socket (through the proxy with `xff`, or direct); returns (socket, its first state's sink)."""
    ws = await http.ws_connect(f"{H.WS}/ws?role=player&kind=phone",
                               headers={"X-Forwarded-For": xff} if xff is not None else None)
    first = [m for m in await H.drain(ws) if m["type"] == "state"]
    return ws, (first[0]["state"]["sink"] if first else None)


async def main(tmp):
    lib = tmp / "lib"
    for n in range(1, 5):
        H.tone(lib / "songs" / f"0{n} Song{n}.flac", 4, 300 + 40 * n)
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(tmp / "data"),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT), ATV_HOME_NETS="10.9.0.0/16, 127.0.0.0/8",
               ATV_AWAY_HOSTS="10.9.0.11 127.0.0.2", ATV_PROXIES="127.0.0.1")
    env.pop("ATV_BONJOUR", None)
    proc = subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)
    try:
        async with aiohttp.ClientSession() as http, \
                aiohttp.ClientSession(connector=aiohttp.TCPConnector(local_addr=("127.0.0.2", 0))) as http2, \
                aiohttp.ClientSession(connector=aiohttp.TCPConnector(local_addr=("127.0.0.3", 0))) as http3:
            check(len(await H.wait_up(http, 4)) == 4, "throwaway library has 4 songs")
            ids = sorted(it["id"] for it in (await H.library(http)).values())

            sink = await http.ws_connect(f"{H.WS}/ws?role=sink&session=tv")
            await H.drain(sink)
            await sink.send_json({"type": "sink", "name": "atv-audio", "output": READY})
            await H.drain(sink)
            # the Apple TV comes through the proxy WITHOUT X-Forwarded-For (= away for an iPhone): never gated
            tv = await H.connect(http, "role=player&kind=tv")
            tvremote = await H.connect(http, "role=remote&session=tv")
            phremote = await H.connect(http, "role=remote&session=phone")
            await tv.send_json({"type": "route", "sink": True})
            await tvremote.send_json({"type": "cmd", "cmd": "play_ids", "ids": ids[:2]})
            await until(tv, "load")
            await tv.send_json({"type": "status", "item": ids[0], "playing": True, "position": 0.5, "duration": 4})
            await H.drain(tv)
            got = await H.drain(sink)
            check("tv" in sessions_of(got) and of_type(got, "load"), "the Apple TV holds the headphones (not gated)")
            await tvremote.send_json({"type": "cmd", "cmd": "pause"})
            await tv.send_json({"type": "status", "item": ids[0], "playing": False, "position": 0.6, "duration": 4})
            await H.drain(tv)
            await H.drain(sink)

            # ---- the iPhone at home, through the proxy (the last X-Forwarded-For entry counts): as in 0.30.1
            home, sk = await phone_from(http, "198.51.100.9, 10.9.0.5")
            check(sk is not None and sk.get("away") is False, f"iPhone from home via the proxy: state away false ({sk})")
            await phremote.send_json({"type": "cmd", "cmd": "play_ids", "ids": ids[2:]})
            await until(home, "load")
            await home.send_json({"type": "status", "item": ids[2], "playing": False, "position": 0, "duration": 4})
            await phremote.send_json({"type": "cmd", "cmd": "pause"})  # play_ids starts playing: the Play below is the edge
            await H.drain(home)
            await home.send_json({"type": "route", "sink": True})
            await H.drain(home)
            check((await H.state(http, "phone"))["sink"]["route"] is True, "... its route to the headphones is accepted")
            await asyncio.sleep(2.1)
            await H.drain(sink)
            await phremote.send_json({"type": "cmd", "cmd": "play"})
            await until(home, "control")
            await home.send_json({"type": "status", "item": ids[2], "playing": True, "position": 0.1, "duration": 4})
            got = await H.drain(sink)
            check("phone" in sessions_of(got) and of_type(got, "load"), "... its Play takes the headphones")
            await home.send_json({"type": "volume", "percent": 44, "muted": False})
            got = await H.drain(sink)
            check({"type": "volume", "percent": 44, "muted": False} in got, "... its volume reaches the headphones")

            # ---- the same iPhone connects from outside while its home socket is still open (not timed out yet)
            await H.drain(tv)
            away, sk = await phone_from(http, "203.0.113.7")
            check(sk is not None and sk.get("away") is True and sk.get("route") is False,
                  f"iPhone from outside: its first state says away, route off ({sk})")
            got = await H.drain(sink)
            check("tv" in sessions_of(got) and "phone" not in sessions_of(got),
                  f"... the headphones leave the iPhone at once (the sink follows the Apple TV again: {sessions_of(got)})")
            await away.send_json({"type": "route", "sink": True})
            await away.send_json({"type": "volume", "percent": 33, "muted": False})
            got = await H.drain(sink)
            check(not [m for m in got if m["type"] in ("route", "volume", "load")] and "phone" not in sessions_of(got),
                  f"... its route + volume do not reach the headphones ({[m['type'] for m in got]})")
            st = await H.state(http, "phone")
            check(st["sink"]["route"] is False and st["sink"]["away"] is True, f"... its state: route false, away ({st['sink']})")
            await asyncio.sleep(2.1)
            await phremote.send_json({"type": "cmd", "cmd": "play"})
            await until(away, "control")
            await away.send_json({"type": "status", "item": ids[2], "playing": True, "position": 0.4, "duration": 4})
            got = await H.drain(sink)
            check("phone" not in sessions_of(got) and not of_type(got, "load"), "... its Play does not take the headphones")
            seen = await H.drain(tv)
            check(not of_type(seen, "notice"), "... the Apple TV gets no notice")
            log = (tmp / "server.log").read_text()
            check("refused the iPhone's route: it is away from home (203.0.113.7)" in log
                  and "(phone) connected from 127.0.0.1 for 203.0.113.7 (away from home)" in log,
                  "... the log names the refused route and where it connected from")
            await home.close()
            await H.drain(away)
            check((await H.state(http, "phone"))["sink"]["away"] is True, "the old home socket closes: still away")
            await away.send_json({"type": "status", "item": ids[2], "playing": False, "position": 0.5, "duration": 4})
            await away.close()
            await H.drain(sink)

            # ---- where an iPhone counts as away / at home
            vpn, sk = await phone_from(http, "10.9.0.11")
            check(sk and sk["away"] is True, f"through the VPN router's address (an away host): away ({sk})")
            await vpn.close()
            fake, sk = await phone_from(http2, "10.9.0.5")
            check(sk and sk["away"] is True, f"X-Forwarded-For from a non-proxy is ignored: away ({sk})")
            await fake.close()
            bare, sk = await phone_from(http)
            check(sk and sk["away"] is True, f"the proxy sends no X-Forwarded-For: away ({sk})")
            await bare.close()
            direct, sk = await phone_from(http3)
            check(sk and sk["away"] is False, f"direct from a home address: at home ({sk})")

            # ---- back home: it takes the headphones again, and the volume it sent while away is not replayed
            await H.drain(sink)
            await direct.send_json({"type": "route", "sink": True})
            await asyncio.sleep(2.1)
            await phremote.send_json({"type": "cmd", "cmd": "play"})
            await until(direct, "control")
            await direct.send_json({"type": "status", "item": ids[2], "playing": True, "position": 0.6, "duration": 4})
            got = await H.drain(sink)
            vols = of_type(got, "volume")
            check("phone" in sessions_of(got) and of_type(got, "load"), "back home: its Play takes the headphones again")
            check(all(v.get("percent") != 33 for v in vols), f"... the volume sent while away does not reach them ({vols})")
            for ws in (sink, tv, tvremote, phremote, direct):
                await ws.close()
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="atv-phoneaway-"))
    try:
        asyncio.run(main(tmp))
    except Exception:
        log = tmp / "server.log"
        print(log.read_text()[-3000:] if log.exists() else "")
        raise
    finally:
        if failed:
            log = tmp / "server.log"
            print(log.read_text()[-4000:] if log.exists() else "")
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
