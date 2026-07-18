"""Preflight and execute a legacy-host SQLite production capture.

This command is intentionally standalone: it uses only the standard library,
does not import Django, and is expected to run with ``python -I -S -B`` from a
controlled tool directory outside the deployed repository.  It verifies and
revalidates the exact reviewed bytes of its Win32 handoff core and backup-set
verifier before executing those bytes.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import ntpath
import os
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import stat
import sys
from types import ModuleType
from typing import Any, Callable, Mapping


FORMAT = "ffxivshare-production-copy-capture-gate"
FORMAT_VERSION = 1
MAX_TOOL_BYTES = 16 * 1024 * 1024
MAX_REPORT_BYTES = 8 * 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
)
TOOL_FILENAMES = {
    "capture_gate": "ProductionCopyCaptureGate.py",
    "handoff_core": "ProductionCopyHandoff.py",
    "backup_verifier": "Verify-SQLiteBackupSet.py",
    "backup_tool": "database_backup.py",
}
PRECHECKS = [
    "trusted_tool_hashes_and_controlled_tool_scope",
    "production_source_path_identity",
    "fixed_local_ntfs_paths_without_reparse_points",
    "dedicated_private_capture_layout",
    "database_directory_exactly_empty",
    "create_new_evidence_path",
]
CAPTURE_CHECKS = [
    "preflight_authority_revalidated",
    "trusted_tool_hashes_and_controlled_tool_scope_revalidated",
    "source_path_identity_revalidated",
    "database_directory_identity_and_acl_revalidated",
    "verified_backup_tool_invoked_for_frozen_source_path_identity_and_output",
    "database_directory_exact_three_members",
    "backup_set_checksum_and_metadata_contract_verified",
    "create_new_final_evidence_path",
]
PRE_LIMITATIONS = {
    "backup_set_content_verified": False,
    "continuous_path_stability_proven": False,
    "cutover_authorization_provided": False,
    "database_media_consistency_proven": False,
    "expected_tool_hashes": "externally_supplied_trust_anchors",
    "source_database_content_stability_proven": False,
}
FINAL_LIMITATIONS = {
    "continuous_path_stability_proven": False,
    "cutover_authorization_provided": False,
    "database_media_consistency_proven": False,
    "integrity_and_foreign_key_values": "backup_producer_metadata_claims_only",
    "sqlite_pragmas_independently_rechecked": False,
    "source_database_content_stability_proven": False,
    "subsequent_snapshot_inspector_required": True,
}


class CaptureGateError(RuntimeError):
    """The production capture gate failed closed."""


def _assert_runtime_contract() -> None:
    if (
        sys.version_info < (3, 10)
        or not sys.flags.isolated
        or not sys.flags.no_site
        or not sys.flags.dont_write_bytecode
        or not sys.flags.utf8_mode
        or sys.flags.optimize != 0
    ):
        raise CaptureGateError(
            "Capture gate requires Python 3.10+ with -I -S -B -X utf8 and no -O."
        )


def _runtime_observation() -> dict[str, Any]:
    return {
        "python_version": sys.version,
        "version_info": list(sys.version_info[:3]),
        "sqlite_version": sqlite3.sqlite_version,
        "isolated": bool(sys.flags.isolated),
        "no_site": bool(sys.flags.no_site),
        "dont_write_bytecode": bool(sys.flags.dont_write_bytecode),
        "utf8_mode": bool(sys.flags.utf8_mode),
        "optimize": int(sys.flags.optimize),
    }


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise CaptureGateError("Capture evidence cannot be serialized canonically.") from exc
    return (rendered + "\n").encode("utf-8")


def _canonical_sha256(value: Any) -> str:
    return sha256(_canonical_json_bytes(value)).hexdigest()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_number(value: str) -> None:
    raise ValueError(f"unsupported JSON number: {value}")


def _strict_json(payload: bytes, *, label: str) -> Any:
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise CaptureGateError(f"{label} is not strict UTF-8 JSON.") from exc


def _stat_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def _stable_read(
    path: str,
    *,
    maximum_size: int,
    label: str,
) -> tuple[bytes, int, str, tuple[int, int, int, int, int, int]]:
    digest = sha256()
    chunks: list[bytes] = []
    total = 0
    try:
        with open(path, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise CaptureGateError(f"{label} must be a unique regular file.")
            if before.st_size > maximum_size:
                raise CaptureGateError(f"{label} is too large.")
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum_size:
                    raise CaptureGateError(f"{label} is too large.")
                digest.update(chunk)
                chunks.append(chunk)
            after = os.fstat(stream.fileno())
        current = os.stat(path)
    except CaptureGateError:
        raise
    except OSError as exc:
        raise CaptureGateError(f"{label} could not be read stably.") from exc
    identity = _stat_identity(before)
    if (
        identity != _stat_identity(after)
        or identity != _stat_identity(current)
        or total != before.st_size
    ):
        raise CaptureGateError(f"{label} changed while it was read.")
    return b"".join(chunks), total, digest.hexdigest(), identity


def _expect_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise CaptureGateError(f"{label} must be a lowercase SHA-256 digest.")
    return value


def _raw_windows_path(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CaptureGateError(f"{label} must be an absolute local Windows path.")
    if value.startswith(("\\\\", "\\?\\", "\\.\\")):
        raise CaptureGateError(f"{label} must not use UNC or device syntax.")
    if "/" in value or ".." in PureWindowsPath(value).parts:
        raise CaptureGateError(f"{label} must be a canonical DOS path without traversal.")
    drive, tail = ntpath.splitdrive(value)
    if re.fullmatch(r"[A-Za-z]:", drive or "") is None or not tail.startswith("\\"):
        raise CaptureGateError(f"{label} must be an absolute drive path.")
    if ":" in tail:
        raise CaptureGateError(f"{label} must not use an alternate data stream.")
    normalized = ntpath.normpath(value)
    if normalized == f"{drive}\\":
        raise CaptureGateError(f"{label} must not be a volume root.")
    return normalized[0].upper() + normalized[1:]


def _load_verified_module(
    path: str,
    *,
    expected_sha256: str,
    module_name: str,
    label: str,
) -> tuple[ModuleType, dict[str, Any]]:
    payload, size, digest, identity = _stable_read(
        path,
        maximum_size=MAX_TOOL_BYTES,
        label=label,
    )
    if digest != expected_sha256:
        raise CaptureGateError(f"{label} SHA-256 does not match the trusted value.")
    try:
        code = compile(payload, path, "exec", dont_inherit=True, optimize=0)
        module = ModuleType(module_name)
        module.__file__ = path
        module.__package__ = ""
        sys.modules[module_name] = module
        exec(code, module.__dict__)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module, {
        "path": path,
        "sha256": digest,
        "size": size,
        "stat_identity": list(identity),
    }


def _load_trusted_cores(
    handoff_path: str,
    handoff_digest: str,
    verifier_path: str,
    verifier_digest: str,
) -> tuple[Any, Any]:
    core, _core_read = _load_verified_module(
        handoff_path,
        expected_sha256=handoff_digest,
        module_name="ffxivshare_capture_verified_handoff",
        label="Handoff core",
    )
    verifier, _verifier_read = _load_verified_module(
        verifier_path,
        expected_sha256=verifier_digest,
        module_name="ffxivshare_capture_verified_backup_verifier",
        label="Backup-set verifier",
    )
    required_core = (
        "_Win32Api",
        "_require_windows_live_check",
        "_resolve_existing_path",
        "_runtime_input_shape",
        "_validate_output_parent",
        "_validate_published_output",
        "_capture_scope",
        "_open_inspected_path",
        "_ancestor_projection",
        "_verify_backup_content",
        "_write_create_new",
        "_unlink_if_identity",
        "_read_stable_file",
        "_windows_paths_overlap",
    )
    if any(not hasattr(core, name) for name in required_core):
        raise CaptureGateError("Verified handoff core lacks required capture primitives.")
    core._BACKUP_MODULE = verifier
    return core, verifier


def _load_trusted_backup_tool(path: str, expected_digest: str) -> Any:
    module, _read = _load_verified_module(
        path,
        expected_sha256=expected_digest,
        module_name="ffxivshare_capture_verified_database_backup",
        label="Database backup tool",
    )
    if not hasattr(module, "backup_sqlite_path"):
        raise CaptureGateError("Verified database backup tool lacks backup_sqlite_path.")
    return module


def _assert_fixed_ntfs(api: Any, core: Any, path: str, *, label: str) -> None:
    core_path = _raw_windows_path(path, label=label)
    api.validate_volume(core_path)
    drive_type = api.kernel32.GetDriveTypeW(core_path[:3])
    if drive_type != core.DRIVE_FIXED:
        raise CaptureGateError(f"{label} must be on a fixed local NTFS volume.")


def _path_authority(
    api: Any,
    core: Any,
    path: str,
    *,
    label: str,
    require_directory: bool,
) -> dict[str, Any]:
    resolved = core._resolve_existing_path(
        api,
        path,
        label=label,
        require_directory=require_directory,
    )
    _assert_fixed_ntfs(api, core, resolved, label=label)
    handle, final, information = core._open_inspected_path(api, resolved)
    try:
        if core._windows_path_key(final) != core._windows_path_key(resolved):
            raise CaptureGateError(f"{label} path identity changed.")
        is_directory = bool(information.dwFileAttributes & 0x10)
        if is_directory != require_directory:
            raise CaptureGateError(f"{label} has an unexpected file kind.")
        if not is_directory and information.nNumberOfLinks != 1:
            raise CaptureGateError(f"{label} must be a single-link file.")
        return {
            "path": final,
            "kind": "directory" if is_directory else "file",
            "volume_serial_number": f"{int(information.dwVolumeSerialNumber):08x}",
            "file_id": (
                f"{((int(information.nFileIndexHigh) << 32) | int(information.nFileIndexLow)):016x}"
            ),
            "link_count": int(information.nNumberOfLinks),
        }
    finally:
        api.close_handle(handle)


def _directory_members(path: str) -> list[str]:
    try:
        return sorted(entry.name for entry in os.scandir(path))
    except OSError as exc:
        raise CaptureGateError(f"Directory cannot be enumerated safely: {path}") from exc


def _writable_scope_snapshot(api: Any, core: Any, path: str) -> dict[str, Any]:
    handle, final, information = core._open_inspected_path(api, path)
    try:
        if not information.dwFileAttributes & 0x10:
            raise CaptureGateError("Writable capture scope is not a directory.")
        security = api.security(handle)
    finally:
        api.close_handle(handle)
    ancestors = core._ancestor_projection(api, final)
    return {
        "path": final,
        "volume_serial_number": f"{int(information.dwVolumeSerialNumber):08x}",
        "file_id": (
            f"{((int(information.nFileIndexHigh) << 32) | int(information.nFileIndexLow)):016x}"
        ),
        "owner_sid": security["owner_sid"],
        "dacl_protected": security["dacl_protected"],
        "dacl_sha256": security["dacl_sha256"],
        "ancestor_chain": ancestors,
        "ancestor_chain_sha256": core._canonical_sha256(ancestors),
    }


def _validate_writable_scope(
    api: Any,
    core: Any,
    path: str,
    *,
    label: str,
) -> dict[str, Any]:
    sentinel = ntpath.join(path, ".ffxivshare-capture-acl-check.must-not-exist")
    if sentinel != core._validate_output_parent(api, sentinel):
        raise CaptureGateError(f"{label} path identity changed during ACL validation.")
    _assert_fixed_ntfs(api, core, path, label=label)
    first = _writable_scope_snapshot(api, core, path)
    second = _writable_scope_snapshot(api, core, path)
    if first != second:
        raise CaptureGateError(f"{label} identity or ACL changed during preflight.")
    return first


def _tool_file_map(tool_files: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not isinstance(tool_files, list) or len(tool_files) != len(TOOL_FILENAMES):
        raise CaptureGateError("Tool authority must contain exactly four files.")
    result: dict[str, dict[str, Any]] = {}
    required = {"role", "path", "sha256", "size", "authority"}
    for item in tool_files:
        if not isinstance(item, dict) or set(item) != required:
            raise CaptureGateError("Tool authority row has an invalid structure.")
        role = item["role"]
        if role not in TOOL_FILENAMES or role in result:
            raise CaptureGateError("Tool authority roles are invalid.")
        _raw_windows_path(item["path"], label=f"Tool {role}")
        _expect_sha256(item["sha256"], label=f"Tool {role} SHA-256")
        if type(item["size"]) is not int or item["size"] < 1:
            raise CaptureGateError("Tool authority size is invalid.")
        if not isinstance(item["authority"], dict):
            raise CaptureGateError("Tool path authority is invalid.")
        result[role] = item
    if set(result) != set(TOOL_FILENAMES):
        raise CaptureGateError("Tool authority roles are incomplete.")
    return result


def _verify_tool_bundle(
    api: Any,
    core: Any,
    *,
    expected_paths: Mapping[str, str],
    expected_hashes: Mapping[str, str],
) -> dict[str, Any]:
    resolved: dict[str, str] = {}
    parents: set[str] = set()
    for role in TOOL_FILENAMES:
        path = core._resolve_existing_path(
            api,
            expected_paths[role],
            label=f"{role} tool",
            require_directory=False,
        )
        _assert_fixed_ntfs(api, core, path, label=f"{role} tool")
        if ntpath.basename(path) != TOOL_FILENAMES[role]:
            raise CaptureGateError(f"{role} tool has an unexpected filename.")
        resolved[role] = path
        parents.add(core._windows_path_key(ntpath.dirname(path)))
    if len(parents) != 1:
        raise CaptureGateError("All four capture tools must share one directory.")
    tool_directory = ntpath.dirname(resolved["capture_gate"])
    if _directory_members(tool_directory) != sorted(TOOL_FILENAMES.values()):
        raise CaptureGateError("Tool directory must contain exactly the four reviewed files.")
    _assert_fixed_ntfs(api, core, tool_directory, label="Tool directory")
    scope = core._capture_scope(
        api,
        role="target_media_root_1",
        scope_path=tool_directory,
    )
    scope["role"] = "capture_tool_bundle"
    files: list[dict[str, Any]] = []
    for role in sorted(TOOL_FILENAMES):
        payload, size, digest, _identity = _stable_read(
            resolved[role],
            maximum_size=MAX_TOOL_BYTES,
            label=f"{role} tool",
        )
        if digest != expected_hashes[role]:
            raise CaptureGateError(f"{role} tool changed or has an untrusted digest.")
        if role == "backup_tool":
            try:
                compile(payload, resolved[role], "exec", dont_inherit=True, optimize=0)
            except (SyntaxError, ValueError) as exc:
                raise CaptureGateError("Backup tool cannot be compiled by this Python.") from exc
        files.append(
            {
                "role": role,
                "path": resolved[role],
                "sha256": digest,
                "size": size,
                "authority": _path_authority(
                    api,
                    core,
                    resolved[role],
                    label=f"{role} tool",
                    require_directory=False,
                ),
            }
        )
    if _directory_members(tool_directory) != sorted(TOOL_FILENAMES.values()):
        raise CaptureGateError("Tool directory changed during verification.")
    return {"directory": tool_directory, "files": files, "scope": scope}


def _is_within(core: Any, child: str, parent: str) -> bool:
    child_key = core._windows_path_key(child)
    parent_key = core._windows_path_key(parent)
    try:
        return ntpath.commonpath((child_key, parent_key)) == parent_key
    except ValueError:
        return False


def _assert_no_overlap(core: Any, first: str, second: str, *, label: str) -> None:
    if core._windows_paths_overlap(first, second):
        raise CaptureGateError(f"Capture paths overlap: {label}.")


def _validate_application_version(core: Any, value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 255
        or value.casefold() in core.PLACEHOLDER_APPLICATION_VERSIONS
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise CaptureGateError("Application version must be a real immutable identifier.")
    return value


def _source_observation(path: str) -> dict[str, Any]:
    try:
        metadata = os.stat(path)
    except OSError as exc:
        raise CaptureGateError("Source database cannot be observed.") from exc
    return {
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
        "wal_exists": os.path.lexists(path + "-wal"),
        "shm_exists": os.path.lexists(path + "-shm"),
        "journal_exists": os.path.lexists(path + "-journal"),
    }


def _publish_report(
    core: Any,
    api: Any,
    *,
    path: str,
    report: dict[str, Any],
    prepublish_check: Callable[[], None],
) -> tuple[int, str]:
    if path != core._validate_output_parent(api, path):
        raise CaptureGateError("Evidence output path identity changed before publication.")
    prepublish_check()
    if path != core._validate_output_parent(api, path):
        raise CaptureGateError("Evidence output path changed after final prepublication checks.")
    publication_identity: list[tuple[int, int]] = []
    try:
        core._write_create_new(path, report, publication_identity=publication_identity)
        core._validate_published_output(api, path)
        payload, size, digest, _identity = core._read_stable_file(
            path,
            maximum_size=MAX_REPORT_BYTES,
            label="Capture evidence",
        )
        if payload != _canonical_json_bytes(report):
            raise CaptureGateError("Published capture evidence bytes are not canonical.")
        payload2, size2, digest2, identity2 = core._read_stable_file(
            path,
            maximum_size=MAX_REPORT_BYTES,
            label="Capture evidence",
        )
        if payload2 != payload or size2 != size or digest2 != digest:
            raise CaptureGateError("Published capture evidence changed after verification.")
        if identity2[5] != 1:
            raise CaptureGateError("Published capture evidence became hard-linked.")
        return size, digest
    except BaseException:
        problem = core._unlink_if_identity(
            path,
            publication_identity[0] if publication_identity else None,
            label="Published capture evidence",
        )
        if problem:
            print(
                f"Capture evidence cleanup warning: {problem}; QUARANTINE REQUIRED.",
                file=sys.stderr,
            )
        raise


def _expected_triplet(output_database: str) -> dict[str, str]:
    return {
        "database": output_database,
        "checksum": output_database + ".sha256",
        "metadata": output_database + ".metadata.json",
    }


def _preflight_authority(
    *,
    application_version: str,
    production_repository: dict[str, Any],
    source_database: dict[str, Any],
    planned_backup_set: dict[str, str],
    tooling: dict[str, Any],
    database_scope: dict[str, Any],
    evidence_scope: dict[str, Any],
    capture_root: str,
    preflight_report: str,
    final_report: str,
) -> dict[str, Any]:
    return {
        "application_version": application_version,
        "production_repository": production_repository,
        "source_database": source_database,
        "planned_backup_set": planned_backup_set,
        "tooling": tooling,
        "database_scope": database_scope,
        "evidence_scope": evidence_scope,
        "capture_root": capture_root,
        "preflight_report": preflight_report,
        "final_report": final_report,
    }


def _preflight_command(arguments: argparse.Namespace) -> int:
    if not arguments.confirm_dedicated_new_empty_output_directory:
        raise CaptureGateError(
            "Preflight requires explicit confirmation of a dedicated new empty output directory."
        )
    expected_hashes = {
        "capture_gate": _expect_sha256(
            arguments.expected_gate_sha256, label="Expected gate SHA-256"
        ),
        "handoff_core": _expect_sha256(
            arguments.expected_handoff_core_sha256,
            label="Expected handoff core SHA-256",
        ),
        "backup_verifier": _expect_sha256(
            arguments.expected_backup_verifier_sha256,
            label="Expected backup verifier SHA-256",
        ),
        "backup_tool": _expect_sha256(
            arguments.expected_backup_tool_sha256,
            label="Expected backup tool SHA-256",
        ),
    }
    gate_path = _raw_windows_path(os.path.abspath(__file__), label="Capture gate")
    handoff_path = _raw_windows_path(arguments.handoff_core, label="Handoff core")
    verifier_path = _raw_windows_path(arguments.backup_verifier, label="Backup verifier")
    backup_tool_path = _raw_windows_path(arguments.backup_tool, label="Backup tool")
    gate_payload, _gate_size, gate_digest, _gate_identity = _stable_read(
        gate_path,
        maximum_size=MAX_TOOL_BYTES,
        label="Capture gate",
    )
    if gate_digest != expected_hashes["capture_gate"]:
        raise CaptureGateError("Capture gate SHA-256 does not match the trusted value.")
    if not gate_payload:
        raise CaptureGateError("Capture gate bytes are empty.")
    core, _verifier = _load_trusted_cores(
        handoff_path,
        expected_hashes["handoff_core"],
        verifier_path,
        expected_hashes["backup_verifier"],
    )
    core._require_windows_live_check()
    api = core._Win32Api()
    expected_paths = {
        "capture_gate": gate_path,
        "handoff_core": handoff_path,
        "backup_verifier": verifier_path,
        "backup_tool": backup_tool_path,
    }
    tooling = _verify_tool_bundle(
        api,
        core,
        expected_paths=expected_paths,
        expected_hashes=expected_hashes,
    )

    repository = core._resolve_existing_path(
        api,
        arguments.production_repository_root,
        label="Production repository root",
        require_directory=True,
    )
    source = core._resolve_existing_path(
        api,
        arguments.source_database,
        label="Source database",
        require_directory=False,
    )
    _assert_fixed_ntfs(api, core, repository, label="Production repository root")
    _assert_fixed_ntfs(api, core, source, label="Source database")
    if not _is_within(core, source, repository):
        raise CaptureGateError("Source database must be inside the recorded production root.")

    output_database = core._runtime_input_shape(
        arguments.output_database,
        label="Output database",
    )
    preflight_report = core._runtime_input_shape(
        arguments.output_report,
        label="Preflight report",
    )
    if ntpath.basename(output_database) != "production.sqlite3":
        raise CaptureGateError("Output database filename must be production.sqlite3.")
    if ntpath.basename(preflight_report) != "capture-preflight.json":
        raise CaptureGateError("Preflight report filename must be capture-preflight.json.")
    database_directory = ntpath.dirname(output_database)
    evidence_directory = ntpath.dirname(preflight_report)
    if ntpath.basename(database_directory) != "Database":
        raise CaptureGateError("Output database parent must be named Database.")
    if ntpath.basename(evidence_directory) != "Audit":
        raise CaptureGateError("Preflight report parent must be named Audit.")
    capture_root = ntpath.dirname(database_directory)
    if core._windows_path_key(ntpath.dirname(evidence_directory)) != core._windows_path_key(
        capture_root
    ):
        raise CaptureGateError("Database and Audit must be sibling capture directories.")
    capture_root = core._resolve_existing_path(
        api,
        capture_root,
        label="Capture root",
        require_directory=True,
    )
    database_directory = core._resolve_existing_path(
        api,
        database_directory,
        label="Database capture directory",
        require_directory=True,
    )
    evidence_directory = core._resolve_existing_path(
        api,
        evidence_directory,
        label="Capture evidence directory",
        require_directory=True,
    )
    output_database = ntpath.join(database_directory, "production.sqlite3")
    preflight_report = ntpath.join(evidence_directory, "capture-preflight.json")
    final_report = ntpath.join(evidence_directory, "capture-final.json")
    _assert_fixed_ntfs(api, core, capture_root, label="Capture root")
    if _directory_members(capture_root) != ["Audit", "Database"]:
        raise CaptureGateError("Capture root must contain exactly Database and Audit.")
    if _directory_members(database_directory):
        raise CaptureGateError("Database capture directory must be exactly empty.")
    if _directory_members(evidence_directory):
        raise CaptureGateError("Audit directory must be exactly empty before preflight.")
    if os.path.lexists(final_report):
        raise CaptureGateError("Final capture report already exists.")

    tool_directory = tooling["directory"]
    _assert_no_overlap(core, repository, tool_directory, label="production/tool")
    _assert_no_overlap(core, repository, capture_root, label="production/capture")
    _assert_no_overlap(core, tool_directory, capture_root, label="tool/capture")
    _assert_no_overlap(
        core,
        database_directory,
        evidence_directory,
        label="database/evidence",
    )
    output_database = core._validate_output_parent(api, output_database)
    preflight_report = core._validate_output_parent(api, preflight_report)
    database_scope = _validate_writable_scope(
        api,
        core,
        database_directory,
        label="Database capture directory",
    )
    evidence_scope = _validate_writable_scope(
        api,
        core,
        evidence_directory,
        label="Capture evidence directory",
    )
    application_version = _validate_application_version(core, arguments.application_version)
    production_authority = _path_authority(
        api,
        core,
        repository,
        label="Production repository root",
        require_directory=True,
    )
    source_authority = _path_authority(
        api,
        core,
        source,
        label="Source database",
        require_directory=False,
    )
    planned_backup_set = _expected_triplet(output_database)
    authority = _preflight_authority(
        application_version=application_version,
        production_repository=production_authority,
        source_database=source_authority,
        planned_backup_set=planned_backup_set,
        tooling=tooling,
        database_scope=database_scope,
        evidence_scope=evidence_scope,
        capture_root=capture_root,
        preflight_report=preflight_report,
        final_report=final_report,
    )
    report = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "phase": "preflight",
        "status": "passed",
        "generated_at": _utc_now(),
        "ready_for_capture": True,
        "capture_set_complete": False,
        "backup_set_contract_verified": False,
        "cutover_authorized": False,
        "authority": authority,
        "authority_sha256": _canonical_sha256(authority),
        "observations": {
            "runtime": _runtime_observation(),
            "source_database": _source_observation(source),
            "database_directory_members": [],
            "evidence_directory_members": [],
        },
        "checks": PRECHECKS,
        "limitations": PRE_LIMITATIONS,
    }

    def prepublish_check() -> None:
        if _directory_members(database_directory):
            raise CaptureGateError("Database directory changed during preflight publication.")
        if _directory_members(evidence_directory):
            raise CaptureGateError("Audit directory changed before preflight publication.")
        if _directory_members(capture_root) != ["Audit", "Database"]:
            raise CaptureGateError("Capture layout changed during preflight publication.")
        current_tooling = _verify_tool_bundle(
            api,
            core,
            expected_paths=expected_paths,
            expected_hashes=expected_hashes,
        )
        if current_tooling != tooling:
            raise CaptureGateError("Tool bundle changed during preflight publication.")
        if _path_authority(
            api,
            core,
            source,
            label="Source database",
            require_directory=False,
        ) != source_authority:
            raise CaptureGateError("Source database path identity changed during preflight.")
        if _validate_writable_scope(
            api,
            core,
            database_directory,
            label="Database capture directory",
        ) != database_scope:
            raise CaptureGateError("Database capture scope changed during preflight.")
        if _validate_writable_scope(
            api,
            core,
            evidence_directory,
            label="Capture evidence directory",
        ) != evidence_scope:
            raise CaptureGateError("Evidence scope changed during preflight.")

    size, digest = _publish_report(
        core,
        api,
        path=preflight_report,
        report=report,
        prepublish_check=prepublish_check,
    )
    print(
        json.dumps(
            {
                "phase": "preflight",
                "ready_for_capture": True,
                "report": preflight_report,
                "sha256": digest,
                "size": size,
                "cutover_authorized": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _validate_path_authority(value: Any, *, label: str) -> dict[str, Any]:
    keys = {"path", "kind", "volume_serial_number", "file_id", "link_count"}
    if not isinstance(value, dict) or set(value) != keys:
        raise CaptureGateError(f"{label} path authority is invalid.")
    _raw_windows_path(value["path"], label=f"{label} path")
    if value["kind"] not in {"file", "directory"}:
        raise CaptureGateError(f"{label} kind is invalid.")
    if (
        not isinstance(value["volume_serial_number"], str)
        or re.fullmatch(r"[0-9a-f]{8}", value["volume_serial_number"]) is None
        or not isinstance(value["file_id"], str)
        or re.fullmatch(r"[0-9a-f]{16}", value["file_id"]) is None
        or type(value["link_count"]) is not int
        or value["link_count"] < 1
    ):
        raise CaptureGateError(f"{label} identity fields are invalid.")
    return value


def _validate_preflight_report(value: Any) -> dict[str, Any]:
    keys = {
        "format",
        "format_version",
        "phase",
        "status",
        "generated_at",
        "ready_for_capture",
        "capture_set_complete",
        "backup_set_contract_verified",
        "cutover_authorized",
        "authority",
        "authority_sha256",
        "observations",
        "checks",
        "limitations",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise CaptureGateError("Preflight report has an unsupported schema.")
    if (
        value["format"] != FORMAT
        or type(value["format_version"]) is not int
        or value["format_version"] != FORMAT_VERSION
        or value["phase"] != "preflight"
        or value["status"] != "passed"
        or value["ready_for_capture"] is not True
        or value["capture_set_complete"] is not False
        or value["backup_set_contract_verified"] is not False
        or value["cutover_authorized"] is not False
        or value["checks"] != PRECHECKS
        or value["limitations"] != PRE_LIMITATIONS
        or not isinstance(value["generated_at"], str)
        or TIMESTAMP_PATTERN.fullmatch(value["generated_at"]) is None
    ):
        raise CaptureGateError("Preflight report status or contract fields are invalid.")
    authority = value["authority"]
    authority_keys = {
        "application_version",
        "production_repository",
        "source_database",
        "planned_backup_set",
        "tooling",
        "database_scope",
        "evidence_scope",
        "capture_root",
        "preflight_report",
        "final_report",
    }
    if not isinstance(authority, dict) or set(authority) != authority_keys:
        raise CaptureGateError("Preflight authority has an invalid structure.")
    _validate_path_authority(authority["production_repository"], label="Production root")
    _validate_path_authority(authority["source_database"], label="Source database")
    planned = authority["planned_backup_set"]
    if not isinstance(planned, dict) or set(planned) != {"database", "checksum", "metadata"}:
        raise CaptureGateError("Planned backup-set authority is invalid.")
    for role, path in planned.items():
        _raw_windows_path(path, label=f"Planned {role}")
    if planned != _expected_triplet(planned["database"]):
        raise CaptureGateError("Planned backup-set filenames are not canonical.")
    tooling = authority["tooling"]
    if not isinstance(tooling, dict) or set(tooling) != {"directory", "files", "scope"}:
        raise CaptureGateError("Tooling authority is invalid.")
    _raw_windows_path(tooling["directory"], label="Tool directory")
    _tool_file_map(tooling["files"])
    for field in ("database_scope", "evidence_scope"):
        scope = authority[field]
        expected = {
            "path",
            "volume_serial_number",
            "file_id",
            "owner_sid",
            "dacl_protected",
            "dacl_sha256",
            "ancestor_chain",
            "ancestor_chain_sha256",
        }
        if not isinstance(scope, dict) or set(scope) != expected:
            raise CaptureGateError(f"{field} authority is invalid.")
        _raw_windows_path(scope["path"], label=field)
    for field in ("capture_root", "preflight_report", "final_report"):
        _raw_windows_path(authority[field], label=field)
    if (
        not isinstance(authority["application_version"], str)
        or not authority["application_version"]
        or _expect_sha256(value["authority_sha256"], label="Authority SHA-256")
        != _canonical_sha256(authority)
    ):
        raise CaptureGateError("Preflight authority digest is invalid.")
    observations = value["observations"]
    if not isinstance(observations, dict) or set(observations) != {
        "runtime",
        "source_database",
        "database_directory_members",
        "evidence_directory_members",
    }:
        raise CaptureGateError("Preflight observations have an invalid structure.")
    runtime = observations["runtime"]
    if (
        not isinstance(runtime, dict)
        or set(runtime)
        != {
            "python_version",
            "version_info",
            "sqlite_version",
            "isolated",
            "no_site",
            "dont_write_bytecode",
            "utf8_mode",
            "optimize",
        }
        or not isinstance(runtime["python_version"], str)
        or not isinstance(runtime["sqlite_version"], str)
        or not runtime["sqlite_version"]
        or not isinstance(runtime["version_info"], list)
        or len(runtime["version_info"]) != 3
        or any(type(part) is not int or part < 0 for part in runtime["version_info"])
        or any(
            runtime[field] is not True
            for field in ("isolated", "no_site", "dont_write_bytecode", "utf8_mode")
        )
        or type(runtime["optimize"]) is not int
        or runtime["optimize"] != 0
    ):
        raise CaptureGateError("Preflight Python runtime evidence is invalid.")
    source_observation = observations["source_database"]
    if (
        not isinstance(source_observation, dict)
        or set(source_observation)
        != {"size", "mtime_ns", "wal_exists", "shm_exists", "journal_exists"}
        or type(source_observation["size"]) is not int
        or source_observation["size"] < 0
        or type(source_observation["mtime_ns"]) is not int
        or any(
            type(source_observation[field]) is not bool
            for field in ("wal_exists", "shm_exists", "journal_exists")
        )
        or observations["database_directory_members"] != []
        or observations["evidence_directory_members"] != []
    ):
        raise CaptureGateError("Preflight source or empty-directory observations are invalid.")
    return authority


def _load_preflight_before_core(
    path: str,
    expected_digest: str,
) -> tuple[dict[str, Any], bytes, int, tuple[int, int, int, int, int, int]]:
    payload, size, digest, identity = _stable_read(
        path,
        maximum_size=MAX_REPORT_BYTES,
        label="Preflight report",
    )
    if digest != expected_digest:
        raise CaptureGateError("Preflight report SHA-256 does not match authority.")
    value = _strict_json(payload, label="Preflight report")
    if payload != _canonical_json_bytes(value):
        raise CaptureGateError("Preflight report is not canonical UTF-8/LF JSON.")
    _validate_preflight_report(value)
    return value, payload, size, identity


def _capture_command(arguments: argparse.Namespace) -> int:
    trusted_hashes = {
        "capture_gate": _expect_sha256(
            arguments.expected_gate_sha256,
            label="Expected gate SHA-256",
        ),
        "handoff_core": _expect_sha256(
            arguments.expected_handoff_core_sha256,
            label="Expected handoff core SHA-256",
        ),
        "backup_verifier": _expect_sha256(
            arguments.expected_backup_verifier_sha256,
            label="Expected backup verifier SHA-256",
        ),
        "backup_tool": _expect_sha256(
            arguments.expected_backup_tool_sha256,
            label="Expected backup tool SHA-256",
        ),
    }
    expected_preflight_digest = _expect_sha256(
        arguments.expected_preflight_sha256,
        label="Expected preflight SHA-256",
    )
    preflight_path = _raw_windows_path(arguments.preflight_report, label="Preflight report")
    value, initial_payload, preflight_size, initial_identity = _load_preflight_before_core(
        preflight_path,
        expected_preflight_digest,
    )
    authority = value["authority"]
    if preflight_path.casefold() != authority["preflight_report"].casefold():
        raise CaptureGateError("Preflight path does not match its frozen authority.")
    tool_map = _tool_file_map(authority["tooling"]["files"])
    if any(tool_map[role]["sha256"] != trusted_hashes[role] for role in TOOL_FILENAMES):
        raise CaptureGateError("Preflight tool digests differ from external trust anchors.")
    capture_runtime = _runtime_observation()
    if capture_runtime != value["observations"]["runtime"]:
        raise CaptureGateError("Capture Python and SQLite runtime differs from preflight.")
    gate_path = _raw_windows_path(os.path.abspath(__file__), label="Capture gate")
    if gate_path.casefold() != tool_map["capture_gate"]["path"].casefold():
        raise CaptureGateError("Capture is running a different capture gate path.")
    gate_payload, _gate_size, gate_digest, _gate_identity = _stable_read(
        gate_path,
        maximum_size=MAX_TOOL_BYTES,
        label="Capture gate",
    )
    if not gate_payload or gate_digest != trusted_hashes["capture_gate"]:
        raise CaptureGateError("Capture gate differs from preflight authority.")
    core, _verifier = _load_trusted_cores(
        tool_map["handoff_core"]["path"],
        trusted_hashes["handoff_core"],
        tool_map["backup_verifier"]["path"],
        trusted_hashes["backup_verifier"],
    )
    core._require_windows_live_check()
    api = core._Win32Api()
    _validate_application_version(core, authority["application_version"])
    core._validate_published_output(api, preflight_path)
    payload, size, digest, identity = core._read_stable_file(
        preflight_path,
        maximum_size=MAX_REPORT_BYTES,
        label="Preflight report",
    )
    if (
        payload != initial_payload
        or size != preflight_size
        or digest != expected_preflight_digest
        or tuple(identity) != tuple(initial_identity)
    ):
        raise CaptureGateError("Preflight report changed while capture was loading it.")
    if _strict_json(payload, label="Preflight report") != value:
        raise CaptureGateError("Preflight report value changed during capture.")

    final_report = core._runtime_input_shape(arguments.output_report, label="Final report")
    if core._windows_path_key(final_report) != core._windows_path_key(authority["final_report"]):
        raise CaptureGateError("Final report path does not match preflight authority.")
    final_report = core._validate_output_parent(api, final_report)
    if ntpath.basename(final_report) != "capture-final.json":
        raise CaptureGateError("Final report filename must be capture-final.json.")

    expected_paths = {role: row["path"] for role, row in tool_map.items()}
    expected_hashes = {role: row["sha256"] for role, row in tool_map.items()}
    current_tooling = _verify_tool_bundle(
        api,
        core,
        expected_paths=expected_paths,
        expected_hashes=expected_hashes,
    )
    if current_tooling != authority["tooling"]:
        raise CaptureGateError("Tool bundle differs from preflight authority.")
    repository = authority["production_repository"]["path"]
    source = authority["source_database"]["path"]
    if _path_authority(
        api,
        core,
        repository,
        label="Production repository root",
        require_directory=True,
    ) != authority["production_repository"]:
        raise CaptureGateError("Production repository identity changed after preflight.")
    if _path_authority(
        api,
        core,
        source,
        label="Source database",
        require_directory=False,
    ) != authority["source_database"]:
        raise CaptureGateError("Source database path identity changed after preflight.")
    database_directory = authority["database_scope"]["path"]
    evidence_directory = authority["evidence_scope"]["path"]
    if _validate_writable_scope(
        api,
        core,
        database_directory,
        label="Database capture directory",
    ) != authority["database_scope"]:
        raise CaptureGateError("Database capture scope identity or ACL changed.")
    if _validate_writable_scope(
        api,
        core,
        evidence_directory,
        label="Capture evidence directory",
    ) != authority["evidence_scope"]:
        raise CaptureGateError("Evidence scope identity or ACL changed.")
    if _directory_members(authority["capture_root"]) != ["Audit", "Database"]:
        raise CaptureGateError("Capture root layout changed after preflight.")
    if _directory_members(evidence_directory) != ["capture-preflight.json"]:
        raise CaptureGateError("Audit directory must contain only the preflight report.")
    planned = authority["planned_backup_set"]
    expected_members = sorted(ntpath.basename(path) for path in planned.values())
    if _directory_members(database_directory):
        raise CaptureGateError("Database directory must still be empty before capture.")
    source_authority_before = _path_authority(
        api,
        core,
        source,
        label="Source database",
        require_directory=False,
    )
    if source_authority_before != authority["source_database"]:
        raise CaptureGateError("Source database identity changed before backup execution.")
    source_observation_before = _source_observation(source)
    backup_module = _load_trusted_backup_tool(
        tool_map["backup_tool"]["path"],
        tool_map["backup_tool"]["sha256"],
    )
    backup_result = backup_module.backup_sqlite_path(
        source,
        planned["database"],
        application_version=authority["application_version"],
    )
    if (
        not isinstance(backup_result, dict)
        or set(backup_result) != {
            "path",
            "checksum_path",
            "metadata_path",
            "sha256",
            "size",
        }
        or core._windows_path_key(str(backup_result["path"]))
        != core._windows_path_key(planned["database"])
        or core._windows_path_key(str(backup_result["checksum_path"]))
        != core._windows_path_key(planned["checksum"])
        or core._windows_path_key(str(backup_result["metadata_path"]))
        != core._windows_path_key(planned["metadata"])
        or _expect_sha256(backup_result["sha256"], label="Backup result SHA-256")
        != backup_result["sha256"]
        or type(backup_result["size"]) is not int
        or backup_result["size"] < 1
    ):
        raise CaptureGateError("Verified backup tool returned an invalid result contract.")
    source_authority_after = _path_authority(
        api,
        core,
        source,
        label="Source database",
        require_directory=False,
    )
    if source_authority_after != source_authority_before:
        raise CaptureGateError("Source database path identity changed during backup execution.")
    source_observation_after = _source_observation(source)
    if _directory_members(database_directory) != expected_members:
        raise CaptureGateError("Backup execution did not produce exactly the expected triplet.")
    post_backup_tooling = _verify_tool_bundle(
        api,
        core,
        expected_paths=expected_paths,
        expected_hashes=expected_hashes,
    )
    if post_backup_tooling != authority["tooling"]:
        raise CaptureGateError("Tool bundle changed during backup execution.")
    file_authorities: dict[str, dict[str, Any]] = {}
    for role, path in planned.items():
        resolved = core._resolve_existing_path(
            api,
            path,
            label=f"Backup {role}",
            require_directory=False,
        )
        _assert_fixed_ntfs(api, core, resolved, label=f"Backup {role}")
        core._validate_published_output(api, resolved)
        file_authorities[role] = _path_authority(
            api,
            core,
            resolved,
            label=f"Backup {role}",
            require_directory=False,
        )
    content = core._verify_backup_content(
        planned["database"],
        planned["checksum"],
        planned["metadata"],
    )
    if (
        backup_result["sha256"] != content["database"]["sha256"]
        or backup_result["size"] != content["database"]["size"]
    ):
        raise CaptureGateError("Backup result summary differs from verified database bytes.")
    if content["application_version"] != authority["application_version"]:
        raise CaptureGateError("Backup metadata application version differs from preflight.")
    if _directory_members(database_directory) != expected_members:
        raise CaptureGateError("Backup directory changed during content verification.")
    if _directory_members(evidence_directory) != ["capture-preflight.json"]:
        raise CaptureGateError("Audit directory changed during capture verification.")

    report = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "phase": "capture",
        "status": "passed",
        "generated_at": _utc_now(),
        "ready_for_capture": False,
        "capture_set_complete": True,
        "backup_set_contract_verified": True,
        "cutover_authorized": False,
        "preflight_authority": {
            "path": preflight_path,
            "sha256": expected_preflight_digest,
            "size": preflight_size,
            "authority_sha256": value["authority_sha256"],
        },
        "application_version": authority["application_version"],
        "source_database": {
            "authority": authority["source_database"],
            "observation_before": source_observation_before,
            "observation_after": source_observation_after,
        },
        "observations": {"runtime": capture_runtime},
        "tooling": current_tooling,
        "output_scope": authority["database_scope"],
        "database_backup_set": {
            "execution": {
                "method": "verified_backup_sqlite_path_in_process",
                "backup_tool_path": tool_map["backup_tool"]["path"],
                "backup_tool_sha256": tool_map["backup_tool"]["sha256"],
                "source_database_authority_before": source_authority_before,
                "source_database_authority_after": source_authority_after,
                "result": backup_result,
            },
            "content": content,
            "file_authorities": file_authorities,
        },
        "checks": CAPTURE_CHECKS,
        "limitations": FINAL_LIMITATIONS,
    }

    def prepublish_check() -> None:
        if _directory_members(database_directory) != expected_members:
            raise CaptureGateError("Backup directory changed before final publication.")
        if _directory_members(evidence_directory) != ["capture-preflight.json"]:
            raise CaptureGateError("Audit directory changed before final publication.")
        if _path_authority(
            api,
            core,
            source,
            label="Source database",
            require_directory=False,
        ) != authority["source_database"]:
            raise CaptureGateError("Source database identity changed before final publication.")
        if _validate_writable_scope(
            api,
            core,
            database_directory,
            label="Database capture directory",
        ) != authority["database_scope"]:
            raise CaptureGateError("Database scope changed before final publication.")
        if _verify_tool_bundle(
            api,
            core,
            expected_paths=expected_paths,
            expected_hashes=expected_hashes,
        ) != authority["tooling"]:
            raise CaptureGateError("Tool bundle changed before final publication.")
        for role, path in planned.items():
            if _path_authority(
                api,
                core,
                path,
                label=f"Backup {role}",
                require_directory=False,
            ) != file_authorities[role]:
                raise CaptureGateError("Backup file identity changed before final publication.")
        final_content = core._verify_backup_content(
            planned["database"],
            planned["checksum"],
            planned["metadata"],
        )
        if final_content != content:
            raise CaptureGateError("Backup content changed before final publication.")

    final_size, final_digest = _publish_report(
        core,
        api,
        path=final_report,
        report=report,
        prepublish_check=prepublish_check,
    )
    print(
        json.dumps(
            {
                "phase": "capture",
                "capture_set_complete": True,
                "backup_set_contract_verified": True,
                "report": final_report,
                "sha256": final_digest,
                "size": final_size,
                "cutover_authorized": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail-closed Win32 gate for a legacy-host production SQLite capture."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--expected-gate-sha256", required=True)
    preflight.add_argument("--handoff-core", required=True)
    preflight.add_argument("--expected-handoff-core-sha256", required=True)
    preflight.add_argument("--backup-verifier", required=True)
    preflight.add_argument("--expected-backup-verifier-sha256", required=True)
    preflight.add_argument("--backup-tool", required=True)
    preflight.add_argument("--expected-backup-tool-sha256", required=True)
    preflight.add_argument("--production-repository-root", required=True)
    preflight.add_argument("--source-database", required=True)
    preflight.add_argument("--output-database", required=True)
    preflight.add_argument("--application-version", required=True)
    preflight.add_argument("--output-report", required=True)
    preflight.add_argument(
        "--confirm-dedicated-new-empty-output-directory",
        action="store_true",
    )
    preflight.set_defaults(handler=_preflight_command)

    capture = subparsers.add_parser("capture")
    capture.add_argument("--expected-gate-sha256", required=True)
    capture.add_argument("--expected-handoff-core-sha256", required=True)
    capture.add_argument("--expected-backup-verifier-sha256", required=True)
    capture.add_argument("--expected-backup-tool-sha256", required=True)
    capture.add_argument("--preflight-report", required=True)
    capture.add_argument("--expected-preflight-sha256", required=True)
    capture.add_argument("--output-report", required=True)
    capture.set_defaults(handler=_capture_command)
    return parser


def _main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        _assert_runtime_contract()
        return int(arguments.handler(arguments))
    except KeyboardInterrupt:
        print(
            "Capture gate interrupted; quarantine any retained evidence and abandon "
            "this CaptureRoot.",
            file=sys.stderr,
        )
        return 130
    except Exception as exc:
        print(f"Capture gate failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
