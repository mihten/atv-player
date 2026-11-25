#!/bin/sh
# Build the LTVPlayer launcher IPK (webOS package format 2) with tar, gzip and ar only.
#   ltv/build-ipk.sh [out-dir]
# Server address: LTV_SERVER (e.g. http://<server>:8080) from the environment or the git-ignored ../local.env.
# Output: out-dir, else LTV_OUT, else the current directory; <id>_<version>_all.ipk. An existing file is never
# overwritten: bump "version" in launcher/appinfo.json for a new build. The IPK is not committed.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
[ -f "$HERE/../local.env" ] && . "$HERE/../local.env"
: "${LTV_SERVER:?set LTV_SERVER=http://<server>:8080 in local.env}"
OUT=${1:-${LTV_OUT:-.}}
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)

field() { sed -n "s/^ *\"$1\": *\"\([^\"]*\)\".*/\1/p" "$HERE/launcher/appinfo.json" | head -1; }
ID=$(field id)
VER=$(field version)
[ -n "$ID" ] && [ -n "$VER" ] || { echo "appinfo.json: id / version not found" >&2; exit 1; }
IPK="$OUT/${ID}_${VER}_all.ipk"
[ -e "$IPK" ] && { echo "$IPK exists: bump the version in launcher/appinfo.json" >&2; exit 1; }

W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
APP="$W/data/usr/palm/applications/$ID"
PKG="$W/data/usr/palm/packages/$ID"
mkdir -p "$APP" "$PKG" "$W/control"
for f in appinfo.json index.html icon.png largeIcon.png; do cp "$HERE/launcher/$f" "$APP/"; done
printf 'window.LTV_SERVER = "%s";\nwindow.LTV_LAUNCHER = "%s";\n' "${LTV_SERVER%/}" "$VER" > "$APP/config.js"
printf '{\n  "id": "%s",\n  "version": "%s",\n  "app": "%s"\n}\n' "$ID" "$VER" "$ID" > "$PKG/packageinfo.json"
SIZE=$(du -sk "$W/data" | cut -f1)
cat > "$W/control/control" <<EOF
Package: $ID
Version: $VER
Section: misc
Priority: optional
Architecture: all
Installed-Size: $((SIZE * 1024))
Maintainer: N/A <nobody@example.com>
Description: LTVPlayer launcher (opens the app hosted by the ATV server)
webOS-Package-Format-Version: 2
webOS-Packager-Version: x.y.x
EOF
printf '2.0\n' > "$W/debian-binary"
(cd "$W/control" && tar --owner=0 --group=0 -czf ../control.tar.gz control)
(cd "$W/data" && tar --owner=0 --group=0 -czf ../data.tar.gz usr)
(cd "$W" && ar rc "$IPK" debian-binary control.tar.gz data.tar.gz)
echo "$IPK"
