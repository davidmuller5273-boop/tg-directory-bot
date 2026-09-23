# Ubuntu 部署手册

推荐 Ubuntu 22.04 或 24.04。默认安装目录为 /opt/tg-directory-bot，运行用户为 tgbot。

## 1. 从 Windows 上传并解压

在 Windows 中打开包含压缩包的文件夹，点击地址栏输入 `powershell` 并回车，然后执行：

~~~bash
scp .\tg-directory-bot-ubuntu.tar.gz root@服务器IP:/tmp/
ssh root@服务器IP
sudo mkdir -p /opt
sudo tar xzf /tmp/tg-directory-bot-ubuntu.tar.gz -C /opt
cd /opt/tg-directory-bot
~~~

前两条命令在 Windows PowerShell 执行；登录成功后的命令在 Ubuntu 服务器执行。Windows 没有 `scp` 时，可使用 WinSCP 把压缩包上传到服务器 `/tmp/`。

## 2. 一键安装

~~~bash
sudo bash deploy/ubuntu/install.sh
~~~

安装脚本会安装 Python、venv、SQLite 和 OpenSSL，创建低权限用户，创建机器人和后台两个 systemd 服务、6 小时备份定时器，并自动生成后台随机密码和会话密钥。脚本输出的后台密码也保存在 /opt/tg-directory-bot/.env。

## 2.1 一键更新

在 Windows 的项目文件夹打开 PowerShell，把新版压缩包上传到服务器：

~~~bash
scp .\tg-directory-bot-ubuntu.tar.gz root@服务器IP:/tmp/
~~~

用 SSH 软件登录 Ubuntu 服务器后执行一条命令：

~~~bash
sudo bash /opt/tg-directory-bot/deploy/ubuntu/update.sh /tmp/tg-directory-bot-ubuntu.tar.gz
~~~

也可以在 Windows PowerShell 直接运行随包附带的一键脚本，它会自动判断首次安装或更新：

~~~powershell
powershell -ExecutionPolicy Bypass -File .\Windows一键安装更新.ps1 -Server 服务器IP -User root
~~~

更新脚本会先备份数据库，保留 `/opt/tg-directory-bot/.env`、`data/`、`backups/` 和虚拟环境，删除其他旧程序文件后完整覆盖新版，并重新安装依赖、更新 systemd 服务和重启机器人及后台。

如果服务器上的旧版还没有 `update.sh`，先从新版压缩包取出脚本再运行：

~~~bash
tar xOf /tmp/tg-directory-bot-ubuntu.tar.gz tg-directory-bot/deploy/ubuntu/update.sh > /tmp/tg-update.sh
sudo bash /tmp/tg-update.sh /tmp/tg-directory-bot-ubuntu.tar.gz
~~~

## 3. 配置并启动

~~~bash
sudo nano /opt/tg-directory-bot/.env
~~~

必须修改：

~~~env
BOT_TOKEN=BotFather给你的Token
ADMIN_IDS=你的Telegram数字ID
~~~

多个管理员用英文逗号分隔。默认第一个 `ADMIN_IDS` 会成为主超级管理员。也可以显式填写：

~~~env
SUPER_ADMIN_IDS=你的主超级管理员数字ID
~~~

生产环境建议填写 TronGrid Key：

~~~env
TRONGRID_API_KEY=你的TronGridKey
TRONSCAN_API_KEY=你的TronScanKey
OKLINK_API_KEY=你的OKLinkKey
TOKENVIEW_API_KEY=你的TokenviewKey
~~~

未填写后两项时仍会使用 TronGrid、TronScan 和 TronScan 兼容接口；填写后，前三个接口不可用会继续自动切换 OKLink、Tokenview。

普通机器人消息默认 180 秒后自动撤回，开奖自动播报和抽奖消息除外：

~~~env
MESSAGE_AUTO_DELETE_SECONDS=180
~~~

## 3.1 群组权限

把机器人加入目标群组后，在 BotFather 依次执行 `/setprivacy`、选择机器人、选择 `Disable`。否则 Telegram 不会把普通群消息发送给机器人，群消息量和活跃用户统计会不完整。

如需使用群违规处罚，请在群管理中把机器人设为管理员，并授予“删除消息”和“封禁用户”权限。处罚默认关闭，开启后只处理链接和管理员独立维护的违规关键词；搜索词不会自动成为违规词。群管理员和机器人管理员不受处罚。群统计只保存用户 ID 和计数，不保存普通聊天正文。

