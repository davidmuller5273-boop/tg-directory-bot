# 波场区块哈希 单/双 开奖走势页面

## 规则
- 每 20 个区块开奖一次：区块高度以 00/20/40/60/80 结尾（即 `高度 % 20 == 0`）的区块为开奖块。
- 取该区块哈希，忽略字母，取**最后一个数字**：1/3/5/7/9 = 单，0/2/4/6/8 = 双。
  例：哈希 `000…a3B45` → 最后数字 5 → 单。
- 开奖块一出现（约 3 秒内）就记录并显示，不再等待确认（`TRON_DRAW_CONFIRMATIONS` 默认 0）。为防极少数分叉，开奖块落后链头 20 块（约 1 分钟，已固化）后会自动用节点复核一次哈希，若不同则自动更正并写日志。
- 开奖块 = 区块高度 % 20 == `TRON_DRAW_BLOCK_OFFSET`（默认 0，即 00/20/40/60/80 结尾）。注意：若你的玩法是按“整点分钟”取区块（见下），这个偏移无法对上。

## 工作方式
- 复用机器人现有的波场区块扫描器（`tron_scanner.py`，任务 `scan_tron_blocks`，每秒一次）及其链头跟踪，没有单独采集进程。
- 即使没有任何地址监控，也会记录开奖块。每个开奖块只多 1 次约 0.5 KB 的 `getblock` 请求（约每 60 秒 1 次）。
- 历史永久保存在主数据库 `data/directory.sqlite3` 的表 `tron_draws`（区块高度、哈希、末位数字、结果、区块时间），不会清理；更新部署不会丢。
- 补漏：机器人停机/重启后，会按区块高度逐个补抓缺失的开奖块（最新的优先），每秒最多 `TRON_DRAW_BACKFILL_PER_STEP`（默认 3）个请求，走现有限流和备用节点；失败的 15 秒后重试。
  - 首次启用（表为空）时自动回补最近 `TRON_DRAW_INITIAL_BACKFILL`（默认 500）期。
  - 之后每次补漏最多回看 `TRON_DRAW_MAX_BACKFILL`（默认 20000 期，约 16 天）。更早的缺口不会补。
  - 依赖节点保存历史区块（TronGrid / 公共节点均可按高度查询）。

## 开关与配置（.env，均有默认值，不改也能用）
| 变量 | 默认 | 说明 |
|---|---|---|
| TRON_DRAW_STATS | 1 | 记录开奖块 + 开放页面/接口；设 0 则关闭（页面返回 404） |
| TRON_BLOCK_SCAN | 1 | 必须为 1（走现有扫描器；仅主机器人进程记录，克隆机器人不记录） |
| TRON_DRAW_CONFIRMATIONS | 0 | 开奖块确认深度（0 = 立即记录，之后自动复核） |
| TRON_DRAW_BLOCK_OFFSET | 0 | 开奖块高度 % 20 的余数（0~19） |
| TRON_DRAW_INITIAL_BACKFILL | 500 | 首次回补期数 |
| TRON_DRAW_MAX_BACKFILL | 20000 | 补漏最大回看期数 |
| TRON_DRAW_BACKFILL_PER_STEP | 3 | 补漏每秒最多请求数 |

页面每 4 秒自动刷新（接口 `Cache-Control: no-store`，Nginx 示例配置不做缓存）；时间显示为区块时间（北京时间）。

页面由现有后台 Web 服务（`run_web.py`，systemd 服务 `tg-directory-bot-web`）提供，监听 `WEB_HOST:WEB_PORT`（默认 `127.0.0.1:8080`，只在本机）。

- 页面：`/tron-draw`
- JSON 接口：`/api/tron-draw?limit=100`（limit 1~5000，默认 100）。返回 `counts`、`cells`（珠盘路坐标）、`recent`（最近 20 期）、`latest`、`next_draw_block`。
- 这两个地址**无需登录**（只含公开的链上数据）；后台其它页面仍需登录。

## 页面功能（走势 / 最终预测 / 我的走势 / 声音）

以下全部是历史统计，哈希末位单双各约 50%，**不保证命中**。

