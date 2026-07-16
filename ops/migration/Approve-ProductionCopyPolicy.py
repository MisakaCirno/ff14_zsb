from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import signal
import stat
import sys
from typing import Any


PROPOSAL_FORMAT = "ffxivshare-source-upgrade-policy-proposal"
PROPOSAL_VERSION = 1
PROPOSAL_BODY_FORMAT = "ffxivshare-source-upgrade-policy-proposal-body"
PROPOSAL_BODY_VERSION = 1
REVIEW_PLAN_FORMAT = "ffxivshare-migration-review-plan"
REVIEW_PLAN_VERSION = 1
POLICY_FORMAT = "ffxivshare-source-upgrade-policy"
POLICY_VERSION = 2
REVIEW_FORMAT = "ffxivshare-migration-lossless-review"
REVIEW_VERSION = 1
LEDGER_FORMAT = "ffxivshare-production-copy-rehearsal-event"
LEDGER_VERSION = 1
BOOTSTRAP_FORMAT = "ffxivshare-production-copy-bootstrap"
BOOTSTRAP_VERSION = 1
BOOTSTRAP_COMPLETION_FORMAT = "ffxivshare-production-copy-bootstrap-completion"
BOOTSTRAP_COMPLETION_VERSION = 1
EXECUTION_INVENTORY_FORMAT = "ffxivshare-production-copy-execution-bundle"
EXECUTION_INVENTORY_VERSION = 1
REVIEWER_IDENTITY_VERIFICATION = "operator_asserted_not_cryptographically_verified"
ZERO_SHA256 = "0" * 64
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MIGRATION_PART_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
UTC_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
MAX_LEDGER_BYTES = 64 * 1024 * 1024
MAX_LEDGER_EVENTS = 10_000
MAX_REVIEW_NOTES_CHARS = 16_384

REQUIRED_EVIDENCE = (
    "bootstrap",
    "execution_inventory",
    "migration_plan",
    "migration_review_plan",
    "runtime_fingerprint",
    "source_backup_verification",
    "source_media_manifest",
    "source_migration_state",
    "source_snapshot_inspection",
)
EXPECTED_EVIDENCE_PATHS = {
    "bootstrap": "evidence/bootstrap.json",
    "execution_inventory": "evidence/execution-bundle.json",
    "migration_review_plan": "evidence/proposal-migration-review-plan.json",
    "runtime_fingerprint": "evidence/proposal-runtime-fingerprint.json",
    "source_backup_verification": "evidence/proposal-source-backup-set.json",
    "source_media_manifest": "artifacts/source-media-manifest.json",
    "source_migration_state": "evidence/proposal-source-migration-state.json",
    "source_snapshot_inspection": "evidence/proposal-source-inspection.json",
}
MANDATORY_EXECUTION_FILES = {
    "manage.py",
    "requirements.txt",
    "ops/migration/ProductionCopyBootstrap.py",
    "ops/migration/Rehearse-ProductionCopy.py",
    "ops/migration/Approve-ProductionCopyPolicy.py",
    "ops/migration/Propose-ProductionCopyPolicy.py",
    "ops/migration/Verify-SQLiteBackupSet.py",
    "ops/migration/Inspect-SQLiteSnapshot.py",
    "ops/migration/Compare-SiteDataExports.py",
    "ops/migration/MediaManifest.py",
}
PROJECTION_KEYS = {
    "format",
    "format_version",
    "policy_id",
    "source_database_sha256",
    "source_media_manifest_sha256",
    "source_media_snapshot_id",
    "source_applied_migrations_sha256",
    "migration_runtime_sha256",
    "runtime_fingerprint_sha256",
    "execution_bundle_sha256",
    "source_leaf_nodes",
    "target_leaf_nodes",
    "migration_plan_sha256",
}
POLICY_AUDIT_KEYS = {
    "approved",
    "approved_at",
    "lossless_reviewed",
    "proposal_id",
    "proposal_sha256",
    "proposal_body_sha256",
    "proposal_run_id",
    "proposal_bootstrap_nonce",
    "proposal_evidence_set_sha256",
    "proposal_ledger_head_sha256",
    "proposal_ledger_event_count",
    "proposal_bootstrap_completion_sha256",
    "review_id",
    "reviewed_at",
    "review_record_sha256",
    "reviewer",
    "reviewer_identity_verification",
    "approval_tool_sha256",
}
POLICY_KEYS = PROJECTION_KEYS | POLICY_AUDIT_KEYS
REVIEW_KEYS = {
    "format",
    "format_version",
    "review_id",
    "reviewed_at",
    "reviewer",
    "reviewer_identity_verification",
    "proposal_sha256",
    "proposal_body_sha256",
    "evidence_set_sha256",
    "conclusion",
    "migrations_reviewed",
    "notes",
}


class ApprovalError(RuntimeError):
    pass


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    raw: bytes
    size: int
    sha256: str
    identity: tuple[int, int, int, int, int]


@dataclass
class SnapshotTracker:
    snapshots: dict[str, FileSnapshot] = field(default_factory=dict)

    def read(self, path: Path, *, maximum_size: int, label: str) -> FileSnapshot:
        key = _canonical_key(path)
        existing = self.snapshots.get(key)
        if existing is not None:
            return existing
        snapshot = _stable_snapshot(path, maximum_size=maximum_size, label=label)
        self.snapshots[key] = snapshot
        return snapshot

    def revalidate(self) -> None:
        for snapshot in tuple(self.snapshots.values()):
            current = _stable_snapshot(
                snapshot.path,
                maximum_size=max(snapshot.size, 1),
                label=f"Tracked artifact {snapshot.path.name}",
            )
            if (
                current.identity != snapshot.identity
                or current.size != snapshot.size
                or current.sha256 != snapshot.sha256
                or current.raw != snapshot.raw
            ):
                raise ApprovalError(f"Tracked artifact changed: {snapshot.path}")


@dataclass(frozen=True)
class LedgerReplay:
    events: tuple[dict[str, Any], ...]
    event_count: int
    head_event_sha256: str


@dataclass
class BindingValidation:
    tracker: SnapshotTracker
    run_root: Path
    approval_directory: Path
    proposal_snapshot: FileSnapshot
    review_snapshot: FileSnapshot
    completion_snapshot: FileSnapshot
    tool_snapshot: FileSnapshot
    proposal: dict[str, Any]
    body: dict[str, Any]
    review: dict[str, Any]
    projection: dict[str, Any]
    ledger: LedgerReplay
    pending_nodes: list[list[str]]
    evidence_set_sha256: str
    code_root: Path
    execution_files: tuple[str, ...]
    run_directory_identities: dict[Path, tuple[int, int]]

    def revalidate(self) -> None:
        self.tracker.revalidate()
        _assert_run_directories(self.run_directory_identities)
        _assert_private_approval_directory(self.run_root, self.approval_directory)
        current_files = _enumerate_regular_files(self.code_root)
        if tuple(current_files) != self.execution_files:
            raise ApprovalError("Frozen execution bundle membership changed.")


@dataclass
class ReviewPreparation:
    tracker: SnapshotTracker
    run_root: Path
    approval_directory: Path
    proposal_snapshot: FileSnapshot
    body_snapshot: FileSnapshot
    proposal: dict[str, Any]
    body: dict[str, Any]
    run_directory_identities: dict[Path, tuple[int, int]]

    def revalidate(self) -> None:
        self.tracker.revalidate()
        _assert_run_directories(self.run_directory_identities)
        _assert_private_approval_directory(self.run_root, self.approval_directory)


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Record, approve, or independently verify a reviewed production-copy "
            "policy using only stable local evidence."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    record_review = subparsers.add_parser(
        "record-review", help="create a canonical lossless review record"
    )
    record_review.add_argument("--proposal", required=True)
    record_review.add_argument("--proposal-run-root", required=True)
    record_review.add_argument("--expected-proposal-sha256", required=True)
    record_review.add_argument("--review-id", required=True)
    record_review.add_argument("--reviewer", required=True)
    record_review.add_argument("--notes", required=True)
    record_review.add_argument("--output", required=True)
    record_review.add_argument("--confirm-lossless-reviewed", action="store_true")
    record_review.add_argument(
        "--confirm-reviewer-operator-asserted",
        action="store_true",
        help=(
            "assert that --reviewer is the operator identity recorded in the review; "
            "this is not cryptographic identity verification"
        ),
    )

    approve = subparsers.add_parser("approve", help="create a reviewed policy")
    approve.add_argument("--proposal", required=True)
    approve.add_argument("--proposal-run-root", required=True)
    approve.add_argument("--expected-proposal-sha256", required=True)
    approve.add_argument("--review", "--review-record", dest="review", required=True)
    approve.add_argument("--expected-review-sha256", required=True)
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--output", required=True)
    approve.add_argument("--confirm-lossless-reviewed", action="store_true")
    approve.add_argument(
        "--confirm-reviewer-operator-asserted",
        action="store_true",
        help=(
            "assert that --reviewer is the operator identity recorded in the review; "
            "this is not cryptographic identity verification"
        ),
    )

    verify = subparsers.add_parser("verify", help="reverify an exact frozen binding")
    verify.add_argument("--policy", required=True)
    verify.add_argument("--proposal", required=True)
    verify.add_argument("--review", "--review-record", dest="review", required=True)
    verify.add_argument("--proposal-run-root", required=True)
    verify.add_argument("--expected-policy-sha256", required=True)
    verify.add_argument("--expected-proposal-sha256", required=True)
    verify.add_argument("--expected-review-sha256", required=True)
    return parser.parse_args()


def _assert_isolated_stdlib_runtime() -> None:
    flags = {
        "isolated": bool(sys.flags.isolated),
        "ignore_environment": bool(sys.flags.ignore_environment),
        "no_site": bool(sys.flags.no_site),
        "no_user_site": bool(sys.flags.no_user_site),
        "dont_write_bytecode": bool(sys.flags.dont_write_bytecode),
        "utf8_mode": bool(sys.flags.utf8_mode),
    }
    if not all(flags.values()):
        raise ApprovalError(
            "Approval verification must run with: python -I -S -B -X utf8."
        )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"JSON constant is not allowed: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _canonical_json_sha256(value: Any) -> str:
    return sha256(_canonical_json_bytes(value)).hexdigest()