常用群命令：

~~~text
/raffle 60 3 奖品名称   创建 60 分钟、3 名中奖者的抽奖
/raffle 60 3 1*188RMB | 2*88RMB   创建多档奖品抽奖
/raffleat 2026-08-27 21:30 | 3 | 奖品   指定北京时间开奖
/raffles               查看本群抽奖
/groupstats            查看本群统计和成员发言排行（每页10人）
/moderation on         超级管理员开启群违规处罚
/moderation off        超级管理员关闭群违规处罚
/moderation status     查看群违规处罚状态
/badword add 关键词    管理员添加违规关键词
/badword del 关键词    管理员删除违规关键词
/badword list          管理员查看违规关键词
/draw 12               立即开奖编号 12
/cancelraffle 12       取消编号 12
~~~

`/groupstats` 默认显示今日排行；超级管理员可用消息下方的“今日 / 近7天 / 近31天”按钮切换独立排行，普通成员只能看今日。每个周期包含全部发过消息的成员，每页 10 人，翻页和切换时都会再次校验权限。文字、照片、文件、贴纸、语音等真人普通消息均计数，机器人消息不计入。Web 后台“群组与抽奖”详情同样每页 10 人。成员与每日统计明细超过 31 天自动清理，群累计消息数继续保留。系统只保存身份信息与计数，不保存普通聊天正文。

群内普通聊天不会自动删除。正常触发机器人功能的成员消息在 180 秒后清理；处罚开启后，发送外链或命中管理员添加的违规关键词的消息立即删除并累计一次，第 1-4 次禁言 3 天，第 5 次踢出群组。管理员可从机器人“管理员 > 违规词管理”、`/badword` 或后台“系统设置”增删词库；只有超级管理员或后台主账号可以开启和关闭处罚。

机器人菜单已经按“搜索服务、提交收录、群组管理、管理员、联系超级管理员、帮助教程”分类。搜索统计按钮只对超级管理员显示；普通用户和普通管理员看不到该选项。联系按钮会直接显示可点击的超级管理员账号，帮助页不显示超级管理员操作教程。

超级管理员私聊机器人使用“搜索服务 > 搜索统计”或 `/searchstats` 查看关键词搜索量排名和搜索用户，使用 `/searchstats 2` 查看“谁搜索了什么”。系统统计菜单搜索、`/search` 和群内地址关键词查询；后台“搜索统计”页面提供两张每页 10 条的明细表。

## 3.1.1 管理员与私密笔记

所有用户都可以私聊或在群内发送 `关键词搜录 内容`，例如 `v8搜录 t.me/example`。内容支持文字、网址、图片、视频、音频或文件；媒体需把 `v8搜录 说明` 写在媒体说明中。旧的“收录”、`/submit 关键词 地址` 和直接发送 `关键词 地址` 继续兼容。所有提交进入待审核状态，只有主超级管理员可以审核、修改和下架。审核通过后，群员直接发送完整关键词即可触发，不需要追加“地址”。

~~~text
/admins              查看管理员
/addadmin 123456789  添加管理员
/deladmin 123456789  删除管理员
/pending             查看待审核收录
/approve 12          通过编号 12
/reject 12 原因      拒绝编号 12
/edit 12 888 https://example.com   修改关键词和地址
/remove 12           下架编号 12
~~~

主超级管理员私聊机器人可使用私人笔记：

~~~text
1 关键词 内容       保存文字笔记
2 关键词            查询最近5条
/noteadd 关键词 内容 保存文字笔记
/notes 关键词        查询最近5条
~~~

保存文件笔记时，把文件发送给机器人，并在文件说明里填写 `1 关键词 说明`。系统按关键词保留最近 99 条，保存 9 个月，普通用户和普通管理员不能查询。

所有机器人和后台显示时间、systemd 进程时区、日志、备份文件名及备份计划均按北京时间（UTC+8）执行；数据库内部仍使用 UTC 保存，避免服务器时区变化造成重复开奖或抽奖提前结束。

普通机器人回复以及用户私聊机器人发送的原消息默认在 180 秒后自动删除。带按钮的操作页面以最后一次点击为准，连续 180 秒没有点击才会撤回，每次点击都会重新计时。群内普通聊天保留，只有正常触发机器人功能的成员消息会在 180 秒后清理；违规链接或违规关键词消息由处罚功能立即删除。群内删除成员消息时，机器人必须具有“删除消息”权限。开奖自动播报、抽奖创建消息和抽奖结果不会自动删除。可在 `.env` 使用 `MESSAGE_AUTO_DELETE_SECONDS=180` 调整秒数。

