#!/usr/bin/env bash
# Push the current code to the server and restart. Run from the mailer folder:
#
#   KEY=~/.ssh/id_ed25519 HOST=you@your-server bash deploy/push.sh
#
# Sends code only. The database lives in /var/lib/mailer and the settings in
# /etc/mailer.env, both outside the directory this writes to, so neither can
# be overwritten by a deploy.
set -euo pipefail

# No defaults for these two, deliberately. A default that happens to be a real
# machine is a deploy onto the wrong server by somebody who never read this far.
KEY=${KEY:?set KEY to the ssh private key that reaches the server}
HOST=${HOST:?set HOST to user@your-server}

# Where the code lives on the server. deploy/provision.sh and the systemd units
# spell out the same path, so it is changed in all of them or in none.
APP_DIR=/opt/mailer

cd "$(dirname "$0")/.."

echo "--- tests first"
if [ -x .venv/Scripts/python.exe ]; then
  .venv/Scripts/python.exe -m unittest discover -s tests -q
elif [ -x .venv/bin/python ]; then
  .venv/bin/python -m unittest discover -s tests -q
fi

# The tests never load the page, so a broken script file passes all of them and
# still ships. And a JavaScript syntax error is not a small fault: the file is
# parsed as a whole, so one bad character switches off every behaviour on every
# screen at once. That is exactly what happened - a string literal lost its
# escape and the live preview, the audience count, Ctrl+S and the confirmation
# dialogs were all dead, with nothing on screen to say so.
if command -v node >/dev/null 2>&1; then
  echo "--- javascript"
  for f in static/*.js; do
    node --check "$f" || { echo "REFUSING TO DEPLOY: $f does not parse" >&2; exit 1; }
  done
  echo "  static/*.js parses"
else
  echo "  (node not installed, skipping the javascript check)"
fi

echo "--- upload"
tar -cz --exclude=.venv --exclude=__pycache__ --exclude='*.pyc' \
        --exclude=.env --exclude='mailer.sqlite3*' --exclude=deploy/push.sh \
        -f - . | ssh -i "$KEY" "$HOST" "sudo tar -xz -C $APP_DIR"

echo "--- reinstall unit, deps, restart"
ssh -i "$KEY" "$HOST" "sudo bash $APP_DIR/deploy/provision.sh"
