# 从加密备份恢复（tg-bot-backup 仓库）

备份文件名形如 `tg-bot-backup-20261027-040000.tar.gz.enc`，仓库里只保留最新 3 份（`BACKUP_KEEP`）。
文件用 AES-256 加密，**解密用的密码就是你安装备份时设置的 `BACKUP_PASS`**（没设置则是自动生成的口令，显示命令：`sudo cat /etc/tg-bot-backup/backup.pass`）。
如果中途改过密码：改之前的旧备份要用旧密码，之后的用新密码。

随时手动备份 / 查看现有备份：`sudo tgbot-backup`、`sudo tgbot-backup list`。

下面每一条命令都是单行，整行复制粘贴即可。

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

先停服务，再覆盖数据库并删掉旧的 -wal/-shm，最后改属主、启动（单行）：

~~~bash
sudo systemctl stop tg-directory-bot tg-directory-bot-web && sudo rm -f /opt/tg-directory-bot/data/*.sqlite3-wal /opt/tg-directory-bot/data/*.sqlite3-shm && sudo cp -a /tmp/tg-restore/data/. /opt/tg-directory-bot/data/ && sudo chown -R tgbot:tgbot /opt/tg-directory-bot/data && sudo systemctl start tg-directory-bot tg-directory-bot-web
~~~

子机器人（`data/clones/`）的库也会一起恢复；它们的 -wal/-shm 如有残留可一并删除：`sudo find /opt/tg-directory-bot/data/clones -name '*.sqlite3-*' -delete`（先停服务）。

## 4. 解压后各部分是什么

| 路径 | 内容 | 怎么恢复 |
| --- | --- | --- |
| `data/directory.sqlite3` 等 | 目录机器人主库、扫描库 | 第 3 节 |
| `data/clones/clone-N.sqlite3` | 子机器人各自的库 | 第 3 节（一起复制） |
| `<钩子名>/`（如 `safe/`） | 对应钩子导出的文件，例如另一个机器人的 MySQL 导出 `.sql` | 见下面「钩子」 |
| `MANIFEST.txt` | 文件清单 + sha256，`hooks:` 行列出成功的钩子，`problems:` 行列出失败的部分 | 仅供核对 |

### 钩子（hook）约定

备份脚本会依次运行 `/etc/tg-bot-backup.d/*.sh` 里**每个可执行**（`chmod +x`）的脚本，以 root 身份，第 1 个参数 `$1` 是暂存目录（也在环境变量 `BACKUP_STAGE_DIR` 里）：

- 钩子把要备份的文件写到 `"$1/<钩子名>/"` 下，`<钩子名>` 就是脚本文件名去掉 `.sh`（目录已建好；`data` 和 `MANIFEST.txt` 为保留名，不能用）。
- 钩子成功 = 退出码 0，输出会进入同一个加密压缩包。钩子失败或超时（默认 900 秒，`BACKUP_HOOK_TIMEOUT`）只会被记录并丢弃它的半成品输出，**不会**中断其它部分；最后备份仍会推送，但命令以退出码 4 结束，`systemctl status tg-bot-backup` 显示失败，`sudo tgbot-backup` 显示「部分失败」。
- 任何名为 `.env`、`.env.*`、`*.env` 的文件都会被自动从备份里剔除，钩子也不要把令牌/密码写进输出。
- 钩子目录只能由 root 写入（它们以 root 运行）。

示例钩子（只是示例，**没有安装**；假设 MySQL 库名 `safe`，账号写在 root 专用的 `/root/.my.cnf` 里，不要写进脚本）：

~~~bash
#!/usr/bin/env bash
set -euo pipefail
mysqldump --single-transaction --routines safe > "$1/safe/safe.sql"
~~~

恢复钩子数据时，把 `safe/safe.sql` 按对应机器人的方式导入，例如：`mysql safe < /tmp/tg-restore/safe/safe.sql`（先确认目标库是否要覆盖）。

## 5. 不在备份里的东西

`.env`（任何项目的机器人令牌、后台密码等）**不会**备份，恢复到新服务器需要自己重新填写；`backups/` 目录、`data/clones/deleted/` 归档和日志也不备份。
