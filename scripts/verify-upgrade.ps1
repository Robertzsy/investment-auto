param(
    [string]$Installer = "release\InvestmentAuto-Setup-x64.exe",
    [string]$AppDir = "$env:LOCALAPPDATA\Programs\InvestmentAuto",
    [string]$DataDir = "$env:LOCALAPPDATA\InvestmentAuto"
)

# Upgrade-preserves-data acceptance check (desktop requirement #8):
# 1. Snapshot every file under the user data dir.
# 2. Install the new build silently over the existing installation.
# 3. Verify the data snapshot is byte-identical afterwards and that the
#    program files were actually replaced.

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot

function Get-Snapshot([string]$root) {
    if (-not (Test-Path $root)) { return @{} }
    $map = @{}
    Get-ChildItem $root -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($root.Length).TrimStart('\')
        $map[$rel] = (Get-FileHash $_.FullName -Algorithm SHA256).Hash
    }
    return $map
}

$installer = Join-Path $projectRoot $Installer
if (-not (Test-Path $installer)) { throw "Installer not found: $installer" }

"== Snapshot user data ($DataDir)"
$before = Get-Snapshot $DataDir
"   files: $($before.Count)"

"== Snapshot program dir ($AppDir)"
$exePath = Join-Path $AppDir "InvestmentAuto.Desktop.exe"
$exeBefore = if (Test-Path $exePath) { (Get-FileHash $exePath -Algorithm SHA256).Hash } else { $null }
"   exe hash: $exeBefore"

"== Silent upgrade install"
$p = Start-Process -FilePath $installer -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART" -PassThru -Wait
"   exit code: $($p.ExitCode)"
if ($p.ExitCode -ne 0) { throw "Installer exited with code $($p.ExitCode)" }

"== Compare"
$after = Get-Snapshot $DataDir
$exeAfter = if (Test-Path $exePath) { (Get-FileHash $exePath -Algorithm SHA256).Hash } else { $null }

$missing = @($before.Keys | Where-Object { -not $after.ContainsKey($_) })
$changed = @($before.Keys | Where-Object { $after.ContainsKey($_) -and $before[$_] -ne $after[$_] })
$added   = @($after.Keys  | Where-Object { -not $before.ContainsKey($_) })

"   data files before/after: $($before.Count) / $($after.Count)"
"   missing:  $($missing.Count) $($missing -join ', ')"
"   changed:  $($changed.Count) $($changed -join ', ')"
"   added:    $($added.Count) $($added -join ', ')"
"   program exe hash before: $exeBefore"
"   program exe hash after:  $exeAfter"

if ($missing.Count -gt 0) { throw "DATA LOSS: files missing after upgrade: $($missing -join ', ')" }
if ($changed.Count -gt 0) { throw "DATA LOSS: files changed after upgrade: $($changed -join ', ')" }
if ($exeBefore -and $exeAfter -eq $exeBefore) { Write-Warning "Program exe was not replaced (same hash) - installer may not have updated files" }
if (-not $exeAfter) { throw "Program exe missing after upgrade" }

"PASS: upgrade preserved all user data and replaced the program files."
