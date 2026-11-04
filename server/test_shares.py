#!/usr/bin/env python3
"""Network shares (Settings -> Library -> Add network share) and the disk-full note.

Unit checks for shares.py and the mount helper's unit file, then a THROWAWAY server with a FAKE mount helper (a thread
that answers the spool like atv_mountd.py would, "mounting" by creating a folder with media in it):
    python3 test_shares.py   (needs ffmpeg + the server's deps)
"""
import asyncio
import json
import os
import pwd
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import atv_mountd as M  # noqa: E402
import shares as SH  # noqa: E402
import test_atv_audio as H  # noqa: E402

PORT = 18097
H.PORT, H.BASE = PORT, f"http://127.0.0.1:{PORT}"
H.WS = H.BASE.replace("http", "ws")
BASE = H.BASE
failed = 0


def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed += 0 if cond else 1


def refused(fn, *a):
    try:
        fn(*a)
        return False
    except SH.ShareError:
        return True


def unit():
    ok = SH.clean_share({"server": "fileserver", "share": "media", "username": "reader"})
    check(ok == {"server": "fileserver", "share": "media", "username": "reader", "domain": "", "vers": "3.1.1"},
          "clean_share: plain fields, default SMB 3.1.1")
    check(SH.clean_share({"server": "\\\\fileserver\\media"})["share"] == "media", "clean_share: \\\\fileserver\\media pasted in Server")
    for bad in ({"server": "fileserver,uid=0", "share": "x"}, {"server": "fileserver", "share": "a,b=c"},
                {"server": "fileserver", "share": "x\nWhat=/"}, {"server": "fileserver", "share": "x", "username": "a\nb"},
                {"server": "fileserver", "share": "x", "vers": "1.0"}, {"server": "", "share": "x"}):
        check(refused(SH.clean_share, bad), f"clean_share refuses {bad}")
    check(SH.clean_subpath("\\Music\\Albums\\") == "Music/Albums" and SH.clean_subpath("") == "",
          "clean_subpath: backslashes, slashes trimmed")
    check(refused(SH.clean_subpath, "a/../../etc") and refused(SH.clean_subpath, "./x"), "clean_subpath refuses ..")
    check(SH.slug_ok("music-2") and not SH.slug_ok("../x") and not SH.slug_ok("A"), "share keys: slug only")
    user = pwd.getpwuid(os.getuid())
    t = M.unit_text({"server": "fileserver", "share": "media", "username": "u", "domain": "", "vers": "3.1.1"},
                    "/media/smb/media", "/etc/atv-media/smb/media.cred", user)
    opts = next(l for l in t.splitlines() if l.startswith("Options="))[8:].split(",")
    check(t.startswith(M.MARKER) and "What=//fileserver/media" in t and "Where=/media/smb/media" in t and "Type=cifs" in t,
          "mount unit: marker, What, Where, cifs")
    check("ro" in opts and "credentials=/etc/atv-media/smb/media.cred" in opts and "vers=3.1.1" in opts
          and f"uid={user.pw_uid}" in opts and "_netdev" in opts and not any("password" in o for o in opts),
          "mount unit: read-only, credentials file (no password in the unit), uid, _netdev")
    g = M.unit_text({"server": "fileserver", "share": "pub", "username": "", "domain": "", "vers": "default"},
                    "/media/smb/pub", "/x.cred", user)
    check("guest" in g and "credentials=" not in g and "vers=" not in g, "guest share: no credentials, automatic SMB")
    check(SH.share_state(None, "x")["state"] == "no-helper"
          and SH.share_state({"shares": {}}, "x")["state"] == "pending", "share_state: no helper / pending")
    check(SH.share_of_path(SH.MOUNT_BASE + "/fs/Music") == "fs" and SH.share_of_path("/media/music") is None,
          "share_of_path")


