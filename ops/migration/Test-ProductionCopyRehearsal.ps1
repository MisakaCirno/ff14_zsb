[CmdletBinding()]
param(
    [string]$RepositoryRoot = '',
    [string]$PythonExecutable = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-Contract {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

function Remove-TestRoot {
    param([Parameter(Mandatory = $true)][string]$Path)
    $temp = [System.IO.Path]::GetFullPath(
        [System.IO.Path]::GetTempPath()
    ).TrimEnd('\', '/')
    $resolved = [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    Assert-Contract `
        -Condition ($resolved.StartsWith(
            $temp + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase
        )) `
        -Message 'Refusing to clean outside the system temp directory.'
    Assert-Contract `
        -Condition ((Split-Path -Leaf $resolved) -match '^ffxivshare-rehearsal-[a-f0-9]{32}$') `
        -Message 'Refusing to clean an unexpected test directory.'
    if (Test-Path -LiteralPath $resolved) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    $RepositoryRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
$RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path

if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $venvPython = Join-Path $RepositoryRoot 'venv\Scripts\python.exe'
    $PythonExecutable = if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        (Resolve-Path -LiteralPath $venvPython).Path
    }
    else {
        (Get-Command python -CommandType Application -ErrorAction Stop).Source
    }
}
else {
    $PythonExecutable = (Resolve-Path -LiteralPath $PythonExecutable).Path
}

$orchestrator = Join-Path $PSScriptRoot 'Rehearse-ProductionCopy.py'
Assert-Contract `
    -Condition (Test-Path -LiteralPath $orchestrator -PathType Leaf) `
    -Message "Rehearsal orchestrator is missing: $orchestrator"

$temporaryRoot = Join-Path `
    ([System.IO.Path]::GetTempPath()) `
    ('ffxivshare-rehearsal-' + [Guid]::NewGuid().ToString('N'))
$fixtureScript = Join-Path $temporaryRoot 'test_rehearsal.py'
New-Item -ItemType Directory -Path $temporaryRoot | Out-Null

$fixtureSource = @'
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import importlib.util
import importlib.machinery
import inspect
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


assert sys.flags.isolated
assert sys.flags.no_user_site
assert sys.flags.ignore_environment
assert sys.flags.dont_write_bytecode
assert sys.flags.utf8_mode
assert sys.flags.optimize == 0


repository_root = Path(sys.argv[1]).resolve()
orchestrator_path = Path(sys.argv[2]).resolve()
test_root = Path(sys.argv[3]).resolve()

spec = importlib.util.spec_from_file_location("ffxivshare_rehearsal", orchestrator_path)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

compressed_probe = module._compressed_python_command(
    "probe_value = 41\nprobe_value += 1\n",
    filename="<compressed-probe>",
)
compressed_namespace: dict[str, object] = {}
exec(compressed_probe, compressed_namespace)
assert compressed_namespace["probe_value"] == 42
for source, filename in (
    (module.RUNTIME_FINGERPRINT_SCRIPT, "<runtime-fingerprint-contract>"),
    (module.RUNTIME_IDENTITY_CHECKPOINT_SCRIPT, "<runtime-checkpoint-contract>"),
):
    compressed = module._compressed_python_command(source, filename=filename)
    assert compressed == module._compressed_python_command(source, filename=filename)
    assert len(compressed) < 12_000
    compile(compressed, filename, "exec")

execute_source = inspect.getsource(module.Rehearsal.execute)
approval_child_index = execute_source.index('"approved_policy_evidence_verification"')
bootstrap_initial_gate_index = execute_source.rfind(
    '"runtime_fingerprint_initial"',
    0,
    approval_child_index,
)
assert bootstrap_initial_gate_index > execute_source.index(
    "self.bootstrap_context.execution_bundle_sha256"
)

SOURCE_APPLIED = [["shares", "0001_initial"]]
SOURCE_LEAVES = [["shares", "0001_initial"]]
TARGET_APPLIED = [
    ["shares", "0001_initial"],
    ["shares", "0002_current"],
]
TARGET_LEAVES = [["shares", "0002_current"]]
PLAN_BYTES = b"Planned operations:\n  Apply shares.0002_current\n"
MIGRATION_RUNTIME_SHA256 = "a" * 64
RUNTIME_PROJECTION = {"fixture": "runtime"}
RUNTIME_FINGERPRINT_SHA256 = module._canonical_json_sha256(RUNTIME_PROJECTION)


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def file_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


class OpenForbiddenPath:
    def __init__(self, path: Path):
        self.path = path

    def __fspath__(self) -> str:
        return os.fspath(self.path)

    def open(self, *_args, **_kwargs):
        raise AssertionError("Oversized checkpoint opened the artifact")


oversized_checkpoint = test_root / "oversized-checkpoint.bin"
with oversized_checkpoint.open("wb") as stream:
    stream.seek(1024)
    stream.write(b"\0")
try:
    module._regular_file_checkpoint(
        OpenForbiddenPath(oversized_checkpoint),
        issue_prefix="oversized_checkpoint",
        maximum_size=1024,
    )
except module.RehearsalBlocked as exc:
    assert exc.code == "oversized_checkpoint_too_large"
else:
    raise AssertionError("Oversized checkpoint was not rejected before open")


def argument_after(argv: list[str], name: str) -> Path:
    return Path(argv[argv.index(name) + 1])


def migration_state(applied: list[list[str]], leaves: list[list[str]]) -> dict[str, object]:
    return {
        "format": "ffxivshare-migration-state",
        "format_version": 1,
        "database_vendor": "sqlite",
        "applied": applied,
        "applied_leaf_nodes": leaves,
        "repository_leaf_nodes": TARGET_LEAVES,
        "unknown_applied_nodes": [],
        "python_version": "fixture-python",
        "django_version": "fixture-django",
        "migration_runtime_sha256": MIGRATION_RUNTIME_SHA256,
    }


def sqlite_schema_inventory() -> dict[str, object]:
    inventory = {
        "format": "ffxivshare-sqlite-schema-inventory",
        "format_version": 1,
        "schema": "main",
        "included_object_types": ["index", "table", "trigger", "view"],
        "excluded_objects": {
            "name_prefix": "sqlite_",
            "comparison": "SQLite ASCII case-insensitive prefix/identifier comparison",
            "reason": "SQLite-reserved internal and automatically generated objects",
        },
        "normalization": {
            "object_order": ["type", "name", "tbl_name", "sql (NULL first)"],
            "string_order": "Unicode code-point order",
            "sql": "verbatim sqlite_schema.sql with NULL preserved",
            "digest": "SHA-256 of canonical UTF-8 JSON excluding sha256",
            "canonical_json": "sorted object keys; no insignificant whitespace",
        },
        "object_count": 1,
        "objects": [
            {
                "type": "table",
                "name": "fixture_table",
                "tbl_name": "fixture_table",
                "sql": "CREATE TABLE fixture_table (id INTEGER PRIMARY KEY)",
            }
        ],
    }
    inventory["sha256"] = sha256(
        json.dumps(
            inventory,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return inventory


SOURCE_SQLITE_SCHEMA_SHA256 = sqlite_schema_inventory()["sha256"]


def table_structure_inventory() -> dict[str, object]:
    inventory = {
        "format": "ffxivshare-sqlite-table-structure-inventory",
        "format_version": 1,
        "schema": "main",
        "table_count": 1,
        "tables": [
            {
                "name": "fixture_table",
                "column_count": 1,
                "columns": [
                    {
                        "cid": 0,
                        "name": "id",
                        "type": "INTEGER",
                        "notnull": 0,
                        "default": None,
                        "primary_key": 1,
                        "hidden": 0,
                    }
                ],
                "foreign_keys": [],
                "unique_constraints": [],
            }
        ],
    }
    inventory["sha256"] = sha256(
        json.dumps(
            inventory,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return inventory


def inspection_report(digest: str, applied: list[list[str]]) -> dict[str, object]:
    return {
        "format": "ffxivshare-sqlite-snapshot-inspection",
        "format_version": 1,
        "database": {
            "sha256": digest,
            "sha256_before": digest,
            "sha256_after": digest,
            "source_unchanged": True,
        },
        "inspection": {
            "query_only": True,
            "integrity_check": "ok",
            "foreign_key_check": {"status": "ok", "violations": 0},
            "sqlite_schema": sqlite_schema_inventory(),
            "sqlite_sequence": {
                "present": True,
                "count": 1,
                "high_water_marks": [{"table": "fixture_table", "sequence": 7}],
            },
            "table_structures": table_structure_inventory(),
            "django_migrations": {
                "present": True,
                "applied": [
                    {"app": app, "name": name, "applied": "2026-01-01T00:00:00Z"}
                    for app, name in applied
                ],
            },
        },
    }


inspection_contract_path = test_root / "inspection-contract.json"
valid_inspection = inspection_report("9" * 64, SOURCE_APPLIED)
write_json(inspection_contract_path, valid_inspection)
inspection_validation = module._validate_inspection_report(
    inspection_contract_path,
    expected_sha256="9" * 64,
)
assert inspection_validation.applied_migrations == SOURCE_APPLIED
assert inspection_validation.schema_sha256 == SOURCE_SQLITE_SCHEMA_SHA256
assert inspection_validation.sqlite_sequence == {"fixture_table": 7}
assert module._sqlite_identifier_key("Straße") != module._sqlite_identifier_key("STRASSE")

for mutation in ("extra_key", "metadata", "order", "digest"):
    candidate = deepcopy(valid_inspection)
    inventory = candidate["inspection"]["sqlite_schema"]
    if mutation == "extra_key":
        inventory["unexpected"] = True
    elif mutation == "metadata":
        inventory["normalization"]["sql"] = "normalized"
    elif mutation == "order":
        second = deepcopy(inventory["objects"][0])
        second["name"] = "aaa_table"
        second["tbl_name"] = "aaa_table"
        second["sql"] = "CREATE TABLE aaa_table (id INTEGER PRIMARY KEY)"
        inventory["objects"].append(second)
        inventory["object_count"] = 2
        inventory["sha256"] = module._sqlite_schema_inventory_sha256(inventory)
    else:
        inventory["sha256"] = "0" * 64
    write_json(inspection_contract_path, candidate)
    try:
        module._validate_inspection_report(
            inspection_contract_path,
            expected_sha256="9" * 64,
        )
    except module.RehearsalError:
        pass
    else:
        raise AssertionError(f"Malformed SQLite schema inventory passed: {mutation}")

case_duplicate_schema = deepcopy(valid_inspection)
case_schema = case_duplicate_schema["inspection"]["sqlite_schema"]
case_object = deepcopy(case_schema["objects"][0])
case_object["name"] = "FIXTURE_TABLE"
case_object["tbl_name"] = "FIXTURE_TABLE"
case_schema["objects"].insert(0, case_object)
case_schema["object_count"] = 2
case_schema["sha256"] = module._sqlite_schema_inventory_sha256(case_schema)
bool_schema_version = deepcopy(valid_inspection)
bool_schema_version["inspection"]["sqlite_schema"]["format_version"] = True


sequence_mutations = []
bad_sequence_shape = deepcopy(valid_inspection)
bad_sequence_shape["inspection"]["sqlite_sequence"]["unexpected"] = True
sequence_mutations.append(bad_sequence_shape)
bool_sequence_count = deepcopy(valid_inspection)
bool_sequence_count["inspection"]["sqlite_sequence"]["count"] = True
sequence_mutations.append(bool_sequence_count)
bool_sequence_value = deepcopy(valid_inspection)
bool_sequence_value["inspection"]["sqlite_sequence"]["high_water_marks"][0][
    "sequence"
] = True
sequence_mutations.append(bool_sequence_value)
negative_sequence = deepcopy(valid_inspection)
negative_sequence["inspection"]["sqlite_sequence"]["high_water_marks"][0][
    "sequence"
] = -1
sequence_mutations.append(negative_sequence)
duplicate_sequence = deepcopy(valid_inspection)
duplicate_sequence["inspection"]["sqlite_sequence"]["count"] = 2
duplicate_sequence["inspection"]["sqlite_sequence"]["high_water_marks"].append(
    {"table": "fixture_table", "sequence": 8}
)
sequence_mutations.append(duplicate_sequence)
case_duplicate_sequence = deepcopy(valid_inspection)
case_duplicate_sequence["inspection"]["sqlite_sequence"]["count"] = 2
case_duplicate_sequence["inspection"]["sqlite_sequence"]["high_water_marks"].insert(
    0, {"table": "FIXTURE_TABLE", "sequence": 8}
)
sequence_mutations.append(case_duplicate_sequence)
unordered_sequence = deepcopy(valid_inspection)
unordered_sequence["inspection"]["sqlite_sequence"]["count"] = 2
unordered_sequence["inspection"]["sqlite_sequence"]["high_water_marks"].append(
    {"table": "aaa_table", "sequence": 1}
)
sequence_mutations.append(unordered_sequence)
bool_report_version = deepcopy(valid_inspection)
bool_report_version["format_version"] = True
sequence_mutations.append(bool_report_version)
sequence_mutations.extend((case_duplicate_schema, bool_schema_version))
for candidate in sequence_mutations:
    write_json(inspection_contract_path, candidate)
    try:
        module._validate_inspection_report(
            inspection_contract_path,
            expected_sha256="9" * 64,
        )
    except module.RehearsalError:
        pass
    else:
        raise AssertionError("Malformed SQLite sequence inventory passed validation")

case_duplicate_table = deepcopy(valid_inspection)
case_tables = case_duplicate_table["inspection"]["table_structures"]
case_table = deepcopy(case_tables["tables"][0])
case_table["name"] = "FIXTURE_TABLE"
case_tables["tables"].insert(0, case_table)
case_tables["table_count"] = 2
case_tables["sha256"] = module._table_structure_sha256(case_tables)
case_duplicate_column = deepcopy(valid_inspection)
case_columns = case_duplicate_column["inspection"]["table_structures"]
case_column = deepcopy(case_columns["tables"][0]["columns"][0])
case_column["cid"] = 1
case_column["name"] = "ID"
case_columns["tables"][0]["columns"].append(case_column)
case_columns["tables"][0]["column_count"] = 2
case_columns["sha256"] = module._table_structure_sha256(case_columns)
bool_table_version = deepcopy(valid_inspection)
bool_table_version["inspection"]["table_structures"]["format_version"] = True
for candidate in (case_duplicate_table, case_duplicate_column, bool_table_version):
    write_json(inspection_contract_path, candidate)
    try:
        module._validate_inspection_report(
            inspection_contract_path,
            expected_sha256="9" * 64,
        )
    except module.RehearsalError:
        pass
    else:
        raise AssertionError("Malformed SQLite table structure inventory passed")


def validated_inspection(payload: dict[str, object]) -> object:
    write_json(inspection_contract_path, payload)
    return module._validate_inspection_report(
        inspection_contract_path,
        expected_sha256="9" * 64,
    )


source_structure = validated_inspection(valid_inspection)
module.SQL_CHANGE_EXCEPTIONS = {}
module.MISSING_OBJECT_EXCEPTIONS = {}
module.ADDED_OBJECT_EXCEPTIONS = {}
module.COLUMN_ATTRIBUTE_EXCEPTIONS = {}
assert module._database_structure_preservation_projection(
    source_structure,
    source_structure,
    source_structure,
)["preserved"] is True

def with_sequence_marks(
    payload: dict[str, object], marks: list[tuple[str, int]]
) -> dict[str, object]:
    candidate = deepcopy(payload)
    candidate["inspection"]["sqlite_sequence"] = {
        "present": True,
        "count": len(marks),
        "high_water_marks": [
            {"table": table, "sequence": sequence}
            for table, sequence in sorted(marks)
        ],
    }
    return candidate


portable_source = validated_inspection(
    with_sequence_marks(
        valid_inspection,
        [
            ("auth_user", 7),
            ("auth_user_groups", 70),
            ("django_migrations", 80),
            ("django_session", 90),
        ],
    )
)
portable_upgraded = validated_inspection(
    with_sequence_marks(
        valid_inspection,
        [
            ("auth_user", 9),
            ("auth_user_groups", 71),
            ("django_migrations", 81),
            ("django_session", 91),
        ],
    )
)
portable_target = validated_inspection(
    with_sequence_marks(
        valid_inspection,
        [
            ("auth_user", 9),
            ("auth_user_groups", 1),
            ("django_migrations", 1),
            ("django_session", 1),
        ],
    )
)
portable_projection = module._database_structure_preservation_projection(
    portable_source,
    portable_upgraded,
    portable_target,
)
assert portable_projection["preserved"] is True
final_sequence = portable_projection["destinations"]["final_target"]
assert final_sequence["sequence_scope"]["declared_tables"] == sorted(
    module.FINAL_TARGET_SEQUENCE_TABLES
)
assert final_sequence["sequence_scope"]["checked_tables"] == ["auth_user"]
assert final_sequence["sequence_checks"] == [
    {
        "table": "auth_user",
        "original_source_floor": 7,
        "upgraded_source_floor": 9,
        "effective_floor": 9,
        "destination_value": 9,
        "preserved": True,
    }
]
excluded_sequence = {
    item["table"]: item
    for item in final_sequence["sequence_scope"]["observed_excluded_entries"]
}
assert set(excluded_sequence) == {
    "auth_user_groups",
    "django_migrations",
    "django_session",
}
assert all(item["reason"] for item in excluded_sequence.values())

portable_target_low = validated_inspection(
    with_sequence_marks(
        valid_inspection,
        [
            ("auth_user", 8),
            ("auth_user_groups", 1),
            ("django_migrations", 1),
            ("django_session", 1),
        ],
    )
)
assert module._database_structure_preservation_projection(
    portable_source,
    portable_upgraded,
    portable_target_low,
)["destinations"]["final_target"]["preserved"] is False

portable_target_missing = validated_inspection(
    with_sequence_marks(
        valid_inspection,
        [
            ("auth_user_groups", 1),
            ("django_migrations", 1),
            ("django_session", 1),
        ],
    )
)
assert module._database_structure_preservation_projection(
    portable_source,
    portable_upgraded,
    portable_target_missing,
)["destinations"]["final_target"]["preserved"] is False

portable_upgraded_low = validated_inspection(
    with_sequence_marks(
        valid_inspection,
        [
            ("auth_user", 9),
            ("auth_user_groups", 69),
            ("django_migrations", 81),
            ("django_session", 91),
        ],
    )
)
assert module._database_structure_preservation_projection(
    portable_source,
    portable_upgraded_low,
    portable_target,
)["destinations"]["upgraded_source"]["preserved"] is False

lower_sequence_report = deepcopy(valid_inspection)
lower_sequence_report["inspection"]["sqlite_sequence"]["high_water_marks"][0][
    "sequence"
] = 6
lower_sequence = validated_inspection(lower_sequence_report)
assert module._database_structure_preservation_projection(
    source_structure,
    lower_sequence,
    lower_sequence,
)["preserved"] is False

missing_sequence_report = deepcopy(valid_inspection)
missing_sequence_report["inspection"]["sqlite_sequence"] = {
    "present": True,
    "count": 0,
    "high_water_marks": [],
}
missing_sequence = validated_inspection(missing_sequence_report)
assert module._database_structure_preservation_projection(
    source_structure,
    missing_sequence,
    missing_sequence,
)["preserved"] is False

changed_sql_report = deepcopy(valid_inspection)
changed_schema = changed_sql_report["inspection"]["sqlite_schema"]
changed_schema["objects"][0]["sql"] += " STRICT"
changed_schema["sha256"] = module._sqlite_schema_inventory_sha256(changed_schema)
changed_sql = validated_inspection(changed_sql_report)
assert module._database_structure_preservation_projection(
    source_structure,
    changed_sql,
    changed_sql,
)["preserved"] is False


sql_identity = ("table", "fixture_table", "fixture_table")
exact_sql_change_report = deepcopy(valid_inspection)
exact_sql_inventory = exact_sql_change_report["inspection"]["sqlite_schema"]
exact_sql_inventory["objects"][0]["sql"] += " STRICT"
exact_sql_inventory["sha256"] = module._sqlite_schema_inventory_sha256(
    exact_sql_inventory
)
exact_sql_change = validated_inspection(exact_sql_change_report)
module.SQL_CHANGE_EXCEPTIONS = {
    sql_identity: {
        "source_sql_sha256": module._sql_sha256(
            source_structure.schema_objects[sql_identity]
        ),
        "destination_sql_sha256": module._sql_sha256(
            exact_sql_change.schema_objects[sql_identity]
        ),
        "reason": "fixture exact SQL transition",
    }
}
assert module._database_structure_preservation_projection(
    source_structure,
    exact_sql_change,
    exact_sql_change,
)["preserved"] is True

tampered_exact_report = deepcopy(exact_sql_change_report)
tampered_exact_inventory = tampered_exact_report["inspection"]["sqlite_schema"]
tampered_exact_inventory["objects"][0]["sql"] += " WITHOUT ROWID"
tampered_exact_inventory["sha256"] = module._sqlite_schema_inventory_sha256(
    tampered_exact_inventory
)
tampered_exact = validated_inspection(tampered_exact_report)
assert module._database_structure_preservation_projection(
    source_structure,
    tampered_exact,
    tampered_exact,
)["preserved"] is False

module.SQL_CHANGE_EXCEPTIONS = {}
module.MISSING_OBJECT_EXCEPTIONS = {sql_identity: {
    "source_sql_sha256": module._sql_sha256(
        source_structure.schema_objects[sql_identity]
    ),
    "reason": "fixture exception must be consumed",
}}
assert module._database_structure_preservation_projection(
    source_structure,
    source_structure,
    source_structure,
)["preserved"] is False
module.MISSING_OBJECT_EXCEPTIONS = {}


source_custom_column_report = deepcopy(valid_inspection)
source_columns = source_custom_column_report["inspection"]["table_structures"]
source_columns["tables"][0]["column_count"] = 2
source_columns["tables"][0]["columns"].append(
    {
        "cid": 1,
        "name": "custom_column",
        "type": "TEXT",
        "notnull": 0,
        "default": None,
        "primary_key": 0,
        "hidden": 0,
    }
)
source_columns["sha256"] = module._table_structure_sha256(source_columns)
source_custom_column = validated_inspection(source_custom_column_report)
assert module._database_structure_preservation_projection(
    source_custom_column,
    source_structure,
    source_structure,
)["preserved"] is False


cid_only_report = deepcopy(valid_inspection)
cid_only_structures = cid_only_report["inspection"]["table_structures"]
cid_only_structures["tables"][0]["columns"][0]["cid"] = 7
cid_only_structures["sha256"] = module._table_structure_sha256(cid_only_structures)
cid_only = validated_inspection(cid_only_report)
cid_projection = module._database_structure_preservation_projection(
    source_structure,
    cid_only,
    cid_only,
)
assert cid_projection["preserved"] is True
for destination_name in ("upgraded_source", "final_target"):
    assert cid_projection["destinations"][destination_name][
        "column_cid_diagnostics"
    ] == [
        {
            "table": "fixture_table",
            "column": "id",
            "source": 0,
            "destination": 7,
        }
    ]

for attribute, changed_value in (
    ("type", "TEXT"),
    ("notnull", 1),
    ("default", "0"),
    ("primary_key", 0),
    ("hidden", 1),
):
    semantic_change_report = deepcopy(valid_inspection)
    semantic_structures = semantic_change_report["inspection"]["table_structures"]
    semantic_structures["tables"][0]["columns"][0][attribute] = changed_value
    semantic_structures["sha256"] = module._table_structure_sha256(
        semantic_structures
    )
    semantic_change = validated_inspection(semantic_change_report)
    assert module._database_structure_preservation_projection(
        source_structure,
        semantic_change,
        semantic_change,
    )["preserved"] is False


unique_source_report = deepcopy(valid_inspection)
unique_schema = unique_source_report["inspection"]["sqlite_schema"]
unique_schema["objects"][0]["sql"] = (
    "CREATE TABLE fixture_table (id INTEGER PRIMARY KEY, value TEXT, "
    "UNIQUE(id, value))"
)
unique_schema["sha256"] = module._sqlite_schema_inventory_sha256(unique_schema)
unique_structures = unique_source_report["inspection"]["table_structures"]
unique_structures["tables"][0]["columns"].append(
    {
        "cid": 1,
        "name": "value",
        "type": "TEXT",
        "notnull": 0,
        "default": None,
        "primary_key": 0,
        "hidden": 0,
    }
)
unique_structures["tables"][0]["column_count"] = 2
unique_structures["tables"][0]["unique_constraints"] = [
    {
        "columns": [
            {"cid": 0, "name": "id", "descending": 0, "collation": "BINARY"},
            {
                "cid": 1,
                "name": "value",
                "descending": 0,
                "collation": "BINARY",
            },
        ],
        "partial": 0,
    }
]
unique_structures["sha256"] = module._table_structure_sha256(unique_structures)
unique_source = validated_inspection(unique_source_report)

unique_cid_only_report = deepcopy(unique_source_report)
unique_cid_only_constraints = unique_cid_only_report["inspection"][
    "table_structures"
]
unique_cid_only_constraints["tables"][0]["unique_constraints"][0]["columns"][
    0
]["cid"] = 7
unique_cid_only_constraints["tables"][0]["unique_constraints"][0]["columns"][
    1
]["cid"] = 8
unique_cid_only_constraints["sha256"] = module._table_structure_sha256(
    unique_cid_only_constraints
)
unique_cid_only = validated_inspection(unique_cid_only_report)
assert module._database_structure_preservation_projection(
    unique_source,
    unique_cid_only,
    unique_cid_only,
)["preserved"] is True

for mutation in ("name", "order", "descending", "collation", "partial", "missing"):
    unique_mutation_report = deepcopy(unique_source_report)
    mutation_structures = unique_mutation_report["inspection"]["table_structures"]
    constraint = mutation_structures["tables"][0]["unique_constraints"][0]
    if mutation == "name":
        constraint["columns"][1]["name"] = "changed_value"
    elif mutation == "order":
        constraint["columns"].reverse()
    elif mutation == "descending":
        constraint["columns"][1]["descending"] = 1
    elif mutation == "collation":
        constraint["columns"][1]["collation"] = "NOCASE"
    elif mutation == "partial":
        constraint["partial"] = 1
    else:
        mutation_structures["tables"][0]["unique_constraints"] = []
    mutation_structures["sha256"] = module._table_structure_sha256(
        mutation_structures
    )
    unique_mutation = validated_inspection(unique_mutation_report)
    assert module._database_structure_preservation_projection(
        unique_source,
        unique_mutation,
        unique_mutation,
    )["preserved"] is False

rowid_unique_report = deepcopy(unique_source_report)
rowid_structures = rowid_unique_report["inspection"]["table_structures"]
rowid_structures["tables"][0]["unique_constraints"][0]["columns"] = [
    {"cid": -1, "name": None, "descending": 0, "collation": "BINARY"}
]
rowid_structures["sha256"] = module._table_structure_sha256(rowid_structures)
rowid_unique = validated_inspection(rowid_unique_report)
expression_unique_report = deepcopy(rowid_unique_report)
expression_structures = expression_unique_report["inspection"]["table_structures"]
expression_structures["tables"][0]["unique_constraints"][0]["columns"][0][
    "cid"
] = -2
expression_structures["sha256"] = module._table_structure_sha256(
    expression_structures
)
expression_unique = validated_inspection(expression_unique_report)
assert module._database_structure_preservation_projection(
    rowid_unique,
    expression_unique,
    expression_unique,
)["preserved"] is False


def validation_report() -> dict[str, object]:
    return {
        "format": "ffxivshare-jsonl",
        "format_version": 3,
        "valid": True,
        "errors": [],
        "warnings": [],
        "quarantined_records": [],
        "entity_counts": {},
    }


for supported_version in (3, 4, 5):
    supported_report = validation_report()
    supported_report["format_version"] = supported_version
    supported_path = test_root / f"validation-v{supported_version}.json"
    write_json(supported_path, supported_report)
    module._validate_validation_report(supported_path)

for unsupported_version in (2, 6):
    unsupported_report = validation_report()
    unsupported_report["format_version"] = unsupported_version
    unsupported_path = test_root / f"validation-v{unsupported_version}.json"
    write_json(unsupported_path, unsupported_report)
    try:
        module._validate_validation_report(unsupported_path)
    except module.RehearsalError as exc:
        assert str(exc) == "dataset_validation_report_invalid"
    else:
        raise AssertionError("Unsupported portable dataset validation version accepted")


def import_report(status: str, target_state: str) -> dict[str, object]:
    return {
        **validation_report(),
        "operation": "site_data_import",
        "status": status,
        "target_state": target_state,
        "database_state": "complete",
        "data_stage": "verified",
        "sequence_stage": "verified",
        "recoverable": False,
        "target_session_row_count": 0,
        "exclusive_target_attested": True,
        "cutover_authorized": False,
    }


class StubRunner:
    def __init__(
        self,
        source: Path,
        *,
        fail_stage: str | None = None,
        tamper_stage: str | None = None,
        interrupt_stage: str | None = None,
        first_import_status: str = "imported",
        inject_execution_entry_stage: str | None = None,
        target_media_drift_after_initial: bool = False,
        rewrite_runtime_report_stage: str | None = None,
        mutate_runtime_projection_at_final: bool = False,
    ):
        self.source = source
        self.source_sha256 = file_hash(source)
        self.fail_stage = fail_stage
        self.tamper_stage = tamper_stage
        self.interrupt_stage = interrupt_stage
        self.first_import_status = first_import_status
        self.inject_execution_entry_stage = inject_execution_entry_stage
        self.target_media_drift_after_initial = target_media_drift_after_initial
        self.rewrite_runtime_report_stage = rewrite_runtime_report_stage
        self.mutate_runtime_projection_at_final = mutate_runtime_projection_at_final
        self.target_media_root: Path | None = None
        self.leaked_environment = False
        self.source_used_by_django = False
        self.original_input_used_by_child = False
        self.stages: list[str] = []
        self.original_inputs = {
            source.resolve(),
            source.with_name(source.name + ".sha256").resolve(),
            source.with_name(source.name + ".metadata.json").resolve(),
            (source.parent / "media-manifest.json").resolve(),
        }

    def run(self, *, stage, argv, cwd, env, stdout_path, stderr_path):
        del cwd
        self.stages.append(stage)
        forbidden_value = os.environ.get("FFXIVSHARE_PRODUCTION_SECRET")
        if "FFXIVSHARE_PRODUCTION_SECRET" in env or forbidden_value in env.values():
            self.leaked_environment = True
        database_path = env.get("DATABASE_PATH")
        if database_path and Path(database_path).resolve() == self.source.resolve():
            self.source_used_by_django = True
        for argument in argv:
            if os.path.isabs(argument) and Path(argument).resolve() in self.original_inputs:
                self.original_input_used_by_child = True

        stdout = PLAN_BYTES if stage == "source_schema_plan" else b"stub command passed\n"
        stdout_path.write_bytes(stdout)
        stderr_path.write_bytes(b"")
        run_root = stdout_path.parent.parent

        if stage == self.interrupt_stage:
            raise KeyboardInterrupt()

        if stage == self.fail_stage:
            (run_root / "artifacts" / "stub-failure-marker.txt").write_text("retained\n", encoding="utf-8")
            return module.CommandResult(17, stdout_path, stderr_path)

        if stage.startswith("runtime_fingerprint_"):
            inline_index = argv.index("-c") + 1
            expected_inline = module._compressed_python_command(
                (
                    module.RUNTIME_FINGERPRINT_SCRIPT
                    if stage in {"runtime_fingerprint_initial", "runtime_fingerprint_final"}
                    else module.RUNTIME_IDENTITY_CHECKPOINT_SCRIPT
                ),
                filename=(
                    "<ffxivshare-runtime-fingerprint>"
                    if stage in {"runtime_fingerprint_initial", "runtime_fingerprint_final"}
                    else "<ffxivshare-runtime-checkpoint>"
                ),
            )
            assert argv[inline_index] == expected_inline
            assert len(argv[inline_index]) < 12_000
            if stage in {"runtime_fingerprint_initial", "runtime_fingerprint_final"}:
                runtime_projection = (
                    {"fixture": "runtime-mutated"}
                    if stage == "runtime_fingerprint_final"
                    and self.mutate_runtime_projection_at_final
                    else RUNTIME_PROJECTION
                )
                write_json(Path(argv[-1]), {
                    "format": "ffxivshare-runtime-fingerprint",
                    "format_version": 1,
                    "fingerprint_sha256": module._canonical_json_sha256(runtime_projection),
                    "projection": runtime_projection,
                    "checkpoint": {
                        "format": "ffxivshare-runtime-identity-checkpoint-source",
                        "format_version": 1,
                        "content_hashed": True,
                        "identity_inventory": [{
                            "path": "$PREFIX/fixture.py",
                            "device": 1,
                            "inode": 1,
                            "size": 1,
                            "mtime_ns": 1,
                            "ctime_ns": 1,
                        }],
                        "closure_scopes": [{
                            "root": "$PREFIX",
                            "mode": "top_level_entries",
                            "files": ["$PREFIX/fixture.py"],
                        }],
                    },
                })
            else:
                if stage == self.rewrite_runtime_report_stage:
                    source_report = Path(argv[-4])
                    rewritten = json.loads(source_report.read_text(encoding="utf-8"))
                    assert rewritten["fingerprint_sha256"] == RUNTIME_FINGERPRINT_SHA256
                    rewritten["checkpoint"]["identity_inventory"][0]["mtime_ns"] += 1
                    write_json(source_report, rewritten)
                write_json(Path(argv[-1]), {
                    "format": "ffxivshare-runtime-identity-checkpoint",
                    "format_version": 1,
                    "fingerprint_sha256": RUNTIME_FINGERPRINT_SHA256,
                    "source_report_sha256": argv[-2],
                    "content_rehashed": False,
                    "identity_inventory_checked": 1,
                    "closure_scopes_checked": 1,
                    "unchanged": True,
                })
        elif stage == "source_artifacts_verified":
            output = argument_after(argv, "--output")
            write_json(output, {
                "format": "ffxivshare-sqlite-backup-set-verification",
                "format_version": 1,
                "verified": True,
                "cutover_authorized": False,
                "inspection_required": True,
                "artifact": {
                    "sha256": self.source_sha256,
                    "size": self.source.stat().st_size,
                },
            })
        elif stage == "source_inspected":
            write_json(argument_after(argv, "--output"), inspection_report(self.source_sha256, SOURCE_APPLIED))
        elif stage == "source_schema_state":
            write_json(Path(argv[-1]), migration_state(SOURCE_APPLIED, SOURCE_LEAVES))
        elif stage == "source_schema_ready_state":
            write_json(Path(argv[-1]), migration_state(TARGET_APPLIED, TARGET_LEAVES))
        elif stage == "upgraded_source_inspected":
            expected = argv[argv.index("--expected-sha256") + 1]
            write_json(
                argument_after(argv, "--output"),
                inspection_report(expected, TARGET_APPLIED),
            )
        elif stage == "dataset_exported":
            output = Path(argv[argv.index("export_site_data") + 1])
            output.mkdir()
            write_json(output / "manifest.json", {"fixture": "source"})
        elif stage == "dataset_validated":
            write_json(argument_after(argv, "--report"), validation_report())
        elif stage == "target_schema_migrate":
            target = Path(env["DATABASE_PATH"])
            connection = sqlite3.connect(target)
            connection.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
            connection.commit()
            connection.close()
        elif stage == "target_schema_state":
            write_json(Path(argv[-1]), migration_state(TARGET_APPLIED, TARGET_LEAVES))
        elif stage == "import_verified":
            target_state = "empty" if self.first_import_status == "imported" else "complete"
            write_json(
                argument_after(argv, "--report"),
                import_report(self.first_import_status, target_state),
            )
        elif stage == "idempotence_verified":
            write_json(argument_after(argv, "--report"), import_report("already_imported", "complete"))
        elif stage == "target_dataset_exported":
            output = Path(argv[argv.index("export_site_data") + 1])
            output.mkdir()
            write_json(output / "manifest.json", {"fixture": "target"})
        elif stage == "target_dataset_validated":
            write_json(argument_after(argv, "--report"), validation_report())
        elif stage == "target_export_compared":
            write_json(argument_after(argv, "--output"), {
                "format": "ffxivshare-site-data-export-comparison",
                "format_version": 1,
                "equivalent": True,
                "cutover_authorized": False,
                "issues": [],
            })
        elif stage == "restriction_preflight":
            write_json(argument_after(argv, "--output"), {
                "valid": True,
                "ready_for_cutover": True,
                "blocking_errors": [],
                "manual_review": {"count": 0, "share_ids": [], "categories": []},
            })
        elif stage == "target_snapshot_backup":
            output = Path(argv[argv.index("backup_database") + 1])
            output.parent.mkdir(exist_ok=True)
            connection = sqlite3.connect(output)
            connection.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
            connection.commit()
            connection.close()
            digest = file_hash(output)
            output.with_name(output.name + ".sha256").write_text(
                f"{digest}  {output.name}\n", encoding="utf-8", newline="\n"
            )
            write_json(output.with_name(output.name + ".metadata.json"), {"sha256": digest})
        elif stage in {"target_snapshot_set_verified", "target_snapshot_set_final_verified"}:
            database = argument_after(argv, "--database")
            write_json(argument_after(argv, "--output"), {
                "format": "ffxivshare-sqlite-backup-set-verification",
                "format_version": 1,
                "verified": True,
                "cutover_authorized": False,
                "inspection_required": True,
                "artifact": {"sha256": file_hash(database), "size": database.stat().st_size},
            })
        elif stage == "target_snapshot_inspected":
            expected = argv[argv.index("--expected-sha256") + 1]
            write_json(argument_after(argv, "--output"), inspection_report(expected, TARGET_APPLIED))
        elif stage == "final_target_migration_state":
            write_json(Path(argv[-1]), migration_state(TARGET_APPLIED, TARGET_LEAVES))
        elif stage == "final_target_dataset_exported":
            output = Path(argv[argv.index("export_site_data") + 1])
            output.mkdir()
            write_json(output / "manifest.json", {"fixture": "final-target"})
        elif stage == "final_target_dataset_validated":
            write_json(argument_after(argv, "--report"), validation_report())
        elif stage == "final_target_export_compared":
            write_json(argument_after(argv, "--output"), {
                "format": "ffxivshare-site-data-export-comparison",
                "format_version": 1,
                "equivalent": True,
                "cutover_authorized": False,
                "issues": [],
            })
        elif stage == "final_target_restriction_preflight":
            write_json(argument_after(argv, "--output"), {
                "valid": True,
                "ready_for_cutover": True,
                "blocking_errors": [],
                "manual_review": {"count": 0, "share_ids": [], "categories": []},
            })
        elif stage in {
            "target_media_manifest_built",
            "target_media_manifest_final_built",
        }:
            self.target_media_root = Path(argument_after(argv, "--root"))
            write_json(argument_after(argv, "--output"), {
                "format": "ffxivshare-media-manifest",
                "format_version": 2,
                "generated_at": "2026-01-01T00:00:00Z",
                "hash_algorithm": "sha256",
                "path_normalization": "unicode_nfc_canonical_caseless_unique",
                "source_snapshot": {
                    "id": argv[argv.index("--snapshot-id") + 1],
                    "offline_confirmed": True,
                },
                "file_count": 0,
                "total_size": 0,
                "files": [],
            })
        elif stage in {"media_compared", "final_media_compared"}:
            if stage == "final_media_compared" and self.target_media_drift_after_initial:
                return module.CommandResult(17, stdout_path, stderr_path)
            write_json(argument_after(argv, "--output"), {
                "format": "ffxivshare-media-comparison",
                "format_version": 1,
                "matched": True,
                "missing_paths": [],
                "unexpected_paths": [],
                "changed_paths": [],
            })
            if (
                stage == "media_compared"
                and self.target_media_drift_after_initial
                and self.target_media_root is not None
            ):
                (self.target_media_root / "late-drift.bin").write_bytes(b"changed")

        if stage == self.tamper_stage:
            with self.source.open("ab") as stream:
                stream.write(b"tampered")
        if stage == self.inject_execution_entry_stage:
            injected = run_root / "code" / "shares" / "__pycache__" / "injected.pyc"
            injected.parent.mkdir()
            injected.write_bytes(b"not trusted bytecode")
        return module.CommandResult(0, stdout_path, stderr_path)


def build_case(name: str, *, bad_plan: bool = False, bad_bundle: bool = False):
    root = test_root / name
    source_root = root / "source"
    target_media_root = root / "target-media"
    source_root.mkdir(parents=True)
    target_media_root.mkdir(parents=True)
    source = source_root / "production.sqlite3"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE django_migrations (id INTEGER PRIMARY KEY, app TEXT, name TEXT, applied TEXT)")
    connection.execute(
        "INSERT INTO django_migrations (app, name, applied) VALUES (?, ?, ?)",
        ("shares", "0001_initial", "2026-01-01T00:00:00Z"),
    )
    connection.commit()
    connection.close()
    digest = file_hash(source)
    source.with_name(source.name + ".sha256").write_text(
        f"{digest}  {source.name}\n", encoding="utf-8", newline="\n"
    )
    write_json(source.with_name(source.name + ".metadata.json"), {
        "schema_version": 1,
        "sha256": digest,
        "size": source.stat().st_size,
    })
    source_media_manifest = source_root / "media-manifest.json"
    write_json(source_media_manifest, {
        "format": "ffxivshare-media-manifest",
        "format_version": 2,
        "generated_at": "2026-01-01T00:00:00Z",
        "hash_algorithm": "sha256",
        "path_normalization": "unicode_nfc_canonical_caseless_unique",
        "source_snapshot": {"id": "source-fixture", "offline_confirmed": True},
        "file_count": 0,
        "total_size": 0,
        "files": [],
    })
    policy = root / "policy" / "upgrade-policy.json"
    policy.parent.mkdir()
    write_json(policy, {
        "format": module.POLICY_FORMAT,
        "format_version": module.POLICY_VERSION,
        "policy_id": "fixture-policy",
        "approved": True,
        "approved_at": "2026-07-16T00:00:00Z",
        "approval_tool_sha256": "1" * 64,
        "lossless_reviewed": True,
        "proposal_id": "fixture-proposal",
        "proposal_sha256": "2" * 64,
        "proposal_body_sha256": "3" * 64,
        "proposal_run_id": "fixture-proposal-run",
        "proposal_bootstrap_nonce": "4" * 64,
        "proposal_evidence_set_sha256": "5" * 64,
        "proposal_ledger_head_sha256": "6" * 64,
        "proposal_ledger_event_count": 8,
        "proposal_bootstrap_completion_sha256": "7" * 64,
        "review_id": "fixture-review",
        "reviewed_at": "2026-07-16T00:01:00Z",
        "review_record_sha256": "8" * 64,
        "reviewer": "fixture-reviewer",
        "reviewer_identity_verification": "operator_asserted_not_cryptographically_verified",
        "source_database_sha256": digest,
        "source_media_manifest_sha256": file_hash(source_media_manifest),
        "source_media_snapshot_id": "source-fixture",
        "source_applied_migrations_sha256": module._canonical_json_sha256(SOURCE_APPLIED),
        "source_sqlite_schema_sha256": SOURCE_SQLITE_SCHEMA_SHA256,
        "migration_runtime_sha256": MIGRATION_RUNTIME_SHA256,
        "runtime_fingerprint_sha256": RUNTIME_FINGERPRINT_SHA256,
        "execution_bundle_sha256": (
            "0" * 64
            if bad_bundle
            else module._execution_bundle_sha256(repository_root)
        ),
        "source_leaf_nodes": SOURCE_LEAVES,
        "target_leaf_nodes": TARGET_LEAVES,
        "migration_plan_sha256": "0" * 64 if bad_plan else sha256(PLAN_BYTES).hexdigest(),
    })
    config = module.RehearsalConfig(
        repository_root=repository_root,
        python_executable=Path(sys.executable).resolve(),
        source_database=source,
        source_checksum=source.with_name(source.name + ".sha256"),
        source_metadata=source.with_name(source.name + ".metadata.json"),
        source_upgrade_policy=policy,
        source_media_manifest=source_media_manifest,
        target_media_root=target_media_root,
        target_media_snapshot_id="target-fixture",
        run_root=root / "run",
        confirm_source_immutable=True,
        confirm_target_media_offline=True,
    )
    return config, source


def load_result(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def verify_chain(path: Path) -> None:
    previous = "0" * 64
    sequence = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw_line)
        sequence += 1
        assert event["sequence"] == sequence
        assert event["previous_event_sha256"] == previous
        digest = event.pop("event_sha256")
        assert module._canonical_json_sha256(event) == digest
        previous = digest


os.environ["FFXIVSHARE_PRODUCTION_SECRET"] = "must-not-reach-child"
module._secure_run_root = lambda _path: "test_only_acl_override"

success_config, success_source = build_case("success")
success_runner = StubRunner(success_source)
status, result_path = module.run_rehearsal(success_config, runner=success_runner)
assert status == "completed", load_result(result_path)
result = load_result(result_path)
assert result["status"] == "completed"
assert result["cutover_authorized"] is False
assert result["network_isolation_enforced"] is False
assert result["network_access_observation"] == "not_measured"
assert result["live_production_service_access_requested_by_orchestrator"] is False
assert result["production_copy_read_performed"] is True
assert result["contains_production_user_data"] is True
assert result["retained_on_success"] is True
assert result["secure_disposal_required"] is True
assert result["sensitive_retention_scope"] == "entire_run_root"
assert result["sensitive_retention_directories"] == ["."]
assert "network_access_performed" not in result
assert "production_access_performed" not in result
assert result["workspace_access_control"] == "test_only_acl_override"
assert success_runner.leaked_environment is False
assert success_runner.source_used_by_django is False
assert success_runner.original_input_used_by_child is False
assert success_runner.stages[0] == "runtime_fingerprint_initial"
assert success_runner.stages[-1] == "runtime_fingerprint_final"
verify_chain(success_config.run_root / "evidence" / "events.jsonl")
success_events = [
    json.loads(line)
    for line in (success_config.run_root / "evidence" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
]
candidate_events = [
    event for event in success_events
    if event["stage"] == "deployment_candidate_verified"
]
assert len(candidate_events) == 1
candidate = candidate_events[0]["details"]
assert set(candidate["backup_set"]) == {"database", "checksum", "metadata"}
assert candidate["target_media_snapshot_id"] == "target-fixture"
assert candidate["cutover_authorized"] is False
assert any(
    event["stage"] == "final_target_verification_copy_unchanged"
    and event["details"]["sqlite_sidecars_absent"] is True
    for event in success_events
)
final_snapshot_events = [
    event
    for event in success_events
    if event["stage"] == "target_snapshot_set_final_verified"
]
assert len(final_snapshot_events) == 1
assert final_snapshot_events[0]["outcome"] == "passed"
assert final_snapshot_events[0]["details"]["backup_set_unchanged"] is True
runtime_events = {
    event["stage"]: event
    for event in success_events
    if event["stage"].endswith("_verified")
    and "runtime_fingerprint_sha256" in event["details"]
}
assert runtime_events["runtime_fingerprint_initial_verified"]["details"]["content_rehashed"] is True
assert runtime_events["runtime_fingerprint_pre_migrate_verified"]["details"]["content_rehashed"] is False
assert runtime_events["runtime_fingerprint_final_verified"]["details"]["content_rehashed"] is True
assert runtime_events["runtime_fingerprint_initial_verified"]["details"][
    "runtime_fingerprint_report_sha256"
] == runtime_events["runtime_fingerprint_pre_migrate_verified"]["details"][
    "runtime_fingerprint_report_sha256"
]
stage_order = [event["stage"] for event in success_events]
assert stage_order.index("target_snapshot_set_final_verified") < stage_order.index(
    "runtime_fingerprint_final_verified"
)
assert stage_order.index("runtime_fingerprint_final_verified") < stage_order.index(
    "deployment_candidate_verified"
)
assert stage_order.index("source_final_verified") < stage_order.index(
    "execution_bundle_final_verified"
)
assert stage_order.index("execution_bundle_final_verified") < stage_order.index(
    "deployment_candidate_verified"
)
assert stage_order.index("deployment_candidate_verified") < stage_order.index("completed")

# The frozen bootstrap path reuses the bundle already created by the trusted
# outer process. Exercise that branch dynamically so artifact publication cannot
# depend on a local that exists only in the non-bootstrap snapshot branch.
bootstrap_config, bootstrap_source = build_case("bootstrap-success")
module._create_run_root(bootstrap_config)
bootstrap_bundle, bootstrap_files = module._create_execution_snapshot(
    repository_root,
    bootstrap_config.run_root / "code",
)
bootstrap_evidence = bootstrap_config.run_root / "evidence"
bootstrap_policy = bootstrap_evidence / "approved-policy.json"
bootstrap_policy.write_bytes(bootstrap_config.source_upgrade_policy.read_bytes())
bootstrap_proposal = bootstrap_evidence / "approved-proposal.json"
bootstrap_review = bootstrap_evidence / "approved-review.json"
write_json(bootstrap_review, {"fixture": "approved-review"})
bootstrap_record = bootstrap_evidence / "bootstrap.json"
write_json(bootstrap_record, {"fixture": "bootstrap"})
proposal_run_root = bootstrap_config.run_root.parent / "proposal-run"
proposal_run_root.mkdir()
proposal_artifacts = proposal_run_root / "artifacts"
proposal_artifacts.mkdir()
secondary_target = bootstrap_config.run_root.parent / "bootstrap-second-target"
secondary_target.mkdir()
source_media_root = bootstrap_config.run_root.parent / "bootstrap-source-media"
source_media_root.mkdir()
handoff_access = {
    "snapshot_sha256": "9" * 64,
    "scopes": [
        {"role": role}
        for role in (
            "database_backup_set",
            "source_media_root",
            "source_media_manifest",
            "target_media_root_1",
            "target_media_root_2",
        )
    ],
}
handoff_payload = {
    "database_backup_set": {
        "database": {
            "path": str(bootstrap_config.source_database),
            "size": bootstrap_config.source_database.stat().st_size,
            "sha256": file_hash(bootstrap_config.source_database),
        },
        "checksum": {
            "path": str(bootstrap_config.source_checksum),
            "size": bootstrap_config.source_checksum.stat().st_size,
            "sha256": file_hash(bootstrap_config.source_checksum),
        },
        "metadata": {
            "path": str(bootstrap_config.source_metadata),
            "size": bootstrap_config.source_metadata.stat().st_size,
            "sha256": file_hash(bootstrap_config.source_metadata),
        },
    },
    "source_media": {
        "root": str(source_media_root),
        "manifest": {"path": str(bootstrap_config.source_media_manifest)},
    },
    "rehearsal_targets": [
        {
            "slot": "first",
            "path": str(bootstrap_config.target_media_root),
            "snapshot_id": bootstrap_config.target_media_snapshot_id,
        },
        {
            "slot": "second",
            "path": str(secondary_target),
            "snapshot_id": "target-fixture-second",
        },
    ],
    "access_baseline": handoff_access,
    "limitations": {
        "tamper_proof": False,
        "continuous_acl_stability_proven": False,
        "offline_process_state": "operator_asserted",
        "trusted_operator_can_override_acl": True,
    },
}
module._assert_external_handoff_artifact_checkpoint(
    handoff_payload,
    "checksum",
    bootstrap_config.source_checksum.stat().st_size,
    file_hash(bootstrap_config.source_checksum),
)
try:
    module._assert_external_handoff_artifact_checkpoint(
        handoff_payload,
        "checksum",
        bootstrap_config.source_checksum.stat().st_size,
        "0" * 64,
    )
except module.RehearsalBlocked as exc:
    assert exc.code == "external_handoff_source_checksum_mismatch"
else:
    raise AssertionError("Handoff sidecar digest mismatch was accepted.")
handoff_path = proposal_artifacts / "source-handoff-manifest.json"
write_json(handoff_path, handoff_payload)
handoff_reference = {
    "path": "artifacts/source-handoff-manifest.json",
    "size": handoff_path.stat().st_size,
    "sha256": file_hash(handoff_path),
}
handoff_verification_trace = []
write_json(
    bootstrap_proposal,
    {"body": {"evidence": {"source_handoff_manifest": handoff_reference}}},
)
bootstrap_policy_payload = load_result(bootstrap_policy)
bootstrap_policy_payload["proposal_sha256"] = file_hash(bootstrap_proposal)
write_json(bootstrap_policy, bootstrap_policy_payload)
bootstrap_config = replace(
    bootstrap_config,
    repository_root=bootstrap_config.run_root / "code",
    source_upgrade_policy=bootstrap_policy,
    source_policy_proposal=bootstrap_proposal,
    source_review_record=bootstrap_review,
    source_proposal_run_root=proposal_run_root,
)
bootstrap_context = module.BootstrapInnerContext(
    run_root=bootstrap_config.run_root,
    record_path=bootstrap_record,
    record_sha256=file_hash(bootstrap_record),
    workspace_access_control="test_only_bootstrap_acl",
    execution_bundle_sha256=bootstrap_bundle,
    execution_bundle_files=bootstrap_files,
    policy_path=bootstrap_policy,
    proposal_path=bootstrap_proposal,
    review_path=bootstrap_review,
    repository_root=repository_root,
)


class FixtureHandoffVerifier:
    def __init__(self):
        self.live_verification_count = 0

    def load_handoff(self, path: Path):
        assert path == handoff_path
        return handoff_payload

    def verify_live_handoff(
        self,
        value,
        original_repository_root,
        disallowed_roots=(),
        verify_content=True,
    ):
        assert value == handoff_payload
        assert original_repository_root == repository_root
        assert bootstrap_config.run_root in disallowed_roots
        assert proposal_run_root in disallowed_roots
        assert handoff_path in disallowed_roots
        assert verify_content is False
        phase = (
            "preflight"
            if self.live_verification_count == 0
            else "final"
        )
        self.live_verification_count += 1
        assert self.live_verification_count <= 2
        handoff_verification_trace.append(f"{phase}_live_verified")
        return handoff_access

    @staticmethod
    def compare_access_baselines(expected, actual):
        assert expected == actual


fixture_handoff_verifier = FixtureHandoffVerifier()


def load_fixture_handoff_verifier(rehearsal):
    rehearsal.external_handoff_verifier = fixture_handoff_verifier
    return fixture_handoff_verifier


loaded_orchestrator = module.__file__
loaded_handoff_loader = module.Rehearsal._load_external_handoff_verifier
loaded_checkpoint = module._regular_file_checkpoint
loaded_record_artifact = module.Rehearsal._record_artifact


def trace_handoff_checkpoint(path, *args, **kwargs):
    result = loaded_checkpoint(path, *args, **kwargs)
    if (
        Path(path) == bootstrap_proposal
        and kwargs.get("expected_identity") is None
    ):
        assert kwargs.get("maximum_size") == 32 * 1024 * 1024
        handoff_verification_trace.append("approved_proposal_bounded_checkpoint")
    if Path(path) == handoff_path:
        if kwargs.get("expected_identity") is None:
            assert kwargs.get("maximum_size") == handoff_reference["size"]
            handoff_verification_trace.append("handoff_bounded_checkpoint")
        elif kwargs.get("expected_sha256") == handoff_reference["sha256"]:
            handoff_verification_trace.append("bound_checkpoint")
    return result


def trace_handoff_artifact(self, stage, path, **details):
    if stage in {
        "external_handoff_preflight_verified",
        "external_handoff_final_verified",
    }:
        handoff_verification_trace.append(stage)
    return loaded_record_artifact(self, stage, path, **details)


module.__file__ = str(
    bootstrap_config.repository_root
    / "ops"
    / "migration"
    / "Rehearse-ProductionCopy.py"
)
module.Rehearsal._load_external_handoff_verifier = load_fixture_handoff_verifier
module._regular_file_checkpoint = trace_handoff_checkpoint
module.Rehearsal._record_artifact = trace_handoff_artifact
try:
    bootstrap_status, bootstrap_result_path = module.run_rehearsal(
        bootstrap_config,
        runner=StubRunner(bootstrap_source),
        bootstrap_context=bootstrap_context,
    )
finally:
    module.__file__ = loaded_orchestrator
    module.Rehearsal._load_external_handoff_verifier = loaded_handoff_loader
    module._regular_file_checkpoint = loaded_checkpoint
    module.Rehearsal._record_artifact = loaded_record_artifact
assert bootstrap_status == "completed", load_result(bootstrap_result_path)
assert handoff_verification_trace.count("approved_proposal_bounded_checkpoint") == 1
assert handoff_verification_trace.count("handoff_bounded_checkpoint") == 1
assert handoff_verification_trace.index(
    "approved_proposal_bounded_checkpoint"
) < handoff_verification_trace.index("handoff_bounded_checkpoint")
assert handoff_verification_trace.index(
    "handoff_bounded_checkpoint"
) < handoff_verification_trace.index("preflight_live_verified")
for phase in ("preflight", "final"):
    live_index = handoff_verification_trace.index(f"{phase}_live_verified")
    artifact_index = handoff_verification_trace.index(
        f"external_handoff_{phase}_verified"
    )
    assert handoff_verification_trace[live_index + 1] == "bound_checkpoint"
    assert live_index < artifact_index
bootstrap_events = [
    json.loads(line)
    for line in (bootstrap_config.run_root / "evidence" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
]
bootstrap_source_artifacts = [
    event
    for event in bootstrap_events
    if event["stage"] == "source_artifacts_verified"
]
assert len(bootstrap_source_artifacts) == 1
assert bootstrap_source_artifacts[0]["outcome"] == "passed"
assert bootstrap_source_artifacts[0]["details"][
    "execution_bundle_sha256"
] == bootstrap_bundle
bootstrap_final_snapshot_events = [
    event
    for event in bootstrap_events
    if event["stage"] == "target_snapshot_set_final_verified"
]
assert len(bootstrap_final_snapshot_events) == 1
assert bootstrap_final_snapshot_events[0]["outcome"] == "passed"
assert bootstrap_final_snapshot_events[0]["details"][
    "backup_set_unchanged"
] is True
bootstrap_stage_order = [event["stage"] for event in bootstrap_events]
preflight_handoff_events = [
    event
    for event in bootstrap_events
    if event["stage"] == "external_handoff_preflight_verified"
]
final_handoff_events = [
    event
    for event in bootstrap_events
    if event["stage"] == "external_handoff_final_verified"
]
assert len(preflight_handoff_events) == 1
assert len(final_handoff_events) == 1
assert preflight_handoff_events[0]["details"]["active_target_slot"] == "first"
assert final_handoff_events[0]["details"]["active_target_slot"] == "first"
assert preflight_handoff_events[0]["details"]["handoff_sha256"] == file_hash(
    handoff_path
)
assert final_handoff_events[0]["details"]["handoff_sha256"] == file_hash(
    handoff_path
)
assert bootstrap_stage_order.index(
    "approved_policy_evidence_verified"
) < bootstrap_stage_order.index("external_handoff_preflight_verified")
assert bootstrap_stage_order.index(
    "target_snapshot_set_final_verified"
) < bootstrap_stage_order.index("runtime_fingerprint_final_verified")
assert bootstrap_stage_order.index(
    "runtime_fingerprint_final_verified"
) < bootstrap_stage_order.index("external_handoff_final_verified")
assert bootstrap_stage_order.index(
    "external_handoff_final_verified"
) < bootstrap_stage_order.index("deployment_candidate_verified")

# Exercise the embedded migration-state probe against a real temporary Django
# SQLite database. The production-copy workflow still uses stubs for all other
# commands so the contract test remains offline and deterministic.
real_state_root = test_root / "real-migration-state"
real_state_root.mkdir()
(real_state_root / "tmp").mkdir()
(real_state_root / "scratch-media").mkdir()
(real_state_root / "runtime-empty.env").write_bytes(b"")
real_state_database = real_state_root / "state.sqlite3"
real_state_output = real_state_root / "state.json"
real_state_env = module._django_environment(
    success_config,
    real_state_root,
    real_state_database,
)
completed = subprocess.run(
    [sys.executable, "-B", str(repository_root / "manage.py"), "migrate", "--noinput", "--verbosity", "0"],
    cwd=repository_root,
    env=real_state_env,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
completed = subprocess.run(
    [sys.executable, "-B", "-c", module.MIGRATION_STATE_SCRIPT, str(real_state_output)],
    cwd=repository_root,
    env=real_state_env,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
real_state = module._validate_migration_state(real_state_output)
assert real_state["applied_leaf_nodes"] == real_state["repository_leaf_nodes"], real_state

# Exercise the real quick-checkpoint script against synthetic authoritative
# reports. The positive case covers both virtual-environment and base-runtime
# top-level directory entries; each negative case removes an already-present
# directory from the expected set, which is equivalent to that directory being
# introduced after the full report was captured.
checkpoint_root = test_root / "runtime-checkpoint-script"
checkpoint_root.mkdir()
checkpoint_output_root = test_root / "runtime-checkpoint-output"
checkpoint_output_root.mkdir()
identity_scope = checkpoint_root / "identity-scope"
identity_scope.mkdir()
identity_file = identity_scope / "identity.bin"
identity_file.write_bytes(b"runtime identity\n")
standalone_identity_file = checkpoint_root / "standalone-runtime.bin"
standalone_identity_file.write_bytes(b"standalone runtime\n")

def top_level_runtime_entries(root: Path, marker: str) -> list[str]:
    entries: list[str] = []
    for candidate in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        relative = candidate.relative_to(root).as_posix()
        label = f"{marker}/{relative}"
        if candidate.is_dir():
            entries.append(label + "/")
        elif candidate.is_file():
            entries.append(label)
        else:
            raise AssertionError(f"Unexpected runtime entry: {candidate}")
    return sorted(entries)

def runtime_identity_entry(path: Path, label: str) -> dict[str, object]:
    metadata = path.stat()
    return {
        "path": label,
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
        "ctime_ns": metadata.st_ctime_ns,
    }

checkpoint_inventory = [
    runtime_identity_entry(
        identity_file,
        "$EXECUTION_ROOT/identity-scope/identity.bin",
    ),
    runtime_identity_entry(
        standalone_identity_file,
        "$EXECUTION_ROOT/standalone-runtime.bin",
    ),
]
for runtime_root, marker in (
    (Path(sys.prefix).resolve(), "$PREFIX"),
    (Path(sys.base_prefix).resolve(), "$BASE_PREFIX"),
):
    checkpoint_inventory.extend(
        runtime_identity_entry(candidate, f"{marker}/{candidate.name}")
        for candidate in sorted(
            runtime_root.iterdir(), key=lambda item: item.name.casefold()
        )
        if candidate.is_file()
    )

checkpoint_projection = {"fixture": "checkpoint-authority"}
checkpoint_fingerprint = module._canonical_json_sha256(checkpoint_projection)
checkpoint_payload = {
    "format": "ffxivshare-runtime-fingerprint",
    "format_version": 1,
    "fingerprint_sha256": checkpoint_fingerprint,
    "projection": checkpoint_projection,
    "checkpoint": {
        "format": "ffxivshare-runtime-identity-checkpoint-source",
        "format_version": 1,
        "content_hashed": True,
        "identity_inventory": sorted(
            checkpoint_inventory, key=lambda item: item["path"]
        ),
        "closure_scopes": sorted(
            [
                {
                    "root": "$EXECUTION_ROOT/identity-scope",
                    "mode": "top_level_entries",
                    "files": top_level_runtime_entries(
                        identity_scope, "$EXECUTION_ROOT/identity-scope"
                    ),
                },
                {
                    "root": "$PREFIX",
                    "mode": "top_level_entries",
                    "files": top_level_runtime_entries(Path(sys.prefix).resolve(), "$PREFIX"),
                },
                {
                    "root": "$BASE_PREFIX",
                    "mode": "top_level_entries",
                    "files": top_level_runtime_entries(
                        Path(sys.base_prefix).resolve(), "$BASE_PREFIX"
                    ),
                },
            ],
            key=lambda item: (item["root"], item["mode"]),
        ),
    },
}

def run_quick_checkpoint(
    source: Path,
    source_sha256: str,
    destination: Path,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            sys.executable,
            "-E",
            "-s",
            "-B",
            "-X",
            "utf8",
            "-c",
            module.RUNTIME_IDENTITY_CHECKPOINT_SCRIPT,
            str(source),
            checkpoint_fingerprint,
            source_sha256,
            str(destination),
        ],
        cwd=checkpoint_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

checkpoint_source = checkpoint_root / "authoritative.json"
write_json(checkpoint_source, checkpoint_payload)
checkpoint_source_sha256 = file_hash(checkpoint_source)
(
    checkpoint_bound_raw,
    checkpoint_bound_size,
    checkpoint_bound_sha256,
    checkpoint_bound_identity,
) = module._read_stable_regular_bytes(
    checkpoint_source,
    maximum_size=64 * 1024 * 1024,
    issue_prefix="checkpoint_contract_source",
)
checkpoint_bound_report = module._validate_runtime_fingerprint_bytes(
    checkpoint_bound_raw
)
assert checkpoint_bound_size == checkpoint_source.stat().st_size
assert checkpoint_bound_sha256 == checkpoint_source_sha256

def assert_invalid_checkpoint_report(payload: dict[str, object]) -> None:
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    try:
        module._validate_runtime_fingerprint_bytes(raw)
    except module.RehearsalError:
        pass
    else:
        raise AssertionError("Invalid runtime checkpoint report was accepted")

duplicate_root_payload = json.loads(json.dumps(checkpoint_payload))
duplicate_root_payload["checkpoint"]["closure_scopes"].append({
    "root": "$EXECUTION_ROOT/identity-scope",
    "mode": "recursive",
    "files": [],
})
duplicate_root_payload["checkpoint"]["closure_scopes"].sort(
    key=lambda item: (item["root"], item["mode"])
)
assert_invalid_checkpoint_report(duplicate_root_payload)

untracked_closure_payload = json.loads(json.dumps(checkpoint_payload))
identity_scope_payload = next(
    scope
    for scope in untracked_closure_payload["checkpoint"]["closure_scopes"]
    if scope["root"] == "$EXECUTION_ROOT/identity-scope"
)
identity_scope_payload["files"].append(
    "$EXECUTION_ROOT/identity-scope/untracked.py"
)
identity_scope_payload["files"].sort()
assert_invalid_checkpoint_report(untracked_closure_payload)

escaped_scope_payload = json.loads(json.dumps(checkpoint_payload))
identity_scope_payload = next(
    scope
    for scope in escaped_scope_payload["checkpoint"]["closure_scopes"]
    if scope["root"] == "$EXECUTION_ROOT/identity-scope"
)
identity_scope_payload["files"] = ["$EXECUTION_ROOT/outside.py"]
assert_invalid_checkpoint_report(escaped_scope_payload)

checkpoint_output = checkpoint_output_root / "checkpoint-pass.json"
completed = run_quick_checkpoint(
    checkpoint_source,
    checkpoint_source_sha256,
    checkpoint_output,
)
assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
module._validate_runtime_identity_checkpoint(
    checkpoint_output,
    expected_fingerprint_sha256=checkpoint_fingerprint,
    expected_source_report_sha256=checkpoint_source_sha256,
)

rewritten_payload = json.loads(checkpoint_source.read_text(encoding="utf-8"))
rewritten_payload["checkpoint"]["identity_inventory"][0]["mtime_ns"] += 1
write_json(checkpoint_source, rewritten_payload)
assert rewritten_payload["fingerprint_sha256"] == checkpoint_fingerprint
assert checkpoint_bound_report["checkpoint"]["identity_inventory"][0][
    "mtime_ns"
] != rewritten_payload["checkpoint"]["identity_inventory"][0]["mtime_ns"]
try:
    module._read_stable_regular_bytes(
        checkpoint_source,
        maximum_size=64 * 1024 * 1024,
        issue_prefix="checkpoint_contract_source",
        expected_sha256=checkpoint_bound_sha256,
        expected_identity=checkpoint_bound_identity,
    )
except module.RehearsalBlocked:
    pass
else:
    raise AssertionError("Post-read authority accepted a rewritten full report")
completed = run_quick_checkpoint(
    checkpoint_source,
    checkpoint_source_sha256,
    checkpoint_output_root / "checkpoint-rewritten.json",
)
assert completed.returncode != 0

for marker, name in (("$PREFIX", "prefix"), ("$BASE_PREFIX", "base-prefix")):
    missing_entry_payload = json.loads(json.dumps(checkpoint_payload))
    scope = next(
        item
        for item in missing_entry_payload["checkpoint"]["closure_scopes"]
        if item["root"] == marker
    )
    directory_entry = next(item for item in scope["files"] if item.endswith("/"))
    scope["files"].remove(directory_entry)
    missing_entry_source = checkpoint_root / f"missing-{name}-entry.json"
    write_json(missing_entry_source, missing_entry_payload)
    completed = run_quick_checkpoint(
        missing_entry_source,
        file_hash(missing_entry_source),
        checkpoint_output_root / f"checkpoint-missing-{name}.json",
    )
    assert completed.returncode != 0, marker

# Run the real full fingerprint script against a tiny synthetic runtime so the
# contract proves that an existing package resource directly under $PREFIX
# changes the canonical projection and final fingerprint even when its path and
# size do not change. It also proves quick closure rejects a newly added resource
# inside an already-active package. The wrapper preloads stdlib modules before
# replacing sys.path, so the probe stays offline and uses a frozen-sized root.
synthetic_parent = test_root / "synthetic-runtime"
synthetic_execution = synthetic_parent / "execution"
synthetic_prefix = synthetic_parent / "prefix"
synthetic_base = synthetic_parent / "base"
synthetic_quick_output = synthetic_parent / "quick-output"
synthetic_site = synthetic_prefix / "Lib" / "site-packages"
synthetic_base_lib = synthetic_base / "Lib"
synthetic_base_site = synthetic_base_lib / "site-packages"
synthetic_package = synthetic_prefix / "django"
synthetic_dormant = synthetic_prefix / "frontend"
for directory in (
    synthetic_execution,
    synthetic_quick_output,
    synthetic_site,
    synthetic_package,
    synthetic_dormant,
    synthetic_base,
    synthetic_base_site,
):
    directory.mkdir(parents=True, exist_ok=True)
(synthetic_execution / "requirements.txt").write_text("", encoding="utf-8")
(synthetic_prefix / "python.exe").write_bytes(b"synthetic-prefix-python\n")
synthetic_pyvenv = synthetic_prefix / "pyvenv.cfg"
synthetic_pyvenv.write_bytes(b"home = before\n")
(synthetic_base / "python.exe").write_bytes(b"synthetic-base-python\n")
(synthetic_base / "_sqlite3.pyd").write_bytes(b"synthetic-sqlite\n")
synthetic_stdlib_file = synthetic_base_lib / "stdlib_probe.py"
synthetic_stdlib_file.write_bytes(b"VALUE = 'stable'\n")
synthetic_site_package = synthetic_site / "venv_probe.py"
synthetic_site_package.write_bytes(b"VALUE = 'stable'\n")
synthetic_package_file = synthetic_package / "__init__.py"
synthetic_package_file.write_bytes(b"VALUE = 'stable'\n")
synthetic_resource_file = synthetic_package / "settings.json"
synthetic_resource_file.write_bytes(b'{"mode":"before"}\n')
(synthetic_dormant / "asset.json").write_text(
    '{"asset":true}\n', encoding="utf-8", newline="\n"
)
synthetic_wrapper = synthetic_execution / "run_full_fingerprint.py"
synthetic_wrapper.write_text(
    """from __future__ import annotations
import csv
from hashlib import sha256
import importlib.machinery
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import site
import sqlite3
import stat
import sys
import sysconfig
import _sqlite3

orchestrator, destination, prefix, base = map(Path, sys.argv[1:5])
options = sys.argv[5:]
activate_base_site = "--activate-base-site" in options
overlap_site_roots = "--overlap-site-roots" in options
tamper_candidates = [
    value
    for value in options
    if value not in {"--activate-base-site", "--overlap-site-roots"}
]
assert len(tamper_candidates) <= 1
tamper_after_fsync = (
    Path(tamper_candidates[0]).resolve(strict=False)
    if tamper_candidates
    else None
)
spec = importlib.util.spec_from_file_location("synthetic_rehearsal", orchestrator)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
prefix = prefix.resolve(strict=True)
base = base.resolve(strict=True)
site_root = (prefix / "Lib" / "site-packages").resolve(strict=True)
sys.prefix = str(prefix)
sys.base_prefix = str(base)
sys.executable = str(prefix / "python.exe")
sys._base_executable = str(base / "python.exe")
sys.path[:] = [
    str(Path.cwd().resolve()),
    str(base / "Lib"),
    str(base),
    str(prefix),
    str(site_root),
]
if activate_base_site:
    sys.path.insert(0, str(base / "Lib" / "site-packages"))
site.ENABLE_USER_SITE = False
site.getusersitepackages = lambda: str(prefix / "user-site")
site.getsitepackages = lambda: (
    [str(prefix), str(site_root)]
    if overlap_site_roots
    else [str(site_root)]
)
def synthetic_get_path(name, *, scheme=None, vars=None, expand=True):
    if vars is not None and name in {"purelib", "platlib"}:
        return str(base / "Lib" / "site-packages")
    return str(site_root)

sysconfig.get_path = synthetic_get_path
importlib.metadata.distributions = lambda: []
importlib.metadata.packages_distributions = lambda: {}
_sqlite3.__file__ = str(base / "_sqlite3.pyd")
if tamper_after_fsync is not None:
    original_fsync = os.fsync
    tampered = False

    def fsync_then_tamper(descriptor):
        global tampered
        original_fsync(descriptor)
        if not tampered:
            tamper_after_fsync.write_bytes(b"VALUE = 'drift!'\\n")
            tampered = True

    os.fsync = fsync_then_tamper
sys.argv = ["runtime-fingerprint", str(destination)]
exec(module.RUNTIME_FINGERPRINT_SCRIPT, {"__name__": "__main__"})
""",
    encoding="utf-8",
    newline="\n",
)
synthetic_quick_wrapper = synthetic_execution / "run_quick_fingerprint.py"
synthetic_quick_wrapper.write_text(
    """from __future__ import annotations
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
(
    orchestrator,
    source,
    expected_fingerprint,
    expected_source_sha256,
    destination,
    prefix,
    base,
) = arguments[:7]
tamper_after_fsync = Path(arguments[7]) if len(arguments) == 8 else None
assert len(arguments) in {7, 8}
spec = importlib.util.spec_from_file_location("synthetic_quick_rehearsal", orchestrator)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
prefix = Path(prefix).resolve(strict=True)
base = Path(base).resolve(strict=True)
sys.prefix = str(prefix)
sys.base_prefix = str(base)
sys.path[:] = [
    str(Path.cwd().resolve()),
    str(base / "Lib"),
    str(base),
    str(prefix),
    str(prefix / "Lib" / "site-packages"),
]
if tamper_after_fsync is not None:
    original_fsync = os.fsync
    tampered = False

    def fsync_then_tamper(descriptor):
        global tampered
        original_fsync(descriptor)
        if not tampered:
            tamper_after_fsync.write_bytes(b"VALUE = 'late'\\n")
            tampered = True

    os.fsync = fsync_then_tamper
sys.argv = [
    "runtime-checkpoint",
    source,
    expected_fingerprint,
    expected_source_sha256,
    destination,
]
exec(module.RUNTIME_IDENTITY_CHECKPOINT_SCRIPT, {"__name__": "__main__"})
""",
    encoding="utf-8",
    newline="\n",
)

def run_synthetic_full(
    destination: Path,
    *options: str,
) -> dict[str, object]:
    completed = subprocess.run(
        [
            sys.executable,
            "-E",
            "-s",
            "-B",
            "-X",
            "utf8",
            str(synthetic_wrapper),
            str(orchestrator_path),
            str(destination),
            str(synthetic_prefix),
            str(synthetic_base),
            *options,
        ],
        cwd=synthetic_execution,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(
        "utf-8", errors="replace"
    )
    return module._validate_runtime_fingerprint(destination)

def run_synthetic_quick(
    source: Path,
    source_report: dict[str, object],
    destination: Path,
    tamper_after_fsync: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            sys.executable,
            "-E",
            "-s",
            "-B",
            "-X",
            "utf8",
            str(synthetic_quick_wrapper),
            str(orchestrator_path),
            str(source),
            source_report["fingerprint_sha256"],
            file_hash(source),
            str(destination),
            str(synthetic_prefix),
            str(synthetic_base),
            *([str(tamper_after_fsync)] if tamper_after_fsync is not None else []),
        ],
        cwd=synthetic_execution,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

terminal_drift_output = synthetic_parent / "terminal-drift.json"
terminal_drift = subprocess.run(
    [
        sys.executable,
        "-E",
        "-s",
        "-B",
        "-X",
        "utf8",
        str(synthetic_wrapper),
        str(orchestrator_path),
        str(terminal_drift_output),
        str(synthetic_prefix),
        str(synthetic_base),
        str(synthetic_package_file),
    ],
    cwd=synthetic_execution,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
assert terminal_drift.returncode != 0
assert not terminal_drift_output.exists()
assert synthetic_package_file.read_bytes() == b"VALUE = 'drift!'\n", (
    terminal_drift.stderr.decode("utf-8", errors="replace")
)
synthetic_package_file.write_bytes(b"VALUE = 'stable'\n")
terminal_new_entry = synthetic_site / "full_fsync_late_probe.py"
terminal_new_entry_output = synthetic_parent / "terminal-new-entry.json"
terminal_new_entry_result = subprocess.run(
    [
        sys.executable,
        "-E",
        "-s",
        "-B",
        "-X",
        "utf8",
        str(synthetic_wrapper),
        str(orchestrator_path),
        str(terminal_new_entry_output),
        str(synthetic_prefix),
        str(synthetic_base),
        str(terminal_new_entry),
    ],
    cwd=synthetic_execution,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
assert terminal_new_entry_result.returncode != 0
assert terminal_new_entry.is_file(), terminal_new_entry_result.stderr.decode(
    "utf-8", errors="replace"
)
assert not terminal_new_entry_output.exists()
terminal_new_entry.unlink()
synthetic_before_source = synthetic_parent / "before.json"
synthetic_before = run_synthetic_full(synthetic_before_source)
synthetic_overlap_source = synthetic_parent / "before-overlapping-site-roots.json"
synthetic_overlap_before = run_synthetic_full(
    synthetic_overlap_source,
    "--overlap-site-roots",
)
overlap_site_projection = synthetic_overlap_before["projection"]["site_packages"]
expected_overlap_site_files = sorted(
    path
    for path in synthetic_prefix.rglob("*")
    if path.is_file()
)
assert overlap_site_projection["roots"] == ["$PREFIX"]
assert overlap_site_projection["file_count"] == len(expected_overlap_site_files)
assert overlap_site_projection["total_size"] == sum(
    path.stat().st_size for path in expected_overlap_site_files
)
recursive_modes = {
    "recursive",
    "recursive_import_entries",
    "recursive_package_entries",
}
overlap_recursive_scopes = [
    scope
    for scope in synthetic_overlap_before["checkpoint"]["closure_scopes"]
    if scope["mode"] in recursive_modes
]
overlap_recursive_roots = [scope["root"] for scope in overlap_recursive_scopes]
assert overlap_recursive_roots == sorted(set(overlap_recursive_roots))

def label_is_within(candidate: str, root: str) -> bool:
    return candidate == root or candidate.startswith(root.rstrip("/") + "/")

for index, left_root in enumerate(overlap_recursive_roots):
    for right_root in overlap_recursive_roots[index + 1:]:
        assert not label_is_within(left_root, right_root)
        assert not label_is_within(right_root, left_root)
overlap_recursive_entries = [
    entry
    for scope in overlap_recursive_scopes
    for entry in scope["files"]
]
assert len(overlap_recursive_entries) == len(set(overlap_recursive_entries))

synthetic_site_package.write_bytes(b"VALUE = 'drift!'\n")
synthetic_overlap_quick = run_synthetic_quick(
    synthetic_overlap_source,
    synthetic_overlap_before,
    synthetic_quick_output / "quick-overlapping-site-roots-drift.json",
)
assert synthetic_overlap_quick.returncode != 0
synthetic_overlap_changed = run_synthetic_full(
    synthetic_parent / "after-overlapping-site-roots-drift.json",
    "--overlap-site-roots",
)
assert (
    synthetic_overlap_before["fingerprint_sha256"]
    != synthetic_overlap_changed["fingerprint_sha256"]
)
synthetic_site_package.write_bytes(b"VALUE = 'stable'\n")
synthetic_overlap_restored_source = (
    synthetic_parent / "before-overlapping-site-roots-added-file.json"
)
synthetic_overlap_restored = run_synthetic_full(
    synthetic_overlap_restored_source,
    "--overlap-site-roots",
)
assert (
    synthetic_overlap_before["fingerprint_sha256"]
    == synthetic_overlap_restored["fingerprint_sha256"]
)

synthetic_overlap_added_file = synthetic_site / "late_overlap_probe.py"
synthetic_overlap_added_file.write_bytes(b"VALUE = 'late'\n")
synthetic_overlap_quick = run_synthetic_quick(
    synthetic_overlap_restored_source,
    synthetic_overlap_restored,
    synthetic_quick_output / "quick-overlapping-site-roots-added.json",
)
assert synthetic_overlap_quick.returncode != 0
synthetic_overlap_added = run_synthetic_full(
    synthetic_parent / "after-overlapping-site-roots-added.json",
    "--overlap-site-roots",
)
assert (
    synthetic_overlap_before["fingerprint_sha256"]
    != synthetic_overlap_added["fingerprint_sha256"]
)
synthetic_overlap_added_file.unlink()
synthetic_overlap_fsync_source = (
    synthetic_parent / "before-overlapping-site-roots-fsync-drift.json"
)
synthetic_overlap_fsync = run_synthetic_full(
    synthetic_overlap_fsync_source,
    "--overlap-site-roots",
)
fsync_tamper_file = synthetic_site / "fsync_late_probe.py"
fsync_tamper_output = synthetic_quick_output / "quick-fsync-directory-drift.json"
synthetic_overlap_quick = run_synthetic_quick(
    synthetic_overlap_fsync_source,
    synthetic_overlap_fsync,
    fsync_tamper_output,
    tamper_after_fsync=fsync_tamper_file,
)
assert synthetic_overlap_quick.returncode != 0
assert fsync_tamper_file.is_file(), synthetic_overlap_quick.stderr.decode(
    "utf-8", errors="replace"
)
assert not fsync_tamper_output.exists()
fsync_tamper_file.unlink()
synthetic_overlap_post_fsync_source = (
    synthetic_parent / "before-overlapping-site-roots-post-fsync-drift.json"
)
synthetic_overlap_post_fsync = run_synthetic_full(
    synthetic_overlap_post_fsync_source,
    "--overlap-site-roots",
)
synthetic_overlap_quick = run_synthetic_quick(
    synthetic_overlap_post_fsync_source,
    synthetic_overlap_post_fsync,
    synthetic_quick_output / "quick-overlapping-site-roots-restored.json",
)
assert synthetic_overlap_quick.returncode == 0, synthetic_overlap_quick.stderr.decode(
    "utf-8", errors="replace"
)
synthetic_before_source = (
    synthetic_parent / "before-after-overlapping-site-roots.json"
)
synthetic_before = run_synthetic_full(synthetic_before_source)

base_runtime_closure = synthetic_before["projection"]["python"][
    "base_runtime_closure"
]
assert base_runtime_closure["excluded_inactive_site_package_roots"] == [
    "$BASE_PREFIX/Lib/site-packages"
]
assert not any(
    item["path"].startswith("$BASE_PREFIX/Lib/site-packages/")
    for item in synthetic_before["checkpoint"]["identity_inventory"]
)
prefix_scope = next(
    scope
    for scope in synthetic_before["checkpoint"]["closure_scopes"]
    if scope["root"] == "$PREFIX" and scope["mode"] == "top_level_entries"
)
assert "$PREFIX/pyvenv.cfg" in prefix_scope["files"]
package_scope = next(
    scope
    for scope in synthetic_before["checkpoint"]["closure_scopes"]
    if scope["root"] == "$PREFIX/django"
)
assert package_scope["mode"] == "recursive_package_entries"
assert "$PREFIX/django/settings.json" in package_scope["files"]
dormant_scope = next(
    scope
    for scope in synthetic_before["checkpoint"]["closure_scopes"]
    if scope["root"] == "$PREFIX/frontend"
)
assert dormant_scope["mode"] == "recursive_import_entries"
assert "$PREFIX/frontend/asset.json" not in dormant_scope["files"]
inactive_base_package = synthetic_base_site / "external_probe"
inactive_base_package.mkdir()
inactive_base_package_file = inactive_base_package / "__init__.py"
inactive_base_package_file.write_bytes(b"VALUE = 'external'\n")
synthetic_quick = subprocess.run(
    [
        sys.executable,
        "-E",
        "-s",
        "-B",
        "-X",
        "utf8",
        str(synthetic_quick_wrapper),
        str(orchestrator_path),
        str(synthetic_before_source),
        synthetic_before["fingerprint_sha256"],
        file_hash(synthetic_before_source),
        str(synthetic_quick_output / "quick-inactive-base-site.json"),
        str(synthetic_prefix),
        str(synthetic_base),
    ],
    cwd=synthetic_execution,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
assert synthetic_quick.returncode == 0, synthetic_quick.stderr.decode(
    "utf-8", errors="replace"
)
synthetic_inactive_after = run_synthetic_full(
    synthetic_parent / "after-inactive-base-site.json"
)
assert synthetic_before["fingerprint_sha256"] == synthetic_inactive_after[
    "fingerprint_sha256"
]
assert synthetic_before["checkpoint"] == synthetic_inactive_after["checkpoint"]
assert not any(
    item["path"].startswith("$BASE_PREFIX/Lib/site-packages/")
    for item in synthetic_inactive_after["checkpoint"]["identity_inventory"]
)
inactive_base_package_file.write_bytes(b"VALUE = 'changed!'\n")
synthetic_inactive_changed = run_synthetic_full(
    synthetic_parent / "after-inactive-base-site-change.json"
)
assert synthetic_before["fingerprint_sha256"] == synthetic_inactive_changed[
    "fingerprint_sha256"
]
assert synthetic_before["checkpoint"] == synthetic_inactive_changed["checkpoint"]

active_base_site_output = synthetic_parent / "active-base-site.json"
active_base_site = subprocess.run(
    [
        sys.executable,
        "-E",
        "-s",
        "-B",
        "-X",
        "utf8",
        str(synthetic_wrapper),
        str(orchestrator_path),
        str(active_base_site_output),
        str(synthetic_prefix),
        str(synthetic_base),
        "--activate-base-site",
    ],
    cwd=synthetic_execution,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
assert active_base_site.returncode != 0
assert not active_base_site_output.exists()
assert b"base Python site-packages directory is active" in active_base_site.stderr

synthetic_stdlib_file.write_bytes(b"VALUE = 'drift!'\n")
synthetic_quick = subprocess.run(
    [
        sys.executable,
        "-E",
        "-s",
        "-B",
        "-X",
        "utf8",
        str(synthetic_quick_wrapper),
        str(orchestrator_path),
        str(synthetic_before_source),
        synthetic_before["fingerprint_sha256"],
        file_hash(synthetic_before_source),
        str(synthetic_quick_output / "quick-base-stdlib-drift.json"),
        str(synthetic_prefix),
        str(synthetic_base),
    ],
    cwd=synthetic_execution,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
assert synthetic_quick.returncode != 0
synthetic_stdlib_after = run_synthetic_full(
    synthetic_parent / "after-base-stdlib-drift.json"
)
assert synthetic_before["fingerprint_sha256"] != synthetic_stdlib_after[
    "fingerprint_sha256"
]
synthetic_stdlib_file.write_bytes(b"VALUE = 'stable'\n")

synthetic_site_package.write_bytes(b"VALUE = 'drift!'\n")
synthetic_quick = subprocess.run(
    [
        sys.executable,
        "-E",
        "-s",
        "-B",
        "-X",
        "utf8",
        str(synthetic_quick_wrapper),
        str(orchestrator_path),
        str(synthetic_before_source),
        synthetic_before["fingerprint_sha256"],
        file_hash(synthetic_before_source),
        str(synthetic_quick_output / "quick-venv-site-drift.json"),
        str(synthetic_prefix),
        str(synthetic_base),
    ],
    cwd=synthetic_execution,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
assert synthetic_quick.returncode != 0
synthetic_site_after = run_synthetic_full(
    synthetic_parent / "after-venv-site-drift.json"
)
assert synthetic_before["fingerprint_sha256"] != synthetic_site_after[
    "fingerprint_sha256"
]
synthetic_site_package.write_bytes(b"VALUE = 'stable'\n")

late_resource = synthetic_package / "late.json"
late_resource.write_text('{"late":true}\n', encoding="utf-8", newline="\n")
synthetic_quick = subprocess.run(
    [
        sys.executable,
        "-E",
        "-s",
        "-B",
        "-X",
        "utf8",
        str(synthetic_quick_wrapper),
        str(orchestrator_path),
        str(synthetic_before_source),
        synthetic_before["fingerprint_sha256"],
        file_hash(synthetic_before_source),
        str(synthetic_quick_output / "quick-new-resource.json"),
        str(synthetic_prefix),
        str(synthetic_base),
    ],
    cwd=synthetic_execution,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
assert synthetic_quick.returncode != 0
late_resource.unlink()
dormant_code = synthetic_dormant / "__init__.py"
dormant_code.write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
synthetic_quick = subprocess.run(
    [
        sys.executable,
        "-E",
        "-s",
        "-B",
        "-X",
        "utf8",
        str(synthetic_quick_wrapper),
        str(orchestrator_path),
        str(synthetic_before_source),
        synthetic_before["fingerprint_sha256"],
        file_hash(synthetic_before_source),
        str(synthetic_quick_output / "quick-new-code.json"),
        str(synthetic_prefix),
        str(synthetic_base),
    ],
    cwd=synthetic_execution,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
assert synthetic_quick.returncode != 0
dormant_code.unlink()
synthetic_resource_file.write_bytes(b'{"mode":"after!"}\n')
assert synthetic_resource_file.stat().st_size == len(b'{"mode":"before"}\n')
synthetic_after = run_synthetic_full(synthetic_parent / "after.json")
assert synthetic_before["fingerprint_sha256"] != synthetic_after["fingerprint_sha256"]
before_closure = synthetic_before["projection"]["python"]["sys_path_import_closure"]
after_closure = synthetic_after["projection"]["python"]["sys_path_import_closure"]
assert before_closure["file_count"] == after_closure["file_count"]
assert before_closure["total_size"] == after_closure["total_size"]
assert before_closure["files_sha256"] != after_closure["files_sha256"]
synthetic_resource_file.write_bytes(b'{"mode":"before"}\n')
synthetic_pyvenv.write_bytes(b"home = after!\n")
assert synthetic_pyvenv.stat().st_size == len(b"home = before\n")
synthetic_top_level_after = run_synthetic_full(
    synthetic_parent / "after-top-level.json"
)
assert (
    synthetic_before["fingerprint_sha256"]
    != synthetic_top_level_after["fingerprint_sha256"]
)
top_level_after_closure = synthetic_top_level_after["projection"]["python"][
    "sys_path_import_closure"
]
assert before_closure["file_count"] == top_level_after_closure["file_count"]
assert before_closure["total_size"] == top_level_after_closure["total_size"]
assert before_closure["files_sha256"] != top_level_after_closure["files_sha256"]

failure_config, failure_source = build_case("failure")
failure_runner = StubRunner(failure_source, fail_stage="dataset_validated")
status, result_path = module.run_rehearsal(failure_config, runner=failure_runner)
assert status == "failed"
assert (failure_config.run_root / "artifacts" / "stub-failure-marker.txt").is_file()
assert load_result(result_path)["failed_artifacts_retained"] is True

interrupt_config, interrupt_source = build_case("interrupt")
status, result_path = module.run_rehearsal(
    interrupt_config,
    runner=StubRunner(interrupt_source, interrupt_stage="dataset_validated"),
)
assert status == "interrupted"
assert load_result(result_path)["issues"] == ["keyboard_interrupt"]
interrupt_events = [
    json.loads(line)
    for line in (interrupt_config.run_root / "evidence" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
]
assert any(
    event["stage"] == "dataset_validated" and event["outcome"] == "interrupted"
    for event in interrupt_events
)

checkpoint_config, checkpoint_source = build_case("checkpoint-interrupt")
original_checkpoint = module._source_snapshot_checkpoint
def interrupt_checkpoint(*_args, **_kwargs):
    raise KeyboardInterrupt()
module._source_snapshot_checkpoint = interrupt_checkpoint
try:
    status, result_path = module.run_rehearsal(
        checkpoint_config,
        runner=StubRunner(checkpoint_source),
    )
finally:
    module._source_snapshot_checkpoint = original_checkpoint
checkpoint_result = load_result(result_path)
assert status == "interrupted"
assert checkpoint_result["source_database_unchanged"] is None
checkpoint_events = [
    json.loads(line)
    for line in (checkpoint_config.run_root / "evidence" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
]
assert any(
    event["stage"] == "source_final_verified"
    and event["outcome"] == "not_proven"
    and event["details"]["issue"] == "source_initial_identity_not_established"
    for event in checkpoint_events
)

wrong_import_config, wrong_import_source = build_case("wrong-import")
status, result_path = module.run_rehearsal(
    wrong_import_config,
    runner=StubRunner(wrong_import_source, first_import_status="already_imported"),
)
assert status == "failed"
assert "import_report_invalid" in load_result(result_path)["issues"]

injection_config, injection_source = build_case("bundle-injection")
status, result_path = module.run_rehearsal(
    injection_config,
    runner=StubRunner(
        injection_source,
        inject_execution_entry_stage="source_schema_plan",
    ),
)
assert status == "blocked"
assert "execution_snapshot_closure_changed" in load_result(result_path)["issues"]

tamper_config, tamper_source = build_case("tamper")
tamper_runner = StubRunner(tamper_source, tamper_stage="dataset_exported")
status, result_path = module.run_rehearsal(tamper_config, runner=tamper_runner)
assert status == "blocked"
assert "source_changed_during_rehearsal" in load_result(result_path)["issues"]

media_drift_config, media_drift_source = build_case("target-media-drift")
status, result_path = module.run_rehearsal(
    media_drift_config,
    runner=StubRunner(
        media_drift_source,
        target_media_drift_after_initial=True,
    ),
)
assert status == "blocked"
assert "final_media_snapshot_mismatch" in load_result(result_path)["issues"]
media_drift_events = [
    json.loads(line)
    for line in (media_drift_config.run_root / "evidence" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
]
assert not any(
    event["stage"] == "deployment_candidate_verified"
    for event in media_drift_events
)

runtime_report_rewrite_config, runtime_report_rewrite_source = build_case(
    "runtime-report-rewrite"
)
status, result_path = module.run_rehearsal(
    runtime_report_rewrite_config,
    runner=StubRunner(
        runtime_report_rewrite_source,
        rewrite_runtime_report_stage="runtime_fingerprint_pre_migrate",
    ),
)
assert status == "blocked"
rewrite_result = load_result(result_path)
assert any(
    issue.startswith("runtime_fingerprint_report_")
    for issue in rewrite_result["issues"]
), rewrite_result

runtime_content_drift_config, runtime_content_drift_source = build_case(
    "runtime-content-drift"
)
status, result_path = module.run_rehearsal(
    runtime_content_drift_config,
    runner=StubRunner(
        runtime_content_drift_source,
        mutate_runtime_projection_at_final=True,
    ),
)
assert status == "blocked"
assert "runtime_fingerprint_mismatch" in load_result(result_path)["issues"]

policy_config, policy_source = build_case("policy", bad_plan=True)
status, result_path = module.run_rehearsal(policy_config, runner=StubRunner(policy_source))
assert status == "blocked"
assert "policy_migration_plan_sha256_mismatch" in load_result(result_path)["issues"]
assert any((policy_config.run_root / "logs").glob("*-source_schema_plan.stdout.txt"))

bundle_config, bundle_source = build_case("bundle-policy", bad_bundle=True)
status, result_path = module.run_rehearsal(bundle_config, runner=StubRunner(bundle_source))
assert status == "blocked"
assert "policy_execution_bundle_sha256_mismatch" in load_result(result_path)["issues"]

reuse_config, _reuse_source = build_case("reuse")
reuse_config.run_root.mkdir()
marker = reuse_config.run_root / "user-marker.txt"
marker.write_text("keep\n", encoding="utf-8")
try:
    module.run_rehearsal(reuse_config, runner=StubRunner(_reuse_source))
except module.ConfigurationError:
    pass
else:
    raise AssertionError("Existing run root was reused")
assert marker.read_text(encoding="utf-8") == "keep\n"

print("Production-copy rehearsal contract tests passed.")
'@

try {
    Set-Content -LiteralPath $fixtureScript -Value $fixtureSource -Encoding UTF8
    $fixtureOutput = [System.Collections.Generic.List[string]]::new()
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $PythonExecutable `
            -I -B -X utf8 `
            $fixtureScript $RepositoryRoot $orchestrator $temporaryRoot 2>&1 |
            ForEach-Object {
                $line = $_.ToString()
                $fixtureOutput.Add($line)
                Write-Host $line
            }
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $global:LASTEXITCODE = 0
    if ($exitCode -ne 0 -and $env:GITHUB_ACTIONS -eq 'true') {
        $details = (
            $fixtureOutput |
            Select-Object -Last 40
        ) -join "`n"
        if ($details.Length -gt 6000) {
            $details = $details.Substring($details.Length - 6000)
        }
        $escapedDetails = $details.
            Replace('%', '%25').
            Replace("`r", '%0D').
            Replace("`n", '%0A')
        Write-Host (
            '::error title=Production-copy rehearsal details::' +
            $escapedDetails
        )
    }
    Assert-Contract `
        -Condition ($exitCode -eq 0) `
        -Message "Production-copy rehearsal contract test failed with exit code $exitCode."

    $wrapper = Join-Path $PSScriptRoot 'Invoke-ProductionCopyRehearsal.ps1'
    $powershell = Join-Path $PSHOME 'powershell.exe'
    foreach ($expectedCode in @(0, 1, 2, 130)) {
        $nativeStub = Join-Path $temporaryRoot "exit-$expectedCode.cmd"
        Set-Content `
            -LiteralPath $nativeStub `
            -Value "@echo off`r`nexit /b $expectedCode`r`n" `
            -Encoding ASCII
        $placeholder = Join-Path $temporaryRoot 'placeholder'
        $previousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            & $powershell -NoLogo -NoProfile -NonInteractive -File $wrapper `
                -RepositoryRoot $RepositoryRoot `
                -PythonExecutable $nativeStub `
                -SourceDatabase $placeholder `
                -SourceChecksum $placeholder `
                -SourceMetadata $placeholder `
                -SourceUpgradePolicy $placeholder `
                -SourcePolicyProposal $placeholder `
                -SourcePolicyReview $placeholder `
                -SourceProposalRunRoot $placeholder `
                -SourceMediaManifest $placeholder `
                -TargetMediaRoot $placeholder `
                -TargetMediaSnapshotId 'wrapper-test' `
                -RunRoot $placeholder `
                -ConfirmSourceImmutable `
                -ConfirmTargetMediaOffline
            $wrapperExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousPreference
        }
        $global:LASTEXITCODE = 0
        Assert-Contract `
            -Condition ($wrapperExitCode -eq $expectedCode) `
            -Message "Wrapper returned $wrapperExitCode; expected $expectedCode."
    }
}
finally {
    Remove-TestRoot -Path $temporaryRoot
}