def _load_json(
    snapshot: FileSnapshot,
    *,
    label: str,
    canonical: bool,
) -> Any:
    try:
        value = json.loads(
            snapshot.raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ApprovalError(f"{label} must be strict UTF-8 JSON.") from exc
    if canonical and _canonical_json_bytes(value) != snapshot.raw:
        raise ApprovalError(f"{label} must use canonical JSON encoding.")
    return value


def _is_reparse_point(path: Path) -> bool:
    metadata = os.lstat(path)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT_ATTRIBUTE
    )


def _assert_no_reparse_components(path: Path, *, include_leaf: bool) -> None:
    candidate = path if include_leaf else path.parent
    current = Path(candidate.anchor)
    parts = candidate.parts[1:] if candidate.anchor else candidate.parts
    for part in parts:
        current /= part
        try:
            if _is_reparse_point(current):
                raise ApprovalError(
                    "Approval paths must not traverse symlinks or reparse points."
                )
        except OSError as exc:
            raise ApprovalError(f"Approval path cannot be inspected: {current}") from exc


def _absolute_local_path(raw: str | Path, *, label: str) -> Path:
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts or path == Path(path.anchor):
        raise ApprovalError(f"{label} must be a safe absolute path.")
    if os.name == "nt":
        if path.drive.startswith("\\") or not re.fullmatch(r"[A-Za-z]:", path.drive):
            raise ApprovalError(f"{label} must not use a UNC or device path.")
        _drive, tail = os.path.splitdrive(os.fspath(path))
        if ":" in tail:
            raise ApprovalError(f"{label} must not use a Windows alternate data stream.")
        try:
            import ctypes

            get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
            get_drive_type.argtypes = [ctypes.c_wchar_p]
            get_drive_type.restype = ctypes.c_uint
            drive_type = get_drive_type(path.anchor)
        except (AttributeError, OSError) as exc:
            raise ApprovalError(f"{label} drive type cannot be inspected.") from exc
        if drive_type != 3:
            raise ApprovalError(f"{label} must be on a fixed local drive.")
    return path


