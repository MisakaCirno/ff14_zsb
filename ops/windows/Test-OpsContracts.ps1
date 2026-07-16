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

$requirementsPath = Join-Path $RepositoryRoot 'requirements.txt'
$smokePath = Join-Path $RepositoryRoot 'ops\windows\Test-WaitressSmoke.ps1'
$contractPath = $MyInvocation.MyCommand.Path

foreach ($path in @($requirementsPath, $smokePath, $contractPath)) {
    Assert-Contract `
        -Condition (Test-Path -LiteralPath $path -PathType Leaf) `
        -Message "Required operations file is missing: $path"
}

$allRequirementLines = @(Get-Content -LiteralPath $requirementsPath)
$waitressRequirementLines = @($allRequirementLines | Where-Object {
    $_.Trim() -match '^waitress\s*[=<>!~]'
})
Assert-Contract `
    -Condition (
        $waitressRequirementLines.Count -eq 1 -and
        $waitressRequirementLines[0].Trim() -eq 'waitress==3.0.2'
    ) `
    -Message 'requirements.txt must pin waitress==3.0.2 exactly once.'

$smokeSource = [System.IO.File]::ReadAllText($smokePath)
$contractSource = [System.IO.File]::ReadAllText($contractPath)

foreach ($entry in @(
    @{ Text = '--listen=127.0.0.1:$port'; Name = 'loopback listen argument' },
    @{ Text = '--threads=4'; Name = 'bounded thread count' },
    @{ Text = '--trusted-proxy=127.0.0.1'; Name = 'loopback trusted proxy' },
    @{ Text = '--trusted-proxy-headers="x-forwarded-for x-forwarded-proto"'; Name = 'trusted proxy header allowlist' },
    @{ Text = '--clear-untrusted-proxy-headers'; Name = 'untrusted proxy header clearing' },
    @{ Text = '--no-expose-tracebacks'; Name = 'traceback exposure disabled' },
    @{ Text = 'DATABASE_PATH'; Name = 'temporary database override' },
    @{ Text = 'Get-FreeLoopbackPort'; Name = 'random loopback port allocation' },
    @{ Text = 'netstat.exe -ano -p tcp'; Name = 'non-admin listener inspection' },
    @{ Text = 'Get-Command'; Name = 'CI PATH executable fallback' },
    @{ Text = "'-m'"; Name = 'Python module execution flag' },
    @{ Text = "'waitress'"; Name = 'Waitress module execution' },
    @{ Text = 'X-Request-ID'; Name = 'request ID validation' },
    @{ Text = 'Stop-TestProcess'; Name = 'process cleanup' },
    @{ Text = 'Remove-TestDirectory -Path $resolvedTemporaryRoot'; Name = 'temporary directory cleanup' }
)) {
    Assert-Contract `
        -Condition $smokeSource.Contains($entry.Text) `
        -Message "Waitress smoke test is missing $($entry.Name)."
}

$listenArguments = @([regex]::Matches($smokeSource, '(?im)--listen\s*='))
Assert-Contract `
    -Condition ($listenArguments.Count -eq 1) `
    -Message 'Waitress smoke test must define exactly one listen argument.'

$unsafeListenPattern = '(?im)--(?:listen|host)\s*=\s*(?:0\.0\.0\.0|\[::\]|\*)'
Assert-Contract `
    -Condition (-not [regex]::IsMatch($smokeSource, $unsafeListenPattern)) `
    -Message 'Waitress smoke test contains a non-loopback listen address.'

foreach ($path in @($smokePath, $contractPath)) {
    $source = [System.IO.File]::ReadAllText($path)
    [void][scriptblock]::Create($source)
    $nonAscii = @($source.ToCharArray() | Where-Object { [int]$_ -gt 127 })
    Assert-Contract `
        -Condition ($nonAscii.Count -eq 0) `
        -Message "PowerShell operations script must remain ASCII-compatible: $path"
}

Assert-Contract `
    -Condition (-not [regex]::IsMatch($contractSource, '(?i)SECRET_KEY\s*=')) `
    -Message 'Static contract script must not contain a secret value.'

Write-Host 'Windows Waitress operations contracts passed.'
