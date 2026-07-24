[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepositoryRoot,
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$Remote = 'origin',
    [ValidatePattern('^[A-Za-z0-9._/-]+$')]
    [string]$Branch = 'master',
    [string]$StatePath = '',
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,
    [switch]$SkipRemoteUpdate,
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

function Invoke-GitText {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string[]]$GitArguments
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $lines = @()
    $exitCode = $null
    try {
        $ErrorActionPreference = 'Continue'
        $lines = @(& git.exe -C $Root @GitArguments 2>&1 |
            ForEach-Object { $_.ToString() })
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) {
        throw "Git command failed: git $($GitArguments -join ' ')"
    }
    return ($lines -join [Environment]::NewLine).Trim()
}

function Invoke-ExternalCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$CommandArguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$Description,
        [switch]$AllowFailure
    )

    Write-Host "==> $Description"
    $previousErrorActionPreference = $ErrorActionPreference
    $exitCode = $null
    Push-Location $WorkingDirectory
    try {
        $ErrorActionPreference = 'Continue'
        & $Executable @CommandArguments 2>&1 |
            ForEach-Object { Write-Host $_.ToString() }
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        Pop-Location
    }
    if ($exitCode -ne 0 -and -not $AllowFailure) {
        throw "$Description failed with exit code $exitCode."
    }
    return [int]$exitCode
}

function Get-GitHead {
    param([Parameter(Mandatory = $true)][string]$Root)

    $head = Invoke-GitText -Root $Root -GitArguments @('rev-parse', 'HEAD')
    if ($head -notmatch '^[a-f0-9]{40}$') {
        throw 'Could not resolve one immutable Git HEAD.'
    }
    return $head
}

function Assert-CriticalWorktreeClean {
    param([Parameter(Mandatory = $true)][string]$Root)

    $criticalPaths = @(
        'ffxivshare',
        'frontend',
        'ops/release',
        'shares',
        'static',
        'templates',
        '.env.production.sample',
        'manage.py',
        'preflight_ffxivshare.bat',
        'requirements.txt',
        'start_ffxivshare.bat',
        'verify.ps1'
    )
    $statusArguments = @(
        'status',
        '--porcelain=v1',
        '--untracked-files=all',
        '--'
    ) + $criticalPaths
    $status = Invoke-GitText -Root $Root -GitArguments $statusArguments
    if (-not [string]::IsNullOrWhiteSpace($status)) {
        Write-Host $status
        throw 'Critical runtime files are modified. Refusing to update or start.'
    }
}

function Test-ManifestAssets {
    param([Parameter(Mandatory = $true)][string]$ManifestPath)

    try {
        $manifest = (
            [System.IO.File]::ReadAllText($ManifestPath) |
            ConvertFrom-Json
        )
        $entry = $manifest.'src/main.ts'
        if ($null -eq $entry -or [string]::IsNullOrWhiteSpace($entry.file)) {
            return $false
        }
        $assetRoot = Split-Path -Parent $ManifestPath
        $assets = @([string]$entry.file)
        $assets += @($entry.css | ForEach-Object { [string]$_ })
        foreach ($asset in $assets) {
            if ($asset.Contains('..') -or
                -not (Test-Path -LiteralPath (Join-Path $assetRoot $asset) -PathType Leaf)) {
                return $false
            }
        }
        return $true
    }
    catch {
        return $false
    }
}

function Test-PreparedState {
    param(
        [Parameter(Mandatory = $true)][string]$Commit,
        [Parameter(Mandatory = $true)][string]$PreparedStatePath,
        [Parameter(Mandatory = $true)][string]$Root
    )

    if (-not (Test-Path -LiteralPath $PreparedStatePath -PathType Leaf)) {
        return $false
    }
    $preparedCommit = (
        [System.IO.File]::ReadAllText($PreparedStatePath)
    ).Trim().ToLowerInvariant()
    if ($preparedCommit -ne $Commit) {
        return $false
    }

    $sourceManifest = Join-Path $Root 'static\app\manifest.json'
    $collectedManifest = Join-Path $Root 'staticfiles\app\manifest.json'
    if (-not (Test-Path -LiteralPath $sourceManifest -PathType Leaf) -or
        -not (Test-Path -LiteralPath $collectedManifest -PathType Leaf)) {
        return $false
    }
    if (-not (Test-ManifestAssets -ManifestPath $sourceManifest) -or
        -not (Test-ManifestAssets -ManifestPath $collectedManifest)) {
        return $false
    }
    return (
        (Get-FileHash -LiteralPath $sourceManifest -Algorithm SHA256).Hash -eq
        (Get-FileHash -LiteralPath $collectedManifest -Algorithm SHA256).Hash
    )
}

