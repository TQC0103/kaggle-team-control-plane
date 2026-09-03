[CmdletBinding()]
param(
    [switch]$SkipAppBuild,
    [string]$Version = '0.2.0-beta.1'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $SkipAppBuild) {
    & (Join-Path $PSScriptRoot 'build-desktop.ps1') -Version $Version
}

$appExe = Join-Path $projectRoot 'release\KaggleControlPlane\KaggleControlPlane.exe'
if (-not (Test-Path $appExe)) {
    throw 'Desktop app is missing. Build the app first.'
}

$compilerCandidates = @(
    (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
    (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
)
$compiler = $compilerCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $compiler) {
    throw 'Inno Setup 6 is missing. Install JRSoftware.InnoSetup with winget.'
}

& $compiler "/DMyAppVersion=$Version" (Join-Path $projectRoot 'installer\KaggleControlPlane.iss')
if ($LASTEXITCODE -ne 0) { throw 'Setup compiler failed.' }

$setup = Join-Path $projectRoot 'release\installer\KaggleControlPlane-Setup.exe'
Write-Host ''
Write-Host "Single-file installer ready: $setup"
& (Join-Path $PSScriptRoot 'write-installer-checksum.ps1') -SetupPath $setup
