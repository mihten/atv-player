#!/bin/bash
# Remote-access check on the iPhone simulator (CI): the app has only an "internet address" + login (no home server
# visible), so it must fall back to it, authenticate through a basic-auth reverse proxy (Caddy) and stream.
#   tools/remote_check.sh <AtvPlayerPhone.app> <out dir>
# Needs the throwaway media server on 127.0.0.1:8080 (demo library + design_driver.py setup) and `caddy` installed.
# Checks the PROXY's access log: 401 without login, 200 with it, WebSocket 101, artwork 200, and the music video
# requested with ?audio=1 (audio-only mode) or without (audio + video mode).
set -uo pipefail
APP=$1 OUT=$2
mkdir -p "$OUT"
PHONE_ID=eu.mihten.atvplayer.phone
DRV=(python3 tools/design_driver.py)
LOG="$OUT/caddy-access.log"
rm -f "$LOG"
PASS=check-secret-1
HASH=$(caddy hash-password --plaintext "$PASS")
cat > "$OUT/Caddyfile" <<EOF
{
	auto_https off
	admin off
}
:8443 {
	basic_auth {
		checker $HASH
	}
	log {
		output file $LOG
		format json
	}
	reverse_proxy 127.0.0.1:8080
}
EOF
caddy start --config "$OUT/Caddyfile" --adapter caddyfile >"$OUT/caddy-start.log" 2>&1 || { cat "$OUT/caddy-start.log"; exit 1; }
sleep 2
echo "proxy without login: $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8443/api/state)  with login: $(curl -s -o /dev/null -w '%{http_code}' -u checker:$PASS http://127.0.0.1:8443/api/state)"

PHONE=$(xcrun simctl list devices available -j | python3 -c '
import json, sys
data = json.load(sys.stdin)["devices"]
for runtime in sorted((r for r in data if ".iOS-" in r), reverse=True):
    for d in data[runtime]:
        if d["name"] in ("iPhone 17 Pro", "iPhone 16 Pro", "iPhone 17", "iPhone 16"):
            print(d["udid"]); sys.exit()
    if data[runtime]:
        print(data[runtime][0]["udid"]); sys.exit()')
xcrun simctl boot "$PHONE"; xcrun simctl bootstatus "$PHONE" -b >/dev/null
xcrun simctl ui "$PHONE" appearance dark
xcrun simctl install "$PHONE" "$APP"

run_mode() {  # $1 = audio | video
  local mode=$1
  xcrun simctl terminate "$PHONE" "$PHONE_ID" 2>/dev/null
  : > "$LOG"
  python3 tools/design_driver.py cmd phone pause >/dev/null 2>&1
  xcrun simctl launch "$PHONE" "$PHONE_ID" -appTheme aurora -remoteURL http://127.0.0.1:8443 -remoteUser checker \
    -remotePasswordDemo "$PASS" -remoteStreamMode "$mode" >/dev/null
  sleep 14   # Bonjour finds nothing; after ~4 s the app uses the internet address
  "${DRV[@]}" play phone videos "Undertow"
  sleep 14
  xcrun simctl io "$PHONE" screenshot --type=png "$OUT/remote-$mode.png" >/dev/null 2>&1
  python3 - "$LOG" "$mode" <<'PY'
import json, sys
from collections import Counter
log, mode = sys.argv[1:3]
rows = []
for line in open(log):
    try:
        r = json.loads(line)
    except ValueError:
        continue
    rows.append((r["request"]["method"], r["request"]["uri"].split("?")[0], "audio=1" in r["request"]["uri"], r["status"]))
c = Counter((m, ("/ws" if p == "/ws" else "/art" if p.startswith("/art/") else "/media" if p.startswith("/media/") else "/api" if p.startswith("/api/") else p), a, s) for m, p, a, s in rows)
for k, v in sorted(c.items(), key=str):
    print(f"  {v:3d} x {k[0]} {k[1]}{' ?audio=1' if k[2] else ''} -> {k[3]}")
ok_ws = any(p == "/ws" and s == 101 for _, p, _, s in rows)
ok_art = any(p.startswith("/art/") and s == 200 for _, p, _, s in rows)
media = [(a, s) for _, p, a, s in rows if p.startswith("/media/")]
ok_media = any(s in (200, 206) and a == (mode == "audio") for a, s in media)
bad_media = any(s in (200, 206) and a != (mode == "audio") for a, s in media)
print(f"RESULT {mode}: websocket={ok_ws} art={ok_art} media_as_expected={ok_media} wrong_media_kind={bad_media}")
sys.exit(0 if (ok_ws and ok_art and ok_media and not bad_media) else 1)
PY
  local rc=$?
  echo "mode $mode exit $rc"
  return $rc
}

fail=0
run_mode audio || fail=1
run_mode video || fail=1
caddy stop >/dev/null 2>&1
xcrun simctl shutdown "$PHONE" >/dev/null 2>&1
exit $fail
