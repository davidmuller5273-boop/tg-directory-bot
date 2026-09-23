#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/tg-directory-bot}"
APP_USER="${APP_USER:-tgbot}"
INSTANCE_NAME="${INSTANCE_NAME:-tg-directory-bot}"
REPO_URL="${REPO_URL:-https://github.com/davidmuller5273-boop/tg-directory-bot.git}"
BRANCH="${BRANCH:-main}"
BOT_SERVICE="$INSTANCE_NAME.service"
WEB_SERVICE="$INSTANCE_NAME-web.service"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请使用 root 权限运行：sudo bash deploy/ubuntu/update-from-git.sh"
  exit 1
fi

if [[ ! -d "$APP_DIR" || ! -f "$APP_DIR/.env" ]]; then
  echo "未找到已安装程序或 .env，请先执行首次安装。"
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  apt-get update
  apt-get install -y git
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

echo "正在从 Git 拉取：$REPO_URL （分支 $BRANCH）"
git clone --depth 1 -b "$BRANCH" "$REPO_URL" "$TMP_DIR/repo"
SOURCE_DIR="$TMP_DIR/repo"

for required in run.py requirements.txt deploy/ubuntu/install.sh; do
  if [[ ! -f "$SOURCE_DIR/$required" ]]; then
    echo "仓库内容不完整，缺少：$required"
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
# 不要把 .git 留在生产目录
rm -rf "$APP_DIR/.git"

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
