[CmdletBinding()]
param(
    [switch]$SkipExecutableBuild,
    [string]$NodeVersion = ""
)

# Build the actual user-facing artifact: one Inno Setup executable that
# installs an onedir Paperclip bundle containing its own portable Node runtime.
# Node/pnpm are build-time tools only. They are never prerequisites for users.
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$builder = Join-Path $repo "packaging\exe\build_exe.py"
$bundle = Join-Path $repo "packaging\exe\dist\paperclip"
$launcher = Join-Path $bundle "paperclip.exe"
$embeddedNode = Join-Path $bundle "_internal\runtime\node\node.exe"
$iss = Join-Path $repo "packaging\windows-installer\paperclip.iss"
$setup = Join-Path $repo "packaging\windows-installer\dist\Paperclip-Setup-0.3.1.exe"

if (-not $SkipExecutableBuild) {
    $python = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $python) {
        $pythonCommand = $python.Source
        $pythonPrefix = @("-3")
    } else {
        $python = Get-Command python -ErrorAction SilentlyContinue
        if ($null -eq $python) {
            throw "Python 3 is required at build time. It is not needed by installed users."
        }
        $pythonCommand = $python.Source
        $pythonPrefix = @()
    }

    $buildArgs = @(
        $builder,
        "--stage-payload",
        "--freeze",
        "--onedir",
        "--embed-node",
        "--run-doctor"
    )
    if ($NodeVersion) {
        $buildArgs += @("--node-version", $NodeVersion)
    }

    Write-Host "Building Paperclip with an embedded Node.js runtime..."
    & $pythonCommand @pythonPrefix @buildArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Paperclip executable build failed with exit code $LASTEXITCODE."
    }
}

if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "Standalone launcher not found: $launcher"
}
if (-not (Test-Path -LiteralPath $embeddedNode -PathType Leaf)) {
    throw "Embedded Node runtime not found: $embeddedNode. Rebuild with --embed-node."
}

$iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if ($null -eq $iscc) {
    $known = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    if ($known.Count -gt 0) {
        $isccPath = $known[0]
    } else {
        throw "Inno Setup 6 (ISCC.exe) is required to create the Setup.exe."
    }
} else {
    $isccPath = $iscc.Source
}

Write-Host "Creating the single Windows installer..."
& $isccPath $iss
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed with exit code $LASTEXITCODE."
}

if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) {
    throw "Inno Setup completed but did not create $setup"
}

$size = [math]::Round((Get-Item -LiteralPath $setup).Length / 1MB, 1)
Write-Host ""
Write-Host "Standalone installer ready: $setup ($size MB)"
Write-Host "Target requirements: Windows only. Node.js is included."
