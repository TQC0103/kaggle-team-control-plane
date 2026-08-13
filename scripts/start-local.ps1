[CmdletBinding()]
param(
    [ValidateSet('Demo', 'Real')]
    [string]$Mode = 'Demo',

    [string]$SourceRoot,

    [string[]]$CredentialRefs,

    [ValidateRange(1, 65535)]
    [int]$ApiPort = 8765,

    [ValidateRange(1, 65535)]
    [int]$DashboardPort = 3100,

    [switch]$InstallDependencies,

    [switch]$DevDashboard,

    [switch]$RebuildDashboard,

    [switch]$VerifyDemo,

    [switch]$ExitAfterReady
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'credential-store.ps1')
$pythonCommand = Get-Command python -ErrorAction Stop
$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npmCommand) {
    $npmCommand = Get-Command npm -ErrorAction Stop
}

function Stop-LocalProcessTree {
    param([System.Diagnostics.Process]$RootProcess)

    if ($null -eq $RootProcess -or $RootProcess.HasExited) {
        return
    }

    try {
        $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($RootProcess.Id)" -ErrorAction Stop
        foreach ($child in $children) {
            try {
                $childProcess = Get-Process -Id $child.ProcessId -ErrorAction Stop
                Stop-LocalProcessTree -RootProcess $childProcess
            }
            catch {
                # The child may already have exited.
            }
        }
    }
    catch {
        # Process cleanup still attempts the known root process below.
    }

    Stop-Process -Id $RootProcess.Id -Force -ErrorAction SilentlyContinue
}

function Get-LogTail {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path) {
        return (Get-Content -LiteralPath $Path -Tail 30 -ErrorAction SilentlyContinue) -join [Environment]::NewLine
    }
    return ''
}

function Wait-LocalEndpoint {
    param(
        [string]$Url,
        [System.Diagnostics.Process]$Process,
        [string]$Name,
        [string]$ErrorLog,
        [int]$TimeoutSeconds = 45,
        [int]$RequestTimeoutSeconds = 2
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($Process.HasExited) {
            $tail = Get-LogTail -Path $ErrorLog
            throw "$Name exited before becoming ready.`n$tail"
        }
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $RequestTimeoutSeconds
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                return
            }
        }
        catch {
            # Expected while the local process is starting.
        }
        Start-Sleep -Milliseconds 250
    }
    throw "$Name did not become ready at $Url within $TimeoutSeconds seconds."
}

function Wait-LocalTcpPort {
    param(
        [string]$HostName,
        [int]$Port,
        [System.Diagnostics.Process]$Process,
        [string]$Name,
        [string]$ErrorLog,
        [int]$TimeoutSeconds = 90
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($Process.HasExited) {
            $tail = Get-LogTail -Path $ErrorLog
            throw "$Name exited before opening port $Port.`n$tail"
        }
        $client = [Net.Sockets.TcpClient]::new()
        try {
            $pending = $client.ConnectAsync($HostName, $Port)
            if ($pending.Wait(1000) -and $client.Connected) {
                return
            }
        }
        catch {
            # Expected while Vite is loading its dependency graph.
        }
        finally {
            $client.Dispose()
        }
        Start-Sleep -Milliseconds 250
    }
    throw "$Name did not open ${HostName}:$Port within $TimeoutSeconds seconds."
}

Push-Location $projectRoot
$startupWatch = [Diagnostics.Stopwatch]::StartNew()
$apiProcess = $null
$dashboardProcess = $null
$credentialValues = @{}
$savedCredentialEnvironment = @{}
$environmentNames = @(
    'KCP_API_TOKEN',
    'KCP_ADAPTER',
    'KCP_DB_PATH',
    'KCP_DATA_DIR',
    'KCP_ALLOWED_SOURCE_ROOT',
    'KCP_MAX_WORKERS',
    'KCP_MAX_JOBS_PER_ACCOUNT',
    'KCP_QUOTA_START_DELAY_SECONDS',
    'KCP_CREDENTIAL_REFS',
    'NEXT_PUBLIC_CONTROL_PLANE_URL'
)
$savedEnvironment = @{}
foreach ($environmentName in $environmentNames) {
    $savedEnvironment[$environmentName] = [Environment]::GetEnvironmentVariable($environmentName, 'Process')
}

