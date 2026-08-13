[CmdletBinding()]
param([switch]$SkipWebBuild)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $projectRoot
try {
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
    Pop-Location
}
