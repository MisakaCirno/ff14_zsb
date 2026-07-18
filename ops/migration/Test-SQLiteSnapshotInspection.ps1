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
    'SCHEMA_INVENTORY_FORMAT',
    '_validate_schema_inventory',
    'TABLE_STRUCTURE_FORMAT',
    '_validate_table_structures',
    'main.sqlite_schema',
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
$sameSchemaDatabase = Join-Path $temporaryRoot 'same-schema.sqlite3'
$schemaDriftDatabase = Join-Path $temporaryRoot 'schema-drift.sqlite3'
$hardlinkSourceDatabase = Join-Path $temporaryRoot 'hardlink-source.sqlite3'
$hardlinkAliasDatabase = Join-Path $temporaryRoot 'hardlink-alias.sqlite3'

$fixtureSource = @'
import sqlite3
import sys

path = sys.argv[1]
invalid_fk = sys.argv[2] == "invalid-fk"
schema_drift = sys.argv[2] == "schema-drift"
data_variant = sys.argv[2] == "data-variant"
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
        "CREATE TABLE parent ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "label TEXT NOT NULL, external_id TEXT NOT NULL UNIQUE)"
    )
    connection.execute(
        "CREATE TABLE child ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "parent_id INTEGER NOT NULL REFERENCES parent(id))"
    )
    connection.execute(
        "INSERT INTO parent(label, external_id) VALUES (?, ?)",
        (
            "different-row" if data_variant else "safe",
            "fixture-2" if data_variant else "fixture-1",
        ),
    )
    parent_id = 999 if invalid_fk else 1
    connection.execute("INSERT INTO child(parent_id) VALUES (?)", (parent_id,))
    connection.execute(
        "CREATE INDEX child_parent_custom_idx ON child(parent_id)"
    )
    view_columns = "label, id" if schema_drift else "id, label"
    connection.execute(
        f"CREATE VIEW parent_labels AS SELECT {view_columns} FROM parent"
    )
    connection.execute(
        "CREATE TRIGGER parent_label_guard "
        "BEFORE UPDATE OF label ON parent BEGIN "
        "SELECT RAISE(ABORT, 'label required') WHERE NEW.label = ''; END"
    )
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
    Invoke-NativeChecked `
        -FilePath $PythonExecutable `
        -Arguments @($fixtureScript, $sameSchemaDatabase, 'data-variant') `
        -Description 'Same-schema SQLite fixture creation'
    Invoke-NativeChecked `
        -FilePath $PythonExecutable `
        -Arguments @($fixtureScript, $schemaDriftDatabase, 'schema-drift') `
        -Description 'Schema-drift SQLite fixture creation'
    Invoke-NativeChecked `
        -FilePath $PythonExecutable `
        -Arguments @($fixtureScript, $hardlinkSourceDatabase, 'valid') `
        -Description 'Hard-link source SQLite fixture creation'
    Invoke-NativeChecked `
        -FilePath $PythonExecutable `
        -Arguments @(
            '-B',
            '-c',
            'import os, sys; os.link(sys.argv[1], sys.argv[2])',
            $hardlinkSourceDatabase,
            $hardlinkAliasDatabase
        ) `
        -Description 'Hard-linked SQLite alias creation'

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

    $tableStructures = $report.inspection.table_structures
    Assert-True `
        -Condition ($tableStructures.format -eq 'ffxivshare-sqlite-table-structure-inventory') `
        -Message 'Inspection report has an unexpected table structure format.'
    Assert-True `
        -Condition ($tableStructures.sha256 -match '^[0-9a-f]{64}$') `
        -Message 'Inspection report has an invalid table structure digest.'
    Assert-True `
        -Condition ($tableStructures.table_count -eq @($tableStructures.tables).Count) `
        -Message 'Inspection report has an inconsistent table structure count.'
    $parentStructure = @($tableStructures.tables | Where-Object { $_.name -eq 'parent' })
    $childStructure = @($tableStructures.tables | Where-Object { $_.name -eq 'child' })
    Assert-True `
        -Condition ($parentStructure.Count -eq 1 -and @($parentStructure[0].columns).Count -eq 3 -and @($parentStructure[0].unique_constraints).Count -ge 1) `
        -Message 'Parent columns or unique constraints were not inventoried.'
    Assert-True `
        -Condition ($childStructure.Count -eq 1 -and @($childStructure[0].foreign_keys).Count -eq 1) `
        -Message 'Child foreign keys were not inventoried.'
    $structuredTableNames = @($tableStructures.tables | ForEach-Object { $_.name })
    foreach ($mark in @($report.inspection.sqlite_sequence.high_water_marks)) {
        Assert-True `
            -Condition ($structuredTableNames -ccontains $mark.table) `
            -Message "sqlite_sequence references a table absent from table_structures: $($mark.table)"
    }

    $schemaInventory = $report.inspection.sqlite_schema
    Assert-True `
        -Condition ($schemaInventory.format -eq 'ffxivshare-sqlite-schema-inventory') `
        -Message 'Inspection report has an unexpected schema inventory format.'
    Assert-True `
        -Condition ($schemaInventory.format_version -eq 1) `
        -Message 'Inspection report has an unexpected schema inventory version.'
    Assert-True `
        -Condition ($schemaInventory.schema -eq 'main') `
        -Message 'Inspection report did not identify the main schema.'
    Assert-True `
        -Condition ($schemaInventory.sha256 -match '^[0-9a-f]{64}$') `
        -Message 'Inspection report has an invalid schema inventory digest.'
    Assert-True `
        -Condition ($schemaInventory.object_count -eq @($schemaInventory.objects).Count) `
        -Message 'Inspection report has an inconsistent schema object count.'
    Assert-True `
        -Condition ($schemaInventory.object_count -eq 6) `
        -Message 'Schema inventory did not contain the complete non-internal fixture schema.'
    Assert-True `
        -Condition ($schemaInventory.excluded_objects.name_prefix -ceq 'sqlite_') `
        -Message 'Inspection report does not declare its internal-object exclusion.'

    $schemaObjects = @($schemaInventory.objects)
    foreach ($expectedObject in @(
        @{ type = 'index'; name = 'child_parent_custom_idx'; table = 'child' },
        @{ type = 'table'; name = 'parent'; table = 'parent' },
        @{ type = 'trigger'; name = 'parent_label_guard'; table = 'parent' },
        @{ type = 'view'; name = 'parent_labels'; table = 'parent_labels' }
    )) {
        $matches = @($schemaObjects | Where-Object {
            $_.type -ceq $expectedObject.type -and
            $_.name -ceq $expectedObject.name -and
            $_.tbl_name -ceq $expectedObject.table -and
            $_.sql -is [string] -and $_.sql.Length -gt 0
        })
        Assert-True `
            -Condition ($matches.Count -eq 1) `
            -Message "Schema inventory omitted $($expectedObject.type) $($expectedObject.name)."
    }
    $internalSchemaObjects = @($schemaObjects | Where-Object {
        $_.name.StartsWith('sqlite_', [System.StringComparison]::Ordinal)
    })
    Assert-True `
        -Condition ($internalSchemaObjects.Count -eq 0) `
        -Message 'Schema inventory exposed a reserved SQLite internal object.'
    Assert-True `
        -Condition (@($schemaObjects | Where-Object { $_.name -eq 'sqlite_autoindex_parent_1' }).Count -eq 0) `
        -Message 'Schema inventory did not exclude an automatic SQLite index.'

    $schemaValidationScript = Join-Path $temporaryRoot 'validate_schema_inventory.py'
    $schemaValidationSource = @'
