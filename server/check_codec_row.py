#!/usr/bin/env python3.13
"""Web remote: Settings > Audio > Codec row (aptX HD / aptX Adaptive). Throwaway server (demo library, port 18097) + a fake
atv-audio sink that reports Bluetooth headphones with both codecs. Checks, at phone and PC size: the row shows both
choices with the active one highlighted, is hidden with one codec or without Bluetooth, a click sends
{op: set_codec, output: <id>}, a 409 shows the server's message, and the page has no console errors. Screenshot
$OUT/codec-row-phone.png. Run:  SERVER_PY=<venv python with aiohttp mutagen pillow> PYTHONPATH=<aiohttp py313 dir> python3.13 check_codec_row.py <demo-library-dir> <out-dir>
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
LIB, OUT = Path(sys.argv[1]), Path(sys.argv[2])
PORT = 18097
BASE = f"http://127.0.0.1:{PORT}"
failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


async def main():
    tmp = Path(tempfile.mkdtemp())
    env = dict(os.environ, ATV_ROOTS=json.dumps({"demo": str(LIB)}), ATV_DATA=str(tmp / "data"), ATV_CACHE=str(tmp / "cache"),
               ATV_PORT=str(PORT), ATV_BONJOUR="0")
    proc = subprocess.Popen([os.environ.get("SERVER_PY", sys.executable), str(HERE / "server.py")], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                async with aiohttp.ClientSession() as h, h.get(BASE + "/api/status") as r:
                    if r.status == 200:
                        break
            except aiohttp.ClientError:
                pass
            time.sleep(0.5)
        async with aiohttp.ClientSession() as http:
            ws = await http.ws_connect(f"{BASE.replace('http', 'ws')}/ws?role=sink&session=tv")
            got = []

            async def reader():
                async for m in ws:
                    if m.type == aiohttp.WSMsgType.TEXT:
                        got.append(json.loads(m.data))
            rd = asyncio.create_task(reader())
            both = [{"codec": "aptx_hd", "label": "aptX HD"}, {"codec": "aptx_adaptive", "label": "aptX Adaptive"}]

            async def report(codecs, codec="aptx_hd", kind="bluetooth", ready=True):
                await ws.send_json({"type": "sink", "name": "atv-audio", "output": {"kind": kind, "name": "Headphones X", "codec": codec, "ready": ready}})
                await ws.send_json({"type": "audio_info", "outputs": [], "devices": [{"address": "AA:BB", "name": "Headphones X", "paired": True, "connected": True, "audio": True}],
                                    "can_bluetooth": True, "can_outputs": True, "codecs": codecs})
            await report(both)
            await asyncio.sleep(0.4)

            errors = []
            async with async_playwright() as pw:
                b = await pw.chromium.launch()
                for label, size in (("phone", {"width": 390, "height": 844}), ("pc", {"width": 1280, "height": 900})):
                    await report(both)
                    await asyncio.sleep(0.4)
                    page = await b.new_page(viewport=size)
                    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    await page.goto(BASE + "/?session=box")
                    await page.wait_for_timeout(1200)
                    await page.evaluate("audioSheet()")
                    await page.wait_for_selector("[data-audio-sheet]")
                    await page.wait_for_timeout(1800)
                    pills = await page.eval_on_selector_all('[data-a="codec"]', "els => els.map(e => [e.textContent.trim(), e.classList.contains('primary')])")
                    check(pills == [["aptX HD", True], ["aptX Adaptive", False]], f"{label}: both codecs, aptX HD highlighted ({pills})")
                    hint = await page.inner_text("#sheetBody")
                    check("Playing on aptX HD" in hint and "pause first" in hint, f"{label}: hint names the codec and says pause first")
                    if label == "phone":
                        await page.screenshot(path=str(OUT / "codec-row-phone.png"))
                        got.clear()
                        await page.click('[data-a="codec"][data-codec="aptx_adaptive"]')
                        await page.wait_for_timeout(800)
                        cmds = [m for m in got if m.get("type") == "audio_cmd" and m.get("op") == "set_codec"]
                        check(cmds == [{"type": "audio_cmd", "op": "set_codec", "output": "aptx_adaptive"}], f"click sends set_codec aptx_adaptive ({cmds})")
                        await report(both, codec="aptx_adaptive")      # the sink switched
                        await page.wait_for_timeout(2200)
                        pills = await page.eval_on_selector_all('[data-a="codec"]', "els => els.map(e => [e.textContent.trim(), e.classList.contains('primary')])")
                        check(pills == [["aptX HD", False], ["aptX Adaptive", True]], f"highlight follows the codec the sink reports ({pills})")
                    await page.close()
                # one codec only / no Bluetooth: no row
                page = await b.new_page(viewport={"width": 390, "height": 844})
                await report(both[:1])
                await page.goto(BASE + "/?session=box"); await page.wait_for_timeout(1200)
                await page.evaluate("audioSheet()"); await page.wait_for_selector("[data-audio-sheet]"); await page.wait_for_timeout(1800)
                check(await page.locator('[data-a="codec"]').count() == 0, "only one codec offered: no switch row")
                await report(both, kind="other", codec=None)
                await page.wait_for_timeout(2200)
                check(await page.locator('[data-a="codec"]').count() == 0, "not a Bluetooth output: no switch row")
                await b.close()
            check(not errors, f"no console errors ({errors[:3]})")
            rd.cancel()
            await ws.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


asyncio.run(main())
print("FAILED" if failed else "ALL OK")
sys.exit(1 if failed else 0)
