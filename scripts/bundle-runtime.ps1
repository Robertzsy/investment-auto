param(
    [string]$Downloads = "",
    [string]$Output = ""
)

# Bundles the portable runtimes for the desktop installer:
#   python/  - full relocatable CPython (python.org NuGet package) with ALL
#              locked dependencies installed offline
#   node/    - official Node.js Windows zip
#   webview2 - Evergreen bootstrapper (kept next to the bundles)
# Build-time only: end users never run PowerShell or pip.

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Downloads) { $Downloads = Join-Path $projectRoot "build\runtime-downloads" }
if (-not $Output) { $Output = Join-Path $projectRoot "build\runtime" }
$Downloads = [System.IO.Path]::GetFullPath($Downloads)
$Output = [System.IO.Path]::GetFullPath($Output)
New-Item -ItemType Directory -Force -Path $Output | Out-Null

Add-Type -AssemblyName System.IO.Compression.FileSystem

# 1) Python from the nupkg (tools/ prefix -> python/)
$nupkg = Get-ChildItem $Downloads -Filter "python-*.nupkg" | Select-Object -First 1
if (-not $nupkg) { throw "未找到 python nupkg，请先运行 fetch-runtime.ps1" }
$pythonDir = Join-Path $Output "python"
if (Test-Path $pythonDir) { Remove-Item $pythonDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $pythonDir | Out-Null
$zip = [System.IO.Compression.ZipFile]::OpenRead($nupkg.FullName)
foreach ($entry in $zip.Entries) {
    if ($entry.FullName -like "tools/*" -and $entry.FullName -ne "tools/") {
        $relative = $entry.FullName.Substring(6)
        $target = Join-Path $pythonDir $relative
        if ($entry.FullName.EndsWith("/")) {
            New-Item -ItemType Directory -Force -Path $target | Out-Null
        } else {
            $parent = Split-Path $target -Parent
            if ($parent -and -not (Test-Path $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
        }
    }
}
$zip.Dispose()

# 2) Node zip (flattened: build/runtime/node/node.exe for the installer)
$nodeZip = Get-ChildItem $Downloads -Filter "node-*-win-x64.zip" | Select-Object -First 1
if (-not $nodeZip) { throw "未找到 node zip，请先运行 fetch-runtime.ps1" }
$nodeDir = Join-Path $Output "node"
if (Test-Path $nodeDir) { Remove-Item $nodeDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $nodeDir | Out-Null
$nodeStage = Join-Path $Output "node-stage"
if (Test-Path $nodeStage) { Remove-Item $nodeStage -Recurse -Force }
[System.IO.Compression.ZipFile]::ExtractToDirectory($nodeZip.FullName, $nodeStage)
$nodeVersionDir = Join-Path $nodeStage (Get-ChildItem $nodeStage -Directory | Select-Object -First 1).Name
Get-ChildItem (Join-Path $nodeStage $nodeVersionDir) | ForEach-Object { Move-Item $_.FullName $nodeDir }
Remove-Item $nodeStage -Recurse -Force
$nodeRoot = $nodeDir

# 3) WebView2 bootstrapper stays in downloads; installer copies it.

# 4) Offline dependencies into the bundled python
#
#    The bundle must be fully self-contained. User-site must be invisible to
#    pip (PYTHONNOUSERSITE + -s), otherwise the build machine's own packages
#    are mistaken for the bundle's and the installer ships a runtime that
#    only works on machines that already have Python.
$wheels = Join-Path $projectRoot "build\wheels"
$python = Join-Path $pythonDir "python.exe"
$env:PYTHONNOUSERSITE = "1"
if (-not (Get-ChildItem $wheels -Filter "*.whl" -ErrorAction SilentlyContinue)) {
    Write-Host "Downloading locked wheels ..."
    New-Item -ItemType Directory -Force -Path $wheels | Out-Null
    & $python -s -m pip download -r (Join-Path $projectRoot "requirements-lock.txt") -d $wheels
    if ($LASTEXITCODE -ne 0) { throw "wheels 下载失败" }
}
Write-Host "Installing dependencies offline ..."
& $python -s -m pip install --no-index --find-links $wheels --ignore-installed -r (Join-Path $projectRoot "requirements-lock.txt")
if ($LASTEXITCODE -ne 0) { throw "依赖离线安装失败" }

# Self-containment gate: the bundle must satisfy itself without user-site.
& $python -s -m pip check
if ($LASTEXITCODE -ne 0) { throw "捆绑 Python 依赖不完整（pip check 失败）" }
& $python -s -c "import pydantic, typing_extensions, pandas, numpy, pymongo, openai, httpx, requests, yaml, apscheduler, dotenv, tzlocal, h11, httpcore; print('bundle imports ok')"
if ($LASTEXITCODE -ne 0) { throw "捆绑 Python 关键模块导入失败" }

Write-Host ""
Write-Host "Bundled runtimes:"
Write-Host ("  python: " + $pythonDir)
Write-Host ("  node:   " + $nodeRoot)
& $python --version
& (Join-Path $nodeRoot "node.exe") --version
