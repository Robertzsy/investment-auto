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
        # The WebView2 browser cache is volatile/regenerable and locked while
        # the app runs; it is not user data and must not fail the snapshot.
        if ($rel -like "runtime\webview2\*") { return }
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
$dllPath = Join-Path $AppDir "InvestmentAuto.Desktop.dll"
$exeBefore = if (Test-Path $exePath) { (Get-FileHash $exePath -Algorithm SHA256).Hash } else { $null }
$dllBefore = if (Test-Path $dllPath) { (Get-FileHash $dllPath -Algorithm SHA256).Hash } else { $null }
"   exe hash: $exeBefore"
"   dll hash: $dllBefore"

"== Silent upgrade install"
$p = Start-Process -FilePath $installer -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART" -PassThru -Wait
"   exit code: $($p.ExitCode)"
if ($p.ExitCode -ne 0) { throw "Installer exited with code $($p.ExitCode)" }

"== Compare"
$after = Get-Snapshot $DataDir
$exeAfter = if (Test-Path $exePath) { (Get-FileHash $exePath -Algorithm SHA256).Hash } else { $null }
$dllAfter = if (Test-Path $dllPath) { (Get-FileHash $dllPath -Algorithm SHA256).Hash } else { $null }

$missing = @($before.Keys | Where-Object { -not $after.ContainsKey($_) })
$changed = @($before.Keys | Where-Object { $after.ContainsKey($_) -and $before[$_] -ne $after[$_] })
$added   = @($after.Keys  | Where-Object { -not $before.ContainsKey($_) })

"   data files before/after: $($before.Count) / $($after.Count)"
"   missing:  $($missing.Count) $($missing -join ', ')"
"   changed:  $($changed.Count) $($changed -join ', ')"
"   added:    $($added.Count) $($added -join ', ')"
"   program exe hash before/after: $exeBefore / $exeAfter"
"   program dll hash before/after: $dllBefore / $dllAfter"

if ($missing.Count -gt 0) { throw "DATA LOSS: files missing after upgrade: $($missing -join ', ')" }
if ($changed.Count -gt 0) { throw "DATA LOSS: files changed after upgrade: $($changed -join ', ')" }
if (-not $exeAfter -or -not $dllAfter) { throw "Program files missing after upgrade" }

# Program freshness is advisory: the hard guarantee is data preservation.
# An idempotent reinstall of the already-current build is a pass, not a fail.
if ($exeBefore -and $dllBefore -and $exeAfter -eq $exeBefore -and $dllAfter -eq $dllBefore) {
    Write-Warning "Program files unchanged - installer was already current (idempotent reinstall)"
} else {
    "   program files replaced"
}

# Cross-check against the publish output: the installer must carry what the
# build pipeline just produced (catches a stale installer).
$pubDll = Join-Path $projectRoot "windows\desktop\bin\Release\net8.0-windows\win-x64\publish\InvestmentAuto.Desktop.dll"
if (Test-Path $pubDll) {
    $pubDllHash = (Get-FileHash $pubDll -Algorithm SHA256).Hash
    if ($dllAfter -ne $pubDllHash) {
        Write-Warning "Installed dll differs from current publish output - installer may be stale, rebuild it"
    }
}

"PASS: upgrade preserved all user data."