- **最长龙 / 最大连跳**：在「区块走势」的自定义期数后面，按所选期数（50/100/500/自定义）统计：最长龙 单X期 / 双X期、最大连跳 X期（单双单双…交替的最长一段）。并列时取最近的一段。点击数字会滚动到走势里对应位置并高亮约 5 秒。
- **算法参考表**：顺势、跟龙/斩龙(N)、近期占比(M)、近期少数方(M) 四个固定算法，各自给出下一期「参考」，并显示「回测命中（所选期数）」，如 `46/100（46%）`。回测逐期向前：每一期只用该期之前的数据。默认：近期占比 M=100、近期少数方 M=20、龙长 N=3（页面可改，保存在浏览器 localStorage）。回测期数始终是所选期数。
- **最终预测**：把投票给同一方的各算法回测命中率(%)相加，合计较大的一方即为预测（例：单 37+37+49=123，双 43 → 单）；合计相等时跟随回测命中率最高的单个算法。页面上显示两边合计。
- **预测列**：区块走势最右侧有「预测」列头，闪烁的格子是下一期最终预测。
- **我的走势**：每一期最终预测对不对，✅ / ❌ 排列，有自己的 50/100/500/自定义期数；旁边显示「最大连中 N✅ / 最大连错 N❌」（按所选期数），点击跳转并高亮。每个历史期的预测都用「该期之前的数据」逐期回算（最终预测里各算法的命中率用「区块走势」所选期数的窗口），所以历史期立即可看。
- **预测留底**：用默认设置（期数 100、N=3、跟龙、M=100、少数方 20）时，服务器在每个开奖块记录后立刻把下一期的最终预测写入 SQLite 表 `tron_draw_predictions`（只在该期开奖前可写入，之后不能改），「我的走势」里这些期显示为开奖前已记录；其它设置或更早的期用逐期回算。自定义设置的「刚才显示过的预测」由浏览器本地记录（localStorage），用于声音判断。
- **声音**：页面顶部「声音：关/开」开关，保存在浏览器，默认关（浏览器要求先点击页面才能发声）。开启后每出一期新开奖：上一期的最终预测对了播放“叮叮”（上扬两声），错了播放低沉“嗡嗡”（下沉两声）；声音由浏览器 WebAudio 合成，无外部文件。可点「试听✅/❌」。刚打开页面时不会响，只对页面打开后新出的开奖响。

- **最佳算法按钮**：在算法设置那一行的最后。**目标是连挂最少（最大连错最小），不是命中率最高。** 点击后服务器用已记录的历史（最近最多 5000 期，至少 300 期）做逐期向前回测（每期只用其之前的数据），为每个算法各选一个设置，并自动填入输入框（模式、龙长≥N、近M期、少数方M，填入处会闪一下黄色，上方显示「已填入：…」，即使和之前的值相同也会重新应用并刷新结果）：
  - 选择顺序：训练段最大连错最少 → 命中率更高 → 达到最大连错的次数更少 → 更接近默认设置。
  - 跟龙/斩龙：N 取 2~8，斩龙与跟龙（=顺势）按同一目标比较，平手选跟龙。「跟龙」永远等于上一期结果，N 对它没有影响；N 一栏始终填入「斩龙时最佳的 N」，方便你切到斩龙使用。
  - 近期占比 M、近期少数方 M：各取 5~200。
  - 页面显示每个算法的：最大连错（及出现次数）、命中率、样本数，分「训练」和「最近验证」两段；还显示默认设置的最大连错作对比，以及顺势基准。
  - 最终预测：按选出的这组设置，把四种算法合并后的最大连错、命中率也一起显示（命中率窗口取默认 100 期，并显示默认设置下的对比）。
  - 防过拟合：前面的期数用于「训练」选参，最后一段（最多 1000 期，约占 1/4）只用来「验证」，不参与选参。
  - 诚实提示：连错本身是随机的，公平硬币约 1000 期的最大连错通常也有 9~10 期；从 200 多组设置里挑最少的，历史上自然会更少，新数据上通常回到随机水平，历史最优不代表未来更准。不保证连错不会超过历史最大值。
  - 接口 `/api/tron-draw/best`（返回 `algorithms.{dragon,ratio,minority}` 的 `train`/`holdout`，其中含 `max_loss`、`max_loss_count`、`rate`、`hits`、`total`；以及 `final`、`trend`、`split`、`random_max_loss`、`note`）；每出一个新开奖才重算一次（缓存），算一次通常不到 1 秒。
  - 用了 Nginx/Caddy 反代的，需要放行 `/api/tron-draw/best`，见下面「更新一次」。

