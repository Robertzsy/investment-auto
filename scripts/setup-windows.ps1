param([switch]$SkipPortfolioInit)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

if (-not (Get-Command node.exe -ErrorAction SilentlyContinue)) {
    throw "未找到 Node.js。请先安装 Node.js 18 或更高版本：https://nodejs.org/"
}

$python = Get-Command py.exe -ErrorAction SilentlyContinue
if ($python) {
    & $python.Source -3 -m venv .venv
} elseif (Get-Command python.exe -ErrorAction SilentlyContinue) {
    & (Get-Command python.exe).Source -m venv .venv
} else {
    throw "未找到 Python。请先安装 Python 3.10 或更高版本。"
}

& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements-lock.txt
if (-not (Test-Path ".env")) {
    Copy-Item -LiteralPath ".env.example" -Destination ".env"
}
if (-not $SkipPortfolioInit -and -not (Test-Path "runtime\data\portfolio.json")) {
    & ".\.venv\Scripts\python.exe" -m src.main init
}

Write-Host ""
Write-Host "初始化完成。请在 .env 或设置页面填写模型 API Key，然后双击 InvestmentAuto.exe。" -ForegroundColor Green
