[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepositoryRoot,
    [string]$UpgradeParent = '',
    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-AbsoluteLocalPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    if ([string]::IsNullOrWhiteSpace($Path) -or
        $Path.StartsWith('\\') -or
        $Path -notmatch '^[A-Za-z]:[\\/]') {
        throw "$Description must be an absolute local Windows path."
    }
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
}

function Test-Listener {
    param([Parameter(Mandatory = $true)][int]$Port)

    try {
        return $null -ne (Get-NetTCPConnection `
            -State Listen `
            -LocalPort $Port `
            -ErrorAction Stop |
            Select-Object -First 1)
    }
    catch {
        $pattern = ":$Port\s+.*LISTENING"
        return $null -ne (netstat.exe -ano -p tcp |
            Select-String -Pattern $pattern |
            Select-Object -First 1)
    }
}

function Get-SchemaStatus {
    param(
        [Parameter(Mandatory = $true)][string]$PythonExecutable,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $lines = @()
    $exitCode = $null
    Push-Location $WorkingDirectory
    try {
        # Django can write non-fatal system-check warnings to stderr. Capture
        # both streams and use the native process exit code as the authority.
        $ErrorActionPreference = 'Continue'
        $lines = @(& $PythonExecutable `
            -B manage.py check_deployment_schema 2>&1 |
            ForEach-Object { $_.ToString() })
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        Pop-Location
    }
    $jsonLine = $lines |
        Where-Object { $_.Trim().StartsWith('{') -and $_.Trim().EndsWith('}') } |
        Select-Object -Last 1
    if ([string]::IsNullOrWhiteSpace($jsonLine)) {
        throw "Schema status did not return JSON: $($lines -join [Environment]::NewLine)"
    }
    $report = $jsonLine | ConvertFrom-Json
    if ($exitCode -ne 0 -and $report.status -ne 'invalid_history') {
        throw "Schema status failed: $($lines -join [Environment]::NewLine)"
    }
    return $report
}

function Invoke-Waitress {
    param(
        [Parameter(Mandatory = $true)][string]$PythonExecutable,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $exitCode = $null
    $env:PYTHONUNBUFFERED = '1'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYTHONUTF8 = '1'
    Push-Location $WorkingDirectory
    try {
        Write-Host 'Starting FFXIVShare on http://127.0.0.1:8000/'
        # Waitress writes normal access and lifecycle logs to stderr. Keep them
        # visible without letting PowerShell 5 reinterpret them as failures.
        $ErrorActionPreference = 'Continue'
        & $PythonExecutable `
            -B `
            -m waitress `
            --listen=127.0.0.1:8000 `
            --threads=4 `
            --trusted-proxy=127.0.0.1 `
            '--trusted-proxy-headers=x-forwarded-for x-forwarded-proto' `
            --clear-untrusted-proxy-headers `
            --no-expose-tracebacks `
            ffxivshare.wsgi:application 2>&1 |
            ForEach-Object { Write-Host $_.ToString() }
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        Pop-Location
    }
    return [int]$exitCode
}

if ($env:OS -ne 'Windows_NT') {
    throw 'The direct Git launcher supports Windows only.'
}
$RepositoryRoot = Get-AbsoluteLocalPath `
    -Path $RepositoryRoot `
    -Description 'RepositoryRoot'
$pythonExecutable = Join-Path $RepositoryRoot 'venv\Scripts\python.exe'
$upgradeScript = Join-Path $RepositoryRoot 'ops\release\Invoke-DirectGitDatabaseUpgrade.ps1'
foreach ($path in @($pythonExecutable, $upgradeScript, (Join-Path $RepositoryRoot 'manage.py'))) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required launcher file is missing: $path"
    }
}
if ([string]::IsNullOrWhiteSpace($UpgradeParent)) {
    $driveRoot = [System.IO.Path]::GetPathRoot($RepositoryRoot)
    $UpgradeParent = Join-Path $driveRoot 'FFXIVShare-R20\Upgrades'
}
$UpgradeParent = Get-AbsoluteLocalPath `
    -Path $UpgradeParent `
    -Description 'UpgradeParent'

if (Test-Listener -Port 8000) {
    throw 'Port 8000 is already listening. Refusing to start a duplicate writer.'
}

$schema = Get-SchemaStatus `
    -PythonExecutable $pythonExecutable `
    -WorkingDirectory $RepositoryRoot
if ($schema.status -eq 'invalid_history') {
    throw 'Migration history is invalid. Keep the application stopped and investigate.'
}

if ($schema.upgrade_required) {
    Write-Host ''
    $pendingMigrations = @($schema.pending_migrations)
    Write-Host "Database upgrade required: $($pendingMigrations.Count) migration(s)."
    foreach ($migration in $pendingMigrations) {
        Write-Host ('  - {0}/{1}' -f $migration[0], $migration[1])
    }
    Write-Host ''
    Write-Host '[1] Create a verified backup, upgrade safely, and start'
    Write-Host '[2] Do not upgrade; keep the application stopped (default)'

    if ($NonInteractive) {
        Write-Host 'Non-interactive launch will not approve a database upgrade.'
        exit 10
    }
    $choice = (Read-Host 'Choose 1 or 2').Trim()
    if ($choice -notin @('1', 'U', 'u', 'UPGRADE', 'upgrade')) {
        Write-Host 'Upgrade declined. No database changes were made.'
        exit 0
    }

    & $upgradeScript `
        -RepositoryRoot $RepositoryRoot `
        -DatabasePath ([string]$schema.database_path) `
        -UpgradeParent $UpgradeParent `
        -Confirm:$false

    $schema = Get-SchemaStatus `
        -PythonExecutable $pythonExecutable `
        -WorkingDirectory $RepositoryRoot
    if (-not $schema.safe_to_start) {
        throw 'The upgraded database did not pass the final schema check.'
    }
}

if (-not $schema.safe_to_start) {
    throw 'The database is not safe to start.'
}
exit (Invoke-Waitress `
    -PythonExecutable $pythonExecutable `
    -WorkingDirectory $RepositoryRoot)
