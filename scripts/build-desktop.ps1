param(
    [string]$Configuration = "Release",
    [string]$Runtime = "win-x64"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

$dotnet = "dotnet"
$candidates = @(
    "C:\Program Files\dotnet\dotnet.exe",
    "$env:ProgramFiles\dotnet\dotnet.exe"
)
foreach ($candidate in $candidates) {
    if (Test-Path $candidate) { $dotnet = $candidate; break }
}

& $dotnet build "windows\desktop\InvestmentAuto.Desktop.csproj" -c $Configuration
if ($LASTEXITCODE -ne 0) { throw "Desktop build failed" }

# The installer consumes the self-contained publish output; produce it here so
# a clean environment can reproduce the release package in one command.
& $dotnet publish "windows\desktop\InvestmentAuto.Desktop.csproj" -c $Configuration -r $Runtime --self-contained true
if ($LASTEXITCODE -ne 0) { throw "Desktop publish failed" }

$output = "windows\desktop\bin\$Configuration\net8.0-windows\$Runtime\publish"
Write-Host "Desktop build OK: $output"
Write-Host "Next: ISCC.exe installer\InvestmentAuto.iss"
Write-Host "Run: InvestmentAuto.Desktop.exe --app-root <project> [--data-root <data>]"
