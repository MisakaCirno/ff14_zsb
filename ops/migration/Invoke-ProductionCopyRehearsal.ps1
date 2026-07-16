[CmdletBinding()]
param(
    [string]$RepositoryRoot = '',
    [string]$PythonExecutable = '',
    [Parameter(Mandatory = $true)][string]$SourceDatabase,
    [Parameter(Mandatory = $true)][string]$SourceChecksum,
    [Parameter(Mandatory = $true)][string]$SourceMetadata,
    [Parameter(Mandatory = $true)][string]$SourceUpgradePolicy,
    [Parameter(Mandatory = $true)][string]$SourcePolicyProposal,
    [Parameter(Mandatory = $true)][string]$SourcePolicyReview,
    [Parameter(Mandatory = $true)][string]$SourceProposalRunRoot,
    [Parameter(Mandatory = $true)][string]$SourceMediaManifest,
    [Parameter(Mandatory = $true)][string]$TargetMediaRoot,
    [Parameter(Mandatory = $true)][string]$TargetMediaSnapshotId,
    [Parameter(Mandatory = $true)][string]$RunRoot,
    [Parameter(Mandatory = $true)][switch]$ConfirmSourceImmutable,
    [Parameter(Mandatory = $true)][switch]$ConfirmTargetMediaOffline
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($MyInvocation.InvocationName -eq '.') {
    throw 'Invoke-ProductionCopyRehearsal.ps1 is a top-level CLI and cannot be dot-sourced.'
}

if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    $RepositoryRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
$RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path

if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $venvPython = Join-Path $RepositoryRoot 'venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "A dedicated project virtual environment is required: $venvPython"
    }
    $PythonExecutable = (Resolve-Path -LiteralPath $venvPython).Path
}
else {
    $PythonExecutable = (Resolve-Path -LiteralPath $PythonExecutable).Path
}

$bootstrap = Join-Path $RepositoryRoot 'ops\migration\ProductionCopyBootstrap.py'
if (-not (Test-Path -LiteralPath $bootstrap -PathType Leaf)) {
    throw "Production-copy rehearsal bootstrap is missing: $bootstrap"
}
$bootstrap = (Resolve-Path -LiteralPath $bootstrap).Path
if (-not $ConfirmSourceImmutable.IsPresent) {
    throw 'ConfirmSourceImmutable must be explicitly affirmed.'
}
if (-not $ConfirmTargetMediaOffline.IsPresent) {
    throw 'ConfirmTargetMediaOffline must be explicitly affirmed.'
}

$arguments = @(
    '-I',
    '-S',
    '-B',
    '-X',
    'utf8',
    $bootstrap,
    '--repository-root', $RepositoryRoot,
    '--python-executable', $PythonExecutable,
    '--run-root', $RunRoot,
    '--mode', 'approved-rehearsal',
    '--policy', $SourceUpgradePolicy,
    '--proposal', $SourcePolicyProposal,
    '--review-record', $SourcePolicyReview,
    '--',
    '--source-database', $SourceDatabase,
    '--source-checksum', $SourceChecksum,
    '--source-metadata', $SourceMetadata,
    '--source-proposal-run-root', $SourceProposalRunRoot,
    '--source-media-manifest', $SourceMediaManifest,
    '--target-media-root', $TargetMediaRoot,
    '--target-media-snapshot-id', $TargetMediaSnapshotId,
    '--run-root', $RunRoot,
    '--confirm-source-immutable',
    '--confirm-target-media-offline'
)

& $PythonExecutable @arguments
$exitCode = $LASTEXITCODE
exit $exitCode
