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
tar -czf - server.py scanner.py loudness.py atv_shot.py static systemd site | ssh "$ATV_HOST" '
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
install -m 644 /opt/atv-media/systemd/*.service /opt/atv-media/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now atv-media-rescan.timer >/dev/null
systemctl enable atv-media.service >/dev/null
systemctl restart atv-media.service
echo deployed'
