[CmdletBinding()]
param(
    [ValidateRange(1, 100)]
    [int]$Count = 10,

    [ValidatePattern('^[A-Za-z_][A-Za-z0-9_]*$')]
    [string]$Prefix = 'KCP_KAGGLE_MEMBER_',

    [string[]]$CredentialRefs,

    [switch]$Persist,

    [switch]$SaveCurrent,

    [switch]$Load,

    [switch]$Clear,

    [switch]$Forget
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'credential-store.ps1')

$operationCount = @($Persist, $SaveCurrent, $Load, $Clear, $Forget).Where({ $_ }).Count
if ($operationCount -gt 1) {
    throw 'Use only one of -Persist, -SaveCurrent, -Load, -Clear, or -Forget.'
}

if (-not $CredentialRefs -or $CredentialRefs.Count -eq 0) {
    $CredentialRefs = 1..$Count | ForEach-Object { '{0}{1:D2}' -f $Prefix, $_ }
}

foreach ($credentialRef in $CredentialRefs) {
    Assert-KcpCredentialRef -CredentialRef $credentialRef
}

if ($Forget) {
    $removedCount = 0
    foreach ($credentialRef in $CredentialRefs) {
        [Environment]::SetEnvironmentVariable($credentialRef, $null, 'Process')
        if (Remove-KcpCredential -CredentialRef $credentialRef) {
            $removedCount += 1
        }
    }
    Write-Host "Forgot $removedCount persisted credential(s) and cleared the selected process variables."
    return
}

if ($Clear) {
    foreach ($credentialRef in $CredentialRefs) {
        [Environment]::SetEnvironmentVariable($credentialRef, $null, 'Process')
    }
    Write-Host "Cleared $($CredentialRefs.Count) Kaggle credential variables from this PowerShell process."
    return
}

if ($Load) {
    foreach ($credentialRef in $CredentialRefs) {
        if (-not (Import-KcpCredential -CredentialRef $credentialRef)) {
            throw "No persisted credential exists for $credentialRef"
        }
    }
    Write-Host "Loaded $($CredentialRefs.Count) encrypted credential(s) into this terminal session."
    return
}

if ($SaveCurrent) {
    foreach ($credentialRef in $CredentialRefs) {
        $plainValue = [Environment]::GetEnvironmentVariable($credentialRef, 'Process')
        if (-not $plainValue) {
            throw "Credential environment variable $credentialRef is not set in this terminal."
        }
        $secureValue = ConvertTo-SecureString -String $plainValue -AsPlainText -Force
        try {
            Save-KcpCredential -CredentialRef $credentialRef -SecureValue $secureValue
        }
        finally {
            $plainValue = $null
            $secureValue.Dispose()
        }
    }
    Write-Host "Saved $($CredentialRefs.Count) credential(s) using Windows user-scoped encryption."
    Write-Host "Store: $(Get-KcpCredentialStoreRoot)"
    return
}

if ($Persist) {
    Write-Host 'Credentials will be encrypted for the current Windows user on this machine.'
    Write-Host "Store: $(Get-KcpCredentialStoreRoot)"
}
else {
    Write-Host 'Kaggle credentials will be held only in this PowerShell process environment.'
}
Write-Host 'No plaintext token is written to the repository or SQLite database.'

foreach ($credentialRef in $CredentialRefs) {
    $secureValue = Read-Host -Prompt "Paste the API token (or legacy credential JSON) for $credentialRef" -AsSecureString
    if ($secureValue.Length -eq 0) {
        throw "No credential was supplied for $credentialRef"
    }

    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
    try {
        $plainValue = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        [Environment]::SetEnvironmentVariable($credentialRef, $plainValue, 'Process')
        if ($Persist) {
            Save-KcpCredential -CredentialRef $credentialRef -SecureValue $secureValue
        }
    }
    finally {
        if ($null -ne $pointer) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }
        $plainValue = $null
        $secureValue.Dispose()
    }
}

Write-Host ''
Write-Host "Loaded $($CredentialRefs.Count) credentials for this terminal session."
if ($Persist) {
    Write-Host 'Encrypted copies were persisted; start-local.ps1 will load them automatically.'
}
Write-Host 'Use these exact references when adding the corresponding owners:'
$CredentialRefs | ForEach-Object { Write-Host "  $_" }
Write-Host 'Use -Clear to clear only this terminal, or -Forget to delete persisted copies too.'
