param(
    [Parameter(Mandatory = $true)]
    [string]$Server,

    [string]$User = "root"
)

$ErrorActionPreference = "Stop"
$ArchiveName = "tg-directory-bot-ubuntu.tar.gz"
$ArchivePath = Join-Path $PSScriptRoot $ArchiveName
$Remote = "${User}@${Server}"

if (-not (Test-Path $ArchivePath)) {
    throw "找不到更新包：$ArchivePath"
}

if (-not (Get-Command scp -ErrorAction SilentlyContinue)) {
    throw "Windows 未安装 OpenSSH/scp。请安装 Windows OpenSSH 客户端，或使用 WinSCP 上传。"
}

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw "Windows 未安装 OpenSSH/ssh。请先安装 Windows OpenSSH 客户端。"
}

Write-Host "正在上传 $ArchiveName 到 $Remote ..." -ForegroundColor Cyan
& scp $ArchivePath "${Remote}:/tmp/$ArchiveName"
if ($LASTEXITCODE -ne 0) {
    throw "上传失败，scp 退出代码：$LASTEXITCODE"
}

$RemoteCommand = @'
set -e
ARCHIVE=/tmp/tg-directory-bot-ubuntu.tar.gz
if sudo test -f /opt/tg-directory-bot/.env; then
  if sudo test -f /opt/tg-directory-bot/deploy/ubuntu/update.sh; then
    sudo bash /opt/tg-directory-bot/deploy/ubuntu/update.sh "$ARCHIVE"
  else
    tar xOf "$ARCHIVE" tg-directory-bot/deploy/ubuntu/update.sh > /tmp/tg-update.sh
    sudo bash /tmp/tg-update.sh "$ARCHIVE"
  fi
else
  sudo mkdir -p /opt
  sudo tar xzf "$ARCHIVE" -C /opt
  sudo bash /opt/tg-directory-bot/deploy/ubuntu/install.sh
  echo
  echo "首次安装完成，请填写配置：sudo nano /opt/tg-directory-bot/.env"
  echo "填写后启动：sudo systemctl restart tg-directory-bot tg-directory-bot-web"
fi
'@

Write-Host "正在连接 Ubuntu 并安装或更新 ..." -ForegroundColor Cyan
& ssh -t $Remote $RemoteCommand
if ($LASTEXITCODE -ne 0) {
    throw "服务器执行失败，ssh 退出代码：$LASTEXITCODE"
}

Write-Host "操作完成。" -ForegroundColor Green
