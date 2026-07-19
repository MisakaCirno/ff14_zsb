[CmdletBinding()]
param(
    [string]$RepositoryRoot = '',
    [string]$PythonExecutable = '',
    [string]$NpmExecutable = '',
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
    $command = Get-Command $Path -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
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

function Get-ListenerProcessIds {
    param([Parameter(Mandatory = $true)][int]$Port)

    return @(
        Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    )
}

function Remove-TestDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)

    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    $tempPath = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    if (-not $resolvedPath.StartsWith($tempPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a browser-test directory outside the system temp root: $resolvedPath"
    }
    if (Test-Path -LiteralPath $resolvedPath) {
        Remove-Item -LiteralPath $resolvedPath -Recurse -Force
    }
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
if ([string]::IsNullOrWhiteSpace($NpmExecutable)) {
    $NpmExecutable = 'npm.cmd'
}
$PythonExecutable = Resolve-RequiredExecutable $PythonExecutable 'Python executable'
$NpmExecutable = Resolve-RequiredExecutable $NpmExecutable 'npm executable'

$temporaryRoot = Join-Path (
    [System.IO.Path]::GetTempPath()
) ('ffxivshare-browser-' + [Guid]::NewGuid().ToString('N'))
[void](New-Item -ItemType Directory -Path $temporaryRoot)
[void](New-Item -ItemType Directory -Path (Join-Path $temporaryRoot 'media'))

$port = Get-FreeLoopbackPort
$databasePath = Join-Path $temporaryRoot 'browser.sqlite3'
$environmentPath = Join-Path $temporaryRoot 'browser.env'
$stdoutPath = Join-Path $temporaryRoot 'django.stdout.log'
$stderrPath = Join-Path $temporaryRoot 'django.stderr.log'
$serverProcess = $null
$succeeded = $false

$testEnvironment = [ordered]@{
    FFXIVSHARE_ENV_FILE = $environmentPath
    FFXIVSHARE_E2E_ROOT = $temporaryRoot
    APP_ENV = 'test'
    SECRET_KEY = 'browser-test-only-secret-key-with-enough-length-123456789'
    DEBUG = 'True'
    ALLOWED_HOSTS = '127.0.0.1,localhost,testserver'
    CSRF_TRUSTED_ORIGINS = "http://127.0.0.1:$port"
    SECURE_SSL_REDIRECT = 'False'
    SESSION_COOKIE_SECURE = 'False'
    CSRF_COOKIE_SECURE = 'False'
    SECURE_HSTS_SECONDS = '0'
    TRUST_X_FORWARDED_FOR = 'False'
    RATE_LIMIT_ENABLED = 'False'
    CSP_REPORT_ONLY = 'True'
    REQUEST_LOG_ENABLED = 'False'
    DATABASE_ENGINE = 'sqlite'
    DATABASE_PATH = $databasePath
    MEDIA_ROOT = (Join-Path $temporaryRoot 'media')
    SQLITE_TIMEOUT = '5'
    SQLITE_TRANSACTION_MODE = 'IMMEDIATE'
    SQLITE_JOURNAL_MODE = 'WAL'
    SQLITE_SYNCHRONOUS = 'FULL'
    PLAYWRIGHT_BASE_URL = "http://127.0.0.1:$port"
    PLAYWRIGHT_OUTPUT_DIR = (Join-Path $temporaryRoot 'playwright-output')
    PYTHONDONTWRITEBYTECODE = '1'
    PYTHONUNBUFFERED = '1'
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
            throw "Browser-test database migration failed with exit code $LASTEXITCODE."
        }
        & $PythonExecutable (Join-Path $RepositoryRoot 'ops\testing\seed_browser_database.py')
        if ($LASTEXITCODE -ne 0) {
            throw "Browser-test database seeding failed with exit code $LASTEXITCODE."
        }

        $serverProcess = Start-Process `
            -FilePath $PythonExecutable `
            -ArgumentList @(
                '-B',
                'manage.py',
                'runserver',
                "127.0.0.1:$port",
                '--noreload'
            ) `
            -WorkingDirectory $RepositoryRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -PassThru

        $healthUri = "http://127.0.0.1:$port/health/ready/"
        $deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
        $healthResponse = $null
        do {
            $serverProcess.Refresh()
            if ($serverProcess.HasExited) {
                throw "Django browser-test server exited with code $($serverProcess.ExitCode)."
            }
            try {
                $healthResponse = Invoke-WebRequest `
                    -UseBasicParsing `
                    -Uri $healthUri `
                    -TimeoutSec 3
            }
            catch {
                $healthResponse = $null
                Start-Sleep -Milliseconds 100
            }
        } while ($null -eq $healthResponse -and [DateTime]::UtcNow -lt $deadline)

        if ($null -eq $healthResponse -or $healthResponse.StatusCode -ne 200) {
            throw 'Django browser-test server did not become ready before the timeout.'
        }

        & $NpmExecutable --prefix frontend run test:e2e
        if ($LASTEXITCODE -ne 0) {
            throw "Browser flow tests failed with exit code $LASTEXITCODE."
        }
        $succeeded = $true
    }
    finally {
        Pop-Location
    }
}
finally {
    foreach ($processId in @(Get-ListenerProcessIds -Port $port)) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
    if ($null -ne $serverProcess) {
        Stop-Process -Id $serverProcess.Id -Force -ErrorAction SilentlyContinue
        try {
            [void]$serverProcess.WaitForExit(5000)
        }
        catch {
            Write-Warning "Could not wait for browser-test server process: $($_.Exception.Message)"
        }
    }

    foreach ($entry in $originalEnvironment.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable(
            $entry.Key,
            $entry.Value,
            [EnvironmentVariableTarget]::Process
        )
    }

    if (-not $succeeded) {
        foreach ($logPath in @($stdoutPath, $stderrPath)) {
            if (Test-Path -LiteralPath $logPath -PathType Leaf) {
                Write-Host "==> $([System.IO.Path]::GetFileName($logPath))"
                Get-Content -LiteralPath $logPath
            }
        }
    }
    Remove-TestDirectory -Path $temporaryRoot
}

Write-Host 'Browser flow and accessibility tests passed.'
