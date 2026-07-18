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
        -Condition ((Split-Path -Leaf $resolved) -match '^ffxivshare-policy-approval-[a-f0-9]{32}$') `
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
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "Dedicated virtual-environment Python is missing: $venvPython"
    }
    $PythonExecutable = (Resolve-Path -LiteralPath $venvPython).Path
}
else {
    $PythonExecutable = (Resolve-Path -LiteralPath $PythonExecutable).Path
}

$approvalTool = Join-Path $PSScriptRoot 'Approve-ProductionCopyPolicy.py'
$bootstrapTool = Join-Path $PSScriptRoot 'ProductionCopyBootstrap.py'
$rehearsalTool = Join-Path $PSScriptRoot 'Rehearse-ProductionCopy.py'
$handoffTool = Join-Path $PSScriptRoot 'ProductionCopyHandoff.py'
foreach ($tool in @($approvalTool, $bootstrapTool, $rehearsalTool, $handoffTool)) {
    Assert-Contract `
        -Condition (Test-Path -LiteralPath $tool -PathType Leaf) `
        -Message "Production-copy policy contract dependency is missing: $tool"
}

$temporaryRoot = Join-Path `
    ([System.IO.Path]::GetTempPath()) `
    ('ffxivshare-policy-approval-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
$fixtureScript = Join-Path $temporaryRoot 'test_policy_approval.py'

$fixtureSource = @'
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import sys
from types import SimpleNamespace
from typing import Any


python = Path(sys.argv[1]).resolve()
approval_tool = Path(sys.argv[2]).resolve()
bootstrap_tool = Path(sys.argv[3]).resolve()
rehearsal_tool = Path(sys.argv[4]).resolve()
handoff_tool = Path(sys.argv[5]).resolve()
root = Path(sys.argv[6]).resolve()


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bootstrap = load_module(bootstrap_tool, "policy_contract_bootstrap")
rehearsal = load_module(rehearsal_tool, "policy_contract_rehearsal")
handoff = load_module(handoff_tool, "policy_contract_handoff")
approval = load_module(approval_tool, "policy_contract_approval")
approval._assert_isolated_stdlib_runtime()
production_private_validator = approval._assert_private_approval_directory
production_output_validator = approval._assert_private_output_file
# The managed Windows test token can create files but lacks WRITE_DAC on its
# temporary directory. Keep production DACL validation intact and inject only
# the already-private test assertion into in-process contract calls.
approval._assert_private_approval_directory = lambda _root, _approval: None
approval._assert_private_output_file = lambda _path, _approval: None


def encoded(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def digest_value(value: Any) -> str:
    return sha256(encoded(value)).hexdigest()


def compact_digest_value(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(raw).hexdigest()


def write_json(path: Path, value: Any, *, canonical: bool = True) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if canonical:
        raw = encoded(value)
    else:
        raw = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
    path.write_bytes(raw)
    return sha256(raw).hexdigest()


def write_bytes(path: Path, raw: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return sha256(raw).hexdigest()


def reference(run_root: Path, path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    return {
        "path": path.relative_to(run_root).as_posix(),
        "size": len(raw),
        "sha256": sha256(raw).hexdigest(),
    }


MANDATORY = (
    "manage.py",
    "requirements.txt",
    "ops/migration/ProductionCopyBootstrap.py",
    "ops/migration/Rehearse-ProductionCopy.py",
    "ops/migration/Approve-ProductionCopyPolicy.py",
    "ops/migration/Propose-ProductionCopyPolicy.py",
    "ops/migration/ProductionCopyHandoff.py",
    "ops/migration/Verify-SQLiteBackupSet.py",
    "ops/migration/Inspect-SQLiteSnapshot.py",
    "ops/migration/Compare-SiteDataExports.py",
    "ops/migration/MediaManifest.py",
    "ops/migration/Verify-ProductionCopyRehearsalPair.py",
    "ffxivshare/__init__.py",
    "shares/__init__.py",
)
SOURCE_NODE = ["shares", "0024_widen_site_message_titles"]
TARGET_NODE = ["shares", "0025_add_collection_owner_index"]
SOURCE_DATABASE_SHA256 = "1" * 64
MIGRATION_RUNTIME_SHA256 = "4" * 64
IDENTITY = "operator_asserted_not_cryptographically_verified"
HANDOFF_SCOPE_ROLES = (
    "database_backup_set",
    "source_media_root",
    "source_media_manifest",
    "target_media_root_1",
    "target_media_root_2",
)


@dataclass
class Fixture:
    run_root: Path
    proposal_path: Path
    proposal_sha256: str
    review_path: Path
    review_sha256: str
    output_path: Path
    proposal: dict[str, Any]
    review: dict[str, Any]


def make_event(
    sequence: int,
    previous: str,
    generated_at: str,
    stage: str,
    outcome: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    event = {
        "format": "ffxivshare-production-copy-rehearsal-event",
        "format_version": 1,
        "sequence": sequence,
        "generated_at": generated_at,
        "stage": stage,
        "outcome": outcome,
        "previous_event_sha256": previous,
        "details": details,
    }
    event["event_sha256"] = digest_value(event)
    return event


def make_handoff(media_reference: dict[str, Any]) -> dict[str, Any]:
    owner_sid = "S-1-5-21-1000"
    aces = [{"type": 0, "flags": 0, "mask": 0xA0000000, "sid": owner_sid}]
    dacl_sha256 = digest_value(aces)

    def scope(
        role: str,
        path: str,
        *,
        entry_count: int,
        directory_count: int,
        file_count: int,
        total_size: int,
        file_id: str,
    ) -> dict[str, Any]:
        ancestors = [
            {
                "path": "D:\\",
                "volume_serial_number": "1234abcd",
                "file_id": "0000000000000001",
                "owner_sid": owner_sid,
                "dacl_protected": True,
                "dacl_sha256": dacl_sha256,
                "aces": aces,
            }
        ]
        if role == "database_backup_set":
            node_rows = [
                (".", "directory", 0),
                ("production.sqlite3", "file", 4096),
                ("production.sqlite3.metadata.json", "file", 256),
                ("production.sqlite3.sha256", "file", 65),
            ]
        elif role == "source_media_manifest":
            node_rows = [(".", "file", total_size)]
        else:
            node_rows = [(".", "directory", 0)]
        nodes = []
        for index, (relative_path, kind, size) in enumerate(node_rows):
            nodes.append(
                {
                    "relative_path": relative_path,
                    "kind": kind,
                    "volume_serial_number": "1234abcd",
                    "file_id": f"{int(file_id, 16) + index:016x}",
                    "attributes": 0x10 if kind == "directory" else 0x20,
                    "last_write_time": 133970112000000000 + index,
                    "size": size,
                    "link_count": 1,
                    "owner_sid": owner_sid,
                    "dacl_protected": True,
                    "dacl_sha256": dacl_sha256,
                }
            )
        assert len(nodes) == entry_count
        reconstructed = [{**node, "aces": aces} for node in nodes]
        return {
            "role": role,
            "path": path,
            "root": {
                "volume_serial_number": "1234abcd",
                "file_id": file_id,
                "owner_sid": owner_sid,
                "dacl_protected": True,
                "dacl_sha256": dacl_sha256,
            },
            "ancestor_chain": ancestors,
            "ancestor_chain_sha256": digest_value(ancestors),
            "dacl_inventory": [
                {
                    "dacl_sha256": dacl_sha256,
                    "aces": aces,
                    "node_count": entry_count,
                }
            ],
            "node_inventory": nodes,
            "owner_inventory": [
                {"owner_sid": owner_sid, "node_count": entry_count}
            ],
            "tree_sha256": digest_value(reconstructed),
            "entry_count": entry_count,
            "directory_count": directory_count,
            "file_count": file_count,
            "total_size": total_size,
        }

    database_path = r"D:\Fixture\Database\production.sqlite3"
    source_media_root = r"D:\Fixture\SourceMedia"
    manifest_path = r"D:\Fixture\Manifest\source-media-manifest.json"
    target_one = r"D:\Fixture\TargetOne"
    target_two = r"D:\Fixture\TargetTwo"
    scopes = [
        scope(
            "database_backup_set",
            r"D:\Fixture\Database",
            entry_count=4,
            directory_count=1,
            file_count=3,
            total_size=4096 + 65 + 256,
            file_id="0000000000000010",
        ),
        scope(
            "source_media_root",
            source_media_root,
            entry_count=1,
            directory_count=1,
            file_count=0,
            total_size=0,
            file_id="0000000000000020",
        ),
        scope(
            "source_media_manifest",
            manifest_path,
            entry_count=1,
            directory_count=0,
            file_count=1,
            total_size=media_reference["size"],
            file_id="0000000000000030",
        ),
        scope(
            "target_media_root_1",
            target_one,
            entry_count=1,
            directory_count=1,
            file_count=0,
            total_size=0,
            file_id="0000000000000040",
        ),
        scope(
            "target_media_root_2",
            target_two,
            entry_count=1,
            directory_count=1,
            file_count=0,
            total_size=0,
            file_id="0000000000000050",
        ),
    ]
    access_projection = {
        "verification": "windows_acl_snapshot",
        "scope_policy": "sealed_read_only_v1",
        "scopes": scopes,
    }
    value = {
        "format": "ffxivshare-production-copy-handoff",
        "format_version": 1,
        "generated_at": "2026-07-16T00:01:30Z",
        "source": {
            "host": "fixture-production-host",
            "captured_at": "2026-07-16T00:00:00Z",
            "operator": "fixture-operator",
            "operator_identity_verification": IDENTITY,
            "release_application_version": "fixture-release-20260716",
            "database_media_consistency": (
                "operator_asserted_same_capture_window_not_cryptographically_verified"
            ),
            "source_immutable": "operator_asserted",
            "target_media_offline": "operator_asserted",
        },
        "database_backup_set": {
            "database": {
                "path": database_path,
                "sha256": SOURCE_DATABASE_SHA256,
                "size": 4096,
            },
            "checksum": {
                "path": f"{database_path}.sha256",
                "sha256": "c" * 64,
                "size": 65,
            },
            "metadata": {
                "path": f"{database_path}.metadata.json",
                "sha256": "d" * 64,
                "size": 256,
            },
        },
        "source_media": {
            "root": source_media_root,
            "snapshot_id": "fixture-media",
            "manifest": {
                "path": manifest_path,
                "sha256": media_reference["sha256"],
                "size": media_reference["size"],
                "generated_at": "2026-07-16T00:01:00Z",
                "snapshot_id": "fixture-media",
                "file_count": 0,
                "total_size": 0,
            },
        },
        "rehearsal_targets": [
            {"slot": "first", "path": target_one, "snapshot_id": "fixture-target-1"},
            {"slot": "second", "path": target_two, "snapshot_id": "fixture-target-2"},
        ],
        "access_baseline": {
            **access_projection,
            "snapshot_sha256": digest_value(access_projection),
        },
        "limitations": {
            "tamper_proof": False,
            "continuous_acl_stability_proven": False,
            "offline_process_state": "operator_asserted",
            "trusted_operator_can_override_acl": True,
        },
    }
    assert handoff.validate_handoff(value) == value
    return value


def build_fixture(
    name: str,
    *,
    pending: bool = True,
    include_bundle_final: bool = True,
    include_handoff_evidence: bool = True,
    include_handoff_initial: bool = True,
    duplicate_handoff_initial: bool = False,
    handoff_initial_after_body: bool = False,
    exact_handoff_initial: bool = True,
    include_handoff_final: bool = True,
    duplicate_handoff_final: bool = False,
    handoff_final_before_body: bool = False,
    exact_handoff_final: bool = True,
    proposal_version: int = 2,
    proposal_body_version: int = 2,
    completion_exit: int = 0,
    secure: bool = True,
) -> Fixture:
    run_root = root / name
    run_root.mkdir()
    if secure:
        if os.name != "nt":
            os.chmod(run_root, 0o700)
    for directory in (
        "approval",
        "artifacts",
        "code",
        "evidence",
        "logs",
        "scratch-media",
        "target",
        "tmp",
        "work",
    ):
        (run_root / directory).mkdir()

    code_root = run_root / "code"
    for relative in MANDATORY:
        path = code_root / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative == "ops/migration/Approve-ProductionCopyPolicy.py":
            shutil.copyfile(approval_tool, path)
        else:
            path.write_text(
                "from __future__ import annotations\n",
                encoding="utf-8",
                newline="\n",
            )
    inventory_files = []
    for path in sorted(candidate for candidate in code_root.rglob("*") if candidate.is_file()):
        raw = path.read_bytes()
        inventory_files.append(
            {
                "path": path.relative_to(code_root).as_posix(),
                "size": len(raw),
                "sha256": sha256(raw).hexdigest(),
            }
        )
    bundle_sha256 = digest_value(inventory_files)
    inventory_path = run_root / "evidence" / "execution-bundle.json"
    write_json(
        inventory_path,
        {
            "format": "ffxivshare-production-copy-execution-bundle",
            "format_version": 1,
            "execution_bundle_sha256": bundle_sha256,
            "files": inventory_files,
        },
    )

    migration_plan_path = run_root / "logs" / "06-proposal_migration_plan.stdout.txt"
    write_bytes(
        migration_plan_path,
        b"Planned operations:\n  shares.0025_add_collection_owner_index AddIndex\n",
    )
    runtime_projection = {"fixture": "isolated-runtime", "version": 1}
    runtime_path = run_root / "evidence" / "proposal-runtime-fingerprint.json"
    runtime_sha256 = digest_value(runtime_projection)
    write_json(
        runtime_path,
        {
            "format": "ffxivshare-runtime-fingerprint",
            "format_version": 1,
            "fingerprint_sha256": runtime_sha256,
            "projection": runtime_projection,
            "checkpoint": {
                "format": "ffxivshare-runtime-identity-checkpoint-source",
                "format_version": 1,
                "content_hashed": True,
                "identity_inventory": [
                    {
                        "path": "python:python.exe",
                        "size": 1024,
                        "device": 1,
                        "inode": 2,
                        "mtime_ns": 3,
                        "ctime_ns": 4,
                    }
                ],
                "closure_scopes": [
                    {
                        "root": "site-packages:0",
                        "mode": "recursive",
                        "files": ["package/__init__.py"],
                    }
                ],
            },
        },
    )
    state_path = run_root / "evidence" / "proposal-source-migration-state.json"
    state = {
        "format": "ffxivshare-migration-state",
        "format_version": 1,
        "database_vendor": "sqlite",
        "applied": [SOURCE_NODE],
        "applied_leaf_nodes": [SOURCE_NODE],
        "repository_leaf_nodes": [TARGET_NODE],
        "unknown_applied_nodes": [],
        "python_version": "3.11.9",
        "django_version": "5.2.16",
        "migration_runtime_sha256": MIGRATION_RUNTIME_SHA256,
    }
    write_json(state_path, state)
    pending_items = (
        [
            {
                "node": TARGET_NODE,
                "module": "shares.migrations.0025_add_collection_owner_index",
                "dependencies": [SOURCE_NODE],
                "replaces": [],
                "operations": [
                    "django.db.migrations.operations.models.AddIndex"
                ],
            }
        ]
        if pending
        else []
    )
    review_plan_path = run_root / "evidence" / "proposal-migration-review-plan.json"
    write_json(
        review_plan_path,
        {
            "format": "ffxivshare-migration-review-plan",
            "format_version": 1,
            "database_vendor": "sqlite",
            "source_applied_migrations": [SOURCE_NODE],
            "target_leaf_nodes": [TARGET_NODE],
            "pending_migrations": pending_items,
        },
    )
    backup_path = run_root / "evidence" / "proposal-source-backup-set.json"
    write_json(
        backup_path,
        {
            "format": "ffxivshare-sqlite-backup-set-verification",
            "format_version": 1,
            "generated_at": "2026-07-16T00:02:00Z",
            "verified": True,
            "cutover_authorized": False,
            "inspection_required": True,
            "artifact": {
                "producer_generated_at": "2026-07-16T00:00:00Z",
                "sha256": SOURCE_DATABASE_SHA256,
                "size": 4096,
            },
            "checks": {
                "checksum_bytes_exact": True,
                "input_set_unchanged": True,
                "metadata_contract": True,
                "sqlite_magic": True,
            },
        },
    )
    inspection_path = run_root / "evidence" / "proposal-source-inspection.json"
    schema_inventory = {
        "format": "ffxivshare-sqlite-schema-inventory",
        "format_version": 1,
        "schema": "main",
        "included_object_types": ["index", "table", "trigger", "view"],
        "excluded_objects": {
            "name_prefix": "sqlite_",
            "comparison": "case-sensitive Unicode code-point prefix match",
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
                "name": "django_migrations",
                "tbl_name": "django_migrations",
                "sql": (
                    "CREATE TABLE django_migrations "
                    "(id integer PRIMARY KEY, app varchar(255), name varchar(255))"
                ),
            }
        ],
    }
    schema_inventory_sha256 = compact_digest_value(schema_inventory)
    schema_inventory["sha256"] = schema_inventory_sha256
    write_json(
        inspection_path,
        {
            "format": "ffxivshare-sqlite-snapshot-inspection",
            "format_version": 1,
            "generated_at": "2026-07-16T00:03:00Z",
            "database": {
                "path": "immutable-source.sqlite3",
                "size_bytes": 4096,
                "sha256": SOURCE_DATABASE_SHA256,
                "sha256_before": SOURCE_DATABASE_SHA256,
                "sha256_after": SOURCE_DATABASE_SHA256,
                "source_unchanged": True,
                "header": {},
            },
            "inspection": {
                "query_only": True,
                "integrity_check": "ok",
                "foreign_key_check": {"status": "ok", "violations": 0},
                "sqlite_schema": schema_inventory,
                "django_migrations": {
                    "present": True,
                    "applied": [
                        {
                            "app": SOURCE_NODE[0],
                            "name": SOURCE_NODE[1],
                            "applied": "2026-07-01T00:00:00Z",
                        }
                    ],
                },
            },
        },
        canonical=False,
    )
    media_path = run_root / "artifacts" / "source-media-manifest.json"
    media_sha256 = write_json(
        media_path,
        {
            "format": "ffxivshare-media-manifest",
            "format_version": 2,
            "generated_at": "2026-07-16T00:01:00Z",
            "hash_algorithm": "sha256",
            "path_normalization": "unicode_nfc_canonical_caseless_unique",
            "source_snapshot": {"id": "fixture-media", "offline_confirmed": True},
            "file_count": 0,
            "total_size": 0,
            "files": [],
        },
        canonical=False,
    )
    media_reference = reference(run_root, media_path)
    handoff_value = make_handoff(media_reference)
    handoff_path = run_root / "artifacts" / "source-handoff-manifest.json"
    write_json(handoff_path, handoff_value)
    handoff_reference = reference(run_root, handoff_path)

    bootstrap_path = run_root / "evidence" / "bootstrap.json"
    inventory_reference = reference(run_root, inventory_path)
    bootstrap_payload = {
        "format": "ffxivshare-production-copy-bootstrap",
        "format_version": 1,
        "generated_at": "2026-07-16T00:00:00Z",
        "run_id": "20260717T000000000000Z-fixture",
        "bootstrap_nonce": "b" * 64,
        "workspace_access_control": (
            "windows_protected_dacl_current_user_system_administrators_full_control_"
            "with_parent_chain_delete_acl_review"
            if os.name == "nt"
            else "posix_mode_0700"
        ),
        "python": {},
        "configuration": {
            "inner_arguments": [],
            "inner_entrypoint": "ops/migration/Propose-ProductionCopyPolicy.py",
            "mode": "policy-proposal",
            "repository_root": str(code_root),
            "run_root": str(run_root),
        },
        "run_layout": [],
        "policy": None,
        "approval_inputs": None,
        "execution_bundle": {
            "authority": "stable_repository_consistency",
            "expected_sha256": bundle_sha256,
            "frozen_sha256": bundle_sha256,
            "manifest": inventory_reference,
        },
        "bootstrap_trusted_not_frozen": True,
        "source_data_read_by_bootstrap": False,
        "media_read_by_bootstrap": False,
    }
    write_json(bootstrap_path, bootstrap_payload)

    evidence = {
        "bootstrap": reference(run_root, bootstrap_path),
        "execution_inventory": inventory_reference,
        "migration_plan": reference(run_root, migration_plan_path),
        "migration_review_plan": reference(run_root, review_plan_path),
        "runtime_fingerprint": reference(run_root, runtime_path),
        "source_backup_verification": reference(run_root, backup_path),
    }
    if include_handoff_evidence:
        evidence["source_handoff_manifest"] = handoff_reference
    evidence.update(
        {
            "source_media_manifest": media_reference,
            "source_migration_state": reference(run_root, state_path),
            "source_snapshot_inspection": reference(run_root, inspection_path),
        }
    )
    evidence_set_sha256 = digest_value(evidence)
    projection = {
        "format": "ffxivshare-source-upgrade-policy",
        "format_version": 2,
        "policy_id": f"{name}-policy",
        "source_database_sha256": SOURCE_DATABASE_SHA256,
        "source_media_manifest_sha256": media_sha256,
        "source_media_snapshot_id": "fixture-media",
        "source_applied_migrations_sha256": digest_value([SOURCE_NODE]),
        "source_sqlite_schema_sha256": schema_inventory_sha256,
        "migration_runtime_sha256": MIGRATION_RUNTIME_SHA256,
        "runtime_fingerprint_sha256": runtime_sha256,
        "execution_bundle_sha256": bundle_sha256,
        "source_leaf_nodes": [SOURCE_NODE],
        "target_leaf_nodes": [TARGET_NODE],
        "migration_plan_sha256": reference(run_root, migration_plan_path)["sha256"],
    }
    pending_nodes = [TARGET_NODE] if pending else []
    body = {
        "format": "ffxivshare-source-upgrade-policy-proposal-body",
        "format_version": proposal_body_version,
        "proposal_id": f"{name}-proposal",
        "run_id": bootstrap_payload["run_id"],
        "bootstrap_nonce": bootstrap_payload["bootstrap_nonce"],
        "policy_projection": projection,
        "evidence": evidence,
        "evidence_set_sha256": evidence_set_sha256,
        "review_requirements": {
            "lossless_review_status": "not_reviewed",
            "pending_migration_nodes": pending_nodes,
            "required_evidence": list(evidence),
        },
    }
    body_path = run_root / "evidence" / "policy-proposal-body.json"
    write_json(body_path, body)
    body_reference = reference(run_root, body_path)

    events: list[dict[str, Any]] = []
    previous = "0" * 64

    def add_event(stage: str, outcome: str, details: dict[str, Any], minute: int) -> None:
        nonlocal previous
        event = make_event(
            len(events) + 1,
            previous,
            f"2026-07-16T00:{minute:02d}:00Z",
            stage,
            outcome,
            details,
        )
        events.append(event)
        previous = event["event_sha256"]

    minute = 1

    def next_minute() -> int:
        nonlocal minute
        value = minute
        minute += 1
        return value

    add_event("proposal_started", "passed", {"run_id": body["run_id"]}, next_minute())
    initial_handoff_details = {
        "artifact": handoff_reference,
        "access_snapshot_sha256": handoff_value["access_baseline"][
            "snapshot_sha256"
        ],
        "scope_roles": list(HANDOFF_SCOPE_ROLES),
    }
    if not exact_handoff_initial:
        initial_handoff_details = {**initial_handoff_details, "unexpected": True}
    if include_handoff_initial and not handoff_initial_after_body:
        add_event(
            "source_handoff_verified",
            "passed",
            initial_handoff_details,
            next_minute(),
        )
    if duplicate_handoff_initial:
        add_event(
            "source_handoff_verified",
            "passed",
            initial_handoff_details,
            next_minute(),
        )
    add_event(
        "proposal_runtime_fingerprint_verified",
        "passed",
        {"artifact": evidence["runtime_fingerprint"]},
        next_minute(),
    )
    final_handoff_details = {
        "artifact": handoff_reference,
        "access_snapshot_sha256": handoff_value["access_baseline"][
            "snapshot_sha256"
        ],
        "scope_roles": list(HANDOFF_SCOPE_ROLES),
        "content_verified": True,
    }
    if not exact_handoff_final:
        final_handoff_details = {**final_handoff_details, "unexpected": True}
    if include_handoff_final and handoff_final_before_body:
        add_event(
            "source_handoff_final_verified",
            "passed",
            final_handoff_details,
            next_minute(),
        )
    add_event(
        "policy_proposal_body_created",
        "passed",
        {
            "proposal_id": body["proposal_id"],
            "run_id": body["run_id"],
            "body": body_reference,
            "evidence_set_sha256": evidence_set_sha256,
            "migration_applied": False,
            "review_required": True,
        },
        next_minute(),
    )
    if include_handoff_initial and handoff_initial_after_body:
        add_event(
            "source_handoff_verified",
            "passed",
            initial_handoff_details,
            next_minute(),
        )
    if include_handoff_final and not handoff_final_before_body:
        add_event(
            "source_handoff_final_verified",
            "passed",
            final_handoff_details,
            next_minute(),
        )
    if duplicate_handoff_final:
        add_event(
            "source_handoff_final_verified",
            "passed",
            final_handoff_details,
            next_minute(),
        )
    add_event(
        "source_final_verified",
        "passed",
        {
            "source_unchanged": True,
            "backup_set": {
                "database": {"size": 4096, "sha256": SOURCE_DATABASE_SHA256},
                "checksum": {"size": 65, "sha256": "c" * 64},
                "metadata": {"size": 256, "sha256": "d" * 64},
            },
        },
        next_minute(),
    )
    if include_bundle_final:
        add_event(
            "execution_bundle_final_verified",
            "passed",
            {
                "bundle_unchanged": True,
                "execution_bundle_sha256": bundle_sha256,
            },
            next_minute(),
        )
    add_event(
        "review_required",
        "terminal",
        {
            "status": "review_required",
            "proposal_body_sha256": body_reference["sha256"],
            "lossless_reviewed": False,
            "migration_applied": False,
            "cutover_authorized": False,
            "contains_production_user_data": True,
            "retained_on_success": True,
            "secure_disposal_required": True,
            "sensitive_retention_scope": "entire_run_root",
        },
        next_minute(),
    )
    ledger_path = run_root / "evidence" / "events.jsonl"
    ledger_path.write_bytes(b"".join(encoded(event) for event in events))
    ledger_reference = reference(run_root, ledger_path)
    proposal = {
        "format": "ffxivshare-source-upgrade-policy-proposal",
        "format_version": proposal_version,
        "generated_at": "2026-07-16T00:10:00Z",
        "proposal_id": body["proposal_id"],
        "run_id": body["run_id"],
        "bootstrap_nonce": body["bootstrap_nonce"],
        "state": "review_required",
        "body": body,
        "body_artifact": body_reference,
        "body_sha256": body_reference["sha256"],
        "ledger": {
            "artifact": ledger_reference,
            "event_count": len(events),
            "head_event_sha256": previous,
            "terminal_status": "review_required",
        },
    }
    proposal_path = run_root / "evidence" / "policy-proposal.json"
    proposal_sha256 = write_json(proposal_path, proposal)

    stdout_path = run_root / "logs" / "inner.stdout.log"
    stderr_path = run_root / "logs" / "inner.stderr.log"
    write_bytes(stdout_path, b"proposal requires review\n")
    write_bytes(stderr_path, b"")
    completion_path = run_root / "evidence" / "completion.json"
    write_json(
        completion_path,
        {
            "format": "ffxivshare-production-copy-bootstrap-completion",
            "format_version": 1,
            "generated_at": "2026-07-16T00:11:00Z",
            "run_id": body["run_id"],
            "inner_exit_code": completion_exit,
            "execution_bundle_sha256": bundle_sha256,
            "execution_bundle_unchanged": True,
            "bootstrap_record_unchanged": True,
            "bundle_manifest_unchanged": True,
            "frozen_policy_unchanged": True,
            "frozen_proposal_unchanged": True,
            "frozen_review_unchanged": True,
            "stdout": reference(run_root, stdout_path),
            "stderr": reference(run_root, stderr_path),
        },
    )

    review = {
        "format": "ffxivshare-migration-lossless-review",
        "format_version": 1,
        "review_id": f"{name}-review",
        "reviewed_at": "2026-07-16T00:12:00Z",
        "reviewer": "fixture-operator",
        "reviewer_identity_verification": IDENTITY,
        "proposal_sha256": proposal_sha256,
        "proposal_body_sha256": body_reference["sha256"],
        "evidence_set_sha256": evidence_set_sha256,
        "conclusion": "lossless",
        "migrations_reviewed": pending_nodes,
        "notes": "The structured AddIndex plan preserves every existing row and value.",
    }
    review_path = run_root / "approval" / "lossless-review.json"
    review_sha256 = write_json(review_path, review)
    return Fixture(
        run_root=run_root,
        proposal_path=proposal_path,
        proposal_sha256=proposal_sha256,
        review_path=review_path,
        review_sha256=review_sha256,
        output_path=run_root / "approval" / "approved-policy.json",
        proposal=proposal,
        review=review,
    )


def invoke_approve(
    fixture: Fixture,
    *,
    output: Path | None = None,
    expected_proposal: str | None = None,
    expected_review: str | None = None,
    confirmations: bool = True,
) -> SimpleNamespace:
    arguments = SimpleNamespace(
        proposal=str(fixture.proposal_path),
        proposal_run_root=str(fixture.run_root),
        expected_proposal_sha256=expected_proposal or fixture.proposal_sha256,
        review=str(fixture.review_path),
        expected_review_sha256=expected_review or fixture.review_sha256,
        reviewer="fixture-operator",
        output=str(output or fixture.output_path),
        confirm_lossless_reviewed=confirmations,
        confirm_reviewer_operator_asserted=confirmations,
    )
    try:
        approval.approve(arguments)
    except (approval.ApprovalError, OSError, ValueError) as exc:
        return SimpleNamespace(returncode=1, stdout=b"", stderr=str(exc).encode("utf-8"))
    return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


def invoke_record_review(
    fixture: Fixture,
    *,
    output: Path | None = None,
    proposal: Path | None = None,
    expected_proposal: str | None = None,
    review_id: str | None = None,
    reviewer: str = "fixture-operator",
    notes: str = "",
    confirmations: bool = True,
) -> SimpleNamespace:
    arguments = SimpleNamespace(
        proposal=str(proposal or fixture.proposal_path),
        proposal_run_root=str(fixture.run_root),
        expected_proposal_sha256=expected_proposal or fixture.proposal_sha256,
        review_id=review_id or f"{fixture.run_root.name}-recorded-review",
        reviewer=reviewer,
        notes=notes,
        output=str(output or fixture.review_path),
        confirm_lossless_reviewed=confirmations,
        confirm_reviewer_operator_asserted=confirmations,
    )
    try:
        approval.record_review(arguments)
    except (approval.ApprovalError, OSError, ValueError) as exc:
        return SimpleNamespace(returncode=1, stdout=b"", stderr=str(exc).encode("utf-8"))
    return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


def invoke_verify(
    fixture: Fixture,
    *,
    policy: Path,
    proposal: Path,
    review: Path,
    expected_policy: str,
    expected_proposal: str | None = None,
    expected_review: str | None = None,
) -> SimpleNamespace:
    arguments = SimpleNamespace(
        policy=str(policy),
        proposal=str(proposal),
        review=str(review),
        proposal_run_root=str(fixture.run_root),
        expected_policy_sha256=expected_policy,
        expected_proposal_sha256=expected_proposal or fixture.proposal_sha256,
        expected_review_sha256=expected_review or fixture.review_sha256,
    )
    try:
        approval.verify(arguments)
    except (approval.ApprovalError, OSError, ValueError) as exc:
        return SimpleNamespace(returncode=1, stdout=b"", stderr=str(exc).encode("utf-8"))
    return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


recorded = build_fixture("record-review-positive")
recorded.review_path.unlink()
recorded_run = invoke_record_review(recorded, notes="")
assert recorded_run.returncode == 0, recorded_run.stderr.decode(
    "utf-8", errors="replace"
)
recorded_raw = recorded.review_path.read_bytes()
recorded_review = json.loads(recorded_raw.decode("utf-8"))
assert recorded_raw == encoded(recorded_review)
assert set(recorded_review) == {
    "format", "format_version", "review_id", "reviewed_at", "reviewer",
    "reviewer_identity_verification", "proposal_sha256",
    "proposal_body_sha256", "evidence_set_sha256", "conclusion",
    "migrations_reviewed", "notes",
}
assert recorded_review["format"] == "ffxivshare-migration-lossless-review"
assert recorded_review["format_version"] == 1
assert recorded_review["reviewer"] == "fixture-operator"
assert recorded_review["reviewer_identity_verification"] == IDENTITY
assert recorded_review["proposal_sha256"] == recorded.proposal_sha256
assert recorded_review["proposal_body_sha256"] == recorded.proposal["body_sha256"]
assert (
    recorded_review["evidence_set_sha256"]
    == recorded.proposal["body"]["evidence_set_sha256"]
)
assert recorded_review["conclusion"] == "lossless"
assert (
    recorded_review["migrations_reviewed"]
    == recorded.proposal["body"]["review_requirements"]["pending_migration_nodes"]
)
assert recorded_review["notes"] == ""
approval._validate_timestamp(recorded_review["reviewed_at"], label="recorded_at")
assert recorded.review_path.stat().st_nlink == 1
recorded.review = recorded_review
recorded.review_sha256 = sha256(recorded_raw).hexdigest()

record_reuse = invoke_record_review(recorded, notes="replacement")
assert record_reuse.returncode == 1
assert recorded.review_path.read_bytes() == recorded_raw

recorded_approval = invoke_approve(recorded)
assert recorded_approval.returncode == 0, recorded_approval.stderr.decode(
    "utf-8", errors="replace"
)

unconfirmed_record = build_fixture("record-review-unconfirmed")
unconfirmed_record.review_path.unlink()
unconfirmed_record_run = invoke_record_review(
    unconfirmed_record, confirmations=False
)
assert unconfirmed_record_run.returncode == 1
assert not unconfirmed_record.review_path.exists()

wrong_record_hash = build_fixture("record-review-wrong-hash")
wrong_record_hash.review_path.unlink()
wrong_record_hash_run = invoke_record_review(
    wrong_record_hash, expected_proposal="0" * 64
)
assert wrong_record_hash_run.returncode == 1
assert not wrong_record_hash.review_path.exists()

wrong_record_path = build_fixture("record-review-wrong-proposal-path")
wrong_record_path.review_path.unlink()
proposal_alias = wrong_record_path.run_root / "approval" / "proposal-copy.json"
shutil.copyfile(wrong_record_path.proposal_path, proposal_alias)
wrong_record_path_run = invoke_record_review(
    wrong_record_path, proposal=proposal_alias
)
assert wrong_record_path_run.returncode == 1
assert not wrong_record_path.review_path.exists()

outside_record = build_fixture("record-review-outside-output")
outside_record.review_path.unlink()
outside_output = outside_record.run_root / "artifacts" / "lossless-review.json"
outside_record_run = invoke_record_review(outside_record, output=outside_output)
assert outside_record_run.returncode == 1
assert not outside_output.exists()

oversize_notes = build_fixture("record-review-oversize-notes")
oversize_notes.review_path.unlink()
oversize_notes_run = invoke_record_review(
    oversize_notes, notes="x" * (approval.MAX_REVIEW_NOTES_CHARS + 1)
)
assert oversize_notes_run.returncode == 1
assert not oversize_notes.review_path.exists()

# record-review publishes before its mandatory full _validate_binding pass.
# Invalid completion evidence must fail that pass and remove the captured inode.
invalid_record_completion = build_fixture(
    "record-review-invalid-completion", completion_exit=2
)
invalid_record_completion.review_path.unlink()
invalid_record_completion_run = invoke_record_review(invalid_record_completion)
assert invalid_record_completion_run.returncode == 1
assert not invalid_record_completion.review_path.exists()

good = build_fixture("good")
unconfirmed = invoke_approve(good, output=good.run_root / "approval" / "unconfirmed.json", confirmations=False)
assert unconfirmed.returncode == 1
assert not (good.run_root / "approval" / "unconfirmed.json").exists()

wrong_review = invoke_approve(
    good,
    output=good.run_root / "approval" / "wrong-review-hash.json",
    expected_review="0" * 64,
)
assert wrong_review.returncode == 1
assert not (good.run_root / "approval" / "wrong-review-hash.json").exists()

approved_run = invoke_approve(good)
assert approved_run.returncode == 0, approved_run.stderr.decode("utf-8", errors="replace")
approved_raw = good.output_path.read_bytes()
approved_sha256 = sha256(approved_raw).hexdigest()
approved = json.loads(approved_raw.decode("utf-8"))
assert set(approved) == {
    "format", "format_version", "policy_id", "source_database_sha256",
    "source_media_manifest_sha256", "source_media_snapshot_id",
    "source_applied_migrations_sha256", "source_sqlite_schema_sha256",
    "migration_runtime_sha256",
    "runtime_fingerprint_sha256", "execution_bundle_sha256",
    "source_leaf_nodes", "target_leaf_nodes", "migration_plan_sha256",
    "approved", "approved_at", "lossless_reviewed", "proposal_id",
    "proposal_sha256", "proposal_body_sha256", "proposal_run_id",
    "proposal_bootstrap_nonce", "proposal_evidence_set_sha256",
    "proposal_ledger_head_sha256", "proposal_ledger_event_count",
    "proposal_bootstrap_completion_sha256", "review_id", "reviewed_at",
    "review_record_sha256", "reviewer", "reviewer_identity_verification",
    "approval_tool_sha256",
}
assert approved["approved"] is True
assert approved["lossless_reviewed"] is True
assert approved["proposal_sha256"] == good.proposal_sha256
assert approved["review_record_sha256"] == good.review_sha256
assert approved["reviewer_identity_verification"] == IDENTITY
assert approved["approval_tool_sha256"] == sha256(approval_tool.read_bytes()).hexdigest()
bootstrap_bundle, bootstrap_policy_reference, bootstrap_policy_raw, bootstrap_policy = (
    bootstrap._load_approved_policy(good.output_path)
)
assert bootstrap_bundle == approved["execution_bundle_sha256"]
assert bootstrap_policy_reference["sha256"] == approved_sha256
assert bootstrap_policy_raw == approved_raw
assert bootstrap_policy == approved

reuse = invoke_approve(good)
assert reuse.returncode == 1
assert good.output_path.read_bytes() == approved_raw

# Publication and post-verification form one interruption-safe unit. A
# BaseException after create-new publication removes only the inode created by
# this approval attempt.
original_output_check = approval._assert_private_output_file

record_interrupted = build_fixture("record-review-interrupted-publication")
record_interrupted.review_path.unlink()
record_interrupt_once = {"raised": False}


def raise_during_record_postvalidation(path: Path, approval_directory: Path) -> None:
    del path, approval_directory
    if not record_interrupt_once["raised"]:
        record_interrupt_once["raised"] = True
        signal.raise_signal(signal.SIGINT)


approval._assert_private_output_file = raise_during_record_postvalidation
try:
    invoke_record_review(record_interrupted)
except KeyboardInterrupt:
    pass
else:
    raise AssertionError("Injected review-record publication interruption was swallowed")
finally:
    approval._assert_private_output_file = original_output_check
assert not record_interrupted.review_path.exists()

interrupted = build_fixture("interrupted-publication")
interrupt_once = {"raised": False}


def raise_after_publish(path: Path, approval_directory: Path) -> None:
    del path, approval_directory
    if not interrupt_once["raised"]:
        interrupt_once["raised"] = True
        signal.raise_signal(signal.SIGINT)


approval._assert_private_output_file = raise_after_publish
try:
    arguments = SimpleNamespace(
        proposal=str(interrupted.proposal_path),
        proposal_run_root=str(interrupted.run_root),
        expected_proposal_sha256=interrupted.proposal_sha256,
        review=str(interrupted.review_path),
        expected_review_sha256=interrupted.review_sha256,
        reviewer="fixture-operator",
        output=str(interrupted.output_path),
        confirm_lossless_reviewed=True,
        confirm_reviewer_operator_asserted=True,
    )
    approval.approve(arguments)
except KeyboardInterrupt:
    pass
else:
    raise AssertionError("Injected publication interruption was swallowed")
finally:
    approval._assert_private_output_file = original_output_check
assert not interrupted.output_path.exists()

# Simulate the narrowest link-publication fault: the filesystem link succeeds,
# but a BaseException is raised before os.link returns to the caller.
link_gap = build_fixture("link-gap-interruption")
real_link = approval.os.link


def link_then_interrupt(source: Path, destination: Path) -> None:
    real_link(source, destination)
    raise KeyboardInterrupt


approval.os.link = link_then_interrupt
try:
    arguments = SimpleNamespace(
        proposal=str(link_gap.proposal_path),
        proposal_run_root=str(link_gap.run_root),
        expected_proposal_sha256=link_gap.proposal_sha256,
        review=str(link_gap.review_path),
        expected_review_sha256=link_gap.review_sha256,
        reviewer="fixture-operator",
        output=str(link_gap.output_path),
        confirm_lossless_reviewed=True,
        confirm_reviewer_operator_asserted=True,
    )
    approval.approve(arguments)
except KeyboardInterrupt:
    pass
else:
    raise AssertionError("Injected os.link interruption was swallowed")
finally:
    approval.os.link = real_link
assert not link_gap.output_path.exists()

# Evidence changed after publication is detected by the final stable rehash,
# and the just-created policy is removed by its captured file identity.
changed_after_publish = build_fixture("changed-after-publication")
original_writer = approval._write_json_create_new


def write_then_change(path: Path, value: dict[str, Any]):
    identity = original_writer(path, value)
    state_path = (
        changed_after_publish.run_root
        / "evidence"
        / "proposal-source-migration-state.json"
    )
    with state_path.open("ab") as stream:
        stream.write(b" ")
    return identity


approval._write_json_create_new = write_then_change
try:
    changed_run = invoke_approve(changed_after_publish)
finally:
    approval._write_json_create_new = original_writer
assert changed_run.returncode == 1
assert not changed_after_publish.output_path.exists()

frozen = root / "frozen"
frozen.mkdir()
frozen_policy = frozen / "policy.json"
frozen_proposal = frozen / "proposal.json"
frozen_review = frozen / "review.json"
shutil.copyfile(good.output_path, frozen_policy)
shutil.copyfile(good.proposal_path, frozen_proposal)
shutil.copyfile(good.review_path, frozen_review)
verified = invoke_verify(
    good,
    policy=frozen_policy,
    proposal=frozen_proposal,
    review=frozen_review,
    expected_policy=approved_sha256,
)
assert verified.returncode == 0, verified.stderr.decode("utf-8", errors="replace")
wrong_policy_hash = invoke_verify(
    good,
    policy=frozen_policy,
    proposal=frozen_proposal,
    review=frozen_review,
    expected_policy="0" * 64,
)
assert wrong_policy_hash.returncode == 1

duplicate = build_fixture("duplicate")
proposal_text = duplicate.proposal_path.read_text(encoding="utf-8")
duplicate_raw = (
    proposal_text[:-2] + ',"state":"review_required"}\n'
).encode("utf-8")
duplicate.proposal_path.write_bytes(duplicate_raw)
duplicate.proposal_sha256 = sha256(duplicate_raw).hexdigest()
duplicate.review["proposal_sha256"] = duplicate.proposal_sha256
duplicate.review_sha256 = write_json(duplicate.review_path, duplicate.review)
duplicate_run = invoke_approve(duplicate)
assert duplicate_run.returncode == 1
assert not duplicate.output_path.exists()

nan_review = build_fixture("nan-review")
review_text = nan_review.review_path.read_text(encoding="utf-8")
nan_raw = review_text.replace(
    '"format_version":1', '"format_version":NaN'
).encode("utf-8")
nan_review.review_path.write_bytes(nan_raw)
nan_review.review_sha256 = sha256(nan_raw).hexdigest()
nan_run = invoke_approve(nan_review)
assert nan_run.returncode == 1
assert not nan_review.output_path.exists()

tampered_evidence = build_fixture("tampered-evidence")
state_path = tampered_evidence.run_root / "evidence" / "proposal-source-migration-state.json"
with state_path.open("ab") as stream:
    stream.write(b" ")
tampered_evidence_run = invoke_approve(tampered_evidence)
assert tampered_evidence_run.returncode == 1
assert not tampered_evidence.output_path.exists()

legacy_proposal = build_fixture(
    "legacy-proposal-v1",
    proposal_version=1,
    proposal_body_version=1,
)
legacy_proposal_run = invoke_approve(legacy_proposal)
assert legacy_proposal_run.returncode == 1
assert not legacy_proposal.output_path.exists()

legacy_body = build_fixture("legacy-proposal-body-v1", proposal_body_version=1)
legacy_body_run = invoke_approve(legacy_body)
assert legacy_body_run.returncode == 1
assert not legacy_body.output_path.exists()

missing_handoff_evidence = build_fixture(
    "missing-handoff-evidence", include_handoff_evidence=False
)
missing_handoff_evidence_run = invoke_approve(missing_handoff_evidence)
assert missing_handoff_evidence_run.returncode == 1
assert not missing_handoff_evidence.output_path.exists()

missing_handoff_initial = build_fixture(
    "missing-handoff-initial", include_handoff_initial=False
)
missing_handoff_initial_run = invoke_approve(missing_handoff_initial)
assert missing_handoff_initial_run.returncode == 1
assert not missing_handoff_initial.output_path.exists()

duplicate_handoff_initial = build_fixture(
    "duplicate-handoff-initial", duplicate_handoff_initial=True
)
duplicate_handoff_initial_run = invoke_approve(duplicate_handoff_initial)
assert duplicate_handoff_initial_run.returncode == 1
assert not duplicate_handoff_initial.output_path.exists()

late_handoff_initial = build_fixture(
    "late-handoff-initial", handoff_initial_after_body=True
)
late_handoff_initial_run = invoke_approve(late_handoff_initial)
assert late_handoff_initial_run.returncode == 1
assert not late_handoff_initial.output_path.exists()

nonexact_handoff_initial = build_fixture(
    "nonexact-handoff-initial", exact_handoff_initial=False
)
nonexact_handoff_initial_run = invoke_approve(nonexact_handoff_initial)
assert nonexact_handoff_initial_run.returncode == 1
assert not nonexact_handoff_initial.output_path.exists()

missing_handoff_final = build_fixture(
    "missing-handoff-final", include_handoff_final=False
)
missing_handoff_final_run = invoke_approve(missing_handoff_final)
assert missing_handoff_final_run.returncode == 1
assert not missing_handoff_final.output_path.exists()

duplicate_handoff_final = build_fixture(
    "duplicate-handoff-final", duplicate_handoff_final=True
)
duplicate_handoff_final_run = invoke_approve(duplicate_handoff_final)
assert duplicate_handoff_final_run.returncode == 1
assert not duplicate_handoff_final.output_path.exists()

early_handoff_final = build_fixture(
    "early-handoff-final", handoff_final_before_body=True
)
early_handoff_final_run = invoke_approve(early_handoff_final)
assert early_handoff_final_run.returncode == 1
assert not early_handoff_final.output_path.exists()

nonexact_handoff_final = build_fixture(
    "nonexact-handoff-final", exact_handoff_final=False
)
nonexact_handoff_final_run = invoke_approve(nonexact_handoff_final)
assert nonexact_handoff_final_run.returncode == 1
assert not nonexact_handoff_final.output_path.exists()

missing_bundle_final = build_fixture("missing-bundle-final", include_bundle_final=False)
missing_bundle_final_run = invoke_approve(missing_bundle_final)
assert missing_bundle_final_run.returncode == 1
assert not missing_bundle_final.output_path.exists()

missing_pending = build_fixture("missing-pending", pending=False)
missing_pending_run = invoke_approve(missing_pending)
assert missing_pending_run.returncode == 1
assert not missing_pending.output_path.exists()

bad_completion = build_fixture("bad-completion", completion_exit=2)
bad_completion_run = invoke_approve(bad_completion)
assert bad_completion_run.returncode == 1
assert not bad_completion.output_path.exists()

bad_review = build_fixture("bad-review")
bad_review.review["migrations_reviewed"] = []
bad_review.review_sha256 = write_json(bad_review.review_path, bad_review.review)
bad_review_run = invoke_approve(bad_review)
assert bad_review_run.returncode == 1
assert not bad_review.output_path.exists()

hard_link = build_fixture("hard-link")
hard_link_alias = hard_link.run_root / "approval" / "review-alias.json"
try:
    os.link(hard_link.review_path, hard_link_alias)
except OSError:
    pass
else:
    hard_link_run = invoke_approve(hard_link)
    assert hard_link_run.returncode == 1
    assert not hard_link.output_path.exists()

not_private = build_fixture("not-private", secure=False)
try:
    production_private_validator(not_private.run_root, not_private.run_root / "approval")
except approval.ApprovalError:
    pass
else:
    raise AssertionError("A non-private approval directory passed production DACL validation")
assert not not_private.output_path.exists()
ordinary_output = not_private.run_root / "approval" / "ordinary-output.json"
ordinary_output.write_text("{}\n", encoding="utf-8", newline="\n")
try:
    production_output_validator(ordinary_output, not_private.run_root / "approval")
except approval.ApprovalError:
    pass
else:
    raise AssertionError("A non-private output file passed production DACL validation")

tampered_frozen_review = frozen / "review-tampered.json"
shutil.copyfile(frozen_review, tampered_frozen_review)
with tampered_frozen_review.open("ab") as stream:
    stream.write(b" ")
tampered_verify = invoke_verify(
    good,
    policy=frozen_policy,
    proposal=frozen_proposal,
    review=tampered_frozen_review,
    expected_policy=approved_sha256,
)
assert tampered_verify.returncode == 1

print("Production-copy policy approval and verification contract tests passed.")
'@

try {
    [System.IO.File]::WriteAllText(
        $fixtureScript,
        $fixtureSource,
        [System.Text.UTF8Encoding]::new($false)
    )
    & $PythonExecutable -I -S -B -X utf8 `
        $fixtureScript `
        $PythonExecutable `
        $approvalTool `
        $bootstrapTool `
        $rehearsalTool `
        $handoffTool `
        $temporaryRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Production-copy policy approval contract tests failed with exit code $LASTEXITCODE."
    }
}
finally {
    Remove-TestRoot -Path $temporaryRoot
}
