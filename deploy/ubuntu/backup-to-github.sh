#!/usr/bin/env bash
# 备份机器人数据 -> 压缩 -> 加密 -> 推送到私有 GitHub 仓库（只推加密文件）。
# 由 systemd 定时器 tg-bot-backup.timer 每 2 天运行一次，也可手动：sudo systemctl start tg-bot-backup
#
# 退出码：0 成功；3 = 推送/拉取 GitHub 失败（常见原因：部署公钥还没添加）；其它非 0 = 备份本身失败。
# 日志全部走 stdout/stderr -> journald；失败时 `systemctl status tg-bot-backup` 能看到。
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/tg-directory-bot}"
STATE_DIR="${STATE_DIR:-/etc/tg-bot-backup}"        # 口令、SSH 部署私钥、配置（root 专用，不放 APP_DIR：更新程序会清空 APP_DIR）
# shellcheck disable=SC1091
[[ -f "$STATE_DIR/runtime.env" ]] && . "$STATE_DIR/runtime.env"   # setup 写入的 APP_DIR / 仓库地址（外部环境变量优先）
# shellcheck disable=SC1091
[[ -f "$STATE_DIR/backup.conf" ]] && . "$STATE_DIR/backup.conf"
DATA_DIR="${DATA_DIR:-$APP_DIR/data}"
PASS_FILE="${PASS_FILE:-$STATE_DIR/backup.pass}"
SSH_KEY="${SSH_KEY:-$STATE_DIR/id_ed25519}"
WORK_DIR="${WORK_DIR:-/var/lib/tg-bot-backup}"
REPO_URL="${BACKUP_REPO_URL:-git@github.com:davidmuller5273-boop/tg-bot-backup.git}"
BRANCH="${BACKUP_BRANCH:-main}"
KEEP="${BACKUP_KEEP:-3}"                             # 仓库里只保留最新 N 份加密备份
MAX_COMMITS="${BACKUP_MAX_COMMITS:-6}"               # 提交数超过它就把历史压成 1 个全新提交（见下）
MAX_FILE_MB="${BACKUP_MAX_FILE_MB:-90}"              # GitHub 单文件上限 100MB
PREFIX="tg-bot-backup"
HOOKS_DIR="${BACKUP_HOOKS_DIR:-/etc/tg-bot-backup.d}"  # 钩子：每个可执行的 *.sh 都会被运行（见 restore-backup.md「钩子约定」）
HOOK_TIMEOUT="${BACKUP_HOOK_TIMEOUT:-900}"
PROBLEMS=()   # 非致命问题（某个库/钩子失败）：不中断其它部分，最后统一报告并以退出码 4 结束

log() { echo "[backup] $*"; }
die() { echo "[backup] 错误：$*" >&2; exit "${2:-1}"; }

[[ "$KEEP" =~ ^[1-9][0-9]*$ ]] || die "BACKUP_KEEP 必须是正整数（当前：$KEEP）"
[[ "$MAX_COMMITS" =~ ^[1-9][0-9]*$ ]] || die "BACKUP_MAX_COMMITS 必须是正整数"
[[ -s "$PASS_FILE" ]] || die "找不到口令文件 $PASS_FILE（请先运行 setup-backup.sh）"
[[ -d "$DATA_DIR" ]] || die "找不到数据目录 $DATA_DIR"
for tool in openssl tar git python3 flock; do command -v "$tool" >/dev/null 2>&1 || die "缺少命令：$tool"; done

mkdir -p "$WORK_DIR"; chmod 700 "$WORK_DIR"
exec 9>"$WORK_DIR/.lock"
flock -n 9 || die "另一个备份正在运行"

TMP="$(mktemp -d "$WORK_DIR/tmp.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
STAGE="$TMP/stage"; mkdir -p "$STAGE"
umask 077

