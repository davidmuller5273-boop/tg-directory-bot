#!/usr/bin/env bash
# 一键安装「每 2 天加密备份到私有 GitHub 仓库」。可重复运行（幂等）。
#   curl -fsSL https://raw.githubusercontent.com/davidmuller5273-boop/tg-directory-bot/main/deploy/ubuntu/setup-backup.sh | sudo bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/tg-directory-bot}"
STATE_DIR="${STATE_DIR:-/etc/tg-bot-backup}"   # 口令/私钥放这里；不能放 APP_DIR（update-from-git.sh 会清空它）
REPO_URL="${BACKUP_REPO_URL:-git@github.com:davidmuller5273-boop/tg-bot-backup.git}"
RAW_BASE="https://raw.githubusercontent.com/davidmuller5273-boop/tg-directory-bot/main/deploy/ubuntu"
LIB_DIR="/usr/local/lib/tg-bot-backup"
UNIT="tg-bot-backup"

[[ "$(id -u)" -eq 0 ]] || { echo "请用 sudo 运行" >&2; exit 1; }

# 1. 依赖
need=()
command -v sqlite3 >/dev/null 2>&1 || need+=(sqlite3)
command -v openssl >/dev/null 2>&1 || need+=(openssl)
command -v git >/dev/null 2>&1 || need+=(git)
command -v ssh-keygen >/dev/null 2>&1 || need+=(openssh-client)
command -v python3 >/dev/null 2>&1 || need+=(python3)
if (( ${#need[@]} )); then
  echo "安装依赖：${need[*]}"
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}"
fi

# 2. 状态目录、口令、部署密钥（已存在就不动）
install -d -m 700 "$STATE_DIR"
# 备份口令：用户用环境变量 BACKUP_PASS 自己指定（推荐，不会丢）；没给才随机生成。
#   curl -fsSL URL | sudo BACKUP_PASS='你的密码' bash
# 口令只用 printf '%s' 写入文件（不经过 eval/echo，特殊字符安全），任何地方都不会回显。
PASS_MODE=keep
if [[ -n "${BACKUP_PASS+x}" ]]; then
  # 按「字符」数（不是字节）检查长度；口令不能含换行（openssl 只读第一行）
  if ! BACKUP_PASS="$BACKUP_PASS" python3 -c 'import os,sys; p=os.environ["BACKUP_PASS"]; sys.exit(0 if len(p)>=8 and "\n" not in p and "\r" not in p else 1)'; then
    if [[ -z "$BACKUP_PASS" ]]; then echo "备份密码不能为空，请至少设置 8 个字符。" >&2
    else echo "备份密码太短（或含换行），请至少设置 8 个字符（不要包含换行）。" >&2; fi
    exit 1
  fi
  if [[ -s "$STATE_DIR/backup.pass" ]] && [[ "$(cat "$STATE_DIR/backup.pass")" != "$BACKUP_PASS" ]]; then PASS_MODE=changed; else PASS_MODE=custom; fi
  ( umask 077; printf '%s' "$BACKUP_PASS" >"$STATE_DIR/backup.pass" )
  unset BACKUP_PASS
elif [[ ! -s "$STATE_DIR/backup.pass" ]]; then
  ( umask 077; openssl rand -base64 33 | tr -d '\n' >"$STATE_DIR/backup.pass" )
  PASS_MODE=random
fi
chmod 600 "$STATE_DIR/backup.pass"
if [[ ! -f "$STATE_DIR/id_ed25519" ]]; then
  ssh-keygen -q -t ed25519 -N '' -C "tg-bot-backup@$(hostname)" -f "$STATE_DIR/id_ed25519"
fi
chmod 600 "$STATE_DIR/id_ed25519"
if [[ ! -f "$STATE_DIR/backup.conf" ]]; then
  cat >"$STATE_DIR/backup.conf" <<CONF
# 备份设置（可改）。仓库里只保留最新 BACKUP_KEEP 份加密备份。
BACKUP_KEEP="\${BACKUP_KEEP:-3}"
CONF
  chmod 600 "$STATE_DIR/backup.conf"
fi

# 3. 安装备份脚本（本地有就用本地，否则从 GitHub 下载）
install -d -m 755 "$LIB_DIR"
SELF_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; fi
for name in backup-to-github.sh restore-backup.sh tgbot-backup.sh; do
  if [[ -n "$SELF_DIR" && -f "$SELF_DIR/$name" ]]; then cp "$SELF_DIR/$name" "$LIB_DIR/$name"
  elif [[ -f "$APP_DIR/deploy/ubuntu/$name" && -z "${BACKUP_FORCE_DOWNLOAD:-}" ]]; then cp "$APP_DIR/deploy/ubuntu/$name" "$LIB_DIR/$name"
  else curl -fsSL "$RAW_BASE/$name" -o "$LIB_DIR/$name"; fi
  chmod 755 "$LIB_DIR/$name"
done

ln -sf "$LIB_DIR/tgbot-backup.sh" /usr/local/bin/tgbot-backup   # 随时手动备份：sudo tgbot-backup
install -d -m 755 /etc/tg-bot-backup.d   # 钩子目录：放可执行的 *.sh（例如导出另一个机器人的 MySQL），见 restore-backup.md
cat >"$STATE_DIR/runtime.env" <<RT
APP_DIR="\${APP_DIR:-$APP_DIR}"
BACKUP_REPO_URL="\${BACKUP_REPO_URL:-$REPO_URL}"
RT
chmod 600 "$STATE_DIR/runtime.env"

# 4. systemd 服务 + 定时器（每 2 天 04:00 上海时间；错过会补跑）
cat >"/etc/systemd/system/$UNIT.service" <<SERVICE
[Unit]
Description=Encrypted backup of tg-directory-bot data to private GitHub repo
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=APP_DIR=$APP_DIR
Environment=STATE_DIR=$STATE_DIR
Environment=BACKUP_REPO_URL=$REPO_URL
ExecStart=/bin/bash $LIB_DIR/backup-to-github.sh
Nice=10
IOSchedulingClass=idle
SERVICE
cat >"/etc/systemd/system/$UNIT.timer" <<TIMER
[Unit]
Description=Run $UNIT every 2 days

[Timer]
# 每月 1、3、5…31 号 04:00（月底 31 号→次月 1 号会连着两天，无害）
OnCalendar=*-*-1/2 04:00:00 Asia/Shanghai
RandomizedDelaySec=10min
Persistent=true

[Install]
WantedBy=timers.target
TIMER
systemctl daemon-reload
systemctl enable --now "$UNIT.timer" >/dev/null

# 5. 说明 + 公钥
echo
echo "==================== 备份口令（请务必记住/保存） ===================="
case "$PASS_MODE" in
  custom) echo "已使用你指定的备份密码。解密备份时用的就是同一个密码。" ;;
  changed) echo "已改用你新指定的备份密码。注意：改密码之前生成的旧备份仍然需要「旧密码」才能解密；新备份用新密码。" ;;
  random) echo "你没有指定密码，已自动生成一个随机备份口令（建议下次用 BACKUP_PASS='你的密码' 自己指定，更不容易丢）。" ;;
  *) echo "备份口令已存在（沿用之前的）。" ;;
