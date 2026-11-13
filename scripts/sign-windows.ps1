[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]]$Path,
    [string]$CertificateBase64 = $env:KCP_SIGNING_CERTIFICATE_BASE64,
    [string]$CertificatePassword = $env:KCP_SIGNING_CERTIFICATE_PASSWORD,
    [string]$TimestampServer = 'http://timestamp.digicert.com'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $CertificateBase64 -or -not $CertificatePassword) {
    throw 'KCP signing certificate and password are required.'
}

$certificateBytes = [Convert]::FromBase64String($CertificateBase64)
$flags = [Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet -bor `
    [Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable
$certificate = [Security.Cryptography.X509Certificates.X509Certificate2]::new(
    $certificateBytes,
    $CertificatePassword,
    $flags
)
try {
    foreach ($candidate in $Path) {
        $resolved = (Resolve-Path -LiteralPath $candidate).Path
        $signature = Set-AuthenticodeSignature `
            -LiteralPath $resolved `
            -Certificate $certificate `
            -HashAlgorithm SHA256 `
            -TimestampServer $TimestampServer
        if ($signature.Status -ne 'Valid') {
            throw "Signing failed for $resolved with status $($signature.Status): $($signature.StatusMessage)"
        }
        Write-Host "Signed: $resolved"
    }
}
finally {
    $certificate.Dispose()
    [Array]::Clear($certificateBytes, 0, $certificateBytes.Length)
}
