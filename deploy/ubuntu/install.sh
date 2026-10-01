#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/tg-directory-bot}"
APP_USER="${APP_USER:-tgbot}"
INSTANCE_NAME="${INSTANCE_NAME:-tg-directory-bot}"
BOT_SERVICE="$INSTANCE_NAME.service"
WEB_SERVICE="$INSTANCE_NAME-web.service"
BACKUP_SERVICE="$INSTANCE_NAME-backup.service"
BACKUP_TIMER="$INSTANCE_NAME-backup.timer"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Please run as root: sudo bash deploy/ubuntu/install.sh"
  exit 1
fi

cd "$APP_DIR"

if [[ "${SKIP_APT:-0}" != "1" ]]; then
  apt-get update
  apt-get install -y python3 python3-venv python3-pip sqlite3 openssl
fi

# 可选：ffmpeg 用于生成视频贴纸预览图（没有也能用，会改用缩略图）
if ! command -v ffmpeg >/dev/null 2>&1; then
  if ! DEBIAN_FRONTEND=noninteractive timeout 600 apt-get install -y --no-install-recommends ffmpeg >/dev/null 2>&1; then
    echo "提示：未能自动安装 ffmpeg（可选）。如需视频贴纸更清晰的预览，可手动执行：sudo apt-get update && sudo apt-get install -y ffmpeg"
  fi
fi

if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

mkdir -p "$APP_DIR/data" "$APP_DIR/backups"

if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.server.example" "$APP_DIR/.env"
  GENERATED_ADMIN_PASSWORD="$(openssl rand -base64 18 | tr -d '\n')"
  GENERATED_SESSION_SECRET="$(openssl rand -hex 32)"
  sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=$GENERATED_ADMIN_PASSWORD|" "$APP_DIR/.env"
  sed -i "s|^SESSION_SECRET=.*|SESSION_SECRET=$GENERATED_SESSION_SECRET|" "$APP_DIR/.env"
  echo "Created $APP_DIR/.env"
  echo "Generated web admin password: $GENERATED_ADMIN_PASSWORD"
  echo "Store this password now. It is also available in $APP_DIR/.env"
fi

"$PYTHON_BIN" -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

cat >"/etc/systemd/system/$BOT_SERVICE" <<EOF
[Unit]
Description=TG Directory Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
Environment=TZ=Asia/Shanghai
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/run.py
Restart=always
RestartSec=5
User=$APP_USER
Group=$APP_USER
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=full
ReadWritePaths=$APP_DIR/data $APP_DIR/backups

[Install]
WantedBy=multi-user.target
EOF

cat >"/etc/systemd/system/$WEB_SERVICE" <<EOF
[Unit]
Description=TG Directory Bot Web Admin
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
Environment=TZ=Asia/Shanghai
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/run_web.py
Restart=always
RestartSec=5
User=$APP_USER
Group=$APP_USER
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=full
ReadWritePaths=$APP_DIR/data $APP_DIR/backups

[Install]
WantedBy=multi-user.target
EOF

cat >"/etc/systemd/system/$BACKUP_SERVICE" <<EOF
[Unit]
Description=Backup TG Directory Bot SQLite database

[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
Environment=TZ=Asia/Shanghai
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/scripts/backup.py --out-dir $APP_DIR/backups --keep 30
User=$APP_USER
Group=$APP_USER
EOF

cat >"/etc/systemd/system/$BACKUP_TIMER" <<EOF
[Unit]
Description=Run TG Directory Bot backup every 6 hours

[Timer]
OnCalendar=*-*-* 00,06,12,18:00:00 Asia/Shanghai
Persistent=true

[Install]
WantedBy=timers.target
EOF

chown -R root:root "$APP_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/data" "$APP_DIR/backups"
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

systemctl daemon-reload
systemctl enable "$BOT_SERVICE"
systemctl enable "$WEB_SERVICE"
systemctl enable --now "$BACKUP_TIMER"

echo "Installed."
echo "Next:"
echo "  1. Edit $APP_DIR/.env"
echo "  2. Run: sudo systemctl restart $INSTANCE_NAME $INSTANCE_NAME-web"
echo "  3. Web admin uses WEB_PORT from $APP_DIR/.env"
echo "  4. Logs: sudo journalctl -u $INSTANCE_NAME -f"