function Set-PreparedState {
    param(
        [Parameter(Mandatory = $true)][string]$Commit,
        [Parameter(Mandatory = $true)][string]$PreparedStatePath
    )

    $stateDirectory = Split-Path -Parent $PreparedStatePath
    if (-not (Test-Path -LiteralPath $stateDirectory -PathType Container)) {
        [void][System.IO.Directory]::CreateDirectory($stateDirectory)
    }
    $temporaryPath = "$PreparedStatePath.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        [System.IO.File]::WriteAllText(
            $temporaryPath,
            "$Commit`r`n",
            [System.Text.Encoding]::ASCII
        )
        Move-Item -LiteralPath $temporaryPath -Destination $PreparedStatePath -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }
}

function Invoke-ReleasePreparation {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$PythonExecutable
    )

    $npmCommand = Get-Command `
        -Name 'npm.cmd' `
        -CommandType Application `
        -ErrorAction Stop |
        Select-Object -First 1
    [void](Invoke-ExternalCommand `
        -Executable $PythonExecutable `
        -CommandArguments @(
            '-B',
            '-m',
            'pip',
            'install',
            '--disable-pip-version-check',
            '--no-input',
            '-r',
            'requirements.txt'
        ) `
        -WorkingDirectory $Root `
        -Description 'Synchronize Python dependencies')
    [void](Invoke-ExternalCommand `
        -Executable $npmCommand.Source `
        -CommandArguments @('ci', '--prefix', 'frontend') `
        -WorkingDirectory $Root `
        -Description 'Synchronize frontend dependencies')
    [void](Invoke-ExternalCommand `
        -Executable $npmCommand.Source `
        -CommandArguments @('--prefix', 'frontend', 'run', 'build') `
        -WorkingDirectory $Root `
        -Description 'Build frontend assets')
    [void](Invoke-ExternalCommand `
        -Executable $PythonExecutable `
        -CommandArguments @(
            '-B',
            'manage.py',
            'collectstatic',
            '--noinput'
        ) `
        -WorkingDirectory $Root `
        -Description 'Collect production static files')
}

function Invoke-ReleaseReadiness {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Commit
    )

    $readinessWrapper = Join-Path $Root 'ops\release\Invoke-DirectGitReleaseReadiness.ps1'
    $hadAppVersion = Test-Path Env:APP_VERSION
    $previousAppVersion = [Environment]::GetEnvironmentVariable(
        'APP_VERSION',
        [EnvironmentVariableTarget]::Process
    )
    try {
        # Readiness must validate the same immutable version binding that the
        # launcher will inject into the Waitress process.
        $env:APP_VERSION = $Commit
        return Invoke-ExternalCommand `
            -Executable 'powershell.exe' `
            -CommandArguments @(
                '-NoProfile',
                '-ExecutionPolicy',
                'Bypass',
                '-File',
                $readinessWrapper,
                '-RepositoryRoot',
                $Root,
                '-TargetCommit',
                $Commit
            ) `
            -WorkingDirectory $Root `
            -Description 'Verify release readiness' `
            -AllowFailure
    }
    finally {
        if ($hadAppVersion) {
            $env:APP_VERSION = $previousAppVersion
        }
        else {
            Remove-Item Env:APP_VERSION -ErrorAction SilentlyContinue
        }
    }
}

if ($env:OS -ne 'Windows_NT') {
    throw 'The direct Git update workflow supports Windows only.'
}
$RepositoryRoot = Get-AbsoluteLocalPath `
    -Path $RepositoryRoot `
    -Description 'RepositoryRoot'
$pythonExecutable = Join-Path $RepositoryRoot 'venv\Scripts\python.exe'
$readinessScript = Join-Path $RepositoryRoot 'ops\release\Invoke-DirectGitReleaseReadiness.ps1'
foreach ($path in @(
    $pythonExecutable,
    $readinessScript,
    (Join-Path $RepositoryRoot 'manage.py'),
    (Join-Path $RepositoryRoot 'requirements.txt'),
    (Join-Path $RepositoryRoot 'frontend\package-lock.json')
)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required update file is missing: $path"
    }
}
if ([string]::IsNullOrWhiteSpace($StatePath)) {
    $driveRoot = [System.IO.Path]::GetPathRoot($RepositoryRoot)
    $StatePath = Join-Path $driveRoot 'FFXIVShare-R20\State\prepared-commit.txt'
}
$StatePath = Get-AbsoluteLocalPath -Path $StatePath -Description 'StatePath'

if (Test-Listener -Port $Port) {
    throw "Port $Port is already listening. Stop the current site before updating."
}
Assert-CriticalWorktreeClean -Root $RepositoryRoot

$currentCommit = Get-GitHead -Root $RepositoryRoot
$updated = $false
$skipUpdateFromEnvironment = (
    -not [string]::IsNullOrWhiteSpace($env:FFXIVSHARE_SKIP_UPDATE) -and
    $env:FFXIVSHARE_SKIP_UPDATE.Trim() -eq '1'
)

