# 从加密备份恢复（tg-bot-backup 仓库）

备份文件名形如 `tg-bot-backup-20261027-040000.tar.gz.enc`，仓库里只保留最新 3 份（`BACKUP_KEEP`）。
文件用 AES-256 加密，**解密用的密码就是你安装备份时设置的 `BACKUP_PASS`**（没设置则是自动生成的口令，显示命令：`sudo cat /etc/tg-bot-backup/backup.pass`）。
如果中途改过密码：改之前的旧备份要用旧密码，之后的用新密码。

随时手动备份 / 查看现有备份：`sudo tgbot-backup`、`sudo tgbot-backup list`。

下面每一条命令都是单行，整行复制粘贴即可。

## 0. 新服务器一条命令完整恢复

在一台全新的 Ubuntu 服务器上（用 root 登录或加 sudo），运行这一条命令（单行；`你的备份密码` 换成安装备份时设置的那个密码）：

~~~bash
curl -fsSL https://raw.githubusercontent.com/davidmuller5273-boop/tg-directory-bot/main/deploy/ubuntu/new-server-restore.sh | sudo BACKUP_PASS='你的备份密码' bash
~~~

- **第一次运行**会生成一把新服务器专用的「只读」公钥并打印出来，然后停止（提示「公钥已生成，添加后重新运行同一条命令」）。把这一行公钥发给开发者，由开发者添加到备份仓库后，**再运行同一条命令**继续。不需要 GitHub 令牌。
- 之后脚本依次：安装依赖 → 下载私有备份仓库 → 取**最新**的 `tg-bot-backup-*.tar.gz.enc`，用密码解密（密码只走环境变量，不会显示）→ 检查所有数据库完整性 → 安装机器人程序 → 恢复 `data/` 和 `.env`（权限 600）→ 运行 install.sh 并启动服务 → 重新安装自动备份（同一个密码，生成**新的写入公钥**，需要再发给开发者添加）→ 运行恢复钩子 → 打印还需要手动做的事。
- 可重复运行：同一份备份已恢复过会跳过；服务器上已有机器人数据或 `.env` 时会拒绝覆盖，确实要覆盖才加 `FORCE_RESTORE=1`。任何一步失败都会用中文说明原因并停止（密码错误、仓库里没有备份、公钥还没添加……）。
- 恢复出来的明文在 `/tmp/tg-restore`（含 `.env` 等机密，权限 700）；全部确认后请删除：`sudo rm -rf /tmp/tg-restore`。

脚本最后会列出还要手动做的事：旧服务器停掉机器人、域名 188.ms / www.188.ms 的 DNS 指向新 IP、Nginx + certbot 的两行命令、`.env` 里 `AI_API_KEY` 为空时的提示。

### 恢复钩子约定（其它项目，例如 safe 机器人）

解密出来的目录里，除 `app/`、`data/` 之外的每个子目录 `<name>/` 都是某个备份钩子写入的部分。对每个 `<name>/`：如果存在可执行的 `/etc/tg-bot-backup.restore.d/<name>.sh`，脚本就运行它：

- `$1` = 解密目录（默认 `/tmp/tg-restore`，同时在环境变量 `BACKUP_DECRYPTED` 里）；这一部分的文件在 `"$1/<name>/"`，也在环境变量 `RESTORE_PART_DIR` 里。
- 钩子负责把该项目装好并恢复（例如 safe：安装程序、导入 MySQL、放回 `.env` 和文件，`.env` 保持 600）。以 root 运行，退出码 0 = 成功；失败只会记录并在最后报告，不影响其它部分。
- 没有对应钩子的部分会被跳过并提示（文件仍在解密目录里）。对 `safe/`：设置了环境变量 `SAFE_RESTORE_URL`（safe 项目恢复脚本的 raw 地址）就自动下载运行；没设置则打印一行命令：`cd ~ && rm -rf safe-restore && git clone https://github.com/davidmuller5273-boop/safe.git safe-restore && sudo BACKUP_DECRYPTED=/tmp/tg-restore bash safe-restore/deploy/ubuntu/restore-from-backup.sh`。如果 safe 仓库是私有的，脚本会另外生成一把 safe 专用公钥（`/etc/tg-bot-backup/safe_ed25519`），打印它和用这把密钥 clone 的命令，公钥添加为 safe 仓库的只读部署密钥后再运行。

下面是手动一步步恢复的方法（新服务器优先用上面的一条命令）。

## 1. 取回备份文件

在原服务器上，备份仓库的本地副本就在 `/var/lib/tg-bot-backup/repo`：

~~~bash
sudo git -C /var/lib/tg-bot-backup/repo pull -q && sudo ls -1 /var/lib/tg-bot-backup/repo
~~~

换新服务器：先在新服务器上按 UBUNTU_CN.md 装好机器人，再用任意方式（例如在电脑上 `git clone` 私有仓库，需要你自己的 GitHub 登录）把想恢复的 `.tar.gz.enc` 文件传到服务器，比如 `/tmp/`。

## 2. 解密并检查（不改动线上数据）

原服务器（口令文件还在）：

~~~bash
sudo bash /usr/local/lib/tg-bot-backup/restore-backup.sh /var/lib/tg-bot-backup/repo/tg-bot-backup-日期时间.tar.gz.enc /tmp/tg-restore
~~~

