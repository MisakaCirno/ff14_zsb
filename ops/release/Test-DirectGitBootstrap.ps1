[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-Condition {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )

    if (-not $Condition) {
        throw $Message
    }
}

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $exitCode = $null
    try {
        $ErrorActionPreference = 'Continue'
        & git.exe -C $Root @Arguments 2>&1 |
            ForEach-Object { $null = $_ }
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) {
        throw "Fixture Git command failed: git $($Arguments -join ' ')"
    }
}

$bootstrapPath = Join-Path $PSScriptRoot 'Invoke-DirectGitBootstrap.ps1'
$consolePath = Join-Path $PSScriptRoot 'LauncherConsole.ps1'
$testRoot = Join-Path (
    [System.IO.Path]::GetTempPath()
) ('ffxivshare-bootstrap-test-' + [Guid]::NewGuid().ToString('N'))

try {
    $releaseRoot = Join-Path $testRoot 'ops\release'
    [void][System.IO.Directory]::CreateDirectory($releaseRoot)
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::Copy(
        $consolePath,
        (Join-Path $releaseRoot 'LauncherConsole.ps1'),
        $false
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $testRoot 'manage.py'),
        "print('fixture')`n",
        $utf8
    )

    $preparerSource = @'
param(
    [string]$RepositoryRoot,
    [string]$StatePath,
    [switch]$SkipRemoteUpdate,
    [switch]$NonInteractive
)

$head = (& git.exe -C $RepositoryRoot rev-parse HEAD).Trim()
Write-Host "Fixture preparation output for $head."
$stateRoot = Split-Path -Parent $StatePath
[void][System.IO.Directory]::CreateDirectory($stateRoot)
[System.IO.File]::WriteAllText(
    $StatePath,
    "$head`r`n",
    [System.Text.Encoding]::ASCII
)
exit 0
'@
    [System.IO.File]::WriteAllText(
        (Join-Path $releaseRoot 'Invoke-DirectGitUpdateAndPrepare.ps1'),
        $preparerSource,
        $utf8
    )

    $launcherSource = @'
param(
    [string]$RepositoryRoot,
    [string]$UpgradeParent = '',
    [switch]$NonInteractive
)

$head = (& git.exe -C $RepositoryRoot rev-parse HEAD).Trim()
Write-Host "Fixture launcher output for $head."
[System.IO.File]::WriteAllText(
    (Join-Path $RepositoryRoot 'launcher-handoff.txt'),
    "$head`r`n",
    [System.Text.Encoding]::ASCII
)
exit 0
'@
    [System.IO.File]::WriteAllText(
        (Join-Path $releaseRoot 'Invoke-DirectGitLauncher.ps1'),
        $launcherSource,
        $utf8
    )

    Invoke-Git -Root $testRoot -Arguments @('init')
    Invoke-Git -Root $testRoot -Arguments @(
        'config', 'user.name', 'Codex Test'
    )
    Invoke-Git -Root $testRoot -Arguments @(
        'config', 'user.email', 'codex-test@example.invalid'
    )
    Invoke-Git -Root $testRoot -Arguments @('add', '--all')
    Invoke-Git -Root $testRoot -Arguments @('commit', '-m', 'fixture')
    $commit = (& git.exe -C $testRoot rev-parse HEAD).Trim()
    $statePath = Join-Path $testRoot '.state\prepared-commit.txt'

    $previousErrorActionPreference = $ErrorActionPreference
    $exitCode = $null
    try {
        $ErrorActionPreference = 'Continue'
        & powershell.exe `
            -NoProfile `
            -ExecutionPolicy Bypass `
            -File $bootstrapPath `
            -RepositoryRoot $testRoot `
            -StatePath $statePath `
            -SkipRemoteUpdate `
            -NonInteractive
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    Assert-Condition `
        -Condition ($exitCode -eq 0) `
        -Message "Bootstrap fixture exited with code $exitCode."
    $handoffPath = Join-Path $testRoot 'launcher-handoff.txt'
    Assert-Condition `
        -Condition (Test-Path -LiteralPath $handoffPath -PathType Leaf) `
        -Message 'Bootstrap did not hand off to the prepared launcher.'
    Assert-Condition `
        -Condition (
            [System.IO.File]::ReadAllText($handoffPath).Trim() -eq $commit
        ) `
        -Message 'Bootstrap handed off a launcher for the wrong Git HEAD.'

    Remove-Item -LiteralPath $handoffPath -Force
    $failedPreparerSource = @'
param(
    [string]$RepositoryRoot,
    [string]$StatePath,
    [switch]$SkipRemoteUpdate,
    [switch]$NonInteractive
)

Write-Host 'Fixture preparation failed after writing normal output.'
exit 13
'@
    [System.IO.File]::WriteAllText(
        (Join-Path $releaseRoot 'Invoke-DirectGitUpdateAndPrepare.ps1'),
        $failedPreparerSource,
        $utf8
    )

    $failureExitCode = $null
    try {
        $ErrorActionPreference = 'Continue'
        & powershell.exe `
            -NoProfile `
            -ExecutionPolicy Bypass `
            -File $bootstrapPath `
            -RepositoryRoot $testRoot `
            -StatePath $statePath `
            -SkipRemoteUpdate `
            -NonInteractive
        $failureExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    Assert-Condition `
        -Condition ($failureExitCode -eq 13) `
        -Message (
            'Bootstrap did not preserve a child failure exit code after ' +
            "normal output: $failureExitCode."
        )
    Assert-Condition `
        -Condition (-not (Test-Path -LiteralPath $handoffPath -PathType Leaf)) `
        -Message 'Bootstrap launched the application after preparation failed.'

    Write-Host 'Direct Git bootstrap test passed.'
}
finally {
    if (Test-Path -LiteralPath $testRoot -PathType Container) {
        $resolvedTestRoot = [System.IO.Path]::GetFullPath($testRoot)
        $resolvedTempRoot = [System.IO.Path]::GetFullPath(
            [System.IO.Path]::GetTempPath()
        )
        if (-not $resolvedTestRoot.StartsWith(
            $resolvedTempRoot,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw 'Refusing to remove a test directory outside the temp root.'
        }
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
}
