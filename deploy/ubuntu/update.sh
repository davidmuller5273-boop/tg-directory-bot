#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/tg-directory-bot}"
APP_USER="${APP_USER:-tgbot}"
INSTANCE_NAME="${INSTANCE_NAME:-tg-directory-bot}"
ARCHIVE="${1:-/tmp/tg-directory-bot-ubuntu.tar.gz}"
BOT_SERVICE="$INSTANCE_NAME.service"
WEB_SERVICE="$INSTANCE_NAME-web.service"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请使用 root 权限运行：sudo bash deploy/ubuntu/update.sh $ARCHIVE"
  exit 1
fi

if [[ ! -f "$ARCHIVE" ]]; then
  for candidate in \
    "/opt/$(basename "$ARCHIVE")" \
    "$PWD/$(basename "$ARCHIVE")" \
    "/root/$(basename "$ARCHIVE")"; do
    if [[ -f "$candidate" ]]; then
      ARCHIVE="$candidate"
      echo "已自动找到更新包：$ARCHIVE"
      break
    fi
  done
fi
if [[ ! -f "$ARCHIVE" ]]; then
  echo "找不到更新包：$(basename "$ARCHIVE")（已检查 /tmp、/opt、当前目录和 /root）"
  exit 1
fi

if [[ ! -d "$APP_DIR" || ! -f "$APP_DIR/.env" ]]; then
  echo "未找到已安装程序或 .env，请先执行首次安装。"
  exit 1
fi

if [[ -f "$ARCHIVE.sha256" ]]; then
  (cd "$(dirname "$ARCHIVE")" && sha256sum -c "$(basename "$ARCHIVE").sha256")
fi

TMP_DIR="$(mktemp -d)"
SERVICES_STOPPED=0

cleanup() {
  status=$?
  rm -rf "$TMP_DIR"
  if [[ $status -ne 0 && $SERVICES_STOPPED -eq 1 ]]; then
    echo "更新失败，正在尝试恢复启动旧服务。"
    systemctl start "$BOT_SERVICE" "$WEB_SERVICE" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT

tar -tzf "$ARCHIVE" >/dev/null
tar -xzf "$ARCHIVE" -C "$TMP_DIR"
SOURCE_DIR="$TMP_DIR/tg-directory-bot"

for required in run.py requirements.txt deploy/ubuntu/install.sh; do
  if [[ ! -f "$SOURCE_DIR/$required" ]]; then
    echo "更新包不完整，缺少：$required"
    exit 1
  fi
done

mkdir -p "$APP_DIR/backups" "$APP_DIR/data"
if [[ -f "$APP_DIR/data/directory.sqlite3" ]]; then
  if [[ -x "$APP_DIR/.venv/bin/python" && -f "$APP_DIR/scripts/backup.py" ]]; then
    "$APP_DIR/.venv/bin/python" "$APP_DIR/scripts/backup.py" \
      --db "$APP_DIR/data/directory.sqlite3" \
      --out-dir "$APP_DIR/backups" \
      --keep 30
  else
    stamp="$(TZ=Asia/Shanghai date +%Y%m%d-%H%M%S)"
    sqlite3 "$APP_DIR/data/directory.sqlite3" ".backup '$APP_DIR/backups/directory-$stamp.sqlite3'"
  fi
fi

systemctl stop "$BOT_SERVICE" "$WEB_SERVICE" || true
SERVICES_STOPPED=1

# 保留配置、数据库、备份和虚拟环境，其余旧程序文件全部删除后覆盖。
find "$APP_DIR" -mindepth 1 -maxdepth 1 \
  ! -name '.env' \
  ! -name '.venv' \
  ! -name 'data' \
  ! -name 'backups' \
  -exec rm -rf -- {} +
cp -a "$SOURCE_DIR"/. "$APP_DIR"/

SKIP_APT=1 APP_DIR="$APP_DIR" APP_USER="$APP_USER" INSTANCE_NAME="$INSTANCE_NAME" \
  bash "$APP_DIR/deploy/ubuntu/install.sh"

systemctl restart "$BOT_SERVICE" "$WEB_SERVICE"
SERVICES_STOPPED=0

echo
echo "更新完成。"
systemctl --no-pager --full status "$BOT_SERVICE" "$WEB_SERVICE" || true
echo
echo "查看机器人日志：sudo journalctl -u $INSTANCE_NAME -f"
echo "查看后台日志：  sudo journalctl -u $INSTANCE_NAME-web -f"
