[CmdletBinding()]
param(
    [string]$RepositoryRoot = '',
    [string]$PythonExecutable = ''
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

function Remove-TestDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $temporaryBase = [System.IO.Path]::GetFullPath(
        [System.IO.Path]::GetTempPath()
    ).TrimEnd('\', '/')
    $resolvedPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    $requiredPrefix = $temporaryBase + [System.IO.Path]::DirectorySeparatorChar
    Assert-Contract `
        -Condition $resolvedPath.StartsWith(
            $requiredPrefix,
            [System.StringComparison]::OrdinalIgnoreCase
        ) `
        -Message 'Refusing to remove a test directory outside the system temp directory.'
    Assert-Contract `
        -Condition (
            (Split-Path -Leaf $resolvedPath) -match `
                '^ffxivshare-backup-set-[a-f0-9]{32}$'
        ) `
        -Message 'Refusing to remove a test directory with an unexpected name.'
    if (Test-Path -LiteralPath $resolvedPath) {
        Remove-Item -LiteralPath $resolvedPath -Recurse -Force
    }
}

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Invoke-Verifier {
    param(
        [Parameter(Mandatory = $true)][string]$Database,
        [Parameter(Mandatory = $true)][string]$Checksum,
        [Parameter(Mandatory = $true)][string]$Metadata,
        [Parameter(Mandatory = $true)][string]$Output,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][int]$ExpectedExitCode
    )

    $logPath = Join-Path $logRoot "$Name.log"
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $PythonExecutable -B $verifierPath `
            --database $Database `
            --checksum $Checksum `
            --metadata $Metadata `
            --output $Output *> $logPath
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }

    # Expected negative cases must not leak their native exit code to verify.ps1.
    $global:LASTEXITCODE = 0
    Assert-Contract `
        -Condition ($exitCode -eq $ExpectedExitCode) `
        -Message "Verifier exited with $exitCode; expected $ExpectedExitCode for $Name."
}

function New-BackupSet {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [string]$Variant = 'valid',
        [string]$DatabaseName = 'backup.sqlite3'
    )

    $inputRoot = Join-Path $temporaryRoot "input-$Name"
    Invoke-NativeChecked `
        -FilePath $PythonExecutable `
        -Arguments @('-B', $fixtureScript, $inputRoot, $DatabaseName, $Variant) `
        -Description "Backup-set fixture creation ($Name)"
    return [pscustomobject]@{
        Root = $inputRoot
        Database = Join-Path $inputRoot $DatabaseName
        Checksum = Join-Path $inputRoot "$DatabaseName.sha256"
        Metadata = Join-Path $inputRoot "$DatabaseName.metadata.json"
    }
}

if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    $RepositoryRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
$RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path

if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $venvPython = Join-Path $RepositoryRoot 'venv\Scripts\python.exe'
    $PythonExecutable = if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $venvPython
    }
    else {
        'python'
    }
}
elseif (Test-Path -LiteralPath $PythonExecutable -PathType Leaf) {
    $PythonExecutable = (Resolve-Path -LiteralPath $PythonExecutable).Path
}

$verifierPath = Join-Path `
    $RepositoryRoot `
    'ops\migration\Verify-SQLiteBackupSet.py'
Assert-Contract `
    -Condition (Test-Path -LiteralPath $verifierPath -PathType Leaf) `
    -Message "SQLite backup-set verifier is missing: $verifierPath"

$verifierSource = [System.IO.File]::ReadAllText($verifierPath)
foreach ($forbiddenText in @(
    'import django',
    'from django',
    'import sqlite3',
    'Inspect-SQLiteSnapshot'
)) {
    Assert-Contract `
        -Condition (-not $verifierSource.Contains($forbiddenText)) `
        -Message "Verifier contains a forbidden dependency: $forbiddenText"
}

$temporaryRoot = Join-Path `
    ([System.IO.Path]::GetTempPath()) `
    ('ffxivshare-backup-set-' + [Guid]::NewGuid().ToString('N'))
