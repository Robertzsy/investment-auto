# Investment Auto 2.0 — seed DSH home from the repo-owned sources.
#
# Copies (dev) or refreshes (production first-run / upgrade):
#   app/profiles/*            -> $DSH_HOME/profiles/*
#   app/presets/*             -> $DSH_HOME/.agent-presets/*
#   app/skills/*              -> $DSH_HOME/skills/*
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

function Copy-Tree([string]$Source, [string]$Target) {
  if (-not (Test-Path $Source)) { return }
  New-Item -ItemType Directory -Force -Path $Target | Out-Null
  Get-ChildItem $Source | ForEach-Object {
    $dest = Join-Path $Target $_.Name
    if ((Test-Path $dest) -and (-not $Force)) {
      Write-Host "  keep  $dest"
      return
    }
    if ($_.PSIsContainer) {
      Copy-Item -Recurse -Force $_.FullName $dest
      Write-Host "  seed  $dest"
    } else {
      Copy-Item -Force $_.FullName $dest
      Write-Host "  seed  $dest"
    }
  }
}

Write-Host "Seeding DSH home: $dshHome"
Copy-Tree (Join-Path $appRoot 'profiles') (Join-Path $dshHome 'profiles')
Copy-Tree (Join-Path $appRoot 'presets') (Join-Path $dshHome '.agent-presets')
Copy-Tree (Join-Path $appRoot 'skills') (Join-Path $dshHome 'skills')
Write-Host 'Done.'