def _canonical_key(path: Path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _is_within(path: Path, directory: Path) -> bool:
    try:
        return os.path.commonpath(
            (_canonical_key(path), _canonical_key(directory))
        ) == _canonical_key(directory)
    except ValueError:
        return False


def _existing_directory(raw: str | Path, *, label: str) -> Path:
    path = _absolute_local_path(raw, label=label)
    _assert_no_reparse_components(path, include_leaf=True)
    try:
        resolved = path.resolve(strict=True)
        metadata = os.lstat(resolved)
    except OSError as exc:
        raise ApprovalError(f"{label} does not exist or cannot be inspected.") from exc
    if not stat.S_ISDIR(metadata.st_mode) or _is_reparse_point(resolved):
        raise ApprovalError(f"{label} must be a real directory.")
    return resolved


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _stable_snapshot(path: Path, *, maximum_size: int, label: str) -> FileSnapshot:
    path = _absolute_local_path(path, label=label)
    _assert_no_reparse_components(path, include_leaf=True)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        path_before = os.lstat(path)
        if (
            not stat.S_ISREG(path_before.st_mode)
            or path_before.st_nlink != 1
            or _is_reparse_point(path)
        ):
            raise ApprovalError(f"{label} must be a single-link regular file.")
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(1024 * 1024, maximum_size + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > maximum_size:
                    raise ApprovalError(f"{label} exceeds its size limit.")
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        path_after = os.lstat(path)
    except ApprovalError:
        raise
    except OSError as exc:
        raise ApprovalError(f"{label} cannot be read safely.") from exc
    identities = {
        _file_identity(path_before),
        _file_identity(before),
        _file_identity(after),
        _file_identity(path_after),
    }
    if len(identities) != 1 or after.st_nlink != 1:
        raise ApprovalError(f"{label} changed while it was read.")
    raw = b"".join(chunks)
    if len(raw) != after.st_size:
        raise ApprovalError(f"{label} size changed while it was read.")
    return FileSnapshot(
        path=path,
        raw=raw,
        size=len(raw),
        sha256=sha256(raw).hexdigest(),
        identity=_file_identity(after),
    )


def _validate_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise ApprovalError(f"{label} must be a lowercase SHA-256 digest.")
    return value


def _validate_identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ApprovalError(f"{label} is invalid.")
    return value


def _validate_timestamp(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or UTC_TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ApprovalError(f"{label} must be a UTC timestamp ending in Z.")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ApprovalError(f"{label} is not a real timestamp.") from exc
    if parsed.tzinfo != UTC:
        raise ApprovalError(f"{label} must be UTC.")
    return parsed


def _validate_review_notes(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_REVIEW_NOTES_CHARS:
        raise ApprovalError("Review record notes must be a bounded string.")
    return value


def _validate_nodes(
    value: Any,
    *,
    label: str,
    allow_empty: bool,
) -> list[list[str]]:
    if not isinstance(value, list):
        raise ApprovalError(f"{label} must be a list.")
    nodes: list[list[str]] = []
    for node in value:
        if (
            not isinstance(node, list)
            or len(node) != 2
            or not all(isinstance(part, str) for part in node)
            or not all(MIGRATION_PART_PATTERN.fullmatch(part) for part in node)
        ):
            raise ApprovalError(f"{label} contains an invalid migration node.")
        nodes.append([node[0], node[1]])
    canonical = [list(node) for node in sorted({tuple(node) for node in nodes})]
    if nodes != canonical or (not nodes and not allow_empty):
        raise ApprovalError(f"{label} must be canonical.")
    return nodes


def _validate_artifact_reference(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"path", "sha256", "size"}:
        raise ApprovalError(f"{label} artifact reference has an invalid shape.")
    relative = value["path"]
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ApprovalError(f"{label} artifact path is invalid.")
    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or relative in {".", ""}
        or ".." in pure.parts
        or pure.as_posix() != relative
        or any(":" in part or part in {"", "."} for part in pure.parts)
    ):
        raise ApprovalError(f"{label} artifact path must be safe and canonical.")
    size = value["size"]
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ApprovalError(f"{label} artifact size is invalid.")
    _validate_sha256(value["sha256"], label=f"{label} artifact sha256")
    return {"path": relative, "size": size, "sha256": value["sha256"]}


def _artifact_snapshot(
    tracker: SnapshotTracker,
    run_root: Path,
    reference: Any,
    *,
    label: str,
    maximum_size: int = MAX_EVIDENCE_BYTES,
    expected_path: str | None = None,
) -> FileSnapshot:
    artifact = _validate_artifact_reference(reference, label=label)
    if expected_path is not None and artifact["path"] != expected_path:
        raise ApprovalError(f"{label} artifact path is not the required path.")
    candidate = run_root.joinpath(*PurePosixPath(artifact["path"]).parts)
    if not _is_within(candidate, run_root):
        raise ApprovalError(f"{label} artifact escaped Proposal RunRoot.")
    snapshot = tracker.read(candidate, maximum_size=maximum_size, label=label)
    if snapshot.size != artifact["size"] or snapshot.sha256 != artifact["sha256"]:
        raise ApprovalError(f"{label} artifact bytes do not match their reference.")
    return snapshot


def _validate_projection(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != PROJECTION_KEYS:
        raise ApprovalError("Policy projection has an invalid exact shape.")
    if value["format"] != POLICY_FORMAT or value["format_version"] != POLICY_VERSION:
        raise ApprovalError("Policy projection targets an unsupported format.")
    _validate_identifier(value["policy_id"], label="policy_id")
    _validate_identifier(
        value["source_media_snapshot_id"], label="source_media_snapshot_id"
    )
    for name in (
        "source_database_sha256",
        "source_media_manifest_sha256",
        "source_applied_migrations_sha256",
        "migration_runtime_sha256",
        "runtime_fingerprint_sha256",
        "execution_bundle_sha256",
        "migration_plan_sha256",
    ):
        _validate_sha256(value[name], label=name)
    value["source_leaf_nodes"] = _validate_nodes(
        value["source_leaf_nodes"], label="source_leaf_nodes", allow_empty=False
    )
    value["target_leaf_nodes"] = _validate_nodes(
        value["target_leaf_nodes"], label="target_leaf_nodes", allow_empty=False
    )
    return value


def _validate_proposal(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    expected = {
        "format",
        "format_version",
        "generated_at",
        "proposal_id",
        "run_id",
        "bootstrap_nonce",
        "state",
        "body",
        "body_artifact",
        "body_sha256",
        "ledger",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ApprovalError("Policy proposal has an invalid exact shape.")
    if value["format"] != PROPOSAL_FORMAT or value["format_version"] != PROPOSAL_VERSION:
        raise ApprovalError("Policy proposal format is unsupported.")
    if value["state"] != "review_required":
        raise ApprovalError("Policy proposal is not awaiting review.")
    _validate_timestamp(value["generated_at"], label="proposal generated_at")
    _validate_identifier(value["proposal_id"], label="proposal_id")
    _validate_identifier(value["run_id"], label="proposal run_id")
    _validate_sha256(value["bootstrap_nonce"], label="proposal bootstrap_nonce")
    _validate_sha256(value["body_sha256"], label="proposal body_sha256")
    _validate_artifact_reference(value["body_artifact"], label="proposal body")
    ledger = value["ledger"]
    if not isinstance(ledger, dict) or set(ledger) != {
        "artifact",
        "event_count",
        "head_event_sha256",
        "terminal_status",
    }:
        raise ApprovalError("Proposal ledger summary has an invalid exact shape.")
    _validate_artifact_reference(ledger["artifact"], label="proposal ledger")
    if (
        not isinstance(ledger["event_count"], int)
        or isinstance(ledger["event_count"], bool)
        or ledger["event_count"] <= 0
        or ledger["event_count"] > MAX_LEDGER_EVENTS
    ):
        raise ApprovalError("Proposal ledger event_count is invalid.")
    _validate_sha256(ledger["head_event_sha256"], label="proposal ledger head")
    if ledger["terminal_status"] != "review_required":
        raise ApprovalError("Proposal ledger is not terminal review_required.")

    body = value["body"]
    expected_body = {
        "format",
        "format_version",
        "proposal_id",
        "run_id",
        "bootstrap_nonce",
        "policy_projection",
        "evidence",
        "evidence_set_sha256",
        "review_requirements",
    }
    if not isinstance(body, dict) or set(body) != expected_body:
        raise ApprovalError("Policy proposal body has an invalid exact shape.")
    if (
        body["format"] != PROPOSAL_BODY_FORMAT
        or body["format_version"] != PROPOSAL_BODY_VERSION
        or body["proposal_id"] != value["proposal_id"]
        or body["run_id"] != value["run_id"]
        or body["bootstrap_nonce"] != value["bootstrap_nonce"]
    ):
        raise ApprovalError("Policy proposal body identity is inconsistent.")
    body["policy_projection"] = _validate_projection(body["policy_projection"])
    evidence = body["evidence"]
    if not isinstance(evidence, dict) or set(evidence) != set(REQUIRED_EVIDENCE):
        raise ApprovalError("Policy proposal evidence set has an invalid exact shape.")
    for name in REQUIRED_EVIDENCE:
        _validate_artifact_reference(evidence[name], label=name)
    expected_evidence_digest = _canonical_json_sha256(evidence)
    if body["evidence_set_sha256"] != expected_evidence_digest:
        raise ApprovalError("Policy proposal evidence-set digest is invalid.")
    requirements = body["review_requirements"]
    if not isinstance(requirements, dict) or set(requirements) != {
        "lossless_review_status",
        "pending_migration_nodes",
        "required_evidence",
    }:
        raise ApprovalError("Proposal review requirements have an invalid exact shape.")
    if requirements["lossless_review_status"] != "not_reviewed":
        raise ApprovalError("Proposal must not claim an already completed review.")
    if requirements["required_evidence"] != list(REQUIRED_EVIDENCE):
        raise ApprovalError("Proposal required-evidence order is not canonical.")
    requirements["pending_migration_nodes"] = _validate_nodes(
        requirements["pending_migration_nodes"],
        label="pending_migration_nodes",
        allow_empty=True,
    )
    return value, body


def _validate_review(
    value: Any,
    *,
    reviewer: str | None,
    proposal_sha256: str,
    proposal_body_sha256: str,
    evidence_set_sha256: str,
    pending_nodes: list[list[str]],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != REVIEW_KEYS:
        raise ApprovalError("Lossless review record has an invalid exact shape.")
    if value["format"] != REVIEW_FORMAT or value["format_version"] != REVIEW_VERSION:
        raise ApprovalError("Lossless review record format is unsupported.")
    _validate_identifier(value["review_id"], label="review_id")
    _validate_timestamp(value["reviewed_at"], label="reviewed_at")
    _validate_identifier(value["reviewer"], label="reviewer")
    if reviewer is not None and value["reviewer"] != reviewer:
        raise ApprovalError("Review record reviewer does not match --reviewer.")
    if value["reviewer_identity_verification"] != REVIEWER_IDENTITY_VERIFICATION:
        raise ApprovalError("Reviewer identity verification semantics are invalid.")
    if (
        value["proposal_sha256"] != proposal_sha256
        or value["proposal_body_sha256"] != proposal_body_sha256
        or value["evidence_set_sha256"] != evidence_set_sha256
    ):
        raise ApprovalError("Review record does not bind the exact proposal evidence.")
    if value["conclusion"] != "lossless":
        raise ApprovalError("Review record does not conclude that migration is lossless.")
    reviewed = _validate_nodes(
        value["migrations_reviewed"],
        label="migrations_reviewed",
        allow_empty=True,
    )
    if reviewed != pending_nodes:
        raise ApprovalError("Review record does not cover the exact pending migration set.")
    _validate_review_notes(value["notes"])
    return value


def _replay_ledger(snapshot: FileSnapshot) -> LedgerReplay:
    raw = snapshot.raw
    if not raw or not raw.endswith(b"\n"):
        raise ApprovalError("Proposal evidence ledger is incomplete.")
    raw_lines = raw.splitlines(keepends=True)
    if len(raw_lines) > MAX_LEDGER_EVENTS:
        raise ApprovalError("Proposal evidence ledger has too many events.")
    previous = ZERO_SHA256
    events: list[dict[str, Any]] = []
    keys = {
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
    last_timestamp: datetime | None = None
    for sequence, raw_line in enumerate(raw_lines, start=1):
        line_snapshot = FileSnapshot(
            path=snapshot.path,
            raw=raw_line,
            size=len(raw_line),
            sha256=sha256(raw_line).hexdigest(),
            identity=snapshot.identity,
        )
        event = _load_json(
            line_snapshot,
            label=f"ledger event {sequence}",
            canonical=True,
        )
        if (
            not isinstance(event, dict)
            or set(event) != keys
            or event["format"] != LEDGER_FORMAT
            or event["format_version"] != LEDGER_VERSION
            or not isinstance(event["sequence"], int)
            or isinstance(event["sequence"], bool)
            or event["sequence"] != sequence
            or event["previous_event_sha256"] != previous
            or not isinstance(event["stage"], str)
            or not event["stage"]
            or not isinstance(event["outcome"], str)
            or not event["outcome"]
            or not isinstance(event["details"], dict)
        ):
            raise ApprovalError("Proposal evidence ledger replay is invalid.")
        generated_at = _validate_timestamp(
            event["generated_at"], label=f"ledger event {sequence} generated_at"
        )
        if last_timestamp is not None and generated_at < last_timestamp:
            raise ApprovalError("Proposal ledger timestamps are not monotonic.")
        last_timestamp = generated_at
        declared = _validate_sha256(
            event["event_sha256"], label=f"ledger event {sequence} sha256"
        )
        projection = dict(event)
        del projection["event_sha256"]
        if _canonical_json_sha256(projection) != declared:
            raise ApprovalError("Proposal evidence ledger hash chain is invalid.")
        previous = declared
        events.append(event)
    if events[-1]["stage"] != "review_required" or events[-1]["outcome"] != "terminal":
        raise ApprovalError("Proposal ledger terminal event is invalid.")
    return LedgerReplay(tuple(events), len(events), previous)


def _find_single_event(ledger: LedgerReplay, stage: str) -> tuple[int, dict[str, Any]]:
    matches = [
        (index, event)
        for index, event in enumerate(ledger.events)
        if event["stage"] == stage
    ]
    if len(matches) != 1:
        raise ApprovalError(f"Proposal ledger must contain exactly one {stage} event.")
    return matches[0]


def _validate_ledger_bindings(
    ledger: LedgerReplay,
    *,
    proposal: dict[str, Any],
    body: dict[str, Any],
    body_reference: dict[str, Any],
) -> None:
    body_index, body_event = _find_single_event(ledger, "policy_proposal_body_created")
    body_details = body_event["details"]
    if (
        body_event["outcome"] != "passed"
        or set(body_details)
        != {
            "proposal_id",
            "run_id",
            "body",
            "evidence_set_sha256",
            "migration_applied",
            "review_required",
        }
        or body_details["proposal_id"] != proposal["proposal_id"]
        or body_details["run_id"] != proposal["run_id"]
        or body_details["body"] != body_reference
        or body_details["evidence_set_sha256"] != body["evidence_set_sha256"]
        or body_details["migration_applied"] is not False
        or body_details["review_required"] is not True
    ):
        raise ApprovalError("Proposal-body ledger event does not bind the exact body.")

    source_index, source_event = _find_single_event(ledger, "source_final_verified")
    bundle_index, bundle_event = _find_single_event(
        ledger, "execution_bundle_final_verified"
    )
    terminal_index = len(ledger.events) - 1
    if not (body_index < source_index < bundle_index < terminal_index):
        raise ApprovalError(
            "Source and execution-bundle final checks must follow the proposal body."
        )
    source_details = source_event["details"]
    source_backup_set = (
        source_details.get("backup_set") if isinstance(source_details, dict) else None
    )
    source_database = (
        source_backup_set.get("database")
        if isinstance(source_backup_set, dict)
        else None
    )
    if (
        source_event["outcome"] != "passed"
        or source_details.get("source_unchanged") is not True
        or not isinstance(source_database, dict)
        or source_database.get("sha256")
        != body["policy_projection"]["source_database_sha256"]
    ):
        raise ApprovalError("Proposal source-final verification is invalid.")
    bundle_details = bundle_event["details"]
    if (
        bundle_event["outcome"] != "passed"
        or bundle_details.get("bundle_unchanged") is not True
        or bundle_details.get("execution_bundle_sha256")
        != body["policy_projection"]["execution_bundle_sha256"]
    ):
        raise ApprovalError("Proposal execution-bundle final verification is invalid.")
    terminal = ledger.events[-1]["details"]
    if (
        set(terminal)
        != {
            "status",
            "proposal_body_sha256",
            "lossless_reviewed",
            "migration_applied",
            "cutover_authorized",
            "contains_production_user_data",
            "retained_on_success",
            "secure_disposal_required",
            "sensitive_retention_scope",
        }
        or terminal["status"] != "review_required"
        or terminal["proposal_body_sha256"] != proposal["body_sha256"]
        or terminal["lossless_reviewed"] is not False
        or terminal["migration_applied"] is not False
        or terminal["cutover_authorized"] is not False
        or terminal["contains_production_user_data"] is not True
        or terminal["retained_on_success"] is not True
        or terminal["secure_disposal_required"] is not True
        or terminal["sensitive_retention_scope"] != "entire_run_root"
    ):
        raise ApprovalError("Proposal terminal ledger details are invalid.")


def _enumerate_regular_files(root: Path) -> list[str]:
    files: list[str] = []
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError as exc:
            raise ApprovalError("Frozen execution bundle cannot be enumerated.") from exc
        for entry in entries:
            path = Path(entry.path)
            if _is_reparse_point(path):
                raise ApprovalError("Frozen execution bundle contains a link.")
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)
            elif entry.is_file(follow_symlinks=False):
                metadata = os.lstat(path)
                if metadata.st_nlink != 1:
                    raise ApprovalError("Frozen execution bundle contains a hard-linked file.")
                files.append(path.relative_to(root).as_posix())
            else:
                raise ApprovalError("Frozen execution bundle contains a special file.")
    return sorted(files)


def _validate_execution_inventory(
    value: Any,
    *,
    snapshot: FileSnapshot,
    tracker: SnapshotTracker,
    run_root: Path,
    projection: dict[str, Any],
    tool_snapshot: FileSnapshot,
) -> tuple[Path, tuple[str, ...]]:
    if not isinstance(value, dict) or set(value) != {
        "format",
        "format_version",
        "execution_bundle_sha256",
        "files",
    }:
        raise ApprovalError("Execution inventory has an invalid exact shape.")
    if (
        value["format"] != EXECUTION_INVENTORY_FORMAT
        or value["format_version"] != EXECUTION_INVENTORY_VERSION
        or value["execution_bundle_sha256"] != projection["execution_bundle_sha256"]
        or not isinstance(value["files"], list)
    ):
        raise ApprovalError("Execution inventory identity is invalid.")
    code_root = run_root / "code"
    files: list[dict[str, Any]] = []
    paths: list[str] = []
    for index, reference in enumerate(value["files"]):
        artifact = _validate_artifact_reference(
            reference, label=f"execution inventory file {index}"
        )
        candidate = code_root.joinpath(*PurePosixPath(artifact["path"]).parts)
        if not _is_within(candidate, code_root):
            raise ApprovalError("Execution inventory file escaped the frozen code root.")
        file_snapshot = tracker.read(
            candidate,
            maximum_size=MAX_EVIDENCE_BYTES,
            label=f"execution file {artifact['path']}",
        )
        if (
            file_snapshot.size != artifact["size"]
            or file_snapshot.sha256 != artifact["sha256"]
        ):
            raise ApprovalError("Execution inventory file bytes do not match.")
        files.append(artifact)
        paths.append(artifact["path"])
    if paths != sorted(set(paths)):
        raise ApprovalError("Execution inventory paths are not canonical and unique.")
    if not MANDATORY_EXECUTION_FILES.issubset(paths):
        raise ApprovalError("Execution inventory is missing a mandatory migration tool.")
    if not any(path.startswith("ffxivshare/") for path in paths) or not any(
        path.startswith("shares/") for path in paths
    ):
        raise ApprovalError("Execution inventory is missing project Python packages.")
    if _canonical_json_sha256(files) != value["execution_bundle_sha256"]:
        raise ApprovalError("Execution inventory digest is invalid.")
    if _enumerate_regular_files(code_root) != paths:
        raise ApprovalError("Frozen execution bundle exact closure is invalid.")
    tool_reference = files[paths.index("ops/migration/Approve-ProductionCopyPolicy.py")]
    if (
        tool_reference["sha256"] != tool_snapshot.sha256
        or tool_reference["size"] != tool_snapshot.size
    ):
        raise ApprovalError("Loaded approval tool does not match the reviewed bundle.")
    del snapshot
    return code_root, tuple(paths)


def _validate_migration_state(value: Any) -> dict[str, Any]:
    keys = {
        "format",
        "format_version",
        "database_vendor",
        "applied",
        "applied_leaf_nodes",
        "repository_leaf_nodes",
        "unknown_applied_nodes",
        "python_version",
        "django_version",
        "migration_runtime_sha256",
    }
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or value["format"] != "ffxivshare-migration-state"
        or value["format_version"] != 1
        or value["database_vendor"] != "sqlite"
        or value["unknown_applied_nodes"] != []
        or not isinstance(value["python_version"], str)
        or not value["python_version"]
        or not isinstance(value["django_version"], str)
        or not value["django_version"]
    ):
        raise ApprovalError("Source migration-state evidence is invalid.")
    _validate_sha256(value["migration_runtime_sha256"], label="migration runtime")
    value["applied"] = _validate_nodes(
        value["applied"], label="source applied migrations", allow_empty=False
    )
    value["applied_leaf_nodes"] = _validate_nodes(
        value["applied_leaf_nodes"], label="source migration leaves", allow_empty=False
    )
    value["repository_leaf_nodes"] = _validate_nodes(
        value["repository_leaf_nodes"], label="repository migration leaves", allow_empty=False
    )
    return value


def _validate_review_plan(value: Any) -> tuple[dict[str, Any], list[list[str]]]:
    keys = {
        "database_vendor",
        "format",
        "format_version",
        "pending_migrations",
        "source_applied_migrations",
        "target_leaf_nodes",
    }
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or value["format"] != REVIEW_PLAN_FORMAT
        or value["format_version"] != REVIEW_PLAN_VERSION
        or value["database_vendor"] != "sqlite"
        or not isinstance(value["pending_migrations"], list)
    ):
        raise ApprovalError("Structured migration review plan is invalid.")
    value["source_applied_migrations"] = _validate_nodes(
        value["source_applied_migrations"],
        label="review-plan source applied migrations",
        allow_empty=False,
    )
    value["target_leaf_nodes"] = _validate_nodes(
        value["target_leaf_nodes"],
        label="review-plan target leaves",
        allow_empty=False,
    )
    pending_nodes: list[list[str]] = []
    for item in value["pending_migrations"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"dependencies", "module", "node", "operations", "replaces"}
            or not isinstance(item["module"], str)
            or not item["module"]
            or not isinstance(item["operations"], list)
            or not all(isinstance(operation, str) and operation for operation in item["operations"])
        ):
            raise ApprovalError("Structured pending-migration entry is invalid.")
        pending_nodes.extend(
            _validate_nodes([item["node"]], label="pending migration", allow_empty=False)
        )
        _validate_nodes(
            item["dependencies"], label="pending dependencies", allow_empty=True
        )
        _validate_nodes(item["replaces"], label="pending replacements", allow_empty=True)
    canonical = [list(node) for node in sorted({tuple(node) for node in pending_nodes})]
    if pending_nodes != canonical:
        raise ApprovalError("Structured pending-migration order is not canonical.")
    return value, pending_nodes


def _validate_evidence(
    *,
    tracker: SnapshotTracker,
    run_root: Path,
    body: dict[str, Any],
    tool_snapshot: FileSnapshot,
) -> tuple[FileSnapshot, Path, tuple[str, ...], list[list[str]]]:
    evidence = body["evidence"]
    snapshots: dict[str, FileSnapshot] = {}
    for name in REQUIRED_EVIDENCE:
        expected_path = EXPECTED_EVIDENCE_PATHS.get(name)
        if name == "migration_plan":
            path = evidence[name]["path"]
            if not isinstance(path, str) or not path.startswith("logs/") or not path.endswith(
                ".stdout.txt"
            ):
                raise ApprovalError("Migration-plan evidence path is invalid.")
        snapshots[name] = _artifact_snapshot(
            tracker,
            run_root,
            evidence[name],
            label=name,
            expected_path=expected_path,
        )

    projection = body["policy_projection"]
    bootstrap = _load_json(snapshots["bootstrap"], label="bootstrap record", canonical=True)
    manifest = _load_json(
        snapshots["execution_inventory"],
        label="execution inventory",
        canonical=True,
    )
    code_root, execution_files = _validate_execution_inventory(
        manifest,
        snapshot=snapshots["execution_inventory"],
        tracker=tracker,
        run_root=run_root,
        projection=projection,
        tool_snapshot=tool_snapshot,
    )
    if (
        not isinstance(bootstrap, dict)
        or set(bootstrap)
        != {
            "format",
            "format_version",
            "generated_at",
            "run_id",
            "bootstrap_nonce",
            "workspace_access_control",
            "python",
            "configuration",
            "run_layout",
            "policy",
            "approval_inputs",
            "execution_bundle",
            "bootstrap_trusted_not_frozen",
            "source_data_read_by_bootstrap",
            "media_read_by_bootstrap",
        }
        or bootstrap["format"] != BOOTSTRAP_FORMAT
        or bootstrap["format_version"] != BOOTSTRAP_VERSION
        or bootstrap["run_id"] != body["run_id"]
        or bootstrap["bootstrap_nonce"] != body["bootstrap_nonce"]
        or bootstrap["policy"] is not None
        or bootstrap["approval_inputs"] is not None
        or bootstrap["bootstrap_trusted_not_frozen"] is not True
        or bootstrap["source_data_read_by_bootstrap"] is not False
        or bootstrap["media_read_by_bootstrap"] is not False
        or not isinstance(bootstrap["python"], dict)
        or not isinstance(bootstrap["run_layout"], list)
        or not isinstance(bootstrap["workspace_access_control"], str)
        or not bootstrap["workspace_access_control"]
    ):
        raise ApprovalError("Proposal bootstrap evidence is invalid.")
    _validate_timestamp(bootstrap["generated_at"], label="bootstrap generated_at")
    configuration = bootstrap["configuration"]
    bundle = bootstrap["execution_bundle"]
    if (
        not isinstance(configuration, dict)
        or set(configuration)
        != {"inner_arguments", "inner_entrypoint", "mode", "repository_root", "run_root"}
        or not isinstance(configuration["inner_arguments"], list)
        or not all(isinstance(item, str) for item in configuration["inner_arguments"])
        or not isinstance(configuration["repository_root"], str)
        or not isinstance(configuration["run_root"], str)
        or configuration["mode"] != "policy-proposal"
        or configuration["inner_entrypoint"]
        != "ops/migration/Propose-ProductionCopyPolicy.py"
        or _canonical_key(Path(configuration["run_root"])) != _canonical_key(run_root)
        or not isinstance(bundle, dict)
        or set(bundle) != {"authority", "expected_sha256", "frozen_sha256", "manifest"}
        or bundle["authority"] != "stable_repository_consistency"
        or bundle["expected_sha256"] != projection["execution_bundle_sha256"]
        or bundle["frozen_sha256"] != projection["execution_bundle_sha256"]
        or bundle["manifest"] != evidence["execution_inventory"]
    ):
        raise ApprovalError("Proposal bootstrap authority is invalid.")

    runtime = _load_json(
        snapshots["runtime_fingerprint"], label="runtime fingerprint", canonical=False
    )
    if (
        not isinstance(runtime, dict)
        or set(runtime)
        != {
            "format",
            "format_version",
            "fingerprint_sha256",
            "projection",
            "checkpoint",
        }
        or runtime["format"] != "ffxivshare-runtime-fingerprint"
        or runtime["format_version"] != 1
        or not isinstance(runtime["projection"], dict)
        or _canonical_json_sha256(runtime["projection"]) != runtime["fingerprint_sha256"]
        or runtime["fingerprint_sha256"] != projection["runtime_fingerprint_sha256"]
    ):
        raise ApprovalError("Runtime-fingerprint evidence is inconsistent.")
    checkpoint = runtime["checkpoint"]
    if (
        not isinstance(checkpoint, dict)
        or set(checkpoint)
        != {
            "closure_scopes",
            "content_hashed",
            "format",
            "format_version",
            "identity_inventory",
        }
        or checkpoint["format"]
        != "ffxivshare-runtime-identity-checkpoint-source"
        or checkpoint["format_version"] != 1
        or checkpoint["content_hashed"] is not True
        or not isinstance(checkpoint["identity_inventory"], list)
        or not checkpoint["identity_inventory"]
        or not isinstance(checkpoint["closure_scopes"], list)
        or not checkpoint["closure_scopes"]
    ):
        raise ApprovalError("Runtime-fingerprint identity checkpoint is invalid.")
    inventory_paths: list[str] = []
    for item in checkpoint["identity_inventory"]:
        if (
            not isinstance(item, dict)
            or set(item)
            != {"ctime_ns", "device", "inode", "mtime_ns", "path", "size"}
            or not isinstance(item["path"], str)
            or not item["path"]
            or not all(
                isinstance(item[name], int) and not isinstance(item[name], bool)
                for name in ("ctime_ns", "device", "inode", "mtime_ns", "size")
            )
            or item["size"] < 0
        ):
            raise ApprovalError("Runtime identity inventory is invalid.")
        inventory_paths.append(item["path"])
    if inventory_paths != sorted(set(inventory_paths)):
        raise ApprovalError("Runtime identity inventory is not canonical.")
    scope_keys: list[tuple[str, str]] = []
    for scope in checkpoint["closure_scopes"]:
        if (
            not isinstance(scope, dict)
            or set(scope) != {"files", "mode", "root"}
            or scope["mode"]
            not in {
                "recursive",
                "recursive_import_entries",
                "recursive_package_entries",
                "top_level_entries",
            }
            or not isinstance(scope["root"], str)
            or not scope["root"]
            or not isinstance(scope["files"], list)
            or not all(isinstance(item, str) for item in scope["files"])
            or scope["files"] != sorted(set(scope["files"]))
        ):
            raise ApprovalError("Runtime identity closure scope is invalid.")
        scope_keys.append((scope["root"], scope["mode"]))
    if scope_keys != sorted(set(scope_keys)):
        raise ApprovalError("Runtime identity closure scopes are not canonical.")

    state = _validate_migration_state(
        _load_json(
            snapshots["source_migration_state"],
            label="source migration state",
            canonical=False,
        )
    )
    review_plan, pending_nodes = _validate_review_plan(
        _load_json(
            snapshots["migration_review_plan"],
            label="migration review plan",
            canonical=False,
        )
    )
    if (
        review_plan["source_applied_migrations"] != state["applied"]
        or review_plan["target_leaf_nodes"] != state["repository_leaf_nodes"]
        or body["review_requirements"]["pending_migration_nodes"] != pending_nodes
        or projection["source_applied_migrations_sha256"]
        != _canonical_json_sha256(state["applied"])
        or projection["migration_runtime_sha256"] != state["migration_runtime_sha256"]
        or projection["source_leaf_nodes"] != state["applied_leaf_nodes"]
        or projection["target_leaf_nodes"] != state["repository_leaf_nodes"]
        or projection["migration_plan_sha256"] != snapshots["migration_plan"].sha256
    ):
        raise ApprovalError("Migration state, plan, pending nodes, and projection disagree.")
    if (
        state["applied_leaf_nodes"] != state["repository_leaf_nodes"]
        and not pending_nodes
    ):
        raise ApprovalError("Different migration leaves require a non-empty pending plan.")

    backup = _load_json(
        snapshots["source_backup_verification"],
        label="source backup verification",
        canonical=False,
    )
    backup_artifact = backup.get("artifact") if isinstance(backup, dict) else None
    if (
        not isinstance(backup, dict)
        or backup.get("format") != "ffxivshare-sqlite-backup-set-verification"
        or backup.get("format_version") != 1
        or backup.get("verified") is not True
        or backup.get("cutover_authorized") is not False
        or backup.get("inspection_required") is not True
        or not isinstance(backup_artifact, dict)
        or backup_artifact.get("sha256") != projection["source_database_sha256"]
    ):
        raise ApprovalError("Source backup verification evidence is inconsistent.")

    inspection = _load_json(
        snapshots["source_snapshot_inspection"],
        label="source snapshot inspection",
        canonical=False,
    )
    database = inspection.get("database") if isinstance(inspection, dict) else None
    checks = inspection.get("inspection") if isinstance(inspection, dict) else None
    migrations = checks.get("django_migrations") if isinstance(checks, dict) else None
    foreign_keys = checks.get("foreign_key_check") if isinstance(checks, dict) else None
    if (
        not isinstance(inspection, dict)
        or inspection.get("format") != "ffxivshare-sqlite-snapshot-inspection"
        or inspection.get("format_version") != 1
        or not isinstance(database, dict)
        or database.get("sha256") != projection["source_database_sha256"]
        or database.get("sha256_before") != projection["source_database_sha256"]
        or database.get("sha256_after") != projection["source_database_sha256"]
        or database.get("source_unchanged") is not True
        or not isinstance(checks, dict)
        or checks.get("query_only") is not True
        or checks.get("integrity_check") != "ok"
        or not isinstance(foreign_keys, dict)
        or foreign_keys.get("status") != "ok"
        or foreign_keys.get("violations") != 0
        or not isinstance(migrations, dict)
        or migrations.get("present") is not True
        or not isinstance(migrations.get("applied"), list)
    ):
        raise ApprovalError("Source snapshot inspection evidence is inconsistent.")
    inspected_nodes = []
    for item in migrations["applied"]:
        if not isinstance(item, dict) or not {"app", "name"}.issubset(item):
            raise ApprovalError("Inspected migration projection is invalid.")
        inspected_nodes.append([item["app"], item["name"]])
    if _validate_nodes(
        inspected_nodes, label="inspected migrations", allow_empty=False
    ) != state["applied"]:
        raise ApprovalError("Inspection and migration-state applied nodes disagree.")

    media = _load_json(
        snapshots["source_media_manifest"],
        label="source media manifest",
        canonical=False,
    )
    source_snapshot = media.get("source_snapshot") if isinstance(media, dict) else None
    if (
        snapshots["source_media_manifest"].sha256
        != projection["source_media_manifest_sha256"]
        or not isinstance(media, dict)
        or media.get("format") != "ffxivshare-media-manifest"
        or media.get("format_version") != 2
        or media.get("hash_algorithm") != "sha256"
        or media.get("path_normalization")
        != "unicode_nfc_canonical_caseless_unique"
        or not isinstance(source_snapshot, dict)
        or set(source_snapshot) != {"id", "offline_confirmed"}
        or source_snapshot.get("offline_confirmed") is not True
        or source_snapshot.get("id") != projection["source_media_snapshot_id"]
    ):
        raise ApprovalError("Source media-manifest evidence is inconsistent.")
    return snapshots["bootstrap"], code_root, execution_files, pending_nodes


def _validate_completion(
    value: Any,
    *,
    snapshot: FileSnapshot,
    tracker: SnapshotTracker,
    run_root: Path,
    body: dict[str, Any],
) -> None:
    if (
        not isinstance(value, dict)
        or set(value)
        != {
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
        or value["format"] != BOOTSTRAP_COMPLETION_FORMAT
        or value["format_version"] != BOOTSTRAP_COMPLETION_VERSION
        or value["run_id"] != body["run_id"]
        or not isinstance(value["inner_exit_code"], int)
        or isinstance(value["inner_exit_code"], bool)
        or value["inner_exit_code"] != 0
        or value["execution_bundle_sha256"]
        != body["policy_projection"]["execution_bundle_sha256"]
        or value["execution_bundle_unchanged"] is not True
        or value["bootstrap_record_unchanged"] is not True
        or value["bundle_manifest_unchanged"] is not True
        or value["frozen_policy_unchanged"] is not True
        or value["frozen_proposal_unchanged"] is not True
        or value["frozen_review_unchanged"] is not True
    ):
        raise ApprovalError("Proposal bootstrap completion is invalid.")
    _validate_timestamp(value["generated_at"], label="bootstrap completion generated_at")
    _artifact_snapshot(tracker, run_root, value["stdout"], label="bootstrap stdout")
    _artifact_snapshot(tracker, run_root, value["stderr"], label="bootstrap stderr")
    del snapshot


def _run_directory_identities(run_root: Path) -> dict[Path, tuple[int, int]]:
    paths = (
        run_root,
        run_root / "approval",
        run_root / "artifacts",
        run_root / "code",
        run_root / "evidence",
        run_root / "logs",
    )
    identities: dict[Path, tuple[int, int]] = {}
    for path in paths:
        directory = _existing_directory(path, label=f"Proposal RunRoot directory {path.name}")
        metadata = os.lstat(directory)
        identities[directory] = (metadata.st_dev, metadata.st_ino)
    return identities


def _assert_run_directories(identities: dict[Path, tuple[int, int]]) -> None:
    for path, identity in identities.items():
        try:
            metadata = os.lstat(path)
        except OSError as exc:
            raise ApprovalError("Proposal RunRoot directory disappeared.") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or _is_reparse_point(path)
            or (metadata.st_dev, metadata.st_ino) != identity
        ):
            raise ApprovalError("Proposal RunRoot directory identity changed.")


def _windows_private_dacl(
    path: Path,
) -> tuple[bool, str, str, dict[str, int]]:
    import ctypes
    from ctypes import wintypes

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TokenUser(ctypes.Structure):
        _fields_ = [("User", SidAndAttributes)]

    class AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    class AceHeader(ctypes.Structure):
        _fields_ = [
            ("AceType", ctypes.c_ubyte),
            ("AceFlags", ctypes.c_ubyte),
            ("AceSize", wintypes.WORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_uint,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.GetFileSecurityW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetFileSecurityW.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorOwner.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorOwner.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_uint,
    ]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetAce.restype = wintypes.BOOL

    def sid_to_string(pointer: int) -> str:
        output = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(
            ctypes.c_void_p(pointer), ctypes.byref(output)
        ):
            raise ApprovalError("Windows SID conversion failed.")
        try:
            return output.value
        finally:
            kernel32.LocalFree(output)

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
    ):
        raise ApprovalError("Current Windows token cannot be inspected.")
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token, 1, buffer, required, ctypes.byref(required)
        ):
            raise ApprovalError("Current Windows user SID cannot be inspected.")
        token_user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
        current_sid = sid_to_string(token_user.User.Sid)
    finally:
        kernel32.CloseHandle(token)

    required = wintypes.DWORD()
    security_information = 0x00000001 | 0x00000004
    advapi32.GetFileSecurityW(
        str(path), security_information, None, 0, ctypes.byref(required)
    )
    descriptor = ctypes.create_string_buffer(required.value)
    if not advapi32.GetFileSecurityW(
        str(path),
        security_information,
        descriptor,
        required,
        ctypes.byref(required),
    ):
        raise ApprovalError("Windows approval-directory DACL cannot be read.")
    control = wintypes.WORD()
    revision = wintypes.DWORD()
    if not advapi32.GetSecurityDescriptorControl(
        descriptor, ctypes.byref(control), ctypes.byref(revision)
    ):
        raise ApprovalError("Windows security descriptor control cannot be read.")
    owner = ctypes.c_void_p()
    owner_defaulted = wintypes.BOOL()
    if (
        not advapi32.GetSecurityDescriptorOwner(
            descriptor, ctypes.byref(owner), ctypes.byref(owner_defaulted)
        )
        or not owner.value
    ):
        raise ApprovalError("Windows approval-directory owner cannot be read.")
    owner_sid = sid_to_string(owner.value)
    present = wintypes.BOOL()
    defaulted = wintypes.BOOL()
    acl = ctypes.c_void_p()
    if (
        not advapi32.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(present),
            ctypes.byref(acl),
            ctypes.byref(defaulted),
        )
        or not present.value
        or not acl.value
    ):
        raise ApprovalError("Windows approval-directory DACL is missing.")
    info = AclSizeInformation()
    if not advapi32.GetAclInformation(
        acl, ctypes.byref(info), ctypes.sizeof(info), 2
    ):
        raise ApprovalError("Windows approval-directory DACL cannot be enumerated.")
    trustees: dict[str, int] = {}
    for index in range(info.AceCount):
        ace = ctypes.c_void_p()
        if not advapi32.GetAce(acl, index, ctypes.byref(ace)):
            raise ApprovalError("Windows approval-directory ACE cannot be read.")
        header = ctypes.cast(ace, ctypes.POINTER(AceHeader)).contents
        mask = ctypes.c_uint32.from_address(ace.value + 4).value
        if header.AceType != 0 or mask != 0x001F01FF:
            raise ApprovalError(
                "Windows approval-directory DACL is not exact private full control."
            )
        sid = sid_to_string(ace.value + 8)
        if sid in trustees:
            raise ApprovalError("Windows approval-directory DACL has duplicate trustees.")
        trustees[sid] = header.AceFlags
    expected = {current_sid, "S-1-5-18", "S-1-5-32-544"}
    if set(trustees) != expected:
        raise ApprovalError("Windows approval-directory trustees are not private.")
    if owner_sid != current_sid:
        raise ApprovalError("Windows approval-directory owner is not the current operator.")
    return bool(control.value & 0x1000), owner_sid, current_sid, trustees


def _assert_private_approval_directory(run_root: Path, approval: Path) -> None:
    if approval != run_root / "approval":
        raise ApprovalError("Approval directory must be Proposal RunRoot/approval.")
    if os.name == "nt":
        root_protected, root_owner, current_sid, root_trustees = (
            _windows_private_dacl(run_root)
        )
        approval_protected, approval_owner, approval_current, approval_trustees = (
            _windows_private_dacl(approval)
        )
        if (
            not root_protected
            or not approval_protected
            or root_owner != current_sid
            or approval_owner != approval_current
            or approval_current != current_sid
            or set(approval_trustees) != set(root_trustees)
            or any(flags not in {0x03, 0x13} for flags in root_trustees.values())
            or any(flags not in {0x03, 0x13} for flags in approval_trustees.values())
            or any((flags & 0x03) != 0x03 for flags in root_trustees.values())
            or any((flags & 0x03) != 0x03 for flags in approval_trustees.values())
        ):
            raise ApprovalError("Proposal RunRoot/approval is not under a protected private DACL.")
    else:
        for path in (run_root, approval):
            if stat.S_IMODE(os.stat(path).st_mode) & 0o077:
                raise ApprovalError("Proposal approval directory is not private.")


def _assert_private_output_file(path: Path, approval: Path) -> None:
    if _canonical_key(path.parent) != _canonical_key(approval):
        raise ApprovalError("Approved policy escaped its private approval directory.")
    if os.name == "nt":
        _protected, owner, current_sid, trustees = _windows_private_dacl(path)
        if (
            owner != current_sid
            or set(trustees) != {current_sid, "S-1-5-18", "S-1-5-32-544"}
            or any(flags not in {0x00, 0x10} for flags in trustees.values())
        ):
            raise ApprovalError("Approved policy file DACL is not exact and private.")
    else:
        metadata = os.stat(path)
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ApprovalError("Approved policy file mode is not private 0600.")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise ApprovalError("Approved policy file owner is not the current operator.")


def _prepare_review_record(
    *,
    proposal_path: Path,
    proposal_run_root: Path,
    expected_proposal_sha256: str,
) -> ReviewPreparation:
    run_root = _existing_directory(proposal_run_root, label="Proposal RunRoot")
    run_identities = _run_directory_identities(run_root)
    approval_directory = run_root / "approval"
    _assert_private_approval_directory(run_root, approval_directory)

    source_proposal_path = run_root / "evidence" / "policy-proposal.json"
    _assert_no_reparse_components(proposal_path, include_leaf=True)
    if _canonical_key(proposal_path) != _canonical_key(source_proposal_path):
        raise ApprovalError(
            "record-review requires Proposal RunRoot/evidence/policy-proposal.json."
        )
    tracker = SnapshotTracker()
    proposal_snapshot = tracker.read(
        source_proposal_path,
        maximum_size=MAX_JSON_BYTES,
        label="Proposal RunRoot policy proposal",
    )
    if proposal_snapshot.sha256 != expected_proposal_sha256:
        raise ApprovalError("Policy proposal SHA-256 does not match the expected value.")
    proposal, body = _validate_proposal(
        _load_json(proposal_snapshot, label="policy proposal", canonical=True)
    )
    body_snapshot = _artifact_snapshot(
        tracker,
        run_root,
        proposal["body_artifact"],
        label="proposal body",
        maximum_size=MAX_JSON_BYTES,
        expected_path="evidence/policy-proposal-body.json",
    )
    if (
        body_snapshot.sha256 != proposal["body_sha256"]
        or _load_json(body_snapshot, label="proposal body", canonical=True) != body
    ):
        raise ApprovalError("Embedded proposal body does not match its body artifact.")

    preparation = ReviewPreparation(
        tracker=tracker,
        run_root=run_root,
        approval_directory=approval_directory,
        proposal_snapshot=proposal_snapshot,
        body_snapshot=body_snapshot,
        proposal=proposal,
        body=body,
        run_directory_identities=run_identities,
    )
    preparation.revalidate()
    return preparation


def _validate_binding(
    *,
    proposal_path: Path,
    review_path: Path,
    proposal_run_root: Path,
    expected_proposal_sha256: str,
    expected_review_sha256: str,
    reviewer: str | None,
    require_source_proposal_path: bool,
) -> BindingValidation:
    run_root = _existing_directory(proposal_run_root, label="Proposal RunRoot")
    run_identities = _run_directory_identities(run_root)
    approval_directory = run_root / "approval"
    _assert_private_approval_directory(run_root, approval_directory)

    tracker = SnapshotTracker()
    source_proposal_path = run_root / "evidence" / "policy-proposal.json"
    source_proposal = tracker.read(
        source_proposal_path,
        maximum_size=MAX_JSON_BYTES,
        label="Proposal RunRoot policy proposal",
    )
    supplied_proposal = tracker.read(
        proposal_path,
        maximum_size=MAX_JSON_BYTES,
        label="Supplied policy proposal",
    )
    if require_source_proposal_path and _canonical_key(supplied_proposal.path) != _canonical_key(
        source_proposal_path
    ):
        raise ApprovalError("approve requires the proposal inside Proposal RunRoot/evidence.")
    if (
        supplied_proposal.raw != source_proposal.raw
        or supplied_proposal.sha256 != source_proposal.sha256
        or supplied_proposal.sha256 != expected_proposal_sha256
    ):
        raise ApprovalError("Supplied proposal does not match the immutable Proposal RunRoot.")
    proposal = _load_json(source_proposal, label="policy proposal", canonical=True)
    proposal, body = _validate_proposal(proposal)

    body_snapshot = _artifact_snapshot(
        tracker,
        run_root,
        proposal["body_artifact"],
        label="proposal body",
        maximum_size=MAX_JSON_BYTES,
        expected_path="evidence/policy-proposal-body.json",
    )
    if (
        body_snapshot.sha256 != proposal["body_sha256"]
        or _load_json(body_snapshot, label="proposal body", canonical=True) != body
    ):
        raise ApprovalError("Embedded proposal body does not match its body artifact.")

    tool_snapshot = tracker.read(
        Path(__file__).resolve(),
        maximum_size=MAX_JSON_BYTES,
        label="Loaded approval tool",
    )
    _bootstrap_snapshot, code_root, execution_files, pending_nodes = _validate_evidence(
        tracker=tracker,
        run_root=run_root,
        body=body,
        tool_snapshot=tool_snapshot,
    )

    ledger_snapshot = _artifact_snapshot(
        tracker,
        run_root,
        proposal["ledger"]["artifact"],
        label="proposal ledger",
        maximum_size=MAX_LEDGER_BYTES,
        expected_path="evidence/events.jsonl",
    )
    ledger = _replay_ledger(ledger_snapshot)
    if (
        ledger.event_count != proposal["ledger"]["event_count"]
        or ledger.head_event_sha256 != proposal["ledger"]["head_event_sha256"]
    ):
        raise ApprovalError("Proposal ledger replay does not match its summary.")
    _validate_ledger_bindings(
        ledger,
        proposal=proposal,
        body=body,
        body_reference=proposal["body_artifact"],
    )

    completion_snapshot = tracker.read(
        run_root / "evidence" / "completion.json",
        maximum_size=MAX_JSON_BYTES,
        label="proposal bootstrap completion",
    )
    completion = _load_json(
        completion_snapshot, label="proposal bootstrap completion", canonical=True
    )
    _validate_completion(
        completion,
        snapshot=completion_snapshot,
        tracker=tracker,
        run_root=run_root,
        body=body,
    )

    review_snapshot = tracker.read(
        review_path,
        maximum_size=MAX_JSON_BYTES,
        label="lossless review record",
    )
    if review_snapshot.sha256 != expected_review_sha256:
        raise ApprovalError("Review record SHA-256 does not match the expected value.")
    review = _validate_review(
        _load_json(review_snapshot, label="lossless review record", canonical=True),
        reviewer=reviewer,
        proposal_sha256=source_proposal.sha256,
        proposal_body_sha256=body_snapshot.sha256,
        evidence_set_sha256=body["evidence_set_sha256"],
        pending_nodes=pending_nodes,
    )
    proposal_time = _validate_timestamp(
        proposal["generated_at"], label="proposal generated_at"
    )
    completion_time = _validate_timestamp(
        completion["generated_at"], label="completion generated_at"
    )
    review_time = _validate_timestamp(review["reviewed_at"], label="reviewed_at")
    terminal_time = _validate_timestamp(
        ledger.events[-1]["generated_at"], label="proposal terminal generated_at"
    )
    if not (terminal_time <= proposal_time <= completion_time <= review_time):
        raise ApprovalError("Proposal, completion, and review timestamps are out of order.")

    binding = BindingValidation(
        tracker=tracker,
        run_root=run_root,
        approval_directory=approval_directory,
        proposal_snapshot=source_proposal,
        review_snapshot=review_snapshot,
        completion_snapshot=completion_snapshot,
        tool_snapshot=tool_snapshot,
        proposal=proposal,
        body=body,
        review=review,
        projection=body["policy_projection"],
        ledger=ledger,
        pending_nodes=pending_nodes,
        evidence_set_sha256=body["evidence_set_sha256"],
        code_root=code_root,
        execution_files=execution_files,
        run_directory_identities=run_identities,
    )
    binding.revalidate()
    return binding


def _policy_for_binding(binding: BindingValidation) -> dict[str, Any]:
    proposal = binding.proposal
    review = binding.review
    return {
        **binding.projection,
        "approved": True,
        "approved_at": datetime.now(UTC).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        ),
        "lossless_reviewed": True,
        "proposal_id": proposal["proposal_id"],
        "proposal_sha256": binding.proposal_snapshot.sha256,
        "proposal_body_sha256": proposal["body_sha256"],
        "proposal_run_id": proposal["run_id"],
        "proposal_bootstrap_nonce": proposal["bootstrap_nonce"],
        "proposal_evidence_set_sha256": binding.evidence_set_sha256,
        "proposal_ledger_head_sha256": binding.ledger.head_event_sha256,
        "proposal_ledger_event_count": binding.ledger.event_count,
        "proposal_bootstrap_completion_sha256": binding.completion_snapshot.sha256,
        "review_id": review["review_id"],
        "reviewed_at": review["reviewed_at"],
        "review_record_sha256": binding.review_snapshot.sha256,
        "reviewer": review["reviewer"],
        "reviewer_identity_verification": REVIEWER_IDENTITY_VERIFICATION,
        "approval_tool_sha256": binding.tool_snapshot.sha256,
    }


def _validate_policy(value: Any, *, binding: BindingValidation) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != POLICY_KEYS:
        raise ApprovalError("Approved policy has an invalid exact 31-field shape.")
    projection = {key: value[key] for key in PROJECTION_KEYS}
    _validate_projection(projection)
    if projection != binding.projection:
        raise ApprovalError("Approved policy projection differs from the proposal.")
    if value["approved"] is not True or value["lossless_reviewed"] is not True:
        raise ApprovalError("Policy is not explicitly approved and lossless-reviewed.")
    approved_at = _validate_timestamp(value["approved_at"], label="approved_at")
    reviewed_at = _validate_timestamp(value["reviewed_at"], label="policy reviewed_at")
    if approved_at < reviewed_at:
        raise ApprovalError("Policy approval predates its review.")
    for name in (
        "proposal_sha256",
        "proposal_body_sha256",
        "proposal_bootstrap_nonce",
        "proposal_evidence_set_sha256",
        "proposal_ledger_head_sha256",
        "proposal_bootstrap_completion_sha256",
        "review_record_sha256",
        "approval_tool_sha256",
    ):
        _validate_sha256(value[name], label=name)
    for name in ("proposal_id", "proposal_run_id", "review_id", "reviewer"):
        _validate_identifier(value[name], label=name)
    if (
        not isinstance(value["proposal_ledger_event_count"], int)
        or isinstance(value["proposal_ledger_event_count"], bool)
        or value["proposal_ledger_event_count"] <= 0
    ):
        raise ApprovalError("Policy proposal ledger event count is invalid.")
    expected_audit = {
        "proposal_id": binding.proposal["proposal_id"],
        "proposal_sha256": binding.proposal_snapshot.sha256,
        "proposal_body_sha256": binding.proposal["body_sha256"],
        "proposal_run_id": binding.proposal["run_id"],
        "proposal_bootstrap_nonce": binding.proposal["bootstrap_nonce"],
        "proposal_evidence_set_sha256": binding.evidence_set_sha256,
        "proposal_ledger_head_sha256": binding.ledger.head_event_sha256,
        "proposal_ledger_event_count": binding.ledger.event_count,
        "proposal_bootstrap_completion_sha256": binding.completion_snapshot.sha256,
        "review_id": binding.review["review_id"],
        "reviewed_at": binding.review["reviewed_at"],
        "review_record_sha256": binding.review_snapshot.sha256,
        "reviewer": binding.review["reviewer"],
        "reviewer_identity_verification": REVIEWER_IDENTITY_VERIFICATION,
        "approval_tool_sha256": binding.tool_snapshot.sha256,
    }
    for key, expected in expected_audit.items():
        if value[key] != expected:
            raise ApprovalError(f"Approved policy audit binding differs: {key}")
    return value


def _output_path(raw: str | Path, *, binding: BindingValidation) -> Path:
    path = _absolute_local_path(raw, label="Approved policy output")
    if _canonical_key(path.parent) != _canonical_key(binding.approval_directory):
        raise ApprovalError("Approved policy output must be directly inside RunRoot/approval.")
    _assert_no_reparse_components(path, include_leaf=False)
    if os.path.lexists(path):
        raise ApprovalError("Approved policy output must be create-new.")
    if _canonical_key(path) in {
        _canonical_key(binding.proposal_snapshot.path),
        _canonical_key(binding.review_snapshot.path),
    }:
        raise ApprovalError("Approved policy output must not replace an input.")
    return path


def _review_output_path(raw: str | Path, *, preparation: ReviewPreparation) -> Path:
    path = _absolute_local_path(raw, label="Lossless review output")
    if _canonical_key(path.parent) != _canonical_key(
        preparation.approval_directory
    ):
        raise ApprovalError(
            "Lossless review output must be directly inside RunRoot/approval."
        )
    _assert_no_reparse_components(path, include_leaf=False)
    if os.path.lexists(path):
        raise ApprovalError("Lossless review output must be create-new.")
    if _canonical_key(path) in {
        _canonical_key(preparation.proposal_snapshot.path),
        _canonical_key(preparation.body_snapshot.path),
    }:
        raise ApprovalError("Lossless review output must not replace an input.")
    return path


@contextmanager
def _defer_sigint_during_publication(on_pending: Any):
    previous: Any | None = None
    installed = False
    state = {"pending": False}

    def defer(_signum: int, _frame: Any) -> None:
        state["pending"] = True

    try:
        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, defer)
        installed = True
    except (AttributeError, OSError, ValueError):
        pass
    try:
        yield
    finally:
        active_exception = sys.exc_info()[0] is not None
        if state["pending"] and not active_exception:
            on_pending()
        if installed:
            signal.signal(signal.SIGINT, previous)
        if state["pending"] and not active_exception:
            raise KeyboardInterrupt