$fixtureScript = Join-Path $temporaryRoot 'create_backup_set.py'
$mutationProbeScript = Join-Path $temporaryRoot 'mutation_probe.py'
$reportRoot = Join-Path $temporaryRoot 'reports'
$logRoot = Join-Path $temporaryRoot 'logs'

$fixtureSource = @'
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys


root = Path(sys.argv[1])
database_name = sys.argv[2]
variant = sys.argv[3]
root.mkdir(parents=True)
database = root / database_name

if variant == "bad-magic":
    database.write_bytes(b"not-a-sqlite-database\x00business-secret")
else:
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO records(value) VALUES (?)", ("business-secret",)
        )
        connection.commit()
    finally:
        connection.close()

payload = database.read_bytes()
digest = sha256(payload).hexdigest()
metadata = {
    "schema_version": 1,
    "generated_at": "2026-07-16T00:00:00Z",
    "backup_method": "sqlite_backup_api",
    "database_vendor": "sqlite",
    "application_version": "backup-contract-release",
    "sha256": digest,
    "size": len(payload),
    "integrity_check": "ok",
    "foreign_key_check": "ok",
}

if variant == "nan":
    metadata["size"] = float("nan")
elif variant == "extra-key":
    metadata["unexpected"] = True
elif variant == "non-utc":
    metadata["generated_at"] = "2026-07-16T08:00:00+08:00"
elif variant == "non-string-version":
    metadata["application_version"] = ["invalid"]
elif variant == "wrong-integrity":
    metadata["integrity_check"] = "not-ok"
elif variant == "wrong-foreign-key":
    metadata["foreign_key_check"] = "not-ok"
elif variant == "wrong-size":
    metadata["size"] = len(payload) + 1
elif variant == "fractional-time":
    metadata["generated_at"] = "2026-07-16T00:00:00.123456Z"
elif variant == "space-timestamp":
    metadata["generated_at"] = "2026-07-16 00:00:00Z"
elif variant == "basic-timestamp":
    metadata["generated_at"] = "20260716T000000Z"
elif variant == "week-timestamp":
    metadata["generated_at"] = "2026-W29-4T00:00:00Z"
elif variant == "comma-timestamp":
    metadata["generated_at"] = "2026-07-16T00:00:00,123456Z"
elif variant == "short-fraction-timestamp":
    metadata["generated_at"] = "2026-07-16T00:00:00.1Z"
elif variant == "invalid-date-timestamp":
    metadata["generated_at"] = "2026-02-30T00:00:00Z"

checksum_digest = digest.upper() if variant == "uppercase-checksum" else digest
checksum_separator = " " if variant == "single-space-checksum" else "  "
checksum_ending = "\r\n" if variant == "crlf-checksum" else "\n"
(root / f"{database_name}.sha256").write_bytes(
    (
        f"{checksum_digest}{checksum_separator}{database_name}"
        f"{checksum_ending}"
    ).encode("utf-8")
)

if variant == "duplicate-key":
    base = json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True)
    metadata_text = (
        base[:-1] + f',\n  "sha256": {json.dumps(digest)}\n}}\n'
    )
else:
    metadata_text = json.dumps(
        metadata,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
(root / f"{database_name}.metadata.json").write_text(
    metadata_text,
    encoding="utf-8",
    newline="\n",
)
'@

$mutationProbeSource = @'
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import sys


verifier_path = Path(sys.argv[1])
database = Path(sys.argv[2])
checksum = Path(sys.argv[3])
metadata = Path(sys.argv[4])
output = Path(sys.argv[5])

spec = importlib.util.spec_from_file_location("backup_set_verifier", verifier_path)
if spec is None or spec.loader is None:
    raise SystemExit("Verifier module could not be loaded.")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)

original_hash = verifier._hash_stable_database
hash_calls = 0