from copy import deepcopy
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("snapshot_inspector", sys.argv[1])
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
with open(sys.argv[2], "r", encoding="utf-8") as stream:
    inventory = json.load(stream)["inspection"]["sqlite_schema"]

module._validate_schema_inventory(inventory)

mutations = []
unknown_key = deepcopy(inventory)
unknown_key["unexpected"] = True
mutations.append(unknown_key)
missing_sql = deepcopy(inventory)
del missing_sql["objects"][0]["sql"]
mutations.append(missing_sql)
changed_sql = deepcopy(inventory)
changed_sql["objects"][-1]["sql"] += " "
mutations.append(changed_sql)
bad_digest = deepcopy(inventory)
bad_digest["sha256"] = "0" * 64
mutations.append(bad_digest)
reversed_objects = deepcopy(inventory)
reversed_objects["objects"].reverse()
mutations.append(reversed_objects)

for candidate in mutations:
    try:
        module._validate_schema_inventory(candidate)
    except module.InspectionError:
        continue
    raise SystemExit("malformed schema inventory passed strict validation")
'@
    [System.IO.File]::WriteAllText(
        $schemaValidationScript,
        $schemaValidationSource,
        $utf8WithoutBom
    )
    Invoke-NativeChecked `
        -FilePath $PythonExecutable `
        -Arguments @($schemaValidationScript, $inspectorPath, $successOutput) `
        -Description 'Strict schema inventory validation contract'

    $sameSchemaHash = (
        Get-FileHash -LiteralPath $sameSchemaDatabase -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    Assert-True `
        -Condition ($sameSchemaHash -ne $validHashBefore) `
        -Message 'Same-schema fixture must have different database content.'
    $sameSchemaOutput = Join-Path $temporaryRoot 'same-schema-report.json'
    $sameSchemaExit = Invoke-Inspector `
        -Database $sameSchemaDatabase `
        -ExpectedSha256 $sameSchemaHash `
        -Output $sameSchemaOutput `
        -LogPath (Join-Path $temporaryRoot 'same-schema.log')
    Assert-True `
        -Condition ($sameSchemaExit -eq 0) `
        -Message 'Same-schema snapshot inspection must succeed.'
    $sameSchemaReport = Get-Content -LiteralPath $sameSchemaOutput -Raw | ConvertFrom-Json
    Assert-True `
        -Condition ($sameSchemaReport.inspection.sqlite_schema.sha256 -eq $schemaInventory.sha256) `
        -Message 'Business row changes must not change the schema inventory digest.'
    Assert-True `
        -Condition ($sameSchemaReport.inspection.table_structures.sha256 -eq $tableStructures.sha256) `
        -Message 'Business row changes must not change the table structure digest.'

    $schemaDriftHash = (
        Get-FileHash -LiteralPath $schemaDriftDatabase -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    $schemaDriftOutput = Join-Path $temporaryRoot 'schema-drift-report.json'
    $schemaDriftExit = Invoke-Inspector `
        -Database $schemaDriftDatabase `
        -ExpectedSha256 $schemaDriftHash `
        -Output $schemaDriftOutput `
        -LogPath (Join-Path $temporaryRoot 'schema-drift.log')
    Assert-True `
        -Condition ($schemaDriftExit -eq 0) `
        -Message 'Schema-drift snapshot inspection must succeed for human review.'
    $schemaDriftReport = Get-Content -LiteralPath $schemaDriftOutput -Raw | ConvertFrom-Json
    Assert-True `
        -Condition ($schemaDriftReport.inspection.sqlite_schema.sha256 -ne $schemaInventory.sha256) `
        -Message 'A changed custom view must change the schema inventory digest.'
    $driftedView = @($schemaDriftReport.inspection.sqlite_schema.objects | Where-Object {
        $_.type -eq 'view' -and $_.name -eq 'parent_labels'
    })
    Assert-True `
        -Condition ($driftedView.Count -eq 1 -and $driftedView[0].sql.Contains('SELECT label, id')) `
        -Message 'Schema-drift SQL was not exposed for human review.'

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

    $hardlinkHash = (
        Get-FileHash -LiteralPath $hardlinkAliasDatabase -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    $hardlinkOutput = Join-Path $temporaryRoot 'hardlink-report.json'
    $hardlinkExit = Invoke-Inspector `
        -Database $hardlinkAliasDatabase `
        -ExpectedSha256 $hardlinkHash `
        -Output $hardlinkOutput `
        -LogPath (Join-Path $temporaryRoot 'hardlink.log')
    Assert-True `
        -Condition ($hardlinkExit -ne 0) `
        -Message 'Hard-linked SQLite snapshot must fail inspection.'
    Assert-True `
        -Condition (-not (Test-Path -LiteralPath $hardlinkOutput)) `
        -Message 'Hard-linked SQLite snapshot must not publish a report.'

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

    foreach ($sidecarSuffix in @('-wal', '-shm', '-journal')) {
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

        $forbiddenOutputExit = Invoke-Inspector `
            -Database $validDatabase `
            -ExpectedSha256 $validHashBefore `
            -Output $sidecarPath `
            -LogPath (Join-Path $temporaryRoot "$sidecarName-output.log")
        Assert-True `
            -Condition ($forbiddenOutputExit -ne 0) `
            -Message "Inspector output must not use SQLite $sidecarSuffix path."
        Assert-True `
            -Condition (-not (Test-Path -LiteralPath $sidecarPath)) `
            -Message "Rejected SQLite $sidecarSuffix output must not be created."
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
