# Release pre-flight for the 2.0 installer (build-machine preparation).
#
# The final ISCC.exe run and the VM acceptance pass need a dedicated build
# machine; this script verifies everything ISCC will need so that machine
# run succeeds first time:
#   1. dotnet publish of the desktop shell (self-contained win-x64)
#   2. every Source path in installer/InvestmentAuto.iss resolves
#      (runtime downloads are reported as build-machine steps, not errors)
#
#   powershell -ExecutionPolicy Bypass -File scripts\release-manifest-check.ps1

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

Write-Host "== 1. Desktop shell publish"
$dotnet = "dotnet"
foreach ($candidate in @("C:\Program Files\dotnet\dotnet.exe", "$env:ProgramFiles\dotnet\dotnet.exe")) {
    if (Test-Path $candidate) { $dotnet = $candidate; break }
}
& $dotnet publish "windows\desktop\InvestmentAuto.Desktop.csproj" -c Release -r win-x64 --self-contained true --nologo -v q
if ($LASTEXITCODE -ne 0) { throw "dotnet publish failed" }

Write-Host "== 2. Installer source manifest"
$iss = Get-Content "installer\InvestmentAuto.iss" -Raw
$missing = @()
$buildMachine = @()
foreach ($match in [regex]::Matches($iss, 'Source:\s*"([^"]+)"')) {
    $source = $match.Groups[1].Value
    if ($source -like "..\build\runtime\*" -or $source -like "..\build\runtime-downloads\*") {
        $buildMachine += $source
        continue
    }
    $path = Join-Path "installer" $source
    if (-not (Test-Path $path)) { $missing += $source }
}

Write-Host "   checked $(@([regex]::Matches($iss, 'Source:' )).Count) source entries"
if ($missing.Count -gt 0) {
    Write-Host ("   MISSING: " + ($missing -join ", ")) -ForegroundColor Red
    throw "installer manifest references missing files"
}
Write-Host "   all non-runtime sources present"
Write-Host "== 3. Build-machine steps (run before ISCC):"
foreach ($source in $buildMachine) { Write-Host "   - ensure $source (scripts\fetch-runtime.ps1 + scripts\bundle-runtime.ps1)" }
Write-Host "   - ISCC.exe installer\InvestmentAuto.iss"
Write-Host "   - scripts\verify-upgrade.ps1 (silent upgrade data-preservation)"
Write-Host "   - clean Win10/11 VM acceptance pass"
Write-Host ""
Write-Host "PRE-FLIGHT PASS: publish output and installer sources verified." -ForegroundColor Green
