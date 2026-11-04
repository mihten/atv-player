"""Server settings that the web remote can change (DATA/settings.json): library folders with display names, skip
patterns, the rescan schedule, the server's name and an OPTIONAL admin password.

The first start fills the file from the environment (ATV_ROOTS / ATV_ROOT_NAMES, rescan daily at 04:15 like the old
systemd timer); from then on the file wins and the environment is only a first-run default. Library folders can only be
picked under the allowed base folders (ATV_LIBRARY_BASES, a JSON list or a path list; default: the parent folders of
the first-run roots), so the web page can never point the server at the rest of the file system.

Root keys (stored with every item in the library DB) never change once made: a new display name or a new path for an
existing folder keeps every item id, playlist entry and heart. Removing a folder removes its items at the next scan.
"""
import fnmatch
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta

RESCAN_MODES = ("off", "hours", "daily")
RESCAN_HOURS = (1, 2, 3, 4, 6, 8, 12)
PBKDF2_ROUNDS = 240_000
TOKEN_SECONDS = 30 * 86400
MAX_SKIP = 50


class SettingsError(ValueError):
    """A request the settings cannot take (shown to the user as is)."""


def _bases_from_env(env, seed_roots):
    raw = (env.get("ATV_LIBRARY_BASES") or "").strip()
    bases = []
    if raw:
        try:
            val = json.loads(raw)
            bases = [str(v) for v in val] if isinstance(val, list) else [str(val)]
        except ValueError:
            bases = [p for p in raw.split(os.pathsep) if p]
    if not bases:
        bases = sorted({os.path.dirname(os.path.abspath(p.rstrip("/"))) or "/" for p in seed_roots.values()})
    return [os.path.realpath(b) for b in bases]


def slug(text, taken):
    base = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:24] or "library"
    key, n = base, 2
    while key in taken:
        key, n = f"{base}-{n}", n + 1
    return key


def clean_skip(patterns):
    out = []
    for p in patterns or []:
        p = str(p).strip()
        if p and p not in out and len(p) <= 120:
            out.append(p)
    if len(out) > MAX_SKIP:
        raise SettingsError(f"at most {MAX_SKIP} skip patterns")
    return out


def skip_matcher(patterns):
    """skip(relpath) for the scanner: a pattern without '/' matches any single folder or file name in the path, one
    with '/' the whole path relative to the library folder. Case-insensitive glob (*, ?, [..])."""
    names = [p.lower() for p in patterns if "/" not in p]
    paths = [p.lower().strip("/") for p in patterns if "/" in p]
    if not names and not paths:
        return None

    def skip(relpath):
        rel = relpath.replace(os.sep, "/").lower()
        if any(fnmatch.fnmatchcase(rel, p) or fnmatch.fnmatchcase(rel, p + "/*") for p in paths):
            return True
        return any(fnmatch.fnmatchcase(part, p) for part in rel.split("/") for p in names)
    return skip


def clean_rescan(r):
    r = r if isinstance(r, dict) else {}
    mode = r.get("mode") if r.get("mode") in RESCAN_MODES else "daily"
    try:
        hours = int(r.get("hours", 6))
    except (TypeError, ValueError):
        hours = 6
    hours = hours if hours in RESCAN_HOURS else 6
    at = str(r.get("at") or "04:15")
    m = re.match(r"^(\d{1,2}):(\d{2})$", at)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise SettingsError("time must be HH:MM")
    return {"mode": mode, "hours": hours, "at": f"{int(m.group(1)):02d}:{m.group(2)}"}


def next_rescan(rescan, last_finished, now=None):
    """Unix time of the next scheduled rescan, or None when off."""
    now = time.time() if now is None else now
    if rescan["mode"] == "off":
        return None
    if rescan["mode"] == "hours":
        step = rescan["hours"] * 3600
        return max(now, (last_finished or now) + step) if last_finished else now + step
    hh, mm = (int(x) for x in rescan["at"].split(":"))
    t = datetime.fromtimestamp(now).replace(hour=hh, minute=mm, second=0, microsecond=0)
    if t.timestamp() <= now:
        t += timedelta(days=1)
    return t.timestamp()


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ROUNDS).hex()
    return {"salt": salt, "hash": digest, "rounds": PBKDF2_ROUNDS}


def check_password(admin, password):
    if not admin or not isinstance(password, str):
        return False
    rounds = int(admin.get("rounds") or PBKDF2_ROUNDS)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(admin["salt"]), rounds).hex()
    return hmac.compare_digest(digest, admin["hash"])


