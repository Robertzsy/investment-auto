param(
    [switch]$SkipUpgrade,
    [switch]$SkipPythonVenv,
    [switch]$SkipPythonBundled,
    [switch]$SkipDotNet,
    [string]$Installer = "release\InvestmentAuto-Setup-x64.exe"
)

# Release-candidate gate (P5): runs every automated acceptance check and
# prints the release manifest (SHA-256 + size). Any failure exits non-zero.
#
#   powershell -ExecutionPolicy Bypass -File scripts\release-check.ps1
#
# Switches skip individual gates (VM runs may want -SkipUpgrade, for example).

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot
$script:failures = @()

function Run-Step([string]$name, [scriptblock]$body) {
    Write-Host "== $name"
    try {
        & $body
        Write-Host "   PASS"
    } catch {
        # script: scope - a plain += would shadow the caller's list inside
        # the function and silently produce a green exit code.
        $script:failures += $name
        Write-Host ("   FAIL: " + $_.Exception.Message) -ForegroundColor Red
    }
}

# 1. Desktop shell unit tests (single instance, ready parse, pythonw locate,
#    autostart, process manager) - test requirements #2/#3/#5/#6.
if (-not $SkipDotNet) {
    $dotnet = "dotnet"
    foreach ($candidate in @("C:\Program Files\dotnet\dotnet.exe", "$env:ProgramFiles\dotnet\dotnet.exe")) {
        if (Test-Path $candidate) { $dotnet = $candidate; break }
    }
    Run-Step "C# desktop tests (dotnet test)" {
        & $dotnet test "windows\desktop\InvestmentAuto.Desktop.Tests\InvestmentAuto.Desktop.Tests.csproj" -c Release --nologo | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "dotnet test exit code $LASTEXITCODE" }
    }
}

# 2. Dev-mode regression: full Python suite on the dev venv (test req #14).
if (-not $SkipPythonVenv -and (Test-Path ".venv\Scripts\python.exe")) {
    Run-Step "Python suite on .venv (dev-mode regression)" {
        & ".venv\Scripts\python.exe" -m pytest tests -q --no-header | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "venv pytest exit code $LASTEXITCODE" }
    }
}

# 3. Bundled runtime must be able to run the same suite (offline deps ok).
if (-not $SkipPythonBundled) {
    $bundled = "build\runtime\python\python.exe"
    if (-not (Test-Path $bundled)) { throw "bundled runtime missing: $bundled (run scripts\bundle-runtime.ps1 first)" }
    Run-Step "Python suite on bundled runtime" {
        & $bundled -m pytest tests -q --no-header | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "bundled pytest exit code $LASTEXITCODE" }
    }
}

# 4. Upgrade over an existing install must preserve every user data byte
#    (test req #8).
if (-not $SkipUpgrade) {
    Run-Step "Upgrade preserves user data" {
        & "$PSScriptRoot\verify-upgrade.ps1" -Installer $Installer | Out-Null
    }
}

# 5. Release manifest.
Run-Step "Installer manifest" {
    $installer = Join-Path $projectRoot $Installer
    if (-not (Test-Path $installer)) { throw "installer missing: $installer" }
    $hash = Get-FileHash $installer -Algorithm SHA256
    Write-Host ("   SHA-256: " + $hash.Hash)
    Write-Host ("   size:    " + (Get-Item $installer).Length + " bytes")
}

Write-Host ""
if ($script:failures.Count -gt 0) {
    Write-Host ("RELEASE CHECK FAILED: " + ($script:failures -join ", ")) -ForegroundColor Red
    exit 1
}
Write-Host "RELEASE CHECK PASSED - all automated gates green." -ForegroundColor Green
