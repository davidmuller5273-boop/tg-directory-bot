#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/tg-directory-bot}"
APP_USER="${APP_USER:-tgbot}"
INSTANCE_NAME="${INSTANCE_NAME:-tg-directory-bot}"

if [[ $# -ne 1 ]]; then
  echo "Usage: sudo bash deploy/ubuntu/restore.sh /opt/tg-directory-bot/backups/directory-YYYYMMDD-HHMMSS.sqlite3"
  exit 1
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Please run as root."
  exit 1
fi

BACKUP="$1"

systemctl stop "$INSTANCE_NAME.service"
systemctl stop "$INSTANCE_NAME-web.service"
"$APP_DIR/.venv/bin/python" "$APP_DIR/scripts/restore.py" "$BACKUP" --yes
chown -R "$APP_USER:$APP_USER" "$APP_DIR/data"
systemctl start "$INSTANCE_NAME.service"
systemctl start "$INSTANCE_NAME-web.service"
systemctl status "$INSTANCE_NAME.service" --no-pager
systemctl status "$INSTANCE_NAME-web.service" --no-pager