try {
    if (-not $InstallDependencies -and -not (Test-Path -LiteralPath (Join-Path $projectRoot 'node_modules'))) {
        throw 'node_modules is missing. Rerun with -InstallDependencies.'
    }

    $runStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $runRoot = Join-Path $projectRoot "work\local-$($Mode.ToLowerInvariant())-$runStamp"
    $null = New-Item -ItemType Directory -Force -Path $runRoot
    $apiOut = Join-Path $runRoot 'api.stdout.log'
    $apiErr = Join-Path $runRoot 'api.stderr.log'
    $dashboardOut = Join-Path $runRoot 'dashboard.stdout.log'
    $dashboardErr = Join-Path $runRoot 'dashboard.stderr.log'

    # This launcher is deliberately loopback-only. A browser-visible bearer
    # token would be worse than no token, so remote access needs a reverse proxy.
    $env:KCP_API_TOKEN = $null
    $env:NEXT_PUBLIC_CONTROL_PLANE_URL = "http://127.0.0.1:$ApiPort"

    if ($Mode -eq 'Demo') {
        $databasePath = Join-Path $runRoot 'demo.sqlite3'
        $apiArguments = @(
            'scripts/demo_server.py',
            '--host', '127.0.0.1',
            '--port', $ApiPort.ToString(),
            '--db', ('"{0}"' -f $databasePath),
            '--data-dir', ('"{0}"' -f (Join-Path $runRoot 'runtime'))
        )
    }
    else {
        if (-not $SourceRoot) {
            throw '-SourceRoot is required in Real mode.'
        }
        $resolvedSourceRoot = (Resolve-Path -LiteralPath $SourceRoot -ErrorAction Stop).Path
        # Resolving the executable is enough for startup. Running `kaggle
        # --version` starts Python and adds several seconds even though each
        # real job already reports an actionable CLI error if invocation fails.
        $null = Get-Command kaggle -ErrorAction Stop
        if (-not $CredentialRefs -or $CredentialRefs.Count -eq 0) {
            throw '-CredentialRefs is required in Real mode.'
        }
        foreach ($credentialRef in $CredentialRefs) {
            Assert-KcpCredentialRef -CredentialRef $credentialRef
            $credentialValue = [Environment]::GetEnvironmentVariable($credentialRef, 'Process')
            $savedCredentialEnvironment[$credentialRef] = $credentialValue
            if (-not $credentialValue) {
                if (Import-KcpCredential -CredentialRef $credentialRef) {
                    $credentialValue = [Environment]::GetEnvironmentVariable($credentialRef, 'Process')
                    Write-Host "Loaded encrypted credential for $credentialRef."
                }
                else {
                    throw "Credential $credentialRef is not set and has no encrypted saved copy. Run set-team-credentials.ps1 -Persist once."
                }
            }
            $credentialValues[$credentialRef] = $credentialValue
        }

        $env:KCP_ADAPTER = 'kaggle'
        $env:KCP_DB_PATH = Join-Path $projectRoot 'data\control-plane.sqlite3'
        $env:KCP_DATA_DIR = Join-Path $projectRoot 'data\runtime'
        $env:KCP_ALLOWED_SOURCE_ROOT = $resolvedSourceRoot
        $env:KCP_MAX_WORKERS = '10'
        $env:KCP_MAX_JOBS_PER_ACCOUNT = '2'
        $env:KCP_QUOTA_START_DELAY_SECONDS = '8'
        $env:KCP_CREDENTIAL_REFS = $CredentialRefs -join ','
        $apiArguments = @('-m', 'control_plane', '--host', '127.0.0.1', '--port', $ApiPort.ToString())
    }

    $apiProcess = Start-Process `
        -FilePath $pythonCommand.Source `
        -ArgumentList $apiArguments `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $apiOut `
        -RedirectStandardError $apiErr `
        -PassThru

    # Kaggle credentials belong only in the backend. Temporarily remove the
    # named values before npm/build and the dashboard process inherit the
    # environment, then restore them for this terminal in the inner finally.
    foreach ($credentialRef in $credentialValues.Keys) {
        [Environment]::SetEnvironmentVariable($credentialRef, $null, 'Process')
    }

    try {
        if ($InstallDependencies) {
            & $npmCommand.Source ci
            if ($LASTEXITCODE -ne 0) {
                throw "npm ci failed with exit code $LASTEXITCODE"
            }
        }

        if ($DevDashboard) {
            # Development mode keeps hot reload, but its first RSC compilation
            # is intentionally not part of the normal fast startup path.
            $dashboardCommand = Join-Path $projectRoot 'node_modules\.bin\vite.cmd'
            if (-not (Test-Path -LiteralPath $dashboardCommand)) {
                throw 'The local Vite executable is missing. Rerun with -InstallDependencies.'
            }
            $dashboardArguments = @(
                '--config', 'vite.local.config.ts',
                '--host', '127.0.0.1',
                '--port', $DashboardPort.ToString(),
                '--strictPort'
            )
        }
        else {
            $buildMarker = Join-Path $projectRoot 'dist\server\BUILD_ID'
            $buildInputs = @(
                (Join-Path $projectRoot 'app'),
                (Join-Path $projectRoot 'public'),
                (Join-Path $projectRoot 'package.json'),
                (Join-Path $projectRoot 'package-lock.json'),
                (Join-Path $projectRoot 'next.config.ts'),
                (Join-Path $projectRoot 'vite.config.ts')
            )
            $needsBuild = $RebuildDashboard -or -not (Test-Path -LiteralPath $buildMarker)
            if (-not $needsBuild) {
                $builtAt = (Get-Item -LiteralPath $buildMarker).LastWriteTimeUtc
                foreach ($inputPath in $buildInputs) {
                    if (-not (Test-Path -LiteralPath $inputPath)) { continue }
                    $latestInput = Get-Item -LiteralPath $inputPath
                    if ($latestInput.PSIsContainer) {
                        $latestInput = Get-ChildItem -LiteralPath $inputPath -Recurse -File |
                            Sort-Object LastWriteTimeUtc -Descending |
                            Select-Object -First 1
                    }
                    if ($latestInput -and $latestInput.LastWriteTimeUtc -gt $builtAt) {
                        $needsBuild = $true
                        break
                    }
                }
            }
            if ($needsBuild) {
                Write-Host 'Dashboard changed; creating the fast-start build once...'
                & $npmCommand.Source run build
                if ($LASTEXITCODE -ne 0) {
                    throw "Dashboard build failed with exit code $LASTEXITCODE"
                }
            }

            $dashboardCommand = $pythonCommand.Source
            $dashboardArguments = @(
                'scripts/static_dashboard.py',
                '--host', '127.0.0.1',
                '--port', $DashboardPort.ToString()
                '--directory', ('"{0}"' -f (Join-Path $projectRoot 'dist\client'))
            )
        }
        $dashboardProcess = Start-Process `
            -FilePath $dashboardCommand `
            -ArgumentList $dashboardArguments `
            -WorkingDirectory $projectRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $dashboardOut `
            -RedirectStandardError $dashboardErr `
            -PassThru
    }
    finally {
        foreach ($credentialRef in $credentialValues.Keys) {
            [Environment]::SetEnvironmentVariable($credentialRef, $savedCredentialEnvironment[$credentialRef], 'Process')
        }
    }

    # Both processes start in parallel. The backend inherited credentials
    # before they were removed from the dashboard environment.
    Wait-LocalEndpoint `
        -Url "http://127.0.0.1:$ApiPort/api/health" `
        -Process $apiProcess `
        -Name 'Control-plane API' `
        -ErrorLog $apiErr

    if ($DevDashboard) {
        # Do not abort development-mode RSC compilation with short HTTP probes.
        Wait-LocalTcpPort `
            -HostName '127.0.0.1' `
            -Port $DashboardPort `
            -Process $dashboardProcess `
            -Name 'Dashboard' `
            -ErrorLog $dashboardErr `
            -TimeoutSeconds 90
    }
    else {
        # Static production files need no runtime compilation or warm-up.
        Wait-LocalTcpPort `
            -HostName '127.0.0.1' `
            -Port $DashboardPort `
            -Process $dashboardProcess `
            -Name 'Dashboard' `
            -ErrorLog $dashboardErr `
            -TimeoutSeconds 30
    }

    if ($Mode -eq 'Demo' -and $VerifyDemo) {
        & $pythonCommand.Source scripts/verify_demo.py `
            --url "http://127.0.0.1:$ApiPort" `
            --timeout 45
        if ($LASTEXITCODE -ne 0) {
            throw 'The ten-account demo verification failed.'
        }
    }

    Write-Host ''
    Write-Host "$Mode control plane is ready."
    Write-Host "Dashboard: http://127.0.0.1:$DashboardPort"
    Write-Host "API:       http://127.0.0.1:$ApiPort"
    Write-Host "Logs:      $runRoot"
    Write-Host ("Ready in:  {0:N1}s" -f $startupWatch.Elapsed.TotalSeconds)

    if ($ExitAfterReady) {
        return
    }

    Write-Host 'Press Ctrl+C to stop both local processes.'
    while ($true) {
        if ($apiProcess.HasExited) {
            throw "Control-plane API exited unexpectedly.`n$(Get-LogTail -Path $apiErr)"
        }
        if ($dashboardProcess.HasExited) {
            throw "Dashboard exited unexpectedly.`n$(Get-LogTail -Path $dashboardErr)"
        }
        Start-Sleep -Seconds 1
    }
}
finally {
    Stop-LocalProcessTree -RootProcess $dashboardProcess
    Stop-LocalProcessTree -RootProcess $apiProcess
    foreach ($environmentName in $environmentNames) {
        [Environment]::SetEnvironmentVariable($environmentName, $savedEnvironment[$environmentName], 'Process')
    }
    foreach ($credentialRef in $savedCredentialEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($credentialRef, $savedCredentialEnvironment[$credentialRef], 'Process')
    }
    Pop-Location
}
