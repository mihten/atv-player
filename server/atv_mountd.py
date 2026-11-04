#!/usr/bin/env python3
"""atv-mountd: the root helper that mounts the SMB library shares the ATV media server asks for (see shares.py).

Run by systemd (server/systemd/atv-mountd.{service,path,timer}): when the server drops an `apply` file into its spool
folder, and every two minutes (retries shares that failed, e.g. the NAS was down at boot). Each share becomes a
read-only systemd mount unit at <base>/<key>; the password lives only in a root-only credentials file. Mount units this
helper did not make are never touched (they carry a marker line).
"""
import argparse
import json
import os
import pwd
import stat
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shares  # noqa: E402  (validation rules shared with the server)

VERSION = "1"
MARKER = "# atv-mountd: generated for the ATV media server, do not edit"
UNIT_DIR = "/etc/systemd/system"
MAX_FILE = 64 * 1024


def log(msg):
    print(msg, flush=True)


def run(*cmd, check=False):
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def read_spool_file(spool, name, owner_uid):
    """Contents of a spool file owned by the server user (never follows a link), or None."""
    path = os.path.join(spool, name)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd) as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid not in (owner_uid, 0) or st.st_size > MAX_FILE:
            log(f"ignoring {path}: not a plain file of the server user")
            return None
        return f.read()


def unit_name(where):
    return run("systemd-escape", "--path", "--suffix=mount", where, check=True).stdout.strip()


def write_root_file(path, text, mode):
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def unit_text(spec, where, cred, user):
    opts = ["ro", "nosuid", "nodev", "noexec", f"uid={user.pw_uid}", f"gid={user.pw_gid}", "forceuid", "forcegid",
            "file_mode=0444", "dir_mode=0555", "soft", "nobrl", "iocharset=utf8", "actimeo=30", "echo_interval=30",
            "_netdev"]
    opts.append(f"credentials={cred}" if spec["username"] else "guest")
    if spec["vers"] != "default":
        opts.append(f"vers={spec['vers']}")
    return (f"{MARKER}\n[Unit]\nDescription=ATV library share //{spec['server']}/{spec['share']}\n"
            "Wants=network-online.target\nAfter=network-online.target\n\n"
            f"[Mount]\nWhat=//{spec['server']}/{spec['share']}\nWhere={where}\nType=cifs\n"
            f"Options={','.join(opts)}\nTimeoutSec=30\n\n[Install]\nWantedBy=remote-fs.target\n")