class FakeHelper(threading.Thread):
    """Answers the spool like atv_mountd.py: password 'good' (or a saved good one) mounts, anything else fails."""

    def __init__(self, spool, status, base, lib_src):
        super().__init__(daemon=True)
        self.spool, self.status, self.base, self.lib_src = Path(spool), Path(status), Path(base), Path(lib_src)
        self.passwords = {}  # key -> password the "root credentials file" holds
        self.seen_secret_files = []
        self.stop = False

    def run(self):
        while not self.stop:
            if (self.spool / "apply").exists():
                self.apply()
            time.sleep(0.1)

    def apply(self):
        listing = json.loads((self.spool / "shares.json").read_text())
        out = {}
        for s in listing:
            key = s["key"]
            sf = self.spool / f"secret-{key}"
            if sf.exists():
                self.seen_secret_files.append(oct(sf.stat().st_mode & 0o777))
                self.passwords[key] = sf.read_text()
                sf.unlink()
            mnt = self.base / key
            if self.passwords.get(key) == "good":
                if not mnt.exists():
                    import shutil
                    shutil.copytree(self.lib_src, mnt)
                out[key] = {"state": "mounted", "error": ""}
            else:
                if mnt.exists():
                    import shutil
                    shutil.rmtree(mnt)
                out[key] = {"state": "failed", "error": "wrong user name or password (mount error(13))"}
        for d in self.base.iterdir() if self.base.exists() else []:
            if d.name not in out:
                import shutil
                shutil.rmtree(d)
        (self.spool / "apply").unlink()
        tmp = self.status.with_suffix(".tmp")
        tmp.write_text(json.dumps({"helper": "fake", "at": time.time(), "shares": out}))
        tmp.replace(self.status)


async def req(http, method, path, **kw):
    async with http.request(method, BASE + path, **kw) as r:
        try:
            body = await r.json()
        except (aiohttp.ContentTypeError, ValueError):
            body = await r.text()
        return r.status, body