def _safe_unlink_created(path: Path, identity: tuple[int, int]) -> bool:
    try:
        metadata = os.lstat(path)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or _is_reparse_point(path)
            or (metadata.st_dev, metadata.st_ino) != identity
        ):
            return False
        path.unlink()
        return True
    except OSError:
        return False


def _write_json_create_new(
    path: Path,
    value: dict[str, Any],
) -> tuple[int, int]:
    encoded = _canonical_json_bytes(value)
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    )
    temporary_identity: tuple[int, int] | None = None
    published_identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
            metadata = os.fstat(stream.fileno())
            temporary_identity = (metadata.st_dev, metadata.st_ino)
        os.link(temporary, path)
        published_identity = temporary_identity
        output_metadata = os.lstat(path)
        if (
            temporary_identity is None
            or (output_metadata.st_dev, output_metadata.st_ino) != temporary_identity
            or not stat.S_ISREG(output_metadata.st_mode)
            or _is_reparse_point(path)
        ):
            raise ApprovalError("Published policy did not bind the create-new file.")
        if not _safe_unlink_created(temporary, temporary_identity):
            raise ApprovalError("Published policy temporary link could not be removed safely.")
        final_metadata = os.lstat(path)
        if (
            final_metadata.st_nlink != 1
            or (final_metadata.st_dev, final_metadata.st_ino) != published_identity
        ):
            raise ApprovalError("Published policy has an unsafe link identity.")
        return published_identity
    except BaseException:
        cleanup_identity = published_identity or temporary_identity
        if cleanup_identity is not None:
            _safe_unlink_created(path, cleanup_identity)
        raise
    finally:
        if temporary_identity is not None:
            _safe_unlink_created(temporary, temporary_identity)


