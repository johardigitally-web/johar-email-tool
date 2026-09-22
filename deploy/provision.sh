#!/usr/bin/env bash
# Set up the mailer on a server. Idempotent: safe to run again after a code
# change, and it will not touch an env file that already exists.
#
#   sudo bash /tmp/provision.sh
#
# It expects to share the machine with other things. Separate Linux user,
# separate directory, separate database, separate service, separate nginx site,
# so nothing else on the box can read this tool's files or it theirs.
set -euo pipefail

# These four are spelled out in the systemd units and in deploy/mailserver.sh as
# well. Changing one means changing all of them.
APP_DIR=/opt/mailer
DATA_DIR=/var/lib/mailer
ENV_FILE=/etc/mailer.env
USER_NAME=mailer

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE does not exist. Copy it into place first." >&2
  exit 1
fi

# The hostname nginx answers on. Taken from PUBLIC_URL in the env file rather
# than written here twice: the name in the nginx config and the name in every
# unsubscribe link have to be the same one, and two places to set it is two
# places to get it wrong. The carriage return strip is not decoration - an env
# file edited on Windows ends every value with one, and nginx would take it as
# part of the hostname.
SITE=${SITE:-$(sed -n 's#^PUBLIC_URL=https\?://\([^/:]*\).*#\1#p' "$ENV_FILE" | tr -d '\r' | tail -1)}
: "${SITE:?set SITE to the hostname this answers on, or PUBLIC_URL in $ENV_FILE}"

echo "--- user"
if ! id -u "$USER_NAME" >/dev/null 2>&1; then
  adduser --system --group --home "$APP_DIR" --no-create-home --shell /usr/sbin/nologin "$USER_NAME"
fi

echo "--- directories"
mkdir -p "$APP_DIR" "$DATA_DIR"
# The database holds names and email addresses of real people, so it is not
# world readable and it lives outside the code directory: redeploying the code
# is a tar extract over $APP_DIR and must never be able to land on it.
chown -R "$USER_NAME:$USER_NAME" "$DATA_DIR"
chmod 750 "$DATA_DIR"

echo "--- env file permissions"
chown root:"$USER_NAME" "$ENV_FILE"
chmod 640 "$ENV_FILE"

echo "--- virtualenv"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements-prod.txt"

# Code is read-only to the service; only the data directory is writable.
chown -R root:"$USER_NAME" "$APP_DIR"
chmod -R o-rwx "$APP_DIR"

echo "--- systemd"
install -m 644 "$APP_DIR/deploy/mailer.service" /etc/systemd/system/mailer.service
# The timer that makes the flows run on their own. Installing it is safe while
# sending is off: every brake is checked inside the functions it calls, so with
# SENDING_ENABLED=false it reads and decides and sends nothing.
install -m 644 "$APP_DIR/deploy/mailer-cron.service" /etc/systemd/system/mailer-cron.service
install -m 644 "$APP_DIR/deploy/mailer-cron.timer" /etc/systemd/system/mailer-cron.timer
systemctl daemon-reload
systemctl enable --now mailer
systemctl restart mailer
systemctl enable --now mailer-cron.timer

echo "--- backup"
install -m 755 "$APP_DIR/deploy/mailer-backup.sh" /usr/local/bin/mailer-backup.sh
cat > /etc/systemd/system/mailer-backup.service <<'UNIT'
[Unit]
Description=Back up the mailer database

[Service]
Type=oneshot
ExecStart=/usr/local/bin/mailer-backup.sh
UNIT
cat > /etc/systemd/system/mailer-backup.timer <<'UNIT'
[Unit]
Description=Daily mailer backup

[Timer]
# Early morning, and at a minute nothing else on the box is likely to pick, so
# a backup never runs on top of whatever else this machine does at 03:00.
OnCalendar=*-*-* 03:30:00
Persistent=true

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now mailer-backup.timer

echo "--- nginx"
# certbot EDITS this file in place when it installs a certificate: it adds the
# listen 443 block, the cert paths and the http->https redirect. Copying the
# repo's copy over the top therefore silently removes HTTPS, which is exactly
# what happened on the first redeploy after the certificate was issued. So once
# certbot has been here, the file on the server is the authority.
#
# To change the nginx config after that: edit it on the server, or delete the
# certbot lines and re-run certbot.
SITE_FILE=/etc/nginx/sites-available/mailer
if grep -q "letsencrypt" "$SITE_FILE" 2>/dev/null; then
  echo "  certbot owns $SITE_FILE, leaving it alone"
else
  # The repo's copy says __SITE__ rather than a hostname. A config carrying
  # somebody else's name answers to nobody and gives no error while it does it.
  sed "s|__SITE__|$SITE|g" "$APP_DIR/deploy/nginx-mailer.conf" > "$SITE_FILE"
  chmod 644 "$SITE_FILE"
fi
ln -sf "$SITE_FILE" /etc/nginx/sites-enabled/mailer
nginx -t
systemctl reload nginx

# Say it out loud rather than leaving it to be discovered by a browser. A
# certificate that exists but is not referenced means the site is answering on
# port 80 only, and anyone logging in is sending the password in the clear.
if [ -f "/etc/letsencrypt/live/$SITE/fullchain.pem" ] && ! grep -q "letsencrypt" "$SITE_FILE"; then
  echo
  echo "  WARNING: a certificate exists for $SITE but nginx is not using it."
  echo "  Run: sudo certbot --nginx -d $SITE --redirect"
fi

echo
echo "Done. Service:"
systemctl is-active mailer
echo "Reachable on the box as:"
# Retry: a reload is not instant, and asking too early gets the answer the old
# config would have given, which reads like a broken deploy.
code=000
for _ in 1 2 3 4 5; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $SITE" http://127.0.0.1/login || true)
  [ "$code" = "200" ] && break
  sleep 1
done
echo "  http://$SITE/login -> $code"
echo
echo "Next: point $SITE at this machine, then run certbot."
