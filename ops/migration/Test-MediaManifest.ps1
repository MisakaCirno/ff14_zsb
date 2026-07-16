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

    & $PythonExecutable -B @Arguments
    $exitCode = $LASTEXITCODE
    Assert-Contract `
        -Condition ($exitCode -eq $ExpectedExitCode) `
        -Message "Python exited with $exitCode; expected $ExpectedExitCode."
}

function Remove-DirectoryTreeWithoutFollowingReparse {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $root = [System.IO.DirectoryInfo]::new(
        [System.IO.Path]::GetFullPath($Path)
    )
    if (-not $root.Exists) {
        return
    }
    $root.Refresh()
    if (($root.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Refusing to recursively clean a reparse-point root.'
    }

    $pending = [System.Collections.Stack]::new()
    $directories = [System.Collections.ArrayList]::new()
    $pending.Push($root)
    while ($pending.Count -gt 0) {
        $directory = [System.IO.DirectoryInfo]$pending.Pop()
        $directory.Refresh()
        if (($directory.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            $directory.Delete()
            continue
        }
        [void]$directories.Add($directory)
        foreach ($entry in $directory.GetFileSystemInfos()) {
            $entry.Refresh()
            if (($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                $entry.Delete()
            }
            elseif ($entry -is [System.IO.DirectoryInfo]) {
                $pending.Push($entry)
            }
            else {
                $entry.Attributes = [System.IO.FileAttributes]::Normal
                $entry.Delete()
            }
        }
    }

    $orderedDirectories = @($directories | Sort-Object {
        $_.FullName.Length
    } -Descending)
    foreach ($directory in $orderedDirectories) {
        $directory.Refresh()
        if (-not $directory.Exists) {
            continue
        }
        if (($directory.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            if ($directory.FullName -eq $root.FullName) {
                throw 'Test root became a reparse point during cleanup.'
            }
            $directory.Delete()
            continue
        }
        $directory.Attributes = [System.IO.FileAttributes]::Directory
        $directory.Delete()
    }
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
$unconfirmedManifest = Join-Path $temporaryRoot 'unconfirmed-manifest.json'
$mutationRoot = Join-Path $temporaryRoot 'mutation-media'
$mutationScript = Join-Path $temporaryRoot 'test-tree-mutation.py'

try {
    [void](New-Item -ItemType Directory -Path $sourceRoot)
    [void](New-Item -ItemType Directory -Path $targetRoot)
    [void](New-Item -ItemType Directory -Path $emptyRoot)
    [void](New-Item -ItemType Directory -Path $mutationRoot)
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
        -Arguments @(
            $scriptPath,
            'build',
            '--root',
            $sourceRoot,
            '--output',
            $sourceManifest,
            '--snapshot-id',
            'source-contract-snapshot',
            '--confirm-offline-snapshot'
        ) `
        -ExpectedExitCode 0
    Invoke-Python `
        -Arguments @(
            $scriptPath,
            'build',
            '--root',
            $targetRoot,
            '--output',
            $targetManifest,
            '--snapshot-id',
            'target-contract-snapshot',
            '--confirm-offline-snapshot'
        ) `
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
        -Arguments @(
            $scriptPath,
            'build',
            '--root',
            $targetRoot,
            '--output',
            $changedManifest,
            '--snapshot-id',
            'changed-contract-snapshot',
            '--confirm-offline-snapshot'
        ) `
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
        -Arguments @(
            $scriptPath,
            'build',
            '--root',
            $sourceRoot,
            '--output',
            $sourceManifest,
            '--snapshot-id',
            'overwrite-contract-snapshot',
            '--confirm-offline-snapshot'
        ) `
        -ExpectedExitCode 1
    Assert-Contract `
        -Condition (-not (Test-Path -LiteralPath (Join-Path $sourceRoot 'manifest.json'))) `
        -Message 'The media root was modified by the manifest tool.'

    Invoke-Python `
        -Arguments @(
            $scriptPath,
            'build',
            '--root',
            $emptyRoot,
            '--output',
            $emptyManifest,
            '--snapshot-id',
            'empty-contract-snapshot',
            '--confirm-offline-snapshot'
        ) `
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
            $unconfirmedManifest,
            '--snapshot-id',
            'unconfirmed-contract-snapshot'
        ) `
        -ExpectedExitCode 1
    Assert-Contract `
        -Condition (-not (Test-Path -LiteralPath $unconfirmedManifest)) `
        -Message 'An unconfirmed media tree produced a manifest.'

    Invoke-Python `
        -Arguments @(
            $scriptPath,
            'build',
            '--root',
            $emptyRoot,
            '--output',
            (Join-Path $emptyRoot 'forbidden.json'),
            '--snapshot-id',
            'forbidden-contract-snapshot',
            '--confirm-offline-snapshot'
        ) `
        -ExpectedExitCode 1
    Assert-Contract `
        -Condition (-not (Test-Path -LiteralPath (Join-Path $emptyRoot 'forbidden.json'))) `
        -Message 'Manifest output was written inside the media root.'

    [System.IO.File]::WriteAllText(
        (Join-Path $mutationRoot 'seed.txt'),
        'seed',
        [System.Text.UTF8Encoding]::new($false)
    )
    $mutationSource = @'
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
import MediaManifest as media_manifest

root = Path(sys.argv[2])
assert media_manifest._canonical_path_key("\u017f\u0301") == (
    media_manifest._canonical_path_key("\u015a")
)
original_hash = media_manifest._file_sha256
mutated = False


def hash_and_mutate(path):
    global mutated
    result = original_hash(path)
    if not mutated:
        (root / "appeared.txt").write_text("late", encoding="utf-8")
        mutated = True
    return result


media_manifest._file_sha256 = hash_and_mutate
try:
    media_manifest.build_manifest(root, snapshot_id="mutation-contract-snapshot")
except media_manifest.MediaManifestError as exc:
    if "tree changed" not in str(exc):
        raise
else:
    raise SystemExit("Tree mutation was not rejected.")
'@
    [System.IO.File]::WriteAllText(
        $mutationScript,
        $mutationSource,
        [System.Text.UTF8Encoding]::new($false)
    )
    Invoke-Python `
        -Arguments @($mutationScript, $PSScriptRoot, $mutationRoot) `
        -ExpectedExitCode 0

    if ($env:OS -eq 'Windows_NT') {
        $cleanupProbe = Join-Path $temporaryRoot 'cleanup-probe'
        $cleanupTarget = Join-Path $temporaryRoot 'cleanup-target'
        $cleanupMarker = Join-Path $cleanupTarget 'must-survive.txt'
        [void](New-Item -ItemType Directory -Path $cleanupProbe)
        [void](New-Item -ItemType Directory -Path $cleanupTarget)
        [System.IO.File]::WriteAllText($cleanupMarker, 'sentinel')
        [void](New-Item `
            -ItemType Junction `
            -Path (Join-Path $cleanupProbe 'linked-target') `
            -Target $cleanupTarget)

        Remove-DirectoryTreeWithoutFollowingReparse -Path $cleanupProbe
        Assert-Contract `
            -Condition (Test-Path -LiteralPath $cleanupMarker -PathType Leaf) `
            -Message 'Safe cleanup followed a junction and deleted its target.'
    }

    Write-Host 'Media manifest contracts passed.'
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        $resolvedRoot = (Resolve-Path -LiteralPath $temporaryRoot).Path
        $resolvedBase = (Resolve-Path -LiteralPath $temporaryBase).Path.TrimEnd('\', '/')
        $requiredPrefix = $resolvedBase + [System.IO.Path]::DirectorySeparatorChar
        Assert-Contract `
            -Condition (
                $resolvedRoot.StartsWith(
                    $requiredPrefix,
                    [StringComparison]::OrdinalIgnoreCase
                )
            ) `
            -Message 'Refusing to clean outside the system temporary directory.'
        Assert-Contract `
            -Condition (
                (Split-Path -Leaf $resolvedRoot) -match `
                    '^ffxivshare-media-contract-[a-f0-9]{32}$'
            ) `
            -Message 'Refusing to clean an unexpectedly named test directory.'
        Remove-DirectoryTreeWithoutFollowingReparse -Path $resolvedRoot
    }
}