## 3.2 未收录提示与自动回复

登录 Web 后台后：

- 在“系统设置”修改未收录提示和转人工确认语，默认未收录提示为“地址没有收录，请联系管理员”。
- 在“自动回复”创建关键词规则，可选择“包含关键词”或“完全匹配”，并查看命中次数、停用或删除规则。
- 私聊消息命中规则时直接自动回复；没有命中时进入“双向客服”，管理员可从后台或 Telegram `/reply` 回复。
- 群内默认发送 `888地址` 会提取“888”并搜索已通过的收录，只有带“地址”后缀才回应；发送 `z0` 会调用 OKX P2P 前 10 商户实时报价。
- 私聊或群内直接发送一个有效的 34 位波场地址，会自动显示激活状态、注册时间、TRX、TRC20-USDT、最近 3 笔已确认 TRX/USDT 转账和本次查询的北京时间。
- “系统设置”可修改“地址”后缀和 `z0` 汇率关键词，也可关闭全部群关键词响应。普通群聊天不会触发回复。

## 3.3 开奖结果与自动播报

系统覆盖双色球、福彩3D、七乐彩、快乐8、超级大乐透、排列3、排列5、7星彩、香港六合彩、澳门六合彩和新澳六合彩。群管理员可打开“群组管理 > 开奖订阅”，使用全部播报总开关或逐个彩种的 ✅/❌ 按钮。命令方式如下：

~~~text
/lottery                 查询全部最新结果
/lottery 双色球          查询单个彩种
/lotteryhistory 快乐8    查询快乐8最近100期
/lotterysub 全部         订阅全部彩种
/lotterysub 福彩         只订阅四种福彩
/lotterysub 体彩         只订阅四种体彩
/lotterysub 大乐透       只订阅单个彩种
/lotterysub 六合彩       订阅香港、澳门和新澳三种彩票
/lotterysubs             查看本群订阅
/lotteryunsub 全部       清空本群订阅
~~~

群内可直接发送以下关键词，结果每页显示 10 期，使用消息下方按钮翻页：

~~~text
双色球历史
福彩3D历史
七乐彩历史
快乐8历史
大乐透历史
排列3历史
排列5历史
7星彩历史
香港六合彩历史
澳门六合彩历史
新澳六合彩历史
~~~

机器人默认每 180 秒检查中国福彩网、中国体彩网和香港赛马会官方接口。福彩接口会先访问对应历史开奖页面获取会话 cookie，再分页读取 JSON 历史记录。官方接口被 403/WAF 拦截时，会自动读取公开 GitHub `public_data/draws/{彩种}.json` 备用源。澳门六合彩和新澳六合彩没有澳门政府认可的官方开奖源，机器人会明确标注为第三方 Marksix6 数据；第三方页面当前提供 10 期历史，后续轮询结果会缓存到最多 100 期。首次启动只保存当前结果作为基线，之后发现新期号才播报，因此重启服务不会重复播报旧期开奖。

## 3.4 群广告与抽奖方案

群管理员进入“群组管理”即可设置：

- 定时广告：发送 `间隔分钟 | 广告文字`，也可发送图片、视频、音频或文件并把该格式写在说明中。
- 消息前广告与消息后广告：只设置文字，文字会合并到机器人同一条群消息的前部或后部，不会分开发送。
- 抽奖方案：按分钟、指定北京时间、快速 10 分钟和多档奖品。指定时间支持 `YYYY-MM-DD HH:MM`、`MM-DD HH:MM` 或 `HH:MM`。
- 开奖订阅：全部总开关和每个彩种独立开关。

广告设置、开奖订阅和抽奖创建只允许目标群的群管理员或机器人管理员操作。

## 3.5 群积分、签到和礼品

群主或超级管理员进入“群组管理 > 积分·签到·礼品”后可以：

- 开启或关闭本群积分。
- 设置每次签到固定积分，或填写最小/最大值作为随机签到积分。
- 设置连续签到奖励：从连续第 3 天开始，每天额外增加指定积分。
- 设置每日随机活跃目标，例如每位成员当天随机达到 10-30 条消息后，随机奖励 2-8 积分；每人每天只奖励一次。
- 添加、删除积分礼品，设置兑换积分和库存，`-1` 库存表示不限量。
- 按用户数字 ID 增加、扣减或清零积分，也可清零本群全部积分。

