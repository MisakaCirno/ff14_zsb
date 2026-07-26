[CmdletBinding()]
param(
    [string]$RepositoryRoot = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-Contract {
    param(
        [Parameter(Mandatory = $true)]
        [bool]$Condition,
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    if (-not $Condition) {
        throw $Message
    }
}

if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    $RepositoryRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
$RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$rootStartPath = Join-Path $RepositoryRoot 'start_ffxivshare.bat'
$rootPreflightPath = Join-Path $RepositoryRoot 'preflight_ffxivshare.bat'
$startPath = Join-Path $RepositoryRoot 'ops\release\Start-DirectGitWaitress.bat'
$preparePath = Join-Path $RepositoryRoot 'ops\release\Invoke-DirectGitUpdateAndPrepare.ps1'
$prepareTestPath = Join-Path $RepositoryRoot 'ops\release\Test-DirectGitUpdateWorkflow.ps1'
$bootstrapTestPath = Join-Path $RepositoryRoot 'ops\release\Test-DirectGitBootstrap.ps1'
$trampolineTestPath = Join-Path $RepositoryRoot 'ops\release\Test-BatchTrampolineImmutability.ps1'
$bootstrapPath = Join-Path $RepositoryRoot 'ops\release\Invoke-DirectGitBootstrap.ps1'
$launcherPath = Join-Path $RepositoryRoot 'ops\release\Invoke-DirectGitLauncher.ps1'
$consolePath = Join-Path $RepositoryRoot 'ops\release\LauncherConsole.ps1'
$upgradePath = Join-Path $RepositoryRoot 'ops\release\Invoke-DirectGitDatabaseUpgrade.ps1'
$readinessWrapperPath = Join-Path $RepositoryRoot 'ops\release\Invoke-DirectGitReleaseReadiness.ps1'
$readinessPath = Join-Path $RepositoryRoot 'ops\release\Test-DirectGitReleaseReadiness.py'
$nginxPath = Join-Path $RepositoryRoot 'ops\nginx\ffxivshare.direct-git.locations.conf.example'

foreach ($path in @(
    $rootStartPath,
    $rootPreflightPath,
    $startPath,
    $bootstrapPath,
    $preparePath,
    $prepareTestPath,
    $bootstrapTestPath,
    $trampolineTestPath,
    $launcherPath,
    $consolePath,
    $upgradePath,
    $readinessWrapperPath,
    $nginxPath
)) {
    Assert-Contract `
        -Condition (Test-Path -LiteralPath $path -PathType Leaf) `
        -Message "Direct Git runtime file is missing: $path"
    $source = [System.IO.File]::ReadAllText($path)
    $nonAscii = @($source.ToCharArray() | Where-Object { [int]$_ -gt 127 })
    Assert-Contract `
        -Condition ($nonAscii.Count -eq 0) `
        -Message "Direct Git runtime file must remain ASCII-compatible: $path"
}
Assert-Contract `
    -Condition (Test-Path -LiteralPath $readinessPath -PathType Leaf) `
    -Message "Direct Git readiness checker is missing: $readinessPath"

$rootStartSource = [System.IO.File]::ReadAllText($rootStartPath)
Assert-Contract `
    -Condition (
        $rootStartSource.Contains('Invoke-DirectGitBootstrap.ps1') -and
        $rootStartSource.Contains('& exit /b') -and
        -not $rootStartSource.Contains('call ')
    ) `
    -Message 'Root start script is not an immutable bootstrap trampoline.'
$rootStartLines = @($rootStartSource -split '\r?\n' | Where-Object {
    -not [string]::IsNullOrWhiteSpace($_)
})
Assert-Contract `
    -Condition ($rootStartLines.Count -eq 1) `
    -Message 'Root start script must remain one physical command line.'
$rootPreflightSource = [System.IO.File]::ReadAllText($rootPreflightPath)
foreach ($requiredText in @(
    'Invoke-DirectGitReleaseReadiness.ps1',
    'FFXIVSHARE_TARGET_COMMIT',
    'Enter the approved 40-character target commit SHA:',
    '-TargetCommit "%TARGET_COMMIT%"'
)) {
    Assert-Contract `
        -Condition $rootPreflightSource.Contains($requiredText) `
        -Message "Root preflight script is missing: $requiredText"
}

$startSource = [System.IO.File]::ReadAllText($startPath)
foreach ($requiredText in @(
    'Invoke-DirectGitBootstrap.ps1',
    'powershell.exe -NoProfile -ExecutionPolicy Bypass',
    '-RepositoryRoot "%~dp0..\.."',
    '& exit /b'
)) {
    Assert-Contract `
        -Condition $startSource.Contains($requiredText) `
        -Message "Direct Git start script is missing: $requiredText"
}
Assert-Contract `
    -Condition (
        @($startSource -split '\r?\n' | Where-Object {
            -not [string]::IsNullOrWhiteSpace($_)
        }).Count -eq 1
    ) `
    -Message 'Compatibility start script must remain one physical command line.'

$bootstrapSource = [System.IO.File]::ReadAllText($bootstrapPath)
foreach ($requiredText in @(
    'chcp.com 65001',
    '$env:NO_COLOR = ''1''',
    'Invoke-DirectGitUpdateAndPrepare.ps1',
    'Invoke-DirectGitLauncher.ps1',
    'Invoke-PowerShellScript',
    '[ref]$ExitCodeReference',
    '-ExitCodeReference ([ref]$prepareExitCode)',
    '-ExitCodeReference ([ref]$launcherExitCode)',
    'Get-GitHead',
    'prepared-commit.txt',
    '$stateHead -ne $preparedHead',
    'Handing off to updated release',
    'Resolve this path only after preparation',
    'Stop-Bootstrap'
)) {
    Assert-Contract `
        -Condition $bootstrapSource.Contains($requiredText) `
        -Message "Direct Git bootstrap is missing: $requiredText"
}
Assert-Contract `
    -Condition (
        $bootstrapSource.IndexOf('$prepareExitCode =') -lt
        $bootstrapSource.IndexOf('$preparedHead =') -and
        $bootstrapSource.IndexOf('$preparedHead =') -lt
        $bootstrapSource.IndexOf('$launcherExitCode =')
    ) `
    -Message 'Bootstrap handoff order is invalid.'

$consoleSource = [System.IO.File]::ReadAllText($consolePath)
foreach ($requiredText in @(
    'Write-LauncherStep',
    'Write-LauncherSuccess',
    'Write-LauncherWarning',
    'Write-LauncherError',
    'Write-LauncherChoice',
    'Read-LauncherChoice',
    'Read-Host',
    'Write-LauncherProcessLine',
    '-ForegroundColor Cyan',
    '-ForegroundColor Green',
    '-ForegroundColor Yellow',
    '-ForegroundColor Magenta',
    "'Red'",
    "'DarkGray'"
)) {
    Assert-Contract `
        -Condition $consoleSource.Contains($requiredText) `
        -Message "Launcher console helpers are missing: $requiredText"
}

$prepareSource = [System.IO.File]::ReadAllText($preparePath)
foreach ($requiredText in @(
    '. $consoleScript',
    'Write-LauncherStep',
    'Write-LauncherProcessLine',
    'Write-LauncherChoice',
    'Read-LauncherChoice',
    'Assert-CriticalWorktreeClean',
    'FFXIVSHARE_SKIP_UPDATE',
    "'fetch'",
    "'merge'",
    "'--ff-only'",
    'Update, prepare, and start (default)',
    'Start the current version without updating',
    'pip',
    'install',
    "'ci', '--prefix', 'frontend'",
    "'--prefix', 'frontend', 'run', 'build'",
    "'collectstatic'",
    'Invoke-DirectGitReleaseReadiness.ps1',
    '$env:APP_VERSION = $Commit',
    'prepared-commit.txt',
    'Test-PreparedState',
    'Set-PreparedState'
)) {
    Assert-Contract `
        -Condition $prepareSource.Contains($requiredText) `
        -Message "Direct Git update workflow is missing: $requiredText"
}
foreach ($forbiddenPattern in @(
    '(?i)\bgit(?:\.exe)?\s+(?:reset|clean|stash|checkout|switch)',
    '(?i)manage\.py[''"]?\s*,?\s*[''"]?migrate',
    '(?i)\bStart-Service\b',
    '(?i)\bStop-Service\b',
    '(?i)\bRestart-Service\b',
    '(?i)\bnginx(?:\.exe)?\b',
    '(?i)\bbun(?:\.exe)?\b',
    '(?i)\bdb\.sqlite3\b'
)) {
    Assert-Contract `
        -Condition (-not [regex]::IsMatch($prepareSource, $forbiddenPattern)) `
        -Message "Direct Git update workflow contains a forbidden operation: $forbiddenPattern"
}

$launcherSource = [System.IO.File]::ReadAllText($launcherPath)
foreach ($requiredText in @(
    '. $consoleScript',
    'Write-LauncherStep',
    'Write-LauncherSuccess',
    'Write-LauncherProcessLine',
    'Write-LauncherChoice',
    'Read-LauncherChoice',
    'manage.py check_deployment_schema',
    'Invoke-DirectGitDatabaseUpgrade.ps1',
    'Create a verified backup, upgrade safely, and start',
    'Do not upgrade; keep the application stopped (default)',
    '-Confirm:$false',
    '$env:APP_VERSION = Get-GitHead -Root $RepositoryRoot',
    '-m waitress',
    '--listen=127.0.0.1:8000',
    '--threads=4',
    '--trusted-proxy=127.0.0.1',
    '--trusted-proxy-headers=x-forwarded-for x-forwarded-proto',
    '--clear-untrusted-proxy-headers',
    '--no-expose-tracebacks',
    'ffxivshare.wsgi:application'
)) {
    Assert-Contract `
        -Condition $launcherSource.Contains($requiredText) `
        -Message "Direct Git launcher is missing: $requiredText"
}

$readinessWrapperSource = [System.IO.File]::ReadAllText($readinessWrapperPath)
foreach ($requiredText in @(
    'Test-DirectGitReleaseReadiness.py',
    'FFXIVShare-R20\Readiness',
    'FFXIVSHARE_ENV_FILE',
    "'-I', '-B', '-X', 'utf8'",
    '--repository-root',
    '--environment-file',
    '--output',
    '--target-commit'
)) {
    Assert-Contract `
        -Condition $readinessWrapperSource.Contains($requiredText) `
        -Message "Direct Git readiness wrapper is missing: $requiredText"
}

foreach ($forbiddenPattern in @(
    '(?i)manage\.py\s+migrate',
    '(?i)manage\.py\s+collectstatic',
    '(?i)\bgit\s+(?:pull|fetch|checkout|switch|reset|clean|stash)',
    '(?i)\bpip\s+install',
    '(?i)\bnpm(?:\.cmd)?\s+(?:install|ci|run)'
)) {
    Assert-Contract `
        -Condition (
            -not [regex]::IsMatch($rootStartSource, $forbiddenPattern) -and
            -not [regex]::IsMatch($rootPreflightSource, $forbiddenPattern) -and
            -not [regex]::IsMatch($startSource, $forbiddenPattern) -and
            -not [regex]::IsMatch($bootstrapSource, $forbiddenPattern) -and
            -not [regex]::IsMatch($launcherSource, $forbiddenPattern) -and
            -not [regex]::IsMatch($readinessWrapperSource, $forbiddenPattern)
        ) `
        -Message "Direct Git start path contains a forbidden mutation: $forbiddenPattern"
}

$upgradeSource = [System.IO.File]::ReadAllText($upgradePath)
foreach ($requiredText in @(
    'SupportsShouldProcess = $true',
    'Assert-PortStopped',
    'Assert-NoSqliteSidecars',
    '[System.IO.File]::Copy($DatabasePath, $sourceRollback, $false)',
    "'manage.py', 'migrate', '--noinput'",
    "'manage.py', 'check_deployment_schema', '--require-current'",
    "'manage.py', 'preflight_share_restrictions'",
    "'manage.py', 'verify_database_upgrade_semantics'",
    'database-semantic-comparison.json',
    'semantic_comparison_report = $semanticReport',
    "'manage.py', 'check', '--deploy'",
    '''manage.py'', ''backup_database'', $verifiedCandidate',
    '[System.IO.File]::Replace(',
    'Automatic rollback hash verification failed.',
    'database_switch_completed = $true',
    'safe_to_start = $true'
)) {
    Assert-Contract `
        -Condition $upgradeSource.Contains($requiredText) `
        -Message "Direct Git upgrade workflow is missing: $requiredText"
}
foreach ($forbiddenPattern in @(
    '(?i)\bgit(?:\.exe)?\s+(?:pull|checkout|switch|reset|clean|stash)',
    '(?i)\bStart-Service\b',
    '(?i)\bStop-Service\b',
    '(?i)\bRestart-Service\b',
    '(?i)\bnginx(?:\.exe)?\b',
    '(?i)\bbun(?:\.exe)?\b'
)) {
    Assert-Contract `
        -Condition (-not [regex]::IsMatch($upgradeSource, $forbiddenPattern)) `
        -Message "Direct Git upgrade workflow contains a forbidden operation: $forbiddenPattern"
}

$nginxSource = [System.IO.File]::ReadAllText($nginxPath)
foreach ($requiredText in @(
    'Include this file inside the existing ff14hub.com HTTPS server block.',
    'RESERVED: /n/',
    'intentionally does not define /n/',
    'alias C:/Users/Administrator/Desktop/srv/ff14_zsb/staticfiles/app/assets/;',
    'alias C:/Users/Administrator/Desktop/srv/ff14_zsb/staticfiles/;',
    'location = /health/live/',
    'location = /health/ready/',
    'proxy_set_header X-Forwarded-For $remote_addr;',
    'proxy_pass http://127.0.0.1:8000;'
)) {
    Assert-Contract `
        -Condition $nginxSource.Contains($requiredText) `
        -Message "Direct Git Nginx include is missing: $requiredText"
}
Assert-Contract `
    -Condition (-not $nginxSource.Contains('location ^~ /n/')) `
    -Message 'Direct Git Nginx include must not replace the /n/ renderer.'
Assert-Contract `
    -Condition (-not $nginxSource.Contains('$proxy_add_x_forwarded_for')) `
    -Message 'Direct Git Nginx include appends untrusted forwarding data.'
Assert-Contract `
    -Condition (-not $nginxSource.Contains('proxy_pass http://[::1]:3000')) `
    -Message 'Direct Git Nginx include unexpectedly controls the Bun renderer.'

$immutablePolicies = [regex]::Matches(
    $nginxSource,
    'Cache-Control\s+"[^"]*immutable[^"]*"'
)
Assert-Contract `
    -Condition ($immutablePolicies.Count -eq 1) `
    -Message 'Only fingerprinted Vite assets may have immutable caching.'

Write-Host 'Direct Git runtime contracts passed.'
