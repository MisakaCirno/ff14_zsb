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
        -Condition ((Split-Path -Leaf $resolved) -match '^ffxivshare-rehearsal-pair-[a-f0-9]{32}$') `
        -Message 'Refusing to clean an unexpected pair-verifier test directory.'
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
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "Dedicated virtual-environment Python is missing: $venvPython"
    }
    $PythonExecutable = (Resolve-Path -LiteralPath $venvPython).Path
}
else {
    $PythonExecutable = (Resolve-Path -LiteralPath $PythonExecutable).Path
}

$verifier = Join-Path $PSScriptRoot 'Verify-ProductionCopyRehearsalPair.py'
Assert-Contract `
    -Condition (Test-Path -LiteralPath $verifier -PathType Leaf) `
    -Message "Production-copy rehearsal-pair verifier is missing: $verifier"

$temporaryRoot = Join-Path `
    ([System.IO.Path]::GetTempPath()) `
    ('ffxivshare-rehearsal-pair-' + [Guid]::NewGuid().ToString('N'))
$fixtureScript = Join-Path $temporaryRoot 'test_rehearsal_pair_verifier.py'
New-Item -ItemType Directory -Path $temporaryRoot | Out-Null

$fixtureSource = @'
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any


assert sys.flags.isolated
assert sys.flags.no_site
assert sys.flags.no_user_site
assert sys.flags.ignore_environment
assert sys.flags.dont_write_bytecode
assert sys.flags.utf8_mode


verifier_path = Path(sys.argv[1]).resolve()
test_root = Path(sys.argv[2]).resolve()
spec = importlib.util.spec_from_file_location(
    "ffxivshare_rehearsal_pair_verifier_contract",
    verifier_path,
)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

inspector_path = verifier_path.with_name("Inspect-SQLiteSnapshot.py")
inspector_spec = importlib.util.spec_from_file_location(
    "ffxivshare_schema_inspector_contract",
    inspector_path,
)
assert inspector_spec is not None and inspector_spec.loader is not None
inspector = importlib.util.module_from_spec(inspector_spec)
sys.modules[inspector_spec.name] = inspector
inspector_spec.loader.exec_module(inspector)


def expect_failure(action, contains: str | None = None) -> None:
    try:
        action()
    except module.PairVerificationError as exc:
        if contains is not None:
            assert contains in str(exc), (contains, str(exc))
    else:
        raise AssertionError("Pair verification unexpectedly succeeded")


def canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def snapshot(path: Path, raw: bytes):
    return module.approval_core.FileSnapshot(
        path=path,
        raw=raw,
        size=len(raw),
        sha256=sha256(raw).hexdigest(),
        identity=(1, 2, len(raw), 3, 4),
    )


python_executable = Path(sys.executable).resolve()
python_bytes = python_executable.read_bytes()
python_identity = {
    "cache_tag": sys.implementation.cache_tag,
    "executable": str(python_executable),
    "executable_sha256": sha256(python_bytes).hexdigest(),
    "executable_size": len(python_bytes),
    "implementation": module.platform.python_implementation(),
    "isolation_flags": {
        "ignore_environment": True,
        "no_user_site": True,
        "dont_write_bytecode": True,
        "utf8_mode": True,
        "isolated": True,
        "no_site": True,
        "safe_path": True,
    },
    "version": module.platform.python_version(),
}
module._validate_current_python_identity(python_identity)
wrong_python_identity = deepcopy(python_identity)
wrong_python_identity["version"] = "0.0.0"
expect_failure(
    lambda: module._validate_current_python_identity(wrong_python_identity),
    "recorded Python identity",
)


def ledger_bytes() -> bytes:
    previous = "0" * 64
    rows = []
    for sequence, stage in enumerate(module.SUCCESS_STAGE_SEQUENCE, start=1):
        outcome = "terminal" if stage == "completed" else "passed"
        details = {}
        if stage == "created":
            details = {"run_id": "run-one"}
        elif stage == "deployment_candidate_verified":
            details = {"cutover_authorized": False}
        elif stage == "completed":
            details = {
                "status": "completed",
                "issues": [],
                "cutover_authorized": False,
            }
        event = {
            "format": "ffxivshare-production-copy-rehearsal-event",
            "format_version": 1,
            "sequence": sequence,
            "generated_at": f"2026-07-17T00:{sequence:02d}:00Z",
            "stage": stage,
            "outcome": outcome,
            "previous_event_sha256": previous,
            "details": details,
        }
        digest = sha256(canonical(event)).hexdigest()
        event["event_sha256"] = digest
        rows.append(canonical(event))
        previous = digest
    return b"".join(rows)


ledger_raw = ledger_bytes()
ledger_snapshot = snapshot(test_root / "events.jsonl", ledger_raw)
ledger = module._replay_rehearsal_ledger(ledger_snapshot)
assert ledger.event_count == len(module.SUCCESS_STAGE_SEQUENCE)
assert ledger.events[-1]["stage"] == "completed"
assert ledger.head_event_sha256 == ledger.events[-1]["event_sha256"]


noncanonical_lines = ledger_raw.splitlines(keepends=True)
noncanonical_event = json.loads(noncanonical_lines[0])
noncanonical_lines[0] = (json.dumps(noncanonical_event, indent=2) + "\n").encode("utf-8")
expect_failure(
    lambda: module._replay_rehearsal_ledger(
        snapshot(test_root / "noncanonical-events.jsonl", b"".join(noncanonical_lines))
    )
)

damaged_lines = ledger_raw.splitlines(keepends=True)
damaged_event = json.loads(damaged_lines[1])
damaged_event["event_sha256"] = "f" * 64
damaged_lines[1] = canonical(damaged_event)
expect_failure(
    lambda: module._replay_rehearsal_ledger(
        snapshot(test_root / "damaged-events.jsonl", b"".join(damaged_lines))
    )
)


result = {
    "format": "ffxivshare-production-copy-rehearsal",
    "format_version": 1,
    "generated_at": "2026-07-17T00:04:00Z",
    "status": "completed",
    "completed_stages": list(module.SUCCESS_STAGE_SEQUENCE[:-1]),
    "issues": [],
    "evidence_chain": {
        "event_count": ledger.event_count,
        "head_event_sha256": ledger.head_event_sha256,
        "ledger": {
            "path": "evidence/events.jsonl",
            "size": ledger_snapshot.size,
            "sha256": ledger_snapshot.sha256,
        },
        "verification": "self_consistent_local_chain",
        "tamper_proof": False,
    },
    "source_immutable_snapshot_attested": True,
    "source_database_unchanged": True,
    "failed_artifacts_retained": False,
    "workspace_access_control": "private_dacl",
    "network_isolation_enforced": False,
    "network_access_observation": "not_measured",
    "live_production_service_access_requested_by_orchestrator": False,
    "production_copy_read_performed": True,
    "contains_production_user_data": True,
    "retained_on_success": True,
    "secure_disposal_required": True,
    "sensitive_retention_scope": "entire_run_root",
    "sensitive_retention_directories": ["."],
    "cutover_authorized": False,
}
result_raw = canonical(result)
result_snapshot = snapshot(test_root / "result.json", result_raw)
module._validate_result(
    result,
    result_snapshot=result_snapshot,
    ledger_snapshot=ledger_snapshot,
    ledger=ledger,
    expected_workspace_access_control="private_dacl",
)

mismatched_result = deepcopy(result)
mismatched_result["evidence_chain"]["head_event_sha256"] = "a" * 64
mismatched_raw = canonical(mismatched_result)
expect_failure(
    lambda: module._validate_result(
        mismatched_result,
        result_snapshot=snapshot(test_root / "mismatched-result.json", mismatched_raw),
        ledger_snapshot=ledger_snapshot,
        ledger=ledger,
        expected_workspace_access_control="private_dacl",
    )
)


# Strict backup-set, restriction-report and physical-schema contracts reject
# structurally plausible evidence when its detailed checks or sidecars drift.
backup_root = test_root / "backup-contract"
backup_root.mkdir()
database_path = backup_root / "contract.sqlite3"
connection = sqlite3.connect(database_path)
try:
    connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT UNIQUE)")
    connection.execute("CREATE TABLE sqliteXboundary (id INTEGER PRIMARY KEY)")
    connection.execute("CREATE INDEX sample_value_idx ON sample(value)")
    connection.commit()
finally:
    connection.close()
database_raw = database_path.read_bytes()
database_sha256 = sha256(database_raw).hexdigest()
database_snapshot = module._stable_large_snapshot(
    database_path,
    expected_size=len(database_raw),
    expected_sha256=database_sha256,
    label="contract database",
)
hardlink_path = backup_root / "contract-hardlink.sqlite3"
try:
    hardlink_path.hardlink_to(database_path)
    expect_failure(
        lambda: module._stable_large_snapshot(
            database_path,
            expected_size=len(database_raw),
            expected_sha256=database_sha256,
            label="hard-linked contract database",
        ),
        "single-link",
    )
finally:
    if hardlink_path.exists():
        hardlink_path.unlink()
checksum_path = database_path.with_suffix(database_path.suffix + ".sha256")
checksum_path.write_bytes(
    f"{database_sha256}  {database_path.name}\n".encode("utf-8")
)
metadata_path = database_path.with_suffix(database_path.suffix + ".metadata.json")
metadata = {
    "application_version": "contract",
    "backup_method": module.backup_core.BACKUP_METHOD,
    "database_vendor": module.backup_core.DATABASE_VENDOR,
    "foreign_key_check": "ok",
    "generated_at": "2026-07-17T00:00:00.000000Z",
    "integrity_check": "ok",
    "schema_version": 1,
    "sha256": database_sha256,
    "size": len(database_raw),
}
metadata_path.write_bytes(canonical(metadata))
checksum_snapshot = module.approval_core._stable_snapshot(
    checksum_path,
    maximum_size=16 * 1024,
    label="contract checksum",
)
metadata_snapshot = module.approval_core._stable_snapshot(
    metadata_path,
    maximum_size=64 * 1024,
    label="contract metadata",
)
module._validate_backup_sidecars(
    database_snapshot,
    checksum_snapshot,
    metadata_snapshot,
    label="contract backup set",
)
live_sidecar = Path(f"{database_path}-wal")
live_sidecar.write_bytes(b"unexpected live state")
expect_failure(
    lambda: module._validate_backup_sidecars(
        database_snapshot,
        checksum_snapshot,
        metadata_snapshot,
        label="contract backup set with WAL",
    ),
    "live SQLite sidecar",
)
live_sidecar.unlink()
schema_projection = module._sqlite_schema_projection(
    database_path,
    label="contract database",
)
assert {row["type"] for row in schema_projection} == {"index", "table"}
assert "sqliteXboundary" in {row["name"] for row in schema_projection}

schema_connection = sqlite3.connect(
    database_path.resolve().as_uri() + "?mode=ro&immutable=1",
    uri=True,
)
try:
    schema_inventory = inspector._sqlite_schema_inventory(schema_connection)
finally:
    schema_connection.close()
schema_sha256 = schema_inventory["sha256"]
assert module.rehearsal_core._validate_sqlite_schema_inventory(
    deepcopy(schema_inventory)
) == schema_sha256
assert module.approval_core._validate_sqlite_schema_inventory(
    deepcopy(schema_inventory)
) == schema_sha256

schema_mutations = []
extra_key = deepcopy(schema_inventory)
extra_key["unexpected"] = True
schema_mutations.append(extra_key)
bad_metadata = deepcopy(schema_inventory)
bad_metadata["normalization"]["sql"] = "normalized"
schema_mutations.append(bad_metadata)
bad_order = deepcopy(schema_inventory)
bad_order["objects"].reverse()
bad_order["sha256"] = inspector._canonical_json_sha256(
    inspector._schema_inventory_projection(bad_order)
)
schema_mutations.append(bad_order)
bad_digest = deepcopy(schema_inventory)
bad_digest["sha256"] = "0" * 64
schema_mutations.append(bad_digest)
for candidate in schema_mutations:
    for validator, error_type in (
        (
            module.rehearsal_core._validate_sqlite_schema_inventory,
            module.rehearsal_core.RehearsalError,
        ),
        (
            module.approval_core._validate_sqlite_schema_inventory,
            module.approval_core.ApprovalError,
        ),
    ):
        try:
            validator(deepcopy(candidate))
        except error_type:
            pass
        else:
            raise AssertionError("Malformed schema inventory passed a consumer validator")

backup_report_path = backup_root / "verification.json"
backup_report = {
    "artifact": {
        "producer_generated_at": "2026-07-17T00:00:00.000000Z",
        "sha256": database_sha256,
        "size": len(database_raw),
    },
    "checks": {
        "checksum_bytes_exact": True,
        "input_set_unchanged": True,
        "metadata_contract": True,
        "sqlite_magic": True,
    },
    "cutover_authorized": False,
    "format": module.backup_core.REPORT_FORMAT,
    "format_version": module.backup_core.REPORT_FORMAT_VERSION,
    "generated_at": "2026-07-17T00:00:01.000000Z",
    "inspection_required": True,
    "verified": True,
}
backup_report_path.write_bytes(canonical(backup_report))
backup_report_snapshot = module.approval_core._stable_snapshot(
    backup_report_path,
    maximum_size=module.MAX_JSON_BYTES,
    label="contract backup report",
)
assert module._validate_backup_report_snapshot(
    backup_report_snapshot,
    label="contract backup report",
) == backup_report
failed_backup_report = deepcopy(backup_report)
failed_backup_report["checks"]["metadata_contract"] = False
backup_report_path.write_bytes(canonical(failed_backup_report))
failed_backup_snapshot = module.approval_core._stable_snapshot(
    backup_report_path,
    maximum_size=module.MAX_JSON_BYTES,
    label="failed contract backup report",
)
expect_failure(
    lambda: module._validate_backup_report_snapshot(
        failed_backup_snapshot,
        label="failed contract backup report",
    ),
    "checks",
)

restriction_path = test_root / "restriction.json"
restriction_report = {
    "blocking_errors": [],
    "counts": {
        "active_restrictions_missing_actor": 0,
        "legacy_private_reviews": 0,
        "private_clear_shares": 0,
        "reports": 1,
        "resolved_report_shares": 0,
        "shares": 1,
    },
    "generated_at": "2026-07-17T00:00:00+00:00",
    "manual_review": {
        "categories": [],
        "count": 0,
        "reason": "Operator review is required and recorded separately.",
        "share_ids": [],
    },
    "ready_for_cutover": True,
    "restriction_states": {"clear": 1},
    "status_visibility": [
        {"count": 1, "status": "approved", "visibility": "public"}
    ],
    "valid": True,
}
restriction_path.write_bytes(canonical(restriction_report))
restriction_snapshot = module.approval_core._stable_snapshot(
    restriction_path,
    maximum_size=module.MAX_JSON_BYTES,
    label="contract restriction report",
)
restriction_projection = module._validate_restriction_report(
    restriction_snapshot,
    label="contract restriction report",
)
assert "generated_at" not in restriction_projection
missing_counts = deepcopy(restriction_report)
del missing_counts["counts"]["reports"]
restriction_path.write_bytes(canonical(missing_counts))
missing_counts_snapshot = module.approval_core._stable_snapshot(
    restriction_path,
    maximum_size=module.MAX_JSON_BYTES,
    label="incomplete restriction report",
)
expect_failure(
    lambda: module._validate_restriction_report(
        missing_counts_snapshot,
        label="incomplete restriction report",
    ),
    "counts",
)
inconsistent_counts = deepcopy(restriction_report)
inconsistent_counts["status_visibility"][0]["count"] = 2
restriction_path.write_bytes(canonical(inconsistent_counts))
inconsistent_counts_snapshot = module.approval_core._stable_snapshot(
    restriction_path,
    maximum_size=module.MAX_JSON_BYTES,
    label="inconsistent restriction report",
)
expect_failure(
    lambda: module._validate_restriction_report(
        inconsistent_counts_snapshot,
        label="inconsistent restriction report",
    ),
    "aggregate counts",
)


media_manifest_path = test_root / "producer-media-manifest.json"
media_manifest = {
    "format": module.media_core.MANIFEST_FORMAT,
    "format_version": module.media_core.MANIFEST_VERSION,
    "generated_at": "2026-07-17T00:00:00.000000Z",
    "hash_algorithm": module.media_core.HASH_ALGORITHM,
    "path_normalization": "unicode_nfc_canonical_caseless_unique",
    "source_snapshot": {"id": "contract-snapshot", "offline_confirmed": True},
    "file_count": 0,
    "total_size": 0,
    "files": [],
}
media_manifest_path.write_text(
    json.dumps(media_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
    newline="\n",
)
media_manifest_snapshot = module.approval_core._stable_snapshot(
    media_manifest_path,
    maximum_size=module.MAX_JSON_BYTES,
    label="producer media manifest",
)
parsed_media_manifest = module._strict_json(
    media_manifest_snapshot,
    label="producer media manifest",
    canonical=False,
)
assert module.media_core._load_manifest(media_manifest_path) == parsed_media_manifest
assert module._media_projection(
    parsed_media_manifest,
    label="producer media manifest",
) == {"file_count": 0, "total_size": 0, "files": []}


semantic_projection = {
    "approved_policy_sha256": "1" * 64,
    "source_handoff_sha256": "2" * 64,
    "source_sqlite_schema_sha256": "5" * 64,
    "final_target_dataset": {
        "entities": {
            "shares": {
                "count": 1,
                "file": "shares.jsonl",
                "model": "shares.share",
                "sha256": "3" * 64,
            }
        }
    },
    "final_target_media": {
        "files": [{"path": "uploads/example.bin", "size": 4, "sha256": "4" * 64}]
    },
    "migration_states": {
        "final_target": {"applied": [["shares", "0001_initial"]]}
    },
    "target_backup_inspection": {"integrity_check": "ok"},
    "database_structure_preservation": {"preserved": True},
}
comparison = module._compare_semantic_projections(
    semantic_projection,
    deepcopy(semantic_projection),
)
assert comparison["matched"] is True
assert comparison["issues"] == []
assert comparison["semantic_projection_sha256"] == sha256(
    canonical(semantic_projection)
).hexdigest()
assert set(comparison["matched_projections"]) == {
    "source_sqlite_schema_sha256",
    "entity_inventory_sha256",
    "media_inventory_sha256",
    "applied_migrations_sha256",
    "target_backup_semantics_sha256",
    "database_structure_preservation_sha256",
}

for mismatched_key in (
    "approved_policy_sha256",
    "source_handoff_sha256",
    "source_sqlite_schema_sha256",
    "final_target_dataset",
    "final_target_media",
    "migration_states",
    "target_backup_inspection",
    "database_structure_preservation",
):
    mismatched_projection = deepcopy(semantic_projection)
    mismatched_projection[mismatched_key] = {"changed": True}
    expect_failure(
        lambda value=mismatched_projection: module._compare_semantic_projections(
            semantic_projection,
            value,
        ),
        mismatched_key,
    )


# Isolate the target-slot gate before any evidence payload is read. A valid
# pair must consume the exact first/second slots frozen in the handoff.
slot_root = test_root / "slot-root"
slot_root.mkdir()
loaded_existing_directory = module.approval_core._existing_directory
loaded_private_directory = module._validate_private_directory
loaded_directory_identities = module.approval_core._run_directory_identities
loaded_snapshot_tracker = module.approval_core.SnapshotTracker
loaded_bootstrap_validator = module._validate_bootstrap
try:
    module.approval_core._existing_directory = lambda path, **_kwargs: Path(path)
    module._validate_private_directory = lambda *_args, **_kwargs: None
    module.approval_core._run_directory_identities = lambda _path: {}
    module.approval_core.SnapshotTracker = lambda: object()
    module._validate_bootstrap = lambda **_kwargs: (None, {}, {}, {})
    expect_failure(
        lambda: module._validate_run(
            label="second",
            expected_slot="second",
            run_root=slot_root,
            api=object(),
            policy={},
            policy_snapshot=None,
            proposal_snapshot=None,
            review_snapshot=None,
            proposal_run_root=test_root / "proposal",
            handoff={"rehearsal_targets": [{"slot": "first"}]},
            handoff_sha256="6" * 64,
        ),
        "second target slot",
    )
finally:
    module.approval_core._existing_directory = loaded_existing_directory
    module._validate_private_directory = loaded_private_directory
    module.approval_core._run_directory_identities = loaded_directory_identities
    module.approval_core.SnapshotTracker = loaded_snapshot_tracker
    module._validate_bootstrap = loaded_bootstrap_validator


proposal_root = test_root / "proposal-root"
first_root = test_root / "first-root"
second_root = test_root / "second-root"
for path in (proposal_root, first_root, second_root):
    path.mkdir()

base_config = {
    "proposal_run_root": proposal_root,
    "policy": test_root / "policy.json",
    "proposal": test_root / "proposal.json",
    "review": test_root / "review.json",
    "expected_policy_sha256": "7" * 64,
    "expected_proposal_sha256": "8" * 64,
    "expected_review_sha256": "9" * 64,
}
expect_failure(
    lambda: module.verify_rehearsal_pair(
        module.PairVerificationConfig(
            first_run_root=first_root,
            second_run_root=first_root,
            output=test_root / "same-root-output.json",
            **base_config,
        )
    ),
    "distinct and non-overlapping",
)

existing_output = test_root / "already-exists.json"
existing_output.write_bytes(b"occupied\n")
loaded_same_path = module._same_path
try:
    # The output-exists gate runs before any Proposal/Rehearsal evidence is read.
    # Bypass only the frozen-tool location assertion to isolate that gate.
    module._same_path = lambda *_args: True
    expect_failure(
        lambda: module.verify_rehearsal_pair(
            module.PairVerificationConfig(
                first_run_root=first_root,
                second_run_root=second_root,
                output=existing_output,
                **base_config,
            )
        ),
        "output parent is invalid",
    )
finally:
    module._same_path = loaded_same_path

print("Production-copy rehearsal-pair verifier fast contracts passed.")
'@

$exitCode = 1
try {
    Set-Content -LiteralPath $fixtureScript -Value $fixtureSource -Encoding UTF8
    & $PythonExecutable `
        -I -S -B -X utf8 `
        $fixtureScript `
        $verifier `
        $temporaryRoot
    $pythonExitCode = $LASTEXITCODE
    $global:LASTEXITCODE = 0
    Assert-Contract `
        -Condition ($pythonExitCode -eq 0) `
        -Message "Rehearsal-pair verifier contracts failed with exit code $pythonExitCode."
    $exitCode = 0
}
finally {
    Remove-TestRoot -Path $temporaryRoot
}

exit $exitCode