esac
echo "备份文件是加密的：没有这个密码，备份就无法恢复！"
echo "如需查看当前口令（单行命令）：  sudo cat $STATE_DIR/backup.pass"
echo "=================================================================="
echo
echo "下面这一行是部署公钥，请整行发给开发者（它是公钥，可以公开）："
cat "$STATE_DIR/id_ed25519.pub"
echo

# 6. 密钥可用才做第一次备份
export GIT_SSH_COMMAND="ssh -i $STATE_DIR/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$STATE_DIR/known_hosts"
if ! git ls-remote "$REPO_URL" >/dev/null 2>&1; then
  echo "公钥还没添加，等通知后再运行 sudo systemctl start tg-bot-backup （或 sudo tgbot-backup）"
  echo "（定时器已安装，之后每 2 天自动备份；公钥添加前的定时运行会显示失败，属正常）"
  exit 0
fi
echo "公钥可用，正在做第一次备份……"
if systemctl start "$UNIT.service"; then
  echo "第一次备份成功。以后随时手动备份：sudo tgbot-backup ；查看备份：sudo tgbot-backup list"
  echo "下次自动备份时间："
  systemctl list-timers "$UNIT.timer" --no-pager | head -n 3
else
  echo "公钥还没添加，或还没有写权限（需勾选 Allow write access）。添加好后再运行 sudo systemctl start tg-bot-backup"
  echo "查看原因：sudo journalctl -u $UNIT -n 30 --no-pager"
fi
exit 0
