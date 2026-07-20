[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepositoryRoot,
    [string]$EnvironmentFile = '',
    [string]$ReportRoot = '',
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-fA-F0-9]{40}$')]
    [string]$TargetCommit
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

if ($env:OS -ne 'Windows_NT') {
    throw 'The direct Git readiness wrapper supports Windows only.'
}
$RepositoryRoot = Get-AbsoluteLocalPath `
    -Path $RepositoryRoot `
    -Description 'RepositoryRoot'
$pythonExecutable = Join-Path $RepositoryRoot 'venv\Scripts\python.exe'
$readinessScript = Join-Path $RepositoryRoot 'ops\release\Test-DirectGitReleaseReadiness.py'
foreach ($path in @($pythonExecutable, $readinessScript, (Join-Path $RepositoryRoot 'manage.py'))) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required readiness file is missing: $path"
    }
}

if ([string]::IsNullOrWhiteSpace($EnvironmentFile)) {
    $EnvironmentFile = if (-not [string]::IsNullOrWhiteSpace($env:FFXIVSHARE_ENV_FILE)) {
        $env:FFXIVSHARE_ENV_FILE
    }
    else {
        Join-Path $RepositoryRoot '.env'
    }
}
$EnvironmentFile = Get-AbsoluteLocalPath `
    -Path $EnvironmentFile `
    -Description 'EnvironmentFile'

if ([string]::IsNullOrWhiteSpace($ReportRoot)) {
    $driveRoot = [System.IO.Path]::GetPathRoot($RepositoryRoot)
    $ReportRoot = Join-Path $driveRoot 'FFXIVShare-R20\Readiness'
}
$ReportRoot = Get-AbsoluteLocalPath -Path $ReportRoot -Description 'ReportRoot'
if (-not (Test-Path -LiteralPath $ReportRoot -PathType Container)) {
    [void][System.IO.Directory]::CreateDirectory($ReportRoot)
}

$reportName = 'readiness-{0}-{1}.json' -f (
    [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
), [Guid]::NewGuid().ToString('N').Substring(0, 8)
$reportPath = Join-Path $ReportRoot $reportName
$arguments = @(
    '-I', '-B', '-X', 'utf8', $readinessScript,
    '--repository-root', $RepositoryRoot,
    '--environment-file', $EnvironmentFile,
    '--output', $reportPath,
    '--target-commit', $TargetCommit
)

$previousErrorActionPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = 'Continue'
    & $pythonExecutable @arguments 2>&1 |
        ForEach-Object { Write-Host $_.ToString() }
    $exitCode = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $previousErrorActionPreference
}
exit $exitCode
