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

更新：

~~~bash
docker compose down
docker compose up -d --build bot web
~~~
