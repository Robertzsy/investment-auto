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

$output = "windows\desktop\bin\$Configuration\net8.0-windows"
Write-Host "Desktop build OK: $output"
Write-Host "Run: InvestmentAuto.Desktop.exe --app-root <project> [--data-root <data>]"
