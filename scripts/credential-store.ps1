Set-StrictMode -Version Latest

function Get-KcpCredentialStoreRoot {
    if ($env:KCP_CREDENTIAL_STORE_DIR) {
        return [IO.Path]::GetFullPath($env:KCP_CREDENTIAL_STORE_DIR)
    }
    if (-not $env:LOCALAPPDATA) {
        throw 'LOCALAPPDATA is unavailable; cannot locate the encrypted credential store.'
    }
    return (Join-Path $env:LOCALAPPDATA 'KaggleControlPlane\credentials')
}

function Assert-KcpCredentialRef {
    param([Parameter(Mandatory)][string]$CredentialRef)
    if ($CredentialRef -notmatch '^KCP_[A-Za-z0-9_]+$') {
        throw "Credential references must start with KCP_: $CredentialRef"
    }
}

function Get-KcpCredentialStorePath {
    param([Parameter(Mandatory)][string]$CredentialRef)
    Assert-KcpCredentialRef -CredentialRef $CredentialRef
    return (Join-Path (Get-KcpCredentialStoreRoot) "$CredentialRef.dpapi")
}

function Save-KcpCredential {
    param(
        [Parameter(Mandatory)][string]$CredentialRef,
        [Parameter(Mandatory)][Security.SecureString]$SecureValue
    )
    Assert-KcpCredentialRef -CredentialRef $CredentialRef
    if ($SecureValue.Length -eq 0) {
        throw "Cannot save an empty credential for $CredentialRef"
    }

    $storeRoot = Get-KcpCredentialStoreRoot
    $null = New-Item -ItemType Directory -Force -Path $storeRoot
    $encryptedValue = ConvertFrom-SecureString -SecureString $SecureValue
    Set-Content `
        -LiteralPath (Get-KcpCredentialStorePath -CredentialRef $CredentialRef) `
        -Value $encryptedValue `
        -Encoding ASCII `
        -NoNewline
}

function Import-KcpCredential {
    param([Parameter(Mandatory)][string]$CredentialRef)
    $storePath = Get-KcpCredentialStorePath -CredentialRef $CredentialRef
    if (-not (Test-Path -LiteralPath $storePath -PathType Leaf)) {
        return $false
    }

    $encryptedValue = (Get-Content -LiteralPath $storePath -Raw -ErrorAction Stop).Trim()
    if (-not $encryptedValue) {
        throw "Stored credential is empty: $storePath"
    }
    try {
        $secureValue = ConvertTo-SecureString -String $encryptedValue -ErrorAction Stop
    }
    catch {
        throw "Cannot decrypt $CredentialRef. The store is tied to the Windows user and machine that saved it."
    }

    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
    try {
        $plainValue = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        [Environment]::SetEnvironmentVariable($CredentialRef, $plainValue, 'Process')
    }
    finally {
        if ($null -ne $pointer) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }
        $plainValue = $null
        $secureValue.Dispose()
    }
    return $true
}

function Remove-KcpCredential {
    param([Parameter(Mandatory)][string]$CredentialRef)
    $storePath = Get-KcpCredentialStorePath -CredentialRef $CredentialRef
    if (Test-Path -LiteralPath $storePath -PathType Leaf) {
        Remove-Item -LiteralPath $storePath -Force
        return $true
    }
    return $false
}
