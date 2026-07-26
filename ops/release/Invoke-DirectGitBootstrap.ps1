[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepositoryRoot,
    [string]$StatePath = '',
    [string]$UpgradeParent = '',
    [switch]$SkipRemoteUpdate,
    [switch]$NonInteractive
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

function Get-GitHead {
    param([Parameter(Mandatory = $true)][string]$Root)

    $previousErrorActionPreference = $ErrorActionPreference
    $lines = @()
    $exitCode = $null
    try {
        $ErrorActionPreference = 'Continue'
        $lines = @(& git.exe -C $Root rev-parse HEAD 2>&1 |
            ForEach-Object { $_.ToString() })
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $head = ($lines -join [Environment]::NewLine).Trim()
    if ($exitCode -ne 0 -or $head -notmatch '^[a-f0-9]{40}$') {
        throw 'Could not resolve one immutable Git HEAD.'
    }
    return $head
}

function Invoke-PowerShellScript {
    param(
        [Parameter(Mandatory = $true)][string]$PowerShellExecutable,
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [Parameter(Mandatory = $true)][string[]]$ScriptArguments,
        [Parameter(Mandatory = $true)][ref]$ExitCodeReference
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $processExitCode = $null
    try {
        $ErrorActionPreference = 'Continue'
        & $PowerShellExecutable `
            -NoProfile `
            -ExecutionPolicy Bypass `
            -File $ScriptPath `
            @ScriptArguments
        $processExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    # Do not return the exit code through PowerShell's success stream. Native
    # child output uses that same stream, so assigning the function result would
    # combine normal output and the integer into an Object[] value.
    $ExitCodeReference.Value = [int]$processExitCode
}

function Stop-Bootstrap {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [Parameter(Mandatory = $true)][int]$ExitCode,
        [switch]$NoPause
    )

    Write-LauncherError -Message $Message
    if (-not $NoPause) {
        [void](Read-LauncherChoice -Prompt 'Press Enter to close: ')
    }
    exit $ExitCode
}

if ($env:OS -ne 'Windows_NT') {
    throw 'The direct Git bootstrap supports Windows only.'
}

[void](& chcp.com 65001)
$env:NO_COLOR = '1'
$RepositoryRoot = Get-AbsoluteLocalPath `
    -Path $RepositoryRoot `
    -Description 'RepositoryRoot'

$consoleScript = Join-Path $RepositoryRoot 'ops\release\LauncherConsole.ps1'
if (-not (Test-Path -LiteralPath $consoleScript -PathType Leaf)) {
    throw "Launcher console helpers are missing: $consoleScript"
}
. $consoleScript

$powerShellExecutable = Join-Path $PSHOME 'powershell.exe'
$preparerPath = Join-Path $RepositoryRoot (
    'ops\release\Invoke-DirectGitUpdateAndPrepare.ps1'
)
foreach ($path in @(
    $powerShellExecutable,
    $preparerPath,
    (Join-Path $RepositoryRoot 'manage.py')
)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        Stop-Bootstrap `
            -Message "Required bootstrap file is missing: $path" `
            -ExitCode 1 `
            -NoPause:$NonInteractive
    }
}

if ([string]::IsNullOrWhiteSpace($StatePath)) {
    $driveRoot = [System.IO.Path]::GetPathRoot($RepositoryRoot)
    $StatePath = Join-Path $driveRoot (
        'FFXIVShare-R20\State\prepared-commit.txt'
    )
}
$StatePath = Get-AbsoluteLocalPath -Path $StatePath -Description 'StatePath'

$initialHead = Get-GitHead -Root $RepositoryRoot
$prepareArguments = @(
    '-RepositoryRoot',
    $RepositoryRoot,
    '-StatePath',
    $StatePath
)
if ($SkipRemoteUpdate) {
    $prepareArguments += '-SkipRemoteUpdate'
}
if ($NonInteractive) {
    $prepareArguments += '-NonInteractive'
}

$prepareExitCode = 0
Invoke-PowerShellScript `
    -PowerShellExecutable $powerShellExecutable `
    -ScriptPath $preparerPath `
    -ScriptArguments $prepareArguments `
    -ExitCodeReference ([ref]$prepareExitCode)
if ($prepareExitCode -ne 0) {
    Stop-Bootstrap `
        -Message (
            'FFXIVShare update or preparation exited with code ' +
            "$prepareExitCode."
        ) `
        -ExitCode $prepareExitCode `
        -NoPause:$NonInteractive
}

$preparedHead = Get-GitHead -Root $RepositoryRoot
if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
    Stop-Bootstrap `
        -Message "Prepared state is missing: $StatePath" `
        -ExitCode 1 `
        -NoPause:$NonInteractive
}
$stateHead = [System.IO.File]::ReadAllText($StatePath).Trim().ToLowerInvariant()
if ($stateHead -ne $preparedHead) {
    Stop-Bootstrap `
        -Message (
            "Prepared state $stateHead does not match Git HEAD $preparedHead."
        ) `
        -ExitCode 1 `
        -NoPause:$NonInteractive
}

if ($preparedHead -ne $initialHead) {
    Write-LauncherNotice -Message (
        "Handing off to updated release $preparedHead."
    )
}
else {
    Write-LauncherDetail -Message (
        "Handing off to prepared release $preparedHead."
    )
}

# Resolve this path only after preparation. The next process intentionally uses
# the launcher from the verified, newly checked-out commit.
$launcherPath = Join-Path $RepositoryRoot (
    'ops\release\Invoke-DirectGitLauncher.ps1'
)
if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
    Stop-Bootstrap `
        -Message "Prepared launcher is missing: $launcherPath" `
        -ExitCode 1 `
        -NoPause:$NonInteractive
}
$launcherArguments = @('-RepositoryRoot', $RepositoryRoot)
if (-not [string]::IsNullOrWhiteSpace($UpgradeParent)) {
    $launcherArguments += @('-UpgradeParent', $UpgradeParent)
}
if ($NonInteractive) {
    $launcherArguments += '-NonInteractive'
}

$launcherExitCode = 0
Invoke-PowerShellScript `
    -PowerShellExecutable $powerShellExecutable `
    -ScriptPath $launcherPath `
    -ScriptArguments $launcherArguments `
    -ExitCodeReference ([ref]$launcherExitCode)
if ($launcherExitCode -ne 0) {
    Stop-Bootstrap `
        -Message "FFXIVShare launcher exited with code $launcherExitCode." `
        -ExitCode $launcherExitCode `
        -NoPause:$NonInteractive
}
exit 0
