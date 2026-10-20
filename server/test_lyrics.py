#!/usr/bin/env python3
"""Embedded lyrics (lyrics.py + scanner + API), against a THROWAWAY server instance (own temp library, data dir
and port; never the real one).   python3 test_lyrics.py   (run on the media server host)
"""
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lyrics as L  # noqa: E402  (unit checks of the parsers)

PORT = 18094
BASE = f"http://127.0.0.1:{PORT}"
failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def tone(path, seconds=3, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = []
    for k, v in {"title": path.stem, "artist": "Test", **tags}.items():
        meta += ["-metadata", f"{k}={v}"]
    ff("-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", *meta, str(path))


def uslt(path, text):
    """A real ID3 USLT frame (ffmpeg only writes a nonstandard TXXX:USLT)."""
    from mutagen.id3 import ID3, ID3NoHeaderError, USLT
    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()
    tags.delall("USLT")
    tags.add(USLT(encoding=3, lang="eng", desc="", text=text))
    tags.save(str(path))


def start(tmp, env):
    return subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)


def stop(proc):
    proc.terminate()
    try:
        proc.wait(30)
    except subprocess.TimeoutExpired:
        proc.kill()


async def get(http, path):
    async with http.get(f"{BASE}{path}") as r:
        return await r.json()


