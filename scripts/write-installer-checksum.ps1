[CmdletBinding()]
param(
    [string]$SetupPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $SetupPath) {
    $SetupPath = Join-Path $projectRoot 'release\installer\KaggleControlPlane-Setup.exe'
}
$resolvedSetup = (Resolve-Path -LiteralPath $SetupPath).Path
$checksum = "$resolvedSetup.sha256"
$setupHash = (Get-FileHash -LiteralPath $resolvedSetup -Algorithm SHA256).Hash
Set-Content -LiteralPath $checksum -Value "$setupHash  $([IO.Path]::GetFileName($resolvedSetup))" -Encoding ASCII
Write-Host "SHA-256: $setupHash"
Write-Host "Checksum file: $checksum"