if (-not $SkipRemoteUpdate -and -not $skipUpdateFromEnvironment) {
    $currentBranch = Invoke-GitText `
        -Root $RepositoryRoot `
        -GitArguments @('branch', '--show-current')
    if ($currentBranch -ne $Branch) {
        throw "Expected branch '$Branch', found '$currentBranch'."
    }

    $fetchExitCode = Invoke-ExternalCommand `
        -Executable 'git.exe' `
        -CommandArguments @(
            '-C',
            $RepositoryRoot,
            'fetch',
            '--prune',
            $Remote,
            $Branch
        ) `
        -WorkingDirectory $RepositoryRoot `
        -Description "Check $Remote/$Branch for updates" `
        -AllowFailure

    if ($fetchExitCode -ne 0) {
        if ($NonInteractive) {
            throw 'Remote update check failed in non-interactive mode.'
        }
        Write-Host ''
        Write-Host 'The remote update check failed.'
        Write-Host '[1] Start the current prepared version (default)'
        Write-Host '[2] Stop'
        $offlineChoice = (Read-Host 'Choose 1 or 2').Trim()
        if ($offlineChoice -notin @('', '1')) {
            throw 'Startup stopped after the remote update check failed.'
        }
    }
    else {
        $remoteReference = "$Remote/$Branch"
        $targetCommit = Invoke-GitText `
            -Root $RepositoryRoot `
            -GitArguments @('rev-parse', "$remoteReference^{commit}")
        if ($targetCommit -notmatch '^[a-f0-9]{40}$') {
            throw 'Could not resolve the remote branch to one immutable commit.'
        }

        if ($targetCommit -ne $currentCommit) {
            $ancestorExitCode = Invoke-ExternalCommand `
                -Executable 'git.exe' `
                -CommandArguments @(
                    '-C',
                    $RepositoryRoot,
                    'merge-base',
                    '--is-ancestor',
                    $currentCommit,
                    $targetCommit
                ) `
                -WorkingDirectory $RepositoryRoot `
                -Description 'Verify fast-forward update path' `
                -AllowFailure
            if ($ancestorExitCode -ne 0) {
                throw 'Local and remote history are not a safe fast-forward.'
            }

            Write-Host ''
            Write-Host "Update available: $currentCommit -> $targetCommit"
            $summary = Invoke-GitText `
                -Root $RepositoryRoot `
                -GitArguments @(
                    'log',
                    '--oneline',
                    '--max-count=10',
                    "$currentCommit..$targetCommit"
                )
            if (-not [string]::IsNullOrWhiteSpace($summary)) {
                Write-Host $summary
            }
            Write-Host ''
            Write-Host '[1] Update, prepare, and start (default)'
            Write-Host '[2] Start the current version without updating'
            Write-Host '[3] Stop'
            if ($NonInteractive) {
                throw 'A remote update requires interactive approval.'
            }
            $updateChoice = (Read-Host 'Choose 1, 2, or 3').Trim()
            if ([string]::IsNullOrWhiteSpace($updateChoice)) {
                $updateChoice = '1'
            }
            if ($updateChoice -eq '3') {
                throw 'Startup stopped before applying the update.'
            }
            if ($updateChoice -eq '1') {
                [void](Invoke-ExternalCommand `
                    -Executable 'git.exe' `
                    -CommandArguments @(
                        '-C',
                        $RepositoryRoot,
                        'merge',
                        '--ff-only',
                        $targetCommit
                    ) `
                    -WorkingDirectory $RepositoryRoot `
                    -Description "Fast-forward to $targetCommit")
                $currentCommit = Get-GitHead -Root $RepositoryRoot
                if ($currentCommit -ne $targetCommit) {
                    throw 'The fast-forward did not reach the approved target commit.'
                }
                Assert-CriticalWorktreeClean -Root $RepositoryRoot
                $updated = $true
            }
            elseif ($updateChoice -ne '2') {
                throw 'Invalid update choice. No update was applied.'
            }
        }
        else {
            Write-Host "Code is current at $currentCommit."
        }
    }
}
else {
    Write-Host 'Remote update check skipped.'
}

$prepared = Test-PreparedState `
    -Commit $currentCommit `
    -PreparedStatePath $StatePath `
    -Root $RepositoryRoot
if ($updated -or -not $prepared) {
    Write-Host ''
    Write-Host "Preparing release $currentCommit..."
    Invoke-ReleasePreparation `
        -Root $RepositoryRoot `
        -PythonExecutable $pythonExecutable
    $readinessExitCode = Invoke-ReleaseReadiness `
        -Root $RepositoryRoot `
        -Commit $currentCommit
    if ($readinessExitCode -ne 0) {
        throw "Release readiness failed with exit code $readinessExitCode."
    }
    Set-PreparedState `
        -Commit $currentCommit `
        -PreparedStatePath $StatePath
    Write-Host "[OK] Release $currentCommit is prepared."
}
else {
    Write-Host "Release $currentCommit is already prepared."
}

exit 0
