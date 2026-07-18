from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import sqlite3
import stat
import sys
import unicodedata
from typing import Any


REPORT_FORMAT = "ffxivshare-production-copy-rehearsal-pair-verification"
REPORT_VERSION = 1
RESULT_FORMAT = "ffxivshare-production-copy-rehearsal"
RESULT_VERSION = 1
LEDGER_FORMAT = "ffxivshare-production-copy-rehearsal-event"
LEDGER_VERSION = 1
ZERO_SHA256 = "0" * 64
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_LEDGER_BYTES = 64 * 1024 * 1024
MAX_LEDGER_EVENTS = 10_000
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

SUCCESS_STAGE_SEQUENCE = (
    "created",
    "runtime_fingerprint_initial_verified",
    "approved_policy_evidence_verified",
    "external_handoff_preflight_verified",
    "runtime_dependencies_consistent",
    "source_private_copy_verified",
    "source_artifacts_verified",
    "source_inspected",
    "work_copy_verified",
    "source_schema_classified",
    "runtime_fingerprint_pre_migrate_verified",
    "execution_bundle_pre_migrate_verified",
    "runtime_fingerprint_post_migrate_verified",
    "source_schema_ready",
    "upgraded_source_inspected",
    "dataset_exported",
    "dataset_validated",
    "target_schema_created",
    "import_verified",
    "idempotence_verified",
    "target_dataset_validated",
    "target_export_compared",
    "restriction_preflight",
    "target_snapshot_verified",
    "database_structure_preserved",
    "final_target_migration_state_verified",
    "final_target_dataset_validated",
    "final_target_export_compared",
    "final_target_restriction_preflight",
    "final_target_verification_copy_unchanged",
    "media_verified",
    "final_media_verified",
    "target_snapshot_set_final_verified",
    "runtime_fingerprint_final_verified",
    "source_final_verified",
    "execution_bundle_final_verified",
    "external_handoff_final_verified",
    "deployment_candidate_verified",
    "completed",
)

ALLOWED_DIFFERENCES = (
    "run_root",
    "run_id",
    "bootstrap_nonce",
    "generated_at",
    "target_media_slot",
    "target_media_root",
    "target_media_snapshot_id",
    "log_artifacts",
    "target_backup_bytes",
)
SHARE_STATUSES = frozenset({"approved", "pending", "rejected"})
SHARE_VISIBILITIES = frozenset({"private", "public", "unlisted"})
SHARE_RESTRICTION_STATES = frozenset(
    {"clear", "legacy_private", "report_takedown", "review_rejected"}
)


class PairVerificationError(RuntimeError):
    pass