接口：`/api/tron-draw?limit=100&mine_limit=100` 新增字段 `reference.final`（next/sums/votes/basis/for_block/recorded）、`runs`（single/double/alternating 的 len 与 start）、`previous`（上一期已记录的预测与对错）、`my_trend`（仅当传 `mine_limit` 时返回：items 每期 predicted/result/ok/source，以及 hits/total/rate/max_win/max_loss）。

## 部署更新
```bash
sudo bash deploy/ubuntu/update-from-git.sh
```
更新脚本会重启机器人和 Web 服务，数据表自动创建。

## 用自己的域名访问（Nginx + HTTPS）
下面命令可整段复制粘贴到 SSH/网页控制台（不用 nano）。**example.com 是示例，必须换成你自己的域名。**

**第 0 步：DNS。** 到域名服务商后台添加一条 A 记录：主机记录 `@`（子域名就填如 `draw`），记录值 = 服务器公网 IPv4，TTL 默认。生效后检查：`dig +short 你的域名`，应返回服务器 IP。

**防火墙：** 只需开放 80 和 443（云厂商安全组也要放行）；**不要**开放 8080，页面只通过 Nginx 访问。

```bash
# 1) 设置域名（把 example.com 换成你的域名，其余命令不用改）
DOMAIN=example.com

# 2) 安装 Nginx 和 certbot
sudo apt-get update && sudo apt-get install -y nginx certbot python3-certbot-nginx

# 3) 写入 Nginx 配置（只开放 /tron-draw 和 /api/tron-draw（含 /best），其它路径 404）
cat <<'EOF' | sudo tee /etc/nginx/sites-available/tron-draw >/dev/null
server {
    listen 80;
    listen [::]:80;
    server_name example.com;

    location = / { return 302 /tron-draw; }

    location = /tron-draw {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location ~ ^/api/tron-draw(/best)?$ {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location / { return 404; }
}
EOF

# 4) 把配置里的 example.com 换成你的域名并启用
sudo sed -i "s/example.com/$DOMAIN/g" /etc/nginx/sites-available/tron-draw
sudo ln -sf /etc/nginx/sites-available/tron-draw /etc/nginx/sites-enabled/tron-draw
sudo nginx -t && sudo systemctl reload nginx

# 5) 防火墙（用 ufw 才需要；云安全组也要放行 80/443）
sudo ufw allow 80/tcp && sudo ufw allow 443/tcp

# 6) 申请 HTTPS 证书并自动跳转（邮箱换成你的）
sudo certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m you@example.com --redirect

# 7) 检查
curl -s "https://$DOMAIN/api/tron-draw?limit=5" | head -c 300; echo
```
打开：`https://你的域名/tron-draw`。证书由 certbot 定时任务自动续期（`sudo certbot renew --dry-run` 可测试）。

如果服务器上已有别的 Nginx 站点（如后台域名），上面的新 server 与它互不影响。

### 备选：Caddy（自动 HTTPS，不需要 certbot）
和 Nginx 二选一，不要同时占用 80/443。
```bash
DOMAIN=example.com
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update && sudo apt-get install -y caddy
cat <<'EOF' | sudo tee /etc/caddy/Caddyfile >/dev/null
example.com {
    handle /tron-draw {
        reverse_proxy 127.0.0.1:8080
    }
    handle /api/tron-draw* {
        reverse_proxy 127.0.0.1:8080
    }
    handle / {
        redir /tron-draw
    }
    handle {
        respond 404
    }
}
EOF
sudo sed -i "s/example.com/$DOMAIN/g" /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

### 已按旧文档配置过 Nginx / Caddy 的，更新一次（让「最佳算法」按钮能访问 /api/tron-draw/best）
```
# Nginx（域名配置文件名按你实际的；默认 tron-draw）
sudo sed -i 's#location = /api/tron-draw {#location ~ ^/api/tron-draw(/best)?$ {#' /etc/nginx/sites-available/tron-draw
sudo nginx -t && sudo systemctl reload nginx
# 如果用的是 Caddy，改用下面两行
sudo sed -i 's#handle /api/tron-draw {#handle /api/tron-draw* {#' /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

## 排查
- 页面"暂无数据"：看机器人日志 `sudo journalctl -u tg-directory-bot -f`，应有 "TRON 单/双 draw statistics enabled"；确认 `TRON_BLOCK_SCAN=1`、`TRON_DRAW_STATS=1`。
- 502：Web 服务没启动：`sudo systemctl status tg-directory-bot-web`；`curl http://127.0.0.1:8080/health`。
- 证书失败：多半是 DNS 未生效或 80 端口未放行。
