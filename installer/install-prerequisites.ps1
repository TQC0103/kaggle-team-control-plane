$ErrorActionPreference = 'Stop'

function Find-Python {
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) { return $python.Source }
    $candidate = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'
    if (Test-Path -LiteralPath $candidate) { return $candidate }
    return $null
}

$pythonPath = Find-Python
if (-not $pythonPath) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw 'Python is missing and Windows Package Manager is unavailable.'
    }
    & $winget.Source install --id Python.Python.3.11 --exact --scope user --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw 'Python installation failed.' }
    $pythonPath = Find-Python
}
if (-not $pythonPath) { throw 'Python 3.11 could not be located after installation.' }

& $pythonPath -m pip install --disable-pip-version-check --upgrade kaggle==2.2.4
if ($LASTEXITCODE -ne 0) { throw 'Kaggle CLI installation failed.' }
