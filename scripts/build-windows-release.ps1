param(
    [string]$Version = "2.1.3",
    [string]$OutputDirectory = "",
    [switch]$SkipDesktopBuild
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $projectRoot "dist"
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
$stage = Join-Path $OutputDirectory ("InvestmentAuto-Windows-v" + $Version)
$archive = $stage + ".zip"
$desktopPublish = Join-Path $projectRoot "windows\desktop\bin\Release\net8.0-windows\win-x64\publish"
$pythonRuntime = Join-Path $projectRoot "build\runtime\python"
$nodeRuntime = Join-Path $projectRoot "build\runtime\node\node-v20.18.1-win-x64"

if (-not $SkipDesktopBuild) {
    & (Join-Path $PSScriptRoot "build-desktop.ps1") -Configuration Release -Runtime win-x64
    if ($LASTEXITCODE -ne 0) { throw "Desktop build failed" }
}
if (-not (Test-Path (Join-Path $desktopPublish "InvestmentAuto.Desktop.exe"))) {
    throw "WPF desktop publish output is missing: $desktopPublish"
}
if (-not (Test-Path (Join-Path $pythonRuntime "python.exe"))) {
    throw "Bundled Python runtime is missing: $pythonRuntime"
}
if (-not (Test-Path (Join-Path $nodeRuntime "node.exe"))) {
    throw "Bundled Node runtime is missing: $nodeRuntime"
}

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
if (Test-Path $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
if (Test-Path $archive) { Remove-Item -LiteralPath $archive -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

# The portable archive now ships the same WPF/WebView2 shell as the installer.
Copy-Item -Path (Join-Path $desktopPublish "*") -Destination $stage -Recurse -Force
Copy-Item -LiteralPath $pythonRuntime -Destination (Join-Path $stage "python") -Recurse
Copy-Item -LiteralPath $nodeRuntime -Destination (Join-Path $stage "node") -Recurse

$rootFiles = @(
    ".env.example", "LICENSE", "README.md", "README_EN.md", "pyproject.toml",
    "requirements.txt", "requirements-lock.txt", "Setup-Windows.cmd"
)
foreach ($name in $rootFiles) {
    $source = Join-Path $projectRoot $name
    if (Test-Path $source) { Copy-Item -LiteralPath $source -Destination $stage }
}
foreach ($directory in @("config", "scripts", "src")) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $directory) -Destination $stage -Recurse
}

New-Item -ItemType Directory -Force -Path (Join-Path $stage "runtime") | Out-Null
Get-ChildItem -LiteralPath $stage -Recurse -Directory -Filter "__pycache__" |
    Sort-Object FullName -Descending | Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $stage -Recurse -File |
    Where-Object { $_.Extension -in @(".pyc", ".pyo") } |
    Remove-Item -Force

Compress-Archive -LiteralPath $stage -DestinationPath $archive -CompressionLevel Optimal
$checksum = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
$checksumFile = $archive + ".sha256"
[System.IO.File]::WriteAllText(
    $checksumFile,
    "$checksum  $([System.IO.Path]::GetFileName($archive))`n",
    [System.Text.UTF8Encoding]::new($false)
)

Write-Host "Desktop executable: $(Join-Path $stage 'InvestmentAuto.Desktop.exe')"
Write-Host "Portable release:   $archive"
Write-Host "SHA256:             $checksum"
