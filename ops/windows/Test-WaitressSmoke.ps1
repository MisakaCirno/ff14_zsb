[CmdletBinding()]
param(
    [string]$RepositoryRoot = '',
    [string]$PythonExecutable = '',
    [ValidateRange(5, 120)]
    [int]$StartupTimeoutSeconds = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Resolve-RequiredExecutable {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    $resolved = Resolve-Path -LiteralPath $Path -ErrorAction SilentlyContinue
    if ($null -ne $resolved -and (Test-Path -LiteralPath $resolved.Path -PathType Leaf)) {
        return $resolved.Path
    }

    $command = Get-Command `
        $Path `
        -CommandType Application `
        -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $command) {
        return $command.Source
    }
    throw "$Description was not found: $Path"
}

function Get-FreeLoopbackPort {
    $listener = New-Object System.Net.Sockets.TcpListener(
        [System.Net.IPAddress]::Loopback,
        0
    )
    try {
        $listener.Start()
        return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    }
    finally {
        $listener.Stop()
    }
}

function Get-ListeningTcpEndpoint {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port
    )

    $output = @(& netstat.exe -ano -p tcp)
    if ($LASTEXITCODE -ne 0) {
        throw "netstat failed with exit code $LASTEXITCODE."
    }

    $endpoints = @()
    foreach ($line in $output) {
        $fields = @($line.Trim() -split '\s+')
        if ($fields.Count -lt 5 -or $fields[0] -ne 'TCP') {
            continue
        }
        if ($fields[3] -ne 'LISTENING') {
            continue
        }

        $localEndpoint = $fields[1]
        $separator = $localEndpoint.LastIndexOf(':')
        if ($separator -lt 1) {
            continue
        }
        $address = $localEndpoint.Substring(0, $separator).Trim('[', ']')
        $parsedPort = 0
        $processId = 0
        if (
            -not [int]::TryParse(
                $localEndpoint.Substring($separator + 1),
                [ref]$parsedPort
            ) -or
            -not [int]::TryParse($fields[4], [ref]$processId) -or
            $parsedPort -ne $Port
        ) {
            continue
        }

        $endpoints += [pscustomobject]@{
            LocalAddress = $address
            LocalPort = $parsedPort
            OwningProcess = $processId
        }
    }
    return $endpoints
}

function Invoke-HealthRequest {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri,
        [hashtable]$Headers = @{}
    )

    return Invoke-WebRequest `
        -UseBasicParsing `
        -Uri $Uri `
        -Headers $Headers `
        -TimeoutSec 5
}

function Assert-RequestId {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Response,
        [string]$RejectedValue = ''
    )

    $requestId = [string]$Response.Headers['X-Request-ID']
    if ($requestId -notmatch '^[0-9a-f]{32}$') {
        throw "Response X-Request-ID is not a server-generated 32-character hex value."
    }
    if ($RejectedValue -and $requestId -eq $RejectedValue) {
        throw 'Response reused the untrusted client X-Request-ID.'
    }
    return $requestId
}

function Stop-TestProcess {
    param(
        [System.Diagnostics.Process]$Process,
        [int[]]$ServerProcessIds = @(),
        [int]$Port
    )

    $processIds = @($ServerProcessIds)
    if ($null -ne $Process) {
        $processIds += $Process.Id
    }
    foreach ($processId in @($processIds | Sort-Object -Unique)) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }

    if ($null -ne $Process) {
        try {
            $Process.Refresh()
            if (-not $Process.HasExited) {
                Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
                [void]$Process.WaitForExit(5000)
            }
        }
        catch {
            Write-Warning "Could not stop Waitress process $($Process.Id): $($_.Exception.Message)"
        }
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    do {
        $listeners = @(Get-ListeningTcpEndpoint -Port $Port)
        if ($listeners.Count -eq 0) {
            return
        }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)

    throw "Waitress listener on port $Port did not terminate."
}

function Remove-TestDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    do {
        try {
            if (Test-Path -LiteralPath $Path) {
                Remove-Item -LiteralPath $Path -Recurse -Force
            }
            return
        }
        catch [System.IO.IOException] {
            Start-Sleep -Milliseconds 100
        }
    } while ([DateTime]::UtcNow -lt $deadline)

    throw "Temporary test directory remained locked: $Path"
}

if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    $RepositoryRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
$RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path

if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $venvPython = Join-Path $RepositoryRoot 'venv\Scripts\python.exe'
    $PythonExecutable = if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $venvPython
    }
    else {
        'python.exe'
    }
}
$PythonExecutable = Resolve-RequiredExecutable `
    -Path $PythonExecutable `
    -Description 'Virtual environment Python executable'

$temporaryBase = [System.IO.Path]::GetTempPath()
$temporaryRoot = Join-Path `
    $temporaryBase `
    ('ffxivshare-waitress-smoke-' + [Guid]::NewGuid().ToString('N'))
[void](New-Item -ItemType Directory -Path $temporaryRoot)

$databasePath = Join-Path $temporaryRoot 'smoke.sqlite3'
$environmentPath = Join-Path $temporaryRoot 'smoke.env'
$stdoutPath = Join-Path $temporaryRoot 'waitress.stdout.log'
$stderrPath = Join-Path $temporaryRoot 'waitress.stderr.log'
$port = Get-FreeLoopbackPort
$process = $null
$serverProcessIds = @()

$testEnvironment = [ordered]@{
    FFXIVSHARE_ENV_FILE = $environmentPath
    APP_ENV = 'test'
    SECRET_KEY = 'waitress-smoke-only-secret-key-with-enough-length-123456789'
    DEBUG = 'False'
    ALLOWED_HOSTS = '127.0.0.1,localhost,testserver'
    CSRF_TRUSTED_ORIGINS = 'http://127.0.0.1'
    SECURE_SSL_REDIRECT = 'False'
    SESSION_COOKIE_SECURE = 'False'
    CSRF_COOKIE_SECURE = 'False'
    SECURE_HSTS_SECONDS = '0'
    SECURE_HSTS_INCLUDE_SUBDOMAINS = 'False'
    SECURE_HSTS_PRELOAD = 'False'
    TRUST_X_FORWARDED_FOR = 'True'
    RATE_LIMIT_ENABLED = 'False'
    REQUEST_LOG_ENABLED = 'True'
    DATABASE_ENGINE = 'sqlite'
    DATABASE_PATH = $databasePath
    SQLITE_TIMEOUT = '5'
    SQLITE_TRANSACTION_MODE = 'IMMEDIATE'
    SQLITE_JOURNAL_MODE = 'WAL'
    SQLITE_SYNCHRONOUS = 'FULL'
    PYTHONUNBUFFERED = '1'
    PYTHONDONTWRITEBYTECODE = '1'
}

$originalEnvironment = @{}
foreach ($name in $testEnvironment.Keys) {
    $originalEnvironment[$name] = [Environment]::GetEnvironmentVariable(
        $name,
        [EnvironmentVariableTarget]::Process
    )
}

try {
    $environmentLines = foreach ($entry in $testEnvironment.GetEnumerator()) {
        '{0}={1}' -f $entry.Key, $entry.Value
    }
    [System.IO.File]::WriteAllLines(
        $environmentPath,
        [string[]]$environmentLines,
        (New-Object System.Text.UTF8Encoding($false))
    )

    foreach ($entry in $testEnvironment.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable(
            $entry.Key,
            [string]$entry.Value,
            [EnvironmentVariableTarget]::Process
        )
    }

    Push-Location $RepositoryRoot
    try {
        & $PythonExecutable manage.py migrate --noinput --verbosity 0
        if ($LASTEXITCODE -ne 0) {
            throw "Temporary database migration failed with exit code $LASTEXITCODE."
        }

        $waitressArguments = @(
            '-m'
            'waitress'
            "--listen=127.0.0.1:$port"
            '--threads=4'
            '--trusted-proxy=127.0.0.1'
            '--trusted-proxy-headers="x-forwarded-for x-forwarded-proto"'
            '--clear-untrusted-proxy-headers'
            '--no-expose-tracebacks'
            'ffxivshare.wsgi:application'
        )
        $process = Start-Process `
            -FilePath $PythonExecutable `
            -ArgumentList $waitressArguments `
            -WorkingDirectory $RepositoryRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -PassThru
    }
    finally {
        Pop-Location
    }

    $liveUri = "http://127.0.0.1:$port/health/live/"
    $readyUri = "http://127.0.0.1:$port/health/ready/"
    $deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    $liveResponse = $null
    do {
        $process.Refresh()
        if ($process.HasExited) {
            throw "Waitress exited before becoming live with code $($process.ExitCode)."
        }
        $observedListeners = @(Get-ListeningTcpEndpoint -Port $port)
        $serverProcessIds += @($observedListeners | Where-Object {
            $_.LocalAddress -eq '127.0.0.1'
        } | Select-Object -ExpandProperty OwningProcess)
        $serverProcessIds = @($serverProcessIds | Sort-Object -Unique)
        try {
            $liveResponse = Invoke-HealthRequest -Uri $liveUri
        }
        catch {
            $liveResponse = $null
            Start-Sleep -Milliseconds 100
        }
    } while ($null -eq $liveResponse -and [DateTime]::UtcNow -lt $deadline)

    if ($null -eq $liveResponse -or $liveResponse.StatusCode -ne 200) {
        throw 'Waitress did not pass the liveness probe before the timeout.'
    }

    $listeners = @(Get-ListeningTcpEndpoint -Port $port)
    if ($listeners.Count -eq 0) {
        throw "No listener was found on the Waitress port $port."
    }
    $unsafeListeners = @($listeners | Where-Object {
        $_.LocalAddress -ne '127.0.0.1'
    })
    if ($unsafeListeners.Count -ne 0) {
        $addresses = ($unsafeListeners.LocalAddress | Sort-Object -Unique) -join ', '
        throw "Waitress exposed a non-loopback listener: $addresses"
    }
    $serverProcessIds += @($listeners | Select-Object -ExpandProperty OwningProcess)
    $serverProcessIds = @($serverProcessIds | Sort-Object -Unique)

    [void](Assert-RequestId -Response $liveResponse)

    $readyResponse = Invoke-HealthRequest -Uri $readyUri
    if ($readyResponse.StatusCode -ne 200) {
        throw "Readiness probe returned HTTP $($readyResponse.StatusCode)."
    }
    $readyPayload = $readyResponse.Content | ConvertFrom-Json
    if ($readyPayload.status -ne 'ok') {
        throw 'Readiness probe did not return the expected status payload.'
    }
    [void](Assert-RequestId -Response $readyResponse)

    $forgedRequestId = 'ffffffffffffffffffffffffffffffff'
    $forgedResponse = Invoke-HealthRequest `
        -Uri $liveUri `
        -Headers @{ 'X-Request-ID' = $forgedRequestId }
    [void](Assert-RequestId `
        -Response $forgedResponse `
        -RejectedValue $forgedRequestId)

    Write-Host "Waitress smoke test passed on 127.0.0.1:$port."
}
catch {
    if (Test-Path -LiteralPath $stderrPath) {
        $stderrTail = @(Get-Content -LiteralPath $stderrPath -Tail 40)
        if ($stderrTail.Count -gt 0) {
            Write-Warning ('Waitress stderr tail:' + [Environment]::NewLine + ($stderrTail -join [Environment]::NewLine))
        }
    }
    throw
}
finally {
    try {
        Stop-TestProcess `
            -Process $process `
            -ServerProcessIds $serverProcessIds `
            -Port $port
    }
    finally {
        foreach ($name in $testEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable(
                $name,
                $originalEnvironment[$name],
                [EnvironmentVariableTarget]::Process
            )
        }

        $resolvedTemporaryBase = [System.IO.Path]::GetFullPath($temporaryBase)
        $resolvedTemporaryRoot = [System.IO.Path]::GetFullPath($temporaryRoot)
        if (-not $resolvedTemporaryRoot.StartsWith(
            $resolvedTemporaryBase,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Refusing to remove an unexpected path: $resolvedTemporaryRoot"
        }
        Remove-TestDirectory -Path $resolvedTemporaryRoot
    }
}
