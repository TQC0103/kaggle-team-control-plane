[CmdletBinding()]
param(
    [string]$SourceRoot,
    [switch]$Build
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ($Build) {
    & (Join-Path $PSScriptRoot 'build-desktop.ps1')
}

$builtRoot = Join-Path $projectRoot 'release\KaggleControlPlane'
$builtExe = Join-Path $builtRoot 'KaggleControlPlane.exe'
if (-not (Test-Path -LiteralPath $builtExe)) {
    throw 'Desktop build is missing. Run scripts\build-desktop.ps1 first or use -Build.'
}

if (-not $SourceRoot) {
    $SourceRoot = Join-Path $projectRoot 'experiments'
}
$resolvedSource = (Resolve-Path -LiteralPath $SourceRoot -ErrorAction Stop).Path
$installRoot = Join-Path $env:LOCALAPPDATA 'Programs\KaggleControlPlane'
$configRoot = Join-Path $env:LOCALAPPDATA 'KaggleControlPlane'
$configPath = Join-Path $configRoot 'desktop-config.json'
$null = New-Item -ItemType Directory -Force -Path $installRoot,$configRoot

Get-ChildItem -LiteralPath $builtRoot -Force | Copy-Item -Destination $installRoot -Recurse -Force

if (Test-Path -LiteralPath $configPath) {
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
}
else {
    $config = [pscustomobject]@{}
}
$config | Add-Member -NotePropertyName source_root -NotePropertyValue $resolvedSource -Force
$config | Add-Member -NotePropertyName legacy_database -NotePropertyValue (Join-Path $projectRoot 'data\control-plane.sqlite3') -Force
$config | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $configPath -Encoding UTF8

$installedExe = Join-Path $installRoot 'KaggleControlPlane.exe'
$shell = New-Object -ComObject WScript.Shell
$desktopShortcut = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'Kaggle Control Plane.lnk'))
$desktopShortcut.TargetPath = $installedExe
$desktopShortcut.WorkingDirectory = $installRoot
$desktopShortcut.Save()
$startMenu = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs\Kaggle Control Plane.lnk'
$startShortcut = $shell.CreateShortcut($startMenu)
$startShortcut.TargetPath = $installedExe
$startShortcut.WorkingDirectory = $installRoot
$startShortcut.Save()

Write-Host "Installed: $installedExe"
Write-Host 'Desktop and Start Menu shortcuts created.'
Write-Host 'Accounts, encrypted tokens, jobs, and app settings remain outside the install folder.'
