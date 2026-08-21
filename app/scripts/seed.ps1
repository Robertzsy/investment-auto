# Investment Auto 2.0 — seed DSH home from the repo-owned sources.
#
# Copies (dev) or refreshes (production first-run / upgrade):
#   app/profiles/*            -> $DSH_HOME/profiles/*
#   app/presets/*             -> $DSH_HOME/.agent-presets/*
#   app/skills/*              -> $DSH_HOME/skills/*
#   app/plugins/*             -> $DSH_HOME/profiles/<profile>/node_modules/<scope>/<name>
#
# Production uses the same script: the installer ships app/ and runs this
# against %LocalAppData%\InvestmentAuto (set $env:DSH_HOME before calling).
# User-owned files are never overwritten: a target that already exists is
# left alone unless -Force is passed.

param(
  [switch]$Force
)

$ErrorActionPreference = 'Stop'

if (-not $env:DSH_HOME) {
  throw 'DSH_HOME is not set. Set it to the dev home (app\dev-home) or the user data directory.'
}

$appRoot = Split-Path -Parent $PSScriptRoot
$dshHome = $env:DSH_HOME

# robocopy exit codes 0-7 are success; >=8 is failure. Contents of $Source
# are copied into $Target (no nested source dir), unlike Copy-Item -Recurse.
function Copy-Tree([string]$Source, [string]$Target) {
  if (-not (Test-Path $Source)) { return }
  if ((Test-Path $Target) -and (-not $Force)) {
    Write-Host "  keep  $Target (exists; use -Force to refresh)"
    return
  }
  New-Item -ItemType Directory -Force -Path $Target | Out-Null
  robocopy $Source $Target /E /R:0 /W:0 /NFL /NDL /NJH /NJS /NP | Out-Null
  if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE): $Source -> $Target" }
  Write-Host "  seed  $Target"
}

function Install-Plugins([string]$PluginsRoot, [string]$ProfilesRoot) {
  if (-not (Test-Path $PluginsRoot)) { return }
  Get-ChildItem $PluginsRoot -Directory | ForEach-Object {
    $plugin = $_
    $manifestPath = Join-Path $plugin.FullName 'package.json'
    if (-not (Test-Path $manifestPath)) { return }
    $manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
    $pkgName = $manifest.name
    if (-not $pkgName -or -not $pkgName.Contains('/')) {
      Write-Warning "plugin $($plugin.Name) has no scoped package name; skipped"
      return
    }
    Get-ChildItem $ProfilesRoot -Directory | Where-Object { $_.Name -ne 'node_modules' } | ForEach-Object {
      $dest = Join-Path $_.FullName "node_modules\$pkgName"
      if ((Test-Path $dest) -and (-not $Force)) {
        Write-Host "  keep  $dest"
        return
      }
      if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
      New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
      robocopy $plugin.FullName $dest /E /R:0 /W:0 /NFL /NDL /NJH /NJS /NP | Out-Null
      if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE): $($plugin.FullName) -> $dest" }
      Write-Host "  seed  $dest"
    }
  }
}

Write-Host "Seeding DSH home: $dshHome"
Copy-Tree (Join-Path $appRoot 'profiles') (Join-Path $dshHome 'profiles')
Copy-Tree (Join-Path $appRoot 'presets') (Join-Path $dshHome '.agent-presets')
Copy-Tree (Join-Path $appRoot 'skills') (Join-Path $dshHome 'skills')
Install-Plugins (Join-Path $appRoot 'plugins') (Join-Path $dshHome 'profiles')
Write-Host 'Done.'
