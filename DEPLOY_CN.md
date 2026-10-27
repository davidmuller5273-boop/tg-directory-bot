# Docker Compose 部署

Ubuntu 生产环境优先使用 UBUNTU_CN.md 的 systemd 方案。本文件用于已经使用 Docker 的服务器。

已安装的 Ubuntu 服务器可用一条命令从 GitHub 更新（无需再 scp 压缩包）：

~~~bash
curl -fsSL https://raw.githubusercontent.com/davidmuller5273-boop/tg-directory-bot/main/deploy/ubuntu/update-from-git.sh | sudo bash
~~~

~~~bash
cp .env.example .env
nano .env
docker compose up -d --build bot web
docker compose logs -f bot web
~~~

后台仅映射到服务器本机 127.0.0.1:8080。使用 deploy/nginx/tg-directory-bot.conf.example 配置域名和 HTTPS。

手动备份：

~~~bash
docker compose --profile tools run --rm backup
~~~

长期保留 .env、data/ 和 backups/。

Ubuntu systemd 部署另有「每 2 天加密备份到私有 GitHub 仓库」，安装方式和备份密码说明见 UBUNTU_CN.md 的 5.1 节（备份含 .env，仅加密后上传，请用强密码）。换新服务器时一条命令完整恢复：见 UBUNTU_CN.md 的 5.2 节（`new-server-restore.sh`）（安装命令：`curl -fsSL https://raw.githubusercontent.com/davidmuller5273-boop/tg-directory-bot/main/deploy/ubuntu/setup-backup.sh | sudo BACKUP_PASS='你的密码' bash`）。

更新：

~~~bash
docker compose down
docker compose up -d --build bot web
~~~