群员直接发送：

~~~text
签到             完成今日签到
积分             查看自己的积分
积分排行         查看本群前100名（每页10名）
积分礼品         查看可兑换礼品
兑换 12          兑换礼品编号12
抽奖             显示当前进行中的抽奖
统计             显示本人权限对应的群统计
活跃排行         显示本人权限对应的活跃统计
~~~

普通群员只能看到今日统计；超级管理员可切换近 7 天和近 31 天。机器人会优先显示 Telegram 姓名或用户名，不再把数字 ID 当成成员名称。

若后台显示接口被安全策略拦截，请先确认服务器可以访问 `api.api16868.com`、`www.cwl.gov.cn`、`webapi.sporttery.cn` 和 `raw.githubusercontent.com`。最新一期优先使用实时接口，官方站和公开历史库作为回退；可以在 `.env` 调整 `LOTTERY_POLL_SECONDS`，最低 60 秒。

后台默认配置：

~~~env
WEB_HOST=127.0.0.1
WEB_PORT=8080
ADMIN_USERNAME=admin
ADMIN_PASSWORD=安装脚本已自动生成
~~~

启动并查看状态：

~~~bash
sudo systemctl restart tg-directory-bot tg-directory-bot-web
sudo systemctl status tg-directory-bot tg-directory-bot-web --no-pager
sudo journalctl -u tg-directory-bot -f
sudo journalctl -u tg-directory-bot-web -f
~~~

## 4. 域名、Nginx 与 HTTPS

不要把 8080 端口直接暴露到公网。

~~~bash
sudo apt-get install -y nginx
sudo cp deploy/nginx/tg-directory-bot.conf.example /etc/nginx/sites-available/tg-directory-bot
sudo nano /etc/nginx/sites-available/tg-directory-bot
~~~

把 bot-admin.example.com 改成后台域名，然后：

~~~bash
sudo ln -s /etc/nginx/sites-available/tg-directory-bot /etc/nginx/sites-enabled/tg-directory-bot
sudo nginx -t
sudo systemctl reload nginx
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d 你的后台域名
~~~

后台地址：https://你的后台域名/login

## 5. 备份

后台“备份管理”页面可以立即创建并下载备份。命令行方式：

~~~bash
sudo systemctl start tg-directory-bot-backup
sudo journalctl -u tg-directory-bot-backup -n 50 --no-pager
sudo systemctl list-timers tg-directory-bot-backup.timer
~~~

备份位于 /opt/tg-directory-bot/backups/，默认保留 30 份，每份都有 .sha256 校验文件。

## 6. 恢复

~~~bash
sudo bash deploy/ubuntu/restore.sh /opt/tg-directory-bot/backups/directory-YYYYMMDD-HHMMSS.sqlite3
~~~

脚本会停止机器人和后台、校验哈希、保存当前数据库安全副本、恢复目标数据库，再启动两个服务。

## 7. 迁移服务器

旧服务器打包关键数据：

~~~bash
cd /opt
sudo tar czf tg-directory-bot-data.tar.gz tg-directory-bot/.env tg-directory-bot/data tg-directory-bot/backups
~~~

将项目压缩包和数据包上传到新服务器。先解压项目并运行安装脚本，再解压数据包并重启两个服务。

## 8. 升级代码

~~~bash
sudo systemctl start tg-directory-bot-backup
sudo systemctl stop tg-directory-bot tg-directory-bot-web
~~~

保留 .env、data/ 和 backups/，覆盖其余程序文件后：

~~~bash
cd /opt/tg-directory-bot
sudo bash deploy/ubuntu/install.sh
sudo systemctl restart tg-directory-bot tg-directory-bot-web
~~~

数据库表会在启动时自动迁移，无需手动执行 SQL。

## 9. 故障排查

~~~bash
sudo systemctl status tg-directory-bot tg-directory-bot-web --no-pager
sudo journalctl -u tg-directory-bot -n 100 --no-pager
sudo journalctl -u tg-directory-bot-web -n 100 --no-pager
curl http://127.0.0.1:8080/health
~~~

若 OKX 榜单不可用，机器人会显示错误并尝试 CoinGecko 备用参考价。若 TronGrid 返回限流，请配置 TRONGRID_API_KEY。