def hash_then_mutate(path):
    global hash_calls
    result = original_hash(path)
    hash_calls += 1
    if hash_calls == 1:
        before = path.stat()
        position = min(512, before.st_size - 1)
        if position < len(verifier.SQLITE_MAGIC):
            raise SystemExit("Mutation fixture is unexpectedly small.")
        with path.open("r+b") as stream:
            stream.seek(position)
            original = stream.read(1)
            stream.seek(position)
            stream.write(bytes((original[0] ^ 0x01,)))
            stream.flush()
            os.fsync(stream.fileno())
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    return result


verifier._hash_stable_database = hash_then_mutate
arguments = argparse.Namespace(
    database=str(database),
    checksum=str(checksum),
    metadata=str(metadata),
    output=str(output),
)
try:
    verifier.verify_backup_set(arguments)
except verifier.VerificationError:
    pass
else:
    raise SystemExit("Mid-verification mutation was not rejected.")

if hash_calls != 2:
    raise SystemExit("Verifier did not perform two database hash passes.")
if output.exists():
    raise SystemExit("Mutation failure published a report.")
'@

try {
    [void](New-Item -ItemType Directory -Path $temporaryRoot)
    [void](New-Item -ItemType Directory -Path $reportRoot)
    [void](New-Item -ItemType Directory -Path $logRoot)
    $utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText(
        $fixtureScript,
        $fixtureSource,
        $utf8WithoutBom
    )
    [System.IO.File]::WriteAllText(
        $mutationProbeScript,
        $mutationProbeSource,
        $utf8WithoutBom
    )

    $valid = New-BackupSet -Name 'valid'
    $validHashBefore = (
        Get-FileHash -LiteralPath $valid.Database -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    $successOutput = Join-Path $reportRoot 'success.json'
    Invoke-Verifier `
        -Database $valid.Database `
        -Checksum $valid.Checksum `
        -Metadata $valid.Metadata `
        -Output $successOutput `
        -Name 'success' `
        -ExpectedExitCode 0

    Assert-Contract `
        -Condition (Test-Path -LiteralPath $successOutput -PathType Leaf) `
        -Message 'Valid backup set did not publish a verification report.'
    $reportText = Get-Content -LiteralPath $successOutput -Raw
    $report = $reportText | ConvertFrom-Json
    $expectedTopLevelKeys = @(
        'artifact',
        'checks',
        'cutover_authorized',
        'format',
        'format_version',
        'generated_at',
        'inspection_required',
        'verified'
    )
    $actualTopLevelKeys = @($report.PSObject.Properties.Name | Sort-Object)
    Assert-Contract `
        -Condition (
            ($actualTopLevelKeys -join ',') -eq ($expectedTopLevelKeys -join ',')
        ) `
        -Message 'Verification report top-level keys changed.'
    Assert-Contract `
        -Condition (
            $report.format -eq 'ffxivshare-sqlite-backup-set-verification' -and
            $report.format_version -eq 1
        ) `
        -Message 'Verification report format changed.'
    Assert-Contract `
        -Condition ([bool]$report.verified) `
        -Message 'Verification report did not set verified=true.'
    Assert-Contract `
        -Condition (-not [bool]$report.cutover_authorized) `
        -Message 'Verification report incorrectly authorized cutover.'
    Assert-Contract `
        -Condition ([bool]$report.inspection_required) `
        -Message 'Verification report did not require full snapshot inspection.'
    Assert-Contract `
        -Condition ($report.artifact.sha256 -eq $validHashBefore) `
        -Message 'Verification report hash does not match the database.'
    Assert-Contract `
        -Condition (
            $report.artifact.size -eq (
                Get-Item -LiteralPath $valid.Database
            ).Length
        ) `
        -Message 'Verification report size does not match the database.'
    $expectedArtifactKeys = @('producer_generated_at', 'sha256', 'size')
    $actualArtifactKeys = @($report.artifact.PSObject.Properties.Name | Sort-Object)
    Assert-Contract `
        -Condition (
            ($actualArtifactKeys -join ',') -eq ($expectedArtifactKeys -join ',')
        ) `
        -Message 'Verification report artifact fields may expose source content.'
    Assert-Contract `
        -Condition (
            [bool]$report.checks.checksum_bytes_exact -and
            [bool]$report.checks.input_set_unchanged -and
            [bool]$report.checks.metadata_contract -and
            [bool]$report.checks.sqlite_magic
        ) `
        -Message 'Verification report checks are incomplete.'
    Assert-Contract `
        -Condition (-not $reportText.Contains($temporaryRoot)) `
        -Message 'Verification report exposed an absolute test path.'
    Assert-Contract `
        -Condition (-not $reportText.Contains('business-secret')) `
        -Message 'Verification report exposed database business content.'
    Assert-Contract `
        -Condition (
            -not $reportText.Contains('"integrity_check"') -and
            -not $reportText.Contains('"foreign_key_check"')
        ) `
        -Message 'Backup-set evidence was presented as a full SQLite inspection.'

    $validHashAfter = (
        Get-FileHash -LiteralPath $valid.Database -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    Assert-Contract `
        -Condition ($validHashAfter -eq $validHashBefore) `
        -Message 'Verifier modified the valid database.'

    $fractionalTime = New-BackupSet `
        -Name 'fractional-time' `
        -Variant 'fractional-time'
    $fractionalTimeOutput = Join-Path $reportRoot 'fractional-time.json'
    Invoke-Verifier `
        -Database $fractionalTime.Database `
        -Checksum $fractionalTime.Checksum `
        -Metadata $fractionalTime.Metadata `
        -Output $fractionalTimeOutput `
        -Name 'fractional-time' `
        -ExpectedExitCode 0
    Assert-Contract `
        -Condition (Test-Path -LiteralPath $fractionalTimeOutput -PathType Leaf) `
        -Message 'Producer timestamp with six fractional digits was rejected.'

    $tampered = New-BackupSet -Name 'tampered'
    [System.IO.File]::AppendAllText(
        $tampered.Database,
        'tampered-after-publication',
        $utf8WithoutBom
    )
    $tamperedOutput = Join-Path $reportRoot 'tampered.json'
    Invoke-Verifier `
        -Database $tampered.Database `
        -Checksum $tampered.Checksum `
        -Metadata $tampered.Metadata `
        -Output $tamperedOutput `
        -Name 'tampered' `
        -ExpectedExitCode 1
    Assert-Contract `
        -Condition (-not (Test-Path -LiteralPath $tamperedOutput)) `
        -Message 'Tampered database published a verification report.'

    foreach ($variant in @(
        'duplicate-key',
        'nan',
        'extra-key',
        'non-utc',
        'non-string-version',
        'wrong-integrity',
        'wrong-foreign-key',
        'wrong-size',
        'space-timestamp',
        'basic-timestamp',
        'week-timestamp',
        'comma-timestamp',
        'short-fraction-timestamp',
        'invalid-date-timestamp'
    )) {
        $invalid = New-BackupSet -Name "metadata-$variant" -Variant $variant
        $invalidOutput = Join-Path $reportRoot "metadata-$variant.json"
        Invoke-Verifier `
            -Database $invalid.Database `
            -Checksum $invalid.Checksum `
            -Metadata $invalid.Metadata `
            -Output $invalidOutput `
            -Name "metadata-$variant" `
            -ExpectedExitCode 1
        Assert-Contract `
            -Condition (-not (Test-Path -LiteralPath $invalidOutput)) `
            -Message "Invalid metadata variant published a report: $variant"
    }

    foreach ($checksumVariant in @(
        'uppercase-checksum',
        'single-space-checksum',
        'crlf-checksum'
    )) {
        $invalidChecksum = New-BackupSet `
            -Name $checksumVariant `
            -Variant $checksumVariant
        $invalidChecksumOutput = Join-Path $reportRoot "$checksumVariant.json"
        Invoke-Verifier `
            -Database $invalidChecksum.Database `
            -Checksum $invalidChecksum.Checksum `
            -Metadata $invalidChecksum.Metadata `
            -Output $invalidChecksumOutput `
            -Name $checksumVariant `
            -ExpectedExitCode 1
        Assert-Contract `
            -Condition (-not (Test-Path -LiteralPath $invalidChecksumOutput)) `
            -Message "Non-canonical checksum bytes published a report: $checksumVariant"
    }

    $badMagic = New-BackupSet -Name 'bad-magic' -Variant 'bad-magic'
    $badMagicOutput = Join-Path $reportRoot 'bad-magic.json'
    Invoke-Verifier `
        -Database $badMagic.Database `
        -Checksum $badMagic.Checksum `
        -Metadata $badMagic.Metadata `
        -Output $badMagicOutput `
        -Name 'bad-magic' `
        -ExpectedExitCode 1
    Assert-Contract `
        -Condition (-not (Test-Path -LiteralPath $badMagicOutput)) `
        -Message 'A self-consistent non-SQLite file published a report.'

    $mutating = New-BackupSet -Name 'mid-verification-mutation'
    $mutationOutput = Join-Path $reportRoot 'mid-verification-mutation.json'
    Invoke-NativeChecked `
        -FilePath $PythonExecutable `
        -Arguments @(
            '-B',
            $mutationProbeScript,
            $verifierPath,
            $mutating.Database,
            $mutating.Checksum,
            $mutating.Metadata,
            $mutationOutput
        ) `
        -Description 'Mid-verification same-size mutation probe'
    Assert-Contract `
        -Condition (-not (Test-Path -LiteralPath $mutationOutput)) `
        -Message 'Mid-verification mutation published a report.'

    foreach ($sidecarSuffix in @('-wal', '-shm', '-journal')) {
        $sidecar = "$($valid.Database)$sidecarSuffix"
        [System.IO.File]::WriteAllBytes($sidecar, [byte[]]@())
        try {
            $sidecarLabel = $sidecarSuffix.TrimStart('-')
            $sidecarOutput = Join-Path $reportRoot "$sidecarLabel.json"
            Invoke-Verifier `
                -Database $valid.Database `
                -Checksum $valid.Checksum `
                -Metadata $valid.Metadata `
                -Output $sidecarOutput `
                -Name "sidecar-$sidecarLabel" `
                -ExpectedExitCode 1
            Assert-Contract `
                -Condition (-not (Test-Path -LiteralPath $sidecarOutput)) `
                -Message "SQLite $sidecarSuffix sidecar published a report."
        }
        finally {
            Remove-Item -LiteralPath $sidecar -Force
        }
    }

    $sidecarDatabase = New-BackupSet `
        -Name 'sidecar-database-name' `
        -DatabaseName 'source.sqlite3-wal'
    $sidecarDatabaseOutput = Join-Path $reportRoot 'sidecar-database-name.json'
    Invoke-Verifier `
        -Database $sidecarDatabase.Database `
        -Checksum $sidecarDatabase.Checksum `
        -Metadata $sidecarDatabase.Metadata `
        -Output $sidecarDatabaseOutput `
        -Name 'sidecar-database-name' `
        -ExpectedExitCode 1
    Assert-Contract `
        -Condition (-not (Test-Path -LiteralPath $sidecarDatabaseOutput)) `
        -Message 'A database using a sidecar filename published a report.'

    $sidecarOutput = Join-Path $reportRoot 'verification-wal'
    Invoke-Verifier `
        -Database $valid.Database `
        -Checksum $valid.Checksum `
        -Metadata $valid.Metadata `
        -Output $sidecarOutput `
        -Name 'sidecar-output-name' `
        -ExpectedExitCode 1
    Assert-Contract `
        -Condition (-not (Test-Path -LiteralPath $sidecarOutput)) `
        -Message 'A report using a sidecar filename was published.'

    $overwriteHashBefore = (
        Get-FileHash -LiteralPath $successOutput -Algorithm SHA256
    ).Hash
    Invoke-Verifier `
        -Database $valid.Database `
        -Checksum $valid.Checksum `
        -Metadata $valid.Metadata `
        -Output $successOutput `
        -Name 'overwrite' `
        -ExpectedExitCode 1
    $overwriteHashAfter = (
        Get-FileHash -LiteralPath $successOutput -Algorithm SHA256
    ).Hash
    Assert-Contract `
        -Condition ($overwriteHashAfter -eq $overwriteHashBefore) `
        -Message 'Existing verification report was overwritten.'

    $insideOutputRoot = Join-Path $valid.Root 'reports'
    [void](New-Item -ItemType Directory -Path $insideOutputRoot)
    $insideOutput = Join-Path $insideOutputRoot 'inside.json'
    Invoke-Verifier `
        -Database $valid.Database `
        -Checksum $valid.Checksum `
        -Metadata $valid.Metadata `
        -Output $insideOutput `
        -Name 'inside-input-directory' `
        -ExpectedExitCode 1
    Assert-Contract `
        -Condition (-not (Test-Path -LiteralPath $insideOutput)) `
        -Message 'Report was written inside the backup input directory.'

    $missingParentOutput = Join-Path `
        (Join-Path $temporaryRoot 'missing-report-directory') `
        'report.json'
    Invoke-Verifier `
        -Database $valid.Database `
        -Checksum $valid.Checksum `
        -Metadata $valid.Metadata `
        -Output $missingParentOutput `
        -Name 'missing-output-parent' `
        -ExpectedExitCode 1
    Assert-Contract `
        -Condition (-not (Test-Path -LiteralPath $missingParentOutput)) `
        -Message 'Report was written under a missing output parent.'

    $wrongName = New-BackupSet -Name 'wrong-checksum-name'
    $renamedChecksum = Join-Path $wrongName.Root 'renamed.sha256'
    Move-Item -LiteralPath $wrongName.Checksum -Destination $renamedChecksum
    $wrongNameOutput = Join-Path $reportRoot 'wrong-checksum-name.json'
    Invoke-Verifier `
        -Database $wrongName.Database `
        -Checksum $renamedChecksum `
        -Metadata $wrongName.Metadata `
        -Output $wrongNameOutput `
        -Name 'wrong-checksum-name' `
        -ExpectedExitCode 1
    Assert-Contract `
        -Condition (-not (Test-Path -LiteralPath $wrongNameOutput)) `
        -Message 'Incorrect checksum filename published a report.'

    $splitSet = New-BackupSet -Name 'split-input-directory'
    $otherInputRoot = Join-Path $temporaryRoot 'other-input-directory'
    [void](New-Item -ItemType Directory -Path $otherInputRoot)
    $movedMetadata = Join-Path `
        $otherInputRoot `
        ([System.IO.Path]::GetFileName($splitSet.Metadata))
    Move-Item -LiteralPath $splitSet.Metadata -Destination $movedMetadata
    $splitOutput = Join-Path $reportRoot 'split-input-directory.json'
    Invoke-Verifier `
        -Database $splitSet.Database `
        -Checksum $splitSet.Checksum `
        -Metadata $movedMetadata `
        -Output $splitOutput `
        -Name 'split-input-directory' `
        -ExpectedExitCode 1
    Assert-Contract `
        -Condition (-not (Test-Path -LiteralPath $splitOutput)) `
        -Message 'Inputs from different directories published a report.'

    $relative = New-BackupSet -Name 'relative-path'
    Push-Location $temporaryRoot
    try {
        $relativeDatabase = 'input-relative-path\backup.sqlite3'
        $relativeOutput = Join-Path $reportRoot 'relative-path.json'
        Invoke-Verifier `
            -Database $relativeDatabase `
            -Checksum $relative.Checksum `
            -Metadata $relative.Metadata `
            -Output $relativeOutput `
            -Name 'relative-path' `
            -ExpectedExitCode 1
        Assert-Contract `
            -Condition (-not (Test-Path -LiteralPath $relativeOutput)) `
            -Message 'Relative database path published a report.'
    }
    finally {
        Pop-Location
    }

    if ($env:OS -eq 'Windows_NT') {
        $uncOutput = Join-Path $reportRoot 'unc-input.json'
        Invoke-Verifier `
            -Database '\\localhost\ffxivshare-contract\missing.sqlite3' `
            -Checksum $valid.Checksum `
            -Metadata $valid.Metadata `
            -Output $uncOutput `
            -Name 'unc-input' `
            -ExpectedExitCode 1
        Assert-Contract `
            -Condition (-not (Test-Path -LiteralPath $uncOutput)) `
            -Message 'UNC database input published a report.'

        $adsInputOutput = Join-Path $reportRoot 'ads-input.json'
        Invoke-Verifier `
            -Database "$($valid.Database):stream" `
            -Checksum $valid.Checksum `
            -Metadata $valid.Metadata `
            -Output $adsInputOutput `
            -Name 'alternate-data-stream-input' `
            -ExpectedExitCode 1
        Assert-Contract `
            -Condition (-not (Test-Path -LiteralPath $adsInputOutput)) `
            -Message 'Alternate-data-stream input published a report.'

        $junctionPath = Join-Path $temporaryRoot 'input-junction'
        $junction = New-Item `
            -ItemType Junction `
            -Path $junctionPath `
            -Target $valid.Root
        try {
            $junctionOutput = Join-Path $reportRoot 'junction-input.json'
            Invoke-Verifier `
                -Database (Join-Path $junctionPath 'backup.sqlite3') `
                -Checksum (Join-Path $junctionPath 'backup.sqlite3.sha256') `
                -Metadata (Join-Path $junctionPath 'backup.sqlite3.metadata.json') `
                -Output $junctionOutput `
                -Name 'junction-input' `
                -ExpectedExitCode 1
            Assert-Contract `
                -Condition (-not (Test-Path -LiteralPath $junctionOutput)) `
                -Message 'Junction-backed input published a report.'
        }
        finally {
            $junction.Delete()
        }

        $adsOutput = "$reportRoot\ads-report.json:stream"
        Invoke-Verifier `
            -Database $valid.Database `
            -Checksum $valid.Checksum `
            -Metadata $valid.Metadata `
            -Output $adsOutput `
            -Name 'alternate-data-stream-output' `
            -ExpectedExitCode 1
        Assert-Contract `
            -Condition (-not (Test-Path -LiteralPath $adsOutput)) `
            -Message 'Alternate-data-stream output was created.'
    }

    $temporaryArtifacts = @(
        Get-ChildItem -LiteralPath $temporaryRoot -Recurse -Force -File |
            Where-Object { $_.Name -match '^\..+\.tmp-[a-f0-9]{32}$' }
    )
    Assert-Contract `
        -Condition ($temporaryArtifacts.Count -eq 0) `
        -Message 'Verifier left temporary report artifacts behind.'

    $allLogs = (
        Get-ChildItem -LiteralPath $logRoot -File |
            ForEach-Object { Get-Content -LiteralPath $_.FullName -Raw }
    ) -join "`n"
    Assert-Contract `
        -Condition (-not $allLogs.Contains($temporaryRoot)) `
        -Message 'Verifier logs exposed an absolute test path.'
    Assert-Contract `
        -Condition (-not $allLogs.Contains('business-secret')) `
        -Message 'Verifier logs exposed database business content.'

    Assert-Contract `
        -Condition ($LASTEXITCODE -eq 0) `
        -Message 'An expected verifier failure leaked its native exit code.'
    $global:LASTEXITCODE = 0
    Write-Output 'SQLite backup-set verification contracts passed.'
}
finally {
    Remove-TestDirectory -Path $temporaryRoot
}