def record_review(arguments: argparse.Namespace) -> Path:
    if not arguments.confirm_lossless_reviewed:
        raise ApprovalError("--confirm-lossless-reviewed is required.")
    if not arguments.confirm_reviewer_operator_asserted:
        raise ApprovalError("--confirm-reviewer-operator-asserted is required.")
    review_id = _validate_identifier(arguments.review_id, label="review_id")
    reviewer = _validate_identifier(arguments.reviewer, label="reviewer")
    notes = _validate_review_notes(arguments.notes)
    expected_proposal = _validate_sha256(
        arguments.expected_proposal_sha256, label="expected proposal sha256"
    )
    proposal_path = _absolute_local_path(arguments.proposal, label="Proposal")
    proposal_run_root = _absolute_local_path(
        arguments.proposal_run_root, label="Proposal RunRoot"
    )
    preparation = _prepare_review_record(
        proposal_path=proposal_path,
        proposal_run_root=proposal_run_root,
        expected_proposal_sha256=expected_proposal,
    )
    output = _review_output_path(arguments.output, preparation=preparation)
    pending_nodes = preparation.body["review_requirements"][
        "pending_migration_nodes"
    ]
    review = {
        "format": REVIEW_FORMAT,
        "format_version": REVIEW_VERSION,
        "review_id": review_id,
        "reviewed_at": datetime.now(UTC).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        ),
        "reviewer": reviewer,
        "reviewer_identity_verification": REVIEWER_IDENTITY_VERIFICATION,
        "proposal_sha256": preparation.proposal_snapshot.sha256,
        "proposal_body_sha256": preparation.body_snapshot.sha256,
        "evidence_set_sha256": preparation.body["evidence_set_sha256"],
        "conclusion": "lossless",
        "migrations_reviewed": pending_nodes,
        "notes": notes,
    }
    _validate_review(
        review,
        reviewer=reviewer,
        proposal_sha256=preparation.proposal_snapshot.sha256,
        proposal_body_sha256=preparation.body_snapshot.sha256,
        evidence_set_sha256=preparation.body["evidence_set_sha256"],
        pending_nodes=pending_nodes,
    )
    expected_review = _canonical_json_sha256(review)
    published_identity: tuple[int, int] | None = None

    def cancel_pending_publication() -> None:
        if published_identity is not None:
            _safe_unlink_created(output, published_identity)

    with _defer_sigint_during_publication(cancel_pending_publication):
        preparation.revalidate()
        try:
            published_identity = _write_json_create_new(output, review)
            _assert_private_output_file(output, preparation.approval_directory)
            binding = _validate_binding(
                proposal_path=proposal_path,
                review_path=output,
                proposal_run_root=proposal_run_root,
                expected_proposal_sha256=expected_proposal,
                expected_review_sha256=expected_review,
                reviewer=reviewer,
                require_source_proposal_path=True,
            )
            if (
                (binding.review_snapshot.identity[0], binding.review_snapshot.identity[1])
                != published_identity
                or binding.review_snapshot.raw != _canonical_json_bytes(review)
                or binding.review != review
            ):
                raise ApprovalError("Recorded review output identity or content changed.")
            binding.revalidate()
            _assert_private_output_file(output, preparation.approval_directory)
        except BaseException:
            if published_identity is not None:
                _safe_unlink_created(output, published_identity)
            raise
    return output


