[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepositoryRoot,
    [Parameter(Mandatory = $true)]
    [string]$DatabasePath,
    [Parameter(Mandatory = $true)]
    [string]$UpgradeParent
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-AbsoluteLocalPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Description
    )

    if ([string]::IsNullOrWhiteSpace($Path) -or
        $Path.StartsWith('\\') -or
        $Path -notmatch '^[A-Za-z]:[\\/]') {
        throw "$Description must be an absolute local Windows path."
    }
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
}

function Test-PathInside {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Parent
    )

    $comparison = [System.StringComparison]::OrdinalIgnoreCase
    if ($Candidate.Equals($Parent, $comparison)) {
        return $true
    }
    return $Candidate.StartsWith(
        $Parent.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar,
        $comparison
    )
}

function Assert-NoSqliteSidecars {
    param([Parameter(Mandatory = $true)][string]$Path)

    $sidecars = @(@('-wal', '-shm', '-journal') |
        ForEach-Object { "$Path$_" } |
        Where-Object { Test-Path -LiteralPath $_ })
    if ($sidecars.Count -ne 0) {
        throw "SQLite sidecars are present: $($sidecars -join ', ')"
    }
}

function Assert-PortStopped {
    try {
        $listener = Get-NetTCPConnection `
            -State Listen `
            -LocalPort 8000 `
            -ErrorAction Stop |
            Select-Object -First 1
    }
    catch {
        $listener = netstat.exe -ano -p tcp |
            Select-String -Pattern ':8000\s+.*LISTENING' |
            Select-Object -First 1
    }
    if ($null -ne $listener) {
        throw 'Port 8000 is still listening. Stop Waitress before upgrading.'
    }
}

function Set-PrivateDirectoryAcl {
    param([Parameter(Mandatory = $true)][string]$Path)

    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $inheritance = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $propagation = [System.Security.AccessControl.PropagationFlags]::None
    $allow = [System.Security.AccessControl.AccessControlType]::Allow
    $full = [System.Security.AccessControl.FileSystemRights]::FullControl
    $identities = @(
        [System.Security.Principal.WindowsIdentity]::GetCurrent().User,
        (New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')),
        (New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544'))
    )
    foreach ($identity in $identities) {
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $identity,
            $full,
            $inheritance,
            $propagation,
            $allow
        )
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Invoke-LoggedPython {
    param(
        [Parameter(Mandatory = $true)][string]$PythonExecutable,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$LogPath,
        [Parameter(Mandatory = $true)][string]$Description
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $lines = @()
    $exitCode = $null
    Push-Location $WorkingDirectory
    try {
        # PowerShell 5 turns a native program's stderr into ErrorRecord objects.
        # Django writes successful system-check warnings to stderr, so decide
        # success from the process exit code after capturing both streams.
        $ErrorActionPreference = 'Continue'
        $lines = @(& $PythonExecutable @ArgumentList 2>&1 |
            ForEach-Object { $_.ToString() })
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        Pop-Location
    }
    [System.IO.File]::WriteAllLines(
        $LogPath,
        $lines,
        (New-Object System.Text.UTF8Encoding($false))
    )
    if ($exitCode -ne 0) {
        throw "$Description failed with exit code $exitCode. See $LogPath"
    }
    return $lines
}

function Get-GitHead {
    param([Parameter(Mandatory = $true)][string]$Root)

    $head = (& git.exe -C $Root rev-parse HEAD 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $head -notmatch '^[a-f0-9]{40}$') {
        throw 'Could not resolve one immutable Git HEAD.'
    }
    return $head
}

if ($env:OS -ne 'Windows_NT') {
    throw 'The direct Git database upgrade supports Windows only.'
}
$RepositoryRoot = Get-AbsoluteLocalPath -Path $RepositoryRoot -Description 'RepositoryRoot'
$DatabasePath = Get-AbsoluteLocalPath -Path $DatabasePath -Description 'DatabasePath'
$UpgradeParent = Get-AbsoluteLocalPath -Path $UpgradeParent -Description 'UpgradeParent'
if (Test-PathInside -Candidate $UpgradeParent -Parent $RepositoryRoot) {
    throw 'UpgradeParent must be outside RepositoryRoot.'
}
if (-not [System.IO.Path]::GetPathRoot($DatabasePath).Equals(
    [System.IO.Path]::GetPathRoot($UpgradeParent),
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw 'UpgradeParent and DatabasePath must be on the same local volume.'
}

$pythonExecutable = Join-Path $RepositoryRoot 'venv\Scripts\python.exe'
foreach ($path in @($pythonExecutable, $DatabasePath, (Join-Path $RepositoryRoot 'manage.py'))) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required upgrade file is missing: $path"
    }
}
$databaseItem = Get-Item -LiteralPath $DatabasePath -Force
if (($databaseItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'DatabasePath must not be a reparse point.'
}
Assert-PortStopped
Assert-NoSqliteSidecars -Path $DatabasePath

$head = Get-GitHead -Root $RepositoryRoot
$targetDescription = "SQLite database $DatabasePath for Git $head"
if (-not $PSCmdlet.ShouldProcess($targetDescription, 'Create backup, migrate a candidate, verify, and atomically replace')) {
    return
}

if (-not (Test-Path -LiteralPath $UpgradeParent -PathType Container)) {
    [void][System.IO.Directory]::CreateDirectory($UpgradeParent)
}
$runId = '{0}-{1}-{2}' -f (
    [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
), $head.Substring(0, 12), [Guid]::NewGuid().ToString('N').Substring(0, 8)
$runRoot = Join-Path $UpgradeParent $runId
[void][System.IO.Directory]::CreateDirectory($runRoot)
Set-PrivateDirectoryAcl -Path $runRoot

$logsRoot = Join-Path $runRoot 'logs'
$evidenceRoot = Join-Path $runRoot 'evidence'
[void][System.IO.Directory]::CreateDirectory($logsRoot)
[void][System.IO.Directory]::CreateDirectory($evidenceRoot)
$sourceRollback = Join-Path $runRoot 'source-rollback.sqlite3'
$candidate = Join-Path $runRoot 'candidate.sqlite3'
$verifiedCandidate = Join-Path $runRoot 'candidate-verified.sqlite3'
$atomicBackup = Join-Path $runRoot 'source-atomic-backup.sqlite3'
$failedReplacement = Join-Path $runRoot 'failed-replacement.sqlite3'
$restrictionReport = Join-Path $evidenceRoot 'restriction-preflight.json'
$semanticReport = Join-Path $evidenceRoot 'database-semantic-comparison.json'
$resultPath = Join-Path $evidenceRoot 'upgrade-result.json'

$sourceItem = Get-Item -LiteralPath $DatabasePath -Force
$sourceSnapshot = [pscustomobject]@{
    length = [long]$sourceItem.Length
    last_write_utc = $sourceItem.LastWriteTimeUtc.ToString('o')
    sha256 = (Get-FileHash -LiteralPath $DatabasePath -Algorithm SHA256).Hash.ToLowerInvariant()
}
[System.IO.File]::Copy($DatabasePath, $sourceRollback, $false)
[System.IO.File]::Copy($DatabasePath, $candidate, $false)
foreach ($copyPath in @($sourceRollback, $candidate)) {
    $copyHash = (Get-FileHash -LiteralPath $copyPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($copyHash -ne $sourceSnapshot.sha256) {
        throw "Initial database copy hash mismatch: $copyPath"
    }
}

$hadDatabasePath = Test-Path Env:DATABASE_PATH
$oldDatabasePath = $env:DATABASE_PATH
$hadAppVersion = Test-Path Env:APP_VERSION
$oldAppVersion = $env:APP_VERSION
$switched = $false
try {
    $env:DATABASE_PATH = $candidate
    $env:APP_VERSION = $head

    Invoke-LoggedPython `
        -PythonExecutable $pythonExecutable `
        -WorkingDirectory $RepositoryRoot `
        -ArgumentList @('-B', 'manage.py', 'migrate', '--noinput') `
        -LogPath (Join-Path $logsRoot 'migrate.log') `
        -Description 'Candidate migration' | Out-Null
    Invoke-LoggedPython `
        -PythonExecutable $pythonExecutable `
        -WorkingDirectory $RepositoryRoot `
        -ArgumentList @('-B', 'manage.py', 'check_deployment_schema', '--require-current') `
        -LogPath (Join-Path $logsRoot 'schema-check.log') `
        -Description 'Candidate schema check' | Out-Null
    Invoke-LoggedPython `
        -PythonExecutable $pythonExecutable `
        -WorkingDirectory $RepositoryRoot `
        -ArgumentList @(
            '-B', 'manage.py', 'preflight_share_restrictions',
            '--strict', '--output', $restrictionReport
        ) `
        -LogPath (Join-Path $logsRoot 'restriction-preflight.log') `
        -Description 'Candidate restriction preflight' | Out-Null
    Invoke-LoggedPython `
        -PythonExecutable $pythonExecutable `
        -WorkingDirectory $RepositoryRoot `
        -ArgumentList @(
            '-B', 'manage.py', 'verify_database_upgrade_semantics',
            $sourceRollback, $candidate, '--output', $semanticReport
        ) `
        -LogPath (Join-Path $logsRoot 'database-semantic-comparison.log') `
        -Description 'Candidate semantic data comparison' | Out-Null
    Invoke-LoggedPython `
        -PythonExecutable $pythonExecutable `
        -WorkingDirectory $RepositoryRoot `
        -ArgumentList @('-B', 'manage.py', 'check', '--deploy') `
        -LogPath (Join-Path $logsRoot 'deploy-check.log') `
        -Description 'Candidate deployment check' | Out-Null
    Invoke-LoggedPython `
        -PythonExecutable $pythonExecutable `
        -WorkingDirectory $RepositoryRoot `
        -ArgumentList @('-B', 'manage.py', 'backup_database', $verifiedCandidate) `
        -LogPath (Join-Path $logsRoot 'verified-backup.log') `
        -Description 'Verified candidate backup' | Out-Null

    Assert-NoSqliteSidecars -Path $candidate
    Assert-PortStopped
    Assert-NoSqliteSidecars -Path $DatabasePath
    $sourceNow = Get-Item -LiteralPath $DatabasePath -Force
    $sourceNowHash = (Get-FileHash -LiteralPath $DatabasePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($sourceNow.Length -ne $sourceSnapshot.length -or
        $sourceNow.LastWriteTimeUtc.ToString('o') -ne $sourceSnapshot.last_write_utc -or
        $sourceNowHash -ne $sourceSnapshot.sha256) {
        throw 'The source database changed while the candidate was being prepared.'
    }

    $verifiedHash = (Get-FileHash -LiteralPath $verifiedCandidate -Algorithm SHA256).Hash.ToLowerInvariant()
    [System.IO.File]::Replace(
        $verifiedCandidate,
        $DatabasePath,
        $atomicBackup,
        $true
    )
    $switched = $true
    $liveHash = (Get-FileHash -LiteralPath $DatabasePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $atomicHash = (Get-FileHash -LiteralPath $atomicBackup -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($liveHash -ne $verifiedHash -or $atomicHash -ne $sourceSnapshot.sha256) {
        throw 'Atomic database replacement verification failed.'
    }

    $env:DATABASE_PATH = $DatabasePath
    Invoke-LoggedPython `
        -PythonExecutable $pythonExecutable `
        -WorkingDirectory $RepositoryRoot `
        -ArgumentList @('-B', 'manage.py', 'check_deployment_schema', '--require-current') `
        -LogPath (Join-Path $logsRoot 'live-schema-check.log') `
        -Description 'Final live schema check' | Out-Null

    $result = [ordered]@{
        format = 'ffxivshare-direct-git-database-upgrade'
        format_version = 1
        status = 'completed'
        completed_at = [DateTime]::UtcNow.ToString('o')
        git_head = $head
        database_path = $DatabasePath
        source_sha256 = $sourceSnapshot.sha256
        upgraded_sha256 = $liveHash
        source_rollback = $sourceRollback
        atomic_backup = $atomicBackup
        restriction_report = $restrictionReport
        semantic_comparison_report = $semanticReport
        database_upgrade_completed = $true
        database_switch_completed = $true
        safe_to_start = $true
    }
    [System.IO.File]::WriteAllText(
        $resultPath,
        (($result | ConvertTo-Json -Depth 6) + [Environment]::NewLine),
        (New-Object System.Text.UTF8Encoding($false))
    )
    Write-Output ($result | ConvertTo-Json -Compress)
}
catch {
    if ($switched -and (Test-Path -LiteralPath $atomicBackup -PathType Leaf)) {
        try {
            [System.IO.File]::Replace(
                $atomicBackup,
                $DatabasePath,
                $failedReplacement,
                $true
            )
            $restoredHash = (Get-FileHash -LiteralPath $DatabasePath -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($restoredHash -ne $sourceSnapshot.sha256) {
                throw 'Automatic rollback hash verification failed.'
            }
            $switched = $false
        }
        catch {
            Write-Error 'CRITICAL: Database switch failed and automatic rollback could not be verified.'
        }
    }
    throw
}
finally {
    if ($hadDatabasePath) { $env:DATABASE_PATH = $oldDatabasePath } else { Remove-Item Env:DATABASE_PATH -ErrorAction SilentlyContinue }
    if ($hadAppVersion) { $env:APP_VERSION = $oldAppVersion } else { Remove-Item Env:APP_VERSION -ErrorAction SilentlyContinue }
}