async def wait_done(http, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            c = await get(http, "/api/lyrics")
            if not c.get("pending") and not c["running"] and sum(v for k, v in c.items() if k != "running") > 0:
                return c
        except (aiohttp.ClientError, KeyError, ValueError):
            pass
        await asyncio.sleep(0.4)
    return None


def unit():
    srt = ("1\n00:00:01,000 --> 00:00:03,500\n<i>Hello</i> there ♪\n\n"
           "2\n00:00:04,000 --> 00:00:06,000\n- First speaker\n- Second {\\an8}speaker\n\n"
           "3\n00:00:07,000 --> 00:00:08,000\n♪ ♪\n")
    cues = L.parse_srt(srt)
    check(len(cues) == 2 and cues[0]["t"] == "Hello there" and cues[0]["s"] == 1.0 and cues[0]["e"] == 3.5,
          f"srt: markup and ♪ stripped, empty cue dropped: {cues}")
    check(cues[1]["t"] == "First speaker\nSecond speaker", "srt: speaker dashes removed, lines kept")
    lrc = ("[ti:T]\n[ar:A]\n[offset:500]\n[00:10.00]one\n[00:12.50]two\n[00:15.00]\n[00:20.00]three\n"
           "[00:30.00][01:00.00]chorus\n[01:10.50]<01:10.60>last <01:11.00>word\n")
    c = L.parse_lrc(lrc)
    check(c[0] == {"s": 9.5, "e": 12.0, "t": "one"}, f"lrc: offset +500 ms shifts 10.0 -> 9.5: {c[0]}")
    check(c[1]["e"] == 14.5, "lrc: an empty line ends the previous cue")
    check([x["s"] for x in c if x["t"] == "chorus"] == [29.5, 59.5], "lrc: two stamps on one line = two cues")
    check(c[-1]["t"] == "last word" and c[-1]["e"] is None, "lrc: word stamps removed, last cue open ended")
    neg = L.parse_lrc("[offset:-4499]\n[00:11.00]a\n[00:12.00]b\n[00:13.00]c\n")
    check(abs(neg[0]["s"] - 15.5) < 0.01, f"lrc: offset -4499 ms shifts 11.0 -> 15.5: {neg[0]['s']}")
    p = L.plain_cues("Verse\r\nline two\r\n\r\n\r\nChorus\n")
    check([x["t"] for x in p] == ["Verse", "line two", "", "Chorus"], f"plain: blank runs become one gap: {[x['t'] for x in p]}")


async def main(tmp):
    lib = tmp / "lib"
    synced = ("[ti:T]\n[offset:500]\n[00:10.00]line one\n[00:12.50]line two\n[00:15.00]\n[00:20.00]line three\n"
              "[00:25.00]line four\n")
    tone(lib / "synced.flac", lyrics=synced)
    tone(lib / "plain.mp3")
    uslt(lib / "plain.mp3", "Verse one\nsecond line\n\nChorus line")
    tone(lib / "plainflac.flac", lyrics="Only plain\nlines here")
    tone(lib / "none.flac")
    (tmp / "a.srt").write_text("1\n00:00:01,000 --> 00:00:03,000\nEnglish <i>one</i>\n\n2\n00:00:04,000 --> 00:00:05,000\nEnglish two\n")
    (tmp / "b.srt").write_text("1\n00:00:01,000 --> 00:00:03,000\nHrvatski jedan\n")
    ff("-f", "lavfi", "-i", "color=c=black:s=64x64:d=6", "-f", "lavfi", "-i", "sine=frequency=500:duration=6",
       "-i", str(tmp / "b.srt"), "-i", str(tmp / "a.srt"), "-map", "0", "-map", "1", "-map", "2", "-map", "3",
       "-c:v", "libx264", "-c:a", "aac", "-c:s", "srt",
       "-metadata:s:s:0", "language=hrv", "-metadata:s:s:1", "language=eng",
       "-disposition:s:0", "0", "-disposition:s:1", "default", str(lib / "clip.mkv"))
    ff("-f", "lavfi", "-i", "color=c=black:s=64x64:d=3", "-f", "lavfi", "-i", "sine=frequency=500:duration=3",
       str(lib / "nosubs.mkv"))
    data = tmp / "data"
    data.mkdir()
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib)}), ATV_DATA=str(data),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT))
    proc = start(tmp, env)
    try:
        async with aiohttp.ClientSession() as http:
            counts = await wait_done(http)
            check(counts is not None, "background job finishes")
            if counts is None:
                return
            check(counts.get("synced") == 2 and counts.get("plain") == 2 and counts.get("none") == 2,
                  f"2 synced (flac + mkv), 2 plain, 2 without: {counts}")
            items = {it["title"]: it for it in (await get(http, "/api/library?all=1"))["items"]}
            check(items["synced"]["lyr"] == 2 and items["plain"]["lyr"] == 1 and items["none"]["lyr"] == 0
                  and items["clip"]["lyr"] == 2 and items["nosubs"]["lyr"] == 0, "items carry lyr 2/1/0/2/0")

            d = await get(http, f"/api/lyrics/{items['synced']['id']}")
            t = d["tracks"][0]
            check(len(d["tracks"]) == 1 and t["synced"] and [c["t"] for c in t["cues"]] == ["line one", "line two", "line three", "line four"],
                  "flac tag: synced cues without the empty marker line")
            check(t["cues"][0]["s"] == 9.5 and t["cues"][1]["e"] == 14.5, f"flac tag: offset applied: {t['cues'][:2]}")

            d = await get(http, f"/api/lyrics/{items['plain']['id']}")
            t = d["tracks"][0]
            check(not t["synced"] and [c["t"] for c in t["cues"]] == ["Verse one", "second line", "", "Chorus line"]
                  and t["cues"][0]["s"] is None, f"mp3 USLT: plain lines with a gap: {[c['t'] for c in t['cues']]}")
            d = await get(http, f"/api/lyrics/{items['plainflac']['id']}")
            check(len(d["tracks"]) == 1 and not d["tracks"][0]["synced"], "flac tag without stamps: plain")

            d = await get(http, f"/api/lyrics/{items['clip']['id']}")
            tr = d["tracks"]
            check([x["ord"] for x in tr] == [0, 1] and [x["lang"] for x in tr] == ["hrv", "eng"],
                  f"mkv: both subtitle tracks, ordinals and languages: {[(x['ord'], x['lang']) for x in tr]}")
            check(tr[1]["is_default"] and not tr[0]["is_default"], "mkv: default flag kept")
            check([c["t"] for c in tr[1]["cues"]] == ["English one", "English two"] and abs(tr[1]["cues"][0]["e"] - 3.0) < 0.1,
                  f"mkv: cue text cleaned, end times kept: {tr[1]['cues']}")
            d = await get(http, f"/api/lyrics/{items['none']['id']}")
            check(d["lyr"] == 0 and d["tracks"] == [], "no lyrics: empty list, lyr 0")
            async with http.get(f"{BASE}/api/lyrics/99999") as r:
                check(r.status == 404, "unknown id: 404")

            # on demand: a row the job has not reached yet is extracted by the request
            db = data / "library.db"
            con = sqlite3.connect(db)
            con.execute("UPDATE items SET lyr = NULL WHERE id = ?", (items["synced"]["id"],))
            con.execute("DELETE FROM lyrics WHERE item_id = ?", (items["synced"]["id"],))
            con.commit()
            con.close()
            d = await get(http, f"/api/lyrics/{items['synced']['id']}")
            check(d["lyr"] == 2 and len(d["tracks"][0]["cues"]) == 4, "on demand: extracted when lyr is NULL")

            # a changed file is read again; a deleted file loses its row
            tone(lib / "plain.mp3")
            uslt(lib / "plain.mp3", "Brand new text")
            os.utime(lib / "plain.mp3", (time.time() + 5, time.time() + 5))
            (lib / "none.flac").unlink()
            async with http.post(f"{BASE}/api/rescan?wait=1") as r:
                check(r.status == 200, "rescan")
            await wait_done(http)
            d = await get(http, f"/api/lyrics/{items['plain']['id']}")
            check(d["tracks"] and d["tracks"][0]["cues"][0]["t"] == "Brand new text", "changed file: new lyrics read")
            con = sqlite3.connect(db)
            orphans = con.execute("SELECT COUNT(*) FROM lyrics WHERE item_id NOT IN (SELECT id FROM items)").fetchone()[0]
            con.close()
            check(orphans == 0, "no orphan lyrics rows")

            # restart: nothing pending, values from the DB
            stop(proc)
            proc = start(tmp, env)
            for _ in range(60):
                try:
                    if len((await get(http, "/api/library?all=1"))["items"]) == 5:
                        break
                except (aiohttp.ClientError, KeyError, ValueError):
                    pass
                await asyncio.sleep(0.2)
            c = await get(http, "/api/lyrics")
            check(not c.get("pending"), f"after a restart nothing is pending: {c}")
    finally:
        stop(proc)


if __name__ == "__main__":
    unit()
    tmp = Path(tempfile.mkdtemp(prefix="atv-lyr-"))
    try:
        asyncio.run(main(tmp))
    finally:
        log = tmp / "server.log"
        if failed and log.exists():
            print(log.read_text()[-3000:])
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
