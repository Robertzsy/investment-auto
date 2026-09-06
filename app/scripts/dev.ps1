# Investment Auto 2.0 — development launcher for the DSH web app.
#
# Usage:
#   .\app\scripts\dev.ps1 [-Port 4567] [-Install] [-ForceSeed]
#
# Sets DSH_HOME to app\dev-home (gitignored user data), seeds profiles /
# presets / skills, and boots the investment-web profile. Ctrl+C stops it.

param(
  [int]$Port = 4567,
  [switch]$Install,
  [switch]$ForceSeed
)

$ErrorActionPreference = 'Stop'

$appRoot = Split-Path -Parent $PSScriptRoot
$repoRoot = Split-Path -Parent $appRoot
$devHome = Join-Path $appRoot 'dev-home'

if ($Install -or -not (Test-Path (Join-Path $appRoot 'node_modules\@deepseek-ai\dsh\package.json'))) {
  Write-Host 'Installing app dependencies (npm install)...'
  Push-Location $appRoot
  try { npm install --no-audit --no-fund; if ($LASTEXITCODE -ne 0) { throw 'npm install failed' } }
  finally { Pop-Location }
}

$env:DSH_HOME = $devHome
$env:DSH_TELEMETRY_DISABLED = '1'
$env:DSH_PERMISSION_MODE = 'danger-full-access'
$env:INVESTMENT_AUTO_ROOT = $repoRoot
$env:INVESTMENT_AUTO_APP_DIR = $appRoot
$env:INVESTMENT_ENGINE_APP_DIR = $appRoot
New-Item -ItemType Directory -Force -Path $devHome | Out-Null

& (Join-Path $PSScriptRoot 'seed.ps1') -Force:$ForceSeed

$bin = Join-Path $appRoot 'node_modules\@deepseek-ai\dsh\lib\bin.js'
Write-Host "Booting investment-web profile on http://127.0.0.1:$Port (Ctrl+C to stop)"
& node $bin --profile investment-web --port $Port
exit $LASTEXITCODE
