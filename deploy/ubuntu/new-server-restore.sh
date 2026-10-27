#!/usr/bin/env bash
# 新服务器一条命令：从加密备份（私有仓库 tg-bot-backup）完整恢复机器人。可重复运行（幂等）。
#
#   curl -fsSL https://raw.githubusercontent.com/davidmuller5273-boop/tg-directory-bot/main/deploy/ubuntu/new-server-restore.sh | sudo BACKUP_PASS='你的备份密码' bash
#
# 第一次运行：生成一把「只读」部署公钥并打印，等它被添加到备份仓库后，再运行同一条命令即可继续。
# 备份密码只通过环境变量传入，不会被打印。
# 整个脚本放在 main 函数里：curl | bash 时，子命令不会误吞脚本本身的 stdin。
main() {
set -Eeuo pipefail
exec </dev/null

APP_DIR="${APP_DIR:-/opt/tg-directory-bot}"
APP_USER="${APP_USER:-tgbot}"
INSTANCE_NAME="${INSTANCE_NAME:-tg-directory-bot}"
STATE_DIR="${STATE_DIR:-/etc/tg-bot-backup}"
RESTORE_DIR="${RESTORE_DIR:-/tmp/tg-restore}"
BACKUP_REPO_URL="${BACKUP_REPO_URL:-git@github.com:davidmuller5273-boop/tg-bot-backup.git}"
REPO_URL="${REPO_URL:-https://github.com/davidmuller5273-boop/tg-directory-bot.git}"
RAW_BASE="${RAW_BASE:-https://raw.githubusercontent.com/davidmuller5273-boop/tg-directory-bot/main/deploy/ubuntu}"
SAFE_REPO_HTTPS="${SAFE_REPO_HTTPS:-https://github.com/davidmuller5273-boop/safe.git}"
SAFE_REPO_SSH="${SAFE_REPO_SSH:-git@github.com:davidmuller5273-boop/safe.git}"
HOOKS_DIR="${RESTORE_HOOKS_DIR:-/etc/tg-bot-backup.restore.d}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
SERVICE_WAIT="${SERVICE_WAIT:-5}"
KEY="$STATE_DIR/restore_ed25519"       # 只读部署密钥（恢复专用，和之后备份用的写密钥不同）
PROBLEMS=()

say()  { echo "▶ $*"; }
ok()   { echo "  ✔ $*"; }
warn() { echo "  ⚠ $*" >&2; }
fail() { echo "❌ $*" >&2; exit 1; }

[[ -n "${NEW_SERVER_TEST:-}" || "$(id -u)" -eq 0 ]] || fail "请用 sudo 运行（命令里要有 sudo）。"
[[ -n "${BACKUP_PASS:-}" ]] || fail "没有备份密码。请这样运行：... | sudo BACKUP_PASS='你的备份密码' bash"

# ---------- 1. 依赖 ----------
say "第 1 步：检查/安装依赖"
if [[ -z "${NEW_SERVER_TEST:-}" && -z "${SKIP_APT:-}" ]]; then
  need=()
  for pair in git:git openssl:openssl python3:python3 sqlite3:sqlite3 ssh-keygen:openssh-client curl:curl; do
    command -v "${pair%%:*}" >/dev/null 2>&1 || need+=("${pair##*:}")
  done
  python3 -c 'import venv, ensurepip' >/dev/null 2>&1 || need+=(python3-venv python3-pip)
  if (( ${#need[@]} )); then
    DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}" \
      || fail "依赖安装失败：${need[*]}"
  fi
fi
ok "依赖就绪"

# ---------- 2. 读取备份仓库用的只读部署密钥 ----------
say "第 2 步：准备访问私有备份仓库的密钥"
install -d -m 700 "$STATE_DIR"
if [[ ! -f "$KEY" ]]; then
  ssh-keygen -q -t ed25519 -N '' -C "tg-bot-restore@$(hostname)" -f "$KEY" || fail "生成密钥失败"
fi
chmod 600 "$KEY"
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$STATE_DIR/known_hosts}"
export GIT_TERMINAL_PROMPT=0
if ! timeout 90 git ls-remote "$BACKUP_REPO_URL" >/dev/null 2>&1; then
  echo
  echo "下面这一行是这台新服务器的「只读」公钥，请整行发给开发者（公钥可以公开）："
  cat "$KEY.pub"
  echo
  echo "公钥已生成，添加后重新运行同一条命令（备份密码不变）。"
  exit 0
fi
ok "可以访问备份仓库"

# ---------- 3. 取最新备份并解密 ----------
say "第 3 步：下载并解密最新备份"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
timeout 600 git clone -q --depth 1 "$BACKUP_REPO_URL" "$WORK/repo" || fail "下载备份仓库失败（网络问题？）"
NEWEST="$(ls -1 "$WORK/repo" 2>/dev/null | grep -E '^tg-bot-backup-[0-9]{8}-[0-9]{6}\.tar\.gz\.enc$' | sort | tail -n1 || true)"
[[ -n "$NEWEST" ]] || fail "备份仓库里没有任何备份文件（tg-bot-backup-*.tar.gz.enc）。请先在旧服务器运行 sudo tgbot-backup。"
ok "使用最新备份：$NEWEST"
rm -rf "$RESTORE_DIR"; mkdir -p "$RESTORE_DIR"; chmod 700 "$RESTORE_DIR"
export BACKUP_PASS
if ! openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:BACKUP_PASS -in "$WORK/repo/$NEWEST" 2>/dev/null | tar -xzp -C "$RESTORE_DIR" 2>/dev/null; then
  rm -rf "$RESTORE_DIR"
  fail "解密失败：备份密码不对（或备份文件损坏）。请核对密码后重新运行同一条命令。"
fi
[[ -f "$RESTORE_DIR/app/.env" ]] || fail "备份里没有 app/.env，无法完整恢复（备份不完整）。"
ok "解密成功，解压到 $RESTORE_DIR（里面有机密，最后请删除）"

# ---------- 4. 完整性检查 ----------
say "第 4 步：检查数据库完整性"
dbs=0
while IFS= read -r -d '' db; do
  r="$(python3 -c 'import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("PRAGMA integrity_check").fetchone()[0])' "$db" 2>&1 || true)"
  [[ "$r" == "ok" ]] || fail "数据库未通过完整性检查：${db#"$RESTORE_DIR"/}（$r）。已停止，没有改动这台服务器。"
  dbs=$((dbs+1))
done < <(find "$RESTORE_DIR/data" -type f \( -name '*.sqlite3' -o -name '*.sqlite' -o -name '*.db' \) -print0 2>/dev/null)
(( dbs > 0 )) || fail "备份里没有数据库。"
ok "$dbs 个数据库全部 ok"

# ---------- 5. 程序代码 ----------
say "第 5 步：安装机器人程序"
if [[ ! -f "$APP_DIR/run.py" ]]; then
  mkdir -p "$APP_DIR"; chmod 755 "$APP_DIR"
  timeout 600 git clone -q --depth 1 "$REPO_URL" "$WORK/code" || fail "下载程序代码失败：$REPO_URL"
  cp -a "$WORK/code"/. "$APP_DIR"/
  rm -rf "$APP_DIR/.git"
  ok "程序已放到 $APP_DIR"
else
  ok "$APP_DIR 已有程序，保持不变（以后更新用 update-from-git.sh）"
fi

# ---------- 6. 恢复数据和 .env ----------
say "第 6 步：恢复数据库和 .env"
MARK="$STATE_DIR/restored-from"
mkdir -p "$APP_DIR/data"
if [[ -f "$MARK" && "$(cat "$MARK")" == "$NEWEST" ]]; then
  ok "这份备份已经恢复过，跳过（重新恢复请先删除 $MARK）"
else
  if [[ -z "${FORCE_RESTORE:-}" ]] && { [[ -f "$APP_DIR/.env" ]] || [[ -n "$(find "$APP_DIR/data" -maxdepth 2 -name '*.sqlite3' -print -quit 2>/dev/null)" ]]; }; then
    fail "这台服务器上已经有机器人数据或 .env，为避免覆盖，已停止。确定要用备份覆盖的话，在命令里加上 FORCE_RESTORE=1。"
  fi
  "$SYSTEMCTL" stop "$INSTANCE_NAME" "$INSTANCE_NAME-web" >/dev/null 2>&1 || true
  rm -f "$APP_DIR"/data/*.sqlite3-wal "$APP_DIR"/data/*.sqlite3-shm "$APP_DIR"/data/clones/*.sqlite3-wal "$APP_DIR"/data/clones/*.sqlite3-shm 2>/dev/null || true
  cp -a "$RESTORE_DIR/data/." "$APP_DIR/data/"
  install -m 600 "$RESTORE_DIR/app/.env" "$APP_DIR/.env"
  printf '%s' "$NEWEST" >"$MARK"; chmod 600 "$MARK"
  ok "已恢复 data/ 和 .env（权限 600）"
fi

# ---------- 7. 安装服务并启动 ----------
say "第 7 步：安装并启动服务"
if [[ -f "$APP_DIR/deploy/ubuntu/install.sh" ]]; then
  SKIP_APT=1 APP_DIR="$APP_DIR" APP_USER="$APP_USER" INSTANCE_NAME="$INSTANCE_NAME" bash "$APP_DIR/deploy/ubuntu/install.sh" >"$WORK/install.log" 2>&1 \
    || { tail -n 20 "$WORK/install.log" >&2; fail "install.sh 失败（上面是最后 20 行）。修复后重新运行同一条命令即可继续。"; }
  ok "依赖和 systemd 服务已安装"
else
  fail "程序目录里缺少 deploy/ubuntu/install.sh"
fi
"$SYSTEMCTL" restart "$INSTANCE_NAME" "$INSTANCE_NAME-web" || true
sleep "$SERVICE_WAIT"
for unit in "$INSTANCE_NAME" "$INSTANCE_NAME-web"; do
  if "$SYSTEMCTL" is-active --quiet "$unit"; then ok "$unit 运行中"
  else warn "$unit 没有运行：查看 sudo journalctl -u $unit -n 50 --no-pager"; PROBLEMS+=("服务 $unit 未运行"); fi
done

# ---------- 8. 重新装上备份系统（新的写密钥，同一个密码）----------
say "第 8 步：重新安装自动备份"
SETUP=""
for cand in "$APP_DIR/deploy/ubuntu/setup-backup.sh"; do [[ -f "$cand" ]] && SETUP="$cand"; done
if [[ -z "$SETUP" ]]; then
  curl -fsSL "$RAW_BASE/setup-backup.sh" -o "$WORK/setup-backup.sh" && SETUP="$WORK/setup-backup.sh" || true
fi
if [[ -n "$SETUP" ]] && APP_DIR="$APP_DIR" STATE_DIR="$STATE_DIR" bash "$SETUP"; then
  ok "自动备份已安装（上面输出里的「部署公钥」那一行需要发给开发者添加为可写）"
else
  warn "自动备份安装失败，之后可单独运行 setup-backup.sh"; PROBLEMS+=("备份系统安装失败")
fi

# ---------- 9. 其它项目的恢复钩子 ----------
say "第 9 步：恢复其它项目（钩子）"
export BACKUP_DECRYPTED="$RESTORE_DIR"
UNHANDLED=()
for dir in "$RESTORE_DIR"/*/; do
  [[ -d "$dir" ]] || continue
  name="$(basename "$dir")"
  [[ "$name" == "app" || "$name" == "data" ]] && continue
  hook="$HOOKS_DIR/$name.sh"
  if [[ -x "$hook" ]]; then
    echo "  运行恢复钩子 $name"
    if RESTORE_PART_DIR="$dir" "$hook" "$RESTORE_DIR"; then ok "$name 已恢复"
    else warn "恢复钩子 $name 失败"; PROBLEMS+=("恢复钩子 $name"); fi
  else
    UNHANDLED+=("$name")
  fi
done

SAFE_LINE=""
for name in "${UNHANDLED[@]:-}"; do
  [[ -n "$name" ]] || continue
  if [[ "$name" == "safe" && -n "${SAFE_RESTORE_URL:-}" ]]; then
    echo "  按 SAFE_RESTORE_URL 恢复 safe 机器人"
    if curl -fsSL "$SAFE_RESTORE_URL" -o "$WORK/safe-restore.sh" && BACKUP_DECRYPTED="$RESTORE_DIR" bash "$WORK/safe-restore.sh"; then ok "safe 已恢复"
    else warn "safe 恢复失败"; PROBLEMS+=("safe 恢复"); fi
  elif [[ "$name" == "safe" ]]; then
    if timeout 60 git ls-remote "$SAFE_REPO_HTTPS" >/dev/null 2>&1; then
      SAFE_LINE="cd ~ && rm -rf safe-restore && git clone $SAFE_REPO_HTTPS safe-restore && sudo BACKUP_DECRYPTED=$RESTORE_DIR bash safe-restore/deploy/ubuntu/restore-from-backup.sh"
    else
      SAFEKEY="$STATE_DIR/safe_ed25519"
      [[ -f "$SAFEKEY" ]] || ssh-keygen -q -t ed25519 -N '' -C "safe-restore@$(hostname)" -f "$SAFEKEY"
      chmod 600 "$SAFEKEY"
      SAFE_LINE="cd ~ && rm -rf safe-restore && sudo GIT_SSH_COMMAND='ssh -i $SAFEKEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new' git clone $SAFE_REPO_SSH safe-restore && sudo BACKUP_DECRYPTED=$RESTORE_DIR bash safe-restore/deploy/ubuntu/restore-from-backup.sh"
      SAFE_NEEDS_KEY="$(cat "$SAFEKEY.pub")"
    fi
  else
    warn "备份里有「$name」部分，但没有恢复钩子 $HOOKS_DIR/$name.sh，已跳过（文件仍在 $RESTORE_DIR/$name/）"
  fi
done

# ---------- 总结 ----------
echo
echo "================ 恢复完成，接下来还需要你手动做 ================"
if (( ${#PROBLEMS[@]} )); then echo "⚠ 有部分步骤失败：${PROBLEMS[*]}（详见上面的输出）"; fi
if [[ -n "$SAFE_LINE" ]]; then
  echo
  echo "1) 恢复 safe 机器人（单行，整行复制）："
  if [[ -n "${SAFE_NEEDS_KEY:-}" ]]; then
    echo "   safe 仓库是私有的：先把下面这行公钥发给开发者添加为 safe 仓库的只读部署密钥，通知后再运行下面的命令："
    echo "   $SAFE_NEEDS_KEY"
  fi
  echo "   $SAFE_LINE"
fi
ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "2) 旧服务器上的机器人请先停掉（同一个机器人令牌不能两处同时运行）：sudo systemctl stop tg-directory-bot tg-directory-bot-web"
echo "3) DNS：把域名 188.ms 和 www.188.ms 的 A 记录改成这台服务器的 IP（${ip:-本机公网IP}），生效后再做下一步。"
echo "4) Nginx + HTTPS（两行，各自单行复制；把 you@example.com 换成你的邮箱）："
echo "   sudo apt-get install -y nginx certbot python3-certbot-nginx && sudo cp $APP_DIR/deploy/nginx/tron-draw.conf.example /etc/nginx/sites-available/tron-draw && sudo ln -sf /etc/nginx/sites-available/tron-draw /etc/nginx/sites-enabled/tron-draw && sudo nginx -t && sudo systemctl reload nginx"
echo "   sudo certbot --nginx -d 188.ms -d www.188.ms --non-interactive --agree-tos -m you@example.com --redirect"
if ! grep -qE '^AI_API_KEY=.+' "$APP_DIR/.env" 2>/dev/null; then
  echo "5) .env 里的 AI_API_KEY 是空的：需要的话编辑 sudo nano $APP_DIR/.env 填上，再 sudo systemctl restart $INSTANCE_NAME $INSTANCE_NAME-web"
fi
echo "6) 把自动备份的「部署公钥」（第 8 步输出的那一行）发给开发者添加为可写，然后运行：sudo tgbot-backup"
echo "7) 全部确认没问题后删除解密出来的机密文件：sudo rm -rf $RESTORE_DIR"
echo "=============================================================="
(( ${#PROBLEMS[@]} == 0 )) || exit 1
exit 0
}
main "$@"