class Settings:
    def __init__(self, path, env=None):
        env = os.environ if env is None else env
        self.path = path
        try:
            seed = json.loads(env.get("ATV_ROOTS") or "{}")
        except ValueError:
            seed = {}
        seed = {str(k): str(v) for k, v in (seed.items() if isinstance(seed, dict) else [])}
        try:
            names = json.loads(env.get("ATV_ROOT_NAMES") or "{}")
        except ValueError:
            names = {}
        self.bases = _bases_from_env(env, seed)
        self.tokens = {}  # admin session token -> expiry (memory only: a restart asks again)
        loaded = None
        try:
            with open(path) as f:
                loaded = json.load(f)
        except (OSError, ValueError):
            pass
        self.data: dict = loaded if isinstance(loaded, dict) else {}
        if not isinstance(loaded, dict):
            self.data = {"roots": [{"key": k, "path": v, "name": str(names.get(k) or k)} for k, v in seed.items()],
                         "skip": [], "rescan": {"mode": "daily", "hours": 6, "at": "04:15"},
                         "server_name": "", "admin": None}
            self.save()
        self.data.setdefault("roots", [])
        self.data["skip"] = clean_skip(self.data.get("skip"))
        self.data["rescan"] = clean_rescan(self.data.get("rescan"))
        self.data.setdefault("server_name", "")
        self.data.setdefault("admin", None)

    # -- persistence
    def save(self):
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=1)
        os.chmod(tmp, 0o600)  # holds the password hash
        os.replace(tmp, self.path)

    # -- views
    def roots(self):
        return {r["key"]: r["path"] for r in self.data["roots"]}

    def root_names(self):
        return {r["key"]: r.get("name") or r["key"] for r in self.data["roots"]}

    @property
    def skip(self):
        return list(self.data["skip"])

    @property
    def rescan(self):
        return dict(self.data["rescan"])

    def server_name(self, fallback):
        return (self.data.get("server_name") or "").strip() or fallback

    @property
    def has_password(self):
        return bool(self.data.get("admin"))

    def public(self):
        """What the web page may read (never the password hash)."""
        return {"roots": [dict(r) for r in self.data["roots"]], "skip": self.skip, "rescan": self.rescan,
                "server_name": self.data.get("server_name") or "", "admin": self.has_password, "bases": self.bases}

    # -- paths
    def allowed(self, path):
        """The real path when it is a directory under one of the allowed bases, else None."""
        if not path or not os.path.isabs(path):
            return None
        real = os.path.realpath(path)
        for b in self.bases:
            if (real == b or real.startswith(b.rstrip("/") + "/")) and os.path.isdir(real):
                return real
        return None

    def browse(self, path=None):
        """Folders the picker may show: the bases, or the direct subfolders of an allowed folder."""
        if not path:
            return {"path": None, "parent": None,
                    "dirs": [{"path": b, "name": b} for b in self.bases if os.path.isdir(b)]}
        real = self.allowed(path)
        if real is None:
            raise SettingsError("not an allowed folder")
        try:
            names = sorted((e.name for e in os.scandir(real) if e.is_dir() and not e.name.startswith(".")),
                           key=str.lower)
        except OSError as e:
            raise SettingsError(f"cannot read {real}: {e.strerror}")
        parent = os.path.dirname(real)
        return {"path": real, "parent": parent if self.allowed(parent) else None,
                "dirs": [{"path": os.path.join(real, n), "name": n} for n in names[:500]]}

    # -- changes
    def add_root(self, path, name):
        real = self.allowed(path)
        if real is None:
            raise SettingsError("pick a folder inside " + ", ".join(self.bases))
        for r in self.data["roots"]:
            if os.path.realpath(r["path"]) == real:
                raise SettingsError(f"already in the library as \"{r.get('name') or r['key']}\"")
        name = (name or os.path.basename(real) or real).strip()[:60]
        key = slug(name, {r["key"] for r in self.data["roots"]})
        self.data["roots"].append({"key": key, "path": real, "name": name})
        self.save()
        return key

    def update_root(self, key, name=None, path=None):
        r = next((r for r in self.data["roots"] if r["key"] == key), None)
        if r is None:
            raise SettingsError("no such library folder")
        if name is not None:
            name = str(name).strip()[:60]
            if not name:
                raise SettingsError("the name cannot be empty")
            r["name"] = name
        if path is not None and os.path.realpath(path) != os.path.realpath(r["path"]):
            real = self.allowed(path)
            if real is None:
                raise SettingsError("pick a folder inside " + ", ".join(self.bases))
            if any(os.path.realpath(o["path"]) == real for o in self.data["roots"] if o is not r):
                raise SettingsError("another library folder already uses it")
            r["path"] = real
        self.save()

    def remove_root(self, key):
        before = len(self.data["roots"])
        self.data["roots"] = [r for r in self.data["roots"] if r["key"] != key]
        if len(self.data["roots"]) == before:
            raise SettingsError("no such library folder")
        self.save()

    def set_skip(self, patterns):
        self.data["skip"] = clean_skip(patterns)
        self.save()

    def set_rescan(self, rescan):
        self.data["rescan"] = clean_rescan({**self.data["rescan"], **(rescan or {})})
        self.save()

    def set_server_name(self, name):
        self.data["server_name"] = str(name or "").strip()[:40]
        self.save()

    # -- optional admin password (none by default: the home network is trusted, a reverse proxy guards the rest)
    def set_password(self, password):
        self.data["admin"] = hash_password(password) if password else None
        self.tokens.clear()
        self.save()

    def unlock(self, password):
        if not check_password(self.data.get("admin"), password):
            return None
        token = secrets.token_urlsafe(24)
        now = time.time()
        self.tokens = {t: exp for t, exp in self.tokens.items() if exp > now}
        self.tokens[token] = now + TOKEN_SECONDS
        return token

    def authorized(self, token):
        if not self.has_password:
            return True
        exp = self.tokens.get(token or "")
        return bool(exp and exp > time.time())
