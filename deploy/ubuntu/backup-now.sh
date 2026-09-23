#!/usr/bin/env bash
set -euo pipefail

INSTANCE_NAME="${INSTANCE_NAME:-tg-directory-bot}"
systemctl start "$INSTANCE_NAME-backup.service"
journalctl -u "$INSTANCE_NAME-backup.service" -n 50 --no-pager
