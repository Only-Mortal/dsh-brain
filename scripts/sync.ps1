# sync.ps1 — 改完一步就同步（测试 + 提交 + 推送）
# 用法： powershell -NoProfile -ExecutionPolicy Bypass -File scripts\sync.ps1 -Message "feat: 描述你改了什么"
param(
  [Parameter(Mandatory = $true)][string]$Message,
  [switch]$SkipTests,
  [switch]$NoPush
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = Join-Path $root "src"

if (-not $SkipTests) {
  Write-Host "== 跑测试 ==" -ForegroundColor Cyan
  python -m unittest discover -s tests
  if ($LASTEXITCODE -ne 0) { throw "测试没过，不提交" }
}

Write-Host "== 提交 ==" -ForegroundColor Cyan
git add -A
$staged = git diff --cached --name-only
if (-not $staged) { Write-Host "没有改动，跳过"; return }

git commit -q -m $Message
if ($LASTEXITCODE -ne 0) { throw "提交失败" }

if (-not $NoPush) {
  Write-Host "== 推送 ==" -ForegroundColor Cyan
  git push -q origin HEAD
  if ($LASTEXITCODE -ne 0) { throw "推送失败（检查 gh auth status）" }
}

$head = git log --oneline -1
Write-Host "完成：$head" -ForegroundColor Green