def approve(arguments: argparse.Namespace) -> Path:
    if not arguments.confirm_lossless_reviewed:
        raise ApprovalError("--confirm-lossless-reviewed is required.")
    if not arguments.confirm_reviewer_operator_asserted:
        raise ApprovalError("--confirm-reviewer-operator-asserted is required.")
    reviewer = _validate_identifier(arguments.reviewer, label="reviewer")
    expected_proposal = _validate_sha256(
        arguments.expected_proposal_sha256, label="expected proposal sha256"
    )
    expected_review = _validate_sha256(
        arguments.expected_review_sha256, label="expected review sha256"
    )
    binding = _validate_binding(
        proposal_path=_absolute_local_path(arguments.proposal, label="Proposal"),
        review_path=_absolute_local_path(arguments.review, label="Review record"),
        proposal_run_root=_absolute_local_path(
            arguments.proposal_run_root, label="Proposal RunRoot"
        ),
        expected_proposal_sha256=expected_proposal,
        expected_review_sha256=expected_review,
        reviewer=reviewer,
        require_source_proposal_path=True,
    )
    if _canonical_key(binding.review_snapshot.path.parent) != _canonical_key(
        binding.approval_directory
    ):
        raise ApprovalError("Review record must be directly inside RunRoot/approval.")
    output = _output_path(arguments.output, binding=binding)
    policy = _policy_for_binding(binding)
    _validate_policy(policy, binding=binding)
    published_identity: tuple[int, int] | None = None

    def cancel_pending_publication() -> None:
        if published_identity is not None:
            _safe_unlink_created(output, published_identity)

    with _defer_sigint_during_publication(cancel_pending_publication):
        binding.revalidate()
        try:
            published_identity = _write_json_create_new(output, policy)
            _assert_private_output_file(output, binding.approval_directory)
            output_snapshot = _stable_snapshot(
                output, maximum_size=MAX_JSON_BYTES, label="Approved policy output"
            )
            if (
                (output_snapshot.identity[0], output_snapshot.identity[1])
                != published_identity
            ):
                raise ApprovalError("Approved policy output identity changed.")
            parsed = _load_json(
                output_snapshot, label="approved policy output", canonical=True
            )
            _validate_policy(parsed, binding=binding)
            binding.revalidate()
            _assert_private_output_file(output, binding.approval_directory)
        except BaseException:
            if published_identity is not None:
                _safe_unlink_created(output, published_identity)
            raise
    return output


