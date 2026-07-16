[CmdletBinding()]
param(
    [string]$PythonExecutable = 'python'
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

function Invoke-Python {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [int]$ExpectedExitCode
    )

    & $PythonExecutable @Arguments
    $exitCode = $LASTEXITCODE
    Assert-Contract `
        -Condition ($exitCode -eq $ExpectedExitCode) `
        -Message "Python exited with $exitCode; expected $ExpectedExitCode."
}

$scriptPath = Join-Path $PSScriptRoot 'MediaManifest.py'
$temporaryBase = [System.IO.Path]::GetTempPath()
$temporaryRoot = Join-Path `
    $temporaryBase `
    ("ffxivshare-media-contract-" + [Guid]::NewGuid().ToString('N'))
$sourceRoot = Join-Path $temporaryRoot 'source-media'
$targetRoot = Join-Path $temporaryRoot 'target-media'
$sourceManifest = Join-Path $temporaryRoot 'source-manifest.json'
$targetManifest = Join-Path $temporaryRoot 'target-manifest.json'
$matchReport = Join-Path $temporaryRoot 'match.json'
$changedManifest = Join-Path $temporaryRoot 'changed-manifest.json'
$mismatchReport = Join-Path $temporaryRoot 'mismatch.json'
$emptyRoot = Join-Path $temporaryRoot 'empty-media'
$emptyManifest = Join-Path $temporaryRoot 'empty-manifest.json'

try {
    [void](New-Item -ItemType Directory -Path $sourceRoot)
    [void](New-Item -ItemType Directory -Path $targetRoot)
    [void](New-Item -ItemType Directory -Path $emptyRoot)
    [void](New-Item -ItemType Directory -Path (Join-Path $sourceRoot 'nested'))
    [void](New-Item -ItemType Directory -Path (Join-Path $targetRoot 'nested'))
    [System.IO.File]::WriteAllText(
        (Join-Path $sourceRoot 'nested\alpha.txt'),
        "alpha`r`nbeta",
        [System.Text.UTF8Encoding]::new($false)
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $targetRoot 'nested\alpha.txt'),
        "alpha`r`nbeta",
        [System.Text.UTF8Encoding]::new($false)
    )
    [System.IO.File]::WriteAllBytes(
        (Join-Path $sourceRoot 'payload.bin'),
        [byte[]](0, 1, 2, 254, 255)
    )
    [System.IO.File]::WriteAllBytes(
        (Join-Path $targetRoot 'payload.bin'),
        [byte[]](0, 1, 2, 254, 255)
    )

    Invoke-Python `
        -Arguments @($scriptPath, 'build', '--root', $sourceRoot, '--output', $sourceManifest) `
        -ExpectedExitCode 0
    Invoke-Python `
        -Arguments @($scriptPath, 'build', '--root', $targetRoot, '--output', $targetManifest) `
        -ExpectedExitCode 0
    Invoke-Python `
        -Arguments @(
            $scriptPath,
            'compare',
            '--source',
            $sourceManifest,
            '--target',
            $targetManifest,
            '--output',
            $matchReport
        ) `
        -ExpectedExitCode 0

    $source = Get-Content -LiteralPath $sourceManifest -Raw | ConvertFrom-Json
    $match = Get-Content -LiteralPath $matchReport -Raw | ConvertFrom-Json
    Assert-Contract -Condition ($source.file_count -eq 2) -Message 'Expected two files.'
    Assert-Contract -Condition ([bool]$match.matched) -Message 'Equal media must match.'

    [System.IO.File]::AppendAllText(
        (Join-Path $targetRoot 'nested\alpha.txt'),
        'changed',
        [System.Text.UTF8Encoding]::new($false)
    )
    Invoke-Python `
        -Arguments @($scriptPath, 'build', '--root', $targetRoot, '--output', $changedManifest) `
        -ExpectedExitCode 0
    Invoke-Python `
        -Arguments @(
            $scriptPath,
            'compare',
            '--source',
            $sourceManifest,
            '--target',
            $changedManifest,
            '--output',
            $mismatchReport
        ) `
        -ExpectedExitCode 2
    $mismatch = Get-Content -LiteralPath $mismatchReport -Raw | ConvertFrom-Json
    Assert-Contract -Condition (-not [bool]$mismatch.matched) -Message 'Changed media matched.'
    Assert-Contract `
        -Condition ($mismatch.changed_paths -contains 'nested/alpha.txt') `
        -Message 'Changed path was not reported.'

    Invoke-Python `
        -Arguments @($scriptPath, 'build', '--root', $sourceRoot, '--output', $sourceManifest) `
        -ExpectedExitCode 1
    Assert-Contract `
        -Condition (-not (Test-Path -LiteralPath (Join-Path $sourceRoot 'manifest.json'))) `
        -Message 'The media root was modified by the manifest tool.'

    Invoke-Python `
        -Arguments @($scriptPath, 'build', '--root', $emptyRoot, '--output', $emptyManifest) `
        -ExpectedExitCode 0
    $empty = Get-Content -LiteralPath $emptyManifest -Raw | ConvertFrom-Json
    Assert-Contract -Condition ($empty.file_count -eq 0) -Message 'Empty file count changed.'
    Assert-Contract -Condition ($empty.total_size -eq 0) -Message 'Empty byte count changed.'

    Invoke-Python `
        -Arguments @(
            $scriptPath,
            'build',
            '--root',
            $emptyRoot,
            '--output',
            (Join-Path $emptyRoot 'forbidden.json')
        ) `
        -ExpectedExitCode 1
    Assert-Contract `
        -Condition (-not (Test-Path -LiteralPath (Join-Path $emptyRoot 'forbidden.json'))) `
        -Message 'Manifest output was written inside the media root.'

    Write-Host 'Media manifest contracts passed.'
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        $resolvedRoot = (Resolve-Path -LiteralPath $temporaryRoot).Path
        $resolvedBase = (Resolve-Path -LiteralPath $temporaryBase).Path
        Assert-Contract `
            -Condition ($resolvedRoot.StartsWith($resolvedBase, [StringComparison]::OrdinalIgnoreCase)) `
            -Message 'Refusing to clean outside the system temporary directory.'
        Remove-Item -LiteralPath $resolvedRoot -Recurse -Force
    }
}
