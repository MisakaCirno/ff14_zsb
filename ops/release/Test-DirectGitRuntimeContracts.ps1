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
$startPath = Join-Path $RepositoryRoot 'ops\release\Start-DirectGitWaitress.bat'
$launcherPath = Join-Path $RepositoryRoot 'ops\release\Invoke-DirectGitLauncher.ps1'
$upgradePath = Join-Path $RepositoryRoot 'ops\release\Invoke-DirectGitDatabaseUpgrade.ps1'
$nginxPath = Join-Path $RepositoryRoot 'ops\nginx\ffxivshare.direct-git.locations.conf.example'

foreach ($path in @($rootStartPath, $startPath, $launcherPath, $upgradePath, $nginxPath)) {
    Assert-Contract `
        -Condition (Test-Path -LiteralPath $path -PathType Leaf) `
        -Message "Direct Git runtime file is missing: $path"
    $source = [System.IO.File]::ReadAllText($path)
    $nonAscii = @($source.ToCharArray() | Where-Object { [int]$_ -gt 127 })
    Assert-Contract `
        -Condition ($nonAscii.Count -eq 0) `
        -Message "Direct Git runtime file must remain ASCII-compatible: $path"
}

$rootStartSource = [System.IO.File]::ReadAllText($rootStartPath)
Assert-Contract `
    -Condition $rootStartSource.Contains('ops\release\Start-DirectGitWaitress.bat') `
    -Message 'Root start script does not delegate to the unified launcher.'

$startSource = [System.IO.File]::ReadAllText($startPath)
foreach ($requiredText in @(
    'Invoke-DirectGitLauncher.ps1',
    'powershell.exe -NoProfile -ExecutionPolicy Bypass',
    '-RepositoryRoot "%PROJECT_DIR%"'
)) {
    Assert-Contract `
        -Condition $startSource.Contains($requiredText) `
        -Message "Direct Git start script is missing: $requiredText"
}

$launcherSource = [System.IO.File]::ReadAllText($launcherPath)
foreach ($requiredText in @(
    'manage.py check_deployment_schema',
    'Invoke-DirectGitDatabaseUpgrade.ps1',
    '[1] Create a verified backup, upgrade safely, and start',
    '[2] Do not upgrade; keep the application stopped (default)',
    'Read-Host',
    '-Confirm:$false',
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
            -not [regex]::IsMatch($startSource, $forbiddenPattern) -and
            -not [regex]::IsMatch($launcherSource, $forbiddenPattern)
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
