#!/usr/bin/env bash
# 随时手动备份：sudo tgbot-backup        立即备份并推送到私有 GitHub 仓库（和每 2 天的定时备份完全一样）
#               sudo tgbot-backup list   查看仓库里现存的备份（最新 3 份）
set -uo pipefail
STATE_DIR="${STATE_DIR:-/etc/tg-bot-backup}"
LIB_DIR="${LIB_DIR:-/usr/local/lib/tg-bot-backup}"
# shellcheck disable=SC1091
[[ -f "$STATE_DIR/runtime.env" ]] && . "$STATE_DIR/runtime.env"
APP_DIR="${APP_DIR:-/opt/tg-directory-bot}"
WORK_DIR="${WORK_DIR:-/var/lib/tg-bot-backup}"
REPO_URL="${BACKUP_REPO_URL:-git@github.com:davidmuller5273-boop/tg-bot-backup.git}"

if [[ "$(id -u)" -ne 0 && -z "${TGBOT_BACKUP_NO_SUDO:-}" ]]; then   # TGBOT_BACKUP_NO_SUDO 仅供测试
  exec sudo "$0" "$@"
fi
export APP_DIR STATE_DIR WORK_DIR BACKUP_REPO_URL="$REPO_URL"
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -i $STATE_DIR/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$STATE_DIR/known_hosts}"

list_backups() {
  local repo="$WORK_DIR/repo" src="GitHub 仓库"
  if [[ ! -d "$repo/.git" ]]; then echo "还没有做过备份（本机没有备份仓库的副本）。先运行：sudo tgbot-backup"; return 1; fi
  if ! git -C "$repo" fetch -q origin 2>/dev/null; then src="本机缓存（连不上 GitHub，可能不是最新）"; fi
  local ref="origin/main"; git -C "$repo" rev-parse -q --verify "$ref" >/dev/null || ref="HEAD"
  echo "备份列表（来源：$src）："
  local n=0 line size name
  while read -r line; do
    size="${line%% *}"; name="${line#* }"
    printf '  %s   %s MB\n' "$name" "$(awk -v b="$size" 'BEGIN{printf "%.1f", b/1048576}')"; n=$((n+1))
  done < <(git -C "$repo" ls-tree -l "$ref" 2>/dev/null | awk '$4 ~ /^[0-9]+$/ && $5 ~ /\.tar\.gz\.enc$/ {print $4" "$5}' | sort -k2 -r)
  (( n )) && echo "共 $n 份（只保留最新的几份，最上面的最新）。" || echo "  （还没有备份）"
}

case "${1:-run}" in
  list|ls) list_backups; exit $? ;;
  run|"")
    echo "开始备份……（数据库在线备份 → 压缩 → 加密 → 推送到 GitHub 私有仓库）"
    bash "$LIB_DIR/backup-to-github.sh"; rc=$?
    echo
    case "$rc" in
      0) echo "✅ 备份完成。"; list_backups ;;
      3) echo "❌ 备份已加密，但推送到 GitHub 失败：公钥还没添加（需要勾选写权限），或网络不通。添加好后再运行 sudo tgbot-backup。" ; exit 3 ;;
      4) echo "⚠️ 备份已推送，但有部分内容失败（见上面的「失败」行）。其余内容已安全保存。"; list_backups; exit 4 ;;
      *) echo "❌ 备份失败（退出码 $rc），详情见上面的输出。" ; exit "$rc" ;;
    esac ;;
  *) echo "用法：sudo tgbot-backup        立即备份"; echo "      sudo tgbot-backup list   查看现存备份"; exit 1 ;;
esac
