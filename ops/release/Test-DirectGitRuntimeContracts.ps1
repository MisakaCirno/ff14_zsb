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
$startPath = Join-Path $RepositoryRoot 'ops\release\Start-DirectGitWaitress.bat'
$nginxPath = Join-Path $RepositoryRoot 'ops\nginx\ffxivshare.direct-git.locations.conf.example'

foreach ($path in @($startPath, $nginxPath)) {
    Assert-Contract `
        -Condition (Test-Path -LiteralPath $path -PathType Leaf) `
        -Message "Direct Git runtime file is missing: $path"
    $source = [System.IO.File]::ReadAllText($path)
    $nonAscii = @($source.ToCharArray() | Where-Object { [int]$_ -gt 127 })
    Assert-Contract `
        -Condition ($nonAscii.Count -eq 0) `
        -Message "Direct Git runtime file must remain ASCII-compatible: $path"
}

$startSource = [System.IO.File]::ReadAllText($startPath)
foreach ($requiredText in @(
    'venv\Scripts\python.exe',
    '"%PYTHON%" -B -m waitress',
    '--listen=127.0.0.1:8000',
    '--threads=4',
    '--trusted-proxy=127.0.0.1',
    '"--trusted-proxy-headers=x-forwarded-for x-forwarded-proto"',
    '--clear-untrusted-proxy-headers',
    '--no-expose-tracebacks',
    'ffxivshare.wsgi:application'
)) {
    Assert-Contract `
        -Condition $startSource.Contains($requiredText) `
        -Message "Direct Git start script is missing: $requiredText"
}

foreach ($forbiddenPattern in @(
    '(?i)\bactivate(?:\.bat)?\b',
    '(?i)manage\.py\s+migrate',
    '(?i)manage\.py\s+collectstatic',
    '(?i)\bgit\s+(?:pull|fetch|checkout|switch|reset|clean|stash)',
    '(?i)\bpip\s+install',
    '(?i)\bnpm(?:\.cmd)?\s+(?:install|ci|run)'
)) {
    Assert-Contract `
        -Condition (-not [regex]::IsMatch($startSource, $forbiddenPattern)) `
        -Message "Direct Git start script contains a forbidden mutation: $forbiddenPattern"
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