def last_error(unit):
    out = run("journalctl", "-u", unit, "-n", "30", "-o", "cat", "--no-pager").stdout.splitlines()
    hits = [l.strip() for l in out if any(w in l.lower() for w in ("error", "denied", "refused", "timed out",
                                                                     "no such", "not found", "failed", "invalid"))]
    text = hits[-1] if hits else "mount failed"
    hints = {"error(13)": "wrong user name or password", "error(2)": "no such share on that server",
             "error(112)": "server not reachable", "error(115)": "server not reachable",
             "error(95)": "SMB version not supported by the server: pick another one"}
    for k, v in hints.items():
        if k in text:
            return f"{v} ({text})"
    return text[:300]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spool", default="/var/lib/atv-media/mounts")
    ap.add_argument("--user", default="atvmedia")
    ap.add_argument("--base", default=shares.MOUNT_BASE)
    ap.add_argument("--cred-dir", default="/etc/atv-media/smb")
    ap.add_argument("--status", default=shares.STATUS_FILE)
    a = ap.parse_args()

    user = pwd.getpwnam(a.user)
    os.makedirs(a.cred_dir, mode=0o700, exist_ok=True)
    os.chmod(a.cred_dir, 0o700)
    os.makedirs(os.path.dirname(a.status), mode=0o755, exist_ok=True)
    try:
        spool_st = os.lstat(a.spool)
        spool_ok = stat.S_ISDIR(spool_st.st_mode) and spool_st.st_uid in (user.pw_uid, 0)
    except OSError:
        spool_ok = False

    wanted = {}
    raw = read_spool_file(a.spool, "shares.json", user.pw_uid) if spool_ok else None
    try:
        listing = json.loads(raw) if raw else []
    except ValueError:
        listing = []
    for entry in listing if isinstance(listing, list) else []:
        key = str((entry or {}).get("key") or "")
        try:
            if not shares.slug_ok(key):
                raise shares.ShareError("bad key")
            wanted[key] = shares.clean_share(entry)
        except shares.ShareError as e:
            log(f"ignoring share {key!r}: {e}")

    status = {}
    changed = False
    for key, spec in wanted.items():
        where = os.path.join(a.base, key)
        unit = unit_name(where)
        cred = os.path.join(a.cred_dir, f"{key}.cred")
        secret = read_spool_file(a.spool, f"secret-{key}", user.pw_uid) if spool_ok else None
        if secret is not None:
            lines = [f"username={spec['username']}", f"password={secret}"]
            if spec["domain"]:
                lines.append(f"domain={spec['domain']}")
            write_root_file(cred, "\n".join(lines) + "\n", 0o600)
            os.unlink(os.path.join(a.spool, f"secret-{key}"))
        elif os.path.exists(cred):  # user name / domain may have changed without a new password
            old = open(cred).read().splitlines()
            pw = next((l[9:] for l in old if l.startswith("password=")), "")
            lines = [f"username={spec['username']}", f"password={pw}"] + (
                [f"domain={spec['domain']}"] if spec["domain"] else [])
            if old != lines:
                write_root_file(cred, "\n".join(lines) + "\n", 0o600)
                secret = ""  # remount with the new name
        os.makedirs(where, mode=0o755, exist_ok=True)
        text = unit_text(spec, where, cred, user)
        path = os.path.join(UNIT_DIR, unit)
        old_text = open(path).read() if os.path.exists(path) else None
        fresh = old_text != text
        if fresh:
            write_root_file(path, text, 0o644)
            changed = True
        if fresh or secret is not None:
            run("systemctl", "daemon-reload")
            run("systemctl", "enable", unit)
            r = run("systemctl", "restart", unit)
        elif run("systemctl", "is-active", "--quiet", unit).returncode != 0:
            r = run("systemctl", "start", unit)
        else:
            r = None
        active = run("systemctl", "is-active", "--quiet", unit).returncode == 0 and os.path.ismount(where)
        status[key] = {"state": "mounted", "error": ""} if active else {"state": "failed", "error": last_error(unit)}
        if r is not None:
            log(f"{key}: {status[key]['state']} {status[key]['error']}".strip())

    # shares no longer wanted: only units carrying our marker
    for name in sorted(os.listdir(UNIT_DIR)):
        if not name.endswith(".mount"):
            continue
        path = os.path.join(UNIT_DIR, name)
        try:
            text = open(path).read()
        except OSError:
            continue
        if not text.startswith(MARKER):
            continue
        where = next((l[6:] for l in text.splitlines() if l.startswith("Where=")), "")
        key = os.path.basename(where)
        if os.path.dirname(where) == a.base.rstrip("/") and key in wanted:
            continue
        run("systemctl", "disable", "--now", name)
        os.unlink(path)
        changed = True
        for f in (os.path.join(a.cred_dir, f"{key}.cred"),):
            if os.path.exists(f):
                os.unlink(f)
        try:
            os.rmdir(where)
        except OSError:
            pass
        log(f"{key}: removed")
    if changed:
        run("systemctl", "daemon-reload")
    # passwords for shares that are not wanted (any more) are dropped
    if spool_ok:
        for name in os.listdir(a.spool):
            if name.startswith("secret-") and name[7:] not in wanted:
                os.unlink(os.path.join(a.spool, name))
        try:
            os.unlink(os.path.join(a.spool, "apply"))
        except OSError:
            pass
    write_root_file(a.status, json.dumps({"helper": VERSION, "at": time.time(), "shares": status}), 0o644)


if __name__ == "__main__":
    main()