def _load_sibling(module_name: str, filename: str) -> Any:
    path = Path(__file__).resolve().with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load frozen sibling module: {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


approval_core = _load_sibling(
    "ffxivshare_pair_frozen_approval", "Approve-ProductionCopyPolicy.py"
)
rehearsal_core = _load_sibling(
    "ffxivshare_pair_frozen_rehearsal", "Rehearse-ProductionCopy.py"
)
comparison_core = _load_sibling(
    "ffxivshare_pair_frozen_comparison", "Compare-SiteDataExports.py"
)
media_core = _load_sibling(
    "ffxivshare_pair_frozen_media", "MediaManifest.py"
)
backup_core = _load_sibling(
    "ffxivshare_pair_frozen_backup", "Verify-SQLiteBackupSet.py"
)
handoff_core = approval_core.handoff_core


@dataclass(frozen=True)
class PairVerificationConfig:
    first_run_root: Path
    second_run_root: Path
    proposal_run_root: Path
    policy: Path
    proposal: Path
    review: Path
    expected_policy_sha256: str
    expected_proposal_sha256: str
    expected_review_sha256: str
    output: Path


@dataclass(frozen=True)
class RehearsalLedgerReplay:
    events: tuple[dict[str, Any], ...]
    event_count: int
    head_event_sha256: str
    raw_size: int
    raw_sha256: str


@dataclass(frozen=True)
class LargeFileSnapshot:
    path: Path
    size: int
    sha256: str
    identity: tuple[int, int, int, int, int]


@dataclass
class RunValidation:
    label: str
    run_root: Path
    tracker: Any
    directory_identities: dict[Path, tuple[int, int]]
    bootstrap_snapshot: Any
    completion_snapshot: Any
    result_snapshot: Any
    ledger_snapshot: Any
    bootstrap: dict[str, Any]
    completion: dict[str, Any]
    result: dict[str, Any]
    ledger: RehearsalLedgerReplay
    target_slot: str
    target_media_root: str
    target_media_snapshot_id: str
    target_backup_sha256: str
    target_backup_size: int
    semantic_projection: dict[str, Any]
    business_summary: dict[str, Any]
    dataset_projections: tuple[tuple[Path, dict[str, Any]], ...]
    large_snapshots: tuple[LargeFileSnapshot, ...]
    code_root: Path
    execution_membership: tuple[str, ...]

    def revalidate(self) -> None:
        self.tracker.revalidate()
        approval_core._assert_run_directories(self.directory_identities)
        try:
            current_membership = tuple(
                approval_core._enumerate_regular_files(self.code_root)
            )
        except Exception as exc:
            raise PairVerificationError(
                f"Frozen execution bundle cannot be re-enumerated: {self.code_root}"
            ) from exc
        _require(
            current_membership == self.execution_membership,
            f"Frozen execution-bundle membership changed: {self.code_root}",
        )
        for root, expected in self.dataset_projections:
            current = _dataset_projection(_inspect_dataset(root, label=str(root)))
            _require(current == expected, f"Tracked dataset changed: {root}")
        for expected in self.large_snapshots:
            _validate_no_live_sidecars(
                expected.path,
                label=f"tracked large artifact {expected.path.name}",
            )
            current = _stable_large_snapshot(
                expected.path,
                expected_size=expected.size,
                expected_sha256=expected.sha256,
                label=f"tracked large artifact {expected.path.name}",
            )
            _require(
                current.identity == expected.identity,
                f"Tracked large artifact identity changed: {expected.path}",
            )
            _validate_no_live_sidecars(
                expected.path,
                label=f"tracked large artifact {expected.path.name}",
            )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PairVerificationError(message)


def _strict_json(snapshot: Any, *, label: str, canonical: bool = True) -> Any:
    try:
        return approval_core._load_json(snapshot, label=label, canonical=canonical)
    except Exception as exc:
        raise PairVerificationError(f"{label} is invalid: {exc}") from exc


def _timestamp(value: Any, *, label: str) -> datetime:
    try:
        return approval_core._validate_timestamp(value, label=label)
    except Exception as exc:
        raise PairVerificationError(f"{label} is invalid: {exc}") from exc


def _sha256_value(value: Any, *, label: str) -> str:
    try:
        return approval_core._validate_sha256(value, label=label)
    except Exception as exc:
        raise PairVerificationError(f"{label} is invalid: {exc}") from exc


def _artifact_snapshot(
    tracker: Any,
    run_root: Path,
    reference: Any,
    *,
    label: str,
    expected_path: str | None = None,
    maximum_size: int = MAX_JSON_BYTES,
) -> Any:
    try:
        return approval_core._artifact_snapshot(
            tracker,
            run_root,
            reference,
            label=label,
            expected_path=expected_path,
            maximum_size=maximum_size,
        )
    except Exception as exc:
        raise PairVerificationError(f"{label} binding is invalid: {exc}") from exc


def _stable_large_snapshot(
    path: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    label: str,
) -> LargeFileSnapshot:
    _require(
        isinstance(expected_size, int)
        and not isinstance(expected_size, bool)
        and expected_size >= 0,
        f"{label} expected size is invalid.",
    )
    _sha256_value(expected_sha256, label=f"{label} expected sha256")
    try:
        candidate = approval_core._absolute_local_path(path, label=label)
        approval_core._assert_no_reparse_components(candidate, include_leaf=True)
        before_path = os.lstat(candidate)
        _require(
            stat.S_ISREG(before_path.st_mode)
            and before_path.st_nlink == 1
            and not approval_core._is_reparse_point(candidate)
            and before_path.st_size == expected_size,
            f"{label} is not a stable single-link regular file.",
        )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(candidate, flags)
        try:
            before = os.fstat(descriptor)
            digest = sha256()
            total = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after_path = os.lstat(candidate)
    except PairVerificationError:
        raise
    except Exception as exc:
        raise PairVerificationError(f"{label} cannot be hashed safely: {exc}") from exc
    identities = {
        approval_core._file_identity(before_path),
        approval_core._file_identity(before),
        approval_core._file_identity(after),
        approval_core._file_identity(after_path),
    }
    actual_sha256 = digest.hexdigest()
    _require(
        len(identities) == 1
        and all(
            stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
            for metadata in (before_path, before, after, after_path)
        )
        and not approval_core._is_reparse_point(candidate)
        and total == expected_size
        and actual_sha256 == expected_sha256,
        f"{label} changed or does not match its reference.",
    )
    return LargeFileSnapshot(
        path=candidate,
        size=total,
        sha256=actual_sha256,
        identity=approval_core._file_identity(after),
    )


def _large_artifact_snapshot(
    run_root: Path,
    reference: Any,
    *,
    label: str,
    expected_path: str,
) -> LargeFileSnapshot:
    try:
        artifact = approval_core._validate_artifact_reference(reference, label=label)
    except Exception as exc:
        raise PairVerificationError(f"{label} reference is invalid: {exc}") from exc
    _require(artifact["path"] == expected_path, f"{label} path is not canonical.")
    candidate = run_root.joinpath(*PurePosixPath(expected_path).parts)
    _require(
        approval_core._is_within(candidate, run_root),
        f"{label} escaped Rehearsal RunRoot.",
    )
    return _stable_large_snapshot(
        candidate,
        expected_size=artifact["size"],
        expected_sha256=artifact["sha256"],
        label=label,
    )


def _replay_rehearsal_ledger(snapshot: Any) -> RehearsalLedgerReplay:
    raw = snapshot.raw
    _require(bool(raw) and raw.endswith(b"\n"), "Rehearsal ledger is incomplete.")
    lines = raw.splitlines(keepends=True)
    _require(len(lines) <= MAX_LEDGER_EVENTS, "Rehearsal ledger has too many events.")
    _require(
        len(lines) == len(SUCCESS_STAGE_SEQUENCE),
        "Rehearsal ledger does not have the exact successful event count.",
    )
    previous = ZERO_SHA256
    previous_timestamp: datetime | None = None
    events: list[dict[str, Any]] = []
    expected_keys = {
        "format",
        "format_version",
        "sequence",
        "generated_at",
        "stage",
        "outcome",
        "previous_event_sha256",
        "details",
        "event_sha256",
    }
    for sequence, (line, expected_stage) in enumerate(
        zip(lines, SUCCESS_STAGE_SEQUENCE, strict=True), start=1
    ):
        line_snapshot = approval_core.FileSnapshot(
            path=snapshot.path,
            raw=line,
            size=len(line),
            sha256=sha256(line).hexdigest(),
            identity=snapshot.identity,
        )
        event = _strict_json(
            line_snapshot, label=f"rehearsal ledger event {sequence}"
        )
        _require(isinstance(event, dict), "Rehearsal ledger event is not an object.")
        _require(set(event) == expected_keys, "Rehearsal ledger event shape is invalid.")
        _require(
            event["format"] == LEDGER_FORMAT
            and event["format_version"] == LEDGER_VERSION,
            "Rehearsal ledger event format is unsupported.",
        )
        _require(
            isinstance(event["sequence"], int)
            and not isinstance(event["sequence"], bool)
            and event["sequence"] == sequence,
            "Rehearsal ledger sequence is invalid.",
        )
        _require(event["stage"] == expected_stage, "Rehearsal stage order is invalid.")
        expected_outcome = "terminal" if expected_stage == "completed" else "passed"
        _require(
            event["outcome"] == expected_outcome,
            f"Rehearsal stage {expected_stage} did not have the required outcome.",
        )
        _require(
            isinstance(event["details"], dict),
            "Rehearsal ledger details are invalid.",
        )
        _require(
            event["previous_event_sha256"] == previous,
            "Rehearsal ledger previous-event binding is invalid.",
        )
        generated_at = _timestamp(
            event["generated_at"], label=f"ledger event {sequence} generated_at"
        )
        if previous_timestamp is not None:
            _require(
                generated_at >= previous_timestamp,
                "Rehearsal ledger timestamps are not monotonic.",
            )
        previous_timestamp = generated_at
        declared = _sha256_value(
            event["event_sha256"], label=f"ledger event {sequence} sha256"
        )
        unsigned = dict(event)
        del unsigned["event_sha256"]
        _require(
            approval_core._canonical_json_sha256(unsigned) == declared,
            "Rehearsal ledger hash chain is invalid.",
        )
        previous = declared
        events.append(event)
    return RehearsalLedgerReplay(
        events=tuple(events),
        event_count=len(events),
        head_event_sha256=previous,
        raw_size=len(raw),
        raw_sha256=sha256(raw).hexdigest(),
    )


def _validate_result(
    value: Any,
    *,
    result_snapshot: Any,
    ledger_snapshot: Any,
    ledger: RehearsalLedgerReplay,
    expected_workspace_access_control: str,
) -> None:
    del result_snapshot
    expected_keys = {
        "format",
        "format_version",
        "generated_at",
        "status",
        "completed_stages",
        "issues",
        "evidence_chain",
        "source_immutable_snapshot_attested",
        "source_database_unchanged",
        "failed_artifacts_retained",
        "workspace_access_control",
        "network_isolation_enforced",
        "network_access_observation",
        "live_production_service_access_requested_by_orchestrator",
        "production_copy_read_performed",
        "contains_production_user_data",
        "retained_on_success",
        "secure_disposal_required",
        "sensitive_retention_scope",
        "sensitive_retention_directories",
        "cutover_authorized",
    }
    _require(isinstance(value, dict) and set(value) == expected_keys, "Result shape is invalid.")
    _require(
        value["format"] == RESULT_FORMAT and value["format_version"] == RESULT_VERSION,
        "Result format is unsupported.",
    )
    _timestamp(value["generated_at"], label="result generated_at")
    _require(value["status"] == "completed", "Rehearsal result is not completed.")
    _require(value["issues"] == [], "Rehearsal result contains issues.")
    _require(
        value["completed_stages"] == list(SUCCESS_STAGE_SEQUENCE[:-1]),
        "Result completed stages are not the exact successful ledger projection.",
    )
    expected_values = {
        "source_immutable_snapshot_attested": True,
        "source_database_unchanged": True,
        "failed_artifacts_retained": False,
        "workspace_access_control": expected_workspace_access_control,
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
    for name, expected in expected_values.items():
        _require(value[name] == expected, f"Result field {name} is invalid.")
    chain = value["evidence_chain"]
    _require(
        isinstance(chain, dict)
        and set(chain)
        == {
            "event_count",
            "head_event_sha256",
            "ledger",
            "verification",
            "tamper_proof",
        },
        "Result evidence-chain shape is invalid.",
    )
    _require(
        chain["event_count"] == ledger.event_count
        and chain["head_event_sha256"] == ledger.head_event_sha256
        and chain["verification"] == "self_consistent_local_chain"
        and chain["tamper_proof"] is False,
        "Result evidence-chain summary does not match the ledger.",
    )
    ledger_reference = chain["ledger"]
    _require(
        ledger_reference
        == {
            "path": "evidence/events.jsonl",
            "size": ledger.raw_size,
            "sha256": ledger.raw_sha256,
        },
        "Result ledger artifact reference is invalid.",
    )
    _require(
        ledger_snapshot.size == ledger.raw_size
        and ledger_snapshot.sha256 == ledger.raw_sha256,
        "Tracked ledger bytes do not match the result.",
    )


def _compare_semantic_projections(
    first: dict[str, Any], second: dict[str, Any]
) -> dict[str, Any]:
    _require(isinstance(first, dict) and isinstance(second, dict), "Semantic projection is invalid.")
    first_sha256 = approval_core._canonical_json_sha256(first)
    second_sha256 = approval_core._canonical_json_sha256(second)
    if first != second or first_sha256 != second_sha256:
        differing = sorted(
            key for key in set(first) | set(second) if first.get(key) != second.get(key)
        )
        rendered = ", ".join(differing[:16]) or "unknown"
        raise PairVerificationError(
            f"Rehearsal semantic projections differ: {rendered}."
        )
    matched_projections = {
        "source_sqlite_schema_sha256": first["source_sqlite_schema_sha256"],
        "database_structure_preservation_sha256": (
            approval_core._canonical_json_sha256(
                first["database_structure_preservation"]
            )
        ),
        "entity_inventory_sha256": approval_core._canonical_json_sha256(
            first["final_target_dataset"]["entities"]
        ),
        "media_inventory_sha256": approval_core._canonical_json_sha256(
            first["final_target_media"]["files"]
        ),
        "applied_migrations_sha256": approval_core._canonical_json_sha256(
            first["migration_states"]["final_target"]["applied"]
        ),
        "target_backup_semantics_sha256": approval_core._canonical_json_sha256(
            first["target_backup_inspection"]
        ),
    }
    return {
        "matched": True,
        "issues": [],
        "allowed_differences": list(ALLOWED_DIFFERENCES),
        "matched_projections": matched_projections,
        "semantic_projection_sha256": first_sha256,
    }


def _canonical_key(path: Path) -> str:
    return approval_core._canonical_key(path)


def _same_path(first: Path | str, second: Path | str) -> bool:
    return _canonical_key(Path(first)) == _canonical_key(Path(second))


def _path_overlap(first: Path, second: Path) -> bool:
    return approval_core._is_within(first, second) or approval_core._is_within(
        second, first
    )


def _validate_private_directory(api: Any, path: Path, *, label: str) -> None:
    probe = path / ".ffxivshare-pair-verifier-read-only-acl-probe"
    try:
        resolved_probe = handoff_core._validate_output_parent(api, os.fspath(probe))
    except Exception as exc:
        raise PairVerificationError(f"{label} is not a protected private directory: {exc}") from exc
    _require(
        _same_path(Path(resolved_probe).parent, path),
        f"{label} changed identity during DACL validation.",
    )


def _reference(path: Path, snapshot: Any, root: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "size": snapshot.size,
        "sha256": snapshot.sha256,
    }


def _validate_external_reference(
    value: Any,
    *,
    expected_snapshot: Any,
    label: str,
    extra_keys: set[str] = frozenset(),
) -> None:
    _require(
        isinstance(value, dict)
        and set(value) == {"path", "size", "sha256"} | set(extra_keys),
        f"{label} external reference shape is invalid.",
    )
    _require(
        isinstance(value["path"], str)
        and _same_path(Path(value["path"]), expected_snapshot.path),
        f"{label} external path does not match the supplied authority.",
    )
    _require(
        value["size"] == expected_snapshot.size
        and value["sha256"] == expected_snapshot.sha256,
        f"{label} external bytes do not match the supplied authority.",
    )


def _parse_inner_arguments(value: Any, *, run_root: Path) -> dict[str, Any]:
    _require(isinstance(value, list), "Bootstrap inner arguments must be a list.")
    names = (
        "--source-database",
        "--source-checksum",
        "--source-metadata",
        "--source-proposal-run-root",
        "--source-media-manifest",
        "--target-media-root",
        "--target-media-snapshot-id",
        "--run-root",
    )
    expected_length = len(names) * 2 + 2
    _require(
        len(value) == expected_length,
        "Bootstrap inner arguments do not have the exact approved-rehearsal shape.",
    )
    parsed: dict[str, Any] = {}
    offset = 0
    for name in names:
        _require(value[offset] == name, "Bootstrap inner argument order is invalid.")
        argument = value[offset + 1]
        _require(
            isinstance(argument, str) and argument and "\x00" not in argument,
            f"Bootstrap inner argument {name} is invalid.",
        )
        parsed[name] = argument
        offset += 2
    _require(
        value[offset:] == ["--confirm-source-immutable", "--confirm-target-media-offline"],
        "Bootstrap immutable/offline confirmations are incomplete.",
    )
    _require(
        _same_path(Path(parsed["--run-root"]), run_root),
        "Bootstrap inner RunRoot does not match its evidence root.",
    )
    return parsed


def _validate_run_layout(value: Any, *, run_root: Path) -> None:
    expected = {
        ".",
        "approval",
        "artifacts",
        "code",
        "evidence",
        "logs",
        "runtime-empty.env",
        "scratch-media",
        "target",
        "tmp",
        "work",
    }
    _require(isinstance(value, list), "Bootstrap RunRoot layout is invalid.")
    seen: set[str] = set()
    for item in value:
        _require(
            isinstance(item, dict)
            and set(item) == {"device", "inode", "kind", "path"},
            "Bootstrap RunRoot layout entry has an invalid shape.",
        )
        relative = item["path"]
        _require(
            isinstance(relative, str)
            and relative not in seen
            and item["kind"] in {"directory", "file"}
            and isinstance(item["device"], int)
            and not isinstance(item["device"], bool)
            and isinstance(item["inode"], int)
            and not isinstance(item["inode"], bool),
            "Bootstrap RunRoot layout entry is invalid.",
        )
        seen.add(relative)
        pure = PurePosixPath(relative)
        _require(
            relative == "."
            or (
                not pure.is_absolute()
                and ".." not in pure.parts
                and "\\" not in relative
                and pure.as_posix() == relative
            ),
            "Bootstrap RunRoot layout path is unsafe.",
        )
        candidate = run_root if relative == "." else run_root.joinpath(*pure.parts)
        try:
            approval_core._assert_no_reparse_components(candidate, include_leaf=True)
            metadata = os.lstat(candidate)
        except Exception as exc:
            raise PairVerificationError("Bootstrap RunRoot layout cannot be inspected.") from exc
        expected_mode = stat.S_ISDIR if item["kind"] == "directory" else stat.S_ISREG
        _require(expected_mode(metadata.st_mode), "Bootstrap RunRoot layout kind changed.")
        if item["kind"] == "file":
            _require(metadata.st_nlink == 1, "Bootstrap RunRoot layout file is hard-linked.")
        _require(
            (metadata.st_dev, metadata.st_ino) == (item["device"], item["inode"]),
            "Bootstrap RunRoot layout identity changed.",
        )
    _require(seen == expected, "Bootstrap RunRoot layout is incomplete.")


def _validate_execution_bundle(
    *,
    tracker: Any,
    run_root: Path,
    execution: Any,
    expected_sha256: str,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    _require(
        isinstance(execution, dict)
        and set(execution)
        == {"authority", "expected_sha256", "frozen_sha256", "manifest"}
        and execution["authority"] == "external_digest"
        and execution["expected_sha256"] == expected_sha256
        and execution["frozen_sha256"] == expected_sha256,
        "Bootstrap execution-bundle authority is invalid.",
    )
    manifest_snapshot = _artifact_snapshot(
        tracker,
        run_root,
        execution["manifest"],
        label="execution bundle manifest",
        expected_path="evidence/execution-bundle.json",
    )
    manifest = _strict_json(manifest_snapshot, label="execution bundle manifest")
    _require(
        isinstance(manifest, dict)
        and set(manifest) == {"execution_bundle_sha256", "files", "format", "format_version"}
        and manifest["format"] == "ffxivshare-production-copy-execution-bundle"
        and manifest["format_version"] == 1
        and manifest["execution_bundle_sha256"] == expected_sha256
        and isinstance(manifest["files"], list)
        and bool(manifest["files"]),
        "Frozen execution-bundle manifest is invalid.",
    )
    code_root = run_root / "code"
    paths: list[str] = []
    for index, item in enumerate(manifest["files"]):
        _require(
            isinstance(item, dict)
            and set(item) == {"path", "size", "sha256"}
            and isinstance(item["size"], int)
            and not isinstance(item["size"], bool)
            and item["size"] >= 0
            and isinstance(item["sha256"], str)
            and SHA256_PATTERN.fullmatch(item["sha256"]) is not None,
            "Frozen execution-bundle entry has an invalid shape.",
        )
        relative = item["path"]
        _require(isinstance(relative, str), "Frozen execution-bundle path is invalid.")
        pure = PurePosixPath(relative)
        _require(
            not pure.is_absolute()
            and ".." not in pure.parts
            and "\\" not in relative
            and pure.as_posix() == relative
            and relative not in {"", "."},
            "Frozen execution-bundle path is unsafe.",
        )
        candidate = code_root.joinpath(*pure.parts)
        snapshot = tracker.read(
            candidate,
            maximum_size=max(item["size"], 1),
            label=f"execution bundle file {index}",
        )
        _require(
            item["size"] == snapshot.size and item["sha256"] == snapshot.sha256,
            "Frozen execution-bundle file does not match its manifest.",
        )
        paths.append(relative)
    _require(paths == sorted(set(paths)), "Frozen execution-bundle membership is not canonical.")
    _require(
        "ops/migration/Verify-ProductionCopyRehearsalPair.py" in paths,
        "Pair verifier is not frozen into the approved execution bundle.",
    )
    _require(
        approval_core._canonical_json_sha256(manifest["files"]) == expected_sha256,
        "Frozen execution-bundle digest is invalid.",
    )
    try:
        actual_membership = tuple(approval_core._enumerate_regular_files(code_root))
    except Exception as exc:
        raise PairVerificationError("Frozen execution bundle cannot be enumerated.") from exc
    _require(actual_membership == tuple(paths), "Frozen execution-bundle membership changed.")
    return manifest, tuple(paths)


def _validate_bootstrap(
    *,
    tracker: Any,
    run_root: Path,
    policy: dict[str, Any],
    policy_snapshot: Any,
    proposal_snapshot: Any,
    review_snapshot: Any,
) -> tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any]]:
    snapshot = tracker.read(
        run_root / "evidence" / "bootstrap.json",
        maximum_size=MAX_JSON_BYTES,
        label="rehearsal bootstrap record",
    )
    record = _strict_json(snapshot, label="rehearsal bootstrap record")
    expected_keys = {
        "approval_inputs",
        "bootstrap_nonce",
        "bootstrap_trusted_not_frozen",
        "configuration",
        "execution_bundle",
        "format",
        "format_version",
        "generated_at",
        "media_read_by_bootstrap",
        "policy",
        "python",
        "run_id",
        "run_layout",
        "source_data_read_by_bootstrap",
        "workspace_access_control",
    }
    _require(isinstance(record, dict) and set(record) == expected_keys, "Bootstrap shape is invalid.")
    _require(
        record["format"] == "ffxivshare-production-copy-bootstrap"
        and record["format_version"] == 1
        and record["bootstrap_trusted_not_frozen"] is False
        and record["source_data_read_by_bootstrap"] is False
        and record["media_read_by_bootstrap"] is False,
        "Bootstrap identity is invalid.",
    )
    try:
        approval_core._validate_identifier(record["run_id"], label="bootstrap run_id")
    except Exception as exc:
        raise PairVerificationError("Bootstrap run_id is invalid.") from exc
    _sha256_value(record["bootstrap_nonce"], label="bootstrap nonce")
    _timestamp(record["generated_at"], label="bootstrap generated_at")
    _require(
        isinstance(record["workspace_access_control"], str)
        and record["workspace_access_control"].startswith("windows_protected_dacl_"),
        "Bootstrap workspace access-control claim is invalid.",
    )
    configuration = record["configuration"]
    _require(
        isinstance(configuration, dict)
        and set(configuration)
        == {"inner_arguments", "inner_entrypoint", "mode", "repository_root", "run_root"}
        and configuration["mode"] == "approved-rehearsal"
        and configuration["inner_entrypoint"] == "ops/migration/Rehearse-ProductionCopy.py"
        and isinstance(configuration["repository_root"], str)
        and isinstance(configuration["run_root"], str)
        and _same_path(Path(configuration["run_root"]), run_root),
        "Bootstrap rehearsal launch configuration is invalid.",
    )
    arguments = _parse_inner_arguments(configuration["inner_arguments"], run_root=run_root)
    _validate_run_layout(record["run_layout"], run_root=run_root)
    python_identity = record["python"]
    _require(
        isinstance(python_identity, dict)
        and set(python_identity)
        == {
            "cache_tag",
            "executable",
            "executable_sha256",
            "executable_size",
            "implementation",
            "isolation_flags",
            "version",
        },
        "Bootstrap Python identity shape is invalid.",
    )
    executable = Path(python_identity["executable"])
    executable_snapshot = tracker.read(
        executable,
        maximum_size=max(int(python_identity.get("executable_size", -1)), 1),
        label="bootstrap Python executable",
    )
    _require(
        executable_snapshot.size == python_identity["executable_size"]
        and executable_snapshot.sha256 == python_identity["executable_sha256"]
        and python_identity["isolation_flags"]
        == {
            "ignore_environment": True,
            "no_user_site": True,
            "dont_write_bytecode": True,
            "utf8_mode": True,
            "isolated": True,
            "no_site": True,
            "safe_path": True,
        },
        "Bootstrap Python identity changed or lacks isolation.",
    )
    manifest, _paths = _validate_execution_bundle(
        tracker=tracker,
        run_root=run_root,
        execution=record["execution_bundle"],
        expected_sha256=policy["execution_bundle_sha256"],
    )
    policy_binding = record["policy"]
    approval_inputs = record["approval_inputs"]
    _require(
        isinstance(policy_binding, dict)
        and set(policy_binding) == {"source", "frozen"}
        and isinstance(approval_inputs, dict)
        and set(approval_inputs) == {"proposal", "review"},
        "Bootstrap approval inputs are incomplete.",
    )
    _validate_external_reference(
        policy_binding["source"],
        expected_snapshot=policy_snapshot,
        label="policy",
        extra_keys={"policy_id", "review_id"},
    )
    _require(
        policy_binding["source"]["policy_id"] == policy["policy_id"]
        and policy_binding["source"]["review_id"] == policy["review_id"],
        "Bootstrap policy identifiers differ from the approved policy.",
    )
    for name, expected_snapshot, expected_path in (
        ("proposal", proposal_snapshot, "evidence/approved-proposal.json"),
        ("review", review_snapshot, "evidence/approved-review.json"),
    ):
        binding = approval_inputs[name]
        _require(
            isinstance(binding, dict) and set(binding) == {"source", "frozen"},
            f"Bootstrap {name} binding shape is invalid.",
        )
        _validate_external_reference(
            binding["source"], expected_snapshot=expected_snapshot, label=name
        )
        frozen = _artifact_snapshot(
            tracker,
            run_root,
            binding["frozen"],
            label=f"frozen {name}",
            expected_path=expected_path,
        )
        _require(
            frozen.raw == expected_snapshot.raw
            and frozen.sha256 == expected_snapshot.sha256,
            f"Frozen {name} differs from the supplied authority.",
        )
    frozen_policy = _artifact_snapshot(
        tracker,
        run_root,
        policy_binding["frozen"],
        label="frozen policy",
        expected_path="evidence/approved-policy.json",
    )
    _require(
        frozen_policy.raw == policy_snapshot.raw
        and frozen_policy.sha256 == policy_snapshot.sha256,
        "Frozen policy differs from the supplied authority.",
    )
    return snapshot, record, arguments, manifest


def _dataset_projection(summary: Any) -> dict[str, Any]:
    return {
        "application_version": summary.application_version,
        "source_database": summary.source_database,
        "record_count": summary.record_count,
        "entities": summary.entities,
        "dependencies": summary.dependencies,
        "table_semantics": summary.table_semantics,
        "migration_nodes": [list(item) for item in summary.migration_nodes],
        "migration_leaf_nodes": [list(item) for item in summary.migration_leaf_nodes],
        "sequences": summary.sequences,
        "session": summary.session,
    }


def _inspect_dataset(root: Path, *, label: str) -> Any:
    try:
        approval_core._assert_no_reparse_components(root, include_leaf=True)
        return comparison_core._inspect_dataset(root)
    except Exception as exc:
        raise PairVerificationError(f"{label} dataset is invalid: {exc}") from exc


def _media_projection(value: Any, *, label: str) -> dict[str, Any]:
    expected_keys = {
        "format",
        "format_version",
        "generated_at",
        "hash_algorithm",
        "path_normalization",
        "source_snapshot",
        "file_count",
        "total_size",
        "files",
    }
    _require(isinstance(value, dict) and set(value) == expected_keys, f"{label} shape is invalid.")
    _require(
        value["format"] == media_core.MANIFEST_FORMAT
        and value["format_version"] == media_core.MANIFEST_VERSION
        and value["hash_algorithm"] == media_core.HASH_ALGORITHM,
        f"{label} format is invalid.",
    )
    _timestamp(value["generated_at"], label=f"{label} generated_at")
    _require(
        value["path_normalization"] == "unicode_nfc_canonical_caseless_unique",
        f"{label} path-normalization contract is invalid.",
    )
    source_snapshot = value["source_snapshot"]
    _require(
        isinstance(source_snapshot, dict)
        and set(source_snapshot) == {"id", "offline_confirmed"}
        and source_snapshot["offline_confirmed"] is True
        and isinstance(source_snapshot["id"], str)
        and media_core.SNAPSHOT_ID_PATTERN.fullmatch(source_snapshot["id"]) is not None,
        f"{label} snapshot identity is invalid.",
    )
    rows = value["files"]
    _require(isinstance(rows, list), f"{label} files projection is invalid.")
    seen: set[str] = set()
    total = 0
    ordered: list[str] = []
    for row in rows:
        _require(
            isinstance(row, dict) and set(row) == {"path", "size", "sha256"},
            f"{label} media row shape is invalid.",
        )
        path = row["path"]
        _require(
            isinstance(path, str)
            and path == unicodedata.normalize("NFC", path)
            and isinstance(row["size"], int)
            and not isinstance(row["size"], bool)
            and row["size"] >= 0
            and isinstance(row["sha256"], str)
            and SHA256_PATTERN.fullmatch(row["sha256"]) is not None,
            f"{label} media row is invalid.",
        )
        pure = PurePosixPath(path)
        _require(
            not pure.is_absolute()
            and ".." not in pure.parts
            and "\\" not in path
            and pure.as_posix() == path
            and path not in {"", "."},
            f"{label} media path is unsafe.",
        )
        canonical_path = media_core._canonical_path_key(path)
        _require(canonical_path not in seen, f"{label} contains a canonical duplicate path.")
        seen.add(canonical_path)
        ordered.append(path)
        total += row["size"]
    _require(
        ordered == sorted(ordered, key=lambda item: (media_core._canonical_path_key(item), item))
        and value["file_count"] == len(rows)
        and value["total_size"] == total,
        f"{label} media inventory is not canonical.",
    )
    return {
        "file_count": value["file_count"],
        "total_size": value["total_size"],
        "files": rows,
    }


def _call_validator(label: str, callback: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return callback(*args, **kwargs)
    except Exception as exc:
        raise PairVerificationError(f"{label} is invalid: {exc}") from exc


def _validate_no_live_sidecars(database: Path, *, label: str) -> None:
    _call_validator(label, backup_core._assert_no_live_sidecars, database)


def _validate_backup_report_snapshot(snapshot: Any, *, label: str) -> dict[str, Any]:
    value = _strict_json(snapshot, label=label, canonical=False)
    _require(
        isinstance(value, dict)
        and set(value)
        == {
            "artifact",
            "checks",
            "cutover_authorized",
            "format",
            "format_version",
            "generated_at",
            "inspection_required",
            "verified",
        },
        f"{label} shape is invalid.",
    )
    _require(
        value["format"] == backup_core.REPORT_FORMAT
        and value["format_version"] == backup_core.REPORT_FORMAT_VERSION
        and value["verified"] is True
        and value["cutover_authorized"] is False
        and value["inspection_required"] is True,
        f"{label} status is invalid.",
    )
    _timestamp(value["generated_at"], label=f"{label} generated_at")
    artifact = value["artifact"]
    _require(
        isinstance(artifact, dict)
        and set(artifact) == {"producer_generated_at", "sha256", "size"}
        and isinstance(artifact["size"], int)
        and not isinstance(artifact["size"], bool)
        and artifact["size"] >= 0,
        f"{label} artifact is invalid.",
    )
    _timestamp(
        artifact["producer_generated_at"],
        label=f"{label} producer_generated_at",
    )
    _sha256_value(artifact["sha256"], label=f"{label} artifact sha256")
    _require(
        value["checks"]
        == {
            "checksum_bytes_exact": True,
            "input_set_unchanged": True,
            "metadata_contract": True,
            "sqlite_magic": True,
        },
        f"{label} checks are not all successful.",
    )
    validated = _call_validator(
        label, rehearsal_core._validate_backup_report, snapshot.path
    )
    _require(validated == value, f"{label} validator observed different bytes.")
    return value


def _validate_backup_sidecars(
    database: LargeFileSnapshot,
    checksum: Any,
    metadata: Any,
    *,
    label: str,
) -> dict[str, Any]:
    _validate_no_live_sidecars(database.path, label=label)
    expected_checksum = f"{database.sha256}  {database.path.name}\n".encode("utf-8")
    _require(checksum.raw == expected_checksum, f"{label} checksum bytes are invalid.")
    parsed = _call_validator(label, backup_core._read_metadata, metadata.raw)
    _call_validator(label, backup_core._validate_metadata, parsed)
    _require(
        parsed["sha256"] == database.sha256
        and parsed["size"] == database.size,
        f"{label} metadata does not bind the database.",
    )
    try:
        with database.path.open("rb") as stream:
            magic = stream.read(len(backup_core.SQLITE_MAGIC))
    except OSError as exc:
        raise PairVerificationError(f"{label} database header cannot be read.") from exc
    _require(magic == backup_core.SQLITE_MAGIC, f"{label} SQLite magic is invalid.")
    _validate_no_live_sidecars(database.path, label=label)
    return parsed


def _validate_restriction_report(snapshot: Any, *, label: str) -> dict[str, Any]:
    value = _strict_json(snapshot, label=label, canonical=False)
    _require(
        isinstance(value, dict)
        and set(value)
        == {
            "blocking_errors",
            "counts",
            "generated_at",
            "manual_review",
            "ready_for_cutover",
            "restriction_states",
            "status_visibility",
            "valid",
        },
        f"{label} shape is invalid.",
    )
    try:
        generated_at = datetime.fromisoformat(value["generated_at"].replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise PairVerificationError(f"{label} generated_at is invalid.") from exc
    _require(generated_at.tzinfo is not None, f"{label} generated_at lacks a timezone.")
    counts = value["counts"]
    _require(
        isinstance(counts, dict)
        and set(counts)
        == {
            "active_restrictions_missing_actor",
            "legacy_private_reviews",
            "private_clear_shares",
            "reports",
            "resolved_report_shares",
            "shares",
        }
        and all(
            isinstance(item, int) and not isinstance(item, bool) and item >= 0
            for item in counts.values()
        ),
        f"{label} counts are invalid.",
    )
    manual = value["manual_review"]
    _require(
        isinstance(manual, dict)
        and set(manual) == {"categories", "count", "reason", "share_ids"}
        and manual["categories"] == []
        and manual["count"] == 0
        and isinstance(manual["reason"], str)
        and bool(manual["reason"])
        and manual["share_ids"] == [],
        f"{label} manual-review gate is invalid.",
    )
    _require(
        value["valid"] is True
        and value["ready_for_cutover"] is True
        and value["blocking_errors"] == []
        and isinstance(value["restriction_states"], dict)
        and all(
            isinstance(item, int) and not isinstance(item, bool) and item >= 0
            for item in value["restriction_states"].values()
        )
        and isinstance(value["status_visibility"], list)
        and all(
            isinstance(item, dict)
            and set(item) == {"count", "status", "visibility"}
            and isinstance(item["count"], int)
            and not isinstance(item["count"], bool)
            and item["count"] >= 0
            and isinstance(item["status"], str)
            and isinstance(item["visibility"], str)
            for item in value["status_visibility"]
        ),
        f"{label} restriction gate is invalid.",
    )
    status_rows = value["status_visibility"]
    status_pairs = [(item["status"], item["visibility"]) for item in status_rows]
    restriction_items = list(value["restriction_states"].items())
    _require(
        all(
            status in SHARE_STATUSES and visibility in SHARE_VISIBILITIES
            for status, visibility in status_pairs
        )
        and status_pairs == sorted(set(status_pairs))
        and sum(item["count"] for item in status_rows) == counts["shares"]
        and all(name in SHARE_RESTRICTION_STATES for name, _count in restriction_items)
        and [name for name, _count in restriction_items]
        == sorted(name for name, _count in restriction_items)
        and sum(count for _name, count in restriction_items) == counts["shares"]
        and counts["private_clear_shares"] <= counts["shares"]
        and counts["legacy_private_reviews"] <= counts["shares"]
        and counts["active_restrictions_missing_actor"] <= counts["shares"]
        and counts["resolved_report_shares"] <= counts["shares"]
        and counts["resolved_report_shares"] <= counts["reports"],
        f"{label} aggregate counts or enum projections are inconsistent.",
    )
    _call_validator(label, rehearsal_core._validate_restriction_preflight, snapshot.path)
    projection = dict(value)
    del projection["generated_at"]
    return projection


def _sqlite_schema_projection(path: Path, *, label: str) -> list[dict[str, Any]]:
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            connection.execute("PRAGMA query_only = ON")
            rows = connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise PairVerificationError(f"{label} sqlite_schema cannot be inspected.") from exc
    projection: list[dict[str, Any]] = []
    for kind, name, table, sql in rows:
        if isinstance(name, str) and rehearsal_core._sqlite_identifier_key(
            name
        ).startswith("sqlite_"):
            continue
        _require(
            kind in {"index", "table", "trigger", "view"}
            and isinstance(name, str)
            and isinstance(table, str)
            and (sql is None or isinstance(sql, str)),
            f"{label} sqlite_schema row is invalid.",
        )
        projection.append(
            {"type": kind, "name": name, "tbl_name": table, "sql": sql}
        )
    projection.sort(
        key=lambda item: (
            item["type"],
            item["name"],
            item["tbl_name"],
            item["sql"] is not None,
            "" if item["sql"] is None else item["sql"],
        )
    )
    return projection


def _event_map(ledger: RehearsalLedgerReplay) -> dict[str, dict[str, Any]]:
    return {event["stage"]: event for event in ledger.events}


def _event_artifact(
    *,
    tracker: Any,
    run_root: Path,
    events: dict[str, dict[str, Any]],
    stage: str,
    expected_path: str,
    maximum_size: int = MAX_JSON_BYTES,
) -> Any:
    details = events[stage]["details"]
    _require("artifact" in details, f"Stage {stage} lacks its artifact binding.")
    return _artifact_snapshot(
        tracker,
        run_root,
        details["artifact"],
        label=f"{stage} artifact",
        expected_path=expected_path,
        maximum_size=maximum_size,
    )


def _validate_completion(
    *,
    tracker: Any,
    run_root: Path,
    bootstrap: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    snapshot = tracker.read(
        run_root / "evidence" / "completion.json",
        maximum_size=MAX_JSON_BYTES,
        label="rehearsal bootstrap completion",
    )
    value = _strict_json(snapshot, label="rehearsal bootstrap completion")
    expected_keys = {
        "format",
        "format_version",
        "generated_at",
        "run_id",
        "inner_exit_code",
        "execution_bundle_sha256",
        "execution_bundle_unchanged",
        "bootstrap_record_unchanged",
        "bundle_manifest_unchanged",
        "frozen_policy_unchanged",
        "frozen_proposal_unchanged",
        "frozen_review_unchanged",
        "stdout",
        "stderr",
    }
    _require(isinstance(value, dict) and set(value) == expected_keys, "Completion shape is invalid.")
    _require(
        value["format"] == "ffxivshare-production-copy-bootstrap-completion"
        and value["format_version"] == 1
        and value["run_id"] == bootstrap["run_id"]
        and isinstance(value["inner_exit_code"], int)
        and not isinstance(value["inner_exit_code"], bool)
        and value["inner_exit_code"] == 0
        and value["execution_bundle_sha256"] == policy["execution_bundle_sha256"],
        "Completion authority is invalid.",
    )
    _require(
        all(
            value[name] is True
            for name in (
                "execution_bundle_unchanged",
                "bootstrap_record_unchanged",
                "bundle_manifest_unchanged",
                "frozen_policy_unchanged",
                "frozen_proposal_unchanged",
                "frozen_review_unchanged",
            )
        ),
        "Completion reports frozen evidence drift.",
    )
    _timestamp(value["generated_at"], label="completion generated_at")
    _artifact_snapshot(
        tracker,
        run_root,
        value["stdout"],
        label="inner stdout",
        expected_path="logs/inner.stdout.log",
        maximum_size=64 * 1024 * 1024,
    )
    _artifact_snapshot(
        tracker,
        run_root,
        value["stderr"],
        label="inner stderr",
        expected_path="logs/inner.stderr.log",
        maximum_size=64 * 1024 * 1024,
    )
    return snapshot, value


def _validate_handoff_report(
    value: Any,
    *,
    phase: str,
    expected_handoff_sha256: str,
) -> dict[str, Any]:
    _require(
        isinstance(value, dict)
        and set(value)
        == {
            "format",
            "format_version",
            "generated_at",
            "phase",
            "handoff_sha256",
            "access_baseline",
            "limitations",
        }
        and value["format"]
        == "ffxivshare-production-copy-external-handoff-verification"
        and value["format_version"] == 1
        and value["phase"] == phase
        and value["handoff_sha256"] == expected_handoff_sha256
        and isinstance(value["access_baseline"], dict)
        and isinstance(value["limitations"], dict),
        f"External handoff {phase} report is invalid.",
    )
    _timestamp(value["generated_at"], label=f"external handoff {phase} generated_at")
    return value


def _validate_run(
    *,
    label: str,
    expected_slot: str,
    run_root: Path,
    api: Any,
    policy: dict[str, Any],
    policy_snapshot: Any,
    proposal_snapshot: Any,
    review_snapshot: Any,
    proposal_run_root: Path,
    handoff: dict[str, Any],
    handoff_sha256: str,
) -> RunValidation:
    try:
        root = approval_core._existing_directory(run_root, label=f"{label} RunRoot")
    except Exception as exc:
        raise PairVerificationError(f"{label} RunRoot is invalid: {exc}") from exc
    _validate_private_directory(api, root, label=f"{label} RunRoot")
    directory_identities = approval_core._run_directory_identities(root)
    tracker = approval_core.SnapshotTracker()
    large_snapshots: list[LargeFileSnapshot] = []
    bootstrap_snapshot, bootstrap, arguments, execution_manifest = _validate_bootstrap(
        tracker=tracker,
        run_root=root,
        policy=policy,
        policy_snapshot=policy_snapshot,
        proposal_snapshot=proposal_snapshot,
        review_snapshot=review_snapshot,
    )
    target = next(
        (item for item in handoff["rehearsal_targets"] if item["slot"] == expected_slot),
        None,
    )
    _require(target is not None, f"Handoff does not define the {expected_slot} target slot.")
    expected_arguments = {
        "--source-database": handoff["database_backup_set"]["database"]["path"],
        "--source-checksum": handoff["database_backup_set"]["checksum"]["path"],
        "--source-metadata": handoff["database_backup_set"]["metadata"]["path"],
        "--source-proposal-run-root": str(proposal_run_root),
        "--source-media-manifest": handoff["source_media"]["manifest"]["path"],
        "--target-media-root": target["path"],
        "--target-media-snapshot-id": target["snapshot_id"],
        "--run-root": str(root),
    }
    for name, expected in expected_arguments.items():
        actual = arguments[name]
        if name == "--target-media-snapshot-id":
            _require(actual == expected, f"{label} target snapshot ID is not handoff-bound.")
        else:
            _require(
                _same_path(Path(actual), Path(expected)),
                f"{label} argument {name} is not handoff-bound.",
            )
    completion_snapshot, completion = _validate_completion(
        tracker=tracker, run_root=root, bootstrap=bootstrap, policy=policy
    )
    ledger_snapshot = tracker.read(
        root / "evidence" / "events.jsonl",
        maximum_size=MAX_LEDGER_BYTES,
        label=f"{label} rehearsal ledger",
    )
    ledger = _replay_rehearsal_ledger(ledger_snapshot)
    events = _event_map(ledger)
    result_snapshot = tracker.read(
        root / "evidence" / "result.json",
        maximum_size=MAX_JSON_BYTES,
        label=f"{label} rehearsal result",
    )
    result = _strict_json(result_snapshot, label=f"{label} rehearsal result")
    _validate_result(
        result,
        result_snapshot=result_snapshot,
        ledger_snapshot=ledger_snapshot,
        ledger=ledger,
        expected_workspace_access_control=bootstrap["workspace_access_control"],
    )
    bootstrap_time = _timestamp(bootstrap["generated_at"], label="bootstrap generated_at")
    terminal_time = _timestamp(
        ledger.events[-1]["generated_at"], label="terminal generated_at"
    )
    result_time = _timestamp(result["generated_at"], label="result generated_at")
    completion_time = _timestamp(
        completion["generated_at"], label="completion generated_at"
    )
    _require(
        bootstrap_time <= terminal_time <= result_time <= completion_time,
        f"{label} bootstrap/result/completion timestamps are out of order.",
    )

    created = events["created"]["details"]
    _require(
        created
        == {
            "run_root_create_new": True,
            "workspace_access_control": bootstrap["workspace_access_control"],
            "network_isolation_enforced": False,
            "network_access_observation": "not_measured",
            "live_production_service_access_requested_by_orchestrator": False,
        },
        f"{label} created event is invalid.",
    )
    terminal = events["completed"]["details"]
    _require(
        terminal
        == {
            "status": "completed",
            "issues": [],
            "cutover_authorized": False,
            "network_isolation_enforced": False,
            "network_access_observation": "not_measured",
            "live_production_service_access_requested_by_orchestrator": False,
            "production_copy_read_performed": True,
            "contains_production_user_data": True,
            "retained_on_success": True,
            "secure_disposal_required": True,
        },
        f"{label} terminal event is invalid.",
    )

    runtime_initial_snapshot = _event_artifact(
        tracker=tracker,
        run_root=root,
        events=events,
        stage="runtime_fingerprint_initial_verified",
        expected_path="evidence/runtime-fingerprint-initial.json",
        maximum_size=64 * 1024 * 1024,
    )
    runtime_initial = _call_validator(
        "initial runtime fingerprint",
        rehearsal_core._validate_runtime_fingerprint,
        runtime_initial_snapshot.path,
    )
    runtime_sha256 = runtime_initial["fingerprint_sha256"]
    runtime_initial_details = events["runtime_fingerprint_initial_verified"]["details"]
    _require(
        runtime_sha256 == policy["runtime_fingerprint_sha256"]
        and runtime_initial_details.get("runtime_fingerprint_sha256") == runtime_sha256
        and runtime_initial_details.get("content_rehashed") is True
        and runtime_initial_details.get("runtime_fingerprint_report_sha256")
        == runtime_initial_snapshot.sha256,
        f"{label} runtime fingerprint differs from the approved policy.",
    )
    runtime_report_sha256 = runtime_initial_snapshot.sha256
    for stage, filename, content_rehashed in (
        (
            "runtime_fingerprint_pre_migrate_verified",
            "evidence/runtime-fingerprint-pre-migrate.json",
            False,
        ),
        (
            "runtime_fingerprint_post_migrate_verified",
            "evidence/runtime-fingerprint-post-migrate.json",
            False,
        ),
        (
            "runtime_fingerprint_final_verified",
            "evidence/runtime-fingerprint-final.json",
            True,
        ),
    ):
        snapshot = _event_artifact(
            tracker=tracker,
            run_root=root,
            events=events,
            stage=stage,
            expected_path=filename,
            maximum_size=64 * 1024 * 1024,
        )
        details = events[stage]["details"]
        expected_report_sha256 = (
            snapshot.sha256 if content_rehashed else runtime_report_sha256
        )
        _require(
            details.get("runtime_fingerprint_sha256") == runtime_sha256
            and details.get("content_rehashed") is content_rehashed
            and details.get("runtime_fingerprint_report_sha256")
            == expected_report_sha256,
            f"{label} {stage} authority is invalid.",
        )
        if content_rehashed:
            report = _call_validator(
                stage, rehearsal_core._validate_runtime_fingerprint, snapshot.path
            )
            _require(
                report["fingerprint_sha256"] == runtime_sha256,
                f"{label} final runtime fingerprint drifted.",
            )
        else:
            _call_validator(
                stage,
                rehearsal_core._validate_runtime_identity_checkpoint,
                snapshot.path,
                expected_fingerprint_sha256=runtime_sha256,
                expected_source_report_sha256=runtime_report_sha256,
            )

    approval_details = events["approved_policy_evidence_verified"]["details"]
    _require(
        approval_details.get("policy_id") == policy["policy_id"]
        and approval_details.get("policy_sha256") == policy_snapshot.sha256
        and approval_details.get("proposal_sha256") == proposal_snapshot.sha256
        and approval_details.get("review_record_sha256") == review_snapshot.sha256,
        f"{label} approval replay event is not authority-bound.",
    )
    for phase in ("preflight", "final"):
        stage = f"external_handoff_{phase}_verified"
        snapshot = _event_artifact(
            tracker=tracker,
            run_root=root,
            events=events,
            stage=stage,
            expected_path=f"evidence/external-handoff-{phase}.json",
        )
        report = _validate_handoff_report(
            _strict_json(snapshot, label=f"external handoff {phase}"),
            phase=phase,
            expected_handoff_sha256=handoff_sha256,
        )
        details = events[stage]["details"]
        _require(
            details.get("active_target_slot") == expected_slot
            and details.get("handoff_sha256") == handoff_sha256,
            f"{label} external handoff {phase} target binding is invalid.",
        )
        _require(
            report["access_baseline"] == handoff["access_baseline"]
            and report["limitations"] == handoff["limitations"],
            f"{label} external handoff {phase} differs from the approved handoff.",
        )
        if phase == "preflight":
            handoff_preflight = report
        else:
            _require(
                report["access_baseline"] == handoff_preflight["access_baseline"]
                and report["limitations"] == handoff_preflight["limitations"],
                f"{label} external handoff access baseline drifted.",
            )

    source_private = events["source_private_copy_verified"]["details"]
    _require(
        isinstance(source_private, dict)
        and set(source_private)
        == {
            "database",
            "checksum",
            "metadata",
            "children_receive_original_paths",
        }
        and source_private.get("children_receive_original_paths") is False
        and all(
            isinstance(source_private.get(name), dict)
            and set(source_private[name]) == {"size", "sha256"}
            and isinstance(source_private[name]["size"], int)
            and not isinstance(source_private[name]["size"], bool)
            and source_private[name]["size"] >= 0
            and isinstance(source_private[name]["sha256"], str)
            and SHA256_PATTERN.fullmatch(source_private[name]["sha256"]) is not None
            for name in ("database", "checksum", "metadata")
        )
        and source_private["database"]["sha256"] == policy["source_database_sha256"]
        and all(
            source_private[name]
            == {
                "size": handoff["database_backup_set"][name]["size"],
                "sha256": handoff["database_backup_set"][name]["sha256"],
            }
            for name in ("database", "checksum", "metadata")
        ),
        f"{label} private source-copy event is invalid.",
    )
    source_input_root = root / "work" / "source-input"
    source_database = _stable_large_snapshot(
        source_input_root / Path(arguments["--source-database"]).name,
        expected_size=source_private["database"]["size"],
        expected_sha256=source_private["database"]["sha256"],
        label=f"{label} private source database",
    )
    source_checksum = tracker.read(
        source_input_root / Path(arguments["--source-checksum"]).name,
        maximum_size=max(source_private["checksum"]["size"], 1),
        label=f"{label} private source checksum",
    )
    source_metadata = tracker.read(
        source_input_root / Path(arguments["--source-metadata"]).name,
        maximum_size=max(source_private["metadata"]["size"], 1),
        label=f"{label} private source metadata",
    )
    _require(
        source_checksum.size == source_private["checksum"]["size"]
        and source_checksum.sha256 == source_private["checksum"]["sha256"]
        and source_metadata.size == source_private["metadata"]["size"]
        and source_metadata.sha256 == source_private["metadata"]["sha256"],
        f"{label} private source sidecars differ from their ledger binding.",
    )
    source_metadata_value = _validate_backup_sidecars(
        source_database,
        source_checksum,
        source_metadata,
        label=f"{label} private source backup set",
    )
    large_snapshots.append(source_database)
    source_backup_snapshot = _event_artifact(
        tracker=tracker,
        run_root=root,
        events=events,
        stage="source_artifacts_verified",
        expected_path="evidence/source-backup-set.json",
    )
    source_backup = _validate_backup_report_snapshot(
        source_backup_snapshot,
        label=f"{label} source backup-set report",
    )
    _require(
        source_backup["artifact"]["sha256"] == source_database.sha256
        and source_backup["artifact"]["size"] == source_database.size
        and source_backup["artifact"]["producer_generated_at"]
        == source_metadata_value["generated_at"],
        f"{label} source backup-set report differs from policy.",
    )
    source_inspection_snapshot = _event_artifact(
        tracker=tracker,
        run_root=root,
        events=events,
        stage="source_inspected",
        expected_path="evidence/source-inspection.json",
    )
    source_inspection_validation = _call_validator(
        "source snapshot inspection",
        rehearsal_core._validate_inspection_report,
        source_inspection_snapshot.path,
        expected_sha256=policy["source_database_sha256"],
    )
    source_inspection = source_inspection_validation.report
    source_applied = source_inspection_validation.applied_migrations
    source_sqlite_schema_sha256 = source_inspection_validation.schema_sha256
    _require(
        approval_core._canonical_json_sha256(source_applied)
        == policy["source_applied_migrations_sha256"],
        f"{label} source inspection migration projection differs from policy.",
    )
    _require(
        source_sqlite_schema_sha256 == policy["source_sqlite_schema_sha256"]
        and events["source_inspected"]["details"].get(
            "source_sqlite_schema_sha256"
        )
        == source_sqlite_schema_sha256,
        f"{label} source inspection SQLite schema digest differs from policy or ledger.",
    )

    migration_states: dict[str, dict[str, Any]] = {}
    for key, stage, filename, expected_leaves in (
        (
            "source",
            "source_schema_classified",
            "evidence/source-migration-state-before.json",
            policy["source_leaf_nodes"],
        ),
        (
            "upgraded_source",
            "source_schema_ready",
            "evidence/source-migration-state-after.json",
            policy["target_leaf_nodes"],
        ),
        (
            "target",
            "target_schema_created",
            "evidence/target-migration-state.json",
            policy["target_leaf_nodes"],
        ),
        (
            "final_target",
            "final_target_migration_state_verified",
            "evidence/final-target-migration-state.json",
            policy["target_leaf_nodes"],
        ),
    ):
        snapshot = _event_artifact(
            tracker=tracker,
            run_root=root,
            events=events,
            stage=stage,
            expected_path=filename,
        )
        state = _call_validator(stage, rehearsal_core._validate_migration_state, snapshot.path)
        _require(
            state["migration_runtime_sha256"] == policy["migration_runtime_sha256"]
            and state["applied_leaf_nodes"] == expected_leaves,
            f"{label} {stage} differs from the approved migration authority.",
        )
        migration_states[key] = state
    _require(
        source_applied == migration_states["source"]["applied"],
        f"{label} source inspection and migration-state history differ.",
    )
    _require(
        migration_states["target"]["applied"]
        == migration_states["final_target"]["applied"],
        f"{label} target migration history changed before final backup.",
    )
    upgraded_inspection_snapshot = _event_artifact(
        tracker=tracker,
        run_root=root,
        events=events,
        stage="upgraded_source_inspected",
        expected_path="evidence/upgraded-source-inspection.json",
    )
    upgraded_details = events["upgraded_source_inspected"]["details"]
    upgraded_database_sha256 = upgraded_details.get("database_sha256")
    _sha256_value(
        upgraded_database_sha256,
        label=f"{label} upgraded source database sha256",
    )
    upgraded_inspection_validation = _call_validator(
        "upgraded source snapshot inspection",
        rehearsal_core._validate_inspection_report,
        upgraded_inspection_snapshot.path,
        expected_sha256=upgraded_database_sha256,
    )
    _require(
        upgraded_inspection_validation.applied_migrations
        == migration_states["upgraded_source"]["applied"]
        and upgraded_details.get("sqlite_schema_sha256")
        == upgraded_inspection_validation.schema_sha256
        and upgraded_details.get("table_structures_sha256")
        == upgraded_inspection_validation.table_structures_sha256,
        f"{label} upgraded source inspection differs from migration state or ledger.",
    )

    for stage, filename, callback, kwargs in (
        (
            "dataset_validated",
            "evidence/source-validation.json",
            rehearsal_core._validate_validation_report,
            {},
        ),
        (
            "import_verified",
            "evidence/target-import.json",
            rehearsal_core._validate_import_report,
            {"expected_status": {"imported"}, "expected_target_state": "empty"},
        ),
        (
            "idempotence_verified",
            "evidence/target-import-idempotence.json",
            rehearsal_core._validate_import_report,
            {
                "expected_status": {"already_imported"},
                "expected_target_state": "complete",
            },
        ),
        (
            "target_dataset_validated",
            "evidence/target-validation.json",
            rehearsal_core._validate_validation_report,
            {},
        ),
        (
            "target_export_compared",
            "evidence/site-data-comparison.json",
            rehearsal_core._validate_comparison,
            {},
        ),
        (
            "restriction_preflight",
            "evidence/restriction-preflight.json",
            rehearsal_core._validate_restriction_preflight,
            {},
        ),
        (
            "final_target_dataset_validated",
            "evidence/final-target-validation.json",
            rehearsal_core._validate_validation_report,
            {},
        ),
        (
            "final_target_export_compared",
            "evidence/final-target-site-data-comparison.json",
            rehearsal_core._validate_comparison,
            {},
        ),
        (
            "final_target_restriction_preflight",
            "evidence/final-target-restriction-preflight.json",
            rehearsal_core._validate_restriction_preflight,
            {},
        ),
        (
            "media_verified",
            "evidence/media-comparison.json",
            rehearsal_core._validate_media_comparison,
            {},
        ),
        (
            "final_media_verified",
            "evidence/media-comparison-final.json",
            rehearsal_core._validate_media_comparison,
            {},
        ),
    ):
        snapshot = _event_artifact(
            tracker=tracker,
            run_root=root,
            events=events,
            stage=stage,
            expected_path=filename,
        )
        _call_validator(stage, callback, snapshot.path, **kwargs)

    restriction_projections: dict[str, dict[str, Any]] = {}
    for name, stage, filename in (
        (
            "initial",
            "restriction_preflight",
            "evidence/restriction-preflight.json",
        ),
        (
            "final",
            "final_target_restriction_preflight",
            "evidence/final-target-restriction-preflight.json",
        ),
    ):
        snapshot = _event_artifact(
            tracker=tracker,
            run_root=root,
            events=events,
            stage=stage,
            expected_path=filename,
        )
        restriction_projections[name] = _validate_restriction_report(
            snapshot,
            label=f"{label} {name} restriction preflight",
        )
    _require(
        restriction_projections["initial"] == restriction_projections["final"],
        f"{label} target restriction data changed after initial validation.",
    )

    dataset_roots = (
        root / "artifacts" / "source-export",
        root / "artifacts" / "target-export",
        root / "artifacts" / "final-target-export",
    )
    dataset_projections = tuple(
        (path, _dataset_projection(_inspect_dataset(path, label=f"{label} {path.name}")))
        for path in dataset_roots
    )
    source_dataset = dataset_projections[0][1]
    target_dataset = dataset_projections[1][1]
    final_dataset = dataset_projections[2][1]
    _require(
        target_dataset == final_dataset,
        f"{label} target export differs from its final backup export.",
    )
    source_summary = _inspect_dataset(dataset_roots[0], label=f"{label} source export")
    target_summary = _inspect_dataset(dataset_roots[1], label=f"{label} target export")
    final_summary = _inspect_dataset(dataset_roots[2], label=f"{label} final export")
    for destination, name in ((target_summary, "target"), (final_summary, "final target")):
        issues, checks = comparison_core._compare_summaries(source_summary, destination)
        _require(not issues and all(checks.values()), f"{label} {name} dataset is not source-equivalent.")

    candidate = events["deployment_candidate_verified"]["details"]
    _require(
        isinstance(candidate, dict)
        and candidate.get("cutover_authorized") is False
        and candidate.get("target_media_directory_rescanned") is True
        and candidate.get("target_media_snapshot_id") == target["snapshot_id"]
        and candidate.get("source_media_manifest_sha256")
        == policy["source_media_manifest_sha256"]
        and candidate.get("source_media_snapshot_id")
        == policy["source_media_snapshot_id"],
        f"{label} deployment candidate authority is invalid.",
    )
    target_inspection_snapshot = _artifact_snapshot(
        tracker,
        root,
        candidate["snapshot_inspection"],
        label="target snapshot inspection",
        expected_path="evidence/target-backup-inspection.json",
    )
    target_backup_sha256 = candidate.get("backup_sha256")
    _sha256_value(target_backup_sha256, label="target backup sha256")
    target_inspection_validation = _call_validator(
        "target snapshot inspection",
        rehearsal_core._validate_inspection_report,
        target_inspection_snapshot.path,
        expected_sha256=target_backup_sha256,
    )
    target_inspection = target_inspection_validation.report
    target_applied = target_inspection_validation.applied_migrations
    _require(
        target_applied == migration_states["target"]["applied"],
        f"{label} target backup migration projection differs from target state.",
    )
    target_snapshot_event = events["target_snapshot_verified"]["details"]
    _require(
        target_snapshot_event.get("artifact") == candidate["snapshot_inspection"]
        and target_snapshot_event.get("backup_sha256") == target_backup_sha256
        and target_snapshot_event.get("backup_set") == candidate["backup_set"]
        and target_snapshot_event.get("backup_set_verification")
        == candidate["backup_set_initial_verification"],
        f"{label} initial target-snapshot ledger binding is invalid.",
    )
    structure_projection = rehearsal_core._database_structure_preservation_projection(
        source_inspection_validation,
        upgraded_inspection_validation,
        target_inspection_validation,
    )
    _require(
        structure_projection["preserved"] is True
        and structure_projection["issues"] == [],
        f"{label} independently recomputed database structure preservation failed.",
    )
    structure_event = events["database_structure_preserved"]["details"]
    structure_snapshot = _artifact_snapshot(
        tracker,
        root,
        candidate["database_structure_preservation"],
        label="database structure preservation",
        expected_path="evidence/database-structure-preservation.json",
    )
    _call_validator(
        "database structure preservation report",
        rehearsal_core._validate_structure_preservation_report,
        structure_snapshot.path,
        expected_projection=structure_projection,
    )
    _require(
        structure_event.get("artifact")
        == candidate["database_structure_preservation"]
        and structure_event.get("source_schema_sha256")
        == source_inspection_validation.schema_sha256
        and structure_event.get("upgraded_source_schema_sha256")
        == upgraded_inspection_validation.schema_sha256
        and structure_event.get("final_target_schema_sha256")
        == target_inspection_validation.schema_sha256,
        f"{label} structure-preservation ledger binding is invalid.",
    )
    initial_backup_snapshot = _artifact_snapshot(
        tracker,
        root,
        candidate["backup_set_initial_verification"],
        label="initial target backup-set verification",
        expected_path="evidence/target-backup-set.json",
    )
    final_backup_snapshot = _artifact_snapshot(
        tracker,
        root,
        candidate["backup_set_final_verification"],
        label="final target backup-set verification",
        expected_path="evidence/target-backup-set-final.json",
    )
    initial_backup = _validate_backup_report_snapshot(
        initial_backup_snapshot,
        label=f"{label} initial target backup-set",
    )
    final_backup = _validate_backup_report_snapshot(
        final_backup_snapshot,
        label=f"{label} final target backup-set",
    )
    _require(
        initial_backup["artifact"]["sha256"] == target_backup_sha256
        and final_backup["artifact"]["sha256"] == target_backup_sha256,
        f"{label} target backup changed during rehearsal.",
    )
    final_snapshot_event = events["target_snapshot_set_final_verified"]["details"]
    _require(
        final_snapshot_event.get("artifact")
        == candidate["backup_set_final_verification"]
        and final_snapshot_event.get("backup_sha256") == target_backup_sha256
        and final_snapshot_event.get("backup_set") == candidate["backup_set"]
        and final_snapshot_event.get("backup_set_unchanged") is True,
        f"{label} final target-snapshot ledger binding is invalid.",
    )
    for name, expected_path in (
        ("final_site_data_comparison", "evidence/final-target-site-data-comparison.json"),
        ("final_restriction_preflight", "evidence/final-target-restriction-preflight.json"),
        ("target_media_final_comparison", "evidence/media-comparison-final.json"),
    ):
        _artifact_snapshot(
            tracker,
            root,
            candidate[name],
            label=f"deployment candidate {name}",
            expected_path=expected_path,
        )
    backup_set = candidate.get("backup_set")
    _require(
        isinstance(backup_set, dict) and set(backup_set) == {"database", "checksum", "metadata"},
        f"{label} deployment backup set shape is invalid.",
    )
    target_database = _large_artifact_snapshot(
        root,
        backup_set["database"],
        label=f"{label} target backup database",
        expected_path="artifacts/target-backup/ffxivshare.sqlite3",
    )
    target_checksum = _artifact_snapshot(
        tracker,
        root,
        backup_set["checksum"],
        label=f"{label} target backup checksum",
        expected_path="artifacts/target-backup/ffxivshare.sqlite3.sha256",
        maximum_size=max(int(backup_set["checksum"].get("size", 0)), 1),
    )
    target_metadata = _artifact_snapshot(
        tracker,
        root,
        backup_set["metadata"],
        label=f"{label} target backup metadata",
        expected_path="artifacts/target-backup/ffxivshare.sqlite3.metadata.json",
        maximum_size=max(int(backup_set["metadata"].get("size", 0)), 1),
    )
    _require(
        target_database.sha256 == target_backup_sha256
        and initial_backup["artifact"]["sha256"] == target_database.sha256
        and initial_backup["artifact"]["size"] == target_database.size
        and final_backup["artifact"]["sha256"] == target_database.sha256
        and final_backup["artifact"]["size"] == target_database.size,
        f"{label} target backup reports do not bind the final database bytes.",
    )
    target_metadata_value = _validate_backup_sidecars(
        target_database,
        target_checksum,
        target_metadata,
        label=f"{label} target backup set",
    )
    _require(
        initial_backup["artifact"]["producer_generated_at"]
        == target_metadata_value["generated_at"]
        and final_backup["artifact"]["producer_generated_at"]
        == target_metadata_value["generated_at"],
        f"{label} target backup reports do not bind metadata generation time.",
    )
    large_snapshots.append(target_database)

    source_media_snapshot = _artifact_snapshot(
        tracker,
        root,
        events["media_verified"]["details"]["source_manifest"],
        label="frozen source media manifest",
        expected_path="artifacts/source-media-manifest.json",
    )
    source_media = _strict_json(
        source_media_snapshot,
        label="frozen source media manifest",
        canonical=False,
    )
    _require(
        _call_validator(
            "frozen source media manifest", media_core._load_manifest, source_media_snapshot.path
        )
        == source_media,
        "Frozen source media validator observed different bytes.",
    )
    source_media_projection = _media_projection(source_media, label="source media manifest")
    _require(
        source_media_snapshot.sha256 == policy["source_media_manifest_sha256"]
        and source_media["source_snapshot"]["id"] == policy["source_media_snapshot_id"],
        f"{label} source media manifest differs from policy.",
    )
    final_media_snapshot = _artifact_snapshot(
        tracker,
        root,
        candidate["target_media_manifest"],
        label="final target media manifest",
        expected_path="artifacts/target-media-manifest-final.json",
    )
    final_media = _strict_json(
        final_media_snapshot,
        label="final target media manifest",
        canonical=False,
    )
    _require(
        _call_validator(
            "final target media manifest", media_core._load_manifest, final_media_snapshot.path
        )
        == final_media,
        "Final target media validator observed different bytes.",
    )
    final_media_projection = _media_projection(final_media, label="final target media manifest")
    initial_media_snapshot = _artifact_snapshot(
        tracker,
        root,
        candidate["target_media_initial_manifest"],
        label="initial target media manifest",
        expected_path="artifacts/target-media-manifest.json",
    )
    initial_media = _strict_json(
        initial_media_snapshot,
        label="initial target media manifest",
        canonical=False,
    )
    _require(
        _call_validator(
            "initial target media manifest", media_core._load_manifest, initial_media_snapshot.path
        )
        == initial_media,
        "Initial target media validator observed different bytes.",
    )
    initial_media_projection = _media_projection(
        initial_media, label="initial target media manifest"
    )
    _require(
        initial_media_projection == source_media_projection
        and final_media_projection == source_media_projection
        and initial_media["source_snapshot"]["id"] == target["snapshot_id"]
        and final_media["source_snapshot"]["id"] == target["snapshot_id"],
        f"{label} final target media inventory differs from source.",
    )
    _require(
        events["media_verified"]["details"].get("target_manifest")
        == candidate["target_media_initial_manifest"]
        and events["final_media_verified"]["details"].get("target_manifest")
        == candidate["target_media_manifest"],
        f"{label} target media ledger bindings are invalid.",
    )

    source_final = events["source_final_verified"]["details"]
    _require(
        source_final.get("source_unchanged") is True
        and source_final.get("backup_set") == {
            "database": source_private["database"],
            "checksum": source_private["checksum"],
            "metadata": source_private["metadata"],
        },
        f"{label} final source checkpoint is invalid.",
    )
    _require(
        events["execution_bundle_pre_migrate_verified"]["details"]
        == {"execution_bundle_sha256": policy["execution_bundle_sha256"]}
        and events["execution_bundle_final_verified"]["details"]
        == {
            "bundle_unchanged": True,
            "execution_bundle_sha256": policy["execution_bundle_sha256"],
        },
        f"{label} execution bundle checkpoint is invalid.",
    )
    target_backup_semantics = {
        "query_only": target_inspection["inspection"]["query_only"],
        "integrity_check": target_inspection["inspection"]["integrity_check"],
        "foreign_key_check": target_inspection["inspection"]["foreign_key_check"],
        "user_version": target_inspection["inspection"]["user_version"],
        "tables": target_inspection["inspection"]["tables"],
        "django_migrations": {
            "present": target_inspection["inspection"]["django_migrations"]["present"],
            "count": target_inspection["inspection"]["django_migrations"]["count"],
            "applied": [
                {"app": item["app"], "name": item["name"]}
                for item in target_inspection["inspection"]["django_migrations"]["applied"]
            ],
        },
        "sqlite_sequence": target_inspection["inspection"]["sqlite_sequence"],
        "table_structures": target_inspection["inspection"]["table_structures"],
        "sqlite_schema": _sqlite_schema_projection(
            target_database.path,
            label=f"{label} target backup",
        ),
    }
    semantic_projection = {
        "source_sqlite_schema_sha256": source_sqlite_schema_sha256,
        "database_structure_preservation": structure_projection,
        "source_backup_set": {
            name: source_private[name] for name in ("database", "checksum", "metadata")
        },
        "runtime_fingerprint_sha256": runtime_sha256,
        "migration_states": migration_states,
        "source_dataset": source_dataset,
        "target_dataset": target_dataset,
        "final_target_dataset": final_dataset,
        "source_media": source_media_projection,
        "final_target_media": final_media_projection,
        "external_access_baseline": handoff_preflight["access_baseline"],
        "handoff_limitations": handoff_preflight["limitations"],
        "target_backup_inspection": target_backup_semantics,
        "restriction_preflight": restriction_projections["final"],
        "completed_stages": list(SUCCESS_STAGE_SEQUENCE[:-1]),
        "cutover_authorized": False,
    }
    business_summary = {
        "entity_inventory_sha256": approval_core._canonical_json_sha256(
            final_dataset["entities"]
        ),
        "entity_counts": {
            name: item["count"] for name, item in final_dataset["entities"].items()
        },
        "record_count": final_dataset["record_count"],
        "media_inventory_sha256": approval_core._canonical_json_sha256(
            final_media_projection["files"]
        ),
        "media_file_count": final_media_projection["file_count"],
        "media_total_size": final_media_projection["total_size"],
        "applied_migrations_sha256": approval_core._canonical_json_sha256(
            migration_states["final_target"]["applied"]
        ),
        "target_leaf_nodes": migration_states["final_target"]["applied_leaf_nodes"],
        "target_backup_semantics_sha256": approval_core._canonical_json_sha256(
            target_backup_semantics
        ),
    }
    validation = RunValidation(
        label=label,
        run_root=root,
        tracker=tracker,
        directory_identities=directory_identities,
        bootstrap_snapshot=bootstrap_snapshot,
        completion_snapshot=completion_snapshot,
        result_snapshot=result_snapshot,
        ledger_snapshot=ledger_snapshot,
        bootstrap=bootstrap,
        completion=completion,
        result=result,
        ledger=ledger,
        target_slot=expected_slot,
        target_media_root=target["path"],
        target_media_snapshot_id=target["snapshot_id"],
        target_backup_sha256=target_backup_sha256,
        target_backup_size=target_database.size,
        semantic_projection=semantic_projection,
        business_summary=business_summary,
        dataset_projections=dataset_projections,
        large_snapshots=tuple(large_snapshots),
        code_root=root / "code",
        execution_membership=tuple(
            item["path"] for item in execution_manifest["files"]
        ),
    )
    validation.revalidate()
    return validation


def _run_report(validation: RunValidation) -> dict[str, Any]:
    return {
        "run_root": str(validation.run_root),
        "run_id": validation.bootstrap["run_id"],
        "bootstrap_nonce": validation.bootstrap["bootstrap_nonce"],
        "target_slot": validation.target_slot,
        "target_media_root": validation.target_media_root,
        "target_media_snapshot_id": validation.target_media_snapshot_id,
        "target_backup_sha256": validation.target_backup_sha256,
        "target_backup_size": validation.target_backup_size,
        "timestamps": {
            "bootstrap": validation.bootstrap["generated_at"],
            "terminal_event": validation.ledger.events[-1]["generated_at"],
            "result": validation.result["generated_at"],
            "completion": validation.completion["generated_at"],
        },
        "log_artifacts": {
            "stdout": validation.completion["stdout"],
            "stderr": validation.completion["stderr"],
        },
        "completion": _reference(
            validation.completion_snapshot.path,
            validation.completion_snapshot,
            validation.run_root,
        ),
        "result": _reference(
            validation.result_snapshot.path,
            validation.result_snapshot,
            validation.run_root,
        ),
        "ledger": {
            **_reference(
                validation.ledger_snapshot.path,
                validation.ledger_snapshot,
                validation.run_root,
            ),
            "event_count": validation.ledger.event_count,
            "head_event_sha256": validation.ledger.head_event_sha256,
        },
        "semantic_projection_sha256": approval_core._canonical_json_sha256(
            validation.semantic_projection
        ),
        "business_summary": validation.business_summary,
    }


def _allowed_difference_values(
    first: RunValidation, second: RunValidation
) -> dict[str, dict[str, Any]]:
    first_report = _run_report(first)
    second_report = _run_report(second)

    def pair(first_value: Any, second_value: Any) -> dict[str, Any]:
        return {"first": first_value, "second": second_value}

    return {
        "run_root": pair(str(first.run_root), str(second.run_root)),
        "run_id": pair(first.bootstrap["run_id"], second.bootstrap["run_id"]),
        "bootstrap_nonce": pair(
            first.bootstrap["bootstrap_nonce"], second.bootstrap["bootstrap_nonce"]
        ),
        "generated_at": pair(
            first_report["timestamps"], second_report["timestamps"]
        ),
        "target_media_slot": pair(first.target_slot, second.target_slot),
        "target_media_root": pair(first.target_media_root, second.target_media_root),
        "target_media_snapshot_id": pair(
            first.target_media_snapshot_id, second.target_media_snapshot_id
        ),
        "log_artifacts": pair(
            first_report["log_artifacts"], second_report["log_artifacts"]
        ),
        "target_backup_bytes": pair(
            {
                "size": first.target_backup_size,
                "sha256": first.target_backup_sha256,
            },
            {
                "size": second.target_backup_size,
                "sha256": second.target_backup_sha256,
            },
        ),
    }


def _revalidate_all(binding: Any, first: RunValidation, second: RunValidation) -> None:
    try:
        binding.revalidate()
        first.revalidate()
        second.revalidate()
    except PairVerificationError:
        raise
    except Exception as exc:
        raise PairVerificationError(f"Tracked input changed during pair verification: {exc}") from exc


def _validate_current_python_identity(expected: Any) -> None:
    _require(isinstance(expected, dict), "Recorded Python identity is invalid.")
    current_executable = Path(sys.executable)
    current_snapshot = _stable_large_snapshot(
        current_executable,
        expected_size=expected["executable_size"],
        expected_sha256=expected["executable_sha256"],
        label="pair-verifier Python executable",
    )
    current_flags = {
        "ignore_environment": bool(sys.flags.ignore_environment),
        "no_user_site": bool(sys.flags.no_user_site),
        "dont_write_bytecode": bool(sys.flags.dont_write_bytecode),
        "utf8_mode": bool(sys.flags.utf8_mode),
        "isolated": bool(sys.flags.isolated),
        "no_site": bool(sys.flags.no_site),
        "safe_path": bool(sys.flags.safe_path),
    }
    _require(
        _same_path(current_snapshot.path, Path(expected["executable"]))
        and platform.python_implementation() == expected["implementation"]
        and platform.python_version() == expected["version"]
        and sys.implementation.cache_tag == expected["cache_tag"]
        and current_flags == expected["isolation_flags"],
        "Pair verifier is not running with the rehearsals' recorded Python identity.",
    )


def verify_rehearsal_pair(config: PairVerificationConfig) -> Path:
    try:
        approval_core._assert_isolated_stdlib_runtime()
        handoff_core._require_windows_live_check()
    except Exception as exc:
        raise PairVerificationError(str(exc)) from exc
    for value, label in (
        (config.expected_policy_sha256, "expected policy sha256"),
        (config.expected_proposal_sha256, "expected proposal sha256"),
        (config.expected_review_sha256, "expected review sha256"),
    ):
        _sha256_value(value, label=label)
    try:
        first_root = approval_core._existing_directory(
            config.first_run_root, label="First Rehearsal RunRoot"
        )
        second_root = approval_core._existing_directory(
            config.second_run_root, label="Second Rehearsal RunRoot"
        )
        proposal_root = approval_core._existing_directory(
            config.proposal_run_root, label="Proposal RunRoot"
        )
    except Exception as exc:
        raise PairVerificationError(f"Pair root is invalid: {exc}") from exc
    _require(
        not _path_overlap(first_root, second_root),
        "First and second Rehearsal RunRoots must be distinct and non-overlapping.",
    )
    _require(
        not _path_overlap(first_root, proposal_root)
        and not _path_overlap(second_root, proposal_root),
        "Rehearsal and Proposal RunRoots must be non-overlapping.",
    )
    expected_tool = (
        first_root
        / "code"
        / "ops"
        / "migration"
        / "Verify-ProductionCopyRehearsalPair.py"
    )
    _require(
        _same_path(Path(__file__).resolve(), expected_tool),
        "Pair verifier must execute from the first Rehearsal RunRoot frozen code bundle.",
    )
    try:
        output_raw = approval_core._absolute_local_path(config.output, label="Pair output")
        api = handoff_core._Win32Api()
        output = Path(handoff_core._validate_output_parent(api, os.fspath(output_raw)))
    except Exception as exc:
        raise PairVerificationError(f"Pair output parent is invalid: {exc}") from exc
    _require(
        not any(
            _path_overlap(output.parent, root)
            for root in (first_root, second_root, proposal_root)
        ),
        "Pair output parent must not overlap Proposal or Rehearsal RunRoots.",
    )

    try:
        policy_snapshot = approval_core._stable_snapshot(
            approval_core._absolute_local_path(config.policy, label="Approved policy"),
            maximum_size=approval_core.MAX_JSON_BYTES,
            label="Approved policy",
        )
    except Exception as exc:
        raise PairVerificationError(f"Approved policy cannot be read safely: {exc}") from exc
    _require(
        policy_snapshot.sha256 == config.expected_policy_sha256,
        "Approved policy SHA-256 does not match the expected authority.",
    )
    try:
        binding = approval_core._validate_binding(
            proposal_path=approval_core._absolute_local_path(
                config.proposal, label="Policy proposal"
            ),
            review_path=approval_core._absolute_local_path(
                config.review, label="Policy review"
            ),
            proposal_run_root=proposal_root,
            expected_proposal_sha256=config.expected_proposal_sha256,
            expected_review_sha256=config.expected_review_sha256,
            reviewer=None,
            require_source_proposal_path=False,
        )
        binding.tracker.snapshots[_canonical_key(policy_snapshot.path)] = policy_snapshot
        policy = approval_core._load_json(
            policy_snapshot, label="approved policy", canonical=True
        )
        policy = approval_core._validate_policy(policy, binding=binding)
        binding.revalidate()
    except Exception as exc:
        raise PairVerificationError(f"Approved Proposal/Review/Policy binding is invalid: {exc}") from exc

    handoff_reference = binding.body["evidence"]["source_handoff_manifest"]
    handoff_snapshot = _artifact_snapshot(
        binding.tracker,
        proposal_root,
        handoff_reference,
        label="approved source handoff",
        expected_path="artifacts/source-handoff-manifest.json",
        maximum_size=handoff_core.MAX_HANDOFF_BYTES,
    )
    try:
        handoff = handoff_core.validate_handoff(
            approval_core._load_json(
                handoff_snapshot, label="approved source handoff", canonical=True
            )
        )
    except Exception as exc:
        raise PairVerificationError(f"Approved source handoff is invalid: {exc}") from exc
    _require(
        handoff["database_backup_set"]["database"]["sha256"]
        == policy["source_database_sha256"]
        and handoff["source_media"]["manifest"]["sha256"]
        == policy["source_media_manifest_sha256"]
        and handoff["source_media"]["snapshot_id"]
        == policy["source_media_snapshot_id"],
        "Approved handoff differs from the Policy source authority.",
    )
    first = _validate_run(
        label="first",
        expected_slot="first",
        run_root=first_root,
        api=api,
        policy=policy,
        policy_snapshot=policy_snapshot,
        proposal_snapshot=binding.proposal_snapshot,
        review_snapshot=binding.review_snapshot,
        proposal_run_root=proposal_root,
        handoff=handoff,
        handoff_sha256=handoff_snapshot.sha256,
    )
    second = _validate_run(
        label="second",
        expected_slot="second",
        run_root=second_root,
        api=api,
        policy=policy,
        policy_snapshot=policy_snapshot,
        proposal_snapshot=binding.proposal_snapshot,
        review_snapshot=binding.review_snapshot,
        proposal_run_root=proposal_root,
        handoff=handoff,
        handoff_sha256=handoff_snapshot.sha256,
    )
    def verify_current_live_handoff() -> dict[str, Any]:
        try:
            current = handoff_core.verify_live_handoff(
                handoff,
                first_root / "code",
                disallowed_roots=(
                    first_root,
                    second_root,
                    proposal_root,
                    handoff_snapshot.path,
                    output.parent,
                ),
                verify_content=True,
            )
        except Exception as exc:
            raise PairVerificationError(
                f"Live source handoff changed after the two rehearsals: {exc}"
            ) from exc
        _require(
            current == handoff["access_baseline"],
            "Live source handoff access baseline differs from the approved authority.",
        )
        return current

    live_access_baseline = verify_current_live_handoff()
    _require(
        first.bootstrap["python"] == second.bootstrap["python"],
        "Rehearsals used different Python runtime identities.",
    )
    _validate_current_python_identity(first.bootstrap["python"])
    _require(
        first.bootstrap["run_id"] != second.bootstrap["run_id"]
        and first.bootstrap["bootstrap_nonce"]
        != second.bootstrap["bootstrap_nonce"],
        "Rehearsals must have distinct run IDs and bootstrap nonces.",
    )
    comparison = _compare_semantic_projections(
        first.semantic_projection, second.semantic_projection
    )
    comparison["allowed_difference_values"] = _allowed_difference_values(first, second)
    comparison["unexplained_differences"] = []
    live_access_sha256 = approval_core._canonical_json_sha256(live_access_baseline)
    authority = {
        "policy_sha256": policy_snapshot.sha256,
        "proposal_sha256": binding.proposal_snapshot.sha256,
        "review_sha256": binding.review_snapshot.sha256,
        "policy_id": policy["policy_id"],
        "proposal_id": policy["proposal_id"],
        "review_id": policy["review_id"],
        "handoff_sha256": handoff_snapshot.sha256,
        "live_handoff_access_baseline_sha256": live_access_sha256,
        "source_database_sha256": policy["source_database_sha256"],
        "source_media_manifest_sha256": policy["source_media_manifest_sha256"],
        "source_media_snapshot_id": policy["source_media_snapshot_id"],
        "source_applied_migrations_sha256": policy[
            "source_applied_migrations_sha256"
        ],
        "source_sqlite_schema_sha256": policy["source_sqlite_schema_sha256"],
        "database_structure_preservation_sha256": (
            approval_core._canonical_json_sha256(
                first.semantic_projection["database_structure_preservation"]
            )
        ),
        "migration_plan_sha256": policy["migration_plan_sha256"],
        "migration_runtime_sha256": policy["migration_runtime_sha256"],
        "runtime_fingerprint_sha256": policy["runtime_fingerprint_sha256"],
        "execution_bundle_sha256": policy["execution_bundle_sha256"],
        "source_leaf_nodes": policy["source_leaf_nodes"],
        "target_leaf_nodes": policy["target_leaf_nodes"],
    }
    report = {
        "format": REPORT_FORMAT,
        "format_version": REPORT_VERSION,
        "generated_at": datetime.now(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        "status": "verified",
        "verification": "self_consistent_local_chain",
        "tamper_proof": False,
        "contains_production_user_data": True,
        "retained_on_success": True,
        "secure_disposal_required": True,
        "sensitive_retention_scope": "pair_report_and_referenced_run_roots",
        "authority": authority,
        "live_handoff_final_verification": {
            "content_reverified": True,
            "access_baseline_matches_approved_handoff": True,
            "access_baseline_sha256": live_access_sha256,
        },
        "runs": {"first": _run_report(first), "second": _run_report(second)},
        "comparison": comparison,
        "cutover_authorized": False,
    }
    _revalidate_all(binding, first, second)
    try:
        publication_output = Path(
            handoff_core._validate_output_parent(api, os.fspath(output))
        )
    except Exception as exc:
        raise PairVerificationError(
            f"Pair output parent changed before publication: {exc}"
        ) from exc
    _require(
        _same_path(publication_output, output),
        "Pair output path identity changed before publication.",
    )
    publication_identities: list[tuple[int, int]] = []
    try:
        handoff_core._write_create_new(
            os.fspath(output),
            report,
            publication_identity=publication_identities,
        )
        handoff_core._validate_published_output(api, os.fspath(output))
        _revalidate_all(binding, first, second)
        published = approval_core._stable_snapshot(
            output,
            maximum_size=MAX_JSON_BYTES,
            label="published pair-verification report",
        )
        _require(
            bool(publication_identities)
            and (published.identity[0], published.identity[1])
            == publication_identities[0],
            "Published pair-verification report identity is invalid.",
        )
        parsed = approval_core._load_json(
            published, label="published pair-verification report", canonical=True
        )
        _require(parsed == report, "Published pair-verification report bytes are invalid.")
        _revalidate_all(binding, first, second)
        final_live_access = verify_current_live_handoff()
        _require(
            approval_core._canonical_json_sha256(final_live_access)
            == live_access_sha256,
            "Live source handoff changed during pair-report publication.",
        )
        handoff_core._validate_published_output(api, os.fspath(output))
        final_published = approval_core._stable_snapshot(
            output,
            maximum_size=MAX_JSON_BYTES,
            label="final published pair-verification report",
        )
        _require(
            final_published.identity == published.identity
            and final_published.size == published.size
            and final_published.sha256 == published.sha256
            and final_published.raw == published.raw,
            "Published pair-verification report changed during final input validation.",
        )
    except BaseException:
        if publication_identities:
            problem = handoff_core._unlink_if_identity(
                os.fspath(output),
                publication_identities[0],
                label="pair-verification report",
            )
            if problem is not None:
                print(
                    f"Production-copy rehearsal pair cleanup warning: {problem}",
                    file=sys.stderr,
                )
        raise
    return output


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fail-closed verification of two completed, approved FFXIVShare "
            "production-copy rehearsals."
        )
    )
    parser.add_argument("--first-run-root", required=True)
    parser.add_argument("--second-run-root", required=True)
    parser.add_argument("--proposal-run-root", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--proposal", required=True)
    parser.add_argument("--review", required=True)
    parser.add_argument("--expected-policy-sha256", required=True)
    parser.add_argument("--expected-proposal-sha256", required=True)
    parser.add_argument("--expected-review-sha256", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> int:
    arguments = _parse_arguments()
    config = PairVerificationConfig(
        first_run_root=Path(arguments.first_run_root),
        second_run_root=Path(arguments.second_run_root),
        proposal_run_root=Path(arguments.proposal_run_root),
        policy=Path(arguments.policy),
        proposal=Path(arguments.proposal),
        review=Path(arguments.review),
        expected_policy_sha256=arguments.expected_policy_sha256,
        expected_proposal_sha256=arguments.expected_proposal_sha256,
        expected_review_sha256=arguments.expected_review_sha256,
        output=Path(arguments.output),
    )
    try:
        output = verify_rehearsal_pair(config)
    except KeyboardInterrupt:
        print("Production-copy rehearsal pair verification interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(
            f"Production-copy rehearsal pair verification failed: {exc}",
            file=sys.stderr,
        )
        return 1
    print(f"Production-copy rehearsal pair verified: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