def verify(arguments: argparse.Namespace) -> str:
    expected_policy = _validate_sha256(
        arguments.expected_policy_sha256, label="expected policy sha256"
    )
    expected_proposal = _validate_sha256(
        arguments.expected_proposal_sha256, label="expected proposal sha256"
    )
    expected_review = _validate_sha256(
        arguments.expected_review_sha256, label="expected review sha256"
    )
    policy_path = _absolute_local_path(arguments.policy, label="Frozen policy")
    policy_snapshot = _stable_snapshot(
        policy_path, maximum_size=MAX_JSON_BYTES, label="Frozen policy"
    )
    if policy_snapshot.sha256 != expected_policy:
        raise ApprovalError("Frozen policy SHA-256 does not match the expected value.")
    binding = _validate_binding(
        proposal_path=_absolute_local_path(arguments.proposal, label="Frozen proposal"),
        review_path=_absolute_local_path(arguments.review, label="Frozen review"),
        proposal_run_root=_absolute_local_path(
            arguments.proposal_run_root, label="Proposal RunRoot"
        ),
        expected_proposal_sha256=expected_proposal,
        expected_review_sha256=expected_review,
        reviewer=None,
        require_source_proposal_path=False,
    )
    binding.tracker.snapshots[_canonical_key(policy_snapshot.path)] = policy_snapshot
    policy = _load_json(policy_snapshot, label="frozen approved policy", canonical=True)
    _validate_policy(policy, binding=binding)
    binding.revalidate()
    return policy_snapshot.sha256


def main() -> int:
    try:
        _assert_isolated_stdlib_runtime()
        arguments = _parse_arguments()
        if arguments.command == "record-review":
            output = record_review(arguments)
            output_digest = _stable_snapshot(
                output,
                maximum_size=MAX_JSON_BYTES,
                label="Lossless review output",
            ).sha256
            print(f"Lossless review record written: {output}; sha256={output_digest}")
        elif arguments.command == "approve":
            output = approve(arguments)
            output_digest = _stable_snapshot(
                output,
                maximum_size=MAX_JSON_BYTES,
                label="Approved policy output",
            ).sha256
            print(
                "Approved production-copy policy written: "
                f"{output}; sha256={output_digest}"
            )
        else:
            digest = verify(arguments)
            print(f"Production-copy policy binding verified: sha256={digest}")
    except KeyboardInterrupt:
        print("Production-copy policy approval interrupted.", file=sys.stderr)
        return 130
    except (ApprovalError, OSError, ValueError) as exc:
        print(f"Production-copy policy approval refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
