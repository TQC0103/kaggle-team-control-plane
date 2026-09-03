[CmdletBinding()]
param(
    [switch]$SkipWebBuild,
    [string]$Version = '0.2.0-beta.1'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$buildInfo = Join-Path $projectRoot 'control_plane\_build.py'
Push-Location $projectRoot
try {
    $buildSha = (git rev-parse --short=12 HEAD 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $buildSha) { $buildSha = 'development' }
    if ($Version -notmatch '^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$') {
        throw 'Version must be SemVer-like, for example 0.2.0-beta.1.'
    }
    Set-Content -LiteralPath $buildInfo -Value @(
        "APP_VERSION = '$Version'",
        "BUILD_SHA = '$buildSha'"
    ) -Encoding ASCII
    python -m pip install --disable-pip-version-check -r .\desktop-requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Desktop Python dependencies failed to install.' }

    if (-not $SkipWebBuild) {
        npm run build
        if ($LASTEXITCODE -ne 0) { throw 'Dashboard build failed.' }
    }
    if (-not (Test-Path .\dist\client\index.html)) {
        throw 'Static dashboard output is missing. Run without -SkipWebBuild.'
    }

    python -m PyInstaller `
        --noconfirm `
        --clean `
        --windowed `
        --onedir `
        --name KaggleControlPlane `
        --distpath .\release `
        --workpath .\build\desktop `
        --specpath .\build\desktop `
        --paths $projectRoot `
        --collect-all webview `
        --hidden-import webview.platforms.edgechromium `
        --add-data "$(Join-Path $projectRoot 'dist\client');dist\client" `
        (Join-Path $projectRoot 'desktop_app.py')
    if ($LASTEXITCODE -ne 0) { throw 'Desktop packaging failed.' }

    Write-Host ''
    Write-Host "Desktop app built: $(Join-Path $projectRoot 'release\KaggleControlPlane\KaggleControlPlane.exe')"
}
finally {
    Remove-Item -LiteralPath $buildInfo -Force -ErrorAction SilentlyContinue
    Pop-Location
}
