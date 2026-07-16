[CmdletBinding()]
param(
    [string]$RepositoryRoot = '',
    [string]$PythonExecutable = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-True {
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
    if (-not $resolvedPath.StartsWith(
        $requiredPrefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'Refusing to remove a test directory outside the system temp directory.'
    }
    if ((Split-Path -Leaf $resolvedPath) -notmatch '^ffxivshare-sqlite-inspection-[a-f0-9]{32}$') {
        throw 'Refusing to remove a test directory with an unexpected name.'
    }
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

function Invoke-Inspector {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Database,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedSha256,
        [Parameter(Mandatory = $true)]
        [string]$Output,
        [Parameter(Mandatory = $true)]
        [string]$LogPath
    )

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $PythonExecutable $inspectorPath `
            --database $Database `
            --expected-sha256 $ExpectedSha256 `
            --output $Output *> $LogPath
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    return $exitCode
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

$inspectorPath = Join-Path $RepositoryRoot 'ops\migration\Inspect-SQLiteSnapshot.py'
Assert-True `
    -Condition (Test-Path -LiteralPath $inspectorPath -PathType Leaf) `
    -Message "SQLite snapshot inspector is missing: $inspectorPath"

$inspectorSource = [System.IO.File]::ReadAllText($inspectorPath)
foreach ($requiredText in @(
    'mode=ro&immutable=1',
    'PRAGMA main.integrity_check',
    'PRAGMA main.foreign_key_check',
    'sha256_before',
    'sha256_after',
    'django_migrations',
    'sqlite_sequence'
)) {
    Assert-True `
        -Condition $inspectorSource.Contains($requiredText) `
        -Message "Inspector is missing required contract text: $requiredText"
}

$temporaryRoot = Join-Path `
    ([System.IO.Path]::GetTempPath()) `
    ('ffxivshare-sqlite-inspection-' + [Guid]::NewGuid().ToString('N'))
$fixtureScript = Join-Path $temporaryRoot 'create_fixture.py'
$validDatabase = Join-Path $temporaryRoot 'valid.sqlite3'
$invalidForeignKeyDatabase = Join-Path $temporaryRoot 'invalid-fk.sqlite3'

$fixtureSource = @'
import sqlite3
import sys

path = sys.argv[1]
invalid_fk = sys.argv[2] == "invalid-fk"
connection = sqlite3.connect(path)
try:
    connection.execute("PRAGMA user_version=25")
    connection.execute(
        "CREATE TABLE django_migrations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "app TEXT NOT NULL, name TEXT NOT NULL, applied TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO django_migrations(app, name, applied) VALUES (?, ?, ?)",
        ("shares", "0025_add_collection_owner_index", "2026-07-16T00:00:00Z"),
    )
    connection.execute(
        "CREATE TABLE parent (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE child ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "parent_id INTEGER NOT NULL REFERENCES parent(id))"
    )
    connection.execute("INSERT INTO parent(label) VALUES (?)", ("safe",))
    parent_id = 999 if invalid_fk else 1
    connection.execute("INSERT INTO child(parent_id) VALUES (?)", (parent_id,))
    connection.commit()
finally:
    connection.close()
'@

try {
    [void](New-Item -ItemType Directory -Path $temporaryRoot)
    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($fixtureScript, $fixtureSource, $utf8WithoutBom)

    Invoke-NativeChecked `
        -FilePath $PythonExecutable `
        -Arguments @($fixtureScript, $validDatabase, 'valid') `
        -Description 'Valid SQLite fixture creation'
    Invoke-NativeChecked `
        -FilePath $PythonExecutable `
        -Arguments @($fixtureScript, $invalidForeignKeyDatabase, 'invalid-fk') `
        -Description 'Invalid foreign-key SQLite fixture creation'

    $validHashBefore = (
        Get-FileHash -LiteralPath $validDatabase -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    $successOutput = Join-Path $temporaryRoot 'success-report.json'
    $successLog = Join-Path $temporaryRoot 'success.log'
    $successExit = Invoke-Inspector `
        -Database $validDatabase `
        -ExpectedSha256 $validHashBefore `
        -Output $successOutput `
        -LogPath $successLog
    Assert-True ($successExit -eq 0) 'Valid snapshot inspection must succeed.'
    Assert-True `
        -Condition (Test-Path -LiteralPath $successOutput -PathType Leaf) `
        -Message 'Valid snapshot inspection did not publish a report.'

    $reportText = Get-Content -LiteralPath $successOutput -Raw
    Assert-True `
        -Condition (-not $reportText.Contains('"safe"')) `
        -Message 'Inspection report exposed a business record value.'
    $report = $reportText | ConvertFrom-Json
    Assert-True `
        -Condition ($report.format -eq 'ffxivshare-sqlite-snapshot-inspection') `
        -Message 'Unexpected inspection report format.'
    Assert-True `
        -Condition ($report.database.sha256 -eq $validHashBefore) `
        -Message 'Inspection report hash does not match the source database.'
    Assert-True `
        -Condition ([bool]$report.database.source_unchanged) `
        -Message 'Inspection report did not confirm an unchanged source.'
    Assert-True `
        -Condition ($report.inspection.user_version -eq 25) `
        -Message 'Inspection report did not preserve user_version.'
    Assert-True `
        -Condition ($report.inspection.integrity_check -eq 'ok') `
        -Message 'Inspection report did not pass integrity_check.'
    Assert-True `
        -Condition ($report.inspection.foreign_key_check.violations -eq 0) `
        -Message 'Inspection report did not pass foreign_key_check.'
    Assert-True `
        -Condition ($report.inspection.django_migrations.count -eq 1) `
        -Message 'Inspection report did not inventory Django migrations.'
    Assert-True `
        -Condition ($report.inspection.django_migrations.applied[0].app -eq 'shares') `
        -Message 'Inspection report contains an unexpected migration app.'
    Assert-True `
        -Condition ($report.inspection.sqlite_sequence.count -ge 1) `
        -Message 'Inspection report did not inventory sqlite_sequence.'

    $parentTable = @($report.inspection.tables | Where-Object { $_.name -eq 'parent' })
    $childTable = @($report.inspection.tables | Where-Object { $_.name -eq 'child' })
    Assert-True `
        -Condition ($parentTable.Count -eq 1 -and $parentTable[0].row_count -eq 1) `
        -Message 'Parent table inventory is incorrect.'
    Assert-True `
        -Condition ($childTable.Count -eq 1 -and $childTable[0].row_count -eq 1) `
        -Message 'Child table inventory is incorrect.'

    $validHashAfter = (
        Get-FileHash -LiteralPath $validDatabase -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    Assert-True `
        -Condition ($validHashAfter -eq $validHashBefore) `
        -Message 'Inspector modified the source SQLite snapshot.'

    $wrongHashOutput = Join-Path $temporaryRoot 'wrong-hash-report.json'
    $wrongHashExit = Invoke-Inspector `
        -Database $validDatabase `
        -ExpectedSha256 ('0' * 64) `
        -Output $wrongHashOutput `
        -LogPath (Join-Path $temporaryRoot 'wrong-hash.log')
    Assert-True ($wrongHashExit -ne 0) 'Incorrect SHA256 must fail inspection.'
    Assert-True `
        -Condition (-not (Test-Path -LiteralPath $wrongHashOutput)) `
        -Message 'Incorrect SHA256 must not publish a report.'

    $uppercaseHashOutput = Join-Path $temporaryRoot 'uppercase-hash-report.json'
    $uppercaseHashExit = Invoke-Inspector `
        -Database $validDatabase `
        -ExpectedSha256 $validHashBefore.ToUpperInvariant() `
        -Output $uppercaseHashOutput `
        -LogPath (Join-Path $temporaryRoot 'uppercase-hash.log')
    Assert-True ($uppercaseHashExit -ne 0) 'Uppercase SHA256 must fail inspection.'
    Assert-True `
        -Condition (-not (Test-Path -LiteralPath $uppercaseHashOutput)) `
        -Message 'Uppercase SHA256 must not publish a report.'

    $invalidForeignKeyHash = (
        Get-FileHash -LiteralPath $invalidForeignKeyDatabase -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    $invalidForeignKeyOutput = Join-Path $temporaryRoot 'invalid-fk-report.json'
    $invalidForeignKeyExit = Invoke-Inspector `
        -Database $invalidForeignKeyDatabase `
        -ExpectedSha256 $invalidForeignKeyHash `
        -Output $invalidForeignKeyOutput `
        -LogPath (Join-Path $temporaryRoot 'invalid-fk.log')
    Assert-True ($invalidForeignKeyExit -ne 0) 'Foreign-key violations must fail inspection.'
    Assert-True `
        -Condition (-not (Test-Path -LiteralPath $invalidForeignKeyOutput)) `
        -Message 'Foreign-key violations must not publish a report.'
    $invalidForeignKeyHashAfter = (
        Get-FileHash -LiteralPath $invalidForeignKeyDatabase -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    Assert-True `
        -Condition ($invalidForeignKeyHashAfter -eq $invalidForeignKeyHash) `
        -Message 'Failed foreign-key inspection modified its source snapshot.'

    foreach ($sidecarSuffix in @('-wal', '-shm')) {
        $sidecarPath = "$validDatabase$sidecarSuffix"
        [System.IO.File]::WriteAllBytes($sidecarPath, [byte[]]@())
        try {
            $sidecarName = $sidecarSuffix.TrimStart('-')
            $sidecarOutput = Join-Path $temporaryRoot "$sidecarName-report.json"
            $sidecarExit = Invoke-Inspector `
                -Database $validDatabase `
                -ExpectedSha256 $validHashBefore `
                -Output $sidecarOutput `
                -LogPath (Join-Path $temporaryRoot "$sidecarName.log")
            Assert-True `
                -Condition ($sidecarExit -ne 0) `
                -Message "SQLite $sidecarSuffix sidecar must fail inspection."
            Assert-True `
                -Condition (-not (Test-Path -LiteralPath $sidecarOutput)) `
                -Message "SQLite $sidecarSuffix sidecar must not publish a report."
        }
        finally {
            Remove-Item -LiteralPath $sidecarPath -Force
        }
    }

    $temporaryArtifacts = @(
        Get-ChildItem -LiteralPath $temporaryRoot -Force -File |
            Where-Object { $_.Name -match '^\..+\.tmp-[a-f0-9]{32}$' }
    )
    Assert-True `
        -Condition ($temporaryArtifacts.Count -eq 0) `
        -Message 'Inspector left temporary report artifacts behind.'

    $finalHash = (
        Get-FileHash -LiteralPath $validDatabase -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    Assert-True `
        -Condition ($finalHash -eq $validHashBefore) `
        -Message 'Failure-path checks modified the source SQLite snapshot.'

    # The final native inspector invocation is intentionally expected to fail.
    # Do not leak that captured exit code to verify.ps1 after all assertions pass.
    $global:LASTEXITCODE = 0
    Write-Output 'SQLite snapshot inspection contracts passed.'
}
finally {
    Remove-TestDirectory -Path $temporaryRoot
}
