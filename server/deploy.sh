#!/bin/sh
# Deploy the media server. Run from this directory.
# Target host: ATV_HOST (e.g. root@<server>) from the environment or the git-ignored ../local.env.
# Site config: the git-ignored server/site/ (copy server/site.example/): env -> /etc/atv-media/env,
# override.conf -> systemd drop-in. Both stay off GitHub.
set -e
[ -f ../local.env ] && . ../local.env
: "${ATV_HOST:?set ATV_HOST=root@<server> in local.env}"
[ -f site/env ] || { echo "missing server/site/env (see server/site.example/)" >&2; exit 1; }
ssh "$ATV_HOST" 'sh -s' <<'EOF'
set -e
id atvmedia >/dev/null 2>&1 || useradd --system --home /nonexistent --shell /usr/sbin/nologin atvmedia
python3 -c "import zeroconf" 2>/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-zeroconf >/dev/null
EOF
tar -czf - server.py box_sound.py settings.py shares.py atv_mountd.py scanner.py loudness.py lyrics.py atv_shot.py static systemd site | ssh "$ATV_HOST" '
set -e
install -d -m 755 /opt/atv-media
tar -xzf - -C /opt/atv-media
chown -R root:root /opt/atv-media
install -d -m 755 /etc/atv-media /etc/systemd/system/atv-media.service.d
install -m 644 /opt/atv-media/site/env /etc/atv-media/env
if [ -f /opt/atv-media/site/override.conf ]; then
  install -m 644 /opt/atv-media/site/override.conf /etc/systemd/system/atv-media.service.d/site.conf
else
  rm -f /etc/systemd/system/atv-media.service.d/site.conf
fi
rm -rf /opt/atv-media/site
[ -x /opt/atv-shot/venv/bin/python ] && install -m 755 /opt/atv-media/atv_shot.py /opt/atv-shot/atv-shot.py
install -m 644 /opt/atv-media/systemd/atv-media.service /etc/systemd/system/
# server 0.20: network shares (Settings -> Library) are mounted by the root helper atv-mountd; only where mounting
# works (a VM or bare metal: not inside a container) and cifs-utils is there
if ! systemd-detect-virt --container --quiet; then
  [ -x /sbin/mount.cifs ] || [ -x /usr/sbin/mount.cifs ] || DEBIAN_FRONTEND=noninteractive apt-get install -y -q cifs-utils >/dev/null
  install -m 644 /opt/atv-media/systemd/atv-mountd.service /opt/atv-media/systemd/atv-mountd.path \
    /opt/atv-media/systemd/atv-mountd.timer /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now atv-mountd.path atv-mountd.timer >/dev/null 2>&1
  systemctl start atv-mountd.service || true
fi
# server 0.18: the server schedules rescans itself (Settings -> Library); the old daily timer goes
if [ -e /etc/systemd/system/atv-media-rescan.timer ]; then
  systemctl disable --now atv-media-rescan.timer >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/atv-media-rescan.timer /etc/systemd/system/atv-media-rescan.service
fi
systemctl daemon-reload
systemctl enable atv-media.service >/dev/null
systemctl restart atv-media.service
echo deployed'