新服务器（没有口令文件，会提示输入你的备份密码；也可以先 `sudo curl -fsSLo /usr/local/bin/restore-backup.sh https://raw.githubusercontent.com/davidmuller5273-boop/tg-directory-bot/main/deploy/ubuntu/restore-backup.sh` 下载脚本）：

~~~bash
sudo bash /usr/local/bin/restore-backup.sh /tmp/tg-bot-backup-日期时间.tar.gz.enc /tmp/tg-restore
~~~

脚本会逐个数据库执行 `PRAGMA integrity_check`，全部显示 `ok` 才算备份可用。手动检查某个库：

~~~bash
sqlite3 /tmp/tg-restore/data/directory.sqlite3 'PRAGMA integrity_check'
~~~

（没有 sqlite3 命令时：`sudo apt-get install -y sqlite3`。）解压结果里 `MANIFEST.txt` 是文件清单和 sha256。

## 3. 恢复到线上

先停服务，再覆盖数据库并删掉旧的 -wal/-shm，把 `.env` 放回（权限 600、属主 `tgbot`），最后启动（单行；会覆盖现有 `.env`，如果不想覆盖就删掉其中 `install ...` 那一段）：

~~~bash
sudo systemctl stop tg-directory-bot tg-directory-bot-web && sudo rm -f /opt/tg-directory-bot/data/*.sqlite3-wal /opt/tg-directory-bot/data/*.sqlite3-shm && sudo cp -a /tmp/tg-restore/data/. /opt/tg-directory-bot/data/ && sudo chown -R tgbot:tgbot /opt/tg-directory-bot/data && sudo install -m 600 -o tgbot -g tgbot /tmp/tg-restore/app/.env /opt/tg-directory-bot/.env && sudo systemctl start tg-directory-bot tg-directory-bot-web
~~~

恢复后确认权限：`sudo ls -l /opt/tg-directory-bot/.env`（应为 `-rw-------`）。钩子里的文件（如 `safe/` 里的 `.env`/配置/`.sql`）按对应项目的位置放回，同样用 `install -m 600` 保持权限，并按 `MANIFEST.txt` 核对。

子机器人（`data/clones/`）的库也会一起恢复；它们的 -wal/-shm 如有残留可一并删除：`sudo find /opt/tg-directory-bot/data/clones -name '*.sqlite3-*' -delete`（先停服务）。

## 4. 解压后各部分是什么

| 路径 | 内容 | 怎么恢复 |
| --- | --- | --- |
| `app/.env` | 目录机器人的 `.env`（令牌、后台密码等机密，权限 600） | 第 3 节，恢复后保持 600 |
| `data/directory.sqlite3` 等 | 目录机器人主库、扫描库 | 第 3 节 |
| `data/clones/clone-N.sqlite3` | 子机器人各自的库 | 第 3 节（一起复制） |
| `<钩子名>/`（如 `safe/`） | 对应钩子放进来的文件，例如另一个机器人的 MySQL 导出 `.sql`，以及它自己的 `.env`/配置（权限 600） | 见下面「钩子」 |
| `MANIFEST.txt` | 文件清单 + sha256，`hooks:` 行列出成功的钩子，`problems:` 行列出失败的部分 | 仅供核对 |

### 钩子（hook）约定

备份脚本会依次运行 `/etc/tg-bot-backup.d/*.sh` 里**每个可执行**（`chmod +x`）的脚本，以 root 身份，第 1 个参数 `$1` 是暂存目录（也在环境变量 `BACKUP_STAGE_DIR` 里）：

- 钩子把要备份的文件写到 `"$1/<钩子名>/"` 下，`<钩子名>` 就是脚本文件名去掉 `.sh`（目录已建好；`data` 和 `MANIFEST.txt` 为保留名，不能用）。
- 钩子成功 = 退出码 0，输出会进入同一个加密压缩包。钩子失败或超时（默认 900 秒，`BACKUP_HOOK_TIMEOUT`）只会被记录并丢弃它的半成品输出，**不会**中断其它部分；最后备份仍会推送，但命令以退出码 4 结束，`systemctl status tg-bot-backup` 显示失败，`sudo tgbot-backup` 显示「部分失败」。
- 钩子输出里的任何文件（包括它有意复制进来的 `.env`/配置）都会原样进入加密包，权限保持钩子设定的值（建议 600）；备份脚本不会剔除也不会打印它们。
- 钩子目录只能由 root 写入（它们以 root 运行）。

示例钩子（只是示例，**没有安装**；假设 MySQL 库名 `safe`，账号写在 root 专用的 `/root/.my.cnf` 里，不要写进脚本）：

~~~bash
#!/usr/bin/env bash
set -euo pipefail
mysqldump --single-transaction --routines safe > "$1/safe/safe.sql"
~~~

恢复钩子数据时，把 `safe/safe.sql` 按对应机器人的方式导入，例如：`mysql safe < /tmp/tg-restore/safe/safe.sql`（先确认目标库是否要覆盖）。

## 5. 不在备份里的东西

`backups/` 目录、`data/clones/deleted/` 归档和日志不备份。`.env` **在**备份里（`app/.env`），所以备份密码一旦泄露，里面的令牌/密码也会泄露；怀疑泄露时请更换密码并重新备份，同时更换机器人令牌。
