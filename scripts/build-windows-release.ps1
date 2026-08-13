param(
    [string]$Version = "0.4.0",
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $projectRoot "dist"
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
$launcherSource = Join-Path $projectRoot "windows\InvestmentAutoLauncher.cs"
$launcherExe = Join-Path $OutputDirectory "InvestmentAuto.exe"
$iconPath = Join-Path $OutputDirectory "investment-auto.ico"
$stage = Join-Path $OutputDirectory ("InvestmentAuto-Windows-v" + $Version)
$archive = $stage + ".zip"

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
if (Test-Path $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
if (Test-Path $archive) { Remove-Item -LiteralPath $archive -Force }

Add-Type -AssemblyName System.Drawing
$bitmap = New-Object System.Drawing.Bitmap 64, 64
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
try {
    $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $graphics.Clear([System.Drawing.Color]::FromArgb(30, 73, 184))
    $pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::White), 5
    try {
        $points = [System.Drawing.Point[]]@(
            (New-Object System.Drawing.Point 10, 45),
            (New-Object System.Drawing.Point 24, 31),
            (New-Object System.Drawing.Point 35, 38),
            (New-Object System.Drawing.Point 53, 17)
        )
        $graphics.DrawLines($pen, $points)
        $graphics.DrawLine($pen, 43, 17, 53, 17)
        $graphics.DrawLine($pen, 53, 17, 53, 27)
    } finally {
        $pen.Dispose()
    }
    $icon = [System.Drawing.Icon]::FromHandle($bitmap.GetHicon())
    try {
        $stream = [System.IO.File]::Open($iconPath, [System.IO.FileMode]::Create)
        try { $icon.Save($stream) } finally { $stream.Dispose() }
    } finally {
        $icon.Dispose()
    }
} finally {
    $graphics.Dispose()
    $bitmap.Dispose()
}

$provider = New-Object Microsoft.CSharp.CSharpCodeProvider
$parameters = New-Object System.CodeDom.Compiler.CompilerParameters
$parameters.GenerateExecutable = $true
$parameters.GenerateInMemory = $false
$parameters.IncludeDebugInformation = $false
$parameters.OutputAssembly = $launcherExe
$parameters.CompilerOptions = "/target:winexe /optimize+ /win32icon:`"$iconPath`""
@("System.dll", "System.Core.dll", "System.Drawing.dll", "System.Windows.Forms.dll", "System.Management.dll") |
    ForEach-Object { [void]$parameters.ReferencedAssemblies.Add($_) }
$result = $provider.CompileAssemblyFromFile($parameters, $launcherSource)
if ($result.Errors.HasErrors) {
    $messages = $result.Errors | ForEach-Object { "$($_.FileName):$($_.Line): $($_.ErrorText)" }
    throw "Launcher compilation failed:`n$($messages -join "`n")"
}

New-Item -ItemType Directory -Force -Path $stage | Out-Null
$rootFiles = @(
    ".env.example", "LICENSE", "README.md", "pyproject.toml", "requirements.txt",
    "requirements-lock.txt", "Setup-Windows.cmd"
)
foreach ($name in $rootFiles) {
    $source = Join-Path $projectRoot $name
    if (Test-Path $source) { Copy-Item -LiteralPath $source -Destination $stage }
}
foreach ($directory in @("config", "scripts", "src")) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $directory) -Destination $stage -Recurse
}
Get-ChildItem -LiteralPath $stage -Recurse -Directory -Filter "__pycache__" |
    Sort-Object FullName -Descending | Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $stage -Recurse -File |
    Where-Object { $_.Extension -in @(".pyc", ".pyo") } |
    Remove-Item -Force
Copy-Item -LiteralPath $launcherExe -Destination (Join-Path $stage "InvestmentAuto.exe")
New-Item -ItemType Directory -Force -Path (Join-Path $stage "runtime") | Out-Null

Compress-Archive -LiteralPath $stage -DestinationPath $archive -CompressionLevel Optimal
$checksum = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
$checksumFile = $archive + ".sha256"
[System.IO.File]::WriteAllText($checksumFile, "$checksum  $([System.IO.Path]::GetFileName($archive))`n", [System.Text.UTF8Encoding]::new($false))

Write-Host "Launcher: $launcherExe"
Write-Host "Release:  $archive"
Write-Host "SHA256:   $checksum"
