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
        [Parameter(Mandatory = $true)][string[]]$GitArguments
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $exitCode = $null
    try {
        $ErrorActionPreference = 'Continue'
        & git.exe -C $Root @GitArguments 2>&1 |
            ForEach-Object { $null = $_ }
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) {
        throw "Fixture Git command failed: git $($GitArguments -join ' ')"
    }
}

function Invoke-WorkflowProcess {
    param(
        [Parameter(Mandatory = $true)][string]$WorkflowPath,
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$PreparedStatePath,
        [Parameter(Mandatory = $true)][int]$Port
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $lines = @()
    $exitCode = $null
    try {
        $ErrorActionPreference = 'Continue'
        $lines = @(
            & powershell.exe `
                -NoProfile `
                -ExecutionPolicy Bypass `
                -File $WorkflowPath `
                -RepositoryRoot $Root `
                -StatePath $PreparedStatePath `
                -Port $Port `
                -SkipRemoteUpdate 2>&1 |
                ForEach-Object { $_.ToString() }
        )
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    return [pscustomobject]@{
        ExitCode = [int]$exitCode
        Output = $lines
    }
}

function Get-FreeTcpPort {
    $listener = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Loopback,
        0
    )
    $listener.Start()
    try {
        return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    }
    finally {
        $listener.Stop()
    }
}

$workflow = Join-Path $PSScriptRoot 'Invoke-DirectGitUpdateAndPrepare.ps1'
$testRoot = Join-Path (
    [System.IO.Path]::GetTempPath()
) ('ffxivshare-update-test-' + [Guid]::NewGuid().ToString('N'))

try {
    foreach ($directory in @(
        'venv\Scripts',
        'ops\release',
        'frontend',
        'static\app\assets',
        'staticfiles\app\assets'
    )) {
        [void][System.IO.Directory]::CreateDirectory(
            (Join-Path $testRoot $directory)
        )
    }

    $utf8 = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText(
        (Join-Path $testRoot 'venv\Scripts\python.exe'),
        '',
        $utf8
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $testRoot 'ops\release\Invoke-DirectGitReleaseReadiness.ps1'),
        '',
        $utf8
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $testRoot 'manage.py'),
        "print('fixture')`n",
        $utf8
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $testRoot 'requirements.txt'),
        "Django==5.2.16`n",
        $utf8
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $testRoot 'frontend\package-lock.json'),
        "{} `n",
        $utf8
    )
    $manifest = (
        '{"src/main.ts":{"file":"assets/main.js","css":["assets/main.css"]}}'
    )
    foreach ($relativePath in @(
        'static\app\manifest.json',
        'staticfiles\app\manifest.json'
    )) {
        [System.IO.File]::WriteAllText(
            (Join-Path $testRoot $relativePath),
            $manifest,
            $utf8
        )
    }
    foreach ($relativePath in @(
        'static\app\assets\main.js',
        'static\app\assets\main.css',
        'staticfiles\app\assets\main.js',
        'staticfiles\app\assets\main.css'
    )) {
        [System.IO.File]::WriteAllText(
            (Join-Path $testRoot $relativePath),
            'fixture',
            $utf8
        )
    }

    Invoke-Git -Root $testRoot -GitArguments @('init')
    Invoke-Git -Root $testRoot -GitArguments @('config', 'user.name', 'Codex Test')
    Invoke-Git -Root $testRoot -GitArguments @(
        'config',
        'user.email',
        'codex-test@example.invalid'
    )
    Invoke-Git -Root $testRoot -GitArguments @('checkout', '-b', 'master')
    Invoke-Git -Root $testRoot -GitArguments @('add', '--all')
    Invoke-Git -Root $testRoot -GitArguments @('commit', '-m', 'fixture')
    $commit = (& git.exe -C $testRoot rev-parse HEAD).Trim()
    Assert-Condition `
        -Condition ($commit -match '^[a-f0-9]{40}$') `
        -Message 'Fixture commit is invalid.'

    $statePath = Join-Path $testRoot 'prepared-commit.txt'
    [System.IO.File]::WriteAllText($statePath, "$commit`r`n", $utf8)
    $port = Get-FreeTcpPort
    $preparedResult = Invoke-WorkflowProcess `
        -WorkflowPath $workflow `
        -Root $testRoot `
        -PreparedStatePath $statePath `
        -Port $port
    Assert-Condition `
        -Condition ($preparedResult.ExitCode -eq 0) `
        -Message "Prepared no-op launch failed: $($preparedResult.Output -join ' ')"
    Assert-Condition `
        -Condition (
            ($preparedResult.Output -join "`n").Contains(
                'is already prepared.'
            )
        ) `
        -Message 'Prepared no-op launch did not use the fast path.'

    [System.IO.File]::AppendAllText(
        (Join-Path $testRoot 'manage.py'),
        "# dirty`n",
        $utf8
    )
    $dirtyResult = Invoke-WorkflowProcess `
        -WorkflowPath $workflow `
        -Root $testRoot `
        -PreparedStatePath $statePath `
        -Port $port
    Assert-Condition `
        -Condition ($dirtyResult.ExitCode -ne 0) `
        -Message 'Dirty critical runtime files were not blocked.'
    Assert-Condition `
        -Condition (
            ($dirtyResult.Output -join "`n").Contains(
                'Critical runtime files are modified.'
            )
        ) `
        -Message 'Dirty critical runtime failure was not explicit.'
    Assert-Condition `
        -Condition (
            [System.IO.File]::ReadAllText($statePath).Trim() -eq $commit
        ) `
        -Message 'The failed dirty-worktree launch changed prepared state.'

    $global:LASTEXITCODE = 0
    Write-Host 'Direct Git update workflow tests passed.'
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