# ---------- 1. 找数据库（data 目录下所有 SQLite，含 clones/；跳过 clones/deleted 归档），再加上 .env 里 DB_PATH 指向的库 ----------
DBS=()
while IFS= read -r -d '' f; do DBS+=("$f"); done < <(find "$DATA_DIR" -type f \( -name '*.sqlite3' -o -name '*.sqlite' -o -name '*.db' \) ! -path '*/deleted/*' -print0 | sort -z)
if [[ -f "$APP_DIR/.env" ]]; then   # 只取 DB_PATH 这一行，不读其它内容
  envdb="$(grep -E '^DB_PATH=' "$APP_DIR/.env" | tail -n1 | cut -d= -f2- | sed -e 's/\r$//' -e 's/^["'"'"']//' -e 's/["'"'"']$//' || true)"
  if [[ -n "$envdb" ]]; then
    [[ "$envdb" = /* ]] || envdb="$APP_DIR/$envdb"
    if [[ -f "$envdb" && "$envdb" != "$DATA_DIR"/* ]]; then DBS+=("$envdb"); fi
  fi
fi
[[ ${#DBS[@]} -gt 0 ]] || { log "警告：没有找到任何 SQLite 数据库（$DATA_DIR）"; PROBLEMS+=("没有找到数据库"); }

sqlite_backup() {   # 在线一致性备份（机器人运行中也安全）：优先 sqlite3 命令，没有就用 python 的 backup API
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$1" ".backup '$2'"
  else
    python3 - "$1" "$2" <<'PY'
import sqlite3, sys
src = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
  fi
}
sqlite_check() {
  python3 - "$1" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1]); r = c.execute("PRAGMA integrity_check").fetchone()[0]; c.close()
sys.exit(0 if r == "ok" else 1)
PY
}

for db in "${DBS[@]}"; do
  if [[ "$db" == "$DATA_DIR"/* ]]; then rel="data/${db#"$DATA_DIR"/}"; else rel="data/extra/$(basename "$db")"; fi
  mkdir -p "$STAGE/$(dirname "$rel")"
  log "备份数据库 $db"
  if ! sqlite_backup "$db" "$STAGE/$rel" || ! sqlite_check "$STAGE/$rel"; then
    log "失败：数据库 $db 备份或完整性检查未通过（已跳过，继续其它部分）"
    rm -f "$STAGE/$rel"; PROBLEMS+=("数据库 $db")
  fi
done

# ---------- 1a. 主项目的 .env（含令牌/密码；只会进入加密压缩包，权限 600 原样保留；内容不会打印）----------
if [[ -f "$APP_DIR/.env" ]]; then
  mkdir -p "$STAGE/app"
  cp -p "$APP_DIR/.env" "$STAGE/app/.env"; chmod 600 "$STAGE/app/.env"
  log "已加入 $APP_DIR/.env（仅在加密包内）"
else
  log "警告：找不到 $APP_DIR/.env，备份里不会有它"; PROBLEMS+=("缺少 .env")
fi

# ---------- 1b. 钩子（例如另一个机器人的 MySQL 导出）----------
# 约定：运行 $HOOKS_DIR/*.sh（需可执行，按文件名顺序，root 身份），第 1 个参数是暂存目录；
# 钩子把文件写到 "$1/<钩子名>/"（<钩子名>=文件名去掉 .sh）。钩子失败只记录，不影响其它部分；
# 失败钩子的半成品输出会被丢弃。钩子输出里的 .env/配置文件原样保留（含权限），不做任何剔除。
HOOKS_RAN=()
if [[ -d "$HOOKS_DIR" ]]; then
  while IFS= read -r -d '' hook; do
    hname="$(basename "$hook" .sh)"
    if [[ ! -x "$hook" ]]; then log "跳过钩子 $hook（没有可执行权限）"; continue; fi
    if [[ "$hname" == "data" || "$hname" == "MANIFEST.txt" ]]; then log "跳过钩子 $hook（名字与保留目录冲突）"; PROBLEMS+=("钩子 $hname（名字保留）"); continue; fi
    log "运行钩子 $hname"
    mkdir -p "$STAGE/$hname"
    if BACKUP_STAGE_DIR="$STAGE" timeout "$HOOK_TIMEOUT" "$hook" "$STAGE"; then
      HOOKS_RAN+=("$hname")
    else
      log "失败：钩子 $hname 退出码 $?（已丢弃它的输出，继续其它部分）"
      rm -rf "$STAGE/$hname"; PROBLEMS+=("钩子 $hname")
    fi
  done < <(find "$HOOKS_DIR" -maxdepth 1 -type f -name '*.sh' -print0 | sort -z)
fi
find "$STAGE" -mindepth 1 -type d -empty -delete 2>/dev/null || true
[[ -n "$(find "$STAGE" -type f -print -quit)" ]] || die "没有任何内容可备份"

MANIFEST="$TMP/MANIFEST.txt"
{
  echo "tg-bot-backup"; echo "created: $(TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M:%S %Z')"; echo "host: $(hostname)"
  echo "hooks: ${HOOKS_RAN[*]:-(none)}"
  echo "problems: ${PROBLEMS[*]:-(none)}"
  echo "included: app/.env (mode 600), data/*.sqlite3, hook outputs"
  echo "skipped (by design): backups/ 目录、data/clones/deleted/ 归档、日志、*.sqlite3-wal/-shm（已并入在线备份）"
  echo "files (sha256):"
  (cd "$STAGE" && find . -type f -print0 | sort -z | xargs -0 sha256sum)
} >"$MANIFEST"
mv "$MANIFEST" "$STAGE/MANIFEST.txt"

# ---------- 2. 压缩 + 加密（AES-256-CBC + PBKDF2，口令来自 root 专用文件） ----------
STAMP="$(TZ=Asia/Shanghai date +%Y%m%d-%H%M%S)"
NAME="$PREFIX-$STAMP.tar.gz.enc"
tar -C "$STAGE" -czf "$TMP/plain.tar.gz" .
openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass "file:$PASS_FILE" -in "$TMP/plain.tar.gz" -out "$TMP/$NAME"
rm -f "$TMP/plain.tar.gz"
size_mb=$(( ($(stat -c %s "$TMP/$NAME") + 1048575) / 1048576 ))
log "已加密：$NAME（约 ${size_mb}MB）"
(( size_mb <= MAX_FILE_MB )) || die "加密备份 ${size_mb}MB，超过 GitHub 单文件限制（${MAX_FILE_MB}MB），请联系开发者改用分卷或其它存储"

# ---------- 3. 推送（仓库里只放加密文件） ----------
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -i $SSH_KEY -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$STATE_DIR/known_hosts}"
REPO="$WORK_DIR/repo"
if [[ ! -d "$REPO/.git" ]]; then
  rm -rf "$REPO"
  git clone -q "$REPO_URL" "$REPO" 2>"$TMP/git.err" || { cat "$TMP/git.err" >&2; die "无法访问备份仓库（公钥是否已添加为可写的部署密钥？）" 3; }
fi
cd "$REPO"
git config user.name "tg-bot-backup"; git config user.email "tg-bot-backup@localhost"
git remote set-url origin "$REPO_URL"
git fetch -q origin 2>"$TMP/git.err" || { cat "$TMP/git.err" >&2; die "无法从备份仓库拉取" 3; }
if git rev-parse -q --verify "origin/$BRANCH" >/dev/null; then
  git checkout -q -B "$BRANCH" "origin/$BRANCH"
else
  git checkout -q -B "$BRANCH"        # 空仓库的第一次
fi

cp "$TMP/$NAME" "$REPO/$NAME"
# 滚动保留：文件名带日期，按名字排序，只留最新 KEEP 个，其余删除
mapfile -t ALL < <(ls -1 "$REPO" | grep -E "^$PREFIX-[0-9]{8}-[0-9]{6}\.tar\.gz\.enc$" | sort)
if (( ${#ALL[@]} > KEEP )); then
  for old in "${ALL[@]:0:${#ALL[@]}-KEEP}"; do log "删除旧备份 $old"; git rm -q -f -- "$old"; done
fi
cat >"$REPO/README.md" <<README
加密备份仓库（私有）。仓库里只有 AES-256 加密文件 ${PREFIX}-日期.tar.gz.enc（最新 ${KEEP} 份），没有密钥/口令。
解密方法见主仓库 deploy/ubuntu/restore-backup.md。没有口令就无法恢复，请把口令另外保存好。
README
git add -A
git commit -q -m "backup $STAMP" || true

# 防止历史无限增长：旧备份文件虽然被删，但仍留在旧提交里。提交数超过 MAX_COMMITS 时，
# 把分支重建成「一个全新的孤儿提交」（只含当前这几份文件）并强制推送，旧历史随之丢弃。
if (( $(git rev-list --count HEAD) > MAX_COMMITS )); then
  log "提交数超过 $MAX_COMMITS，压缩历史为单个提交"
  git checkout -q --orphan squashed
  git add -A
  git commit -q -m "backup snapshot $STAMP (history squashed)"
  git branch -q -M "$BRANCH"
  git push -q --force origin "$BRANCH" 2>"$TMP/git.err" || { cat "$TMP/git.err" >&2; die "推送失败（公钥是否有写权限？）" 3; }
else
  git push -q origin "HEAD:$BRANCH" 2>"$TMP/git.err" || { cat "$TMP/git.err" >&2; die "推送失败（公钥是否有写权限？）" 3; }
fi
git gc -q --prune=now 2>/dev/null || true
log "完成：仓库里现有 $(ls -1 "$REPO" | grep -cE "\.tar\.gz\.enc$") 份加密备份（最多保留 $KEEP 份）"
if (( ${#PROBLEMS[@]} )); then
  log "备份已推送，但有部分失败：${PROBLEMS[*]}"
  exit 4
fi
