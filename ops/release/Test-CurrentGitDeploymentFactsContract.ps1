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

if ($env:OS -ne 'Windows_NT') {
    throw 'Current deployment fact contracts require Windows.'
}
if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    $RepositoryRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
$RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$toolPath = Join-Path $PSScriptRoot 'Get-CurrentGitDeploymentFacts.ps1'
Assert-Contract `
    -Condition (Test-Path -LiteralPath $toolPath -PathType Leaf) `
    -Message "Deployment fact tool is missing: $toolPath"

$source = [System.IO.File]::ReadAllText($toolPath)
[void][scriptblock]::Create($source)
$nonAscii = @($source.ToCharArray() | Where-Object { [int]$_ -gt 127 })
Assert-Contract `
    -Condition ($nonAscii.Count -eq 0) `
    -Message 'Deployment fact tool must remain ASCII-compatible with Windows PowerShell 5.1.'

foreach ($forbiddenPattern in @(
    '(?i)\bStart-Service\b',
    '(?i)\bStop-Service\b',
    '(?i)\bRestart-Service\b',
    '(?i)\bSet-Service\b',
    '(?i)\bRemove-Item\b',
    '(?i)\bgit(?:\.exe)?\s+(?:pull|checkout|switch|reset|clean|stash)\b',
    '(?i)manage\.py\s+(?:migrate|collectstatic|changepassword)',
    '(?i)\bpip\s+install\b',
    '(?i)\bnpm(?:\.cmd)?\s+(?:install|ci|run)\b'
)) {
    Assert-Contract `
        -Condition (-not [regex]::IsMatch($source, $forbiddenPattern)) `
        -Message "Deployment fact tool contains a forbidden mutation pattern: $forbiddenPattern"
}

foreach ($requiredText in @(
    'read_only_observation = $true',
    'cutover_authorized = $false',
    'secrets_collected = $false',
    'environment_file_contents_collected = $false',
    'database_contents_opened = $false',
    'database_sha256_computed = $false',
    'config_contents_collected = $false',
    '[System.IO.FileMode]::CreateNew',
    'Protect-CommandLine'
)) {
    Assert-Contract `
        -Condition $source.Contains($requiredText) `
        -Message "Deployment fact tool is missing required safety text: $requiredText"
}

$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    'ffxivshare-deployment-facts-contract-' + [Guid]::NewGuid().ToString('N')
)
[void][System.IO.Directory]::CreateDirectory($temporaryRoot)
$outputPath = Join-Path $temporaryRoot 'facts.json'
try {
    $consoleOutput = & $toolPath `
        -RepositoryRoot $RepositoryRoot `
        -OutputPath $outputPath
    Assert-Contract `
        -Condition (Test-Path -LiteralPath $outputPath -PathType Leaf) `
        -Message 'Deployment fact tool did not create its report.'

    $summary = $consoleOutput | ConvertFrom-Json
    $report = Get-Content -LiteralPath $outputPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-Contract ($summary.status -eq 'captured') 'Console summary status is invalid.'
    Assert-Contract ($summary.cutover_authorized -eq $false) 'Console summary authorized cutover.'
    Assert-Contract ($report.status -eq 'captured') 'Report status is invalid.'
    Assert-Contract ($report.read_only_observation -eq $true) 'Report is not read-only.'
    Assert-Contract ($report.cutover_authorized -eq $false) 'Report authorized cutover.'
    Assert-Contract ($report.secrets_collected -eq $false) 'Report claims to collect secrets.'
    Assert-Contract ($report.repository.head -match '^[a-f0-9]{40}$') 'Git HEAD is invalid.'
    Assert-Contract `
        ($report.repository.root -eq $RepositoryRoot) `
        'Repository root was not preserved.'
    Assert-Contract `
        ($report.runtime.environment_file_contents_collected -eq $false) `
        'Environment-file contents were marked as collected.'
    Assert-Contract `
        ($report.data.database_contents_opened -eq $false) `
        'Database contents were marked as opened.'

    $duplicateRejected = $false
    try {
        & $toolPath -RepositoryRoot $RepositoryRoot -OutputPath $outputPath | Out-Null
    }
    catch {
        $duplicateRejected = $_.Exception.Message -like '*already exists*'
    }
    Assert-Contract $duplicateRejected 'Existing output was not rejected.'
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}

Write-Host 'Current Git deployment fact contracts passed.'
