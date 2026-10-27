#!/usr/bin/env bash
# 解密并解压一份加密备份到一个临时目录，并对里面每个数据库做完整性检查。不会改动线上数据。
# 用法：bash restore-backup.sh 备份文件.tar.gz.enc [输出目录，默认 /tmp/tg-restore]
# 口令：默认读 /etc/tg-bot-backup/backup.pass；换新服务器时可用 PASS_FILE=口令文件路径、环境变量 BACKUP_PASS，或不设置而按提示输入（就是你设置备份时用的同一个密码）。
set -Eeuo pipefail
FILE="${1:?用法：restore-backup.sh 备份文件.tar.gz.enc [输出目录]}"
OUT="${2:-/tmp/tg-restore}"
PASS_FILE="${PASS_FILE:-/etc/tg-bot-backup/backup.pass}"
[[ -f "$FILE" ]] || { echo "找不到文件 $FILE" >&2; exit 1; }
mkdir -p "$OUT"
if [[ -s "$PASS_FILE" ]]; then
  PASS_ARGS=(-pass "file:$PASS_FILE")
elif [[ -n "${BACKUP_PASS:-}" ]]; then
  export BACKUP_PASS; PASS_ARGS=(-pass env:BACKUP_PASS)
else
  read -rsp "请输入备份口令: " BACKUP_PASS </dev/tty; echo
  export BACKUP_PASS; PASS_ARGS=(-pass env:BACKUP_PASS)
fi
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 "${PASS_ARGS[@]}" -in "$FILE" | tar -xzp -C "$OUT" \
  || { echo "解密失败：口令不对或文件损坏" >&2; exit 1; }
bad=0
while IFS= read -r -d '' db; do
  r="$(python3 -c 'import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("PRAGMA integrity_check").fetchone()[0])' "$db" 2>&1 || true)"
  echo "完整性检查 ${db#"$OUT"/}: $r"
  [[ "$r" == "ok" ]] || bad=1
done < <(find "$OUT/data" -type f \( -name '*.sqlite3' -o -name '*.sqlite' -o -name '*.db' \) -print0)
(( bad == 0 )) || { echo "有数据库未通过完整性检查" >&2; exit 2; }
echo "已解压到 $OUT （data/ 下是数据库，app/.env 是机器人配置（含密钥，注意保密），MANIFEST.txt 是清单）"
