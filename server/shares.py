"""SMB/CIFS library shares, added in the web remote (Settings -> Library -> Add network share).

The media server runs unprivileged and cannot mount anything itself. It writes what it wants into a spool folder it owns
(ATV_MOUNTS_DIR, default DATA/mounts):

  shares.json      every wanted share, WITHOUT passwords: [{key, server, share, username, domain, vers}]
  secret-<key>     a new password for one share (mode 0600); the helper moves it into its root-only credentials file
                   and deletes it
  apply            "please apply now" (the helper's systemd path unit starts on it; the helper deletes it)

and the root helper `atv-mountd` (server/mounts/) makes one systemd mount unit per share, read-only, at
MOUNT_BASE/<key> (default /media/smb/<key>), and reports in ATV_MOUNTS_STATUS (default /run/atv-mounts/status.json):

  {"helper": "<version>", "at": <unix time>, "shares": {"<key>": {"state": "mounted|failed|pending", "error": "..."}}}

Without the helper (an unprivileged container cannot mount CIFS at all) the status file is missing and the web page says
so. Library folders on a share are normal library folders (Settings roots) whose path lies under MOUNT_BASE/<key>.
"""
import json
import os
import re
import time

MOUNT_BASE = os.path.realpath(os.environ.get("ATV_MOUNT_BASE", "/media/smb"))
STATUS_FILE = os.environ.get("ATV_MOUNTS_STATUS", "/run/atv-mounts/status.json")
VERSIONS = ("3.1.1", "3.0", "2.1", "default")

_HOST = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")  # name or IPv4
_SHARE = re.compile(r"^[^\\/:*?\"<>|,=\x00-\x1f]{1,80}$")
_USER = re.compile(r"^[A-Za-z0-9._@ -]{0,64}$")
_DOMAIN = re.compile(r"^[A-Za-z0-9.-]{0,64}$")
_SUBPATH = re.compile(r"^[^\x00-\x1f\\]{0,400}$")


class ShareError(ValueError):
    """A share request the server cannot take (shown to the user as is)."""


def clean_share(spec):
    """Validated share fields (no password): server, share, username, domain, vers."""
    spec = spec if isinstance(spec, dict) else {}
    server = str(spec.get("server") or "").strip().strip("\\/")
    share = str(spec.get("share") or "").strip().strip("\\/")
    if "\\" in server or "/" in server:  # \\fileserver\media pasted into the server field
        parts = [p for p in re.split(r"[\\/]+", server) if p]
        server, share = parts[0], share or (parts[1] if len(parts) > 1 else "")
    if not _HOST.match(server):
        raise ShareError("server: a host name or IP address, e.g. fileserver or 10.0.0.5")
    if not _SHARE.match(share):
        raise ShareError("share: the share name, e.g. music")
    username = str(spec.get("username") or "").strip()
    if not _USER.match(username):
        raise ShareError("user name: letters, digits and . _ @ - only")
    domain = str(spec.get("domain") or "").strip()
    if not _DOMAIN.match(domain):
        raise ShareError("domain: letters, digits, . and - only")
    vers = str(spec.get("vers") or "3.1.1")
    if vers not in VERSIONS:
        raise ShareError("SMB version: one of " + ", ".join(VERSIONS))
    return {"server": server, "share": share, "username": username, "domain": domain, "vers": vers}


def clean_subpath(path):
    """A folder inside the share ('' = the whole share), never leaving it."""
    path = str(path or "").replace("\\", "/").strip().strip("/")
    if not _SUBPATH.match(path) or any(p in ("..", ".") for p in path.split("/")):
        raise ShareError("folder in the share: a path like Music/Albums, without ..")
    return path


def mount_point(key):
    return os.path.join(MOUNT_BASE, key)


def slug_ok(key):
    """A share key as settings.slug makes them (also the mount folder name)."""
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,29}", str(key or "")))


def share_of_path(path):
    """The share key a library path lies on, or None."""
    real = os.path.realpath(path or "")
    base = MOUNT_BASE.rstrip("/") + "/"
    if not real.startswith(base):
        return None
    return real[len(base):].split("/", 1)[0] or None


def read_status(path=None):
    """The helper's status file, or None when there is no helper."""
    try:
        with open(path or STATUS_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class Spool:
    """The server side of the spool folder (see the module doc)."""

    def __init__(self, folder):
        self.folder = folder

    def _write(self, name, text, mode=0o600):
        os.makedirs(self.folder, mode=0o700, exist_ok=True)
        tmp = os.path.join(self.folder, f".{name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, os.path.join(self.folder, name))

    def publish(self, shares, secrets=None):
        """Write the wanted list (+ new passwords) and ask the helper to apply it."""
        for key, password in (secrets or {}).items():
            self._write(f"secret-{key}", password)
        listing = [{k: s[k] for k in ("key", "server", "share", "username", "domain", "vers")} for s in shares]
        self._write("shares.json", json.dumps(listing, indent=1), 0o644)
        self._write("apply", str(time.time()), 0o644)


def share_state(status, key):
    """{state, error} of one share for the page: pending until the helper has reported on it."""
    if status is None:
        return {"state": "no-helper", "error": "this server cannot mount network shares (no mount helper)"}
    s = (status.get("shares") or {}).get(key)
    if not isinstance(s, dict):
        return {"state": "pending", "error": ""}
    return {"state": str(s.get("state") or "pending"), "error": str(s.get("error") or "")}