async def main(tmp):
    unit()
    tmp = Path(os.path.realpath(tmp))
    lib = tmp / "lib"
    H.tone(lib / "music" / "A" / "01 One.flac", 3, 330)
    nas = tmp / "nas-share"  # what the fake "share" contains
    H.tone(nas / "Albums" / "X" / "01 Net1.flac", 3, 440)
    H.tone(nas / "Albums" / "X" / "02 Net2.flac", 3, 460)
    H.tone(nas / "Other" / "01 Else.flac", 3, 480)
    base, status, data = tmp / "smb", tmp / "run" / "status.json", tmp / "data"
    base.mkdir()
    env = dict(os.environ, ATV_ROOTS=json.dumps({"music": str(lib / "music")}), ATV_DATA=str(data),
               ATV_CACHE=str(tmp / "cache"), ATV_PORT=str(PORT), ATV_MOUNT_BASE=str(base),
               ATV_MOUNTS_STATUS=str(status), ATV_DISK_WARN="80")
    for k in ("ATV_BONJOUR", "ATV_LIBRARY_BASES", "ATV_MOUNTS_DIR"):
        env.pop(k, None)
    proc = subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "a"), stderr=subprocess.STDOUT)
    helper = None
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
            check(len(await H.wait_up(http, 1)) == 1, "server up, 1 item")
            code, st = await req(http, "GET", "/api/settings")
            check(code == 200 and st["mount_helper"] is False and st["shares"] == [], "no helper yet: mount_helper false")
            code, msg = await req(http, "POST", "/api/settings/shares", json={"server": "fileserver", "share": "media",
                                                                               "password": "good"})
            check(code == 400 and "mount helper" in str(msg), f"add share without a helper: 400 ({msg})")

            status.parent.mkdir()
            status.write_text(json.dumps({"helper": "fake", "at": time.time(), "shares": {}}))
            helper = FakeHelper(data / "mounts", status, base, nas)
            helper.start()

            # wrong password: 400 with the helper's message, nothing kept
            code, msg = await req(http, "POST", "/api/settings/shares",
                                  json={"server": "fileserver", "share": "media", "username": "reader", "password": "bad",
                                        "path": "Albums"})
            check(code == 400 and "wrong user name or password" in str(msg), f"bad password: 400 + reason ({msg})")
            code, st = await req(http, "GET", "/api/settings")
            check(st["shares"] == [] and len(st["roots"]) == 1, "bad password: no share, no folder kept")

            # good password, a folder in the share
            t0 = time.time()
            code, st = await req(http, "POST", "/api/settings/shares",
                                 json={"server": "fileserver", "share": "media", "username": "reader", "password": "good",
                                       "path": "Albums", "name": "NAS albums"})
            check(code == 200, f"add share: 200 ({'' if code == 200 else st})")
            if code == 200:
                sh = st["shares"][0]
                root = next(r for r in st["roots"] if r["name"] == "NAS albums")
                check(sh["state"] == "mounted" and sh["roots"] == [root["key"]] and root["share"] == sh["key"],
                      "share mounted, its library folder linked to it")
                check(root["path"] == str(base / sh["key"] / "Albums"), f"folder path = mount/Albums ({root['path']})")
                check(time.time() - t0 < 10, "add answered within 10 s")
            saved = (data / "settings.json").read_text()
            check("good" not in saved and "password" not in saved, "settings.json holds no password")
            spool_list = (data / "mounts" / "shares.json").read_text()
            check("good" not in spool_list and "password" not in spool_list, "spool shares.json holds no password")
            check(len(helper.seen_secret_files) == 2 and set(helper.seen_secret_files) == {"0o600"},
                  f"passwords handed over in 0600 files ({helper.seen_secret_files})")
            check(not list((data / "mounts").glob("secret-*")), "password file consumed")
            items = {}
            for _ in range(40):
                items = await H.library(http)
                if len(items) == 3:
                    break
                await asyncio.sleep(0.25)
            check(sorted(items) == ["01 Net1", "01 One", "02 Net2"], f"share's folder scanned ({sorted(items)})")

            # same share again, another folder: reuses the share (no second mount)
            code, st = await req(http, "POST", "/api/settings/shares",
                                 json={"server": "FILESERVER", "share": "media", "path": "Other"})
            check(code == 200 and len(st["shares"]) == 1 and len(st["shares"][0]["roots"]) == 2,
                  "second folder of the same share: one share, two folders")
            code, st = await req(http, "POST", "/api/settings/shares",
                                 json={"server": "fileserver", "share": "media", "path": "Albums"})
            check(code == 400 and "already in the library" in str(st), "same folder twice refused")

            # change the login: bad -> failed (400, saved), good -> mounted
            key = st["shares"][0]["key"] if isinstance(st, dict) else (await req(http, "GET", "/api/settings"))[1]["shares"][0]["key"]
            code, msg = await req(http, "PUT", f"/api/settings/shares/{key}", json={"password": "wrong"})
            check(code == 400 and "does not mount" in str(msg), f"new wrong password: 400 ({msg})")
            code, st = await req(http, "GET", "/api/settings")
            check(st["shares"][0]["state"] == "failed" and "password" in st["shares"][0]["error"],
                  "share shows failed + reason")
            code, st = await req(http, "PUT", f"/api/settings/shares/{key}", json={"password": "good", "vers": "3.0"})
            check(code == 200 and st["shares"][0]["state"] == "mounted" and st["shares"][0]["vers"] == "3.0",
                  "fixed password: mounted again, SMB 3.0 saved")

            # a share in use cannot go; removing its folders removes it too
            code, msg = await req(http, "DELETE", f"/api/settings/shares/{key}")
            check(code == 400 and "library folder" in str(msg), "delete share in use: 400")
            roots = [r["key"] for r in st["roots"] if r.get("share") == key]
            code, st = await req(http, "DELETE", f"/api/settings/roots/{roots[0]}")
            check(code == 200 and len(st["shares"]) == 1, "first folder removed: share stays (one folder left)")
            code, st = await req(http, "DELETE", f"/api/settings/roots/{roots[1]}")
            check(code == 200 and st["shares"] == [], "last folder removed: share removed too")
            await asyncio.sleep(0.6)
            check(json.loads((data / "mounts" / "shares.json").read_text()) == [] and not (base / key).exists(),
                  "helper told: share unmounted")

            # disk note
            code, s = await req(http, "GET", "/api/state")
            d = s.get("disk") or {}
            check(code == 200 and {"used_pct", "free", "total", "warn"} <= set(d) and d["warn_at"] == 80,
                  f"state carries disk ({d.get('used_pct')} %)")
            check(d["warn"] == (d["used_pct"] >= 80), "warn = used >= 80 %")
            code, s2 = await req(http, "GET", "/api/status")
            check(code == 200 and s2.get("disk", {}).get("total") == d["total"], "/api/status carries disk")
    finally:
        if helper:
            helper.stop = True
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()

    # a second server with the threshold at 0 % always warns
    env2 = dict(env, ATV_DISK_WARN="0", ATV_DATA=str(tmp / "data2"), ATV_CACHE=str(tmp / "cache2"))
    proc = subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env2,
                            stdout=open(tmp / "server2.log", "a"), stderr=subprocess.STDOUT)
    try:
        async with aiohttp.ClientSession() as http:
            await H.wait_up(http, 1)
            code, s = await req(http, "GET", "/api/state")
            check(code == 200 and s["disk"]["warn"] is True, "ATV_DISK_WARN=0: warn on")
    finally:
        proc.terminate()
        proc.wait(10)
    if failed:
        print(open(tmp / "server.log").read()[-3000:])


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        asyncio.run(main(Path(d)))
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
