from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import subprocess
import sys
from contextlib import contextmanager
from typing import Any, Protocol


REPORT_FORMAT = "ffxivshare-production-copy-rehearsal"
REPORT_VERSION = 1
LEDGER_FORMAT = "ffxivshare-production-copy-rehearsal-event"
LEDGER_VERSION = 1
POLICY_FORMAT = "ffxivshare-source-upgrade-policy"
POLICY_VERSION = 2
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
POLICY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SNAPSHOT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MIGRATION_PART_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
SQLITE_SCHEMA_INVENTORY_KEYS = frozenset(
    {
        "format",
        "format_version",
        "schema",
        "included_object_types",
        "excluded_objects",
        "normalization",
        "object_count",
        "objects",
        "sha256",
    }
)
SQLITE_SCHEMA_OBJECT_KEYS = frozenset({"type", "name", "tbl_name", "sql"})
SQLITE_SCHEMA_OBJECT_TYPES = ("index", "table", "trigger", "view")
SQLITE_SCHEMA_INTERNAL_PREFIX = "sqlite_"
UTC_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
EXECUTION_FIXED_FILES = (
    "manage.py",
    "requirements.txt",
    "ops/migration/ProductionCopyBootstrap.py",
    "ops/migration/ProductionCopyHandoff.py",
    "ops/migration/Rehearse-ProductionCopy.py",
    "ops/migration/Approve-ProductionCopyPolicy.py",
    "ops/migration/Propose-ProductionCopyPolicy.py",
    "ops/migration/Verify-SQLiteBackupSet.py",
    "ops/migration/Inspect-SQLiteSnapshot.py",
    "ops/migration/Compare-SiteDataExports.py",
    "ops/migration/MediaManifest.py",
    "ops/migration/Verify-ProductionCopyRehearsalPair.py",
)


class ConfigurationError(RuntimeError):
    pass


class RehearsalError(RuntimeError):
    def __init__(self, code: str, *, stage: str | None = None):
        super().__init__(code)
        self.code = code
        self.stage = stage


class RehearsalBlocked(RehearsalError):
    pass


class RehearsalInterrupted(RehearsalError):
    pass


@contextmanager
def _coherent_finalization_signals():
    """Raise the first Ctrl+C, then ignore further Ctrl+C while finalizing."""

    state = {"finalizing": False}
    previous_handler: Any | None = None
    installed = False

    def handle_sigint(_signum: int, _frame: Any) -> None:
        if not state["finalizing"]:
            state["finalizing"] = True
            raise KeyboardInterrupt

    try:
        previous_handler = signal.getsignal(signal.SIGINT)
        if previous_handler != signal.SIG_IGN:
            signal.signal(signal.SIGINT, handle_sigint)
            installed = True
    except (AttributeError, OSError, ValueError):
        # Signal handlers can only be changed by the main thread. Callers still
        # retain the ordinary finally/close guarantees in unsupported contexts.
        pass

    def begin_finalization() -> None:
        state["finalizing"] = True

    try:
        yield begin_finalization
    finally:
        if installed:
            signal.signal(signal.SIGINT, previous_handler)


@dataclass(frozen=True)
class RehearsalConfig:
    repository_root: Path
    python_executable: Path
    source_database: Path
    source_checksum: Path
    source_metadata: Path
    source_upgrade_policy: Path
    source_media_manifest: Path
    target_media_root: Path
    target_media_snapshot_id: str
    run_root: Path
    confirm_source_immutable: bool
    confirm_target_media_offline: bool
    source_policy_proposal: Path | None = None
    source_review_record: Path | None = None
    source_proposal_run_root: Path | None = None


@dataclass(frozen=True)
class BootstrapInnerContext:
    run_root: Path
    record_path: Path
    record_sha256: str
    workspace_access_control: str
    execution_bundle_sha256: str
    execution_bundle_files: tuple[str, ...]
    policy_path: Path
    proposal_path: Path
    review_path: Path
    repository_root: Path | None = None


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout_path: Path
    stderr_path: Path


class Runner(Protocol):
    def run(
        self,
        *,
        stage: str,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        stdout_path: Path,
        stderr_path: Path,
    ) -> CommandResult: ...


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"JSON constant is not allowed: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"Duplicate JSON key: {key}")
        payload[key] = value
    return payload


def _load_json(path: Path, *, maximum_size: int = 4 * 1024 * 1024) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RehearsalError("artifact_read_failed") from exc
    if len(raw) > maximum_size:
        raise RehearsalError("artifact_too_large")
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RehearsalError("artifact_invalid_json") from exc


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _canonical_json_sha256(value: Any) -> str:
    return sha256(_canonical_json_bytes(value)).hexdigest()


def _stat_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _is_reparse_point(path: Path) -> bool:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        return True
    return bool(
        getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT_ATTRIBUTE
    )


def _read_stable_regular_bytes(
    path: Path,
    *,
    maximum_size: int,
    issue_prefix: str,
    expected_sha256: str | None = None,
    expected_identity: tuple[int, int, int, int, int] | None = None,
) -> tuple[bytes, int, str, tuple[int, int, int, int, int]]:
    try:
        before_path = os.lstat(path)
    except OSError as exc:
        raise RehearsalBlocked(f"{issue_prefix}_unavailable") from exc
    if (
        not stat.S_ISREG(before_path.st_mode)
        or stat.S_ISLNK(before_path.st_mode)
        or getattr(before_path, "st_file_attributes", 0)
        & REPARSE_POINT_ATTRIBUTE
        or before_path.st_nlink != 1
    ):
        raise RehearsalBlocked(f"{issue_prefix}_identity_unsafe")
    before_identity = _stat_identity(before_path)
    if expected_identity is not None and before_identity != expected_identity:
        raise RehearsalBlocked(f"{issue_prefix}_identity_changed")
    if before_path.st_size > maximum_size:
        raise RehearsalBlocked(f"{issue_prefix}_too_large")

    raw = bytearray()
    digest = sha256()
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            while True:
                remaining = maximum_size + 1 - len(raw)
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                raw.extend(chunk)
                digest.update(chunk)
                if len(raw) > maximum_size:
                    raise RehearsalBlocked(f"{issue_prefix}_too_large")
            after = os.fstat(stream.fileno())
        current = os.lstat(path)
    except RehearsalBlocked:
        raise
    except OSError as exc:
        raise RehearsalBlocked(f"{issue_prefix}_read_failed") from exc
    current_identity = _stat_identity(current)
    actual_sha256 = digest.hexdigest()
    if (
        _stat_identity(before) != before_identity
        or _stat_identity(after) != before_identity
        or current_identity != before_identity
        or not stat.S_ISREG(current.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or getattr(current, "st_file_attributes", 0)
        & REPARSE_POINT_ATTRIBUTE
        or current.st_nlink != 1
        or (expected_sha256 is not None and actual_sha256 != expected_sha256)
    ):
        raise RehearsalBlocked(f"{issue_prefix}_checkpoint_failed")
    return bytes(raw), len(raw), actual_sha256, current_identity


def _assert_no_reparse_components(path: Path, *, include_leaf: bool) -> None:
    candidate = path if include_leaf else path.parent
    current = Path(candidate.anchor)
    parts = candidate.parts[1:] if candidate.anchor else candidate.parts
    for part in parts:
        current /= part
        try:
            if _is_reparse_point(current):
                raise ConfigurationError("Path traverses a symlink or reparse point.")
        except FileNotFoundError:
            break
        except OSError as exc:
            raise ConfigurationError("Path components could not be inspected.") from exc


def _absolute_path(raw: str | Path, *, label: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise ConfigurationError(f"{label} must be an absolute path.")
    if ".." in path.parts:
        raise ConfigurationError(f"{label} must not contain parent traversal.")
    if path == Path(path.anchor):
        raise ConfigurationError(f"{label} must not be a filesystem root.")
    if os.name == "nt":
        if path.drive.startswith("\\\\"):
            raise ConfigurationError(f"{label} must be on a local drive, not UNC.")
        _drive, tail = os.path.splitdrive(os.fspath(path))
        if ":" in tail:
            raise ConfigurationError(
                f"{label} must not use a Windows alternate data stream."
            )
        try:
            import ctypes

            get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
            get_drive_type.argtypes = [ctypes.c_wchar_p]
            get_drive_type.restype = ctypes.c_uint
            drive_type = get_drive_type(path.anchor)
        except (AttributeError, OSError) as exc:
            raise ConfigurationError(f"{label} drive type could not be inspected.") from exc
        if drive_type == 4:  # DRIVE_REMOTE, including mapped network drives.
            raise ConfigurationError(f"{label} must not use a mapped network drive.")
    return path


def _existing_path(raw: str | Path, *, label: str, directory: bool) -> Path:
    path = _absolute_path(raw, label=label)
    _assert_no_reparse_components(path, include_leaf=True)
    try:
        resolved = path.resolve(strict=True)
        metadata = os.stat(resolved)
    except (FileNotFoundError, OSError) as exc:
        raise ConfigurationError(f"{label} does not exist or cannot be inspected.") from exc
    expected = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if not expected:
        kind = "directory" if directory else "regular file"
        raise ConfigurationError(f"{label} must be an existing {kind}.")
    return resolved


def _canonical_key(path: Path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _is_within(path: Path, directory: Path) -> bool:
    try:
        return os.path.commonpath((_canonical_key(path), _canonical_key(directory))) == _canonical_key(directory)
    except ValueError:
        return False


def _hash_stable(path: Path) -> tuple[int, str]:
    digest = sha256()
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        current = os.stat(path)
    except OSError as exc:
        raise RehearsalError("artifact_hash_failed") from exc
    if (
        _stat_identity(before) != _stat_identity(after)
        or _stat_identity(after) != _stat_identity(current)
    ):
        raise RehearsalBlocked("artifact_changed_while_reading")
    return after.st_size, digest.hexdigest()


def _assert_no_sqlite_sidecars(
    database: Path,
    *,
    issue_code: str = "source_sqlite_sidecar_present",
) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        if os.path.lexists(Path(f"{database}{suffix}")):
            raise RehearsalBlocked(issue_code)


def _source_snapshot_checkpoint(
    database: Path,
    *,
    expected_sha256: str,
    expected_identity: tuple[int, int, int, int, int] | None,
    issue_prefix: str = "source",
) -> tuple[int, str, tuple[int, int, int, int, int]]:
    _assert_no_sqlite_sidecars(
        database,
        issue_code=f"{issue_prefix}_sqlite_sidecar_present",
    )
    try:
        before = os.lstat(database)
    except OSError as exc:
        raise RehearsalBlocked(f"{issue_prefix}_snapshot_unavailable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or _is_reparse_point(database)
        or before.st_nlink != 1
    ):
        raise RehearsalBlocked(f"{issue_prefix}_snapshot_identity_unsafe")
    before_identity = _stat_identity(before)
    if expected_identity is not None and before_identity != expected_identity:
        raise RehearsalBlocked(f"{issue_prefix}_snapshot_identity_changed")
    size, digest = _hash_stable(database)
    _assert_no_sqlite_sidecars(
        database,
        issue_code=f"{issue_prefix}_sqlite_sidecar_present",
    )
    try:
        after = os.lstat(database)
    except OSError as exc:
        raise RehearsalBlocked(f"{issue_prefix}_snapshot_unavailable") from exc
    after_identity = _stat_identity(after)
    if (
        not stat.S_ISREG(after.st_mode)
        or _is_reparse_point(database)
        or after.st_nlink != 1
        or after_identity != before_identity
        or (expected_identity is not None and after_identity != expected_identity)
        or digest != expected_sha256
    ):
        raise RehearsalBlocked(f"{issue_prefix}_snapshot_checkpoint_failed")
    return size, digest, after_identity


def _regular_file_checkpoint(
    path: Path,
    *,
    issue_prefix: str,
    expected_sha256: str | None = None,
    expected_identity: tuple[int, int, int, int, int] | None = None,
    maximum_size: int | None = None,
) -> tuple[int, str, tuple[int, int, int, int, int]]:
    if maximum_size is not None:
        _, size, digest, identity = _read_stable_regular_bytes(
            path,
            maximum_size=maximum_size,
            issue_prefix=issue_prefix,
            expected_sha256=expected_sha256,
            expected_identity=expected_identity,
        )
        return size, digest, identity
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise RehearsalBlocked(f"{issue_prefix}_unavailable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or _is_reparse_point(path)
        or before.st_nlink != 1
    ):
        raise RehearsalBlocked(f"{issue_prefix}_identity_unsafe")
    before_identity = _stat_identity(before)
    if expected_identity is not None and before_identity != expected_identity:
        raise RehearsalBlocked(f"{issue_prefix}_identity_changed")
    size, digest = _hash_stable(path)
    try:
        after = os.lstat(path)
    except OSError as exc:
        raise RehearsalBlocked(f"{issue_prefix}_unavailable") from exc
    after_identity = _stat_identity(after)
    if (
        not stat.S_ISREG(after.st_mode)
        or _is_reparse_point(path)
        or after.st_nlink != 1
        or after_identity != before_identity
        or (expected_identity is not None and after_identity != expected_identity)
        or (expected_sha256 is not None and digest != expected_sha256)
    ):
        raise RehearsalBlocked(f"{issue_prefix}_checkpoint_failed")
    return size, digest, after_identity


def _iter_execution_bundle_files(root: Path) -> list[tuple[str, Path]]:
    files: dict[str, Path] = {}
    for relative in EXECUTION_FIXED_FILES:
        path = root / Path(relative)
        try:
            metadata = os.lstat(path)
        except OSError as exc:
            raise RehearsalBlocked("execution_bundle_file_missing") from exc
        if not stat.S_ISREG(metadata.st_mode) or _is_reparse_point(path):
            raise RehearsalBlocked("execution_bundle_file_unsafe")
        files[relative] = path

    for directory_name in ("ffxivshare", "shares"):
        directory = root / directory_name
        pending = [directory]
        while pending:
            current = pending.pop()
            try:
                if _is_reparse_point(current):
                    raise RehearsalBlocked("execution_bundle_directory_unsafe")
                entries = sorted(os.scandir(current), key=lambda entry: entry.name)
            except OSError as exc:
                raise RehearsalBlocked("execution_bundle_directory_unreadable") from exc
            for entry in entries:
                path = Path(entry.path)
                if _is_reparse_point(path):
                    raise RehearsalBlocked("execution_bundle_path_unsafe")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False) and path.suffix == ".py":
                    relative = path.relative_to(root).as_posix()
                    files[relative] = path
    return sorted(files.items())


def _execution_bundle_sha256(root: Path) -> str:
    projection = []
    for relative, path in _iter_execution_bundle_files(root):
        size, digest = _hash_stable(path)
        projection.append({"path": relative, "size": size, "sha256": digest})
    return _canonical_json_sha256(projection)


def _execution_snapshot_sha256(
    root: Path,
    expected_files: tuple[str, ...],
) -> str:
    expected_file_set = set(expected_files)
    expected_directories: set[str] = set()
    for relative in expected_files:
        parent = Path(relative).parent
        while parent != Path("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent

    discovered_files: dict[str, Path] = {}
    discovered_directories: set[str] = set()
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError as exc:
            raise RehearsalBlocked("execution_snapshot_unreadable") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            if _is_reparse_point(path):
                raise RehearsalBlocked("execution_snapshot_path_unsafe")
            if entry.is_dir(follow_symlinks=False):
                discovered_directories.add(relative)
                pending.append(path)
            elif entry.is_file(follow_symlinks=False):
                discovered_files[relative] = path
            else:
                raise RehearsalBlocked("execution_snapshot_entry_unsupported")

    if (
        set(discovered_files) != expected_file_set
        or discovered_directories != expected_directories
    ):
        raise RehearsalBlocked("execution_snapshot_closure_changed")
    projection = []
    for relative in sorted(discovered_files):
        size, digest = _hash_stable(discovered_files[relative])
        projection.append({"path": relative, "size": size, "sha256": digest})
    return _canonical_json_sha256(projection)


def _create_execution_snapshot(
    source_root: Path,
    target_root: Path,
) -> tuple[str, tuple[str, ...]]:
    if any(target_root.iterdir()):
        raise RehearsalError("execution_snapshot_not_empty")
    source_files = _iter_execution_bundle_files(source_root)
    expected_files = tuple(relative for relative, _source in source_files)
    for relative, source in source_files:
        destination = target_root / Path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _copy_stable(source, destination)
        os.chmod(destination, stat.S_IREAD)
    return (
        _execution_snapshot_sha256(target_root, expected_files),
        expected_files,
    )


def _bootstrap_artifact(
    reference: Any,
    *,
    run_root: Path,
    label: str,
    expected_relative: str | None = None,
) -> Path:
    if (
        not isinstance(reference, dict)
        or set(reference) != {"path", "size", "sha256"}
        or not isinstance(reference["path"], str)
        or not isinstance(reference["size"], int)
        or isinstance(reference["size"], bool)
        or reference["size"] < 0
        or not isinstance(reference["sha256"], str)
        or SHA256_PATTERN.fullmatch(reference["sha256"]) is None
    ):
        raise ConfigurationError(f"{label} bootstrap reference is invalid.")
    relative = PurePosixPath(reference["path"])
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or "\\" in reference["path"]
        or relative.as_posix() != reference["path"]
        or (expected_relative is not None and reference["path"] != expected_relative)
    ):
        raise ConfigurationError(f"{label} bootstrap path is unsafe.")
    path = _existing_path(
        run_root / Path(*relative.parts),
        label=f"Frozen {label}",
        directory=False,
    )
    if not _is_within(path, run_root):
        raise ConfigurationError(f"Frozen {label} escaped RunRoot.")
    metadata = os.lstat(path)
    if metadata.st_nlink != 1 or _is_reparse_point(path):
        raise ConfigurationError(f"Frozen {label} identity is unsafe.")
    size, digest = _hash_stable(path)
    if size != reference["size"] or digest != reference["sha256"]:
        raise ConfigurationError(f"Frozen {label} does not match its reference.")
    return path


def _load_bootstrap_inner_context(run_root_raw: str | Path) -> BootstrapInnerContext:
    required_flags = {
        "ignore_environment": bool(sys.flags.ignore_environment),
        "no_user_site": bool(sys.flags.no_user_site),
        "dont_write_bytecode": bool(sys.flags.dont_write_bytecode),
        "utf8_mode": bool(sys.flags.utf8_mode),
    }
    if not all(required_flags.values()):
        raise ConfigurationError(
            "Rehearsal inner must run with python -E -s -B -X utf8."
        )
    run_root = _existing_path(
        run_root_raw,
        label="Bootstrap RunRoot",
        directory=True,
    )
    environment = {
        name: os.environ.get(name)
        for name in (
            "FFXIVSHARE_BOOTSTRAP_NONCE",
            "FFXIVSHARE_BOOTSTRAP_RECORD",
            "FFXIVSHARE_BOOTSTRAP_RUN_ID",
            "FFXIVSHARE_BOOTSTRAP_RUN_ROOT",
            "FFXIVSHARE_BOOTSTRAP_POLICY",
            "FFXIVSHARE_BOOTSTRAP_PROPOSAL",
            "FFXIVSHARE_BOOTSTRAP_REVIEW",
        )
    }
    if not all(isinstance(value, str) and value for value in environment.values()):
        raise ConfigurationError("Approved rehearsal requires complete bootstrap bindings.")
    if _canonical_key(Path(environment["FFXIVSHARE_BOOTSTRAP_RUN_ROOT"])) != _canonical_key(
        run_root
    ):
        raise ConfigurationError("Bootstrap RunRoot binding does not match the CLI.")
    record_path = _existing_path(
        environment["FFXIVSHARE_BOOTSTRAP_RECORD"],
        label="Bootstrap record",
        directory=False,
    )
    expected_record = run_root / "evidence" / "bootstrap.json"
    if _canonical_key(record_path) != _canonical_key(expected_record):
        raise ConfigurationError("Bootstrap record is not in its fixed evidence location.")
    record = _load_json(record_path)
    expected_record_keys = {
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
    if (
        not isinstance(record, dict)
        or set(record) != expected_record_keys
        or record["format"] != "ffxivshare-production-copy-bootstrap"
        or record["format_version"] != 1
        or record["run_id"] != environment["FFXIVSHARE_BOOTSTRAP_RUN_ID"]
        or record["bootstrap_nonce"] != environment["FFXIVSHARE_BOOTSTRAP_NONCE"]
        or record["bootstrap_trusted_not_frozen"] is not False
        or record["source_data_read_by_bootstrap"] is not False
        or record["media_read_by_bootstrap"] is not False
        or not isinstance(record["workspace_access_control"], str)
        or not record["workspace_access_control"]
    ):
        raise ConfigurationError("Approved-rehearsal bootstrap identity is invalid.")
    if (
        not isinstance(record["run_id"], str)
        or POLICY_ID_PATTERN.fullmatch(record["run_id"]) is None
        or not isinstance(record["bootstrap_nonce"], str)
        or SHA256_PATTERN.fullmatch(record["bootstrap_nonce"]) is None
        or not isinstance(record["generated_at"], str)
        or UTC_TIMESTAMP_PATTERN.fullmatch(record["generated_at"]) is None
    ):
        raise ConfigurationError("Bootstrap identifiers are invalid.")
    configuration = record["configuration"]
    if (
        not isinstance(configuration, dict)
        or set(configuration)
        != {"inner_arguments", "inner_entrypoint", "mode", "repository_root", "run_root"}
        or configuration["mode"] != "approved-rehearsal"
        or configuration["inner_entrypoint"]
        != "ops/migration/Rehearse-ProductionCopy.py"
        or configuration["inner_arguments"] != sys.argv[1:]
        or not isinstance(configuration["run_root"], str)
        or not isinstance(configuration["repository_root"], str)
        or _canonical_key(Path(configuration["run_root"])) != _canonical_key(run_root)
    ):
        raise ConfigurationError("Bootstrap rehearsal launch configuration is invalid.")
    repository_root = _absolute_path(
        configuration["repository_root"],
        label="Bootstrap repository root",
    )
    code_root = _existing_path(run_root / "code", label="Frozen code", directory=True)
    loaded = _existing_path(Path(__file__), label="Loaded rehearsal", directory=False)
    expected_loaded = code_root / "ops" / "migration" / "Rehearse-ProductionCopy.py"
    if (
        _canonical_key(loaded) != _canonical_key(expected_loaded)
        or _canonical_key(Path.cwd()) != _canonical_key(code_root)
    ):
        raise ConfigurationError("Rehearsal is not executing from its frozen code root.")
    python_identity = record["python"]
    if not isinstance(python_identity, dict):
        raise ConfigurationError("Bootstrap Python identity is invalid.")
    python_path = _existing_path(
        python_identity.get("executable", ""),
        label="Bootstrap Python executable",
        directory=False,
    )
    python_size, python_digest = _hash_stable(python_path)
    if (
        _canonical_key(python_path) != _canonical_key(Path(sys.executable))
        or python_identity.get("executable_size") != python_size
        or python_identity.get("executable_sha256") != python_digest
    ):
        raise ConfigurationError("Bootstrap Python identity changed before rehearsal.")
    execution = record["execution_bundle"]
    if (
        not isinstance(execution, dict)
        or set(execution)
        != {"authority", "expected_sha256", "frozen_sha256", "manifest"}
        or execution["authority"] != "external_digest"
        or execution["expected_sha256"] != execution["frozen_sha256"]
        or not isinstance(execution["frozen_sha256"], str)
        or SHA256_PATTERN.fullmatch(execution["frozen_sha256"]) is None
    ):
        raise ConfigurationError("Bootstrap execution-bundle authority is invalid.")
    manifest_path = _bootstrap_artifact(
        execution["manifest"],
        run_root=run_root,
        label="execution bundle manifest",
        expected_relative="evidence/execution-bundle.json",
    )
    manifest = _load_json(manifest_path, maximum_size=32 * 1024 * 1024)
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"execution_bundle_sha256", "files", "format", "format_version"}
        or manifest["format"] != "ffxivshare-production-copy-execution-bundle"
        or manifest["format_version"] != 1
        or manifest["execution_bundle_sha256"] != execution["frozen_sha256"]
        or not isinstance(manifest["files"], list)
        or not manifest["files"]
    ):
        raise ConfigurationError("Frozen execution-bundle manifest is invalid.")
    expected_files: list[str] = []
    for item in manifest["files"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "size", "sha256"}
            or not isinstance(item["path"], str)
            or not isinstance(item["size"], int)
            or isinstance(item["size"], bool)
            or item["size"] < 0
            or not isinstance(item["sha256"], str)
            or SHA256_PATTERN.fullmatch(item["sha256"]) is None
        ):
            raise ConfigurationError("Frozen execution-bundle entry is invalid.")
        relative = PurePosixPath(item["path"])
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "\\" in item["path"]
            or relative.as_posix() != item["path"]
        ):
            raise ConfigurationError("Frozen execution-bundle path is unsafe.")
        path = _existing_path(
            code_root / Path(*relative.parts),
            label="Frozen execution-bundle file",
            directory=False,
        )
        metadata = os.lstat(path)
        size, digest = _hash_stable(path)
        if metadata.st_nlink != 1 or size != item["size"] or digest != item["sha256"]:
            raise ConfigurationError("Frozen execution-bundle file changed.")
        expected_files.append(item["path"])
    if (
        expected_files != sorted(set(expected_files))
        or _canonical_json_sha256(manifest["files"]) != execution["frozen_sha256"]
        or _execution_snapshot_sha256(code_root, tuple(expected_files))
        != execution["frozen_sha256"]
    ):
        raise ConfigurationError("Frozen execution-bundle closure is invalid.")
    policy_record = record["policy"]
    approval_inputs = record["approval_inputs"]
    if (
        not isinstance(policy_record, dict)
        or set(policy_record) != {"source", "frozen"}
        or not isinstance(approval_inputs, dict)
        or set(approval_inputs) != {"proposal", "review"}
    ):
        raise ConfigurationError("Bootstrap approval inputs are incomplete.")
    policy_path = _bootstrap_artifact(
        policy_record["frozen"],
        run_root=run_root,
        label="approved policy",
        expected_relative="evidence/approved-policy.json",
    )
    proposal_binding = approval_inputs["proposal"]
    review_binding = approval_inputs["review"]
    if (
        not isinstance(proposal_binding, dict)
        or set(proposal_binding) != {"source", "frozen"}
        or not isinstance(review_binding, dict)
        or set(review_binding) != {"source", "frozen"}
    ):
        raise ConfigurationError("Bootstrap approval input bindings are invalid.")
    proposal_path = _bootstrap_artifact(
        proposal_binding["frozen"],
        run_root=run_root,
        label="approved proposal",
        expected_relative="evidence/approved-proposal.json",
    )
    review_path = _bootstrap_artifact(
        review_binding["frozen"],
        run_root=run_root,
        label="approved review",
        expected_relative="evidence/approved-review.json",
    )
    for name, expected in (
        ("FFXIVSHARE_BOOTSTRAP_POLICY", policy_path),
        ("FFXIVSHARE_BOOTSTRAP_PROPOSAL", proposal_path),
        ("FFXIVSHARE_BOOTSTRAP_REVIEW", review_path),
    ):
        if _canonical_key(Path(environment[name])) != _canonical_key(expected):
            raise ConfigurationError(f"{name} does not match its frozen artifact.")
    run_layout = record["run_layout"]
    if not isinstance(run_layout, list) or not run_layout:
        raise ConfigurationError("Bootstrap RunRoot layout is invalid.")
    seen_layout: set[str] = set()
    expected_layout = {
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
    for entry in run_layout:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"device", "inode", "kind", "path"}
            or entry["kind"] not in {"directory", "file"}
            or not isinstance(entry["device"], int)
            or not isinstance(entry["inode"], int)
            or not isinstance(entry["path"], str)
            or entry["path"] in seen_layout
        ):
            raise ConfigurationError("Bootstrap RunRoot layout entry is invalid.")
        seen_layout.add(entry["path"])
        relative = PurePosixPath(entry["path"])
        if (
            entry["path"] != "."
            and (
                relative.is_absolute()
                or ".." in relative.parts
                or "\\" in entry["path"]
                or relative.as_posix() != entry["path"]
            )
        ):
            raise ConfigurationError("Bootstrap RunRoot layout path is unsafe.")
        candidate = (
            run_root
            if entry["path"] == "."
            else run_root / Path(*relative.parts)
        )
        checked = _existing_path(
            candidate,
            label="Bootstrap RunRoot layout entry",
            directory=entry["kind"] == "directory",
        )
        metadata = os.stat(checked)
        if (metadata.st_dev, metadata.st_ino) != (entry["device"], entry["inode"]):
            raise ConfigurationError("Bootstrap RunRoot layout identity changed.")
    if seen_layout != expected_layout:
        raise ConfigurationError("Bootstrap RunRoot layout is incomplete.")
    record_size, record_digest = _hash_stable(record_path)
    if record_size <= 0:
        raise ConfigurationError("Bootstrap record is empty.")
    return BootstrapInnerContext(
        run_root=run_root,
        record_path=record_path,
        record_sha256=record_digest,
        workspace_access_control=record["workspace_access_control"],
        execution_bundle_sha256=execution["frozen_sha256"],
        execution_bundle_files=tuple(expected_files),
        policy_path=policy_path,
        proposal_path=proposal_path,
        review_path=review_path,
        repository_root=repository_root,
    )


def _source_media_manifest_identity(path: Path) -> tuple[str, str]:
    _size, digest = _hash_stable(path)
    manifest = _load_json(path, maximum_size=32 * 1024 * 1024)
    source_snapshot = manifest.get("source_snapshot") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != "ffxivshare-media-manifest"
        or manifest.get("format_version") != 2
        or manifest.get("hash_algorithm") != "sha256"
        or manifest.get("path_normalization")
        != "unicode_nfc_canonical_caseless_unique"
        or not isinstance(source_snapshot, dict)
        or set(source_snapshot) != {"id", "offline_confirmed"}
        or source_snapshot.get("offline_confirmed") is not True
        or not isinstance(source_snapshot.get("id"), str)
        or SNAPSHOT_ID_PATTERN.fullmatch(source_snapshot["id"]) is None
    ):
        raise RehearsalBlocked("source_media_manifest_identity_invalid")
    return digest, source_snapshot["id"]


def _assert_external_handoff_artifact_checkpoint(
    handoff: dict[str, Any] | None,
    artifact_name: str,
    size: int,
    digest: str,
) -> None:
    if handoff is None:
        return
    try:
        artifact = handoff["database_backup_set"][artifact_name]
        matched = artifact["size"] == size and artifact["sha256"] == digest
    except (KeyError, TypeError):
        matched = False
    if not matched:
        raise RehearsalBlocked(
            f"external_handoff_source_{artifact_name}_mismatch"
        )


def _copy_stable(source: Path, destination: Path) -> tuple[int, str]:
    if os.path.lexists(destination):
        raise RehearsalError("copy_destination_exists")
    digest = sha256()
    try:
        with source.open("rb") as input_stream:
            before = os.fstat(input_stream.fileno())
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as output_stream:
                for chunk in iter(lambda: input_stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                    output_stream.write(chunk)
                output_stream.flush()
                os.fsync(output_stream.fileno())
            after = os.fstat(input_stream.fileno())
        current = os.stat(source)
    except Exception:
        if os.path.lexists(destination):
            try:
                destination.unlink()
            except OSError:
                pass
        raise
    if (
        _stat_identity(before) != _stat_identity(after)
        or _stat_identity(after) != _stat_identity(current)
    ):
        destination.unlink(missing_ok=True)
        raise RehearsalBlocked("source_changed_while_copying")
    copied_size, copied_digest = _hash_stable(destination)
    if copied_size != after.st_size or copied_digest != digest.hexdigest():
        raise RehearsalError("work_copy_verification_failed")
    return copied_size, copied_digest


def _write_json_create_new(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{os.urandom(8).hex()}")
    published = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical_json_bytes(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        published = True
        temporary.unlink()
    except Exception:
        if published and os.path.lexists(path):
            try:
                path.unlink()
            except OSError:
                pass
        raise
    finally:
        if os.path.lexists(temporary):
            try:
                temporary.unlink()
            except OSError:
                pass


class EvidenceLedger:
    def __init__(self, path: Path):
        self.path = path
        self.descriptor = os.open(
            path,
            os.O_RDWR
            | os.O_APPEND
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0),
            0o600,
        )
        metadata = os.fstat(self.descriptor)
        self.identity = (metadata.st_dev, metadata.st_ino)
        self.sequence = 0
        self.head_sha256 = "0" * 64
        self.completed_stages: list[str] = []

    def _assert_bound_path(self) -> None:
        try:
            descriptor_metadata = os.fstat(self.descriptor)
            path_metadata = os.lstat(self.path)
        except OSError as exc:
            raise RehearsalError("evidence_ledger_binding_changed") from exc
        if (
            not stat.S_ISREG(descriptor_metadata.st_mode)
            or not stat.S_ISREG(path_metadata.st_mode)
            or descriptor_metadata.st_nlink != 1
            or path_metadata.st_nlink != 1
            or _is_reparse_point(self.path)
            or (descriptor_metadata.st_dev, descriptor_metadata.st_ino) != self.identity
            or (path_metadata.st_dev, path_metadata.st_ino) != self.identity
        ):
            raise RehearsalError("evidence_ledger_binding_changed")

    def _write_all(self, payload: bytes) -> None:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(self.descriptor, remaining)
            if written <= 0:
                raise RehearsalError("evidence_ledger_write_failed")
            remaining = remaining[written:]

    def record(
        self,
        stage: str,
        outcome: str,
        details: dict[str, Any] | None = None,
    ) -> str:
        event = {
            "format": LEDGER_FORMAT,
            "format_version": LEDGER_VERSION,
            "sequence": self.sequence + 1,
            "generated_at": _utc_now(),
            "stage": stage,
            "outcome": outcome,
            "previous_event_sha256": self.head_sha256,
            "details": details or {},
        }
        event_sha256 = _canonical_json_sha256(event)
        event["event_sha256"] = event_sha256
        encoded = _canonical_json_bytes(event)
        self._assert_bound_path()
        self._write_all(encoded)
        os.fsync(self.descriptor)
        self._assert_bound_path()
        self.sequence += 1
        self.head_sha256 = event_sha256
        if outcome == "passed":
            self.completed_stages.append(stage)
        return event_sha256

    def verify_replay(self, *, expected_terminal: str) -> dict[str, Any]:
        self._assert_bound_path()
        os.lseek(self.descriptor, 0, os.SEEK_SET)
        chunks = []
        while True:
            chunk = os.read(self.descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
        if not raw.endswith(b"\n"):
            raise RehearsalError("evidence_ledger_replay_invalid")
        previous = "0" * 64
        completed: list[str] = []
        expected_event_keys = {
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
        events = []
        for sequence, raw_line in enumerate(raw.splitlines(), start=1):
            try:
                event = json.loads(
                    raw_line.decode("utf-8"),
                    object_pairs_hook=_strict_object,
                    parse_constant=_reject_json_constant,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise RehearsalError("evidence_ledger_replay_invalid") from exc
            if (
                not isinstance(event, dict)
                or set(event) != expected_event_keys
                or event["format"] != LEDGER_FORMAT
                or event["format_version"] != LEDGER_VERSION
                or event["sequence"] != sequence
                or event["previous_event_sha256"] != previous
                or not isinstance(event["event_sha256"], str)
                or SHA256_PATTERN.fullmatch(event["event_sha256"]) is None
                or _canonical_json_bytes(event) != raw_line + b"\n"
            ):
                raise RehearsalError("evidence_ledger_replay_invalid")
            declared_digest = event.pop("event_sha256")
            if _canonical_json_sha256(event) != declared_digest:
                raise RehearsalError("evidence_ledger_replay_invalid")
            previous = declared_digest
            if event["outcome"] == "passed":
                completed.append(event["stage"])
            events.append(event)
        if (
            len(events) != self.sequence
            or previous != self.head_sha256
            or completed != self.completed_stages
            or not events
            or events[-1]["stage"] != expected_terminal
            or events[-1]["outcome"] != "terminal"
        ):
            raise RehearsalError("evidence_ledger_replay_invalid")
        self._assert_bound_path()
        return {
            "event_count": len(events),
            "head_event_sha256": previous,
            "ledger": {
                "path": self.path.name,
                "size": len(raw),
                "sha256": sha256(raw).hexdigest(),
            },
        }

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1


class SubprocessRunner:
    def run(
        self,
        *,
        stage: str,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        stdout_path: Path,
        stderr_path: Path,
    ) -> CommandResult:
        del stage
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                check=False,
            )
            stdout.flush()
            stderr.flush()
            os.fsync(stdout.fileno())
            os.fsync(stderr.fileno())
        return CommandResult(completed.returncode, stdout_path, stderr_path)


def _artifact_reference(path: Path, run_root: Path) -> dict[str, Any]:
    size, digest = _hash_stable(path)
    try:
        relative = path.relative_to(run_root).as_posix()
    except ValueError as exc:
        raise RehearsalError("artifact_outside_run_root") from exc
    return {"path": relative, "size": size, "sha256": digest}


def _backup_set_artifact_references(
    database: Path,
    run_root: Path,
) -> dict[str, dict[str, Any]]:
    _assert_no_sqlite_sidecars(
        database,
        issue_code="target_backup_sqlite_sidecar_present",
    )
    paths = {
        "database": database,
        "checksum": database.with_name(f"{database.name}.sha256"),
        "metadata": database.with_name(f"{database.name}.metadata.json"),
    }
    references: dict[str, dict[str, Any]] = {}
    for role, path in paths.items():
        try:
            metadata = os.lstat(path)
        except OSError as exc:
            raise RehearsalBlocked(f"target_backup_{role}_unavailable") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or _is_reparse_point(path)
            or metadata.st_nlink != 1
        ):
            raise RehearsalBlocked(f"target_backup_{role}_identity_unsafe")
        references[role] = _artifact_reference(path, run_root)
    return references


def _validate_migration_nodes(value: Any, *, label: str) -> list[list[str]]:
    if not isinstance(value, list):
        raise RehearsalBlocked(f"policy_{label}_invalid")
    nodes: list[list[str]] = []
    for node in value:
        if (
            not isinstance(node, list)
            or len(node) != 2
            or not all(isinstance(part, str) for part in node)
            or not all(MIGRATION_PART_PATTERN.fullmatch(part) for part in node)
        ):
            raise RehearsalBlocked(f"policy_{label}_invalid")
        nodes.append([node[0], node[1]])
    canonical = sorted({(node[0], node[1]) for node in nodes})
    if nodes != [list(node) for node in canonical] or not nodes:
        raise RehearsalBlocked(f"policy_{label}_not_canonical")
    return nodes


def _load_policy(path: Path) -> dict[str, Any]:
    value = _load_json(path)
    expected_keys = {
        "approved",
        "approved_at",
        "approval_tool_sha256",
        "execution_bundle_sha256",
        "format",
        "format_version",
        "lossless_reviewed",
        "migration_plan_sha256",
        "migration_runtime_sha256",
        "policy_id",
        "proposal_body_sha256",
        "proposal_bootstrap_completion_sha256",
        "proposal_bootstrap_nonce",
        "proposal_evidence_set_sha256",
        "proposal_id",
        "proposal_ledger_event_count",
        "proposal_ledger_head_sha256",
        "proposal_run_id",
        "proposal_sha256",
        "review_id",
        "review_record_sha256",
        "reviewed_at",
        "reviewer",
        "reviewer_identity_verification",
        "runtime_fingerprint_sha256",
        "source_applied_migrations_sha256",
        "source_database_sha256",
        "source_leaf_nodes",
        "source_media_manifest_sha256",
        "source_media_snapshot_id",
        "source_sqlite_schema_sha256",
        "target_leaf_nodes",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise RehearsalBlocked("policy_shape_invalid")
    if value["format"] != POLICY_FORMAT or value["format_version"] != POLICY_VERSION:
        raise RehearsalBlocked("policy_format_unsupported")
    if (
        not isinstance(value["policy_id"], str)
        or POLICY_ID_PATTERN.fullmatch(value["policy_id"]) is None
    ):
        raise RehearsalBlocked("policy_id_invalid")
    if value["approved"] is not True:
        raise RehearsalBlocked("policy_not_approved")
    if value["lossless_reviewed"] is not True:
        raise RehearsalBlocked("policy_not_reviewed_for_losslessness")
    for name in (
        "approval_tool_sha256",
        "execution_bundle_sha256",
        "migration_plan_sha256",
        "migration_runtime_sha256",
        "proposal_body_sha256",
        "proposal_bootstrap_completion_sha256",
        "proposal_bootstrap_nonce",
        "proposal_evidence_set_sha256",
        "proposal_ledger_head_sha256",
        "proposal_sha256",
        "review_record_sha256",
        "runtime_fingerprint_sha256",
        "source_applied_migrations_sha256",
        "source_database_sha256",
        "source_media_manifest_sha256",
        "source_sqlite_schema_sha256",
    ):
        if not isinstance(value[name], str) or SHA256_PATTERN.fullmatch(value[name]) is None:
            raise RehearsalBlocked(f"policy_{name}_invalid")
    for name in (
        "policy_id",
        "proposal_id",
        "proposal_run_id",
        "review_id",
        "reviewer",
    ):
        if (
            not isinstance(value[name], str)
            or POLICY_ID_PATTERN.fullmatch(value[name]) is None
        ):
            raise RehearsalBlocked(f"policy_{name}_invalid")
    for name in ("approved_at", "reviewed_at"):
        if (
            not isinstance(value[name], str)
            or UTC_TIMESTAMP_PATTERN.fullmatch(value[name]) is None
        ):
            raise RehearsalBlocked(f"policy_{name}_invalid")
    if (
        value["reviewer_identity_verification"]
        != "operator_asserted_not_cryptographically_verified"
    ):
        raise RehearsalBlocked("policy_reviewer_identity_verification_invalid")
    if (
        not isinstance(value["proposal_ledger_event_count"], int)
        or isinstance(value["proposal_ledger_event_count"], bool)
        or value["proposal_ledger_event_count"] <= 0
    ):
        raise RehearsalBlocked("policy_proposal_ledger_event_count_invalid")
    if (
        not isinstance(value["source_media_snapshot_id"], str)
        or SNAPSHOT_ID_PATTERN.fullmatch(value["source_media_snapshot_id"]) is None
    ):
        raise RehearsalBlocked("policy_source_media_snapshot_id_invalid")
    value["source_leaf_nodes"] = _validate_migration_nodes(
        value["source_leaf_nodes"], label="source_leaf_nodes"
    )
    value["target_leaf_nodes"] = _validate_migration_nodes(
        value["target_leaf_nodes"], label="target_leaf_nodes"
    )
    return value


def _validate_backup_report(path: Path) -> dict[str, Any]:
    report = _load_json(path)
    artifact = report.get("artifact") if isinstance(report, dict) else None
    if (
        not isinstance(report, dict)
        or report.get("format") != "ffxivshare-sqlite-backup-set-verification"
        or report.get("format_version") != 1
        or report.get("verified") is not True
        or report.get("cutover_authorized") is not False
        or report.get("inspection_required") is not True
        or not isinstance(artifact, dict)
        or not isinstance(artifact.get("sha256"), str)
        or SHA256_PATTERN.fullmatch(artifact["sha256"]) is None
        or not isinstance(artifact.get("size"), int)
        or isinstance(artifact.get("size"), bool)
        or artifact["size"] < 0
    ):
        raise RehearsalError("backup_verification_report_invalid")
    return report


def _sqlite_schema_inventory_projection(inventory: dict[str, Any]) -> dict[str, Any]:
    return {
        key: inventory[key]
        for key in (
            "format",
            "format_version",
            "schema",
            "included_object_types",
            "excluded_objects",
            "normalization",
            "object_count",
            "objects",
        )
    }


def _sqlite_schema_inventory_sha256(inventory: dict[str, Any]) -> str:
    serialized = json.dumps(
        _sqlite_schema_inventory_projection(inventory),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(serialized.encode("utf-8")).hexdigest()


def _sqlite_schema_object_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    sql = item["sql"]
    return (
        item["type"],
        item["name"],
        item["tbl_name"],
        sql is not None,
        "" if sql is None else sql,
    )


def _validate_sqlite_schema_inventory(value: Any) -> str:
    if not isinstance(value, dict) or set(value) != SQLITE_SCHEMA_INVENTORY_KEYS:
        raise RehearsalError("snapshot_sqlite_schema_inventory_invalid")
    if (
        value["format"] != "ffxivshare-sqlite-schema-inventory"
        or value["format_version"] != 1
        or value["schema"] != "main"
        or value["included_object_types"] != list(SQLITE_SCHEMA_OBJECT_TYPES)
        or value["excluded_objects"]
        != {
            "name_prefix": SQLITE_SCHEMA_INTERNAL_PREFIX,
            "comparison": "case-sensitive Unicode code-point prefix match",
            "reason": "SQLite-reserved internal and automatically generated objects",
        }
        or value["normalization"]
        != {
            "object_order": ["type", "name", "tbl_name", "sql (NULL first)"],
            "string_order": "Unicode code-point order",
            "sql": "verbatim sqlite_schema.sql with NULL preserved",
            "digest": "SHA-256 of canonical UTF-8 JSON excluding sha256",
            "canonical_json": "sorted object keys; no insignificant whitespace",
        }
    ):
        raise RehearsalError("snapshot_sqlite_schema_metadata_invalid")

    objects = value["objects"]
    object_count = value["object_count"]
    if (
        not isinstance(objects, list)
        or not isinstance(object_count, int)
        or isinstance(object_count, bool)
        or object_count != len(objects)
    ):
        raise RehearsalError("snapshot_sqlite_schema_count_invalid")

    seen: set[tuple[str, str, str]] = set()
    for item in objects:
        if not isinstance(item, dict) or set(item) != SQLITE_SCHEMA_OBJECT_KEYS:
            raise RehearsalError("snapshot_sqlite_schema_object_invalid")
        object_type = item["type"]
        name = item["name"]
        table_name = item["tbl_name"]
        sql = item["sql"]
        if (
            object_type not in SQLITE_SCHEMA_OBJECT_TYPES
            or not isinstance(name, str)
            or not name
            or name.startswith(SQLITE_SCHEMA_INTERNAL_PREFIX)
            or not isinstance(table_name, str)
            or not table_name
            or not isinstance(sql, str)
            or not sql
        ):
            raise RehearsalError("snapshot_sqlite_schema_object_value_invalid")
        identity = (object_type, name, table_name)
        if identity in seen:
            raise RehearsalError("snapshot_sqlite_schema_duplicate_object")
        seen.add(identity)
    if objects != sorted(objects, key=_sqlite_schema_object_sort_key):
        raise RehearsalError("snapshot_sqlite_schema_not_canonical")

    digest = value["sha256"]
    if (
        not isinstance(digest, str)
        or SHA256_PATTERN.fullmatch(digest) is None
        or digest != _sqlite_schema_inventory_sha256(value)
    ):
        raise RehearsalError("snapshot_sqlite_schema_sha256_invalid")
    return digest


def _validate_inspection_report(
    path: Path,
    *,
    expected_sha256: str,
) -> tuple[dict[str, Any], list[list[str]], str]:
    report = _load_json(path, maximum_size=32 * 1024 * 1024)
    database = report.get("database") if isinstance(report, dict) else None
    inspection = report.get("inspection") if isinstance(report, dict) else None
    migrations = inspection.get("django_migrations") if isinstance(inspection, dict) else None
    foreign_keys = inspection.get("foreign_key_check") if isinstance(inspection, dict) else None
    sqlite_schema = inspection.get("sqlite_schema") if isinstance(inspection, dict) else None
    if (
        not isinstance(report, dict)
        or report.get("format") != "ffxivshare-sqlite-snapshot-inspection"
        or report.get("format_version") != 1
        or not isinstance(database, dict)
        or database.get("sha256") != expected_sha256
        or database.get("sha256_before") != expected_sha256
        or database.get("sha256_after") != expected_sha256
        or database.get("source_unchanged") is not True
        or not isinstance(inspection, dict)
        or inspection.get("query_only") is not True
        or inspection.get("integrity_check") != "ok"
        or not isinstance(foreign_keys, dict)
        or foreign_keys.get("status") != "ok"
        or foreign_keys.get("violations") != 0
        or not isinstance(migrations, dict)
        or migrations.get("present") is not True
        or not isinstance(migrations.get("applied"), list)
    ):
        raise RehearsalError("snapshot_inspection_report_invalid")
    applied: list[list[str]] = []
    for item in migrations["applied"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("app"), str)
            or not isinstance(item.get("name"), str)
            or MIGRATION_PART_PATTERN.fullmatch(item["app"]) is None
            or MIGRATION_PART_PATTERN.fullmatch(item["name"]) is None
        ):
            raise RehearsalError("snapshot_migration_projection_invalid")
        applied.append([item["app"], item["name"]])
    canonical = [list(node) for node in sorted({tuple(node) for node in applied})]
    if canonical != sorted(applied) or len(canonical) != len(applied):
        raise RehearsalError("snapshot_migration_projection_not_canonical")
    schema_sha256 = _validate_sqlite_schema_inventory(sqlite_schema)
    return report, canonical, schema_sha256


def _validate_migration_state(path: Path) -> dict[str, Any]:
    state = _load_json(path)
    expected_keys = {
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
        not isinstance(state, dict)
        or set(state) != expected_keys
        or state["format"] != "ffxivshare-migration-state"
        or state["format_version"] != 1
        or state["database_vendor"] != "sqlite"
        or state["unknown_applied_nodes"] != []
        or not isinstance(state["python_version"], str)
        or not state["python_version"]
        or not isinstance(state["django_version"], str)
        or not state["django_version"]
        or not isinstance(state["migration_runtime_sha256"], str)
        or SHA256_PATTERN.fullmatch(state["migration_runtime_sha256"]) is None
    ):
        raise RehearsalError("migration_state_invalid")
    for key in ("applied", "applied_leaf_nodes", "repository_leaf_nodes"):
        state[key] = _validate_migration_nodes(state[key], label=key)
    return state


def _runtime_checkpoint_label_parts(
    label: object,
    *,
    directory_entry: bool = False,
) -> tuple[str, tuple[str, ...]]:
    if (
        not isinstance(label, str)
        or not label
        or "\\" in label
        or "\x00" in label
        or label.endswith("/") != directory_entry
    ):
        raise RehearsalError("runtime_fingerprint_checkpoint_invalid")
    core = label[:-1] if directory_entry else label
    for marker in ("$EXECUTION_ROOT", "$PREFIX", "$BASE_PREFIX"):
        if core == marker:
            relative = ""
        elif core.startswith(marker + "/"):
            relative = core[len(marker) + 1 :]
        else:
            continue
        parts = tuple(relative.split("/")) if relative else ()
        if (
            any(part in {"", ".", ".."} for part in parts)
            or core != marker + ("/" + "/".join(parts) if parts else "")
            or (directory_entry and not parts)
        ):
            raise RehearsalError("runtime_fingerprint_checkpoint_invalid")
        return marker, parts
    raise RehearsalError("runtime_fingerprint_checkpoint_invalid")


def _validate_runtime_fingerprint_bytes(raw: bytes) -> dict[str, Any]:
    try:
        report = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RehearsalError("runtime_fingerprint_report_invalid") from exc
    if (
        not isinstance(report, dict)
        or set(report) != {
            "format",
            "format_version",
            "fingerprint_sha256",
            "projection",
            "checkpoint",
        }
        or report["format"] != "ffxivshare-runtime-fingerprint"
        or report["format_version"] != 1
        or not isinstance(report["fingerprint_sha256"], str)
        or SHA256_PATTERN.fullmatch(report["fingerprint_sha256"]) is None
        or not isinstance(report["projection"], dict)
        or _canonical_json_sha256(report["projection"])
        != report["fingerprint_sha256"]
    ):
        raise RehearsalError("runtime_fingerprint_report_invalid")
    checkpoint = report["checkpoint"]
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
        raise RehearsalError("runtime_fingerprint_checkpoint_invalid")
    inventory_paths = []
    for item in checkpoint["identity_inventory"]:
        if (
            not isinstance(item, dict)
            or set(item)
            != {"ctime_ns", "device", "inode", "mtime_ns", "path", "size"}
            or not isinstance(item["path"], str)
            or not all(
                isinstance(item[name], int) and not isinstance(item[name], bool)
                for name in ("ctime_ns", "device", "inode", "mtime_ns", "size")
            )
            or item["size"] < 0
        ):
            raise RehearsalError("runtime_fingerprint_checkpoint_invalid")
        _runtime_checkpoint_label_parts(item["path"])
        inventory_paths.append(item["path"])
    if inventory_paths != sorted(set(inventory_paths)):
        raise RehearsalError("runtime_fingerprint_checkpoint_invalid")
    scope_keys = []
    scope_roots = set()
    closure_entries = set()
    closure_file_paths = set()
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
            or not isinstance(scope["files"], list)
            or not all(isinstance(item, str) for item in scope["files"])
            or scope["files"] != sorted(set(scope["files"]))
        ):
            raise RehearsalError("runtime_fingerprint_checkpoint_invalid")
        root_parts = _runtime_checkpoint_label_parts(scope["root"])
        if scope["root"] in scope_roots:
            raise RehearsalError("runtime_fingerprint_checkpoint_invalid")
        scope_roots.add(scope["root"])
        for entry in scope["files"]:
            directory_entry = entry.endswith("/")
            entry_parts = _runtime_checkpoint_label_parts(
                entry,
                directory_entry=directory_entry,
            )
            top_level = scope["mode"] == "top_level_entries"
            if (
                entry_parts[0] != root_parts[0]
                or entry_parts[1][: len(root_parts[1])] != root_parts[1]
                or (
                    len(entry_parts[1]) != len(root_parts[1]) + 1
                    if top_level
                    else len(entry_parts[1]) <= len(root_parts[1])
                )
                or entry in closure_entries
            ):
                raise RehearsalError("runtime_fingerprint_checkpoint_invalid")
            closure_entries.add(entry)
            if not directory_entry:
                closure_file_paths.add(entry)
        scope_keys.append((scope["root"], scope["mode"]))
    if scope_keys != sorted(set(scope_keys)):
        raise RehearsalError("runtime_fingerprint_checkpoint_invalid")
    if not closure_file_paths.issubset(set(inventory_paths)):
        raise RehearsalError("runtime_fingerprint_checkpoint_invalid")
    return report


def _validate_runtime_fingerprint(path: Path) -> dict[str, Any]:
    raw, _size, _digest, _identity = _read_stable_regular_bytes(
        path,
        maximum_size=64 * 1024 * 1024,
        issue_prefix="runtime_fingerprint_report",
    )
    return _validate_runtime_fingerprint_bytes(raw)


def _validate_runtime_identity_checkpoint(
    path: Path,
    *,
    expected_fingerprint_sha256: str,
    expected_source_report_sha256: str,
) -> dict[str, Any]:
    report = _load_json(path)
    if (
        not isinstance(report, dict)
        or set(report)
        != {
            "closure_scopes_checked",
            "content_rehashed",
            "fingerprint_sha256",
            "format",
            "format_version",
            "identity_inventory_checked",
            "source_report_sha256",
            "unchanged",
        }
        or report["format"] != "ffxivshare-runtime-identity-checkpoint"
        or report["format_version"] != 1
        or report["fingerprint_sha256"] != expected_fingerprint_sha256
        or report["source_report_sha256"] != expected_source_report_sha256
        or report["content_rehashed"] is not False
        or report["unchanged"] is not True
        or not isinstance(report["identity_inventory_checked"], int)
        or isinstance(report["identity_inventory_checked"], bool)
        or report["identity_inventory_checked"] <= 0
        or not isinstance(report["closure_scopes_checked"], int)
        or isinstance(report["closure_scopes_checked"], bool)
        or report["closure_scopes_checked"] <= 0
    ):
        raise RehearsalError("runtime_identity_checkpoint_invalid")
    return report


def _validate_validation_report(path: Path) -> None:
    report = _load_json(path, maximum_size=32 * 1024 * 1024)
    if (
        not isinstance(report, dict)
        or report.get("format") != "ffxivshare-jsonl"
        or report.get("format_version") != 3
        or report.get("valid") is not True
        or report.get("errors") != []
        or report.get("quarantined_records") != []
    ):
        raise RehearsalError("dataset_validation_report_invalid")


def _validate_import_report(
    path: Path,
    *,
    expected_status: set[str],
    expected_target_state: str,
) -> None:
    report = _load_json(path, maximum_size=32 * 1024 * 1024)
    if (
        not isinstance(report, dict)
        or report.get("operation") != "site_data_import"
        or report.get("status") not in expected_status
        or report.get("target_state") != expected_target_state
        or report.get("valid") is not True
        or report.get("errors") != []
        or report.get("quarantined_records") != []
        or report.get("database_state") != "complete"
        or report.get("data_stage") != "verified"
        or report.get("sequence_stage") != "verified"
        or report.get("recoverable") is not False
        or report.get("target_session_row_count") != 0
        or report.get("exclusive_target_attested") is not True
        or report.get("cutover_authorized") is not False
    ):
        raise RehearsalError("import_report_invalid")


def _validate_comparison(path: Path) -> None:
    report = _load_json(path)
    if (
        not isinstance(report, dict)
        or report.get("format") != "ffxivshare-site-data-export-comparison"
        or report.get("format_version") != 1
        or report.get("equivalent") is not True
        or report.get("cutover_authorized") is not False
        or report.get("issues") != []
    ):
        raise RehearsalError("site_data_comparison_invalid")


def _validate_restriction_preflight(path: Path) -> None:
    report = _load_json(path, maximum_size=32 * 1024 * 1024)
    manual = report.get("manual_review") if isinstance(report, dict) else None
    if (
        not isinstance(report, dict)
        or report.get("valid") is not True
        or report.get("ready_for_cutover") is not True
        or report.get("blocking_errors") != []
        or not isinstance(manual, dict)
        or manual.get("count") != 0
        or manual.get("share_ids") != []
    ):
        raise RehearsalBlocked("restriction_preflight_requires_review")


def _validate_media_comparison(path: Path) -> None:
    report = _load_json(path, maximum_size=32 * 1024 * 1024)
    if (
        not isinstance(report, dict)
        or report.get("format") != "ffxivshare-media-comparison"
        or report.get("format_version") != 1
        or report.get("matched") is not True
        or report.get("missing_paths") != []
        or report.get("unexpected_paths") != []
        or report.get("changed_paths") != []
    ):
        raise RehearsalBlocked("media_snapshot_mismatch")


MIGRATION_STATE_SCRIPT = r'''from __future__ import annotations
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import platform
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ffxivshare.settings")
django.setup()
from django.db import connection
from django.db.migrations.loader import MigrationLoader
loader = MigrationLoader(connection, ignore_no_migrations=True)
applied_set = set(loader.applied_migrations)
known = set(loader.graph.nodes)
unknown = sorted(applied_set - known)
applied = sorted(applied_set & known)
leaves = []
for node in applied:
    children = loader.graph.node_map[node].children
    if not any(
        child.key in applied_set and child.key[0] == node[0]
        for child in children
    ):
        leaves.append(node)
migration_code = []
for node, migration in sorted(loader.disk_migrations.items()):
    specification = importlib.util.find_spec(migration.__module__)
    if specification is None or specification.origin is None:
        raise RuntimeError(f"Cannot locate migration module: {migration.__module__}")
    origin = Path(specification.origin)
    if not origin.is_file():
        raise RuntimeError(f"Migration module is not a regular file: {migration.__module__}")
    migration_code.append({
        "node": list(node),
        "module": migration.__module__,
        "sha256": sha256(origin.read_bytes()).hexdigest(),
    })
runtime_projection = {
    "python_version": platform.python_version(),
    "django_version": django.get_version(),
    "migration_code": migration_code,
}
runtime_bytes = (
    json.dumps(
        runtime_projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    + "\n"
).encode("utf-8")
payload = {
    "format": "ffxivshare-migration-state",
    "format_version": 1,
    "database_vendor": connection.vendor,
    "applied": [list(node) for node in applied],
    "applied_leaf_nodes": [list(node) for node in sorted(leaves)],
    "repository_leaf_nodes": [list(node) for node in sorted(loader.graph.leaf_nodes())],
    "unknown_applied_nodes": [list(node) for node in unknown],
    "python_version": runtime_projection["python_version"],
    "django_version": runtime_projection["django_version"],
    "migration_runtime_sha256": sha256(runtime_bytes).hexdigest(),
}
destination = Path(__import__("sys").argv[1])
with destination.open("x", encoding="utf-8", newline="\n") as stream:
    json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
connection.close()
'''


RUNTIME_FINGERPRINT_SCRIPT = r'''from __future__ import annotations
import csv
from hashlib import sha256
from importlib import machinery, metadata, util
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

root = Path.cwd().resolve(strict=True)
prefix = Path(sys.prefix).resolve(strict=True)
base_prefix = Path(sys.base_prefix).resolve(strict=True)
seen_requirement_files = set()
pins = {}
requirement_files = []
name_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
hash_cache = {}
identity_cache = {}
identity_source_paths = {}
reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

def canonical(path):
    return os.path.normcase(os.path.realpath(os.fspath(path)))

root_key = canonical(root)
prefix_key = canonical(prefix)
base_prefix_key = canonical(base_prefix)
trusted_roots = (
    ("$EXECUTION_ROOT", root, root_key),
    ("$PREFIX", prefix, prefix_key),
    ("$BASE_PREFIX", base_prefix, base_prefix_key),
)

def file_identity(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )

if canonical(prefix) == canonical(base_prefix):
    raise RuntimeError("Production-copy rehearsal requires a dedicated virtual environment.")

def within(path, directory):
    try:
        path_key = canonical(path)
        directory_key = canonical(directory)
        return os.path.commonpath((path_key, directory_key)) == directory_key
    except ValueError:
        return False

def resolved_within(path, directory_key):
    path_key = os.path.normcase(os.path.abspath(os.fspath(path)))
    try:
        return os.path.commonpath((path_key, directory_key)) == directory_key
    except ValueError:
        return False

def absolute_path_label(path):
    absolute = os.path.abspath(os.fspath(path))
    absolute_key = os.path.normcase(absolute)
    for marker, marker_root, marker_key in trusted_roots:
        try:
            if os.path.commonpath((absolute_key, marker_key)) != marker_key:
                continue
        except ValueError:
            continue
        relative = os.path.relpath(absolute, os.fspath(marker_root)).replace(os.sep, "/")
        return marker if relative == "." else f"{marker}/{relative}"
    raise RuntimeError(f"Runtime path escaped trusted roots: {path}")

def path_label(path):
    return absolute_path_label(Path(path).resolve(strict=False))

def hashed_path_label(path):
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    item = identity_cache.get(key)
    if item is None:
        raise RuntimeError(f"Runtime path was not hashed before projection: {path}")
    return item["path"]

def digest_file(path):
    path = Path(path)
    key = canonical(path)
    if key in hash_cache:
        return hash_cache[key]
    before_path = os.lstat(path)
    if (
        not stat.S_ISREG(before_path.st_mode)
        or stat.S_ISLNK(before_path.st_mode)
        or getattr(before_path, "st_file_attributes", 0) & reparse_attribute
        or before_path.st_nlink != 1
        or key != os.path.normcase(os.path.abspath(os.fspath(path)))
    ):
        raise RuntimeError(f"Runtime file identity is unsafe: {path}")
    digest = sha256()
    size = 0
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    current = os.lstat(path)
    current_key = canonical(path)
    if (
        not stat.S_ISREG(current.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or getattr(current, "st_file_attributes", 0) & reparse_attribute
        or current.st_nlink != 1
        or current_key != os.path.normcase(os.path.abspath(os.fspath(path)))
        or file_identity(before_path) != file_identity(before)
        or file_identity(before) != file_identity(after)
        or file_identity(after) != file_identity(current)
    ):
        raise RuntimeError(f"Runtime file changed while hashing: {path}")
    result = (size, digest.hexdigest())
    hash_cache[key] = result
    identity_cache[key] = {
        "path": absolute_path_label(path),
        "device": current.st_dev,
        "inode": current.st_ino,
        "size": current.st_size,
        "mtime_ns": current.st_mtime_ns,
        "ctime_ns": current.st_ctime_ns,
    }
    identity_source_paths[key] = path
    return result

def revalidate_hashed_file_identities():
    for key in sorted(identity_cache):
        path = identity_source_paths[key]
        expected = identity_cache[key]
        try:
            current = os.lstat(path)
        except OSError as exc:
            raise RuntimeError(
                f"Runtime file disappeared before fingerprint publication: {path}"
            ) from exc
        if (
            not stat.S_ISREG(current.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or getattr(current, "st_file_attributes", 0) & reparse_attribute
            or current.st_nlink != 1
            or canonical(path) != os.path.normcase(os.path.abspath(os.fspath(path)))
            or file_identity(current)
            != (
                expected["device"],
                expected["inode"],
                expected["size"],
                expected["mtime_ns"],
                expected["ctime_ns"],
            )
        ):
            raise RuntimeError(
                f"Runtime file changed before fingerprint publication: {path}"
            )

def canonical_digest(value):
    encoded = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    return sha256(encoded).hexdigest()

def load_requirements(path):
    path = path.resolve(strict=True)
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        raise RuntimeError("Requirement include escaped the execution snapshot.")
    if relative in seen_requirement_files:
        return
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("Requirement input is not a regular snapshot file.")
    seen_requirement_files.add(relative)
    size, digest = digest_file(path)
    requirement_files.append({"path": relative, "size": size, "sha256": digest})
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r ") or line.startswith("--requirement "):
            include = line.split(None, 1)[1].strip()
            load_requirements(path.parent / include)
            continue
        if "==" not in line or line.count("==") != 1:
            raise RuntimeError(f"Runtime requirement is not exactly pinned: {line}")
        name, expected = (part.strip() for part in line.split("==", 1))
        if not name_pattern.fullmatch(name) or not expected:
            raise RuntimeError(f"Runtime requirement is invalid: {line}")
        normalized = re.sub(r"[-_.]+", "-", name).lower()
        if normalized in pins and pins[normalized] != expected:
            raise RuntimeError(f"Runtime requirement has conflicting pins: {name}")
        pins[normalized] = expected

load_requirements(root / "requirements.txt")
if (
    site.ENABLE_USER_SITE is not False
    or not sys.flags.no_user_site
    or not sys.flags.ignore_environment
    or not sys.flags.dont_write_bytecode
    or not sys.flags.utf8_mode
):
    raise RuntimeError("Python isolation flags are incomplete.")
user_site = site.getusersitepackages()
user_sites = [user_site] if isinstance(user_site, str) else list(user_site)
if any(canonical(entry) == canonical(user) for entry in sys.path for user in user_sites):
    raise RuntimeError("Python user-site is present on sys.path.")

sys_path_projection = []
seen_sys_paths = set()
resolved_sys_paths = []
for raw_entry in sys.path:
    entry = root if raw_entry == "" else Path(raw_entry)
    label = path_label(entry)
    if label in seen_sys_paths:
        raise RuntimeError(f"Duplicate sys.path entry: {label}")
    seen_sys_paths.add(label)
    sys_path_projection.append({"path": label, "exists": entry.exists()})
    if entry.exists():
        resolved_sys_paths.append(entry.resolve(strict=True))

site_package_candidates = {}
for candidate in [
    *site.getsitepackages(),
    sysconfig.get_path("purelib"),
    sysconfig.get_path("platlib"),
]:
    if not candidate:
        continue
    candidate_path = Path(candidate).resolve(strict=True)
    if not within(candidate_path, prefix):
        raise RuntimeError("A site-packages root escaped the virtual environment.")
    site_package_candidates.setdefault(canonical(candidate_path), candidate_path)

# CPython on Windows can report both the virtual-environment prefix and its
# nested Lib/site-packages directory. The outer recursive scan already covers
# the complete union, so retaining the nested root only repeats path resolution,
# closure construction, and checkpoint work without adding any coverage.
site_package_roots = []
for candidate_path in sorted(
    site_package_candidates.values(),
    key=lambda path: (len(Path(canonical(path)).parts), canonical(path)),
):
    if any(within(candidate_path, existing) for existing in site_package_roots):
        continue
    site_package_roots.append(candidate_path)
if not site_package_roots:
    raise RuntimeError("The virtual environment has no site-packages roots.")

distribution_projection = []
seen_distributions = set()
claimed_paths = {}
installed = list(metadata.distributions())
for distribution in installed:
    raw_name = distribution.metadata.get("Name")
    version = distribution.version
    if not raw_name or not version:
        raise RuntimeError("Installed distribution metadata is incomplete.")
    name = re.sub(r"[-_.]+", "-", raw_name).lower()
    if name in seen_distributions:
        raise RuntimeError(f"Duplicate installed distribution: {name}")
    seen_distributions.add(name)
    metadata_directory = Path(distribution._path).resolve(strict=True)
    if not resolved_within(metadata_directory, prefix_key):
        raise RuntimeError(f"Distribution escaped the virtual environment: {name}")
    metadata_path = metadata_directory / "METADATA"
    record_path = metadata_directory / "RECORD"
    metadata_size, metadata_sha256 = digest_file(metadata_path)
    record_size, record_sha256 = digest_file(record_path)
    metadata_file_key = os.path.normcase(os.path.abspath(os.fspath(metadata_path)))
    record_file_key = os.path.normcase(os.path.abspath(os.fspath(record_path)))
    try:
        with record_path.open("r", encoding="utf-8", newline="") as record_stream:
            rows = list(csv.reader(record_stream))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise RuntimeError(f"Distribution RECORD is invalid: {name}") from exc
    entries = []
    seen_record_names = set()
    metadata_was_recorded = False
    record_was_recorded = False
    for row in rows:
        if len(row) != 3 or not row[0] or "\x00" in row[0]:
            raise RuntimeError(f"Distribution RECORD row is invalid: {name}")
        record_name = row[0].replace("\\", "/")
        record_name_key = record_name.casefold()
        if record_name_key in seen_record_names:
            raise RuntimeError(f"Distribution RECORD contains duplicates: {name}")
        seen_record_names.add(record_name_key)
        target = Path(distribution.locate_file(metadata.PackagePath(record_name)))
        try:
            target = target.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError(f"Distribution RECORD target is missing: {name}") from exc
        target_key = os.path.normcase(os.fspath(target))
        if not resolved_within(target, prefix_key):
            raise RuntimeError(f"Distribution RECORD escaped the virtual environment: {name}")
        owner = claimed_paths.setdefault(target_key, name)
        if owner != name:
            raise RuntimeError(f"Distribution RECORD target has multiple owners: {record_name}")
        size, digest = digest_file(target)
        label = hashed_path_label(target)
        entries.append({"path": label, "size": size, "sha256": digest})
        metadata_was_recorded = metadata_was_recorded or target_key == metadata_file_key
        record_was_recorded = record_was_recorded or target_key == record_file_key
    if not metadata_was_recorded or not record_was_recorded:
        raise RuntimeError(f"Distribution metadata files are absent from RECORD: {name}")
    if name in pins and pins[name] != version:
        raise RuntimeError(f"Installed distribution {name} is {version}, expected {pins[name]}.")
    entries.sort(key=lambda item: item["path"])
    distribution_projection.append({
        "name": name,
        "version": version,
        "direct_requirement": name in pins,
        "metadata_directory": path_label(metadata_directory),
        "metadata_size": metadata_size,
        "metadata_sha256": metadata_sha256,
        "record_size": record_size,
        "record_sha256": record_sha256,
        "recorded_file_count": len(entries),
        "recorded_total_size": sum(item["size"] for item in entries),
        "recorded_files_sha256": canonical_digest(entries),
    })
missing_direct = sorted(set(pins) - seen_distributions)
if missing_direct:
    raise RuntimeError(f"Required distributions are missing: {missing_direct}")
distribution_projection.sort(key=lambda item: item["name"])

site_files_by_path = {}
pth_files_by_path = {}
closure_scopes = []
closure_scope_keys = set()
recursive_scope_roots = []
import_closure_paths = {}
directory_identity_cache = {}
importable_suffixes = tuple(
    sorted(
        {suffix.casefold() for suffix in machinery.all_suffixes()},
        key=len,
        reverse=True,
    )
)

def register_import_closure_file(path):
    path = Path(path).resolve(strict=True)
    import_closure_paths.setdefault(canonical(path), path)

def is_importable_file(path):
    name = Path(path).name.casefold()
    return any(name.endswith(suffix) for suffix in importable_suffixes)

def add_closure_scope(root_path, mode, entries):
    scope = {
        "root": path_label(root_path),
        "mode": mode,
        "files": sorted(set(entries)),
    }
    key = (scope["root"], mode)
    if key in closure_scope_keys:
        existing = next(
            item
            for item in closure_scopes
            if (item["root"], item["mode"]) == key
        )
        if existing != scope:
            raise RuntimeError(f"Runtime closure scope disagrees with itself: {key}")
        return
    closure_scope_keys.add(key)
    closure_scopes.append(scope)

def safe_directory(path, *, label, metadata=None):
    path = Path(path)
    metadata = os.lstat(path) if metadata is None else metadata
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or getattr(metadata, "st_file_attributes", 0) & reparse_attribute
    ):
        raise RuntimeError(f"{label} is not a real directory: {path}")
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    expected = file_identity(metadata)
    existing = directory_identity_cache.get(key)
    if existing is not None and existing[1] != expected:
        raise RuntimeError(f"{label} changed while being checked: {path}")
    directory_identity_cache.setdefault(key, (path, expected))

def revalidate_runtime_directory_identities():
    for key in sorted(directory_identity_cache):
        path, expected = directory_identity_cache[key]
        try:
            current = os.lstat(path)
        except OSError as exc:
            raise RuntimeError(
                f"Runtime directory disappeared before fingerprint publication: {path}"
            ) from exc
        if (
            not stat.S_ISDIR(current.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or getattr(current, "st_file_attributes", 0) & reparse_attribute
            or file_identity(current) != expected
        ):
            raise RuntimeError(
                f"Runtime directory changed before fingerprint publication: {path}"
            )

def top_level_entries(directory):
    safe_directory(directory, label="Runtime sys.path closure root")
    entries = []
    children = []
    for candidate in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
        metadata = os.lstat(candidate)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or getattr(metadata, "st_file_attributes", 0) & reparse_attribute
        ):
            raise RuntimeError(f"Runtime closure contains a linked entry: {candidate}")
        if stat.S_ISDIR(metadata.st_mode):
            safe_directory(
                candidate,
                label="Runtime sys.path closure entry",
                metadata=metadata,
            )
            entries.append(path_label(candidate) + "/")
            children.append(candidate)
        elif stat.S_ISREG(metadata.st_mode):
            entries.append(path_label(candidate))
            register_import_closure_file(candidate)
        else:
            raise RuntimeError(f"Runtime closure contains a special entry: {candidate}")
    return sorted(entries), children

def classified_recursive_entries(directory):
    safe_directory(directory, label="Runtime recursive closure root")
    all_entries = []
    import_entries = []
    ordinary_files = []
    contains_importable_code = False
    for current, directory_names, file_names in os.walk(directory, followlinks=False):
        current_path = Path(current)
        safe_directories = []
        for directory_name in sorted(directory_names):
            directory_path = current_path / directory_name
            safe_directory(directory_path, label="Runtime closure directory")
            directory_label = path_label(directory_path) + "/"
            all_entries.append(directory_label)
            import_entries.append(directory_label)
            safe_directories.append(directory_name)
        directory_names[:] = safe_directories
        for file_name in sorted(file_names):
            file_path = current_path / file_name
            metadata = os.lstat(file_path)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or getattr(metadata, "st_file_attributes", 0) & reparse_attribute
            ):
                raise RuntimeError(f"Runtime closure file is unsafe: {file_path}")
            file_label = path_label(file_path)
            ordinary_files.append(file_path)
            all_entries.append(file_label)
            if is_importable_file(file_path):
                contains_importable_code = True
                import_entries.append(file_label)
    if contains_importable_code:
        for file_path in ordinary_files:
            register_import_closure_file(file_path)
        return "recursive_package_entries", sorted(all_entries)
    return "recursive_import_entries", sorted(import_entries)

def add_partitioned_recursive_scope(directory):
    directory = Path(directory).resolve(strict=True)
    if any(within(directory, existing) for existing in recursive_scope_roots):
        return
    directory_label = path_label(directory)
    if (directory_label, "top_level_entries") in closure_scope_keys:
        entries, children = top_level_entries(directory)
        add_closure_scope(directory, "top_level_entries", entries)
        for child in children:
            if child.name.isidentifier():
                add_partitioned_recursive_scope(child)
        return
    nested_existing = [
        existing
        for existing in recursive_scope_roots
        if within(existing, directory)
    ]
    if nested_existing:
        entries, children = top_level_entries(directory)
        add_closure_scope(directory, "top_level_entries", entries)
        for child in children:
            if child.name.isidentifier():
                add_partitioned_recursive_scope(child)
        return
    mode, entries = classified_recursive_entries(directory)
    add_closure_scope(directory, mode, entries)
    recursive_scope_roots.append(directory)

for site_root in sorted(site_package_roots, key=canonical):
    scope_entries = []
    safe_directory(site_root, label="Site-packages root")
    for current, directory_names, file_names in os.walk(site_root, followlinks=False):
        current_path = Path(current)
        safe_directories = []
        for directory_name in sorted(directory_names):
            directory_path = current_path / directory_name
            directory_metadata = os.lstat(directory_path)
            safe_directory(
                directory_path,
                label="Site-packages directory",
                metadata=directory_metadata,
            )
            scope_entries.append(path_label(directory_path) + "/")
            safe_directories.append(directory_name)
        directory_names[:] = safe_directories
        for file_name in sorted(file_names):
            path = current_path / file_name
            size, digest = digest_file(path)
            item = {"path": hashed_path_label(path), "size": size, "sha256": digest}
            existing = site_files_by_path.setdefault(item["path"], item)
            if existing != item:
                raise RuntimeError(f"Site-packages file projection is ambiguous: {path}")
            scope_entries.append(item["path"])
            if path.suffix.lower() == ".pth":
                existing_pth = pth_files_by_path.setdefault(item["path"], item)
                if existing_pth != item:
                    raise RuntimeError(f"Site-packages .pth projection is ambiguous: {path}")
    add_closure_scope(site_root, "recursive", scope_entries)
    recursive_scope_roots.append(site_root)
site_files = [site_files_by_path[path] for path in sorted(site_files_by_path)]
pth_files = [pth_files_by_path[path] for path in sorted(pth_files_by_path)]

base_runtime_files = []
seen_base_runtime_paths = set()
base_scheme_variables = dict(sysconfig.get_config_vars())
base_scheme_variables.update({
    "base": str(base_prefix),
    "exec_prefix": str(base_prefix),
    "installed_base": str(base_prefix),
    "installed_platbase": str(base_prefix),
    "platbase": str(base_prefix),
    "prefix": str(base_prefix),
})
inactive_base_site_package_roots = set()
for path_name in ("purelib", "platlib"):
    candidate = sysconfig.get_path(
        path_name,
        vars=dict(base_scheme_variables),
    )
    if not candidate:
        continue
    candidate_path = Path(candidate).resolve(strict=False)
    if not within(candidate_path, base_prefix) or within(candidate_path, prefix):
        continue
    if any(within(entry, candidate_path) for entry in resolved_sys_paths):
        raise RuntimeError(
            "The base Python site-packages directory is active on sys.path; "
            "the rehearsal requires an isolated virtual environment."
        )
    inactive_base_site_package_roots.add(candidate_path)

def is_inactive_base_site_package_root(path):
    return any(
        canonical(path) == canonical(candidate)
        for candidate in inactive_base_site_package_roots
    )

# Treat inactive base package roots as partition boundaries for the later
# sys.path closure as well as for the full base-runtime hash. Their presence at
# the base Lib level remains guarded, but their unused contents are not walked.
recursive_scope_roots.extend(inactive_base_site_package_roots)

for raw_entry in sys.path:
    entry = root if raw_entry == "" else Path(raw_entry)
    if not entry.exists() or not within(entry, base_prefix) or within(entry, prefix):
        continue
    entry = entry.resolve(strict=True)
    if entry.is_file():
        candidates = [entry]
    elif canonical(entry) == canonical(base_prefix):
        candidates = [
            candidate
            for candidate in sorted(entry.iterdir(), key=lambda item: item.name.casefold())
            if candidate.is_file()
        ]
    else:
        candidates = []
        for current, directory_names, file_names in os.walk(entry, followlinks=False):
            current_path = Path(current)
            safe_directories = []
            for directory_name in sorted(directory_names):
                directory_path = current_path / directory_name
                if is_inactive_base_site_package_root(directory_path):
                    continue
                directory_metadata = os.lstat(directory_path)
                if (
                    stat.S_ISLNK(directory_metadata.st_mode)
                    or getattr(directory_metadata, "st_file_attributes", 0) & reparse_attribute
                ):
                    raise RuntimeError(f"Base runtime directory is a link: {directory_path}")
                safe_directories.append(directory_name)
            directory_names[:] = safe_directories
            candidates.extend(current_path / name for name in sorted(file_names))
    for path in candidates:
        key = canonical(path)
        if key in seen_base_runtime_paths:
            continue
        seen_base_runtime_paths.add(key)
        size, digest = digest_file(path)
        base_runtime_files.append({
            "path": hashed_path_label(path),
            "size": size,
            "sha256": digest,
        })
base_runtime_files.sort(key=lambda item: item["path"])

# Every directory-type sys.path entry receives an exact one-level guard. Existing
# importable child directories are recursively closed, partitioning only around
# already-covered roots so site-packages and nested virtual environments are not
# walked or reported more than once.
for raw_entry in sys.path:
    entry = root if raw_entry == "" else Path(raw_entry)
    try:
        entry_metadata = os.lstat(entry)
    except FileNotFoundError:
        continue
    if (
        stat.S_ISLNK(entry_metadata.st_mode)
        or getattr(entry_metadata, "st_file_attributes", 0) & reparse_attribute
    ):
        raise RuntimeError(f"Runtime sys.path entry is linked: {entry}")
    if stat.S_ISREG(entry_metadata.st_mode):
        register_import_closure_file(entry)
        continue
    if not stat.S_ISDIR(entry_metadata.st_mode):
        continue
    entry = entry.resolve(strict=True)
    if any(within(entry, covered_root) for covered_root in recursive_scope_roots):
        continue
    entries, children = top_level_entries(entry)
    add_closure_scope(entry, "top_level_entries", entries)
    for child in children:
        if child.name.isidentifier():
            add_partitioned_recursive_scope(child)

package_mapping = metadata.packages_distributions()
direct_import_origins = []
for package_name, owners in sorted(package_mapping.items()):
    normalized_owners = sorted({re.sub(r"[-_.]+", "-", owner).lower() for owner in owners})
    direct_owners = sorted(set(normalized_owners) & set(pins))
    if not direct_owners:
        continue
    specification = util.find_spec(package_name)
    if specification is None:
        raise RuntimeError(f"Direct dependency import cannot be resolved: {package_name}")
    origin = None
    if specification.origin not in {None, "built-in", "frozen"}:
        origin_path = Path(specification.origin).resolve(strict=True)
        if not any(within(origin_path, site_root) for site_root in site_package_roots):
            raise RuntimeError(f"Direct dependency import escaped site-packages: {package_name}")
        origin = path_label(origin_path)
    locations = []
    for location in specification.submodule_search_locations or ():
        location_path = Path(location).resolve(strict=True)
        if not any(within(location_path, site_root) for site_root in site_package_roots):
            raise RuntimeError(f"Direct dependency package escaped site-packages: {package_name}")
        locations.append(path_label(location_path))
    direct_import_origins.append({
        "package": package_name,
        "distributions": direct_owners,
        "origin": origin,
        "search_locations": sorted(locations),
    })

runtime_paths = {
    "python_executable": Path(sys.executable).resolve(strict=True),
    "python_base_executable": Path(getattr(sys, "_base_executable", sys.executable)).resolve(strict=True),
    "sqlite_extension": Path(__import__("_sqlite3").__file__).resolve(strict=True),
}
if os.name == "nt":
    for directory in {runtime_paths["python_base_executable"].parent, base_prefix}:
        for pattern in ("python*.dll", "vcruntime*.dll"):
            for candidate in directory.glob(pattern):
                runtime_paths[f"windows_runtime_{candidate.name.lower()}"] = candidate.resolve(strict=True)
runtime_files = []
for role, path in sorted(runtime_paths.items()):
    size, digest = digest_file(path)
    runtime_files.append({
        "role": role,
        "path": hashed_path_label(path),
        "size": size,
        "sha256": digest,
    })

# Anything already present in hash_cache contributes to an existing projection
# component (requirements, distributions/site-packages, base runtime, or an
# explicitly named runtime file). Hash every remaining sys.path-importable file
# exactly once and fold only its compact canonical digest into the fingerprint.
already_projected_paths = set(hash_cache)
sys_path_import_closure_files = []
for key, path in sorted(
    import_closure_paths.items(),
    key=lambda item: path_label(item[1]),
):
    if key in already_projected_paths:
        continue
    size, digest = digest_file(path)
    sys_path_import_closure_files.append({
        "path": hashed_path_label(path),
        "size": size,
        "sha256": digest,
    })
sys_path_import_closure_files.sort(key=lambda item: item["path"])

projection = {
    "python": {
        "version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "cache_tag": sys.implementation.cache_tag,
        "abi_flags": getattr(sys, "abiflags", ""),
        "flags": {
            "dont_write_bytecode": bool(sys.flags.dont_write_bytecode),
            "ignore_environment": bool(sys.flags.ignore_environment),
            "no_user_site": bool(sys.flags.no_user_site),
            "utf8_mode": bool(sys.flags.utf8_mode),
        },
        "sys_path": sys_path_projection,
        "runtime_files": runtime_files,
        "base_runtime_closure": {
            "excluded_inactive_site_package_roots": sorted(
                path_label(path) for path in inactive_base_site_package_roots
            ),
            "file_count": len(base_runtime_files),
            "total_size": sum(item["size"] for item in base_runtime_files),
            "files_sha256": canonical_digest(base_runtime_files),
        },
        "sys_path_import_closure": {
            "file_count": len(sys_path_import_closure_files),
            "total_size": sum(
                item["size"] for item in sys_path_import_closure_files
            ),
            "files_sha256": canonical_digest(sys_path_import_closure_files),
        },
    },
    "sqlite": {
        "runtime_version": sqlite3.sqlite_version,
        "python_module_version": sqlite3.version,
    },
    "requirements": sorted(requirement_files, key=lambda item: item["path"]),
    "distributions": distribution_projection,
    "direct_import_origins": direct_import_origins,
    "site_packages": {
        "roots": sorted(path_label(path) for path in site_package_roots),
        "file_count": len(site_files),
        "total_size": sum(item["size"] for item in site_files),
        "files_sha256": canonical_digest(site_files),
        "pth_files": pth_files,
    },
}
encoded = (
    json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    + "\n"
).encode("utf-8")
checkpoint_identity_inventory = sorted(
    identity_cache.values(), key=lambda item: item["path"]
)
checkpoint_closure_scopes = sorted(
    closure_scopes, key=lambda item: (item["root"], item["mode"])
)
checkpoint_scope_roots = [scope["root"] for scope in checkpoint_closure_scopes]
checkpoint_closure_entries = [
    entry
    for scope in checkpoint_closure_scopes
    for entry in scope["files"]
]
checkpoint_closure_files = {
    entry for entry in checkpoint_closure_entries if not entry.endswith("/")
}
if (
    checkpoint_scope_roots != sorted(set(checkpoint_scope_roots))
    or len(checkpoint_closure_entries) != len(set(checkpoint_closure_entries))
    or not checkpoint_closure_files.issubset(
        {item["path"] for item in checkpoint_identity_inventory}
    )
):
    raise RuntimeError("Runtime checkpoint scopes are not disjoint and complete.")
payload = {
    "format": "ffxivshare-runtime-fingerprint",
    "format_version": 1,
    "fingerprint_sha256": sha256(encoded).hexdigest(),
    "projection": projection,
    "checkpoint": {
        "format": "ffxivshare-runtime-identity-checkpoint-source",
        "format_version": 1,
        "content_hashed": True,
        "identity_inventory": checkpoint_identity_inventory,
        "closure_scopes": checkpoint_closure_scopes,
    },
}
destination = Path(sys.argv[1])
output_identity = None
try:
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        output_metadata = os.fstat(stream.fileno())
        output_identity = (output_metadata.st_dev, output_metadata.st_ino)
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    revalidate_runtime_directory_identities()
    revalidate_hashed_file_identities()
except BaseException:
    if output_identity is not None:
        try:
            current_output = os.lstat(destination)
            if (
                stat.S_ISREG(current_output.st_mode)
                and not stat.S_ISLNK(current_output.st_mode)
                and not (
                    getattr(current_output, "st_file_attributes", 0)
                    & reparse_attribute
                )
                and current_output.st_nlink == 1
                and (current_output.st_dev, current_output.st_ino)
                == output_identity
            ):
                destination.unlink()
        except FileNotFoundError:
            pass
    raise
'''


RUNTIME_IDENTITY_CHECKPOINT_SCRIPT = r'''from __future__ import annotations
from hashlib import sha256
import json
import os
from importlib import machinery
from pathlib import Path, PurePosixPath
import stat
import sys

reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
execution_root = Path.cwd().resolve(strict=True)
prefix = Path(sys.prefix).resolve(strict=True)
base_prefix = Path(sys.base_prefix).resolve(strict=True)

def reject_constant(value):
    raise ValueError(f"JSON constant is not allowed: {value}")

def strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON key: {key}")
        value[key] = item
    return value

roots = {
    "$EXECUTION_ROOT": execution_root,
    "$PREFIX": prefix,
    "$BASE_PREFIX": base_prefix,
}
importable_suffixes = tuple(
    sorted(
        {suffix.casefold() for suffix in machinery.all_suffixes()},
        key=len,
        reverse=True,
    )
)

def is_importable_file(path):
    name = Path(path).name.casefold()
    return any(name.endswith(suffix) for suffix in importable_suffixes)

def identity(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )

def path_key(path):
    return os.path.normcase(os.path.abspath(os.fspath(path)))

def parse_label(label, *, directory_entry=False):
    if (
        not isinstance(label, str)
        or not label
        or "\\" in label
        or "\x00" in label
        or (label.endswith("/") != directory_entry)
    ):
        raise RuntimeError("Runtime checkpoint path label is invalid.")
    core = label[:-1] if directory_entry else label
    for marker, root in roots.items():
        if core == marker:
            relative_text = ""
        elif core.startswith(marker + "/"):
            relative_text = core[len(marker) + 1:]
        else:
            continue
        relative = PurePosixPath(relative_text or ".")
        parts = () if not relative_text else relative.parts
        if (
            relative.is_absolute()
            or any(part in {"", ".", ".."} for part in parts)
            or core != marker + ("/" + "/".join(parts) if parts else "")
            or (directory_entry and not parts)
        ):
            raise RuntimeError("Runtime checkpoint path escaped its root.")
        return marker, root, parts
    raise RuntimeError("Runtime checkpoint path uses an unknown root.")

def label_within_scope(label_parts, root_parts, *, top_level):
    marker, _, parts = label_parts
    root_marker, _, scope_parts = root_parts
    if marker != root_marker or parts[:len(scope_parts)] != scope_parts:
        return False
    return (
        len(parts) == len(scope_parts) + 1
        if top_level
        else len(parts) > len(scope_parts)
    )

directory_guards = {}

def guard_directory(path, *, issue, metadata=None):
    path = Path(path)
    metadata = os.lstat(path) if metadata is None else metadata
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or getattr(metadata, "st_file_attributes", 0) & reparse_attribute
    ):
        raise RuntimeError(f"{issue} is not a real directory: {path}")
    key = path_key(path)
    expected = identity(metadata)
    existing = directory_guards.get(key)
    if existing is not None and existing[1] != expected:
        raise RuntimeError(f"{issue} changed while being checked: {path}")
    directory_guards.setdefault(key, (path, expected))
    return path

def guard_marker_root(root):
    root = Path(root)
    if not root.is_absolute() or not root.anchor:
        raise RuntimeError("Runtime checkpoint marker root is not absolute.")
    current = Path(root.anchor)
    guard_directory(current, issue="Runtime checkpoint marker component")
    for part in root.parts[1:]:
        current /= part
        guard_directory(current, issue="Runtime checkpoint marker component")

def expand_directory_label(label):
    marker, root, parts = parse_label(label)
    current = root
    guard_directory(current, issue="Runtime checkpoint marker root")
    for part in parts:
        current /= part
        guard_directory(current, issue="Runtime checkpoint scope component")
    return current

def expand_file_label(label):
    marker, root, parts = parse_label(label)
    if not parts:
        raise RuntimeError("Runtime checkpoint file label names a root directory.")
    current = root
    guard_directory(current, issue="Runtime checkpoint marker root")
    for part in parts[:-1]:
        current /= part
        guard_directory(current, issue="Runtime checkpoint file component")
    path = current / parts[-1]
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise RuntimeError(f"Runtime inventory file disappeared: {label}") from exc
    return path, metadata

def compose_label(scope_root, relative_parts, *, directory_entry=False):
    if not relative_parts or any("\\" in part or "\x00" in part for part in relative_parts):
        raise RuntimeError("Runtime closure contains an invalid path name.")
    label = scope_root + "/" + "/".join(relative_parts)
    return label + "/" if directory_entry else label

def safe_file_metadata(path, metadata):
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or getattr(metadata, "st_file_attributes", 0) & reparse_attribute
        or metadata.st_nlink != 1
    ):
        raise RuntimeError(f"Runtime closure file is unsafe: {path}")

def revalidate_runtime_files(file_guards):
    for label in sorted(file_guards):
        path, expected = file_guards[label]
        metadata = os.lstat(path)
        safe_file_metadata(path, metadata)
        if identity(metadata) != expected:
            raise RuntimeError(f"Runtime file identity changed: {label}")

def revalidate_runtime_directories():
    for key in sorted(directory_guards):
        path, expected = directory_guards[key]
        metadata = os.lstat(path)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or getattr(metadata, "st_file_attributes", 0) & reparse_attribute
            or identity(metadata) != expected
        ):
            raise RuntimeError(f"Runtime directory identity changed: {path}")

source_path = Path(sys.argv[1])
expected_fingerprint = sys.argv[2]
expected_source_sha256 = sys.argv[3]
destination = Path(sys.argv[4])
if (
    len(expected_fingerprint) != 64
    or any(character not in "0123456789abcdef" for character in expected_fingerprint)
    or len(expected_source_sha256) != 64
    or any(character not in "0123456789abcdef" for character in expected_source_sha256)
):
    raise RuntimeError("Runtime checkpoint expected hashes are invalid.")
before_path = os.lstat(source_path)
if (
    not stat.S_ISREG(before_path.st_mode)
    or stat.S_ISLNK(before_path.st_mode)
    or getattr(before_path, "st_file_attributes", 0) & reparse_attribute
    or before_path.st_nlink != 1
    or before_path.st_size > 64 * 1024 * 1024
):
    raise RuntimeError("Runtime checkpoint source identity is unsafe.")
raw = bytearray()
digest = sha256()
with source_path.open("rb") as stream:
    before = os.fstat(stream.fileno())
    while True:
        chunk = stream.read(min(1024 * 1024, 64 * 1024 * 1024 + 1 - len(raw)))
        if not chunk:
            break
        raw.extend(chunk)
        digest.update(chunk)
        if len(raw) > 64 * 1024 * 1024:
            raise RuntimeError("Runtime checkpoint source is too large.")
    after = os.fstat(stream.fileno())
current = os.lstat(source_path)
if (
    identity(before_path) != identity(before)
    or identity(before) != identity(after)
    or identity(after) != identity(current)
    or digest.hexdigest() != expected_source_sha256
):
    raise RuntimeError("Runtime checkpoint source changed or is not authoritative.")
try:
    report = json.loads(
        bytes(raw).decode("utf-8"),
        object_pairs_hook=strict_object,
        parse_constant=reject_constant,
    )
except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
    raise RuntimeError("Runtime checkpoint source JSON is invalid.") from exc
checkpoint = report.get("checkpoint") if isinstance(report, dict) else None
if (
    not isinstance(report, dict)
    or set(report) != {
        "checkpoint", "fingerprint_sha256", "format", "format_version", "projection"
    }
    or report.get("format") != "ffxivshare-runtime-fingerprint"
    or report.get("format_version") != 1
    or report.get("fingerprint_sha256") != expected_fingerprint
    or not isinstance(report.get("projection"), dict)
    or sha256(
        (
            json.dumps(
                report["projection"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    != expected_fingerprint
    or not isinstance(checkpoint, dict)
    or set(checkpoint) != {
        "closure_scopes", "content_hashed", "format", "format_version", "identity_inventory"
    }
    or checkpoint["format"] != "ffxivshare-runtime-identity-checkpoint-source"
    or checkpoint["format_version"] != 1
    or checkpoint["content_hashed"] is not True
    or not isinstance(checkpoint["identity_inventory"], list)
    or not isinstance(checkpoint["closure_scopes"], list)
):
    raise RuntimeError("Runtime checkpoint source is invalid.")

for marker_root in roots.values():
    guard_marker_root(marker_root)

inventory_by_path = {}
inventory_paths = []
for item in checkpoint["identity_inventory"]:
    if (
        not isinstance(item, dict)
        or set(item) != {"ctime_ns", "device", "inode", "mtime_ns", "path", "size"}
        or not isinstance(item["path"], str)
        or not all(
            isinstance(item[name], int) and not isinstance(item[name], bool)
            for name in ("ctime_ns", "device", "inode", "mtime_ns", "size")
        )
        or item["size"] < 0
    ):
        raise RuntimeError("Runtime checkpoint inventory is invalid.")
    parse_label(item["path"])
    inventory_paths.append(item["path"])
    inventory_by_path[item["path"]] = (
        item["device"],
        item["inode"],
        item["size"],
        item["mtime_ns"],
        item["ctime_ns"],
    )
if inventory_paths != sorted(set(inventory_paths)):
    raise RuntimeError("Runtime checkpoint inventory is not canonical.")

scope_plans = []
scope_keys = []
scope_roots = set()
closure_entries = set()
closure_file_labels = set()
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
        or not isinstance(scope["files"], list)
        or not all(isinstance(item, str) for item in scope["files"])
        or scope["files"] != sorted(set(scope["files"]))
    ):
        raise RuntimeError("Runtime checkpoint closure is invalid.")
    root_parts = parse_label(scope["root"])
    if scope["root"] in scope_roots:
        raise RuntimeError("Runtime checkpoint closure roots are not unique.")
    scope_roots.add(scope["root"])
    for entry in scope["files"]:
        directory_entry = entry.endswith("/")
        entry_parts = parse_label(entry, directory_entry=directory_entry)
        if not label_within_scope(
            entry_parts,
            root_parts,
            top_level=scope["mode"] == "top_level_entries",
        ):
            raise RuntimeError("Runtime checkpoint closure entry escaped its scope.")
        if (
            scope["mode"] == "recursive_import_entries"
            and not directory_entry
            and not is_importable_file(entry)
        ):
            raise RuntimeError("Runtime import-only closure contains an ordinary file.")
        if entry in closure_entries:
            raise RuntimeError("Runtime checkpoint closure entries overlap.")
        closure_entries.add(entry)
        if not directory_entry:
            closure_file_labels.add(entry)
    scope_keys.append((scope["root"], scope["mode"]))
    scope_plans.append((scope, root_parts))
if scope_keys != sorted(set(scope_keys)):
    raise RuntimeError("Runtime checkpoint closures are not canonical.")
if not closure_file_labels.issubset(set(inventory_paths)):
    raise RuntimeError("Runtime checkpoint closure references an untracked file.")

expanded_plans = []
expanded_root_keys = set()
expanded_scope_roots = []
for scope, root_parts in scope_plans:
    root = expand_directory_label(scope["root"])
    root_key = path_key(root)
    if root_key in expanded_root_keys:
        raise RuntimeError("Runtime checkpoint closure roots resolve to the same directory.")
    expanded_root_keys.add(root_key)
    for existing_root, existing_mode in expanded_scope_roots:
        try:
            common = os.path.commonpath((root_key, existing_root))
        except ValueError:
            continue
        if (
            scope["mode"] != "top_level_entries" and common == root_key
            or existing_mode != "top_level_entries" and common == existing_root
        ):
            raise RuntimeError("Runtime recursive closure scopes overlap.")
    expanded_scope_roots.append((root_key, scope["mode"]))
    expanded_plans.append((scope, root))

verified_files = {}
verified_file_keys = set()

def inspect_file(path, label, metadata, *, included):
    safe_file_metadata(path, metadata)
    if not included:
        return
    expected = inventory_by_path.get(label)
    observed = identity(metadata)
    if expected is None or observed != expected:
        raise RuntimeError(f"Runtime file identity changed: {label}")
    if label in verified_files:
        raise RuntimeError(f"Runtime file was checked more than once: {label}")
    key = path_key(path)
    if key in verified_file_keys:
        raise RuntimeError(f"Runtime file has more than one checkpoint label: {label}")
    verified_file_keys.add(key)
    verified_files[label] = (Path(path), expected)

for label in sorted(set(inventory_paths) - closure_file_labels):
    path, metadata = expand_file_label(label)
    inspect_file(path, label, metadata, included=True)

for scope, root in expanded_plans:
    discovered = []
    pending = [(root, ())]
    while pending:
        current_path, current_parts = pending.pop()
        guard_directory(current_path, issue="Runtime closure directory")
        try:
            with os.scandir(current_path) as stream:
                entries = sorted(stream, key=lambda item: item.name.casefold())
        except OSError as exc:
            raise RuntimeError(
                f"Runtime closure directory could not be enumerated: {current_path}"
            ) from exc
        child_directories = []
        for entry in entries:
            candidate = current_path / entry.name
            try:
                metadata = os.lstat(candidate)
            except OSError as exc:
                raise RuntimeError(f"Runtime closure entry disappeared: {candidate}") from exc
            relative_parts = current_parts + (entry.name,)
            if (
                stat.S_ISLNK(metadata.st_mode)
                or getattr(metadata, "st_file_attributes", 0) & reparse_attribute
            ):
                raise RuntimeError(f"Runtime closure contains a linked entry: {candidate}")
            if stat.S_ISDIR(metadata.st_mode):
                guard_directory(
                    candidate,
                    issue="Runtime closure directory",
                    metadata=metadata,
                )
                discovered.append(
                    compose_label(scope["root"], relative_parts, directory_entry=True)
                )
                if scope["mode"] != "top_level_entries":
                    child_directories.append((candidate, relative_parts))
                continue
            included = (
                scope["mode"] != "recursive_import_entries"
                or is_importable_file(candidate)
            )
            label = compose_label(scope["root"], relative_parts)
            inspect_file(candidate, label, metadata, included=included)
            if included:
                discovered.append(label)
        pending.extend(reversed(child_directories))
        if scope["mode"] == "top_level_entries":
            break
    if sorted(discovered) != scope["files"]:
        raise RuntimeError(f"Runtime closure changed: {scope['root']}")

if set(verified_files) != set(inventory_paths):
    raise RuntimeError("Runtime checkpoint did not verify every inventory file.")
revalidate_runtime_directories()

payload = {
    "format": "ffxivshare-runtime-identity-checkpoint",
    "format_version": 1,
    "fingerprint_sha256": expected_fingerprint,
    "source_report_sha256": expected_source_sha256,
    "content_rehashed": False,
    "identity_inventory_checked": len(inventory_paths),
    "closure_scopes_checked": len(scope_keys),
    "unchanged": True,
}
output_identity = None
try:
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        output_metadata = os.fstat(stream.fileno())
        output_identity = (output_metadata.st_dev, output_metadata.st_ino)
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    revalidate_runtime_directories()
    revalidate_runtime_files(verified_files)
    final_source = os.lstat(source_path)
    if (
        not stat.S_ISREG(final_source.st_mode)
        or stat.S_ISLNK(final_source.st_mode)
        or getattr(final_source, "st_file_attributes", 0) & reparse_attribute
        or final_source.st_nlink != 1
        or identity(final_source) != identity(current)
    ):
        raise RuntimeError("Runtime checkpoint source changed before publication.")
except BaseException:
    if output_identity is not None:
        try:
            current_output = os.lstat(destination)
            if (
                stat.S_ISREG(current_output.st_mode)
                and not stat.S_ISLNK(current_output.st_mode)
                and not (
                    getattr(current_output, "st_file_attributes", 0)
                    & reparse_attribute
                )
                and current_output.st_nlink == 1
                and (current_output.st_dev, current_output.st_ino)
                == output_identity
            ):
                destination.unlink()
        except FileNotFoundError:
            pass
    raise
'''


def _compressed_python_command(source: str, *, filename: str) -> str:
    """Keep audited embedded programs below the Windows command-line limit."""

    import base64
    import zlib

    encoded = base64.b85encode(zlib.compress(source.encode("utf-8"), level=9)).decode(
        "ascii"
    )
    command = (
        "import base64,zlib;"
        "exec(compile(zlib.decompress(base64.b85decode("
        f"{encoded!r})).decode('utf-8'),{filename!r},'exec'))"
    )
    if len(command) >= 24_000:
        raise RehearsalError("embedded_python_command_too_large")
    return command


def _secure_run_root(path: Path) -> str:
    if os.name != "nt":
        os.chmod(path, 0o700)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode != 0o700:
            raise ConfigurationError("Run root permissions are not private.")
        return "posix_mode_0700"

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
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.SetFileSecurityW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    advapi32.SetFileSecurityW.restype = wintypes.BOOL
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
    token = wintypes.HANDLE()
    TOKEN_QUERY = 0x0008
    TOKEN_USER_CLASS = 1
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        raise ConfigurationError("Current Windows token cannot be inspected.")

    def sid_to_string(sid_pointer: int) -> str:
        output = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(
            ctypes.c_void_p(sid_pointer), ctypes.byref(output)
        ):
            raise ConfigurationError("Windows SID conversion failed.")
        try:
            return output.value
        finally:
            kernel32.LocalFree(output)

    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(
            token,
            TOKEN_USER_CLASS,
            None,
            0,
            ctypes.byref(required),
        )
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            TOKEN_USER_CLASS,
            buffer,
            required,
            ctypes.byref(required),
        ):
            raise ConfigurationError("Current Windows user SID cannot be inspected.")
        token_user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
        current_user_sid = sid_to_string(token_user.User.Sid)
    finally:
        kernel32.CloseHandle(token)

    security_descriptor = ctypes.c_void_p()
    descriptor_size = wintypes.DWORD()
    sddl = (
        "D:P"
        f"(A;OICI;FA;;;{current_user_sid})"
        "(A;OICI;FA;;;S-1-5-18)"
        "(A;OICI;FA;;;S-1-5-32-544)"
    )
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        1,
        ctypes.byref(security_descriptor),
        ctypes.byref(descriptor_size),
    ):
        raise ConfigurationError("Private Windows security descriptor is invalid.")
    try:
        security_information = 0x00000004
        if not advapi32.SetFileSecurityW(
            str(path), security_information, security_descriptor
        ):
            error_code = ctypes.get_last_error()
            raise ConfigurationError(
                f"Private Windows DACL could not be applied (Win32 {error_code})."
            )
    finally:
        kernel32.LocalFree(security_descriptor)

    required = wintypes.DWORD()
    advapi32.GetFileSecurityW(str(path), 0x00000004, None, 0, ctypes.byref(required))
    descriptor_buffer = ctypes.create_string_buffer(required.value)
    if not advapi32.GetFileSecurityW(
        str(path),
        0x00000004,
        descriptor_buffer,
        required,
        ctypes.byref(required),
    ):
        raise ConfigurationError("Private Windows DACL could not be read back.")
    control = wintypes.WORD()
    revision = wintypes.DWORD()
    if (
        not advapi32.GetSecurityDescriptorControl(
            descriptor_buffer, ctypes.byref(control), ctypes.byref(revision)
        )
        or not control.value & 0x1000
    ):
        raise ConfigurationError("Private Windows DACL is not protected.")
    present = wintypes.BOOL()
    defaulted = wintypes.BOOL()
    acl = ctypes.c_void_p()
    if (
        not advapi32.GetSecurityDescriptorDacl(
            descriptor_buffer,
            ctypes.byref(present),
            ctypes.byref(acl),
            ctypes.byref(defaulted),
        )
        or not present.value
        or not acl.value
    ):
        raise ConfigurationError("Private Windows DACL is missing.")
    acl_info = AclSizeInformation()
    if not advapi32.GetAclInformation(
        acl,
        ctypes.byref(acl_info),
        ctypes.sizeof(acl_info),
        2,
    ):
        raise ConfigurationError("Private Windows DACL cannot be enumerated.")
    if acl_info.AceCount != 3:
        raise ConfigurationError("Private Windows DACL has unexpected entries.")
    expected_sids = {
        current_user_sid,
        "S-1-5-18",
        "S-1-5-32-544",
    }
    actual_sids = set()
    for index in range(acl_info.AceCount):
        ace_pointer = ctypes.c_void_p()
        if not advapi32.GetAce(acl, index, ctypes.byref(ace_pointer)):
            raise ConfigurationError("Private Windows DACL ACE cannot be read.")
        header = ctypes.cast(ace_pointer, ctypes.POINTER(AceHeader)).contents
        mask = ctypes.c_uint32.from_address(ace_pointer.value + 4).value
        sid_pointer = ace_pointer.value + 8
        if header.AceType != 0 or header.AceFlags != 0x03 or mask != 0x001F01FF:
            raise ConfigurationError("Private Windows DACL ACE is not exact.")
        sid = sid_to_string(sid_pointer)
        if sid not in expected_sids or sid in actual_sids:
            raise ConfigurationError("Private Windows DACL trustee is unexpected.")
        actual_sids.add(sid)
    if actual_sids != expected_sids:
        raise ConfigurationError("Private Windows DACL trustees are incomplete.")
    return "windows_protected_dacl_current_user_system_administrators_full_control"


def _base_environment(config: RehearsalConfig, run_root: Path) -> dict[str, str]:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        def trusted_windows_directory(function_name: str) -> str:
            function = getattr(kernel32, function_name)
            function.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
            function.restype = ctypes.c_uint
            buffer = ctypes.create_unicode_buffer(32768)
            length = function(buffer, len(buffer))
            if not length or length >= len(buffer):
                raise ConfigurationError(
                    f"Trusted Windows path cannot be determined by {function_name}."
                )
            path = Path(buffer.value)
            try:
                checked = _existing_path(
                    path,
                    label=f"Trusted Windows path from {function_name}",
                    directory=True,
                )
            except ConfigurationError:
                raise
            if _is_reparse_point(checked):
                raise ConfigurationError("Trusted Windows path must not be a reparse point.")
            return str(checked)

        system_root = trusted_windows_directory("GetWindowsDirectoryW")
        system32 = trusted_windows_directory("GetSystemDirectoryW")
        command_processor = _existing_path(
            Path(system32) / "cmd.exe",
            label="Trusted Windows command processor",
            directory=False,
        )
        path_value = system32
    else:
        system_root = ""
        system32 = ""
        command_processor = None
        path_value = os.pathsep.join(("/usr/bin", "/bin"))
    env = {
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONIOENCODING": "utf-8",
        "FFXIVSHARE_ENV_FILE": str(run_root / "runtime-empty.env"),
        # Every Python child is launched by the already validated absolute
        # interpreter path. Keeping the virtualenv Scripts directory out of PATH
        # prevents an unbound helper executable from shadowing an operating-system
        # command used by a dependency.
        "PATH": path_value,
        "TEMP": str(run_root / "tmp"),
        "TMP": str(run_root / "tmp"),
    }
    if os.name == "nt":
        env.update({
            "SystemRoot": system_root,
            "WINDIR": system_root,
            "COMSPEC": str(command_processor),
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        })
    return env


def _django_environment(
    config: RehearsalConfig,
    run_root: Path,
    database: Path,
) -> dict[str, str]:
    if _canonical_key(database) == _canonical_key(config.source_database):
        raise RehearsalError("source_database_forbidden_for_django")
    env = _base_environment(config, run_root)
    env.update({
        "APP_ENV": "development",
        "DEBUG": "0",
        "DATABASE_ENGINE": "sqlite",
        "DATABASE_PATH": str(database),
        "SQLITE_TIMEOUT": "30",
        "SQLITE_TRANSACTION_MODE": "IMMEDIATE",
        "SQLITE_JOURNAL_MODE": "DELETE",
        "SQLITE_SYNCHRONOUS": "FULL",
        "MEDIA_ROOT": str(run_root / "scratch-media"),
        "APP_VERSION": "migration-rehearsal",
    })
    return env


def _prepare_config(
    config: RehearsalConfig,
    *,
    bootstrap_context: BootstrapInnerContext | None = None,
) -> RehearsalConfig:
    if not config.confirm_source_immutable:
        raise ConfigurationError("Source immutable-snapshot confirmation is required.")
    if not config.confirm_target_media_offline:
        raise ConfigurationError("Target offline-media confirmation is required.")
    if POLICY_ID_PATTERN.fullmatch(config.target_media_snapshot_id) is None:
        raise ConfigurationError("Target media snapshot ID is invalid.")

    def prepare_external_path(
        raw: str | Path,
        *,
        label: str,
        directory: bool,
    ) -> Path:
        if bootstrap_context is None:
            return _existing_path(raw, label=label, directory=directory)
        return _absolute_path(raw, label=label)

    resolved = RehearsalConfig(
        repository_root=_existing_path(config.repository_root, label="Repository root", directory=True),
        python_executable=_existing_path(config.python_executable, label="Python executable", directory=False),
        source_database=prepare_external_path(
            config.source_database,
            label="Source database",
            directory=False,
        ),
        source_checksum=prepare_external_path(
            config.source_checksum,
            label="Source checksum",
            directory=False,
        ),
        source_metadata=prepare_external_path(
            config.source_metadata,
            label="Source metadata",
            directory=False,
        ),
        source_upgrade_policy=_existing_path(config.source_upgrade_policy, label="Source upgrade policy", directory=False),
        source_media_manifest=prepare_external_path(
            config.source_media_manifest,
            label="Source media manifest",
            directory=False,
        ),
        target_media_root=prepare_external_path(
            config.target_media_root,
            label="Target media root",
            directory=True,
        ),
        target_media_snapshot_id=config.target_media_snapshot_id,
        run_root=_absolute_path(config.run_root, label="Run root"),
        confirm_source_immutable=True,
        confirm_target_media_offline=True,
        source_policy_proposal=(
            _existing_path(
                config.source_policy_proposal,
                label="Source policy proposal",
                directory=False,
            )
            if config.source_policy_proposal is not None
            else None
        ),
        source_review_record=(
            _existing_path(
                config.source_review_record,
                label="Source policy review record",
                directory=False,
            )
            if config.source_review_record is not None
            else None
        ),
        source_proposal_run_root=(
            _existing_path(
                config.source_proposal_run_root,
                label="Source proposal RunRoot",
                directory=True,
            )
            if config.source_proposal_run_root is not None
            else None
        ),
    )
    approval_inputs = (
        resolved.source_policy_proposal,
        resolved.source_review_record,
        resolved.source_proposal_run_root,
    )
    if bootstrap_context is not None and any(value is None for value in approval_inputs):
        raise ConfigurationError(
            "Bootstrap rehearsal requires proposal, review, and proposal RunRoot inputs."
        )
    if bootstrap_context is None and any(value is not None for value in approval_inputs):
        raise ConfigurationError(
            "Approval inputs are accepted only through the frozen bootstrap workflow."
        )
    _existing_path(
        resolved.python_executable.parent.parent / "pyvenv.cfg",
        label="Python virtual-environment marker",
        directory=False,
    )
    loaded_orchestrator = _existing_path(
        Path(__file__),
        label="Loaded rehearsal orchestrator",
        directory=False,
    )
    expected_orchestrator = _existing_path(
        (
            bootstrap_context.run_root / "code"
            if bootstrap_context is not None
            else resolved.repository_root
        )
        / "ops"
        / "migration"
        / "Rehearse-ProductionCopy.py",
        label="Repository rehearsal orchestrator",
        directory=False,
    )
    if _canonical_key(loaded_orchestrator) != _canonical_key(expected_orchestrator):
        raise ConfigurationError(
            "Loaded rehearsal orchestrator does not belong to the repository root."
        )
    if bootstrap_context is None:
        expected_checksum = resolved.source_database.with_name(
            f"{resolved.source_database.name}.sha256"
        )
        expected_metadata = resolved.source_database.with_name(
            f"{resolved.source_database.name}.metadata.json"
        )
        if (
            _canonical_key(resolved.source_checksum) != _canonical_key(expected_checksum)
            or _canonical_key(resolved.source_metadata) != _canonical_key(expected_metadata)
        ):
            raise ConfigurationError("Source backup sidecars do not use the required names.")
        try:
            source_metadata = os.lstat(resolved.source_database)
        except OSError as exc:
            raise ConfigurationError("Source database identity cannot be inspected.") from exc
        if source_metadata.st_nlink != 1:
            raise ConfigurationError("Source database must not have hard-link aliases.")
        for suffix in ("-wal", "-shm", "-journal"):
            if os.path.lexists(Path(f"{resolved.source_database}{suffix}")):
                raise ConfigurationError("Source database has a live SQLite sidecar.")
    fixed_tools = (
        resolved.repository_root / "manage.py",
        resolved.repository_root / "ops" / "migration" / "Verify-SQLiteBackupSet.py",
        resolved.repository_root / "ops" / "migration" / "Inspect-SQLiteSnapshot.py",
        resolved.repository_root / "ops" / "migration" / "Compare-SiteDataExports.py",
        resolved.repository_root / "ops" / "migration" / "MediaManifest.py",
    )
    for tool in fixed_tools:
        checked_tool = _existing_path(tool, label="Required migration tool", directory=False)
        if not _is_within(checked_tool, resolved.repository_root):
            raise ConfigurationError("Required migration tool escaped the repository root.")
    if bootstrap_context is None:
        if os.path.lexists(resolved.run_root):
            raise ConfigurationError("Run root already exists; refusing to reuse it.")
        _assert_no_reparse_components(resolved.run_root, include_leaf=False)
    else:
        if _canonical_key(resolved.run_root) != _canonical_key(bootstrap_context.run_root):
            raise ConfigurationError("Rehearsal RunRoot does not match its bootstrap.")
        _assert_no_reparse_components(resolved.run_root, include_leaf=True)
    try:
        parent = resolved.run_root.parent.resolve(strict=True)
    except OSError as exc:
        raise ConfigurationError("Run root parent must be an existing directory.") from exc
    if not parent.is_dir():
        raise ConfigurationError("Run root parent must be an existing directory.")
    forbidden_directories: set[Path] = set()
    if bootstrap_context is None:
        forbidden_directories.update({
            resolved.source_database.parent,
            resolved.source_media_manifest.parent,
            resolved.target_media_root,
        })
        forbidden_directories.add(resolved.repository_root)
        forbidden_directories.add(resolved.source_upgrade_policy.parent)
    if resolved.source_proposal_run_root is not None:
        forbidden_directories.add(resolved.source_proposal_run_root)
    for directory in forbidden_directories:
        if _is_within(resolved.run_root, directory) or _is_within(
            directory,
            resolved.run_root,
        ):
            raise ConfigurationError(
                "Run root must be outside the repository and immutable input directories."
            )
    return resolved


def _create_run_root(config: RehearsalConfig) -> str:
    try:
        os.mkdir(config.run_root, mode=0o700)
    except FileExistsError as exc:
        raise ConfigurationError("Run root appeared concurrently; refusing to reuse it.") from exc
    access_control = _secure_run_root(config.run_root)
    for name in (
        "artifacts",
        "code",
        "evidence",
        "logs",
        "scratch-media",
        "target",
        "tmp",
        "work",
    ):
        (config.run_root / name).mkdir()
    empty_environment = config.run_root / "runtime-empty.env"
    descriptor = os.open(
        empty_environment,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.flush()
        os.fsync(stream.fileno())
    return access_control


class Rehearsal:
    def __init__(
        self,
        config: RehearsalConfig,
        runner: Runner,
        *,
        workspace_access_control: str,
        bootstrap_context: BootstrapInnerContext | None = None,
    ):
        self.config = config
        self.runner = runner
        self.root = config.run_root
        self.execution_root = self.root / "code"
        self.artifacts = self.root / "artifacts"
        self.evidence = self.root / "evidence"
        self.logs = self.root / "logs"
        self.work_database = self.root / "work" / "source-upgrade.sqlite3"
        self.target_database = self.root / "target" / "ffxivshare.sqlite3"
        self.ledger = EvidenceLedger(self.evidence / "events.jsonl")
        self.source_expected_sha256: str | None = None
        self.source_initial_identity: tuple[int, int, int, int, int] | None = None
        self.source_sidecar_checkpoints: dict[
            str, tuple[int, str, tuple[int, int, int, int, int]]
        ] = {}
        self.source_final_recorded = False
        self.source_database_unchanged: bool | None = None
        self.production_copy_read_performed = False
        self.execution_bundle_expected: str | None = None
        self.execution_bundle_files: tuple[str, ...] = ()
        self.runtime_fingerprint_expected: str | None = None
        self.runtime_fingerprint_report_path: Path | None = None
        self.runtime_fingerprint_report_size: int | None = None
        self.runtime_fingerprint_report_sha256: str | None = None
        self.runtime_fingerprint_report_identity: (
            tuple[int, int, int, int, int] | None
        ) = None
        self.external_handoff: dict[str, Any] | None = None
        self.external_handoff_path: Path | None = None
        self.external_handoff_sha256: str | None = None
        self.external_handoff_identity: (
            tuple[int, int, int, int, int] | None
        ) = None
        self.external_handoff_preflight_baseline: dict[str, Any] | None = None
        self.external_handoff_active_target_slot: str | None = None
        self.external_handoff_verifier: Any | None = None
        self.deployment_candidate_details: dict[str, Any] | None = None
        self.workspace_access_control = workspace_access_control
        self.bootstrap_context = bootstrap_context
        self.command_number = 0
        self.run_directory_identities = {
            path: (os.stat(path).st_dev, os.stat(path).st_ino)
            for path in (
                self.root,
                self.artifacts,
                self.execution_root,
                self.evidence,
                self.logs,
                self.root / "scratch-media",
                self.root / "target",
                self.root / "tmp",
                self.root / "work",
            )
        }

    def _assert_run_tree_unchanged(self) -> None:
        for path, expected_identity in self.run_directory_identities.items():
            try:
                metadata = os.lstat(path)
            except OSError as exc:
                raise RehearsalError("run_directory_changed") from exc
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or _is_reparse_point(path)
                or (metadata.st_dev, metadata.st_ino) != expected_identity
            ):
                raise RehearsalError("run_directory_changed")

    def _assert_runtime_fingerprint_report_unchanged(self) -> None:
        if self.runtime_fingerprint_report_path is None:
            return
        if (
            self.runtime_fingerprint_report_size is None
            or self.runtime_fingerprint_report_sha256 is None
            or self.runtime_fingerprint_report_identity is None
        ):
            raise RehearsalError("runtime_fingerprint_report_authority_missing")
        size, digest, identity = _regular_file_checkpoint(
            self.runtime_fingerprint_report_path,
            issue_prefix="runtime_fingerprint_report",
            expected_sha256=self.runtime_fingerprint_report_sha256,
            expected_identity=self.runtime_fingerprint_report_identity,
        )
        if size != self.runtime_fingerprint_report_size:
            raise RehearsalBlocked("runtime_fingerprint_report_size_changed")
        if digest != self.runtime_fingerprint_report_sha256:
            raise RehearsalBlocked("runtime_fingerprint_report_sha256_changed")
        if identity != self.runtime_fingerprint_report_identity:
            raise RehearsalBlocked("runtime_fingerprint_report_identity_changed")

    def _command(
        self,
        stage: str,
        argv: list[str],
        *,
        env: dict[str, str],
        blocked_code: str | None = None,
    ) -> CommandResult:
        self._assert_run_tree_unchanged()
        self._assert_runtime_fingerprint_report_unchanged()
        if (
            self.execution_bundle_expected is not None
            and _execution_snapshot_sha256(
                self.execution_root,
                self.execution_bundle_files,
            )
            != self.execution_bundle_expected
        ):
            raise RehearsalBlocked(
                "execution_bundle_changed_before_command",
                stage=stage,
            )
        forbidden_inputs = [
            self.config.source_database,
            self.config.source_checksum,
            self.config.source_metadata,
            self.config.source_media_manifest,
        ]
        if self.bootstrap_context is None:
            forbidden_inputs.append(self.config.source_upgrade_policy)
        forbidden_input_keys = {
            os.path.normcase(os.path.abspath(path)) for path in forbidden_inputs
        }
        absolute_arguments = {
            os.path.normcase(os.path.abspath(argument))
            for argument in argv
            if os.path.isabs(argument)
        }
        if forbidden_input_keys & absolute_arguments:
            raise RehearsalError("immutable_input_passed_to_child_command")
        configured_database = env.get("DATABASE_PATH")
        if (
            configured_database is not None
            and os.path.normcase(os.path.abspath(configured_database))
            in forbidden_input_keys
        ):
            raise RehearsalError("immutable_input_forbidden_for_django")
        self.command_number += 1
        prefix = f"{self.command_number:02d}-{stage}"
        try:
            result = self.runner.run(
                stage=stage,
                argv=argv,
                cwd=self.execution_root,
                env=env,
                stdout_path=self.logs / f"{prefix}.stdout.txt",
                stderr_path=self.logs / f"{prefix}.stderr.txt",
            )
        except KeyboardInterrupt as exc:
            raise RehearsalInterrupted("keyboard_interrupt", stage=stage) from exc
        self._assert_run_tree_unchanged()
        self._assert_runtime_fingerprint_report_unchanged()
        if result.returncode != 0:
            if blocked_code is not None:
                raise RehearsalBlocked(blocked_code, stage=stage)
            raise RehearsalError(f"command_failed_{stage}", stage=stage)
        return result

    def _python_tool(self, name: str) -> str:
        path = self.execution_root / "ops" / "migration" / name
        return str(path)

    def _manage(self, database: Path, *arguments: str) -> tuple[list[str], dict[str, str]]:
        if _canonical_key(database) == _canonical_key(self.config.source_database):
            raise RehearsalError("source_database_forbidden_for_django")
        return (
            [
                str(self.config.python_executable),
                "-E",
                "-s",
                "-B",
                "-X",
                "utf8",
                str(self.execution_root / "manage.py"),
                *arguments,
            ],
            _django_environment(self.config, self.root, database),
        )

    def _record_artifact(self, stage: str, path: Path, **details: Any) -> None:
        self._assert_run_tree_unchanged()
        self._assert_runtime_fingerprint_report_unchanged()
        self.ledger.record(
            stage,
            "passed",
            {"artifact": _artifact_reference(path, self.root), **details},
        )

    def _load_external_handoff_verifier(self) -> Any:
        if self.external_handoff_verifier is not None:
            return self.external_handoff_verifier
        stage = "external_handoff_preflight_verified"
        module_path = (
            self.execution_root
            / "ops"
            / "migration"
            / "ProductionCopyHandoff.py"
        )
        try:
            if (
                self.execution_bundle_expected is None
                or _execution_snapshot_sha256(
                    self.execution_root,
                    self.execution_bundle_files,
                )
                != self.execution_bundle_expected
            ):
                raise RuntimeError("Frozen execution bundle changed.")
            module_path = _existing_path(
                module_path,
                label="Frozen production-copy handoff verifier",
                directory=False,
            )
            if not _is_within(module_path, self.execution_root):
                raise RuntimeError("Frozen handoff verifier escaped the execution root.")
            module_name = (
                f"ffxivshare_frozen_handoff_rehearsal_{os.getpid()}_{id(self):x}"
            )
            specification = importlib.util.spec_from_file_location(
                module_name,
                module_path,
            )
            if specification is None or specification.loader is None:
                raise RuntimeError("Frozen handoff verifier cannot be loaded.")
            module = importlib.util.module_from_spec(specification)
            sys.modules[module_name] = module
            try:
                specification.loader.exec_module(module)
            except Exception:
                sys.modules.pop(module_name, None)
                raise
            if not all(
                callable(getattr(module, name, None))
                for name in (
                    "load_handoff",
                    "verify_live_handoff",
                    "compare_access_baselines",
                )
            ) or not isinstance(getattr(module, "HandoffError", None), type):
                raise RuntimeError("Frozen handoff verifier API is incomplete.")
        except Exception as exc:
            raise RehearsalBlocked(
                "external_handoff_verifier_invalid",
                stage=stage,
            ) from exc
        self.external_handoff_verifier = module
        return module

    def _verify_external_handoff_preflight(
        self,
        *,
        expected_proposal_sha256: str,
    ) -> None:
        stage = "external_handoff_preflight_verified"
        if (
            self.bootstrap_context is None
            or self.bootstrap_context.repository_root is None
            or self.config.source_policy_proposal is None
            or self.config.source_proposal_run_root is None
        ):
            raise RehearsalBlocked(
                "external_handoff_approval_context_missing",
                stage=stage,
            )
        # From this point onward the run can read the production handoff and
        # external DB/media path metadata. Any failure must therefore retain
        # the RunRoot under the production-data disposal policy.
        self.production_copy_read_performed = True
        verifier = self._load_external_handoff_verifier()
        handoff_path = (
            self.config.source_proposal_run_root
            / "artifacts"
            / "source-handoff-manifest.json"
        )
        try:
            _, proposal_sha256, proposal_identity = _regular_file_checkpoint(
                self.config.source_policy_proposal,
                issue_prefix="approved_proposal",
                expected_sha256=expected_proposal_sha256,
                maximum_size=32 * 1024 * 1024,
            )
            proposal = _load_json(
                self.config.source_policy_proposal,
                maximum_size=32 * 1024 * 1024,
            )
            _regular_file_checkpoint(
                self.config.source_policy_proposal,
                issue_prefix="approved_proposal",
                expected_sha256=proposal_sha256,
                expected_identity=proposal_identity,
            )
            handoff_reference = proposal["body"]["evidence"][
                "source_handoff_manifest"
            ]
            if (
                not isinstance(handoff_reference, dict)
                or set(handoff_reference) != {"path", "sha256", "size"}
                or handoff_reference["path"]
                != "artifacts/source-handoff-manifest.json"
                or not isinstance(handoff_reference["sha256"], str)
                or SHA256_PATTERN.fullmatch(handoff_reference["sha256"]) is None
                or not isinstance(handoff_reference["size"], int)
                or isinstance(handoff_reference["size"], bool)
                or handoff_reference["size"] <= 0
            ):
                raise RuntimeError("Approved handoff artifact reference is invalid.")
        except Exception as exc:
            raise RehearsalBlocked(
                "external_handoff_approval_binding_invalid",
                stage=stage,
            ) from exc
        try:
            handoff_path = _existing_path(
                handoff_path,
                label="Approved source handoff manifest",
                directory=False,
            )
            handoff_size, handoff_sha256, handoff_identity = (
                _regular_file_checkpoint(
                    handoff_path,
                    issue_prefix="external_handoff_manifest",
                    maximum_size=handoff_reference["size"],
                )
            )
            if (
                handoff_size != handoff_reference["size"]
                or handoff_sha256 != handoff_reference["sha256"]
            ):
                raise RuntimeError("Approved handoff artifact reference changed.")
            handoff = verifier.load_handoff(handoff_path)
            _regular_file_checkpoint(
                handoff_path,
                issue_prefix="external_handoff_manifest",
                expected_sha256=handoff_sha256,
                expected_identity=handoff_identity,
            )
        except Exception as exc:
            raise RehearsalBlocked(
                "external_handoff_manifest_invalid",
                stage=stage,
            ) from exc

        try:
            configured_paths = {
                "database": _existing_path(
                    self.config.source_database,
                    label="Source database",
                    directory=False,
                ),
                "checksum": _existing_path(
                    self.config.source_checksum,
                    label="Source checksum",
                    directory=False,
                ),
                "metadata": _existing_path(
                    self.config.source_metadata,
                    label="Source metadata",
                    directory=False,
                ),
                "source_media_manifest": _existing_path(
                    self.config.source_media_manifest,
                    label="Source media manifest",
                    directory=False,
                ),
            }
            configured_target_media_root = _existing_path(
                self.config.target_media_root,
                label="Target media root",
                directory=True,
            )
        except ConfigurationError as exc:
            raise RehearsalBlocked(
                "external_handoff_configuration_invalid",
                stage=stage,
            ) from exc
        handoff_paths = {
            "database": handoff["database_backup_set"]["database"]["path"],
            "checksum": handoff["database_backup_set"]["checksum"]["path"],
            "metadata": handoff["database_backup_set"]["metadata"]["path"],
            "source_media_manifest": handoff["source_media"]["manifest"]["path"],
        }
        if any(
            _canonical_key(configured_paths[name])
            != _canonical_key(Path(handoff_paths[name]))
            for name in configured_paths
        ):
            raise RehearsalBlocked(
                "external_handoff_configuration_mismatch",
                stage=stage,
            )
        active_targets = [
            target
            for target in handoff["rehearsal_targets"]
            if _canonical_key(Path(target["path"]))
            == _canonical_key(configured_target_media_root)
            and target["snapshot_id"] == self.config.target_media_snapshot_id
        ]
        if len(active_targets) != 1:
            raise RehearsalBlocked(
                "external_handoff_target_mismatch",
                stage=stage,
            )
        active_target_slot = active_targets[0]["slot"]
        try:
            access_baseline = verifier.verify_live_handoff(
                handoff,
                self.bootstrap_context.repository_root,
                disallowed_roots=(
                    self.execution_root,
                    self.root,
                    self.config.source_proposal_run_root,
                    handoff_path,
                ),
                verify_content=False,
            )
            _regular_file_checkpoint(
                handoff_path,
                issue_prefix="external_handoff_manifest",
                expected_sha256=handoff_sha256,
                expected_identity=handoff_identity,
            )
        except Exception as exc:
            raise RehearsalBlocked(
                "external_handoff_preflight_invalid",
                stage=stage,
            ) from exc

        report_path = self.evidence / "external-handoff-preflight.json"
        try:
            _write_json_create_new(
                report_path,
                {
                    "format": "ffxivshare-production-copy-external-handoff-verification",
                    "format_version": 1,
                    "generated_at": _utc_now(),
                    "phase": "preflight",
                    "handoff_sha256": handoff_sha256,
                    "access_baseline": access_baseline,
                    "limitations": handoff["limitations"],
                },
            )
        except Exception as exc:
            raise RehearsalError(
                "external_handoff_preflight_evidence_write_failed",
                stage=stage,
            ) from exc
        self._record_artifact(
            stage,
            report_path,
            active_target_slot=active_target_slot,
            handoff_sha256=handoff_sha256,
        )
        self.external_handoff = handoff
        self.external_handoff_path = handoff_path
        self.external_handoff_sha256 = handoff_sha256
        self.external_handoff_identity = handoff_identity
        self.external_handoff_preflight_baseline = access_baseline
        self.external_handoff_active_target_slot = active_target_slot

    def _verify_external_handoff_final(self) -> None:
        stage = "external_handoff_final_verified"
        verifier = self.external_handoff_verifier
        if (
            verifier is None
            or self.bootstrap_context is None
            or self.bootstrap_context.repository_root is None
            or self.external_handoff is None
            or self.external_handoff_path is None
            or self.external_handoff_sha256 is None
            or self.external_handoff_identity is None
            or self.external_handoff_preflight_baseline is None
            or self.external_handoff_active_target_slot is None
        ):
            raise RehearsalError("external_handoff_preflight_missing", stage=stage)
        try:
            _regular_file_checkpoint(
                self.external_handoff_path,
                issue_prefix="external_handoff_manifest",
                expected_sha256=self.external_handoff_sha256,
                expected_identity=self.external_handoff_identity,
            )
            access_baseline = verifier.verify_live_handoff(
                self.external_handoff,
                self.bootstrap_context.repository_root,
                disallowed_roots=(
                    self.execution_root,
                    self.root,
                    self.config.source_proposal_run_root,
                    self.external_handoff_path,
                ),
                verify_content=False,
            )
            verifier.compare_access_baselines(
                self.external_handoff_preflight_baseline,
                access_baseline,
            )
            _regular_file_checkpoint(
                self.external_handoff_path,
                issue_prefix="external_handoff_manifest",
                expected_sha256=self.external_handoff_sha256,
                expected_identity=self.external_handoff_identity,
            )
        except Exception as exc:
            raise RehearsalBlocked(
                "external_handoff_final_invalid",
                stage=stage,
            ) from exc

        report_path = self.evidence / "external-handoff-final.json"
        try:
            _write_json_create_new(
                report_path,
                {
                    "format": "ffxivshare-production-copy-external-handoff-verification",
                    "format_version": 1,
                    "generated_at": _utc_now(),
                    "phase": "final",
                    "handoff_sha256": self.external_handoff_sha256,
                    "access_baseline": access_baseline,
                    "limitations": self.external_handoff["limitations"],
                },
            )
        except Exception as exc:
            raise RehearsalError(
                "external_handoff_final_evidence_write_failed",
                stage=stage,
            ) from exc
        self._record_artifact(
            stage,
            report_path,
            active_target_slot=self.external_handoff_active_target_slot,
            handoff_sha256=self.external_handoff_sha256,
        )

    def _verify_runtime_fingerprint(
        self,
        stage: str,
        *,
        env: dict[str, str],
        establish_expected: bool = False,
        full_content_hash: bool = False,
    ) -> dict[str, Any]:
        output = self.evidence / f"{stage.replace('_', '-')}.json"
        if self.runtime_fingerprint_report_path is not None:
            self._assert_runtime_fingerprint_report_unchanged()
        if self.runtime_fingerprint_report_path is None or full_content_hash:
            self._command(
                stage,
                [
                    str(self.config.python_executable),
                    "-E",
                    "-s",
                    "-B",
                    "-X",
                    "utf8",
                    "-c",
                    _compressed_python_command(
                        RUNTIME_FINGERPRINT_SCRIPT,
                        filename="<ffxivshare-runtime-fingerprint>",
                    ),
                    str(output),
                ],
                env=env,
            )
            (
                report_raw,
                report_size,
                report_sha256,
                report_identity,
            ) = _read_stable_regular_bytes(
                output,
                maximum_size=64 * 1024 * 1024,
                issue_prefix="runtime_fingerprint_report",
            )
            report = _validate_runtime_fingerprint_bytes(report_raw)
            (
                _confirmed_raw,
                confirmed_size,
                confirmed_sha256,
                confirmed_identity,
            ) = _read_stable_regular_bytes(
                    output,
                    maximum_size=64 * 1024 * 1024,
                    issue_prefix="runtime_fingerprint_report",
                    expected_sha256=report_sha256,
                    expected_identity=report_identity,
                )
            if (
                confirmed_size != report_size
                or confirmed_sha256 != report_sha256
                or confirmed_identity != report_identity
            ):
                raise RehearsalBlocked(
                    "runtime_fingerprint_report_changed_during_validation",
                    stage=stage,
                )
            if self.runtime_fingerprint_expected is None:
                if not establish_expected:
                    raise RehearsalError(
                        "runtime_fingerprint_expected_missing",
                        stage=stage,
                    )
                self.runtime_fingerprint_expected = report["fingerprint_sha256"]
            elif report["fingerprint_sha256"] != self.runtime_fingerprint_expected:
                raise RehearsalBlocked("runtime_fingerprint_mismatch", stage=stage)
            if self.runtime_fingerprint_report_path is None:
                self.runtime_fingerprint_report_size = report_size
                self.runtime_fingerprint_report_sha256 = report_sha256
                self.runtime_fingerprint_report_identity = report_identity
                self.runtime_fingerprint_report_path = output
            else:
                self._assert_runtime_fingerprint_report_unchanged()
            details = {
                "runtime_fingerprint_sha256": report["fingerprint_sha256"],
                "content_rehashed": True,
                "runtime_fingerprint_report_size": report_size,
                "runtime_fingerprint_report_sha256": report_sha256,
                "runtime_fingerprint_report_identity": {
                    "device": report_identity[0],
                    "inode": report_identity[1],
                    "size": report_identity[2],
                    "mtime_ns": report_identity[3],
                    "ctime_ns": report_identity[4],
                },
            }
        else:
            if (
                self.runtime_fingerprint_expected is None
                or self.runtime_fingerprint_report_sha256 is None
            ):
                raise RehearsalError("runtime_fingerprint_report_authority_missing")
            self._command(
                stage,
                [
                    str(self.config.python_executable),
                    "-E",
                    "-s",
                    "-B",
                    "-X",
                    "utf8",
                    "-c",
                    _compressed_python_command(
                        RUNTIME_IDENTITY_CHECKPOINT_SCRIPT,
                        filename="<ffxivshare-runtime-checkpoint>",
                    ),
                    str(self.runtime_fingerprint_report_path),
                    str(self.runtime_fingerprint_expected),
                    self.runtime_fingerprint_report_sha256,
                    str(output),
                ],
                env=env,
            )
            self._assert_runtime_fingerprint_report_unchanged()
            report = _validate_runtime_identity_checkpoint(
                output,
                expected_fingerprint_sha256=str(self.runtime_fingerprint_expected),
                expected_source_report_sha256=self.runtime_fingerprint_report_sha256,
            )
            self._assert_runtime_fingerprint_report_unchanged()
            details = {
                "runtime_fingerprint_sha256": report["fingerprint_sha256"],
                "content_rehashed": False,
                "runtime_fingerprint_report_size": self.runtime_fingerprint_report_size,
                "runtime_fingerprint_report_sha256": self.runtime_fingerprint_report_sha256,
                "identity_inventory_checked": report["identity_inventory_checked"],
                "closure_scopes_checked": report["closure_scopes_checked"],
            }
        self._record_artifact(
            f"{stage}_verified",
            output,
            **details,
        )
        self._assert_runtime_fingerprint_report_unchanged()
        return report

    def _record_source_final(self) -> None:
        if self.source_final_recorded or self.source_expected_sha256 is None:
            return
        self._assert_run_tree_unchanged()
        if self.source_initial_identity is None:
            self.source_final_recorded = True
            self.source_database_unchanged = None
            self.ledger.record(
                "source_final_verified",
                "not_proven",
                {
                    "source_unchanged": None,
                    "issue": "source_initial_identity_not_established",
                },
            )
            return
        try:
            size, digest, _identity = _source_snapshot_checkpoint(
                self.config.source_database,
                expected_sha256=self.source_expected_sha256,
                expected_identity=self.source_initial_identity,
            )
            final_inputs: dict[str, dict[str, Any]] = {
                "database": {"size": size, "sha256": digest}
            }
            for label, path in (
                ("checksum", self.config.source_checksum),
                ("metadata", self.config.source_metadata),
            ):
                expected = self.source_sidecar_checkpoints.get(label)
                if expected is None:
                    raise RehearsalError("source_sidecar_initial_checkpoint_missing")
                sidecar_size, sidecar_digest, _sidecar_identity = (
                    _regular_file_checkpoint(
                        path,
                        issue_prefix=f"source_{label}",
                        expected_sha256=expected[1],
                        expected_identity=expected[2],
                    )
                )
                if sidecar_size != expected[0]:
                    raise RehearsalBlocked(f"source_{label}_size_changed")
                final_inputs[label] = {
                    "size": sidecar_size,
                    "sha256": sidecar_digest,
                }
        except RehearsalError:
            self.ledger.record(
                "source_final_verified",
                "failed",
                {"source_unchanged": False},
            )
            self.source_final_recorded = True
            self.source_database_unchanged = False
            raise RehearsalBlocked("source_changed_during_rehearsal")
        self.source_final_recorded = True
        self.source_database_unchanged = True
        self.ledger.record(
            "source_final_verified",
            "passed",
            {"source_unchanged": True, "backup_set": final_inputs},
        )
        if self.execution_bundle_expected is not None:
            final_bundle = _execution_snapshot_sha256(
                self.execution_root,
                self.execution_bundle_files,
            )
            if final_bundle != self.execution_bundle_expected:
                self.ledger.record(
                    "execution_bundle_final_verified",
                    "failed",
                    {"bundle_unchanged": False},
                )
                raise RehearsalBlocked("execution_bundle_changed_during_rehearsal")
            self.ledger.record(
                "execution_bundle_final_verified",
                "passed",
                {
                    "bundle_unchanged": True,
                    "execution_bundle_sha256": final_bundle,
                },
            )

    def execute(self) -> None:
        config = self.config
        base_env = _base_environment(config, self.root)
        self.ledger.record(
            "created",
            "passed",
            {
                "run_root_create_new": True,
                "workspace_access_control": self.workspace_access_control,
                "network_isolation_enforced": False,
                "network_access_observation": "not_measured",
                "live_production_service_access_requested_by_orchestrator": False,
            },
        )

        if self.bootstrap_context is None:
            policy_copy = self.artifacts / "source-upgrade-policy.json"
            _copy_stable(config.source_upgrade_policy, policy_copy)
        else:
            policy_copy = config.source_upgrade_policy
        policy = _load_policy(policy_copy)
        self.runtime_fingerprint_expected = policy["runtime_fingerprint_sha256"]

        if self.bootstrap_context is None:
            repository_bundle = _execution_bundle_sha256(config.repository_root)
            if repository_bundle != policy["execution_bundle_sha256"]:
                raise RehearsalBlocked("policy_execution_bundle_sha256_mismatch")
            snapshot_bundle, snapshot_files = _create_execution_snapshot(
                config.repository_root,
                self.execution_root,
            )
            if snapshot_bundle != repository_bundle:
                raise RehearsalBlocked("execution_snapshot_bundle_mismatch")
            self.execution_bundle_expected = snapshot_bundle
            self.execution_bundle_files = snapshot_files
            self._verify_runtime_fingerprint(
                "runtime_fingerprint_initial",
                env=base_env,
            )
        else:
            if (
                self.bootstrap_context.execution_bundle_sha256
                != policy["execution_bundle_sha256"]
            ):
                raise RehearsalBlocked("policy_execution_bundle_sha256_mismatch")
            self.execution_bundle_expected = (
                self.bootstrap_context.execution_bundle_sha256
            )
            self.execution_bundle_files = self.bootstrap_context.execution_bundle_files
            self._verify_runtime_fingerprint(
                "runtime_fingerprint_initial",
                env=base_env,
            )
            if (
                config.source_policy_proposal is None
                or config.source_review_record is None
                or config.source_proposal_run_root is None
            ):
                raise RehearsalError("approval_inputs_missing")
            policy_size, policy_sha256 = _hash_stable(policy_copy)
            if policy_size <= 0:
                raise RehearsalBlocked("approved_policy_empty")
            approval_verification = self._command(
                "approved_policy_evidence_verification",
                [
                    str(config.python_executable),
                    "-I",
                    "-S",
                    "-B",
                    "-X",
                    "utf8",
                    str(
                        self.execution_root
                        / "ops"
                        / "migration"
                        / "Approve-ProductionCopyPolicy.py"
                    ),
                    "verify",
                    "--policy",
                    str(policy_copy),
                    "--proposal",
                    str(config.source_policy_proposal),
                    "--review",
                    str(config.source_review_record),
                    "--proposal-run-root",
                    str(config.source_proposal_run_root),
                    "--expected-policy-sha256",
                    policy_sha256,
                    "--expected-proposal-sha256",
                    policy["proposal_sha256"],
                    "--expected-review-sha256",
                    policy["review_record_sha256"],
                ],
                env=base_env,
                blocked_code="approved_policy_evidence_invalid",
            )
            self._record_artifact(
                "approved_policy_evidence_verified",
                approval_verification.stdout_path,
                policy_id=policy["policy_id"],
                policy_sha256=policy_sha256,
                proposal_sha256=policy["proposal_sha256"],
                review_record_sha256=policy["review_record_sha256"],
            )
            # Keep this as the first approved-mode DB/media live-path gate.
            # The trusted Approval replay above must finish before it runs.
            self._verify_external_handoff_preflight(
                expected_proposal_sha256=policy["proposal_sha256"],
            )

        media_manifest_copy = self.artifacts / "source-media-manifest.json"
        _copy_stable(config.source_media_manifest, media_manifest_copy)
        dependency_check = self._command(
            "runtime_dependencies_check",
            [
                str(config.python_executable),
                "-E",
                "-s",
                "-B",
                "-X",
                "utf8",
                "-m",
                "pip",
                "check",
            ],
            env=base_env,
            blocked_code="runtime_dependencies_inconsistent",
        )
        self._record_artifact(
            "runtime_dependencies_consistent",
            dependency_check.stdout_path,
            runtime_fingerprint_sha256=self.runtime_fingerprint_expected,
        )

        source_media_sha256, source_media_snapshot_id = (
            _source_media_manifest_identity(media_manifest_copy)
        )
        if source_media_sha256 != policy["source_media_manifest_sha256"]:
            raise RehearsalBlocked("policy_source_media_manifest_sha256_mismatch")
        if source_media_snapshot_id != policy["source_media_snapshot_id"]:
            raise RehearsalBlocked("policy_source_media_snapshot_id_mismatch")

        self.source_expected_sha256 = policy["source_database_sha256"]
        self.production_copy_read_performed = True
        source_size, source_digest, source_database_identity = (
            _source_snapshot_checkpoint(
                config.source_database,
                expected_sha256=self.source_expected_sha256,
                expected_identity=None,
            )
        )
        _assert_external_handoff_artifact_checkpoint(
            self.external_handoff,
            "database",
            source_size,
            source_digest,
        )

        source_input_directory = self.root / "work" / "source-input"
        source_input_directory.mkdir()
        private_source_database = source_input_directory / config.source_database.name
        private_source_checksum = source_input_directory / config.source_checksum.name
        private_source_metadata = source_input_directory / config.source_metadata.name
        source_sidecar_checkpoints: dict[
            str, tuple[int, str, tuple[int, int, int, int, int]]
        ] = {}
        copied_size, copied_digest = _copy_stable(
            config.source_database,
            private_source_database,
        )
        if copied_digest != self.source_expected_sha256 or copied_size != source_size:
            raise RehearsalBlocked("private_source_copy_does_not_match_source")

        for label, source, destination in (
            ("checksum", config.source_checksum, private_source_checksum),
            ("metadata", config.source_metadata, private_source_metadata),
        ):
            sidecar_size, sidecar_digest, sidecar_identity = _regular_file_checkpoint(
                source,
                issue_prefix=f"source_{label}",
            )
            source_sidecar_checkpoints[label] = (
                sidecar_size,
                sidecar_digest,
                sidecar_identity,
            )
            _assert_external_handoff_artifact_checkpoint(
                self.external_handoff,
                label,
                sidecar_size,
                sidecar_digest,
            )
            copied_sidecar_size, copied_sidecar_digest = _copy_stable(
                source,
                destination,
            )
            if (
                copied_sidecar_size != sidecar_size
                or copied_sidecar_digest != sidecar_digest
            ):
                raise RehearsalBlocked(f"private_source_{label}_copy_mismatch")

        # Bind the original immutable handoff once more after all three bytesets
        # have been copied. From here onward every child receives only private
        # RunRoot paths; the original handoff is touched again only at finalization.
        _source_snapshot_checkpoint(
            config.source_database,
            expected_sha256=self.source_expected_sha256,
            expected_identity=source_database_identity,
        )
        for label, path in (
            ("checksum", config.source_checksum),
            ("metadata", config.source_metadata),
        ):
            expected = source_sidecar_checkpoints[label]
            _regular_file_checkpoint(
                path,
                issue_prefix=f"source_{label}",
                expected_sha256=expected[1],
                expected_identity=expected[2],
            )
        # Publish the original backup-set checkpoint only after every member was
        # copied and re-read successfully. An interruption before this line is
        # reported as not proven, not as a false claim that the source changed.
        self.source_initial_identity = source_database_identity
        self.source_sidecar_checkpoints = source_sidecar_checkpoints
        self.ledger.record(
            "source_private_copy_verified",
            "passed",
            {
                "database": {"size": copied_size, "sha256": copied_digest},
                "checksum": {
                    "size": source_sidecar_checkpoints["checksum"][0],
                    "sha256": source_sidecar_checkpoints["checksum"][1],
                },
                "metadata": {
                    "size": source_sidecar_checkpoints["metadata"][0],
                    "sha256": source_sidecar_checkpoints["metadata"][1],
                },
                "children_receive_original_paths": False,
            },
        )

        source_backup_evidence = self.evidence / "source-backup-set.json"
        self._command(
            "source_artifacts_verified",
            [
                str(config.python_executable),
                "-E",
                "-s",
                "-B",
                "-X",
                "utf8",
                self._python_tool("Verify-SQLiteBackupSet.py"),
                "--database",
                str(private_source_database),
                "--checksum",
                str(private_source_checksum),
                "--metadata",
                str(private_source_metadata),
                "--output",
                str(source_backup_evidence),
            ],
            env=base_env,
        )
        backup_report = _validate_backup_report(source_backup_evidence)
        source_artifact = backup_report["artifact"]
        private_source_size, private_source_digest, private_source_identity = (
            _source_snapshot_checkpoint(
                private_source_database,
                expected_sha256=self.source_expected_sha256,
                expected_identity=None,
                issue_prefix="private_source",
            )
        )
        if (
            source_artifact["sha256"] != self.source_expected_sha256
            or private_source_size != source_artifact["size"]
            or private_source_digest != source_digest
        ):
            raise RehearsalBlocked("private_source_backup_verification_mismatch")
        self._record_artifact(
            "source_artifacts_verified",
            source_backup_evidence,
            source_sha256=source_digest,
            policy_id=policy["policy_id"],
            policy_sha256=_hash_stable(policy_copy)[1],
            lossless_reviewed=True,
            execution_bundle_sha256=self.execution_bundle_expected,
            source_media_manifest_sha256=source_media_sha256,
            source_media_snapshot_id=source_media_snapshot_id,
            verified_private_copy=True,
        )

        source_inspection = self.evidence / "source-inspection.json"
        self._command(
            "source_inspected",
            [
                str(config.python_executable),
                "-E",
                "-s",
                "-B",
                "-X",
                "utf8",
                self._python_tool("Inspect-SQLiteSnapshot.py"),
                "--database",
                str(private_source_database),
                "--expected-sha256",
                self.source_expected_sha256,
                "--output",
                str(source_inspection),
            ],
            env=base_env,
        )
        _source_snapshot_checkpoint(
            private_source_database,
            expected_sha256=self.source_expected_sha256,
            expected_identity=private_source_identity,
            issue_prefix="private_source",
        )
        _inspection, inspected_applied, source_sqlite_schema_sha256 = (
            _validate_inspection_report(
                source_inspection,
                expected_sha256=self.source_expected_sha256,
            )
        )
        if source_sqlite_schema_sha256 != policy["source_sqlite_schema_sha256"]:
            raise RehearsalBlocked("policy_source_sqlite_schema_sha256_mismatch")
        self._record_artifact(
            "source_inspected",
            source_inspection,
            applied_migration_count=len(inspected_applied),
            source_sqlite_schema_sha256=source_sqlite_schema_sha256,
        )

        copied_size, copied_digest = _copy_stable(
            private_source_database,
            self.work_database,
        )
        if copied_digest != self.source_expected_sha256 or copied_size != source_size:
            raise RehearsalBlocked("work_copy_does_not_match_source")
        _source_snapshot_checkpoint(
            private_source_database,
            expected_sha256=self.source_expected_sha256,
            expected_identity=private_source_identity,
            issue_prefix="private_source",
        )
        self.ledger.record(
            "work_copy_verified",
            "passed",
            {
                "size": copied_size,
                "sha256": copied_digest,
                "private_source_unchanged": True,
            },
        )

        pre_state_path = self.evidence / "source-migration-state-before.json"
        self._command(
            "source_schema_state",
            [
                str(config.python_executable),
                "-E",
                "-s",
                "-B",
                "-X",
                "utf8",
                "-c",
                MIGRATION_STATE_SCRIPT,
                str(pre_state_path),
            ],
            env=_django_environment(config, self.root, self.work_database),
        )
        pre_state = _validate_migration_state(pre_state_path)
        if pre_state["applied"] != inspected_applied:
            raise RehearsalBlocked("work_copy_migration_state_differs_from_inspection")
        if pre_state["applied_leaf_nodes"] != policy["source_leaf_nodes"]:
            raise RehearsalBlocked("policy_source_leaf_nodes_mismatch")
        applied_hash = _canonical_json_sha256(pre_state["applied"])
        if applied_hash != policy["source_applied_migrations_sha256"]:
            raise RehearsalBlocked("policy_source_applied_migrations_mismatch")
        if pre_state["migration_runtime_sha256"] != policy["migration_runtime_sha256"]:
            raise RehearsalBlocked("policy_migration_runtime_sha256_mismatch")
        self._record_artifact(
            "source_schema_classified",
            pre_state_path,
            applied_migrations_sha256=applied_hash,
            migration_runtime_sha256=pre_state["migration_runtime_sha256"],
            source_leaf_nodes=pre_state["applied_leaf_nodes"],
        )

        plan_argv, plan_env = self._manage(
            self.work_database,
            "migrate",
            "--plan",
            "--noinput",
            "--verbosity",
            "1",
        )
        plan_result = self._command("source_schema_plan", plan_argv, env=plan_env)
        plan_sha256 = _hash_stable(plan_result.stdout_path)[1]
        if plan_sha256 != policy["migration_plan_sha256"]:
            raise RehearsalBlocked("policy_migration_plan_sha256_mismatch")
        self._verify_runtime_fingerprint(
            "runtime_fingerprint_pre_migrate",
            env=base_env,
        )
        bundle_before_migrate = _execution_snapshot_sha256(
            self.execution_root,
            self.execution_bundle_files,
        )
        if bundle_before_migrate != policy["execution_bundle_sha256"]:
            raise RehearsalBlocked("execution_bundle_changed_before_migrate")
        self.ledger.record(
            "execution_bundle_pre_migrate_verified",
            "passed",
            {"execution_bundle_sha256": bundle_before_migrate},
        )

        migrate_argv, migrate_env = self._manage(
            self.work_database,
            "migrate",
            "--noinput",
            "--verbosity",
            "1",
        )
        self._command("source_schema_migrate", migrate_argv, env=migrate_env)
        self._verify_runtime_fingerprint(
            "runtime_fingerprint_post_migrate",
            env=base_env,
        )
        post_state_path = self.evidence / "source-migration-state-after.json"
        self._command(
            "source_schema_ready_state",
            [
                str(config.python_executable),
                "-E",
                "-s",
                "-B",
                "-X",
                "utf8",
                "-c",
                MIGRATION_STATE_SCRIPT,
                str(post_state_path),
            ],
            env=_django_environment(config, self.root, self.work_database),
        )
        post_state = _validate_migration_state(post_state_path)
        if post_state["migration_runtime_sha256"] != policy["migration_runtime_sha256"]:
            raise RehearsalBlocked("migration_runtime_changed_during_source_upgrade")
        if post_state["applied_leaf_nodes"] != policy["target_leaf_nodes"]:
            raise RehearsalBlocked("policy_target_leaf_nodes_mismatch_after_upgrade")
        self._record_artifact(
            "source_schema_ready",
            post_state_path,
            migration_plan_sha256=plan_sha256,
            target_leaf_nodes=post_state["applied_leaf_nodes"],
        )

        source_export = self.artifacts / "source-export"
        export_argv, export_env = self._manage(
            self.work_database,
            "export_site_data",
            str(source_export),
        )
        self._command("dataset_exported", export_argv, env=export_env)
        source_manifest = source_export / "manifest.json"
        self._record_artifact("dataset_exported", source_manifest)

        source_validation = self.evidence / "source-validation.json"
        validate_argv, validate_env = self._manage(
            self.work_database,
            "validate_site_data",
            str(source_export),
            "--report",
            str(source_validation),
        )
        self._command("dataset_validated", validate_argv, env=validate_env)
        _validate_validation_report(source_validation)
        self._record_artifact("dataset_validated", source_validation)

        target_migrate_argv, target_env = self._manage(
            self.target_database,
            "migrate",
            "--noinput",
            "--verbosity",
            "1",
        )
        self._command("target_schema_migrate", target_migrate_argv, env=target_env)
        target_state_path = self.evidence / "target-migration-state.json"
        self._command(
            "target_schema_state",
            [
                str(config.python_executable),
                "-E",
                "-s",
                "-B",
                "-X",
                "utf8",
                "-c",
                MIGRATION_STATE_SCRIPT,
                str(target_state_path),
            ],
            env=_django_environment(config, self.root, self.target_database),
        )
        target_state = _validate_migration_state(target_state_path)
        if target_state["migration_runtime_sha256"] != policy["migration_runtime_sha256"]:
            raise RehearsalBlocked("migration_runtime_changed_before_target_import")
        if target_state["applied_leaf_nodes"] != policy["target_leaf_nodes"]:
            raise RehearsalBlocked("target_schema_leaf_nodes_mismatch")
        self._record_artifact(
            "target_schema_created",
            target_state_path,
            target_leaf_nodes=target_state["applied_leaf_nodes"],
        )

        import_report = self.evidence / "target-import.json"
        import_argv, import_env = self._manage(
            self.target_database,
            "import_site_data",
            str(source_export),
            "--report",
            str(import_report),
            "--confirm-exclusive-target",
        )
        self._command("import_verified", import_argv, env=import_env)
        _validate_import_report(
            import_report,
            expected_status={"imported"},
            expected_target_state="empty",
        )
        self._record_artifact("import_verified", import_report)

        idempotence_report = self.evidence / "target-import-idempotence.json"
        idempotence_argv, idempotence_env = self._manage(
            self.target_database,
            "import_site_data",
            str(source_export),
            "--report",
            str(idempotence_report),
            "--confirm-exclusive-target",
        )
        self._command("idempotence_verified", idempotence_argv, env=idempotence_env)
        _validate_import_report(
            idempotence_report,
            expected_status={"already_imported"},
            expected_target_state="complete",
        )
        self._record_artifact("idempotence_verified", idempotence_report)

        target_export = self.artifacts / "target-export"
        target_export_argv, target_export_env = self._manage(
            self.target_database,
            "export_site_data",
            str(target_export),
        )
        self._command("target_dataset_exported", target_export_argv, env=target_export_env)
        target_validation = self.evidence / "target-validation.json"
        target_validate_argv, target_validate_env = self._manage(
            self.target_database,
            "validate_site_data",
            str(target_export),
            "--report",
            str(target_validation),
        )
        self._command("target_dataset_validated", target_validate_argv, env=target_validate_env)
        _validate_validation_report(target_validation)
        self._record_artifact("target_dataset_validated", target_validation)
        comparison = self.evidence / "site-data-comparison.json"
        self._command(
            "target_export_compared",
            [
                str(config.python_executable),
                "-E",
                "-s",
                "-B",
                "-X",
                "utf8",
                self._python_tool("Compare-SiteDataExports.py"),
                "--source",
                str(source_export),
                "--target",
                str(target_export),
                "--output",
                str(comparison),
            ],
            env=base_env,
            blocked_code="site_data_exports_not_equivalent",
        )
        _validate_comparison(comparison)
        self._record_artifact("target_export_compared", comparison)

        restriction_report = self.evidence / "restriction-preflight.json"
        restriction_argv, restriction_env = self._manage(
            self.target_database,
            "preflight_share_restrictions",
            "--strict",
            "--output",
            str(restriction_report),
        )
        self._command(
            "restriction_preflight",
            restriction_argv,
            env=restriction_env,
            blocked_code="restriction_preflight_requires_review",
        )
        _validate_restriction_preflight(restriction_report)
        self._record_artifact("restriction_preflight", restriction_report)

        target_backup_directory = self.artifacts / "target-backup"
        target_backup_directory.mkdir()
        target_backup = target_backup_directory / "ffxivshare.sqlite3"
        backup_argv, backup_env = self._manage(
            self.target_database,
            "backup_database",
            str(target_backup),
        )
        self._command("target_snapshot_backup", backup_argv, env=backup_env)
        target_backup_evidence = self.evidence / "target-backup-set.json"
        self._command(
            "target_snapshot_set_verified",
            [
                str(config.python_executable),
                "-E",
                "-s",
                "-B",
                "-X",
                "utf8",
                self._python_tool("Verify-SQLiteBackupSet.py"),
                "--database",
                str(target_backup),
                "--checksum",
                str(target_backup.with_name(f"{target_backup.name}.sha256")),
                "--metadata",
                str(target_backup.with_name(f"{target_backup.name}.metadata.json")),
                "--output",
                str(target_backup_evidence),
            ],
            env=base_env,
        )
        target_backup_report = _validate_backup_report(target_backup_evidence)
        target_backup_sha256 = target_backup_report["artifact"]["sha256"]
        target_backup_initial_references = _backup_set_artifact_references(
            target_backup,
            self.root,
        )
        if (
            target_backup_initial_references["database"]["sha256"]
            != target_backup_sha256
            or target_backup_initial_references["database"]["size"]
            != target_backup_report["artifact"]["size"]
        ):
            raise RehearsalBlocked("target_backup_set_reference_mismatch")
        target_backup_initial_evidence_reference = _artifact_reference(
            target_backup_evidence,
            self.root,
        )
        target_inspection = self.evidence / "target-backup-inspection.json"
        self._command(
            "target_snapshot_inspected",
            [
                str(config.python_executable),
                "-E",
                "-s",
                "-B",
                "-X",
                "utf8",
                self._python_tool("Inspect-SQLiteSnapshot.py"),
                "--database",
                str(target_backup),
                "--expected-sha256",
                target_backup_sha256,
                "--output",
                str(target_inspection),
            ],
            env=base_env,
        )
        _target_inspection, _target_applied, _target_sqlite_schema_sha256 = (
            _validate_inspection_report(
                target_inspection,
                expected_sha256=target_backup_sha256,
            )
        )
        self._record_artifact(
            "target_snapshot_verified",
            target_inspection,
            backup_sha256=target_backup_sha256,
            backup_set=target_backup_initial_references,
            backup_set_verification=target_backup_initial_evidence_reference,
        )

        final_verification_database = (
            self.root / "work" / "final-target-backup-verification.sqlite3"
        )
        final_size, final_digest = _copy_stable(
            target_backup,
            final_verification_database,
        )
        if final_digest != target_backup_sha256:
            raise RehearsalError("final_verification_copy_hash_mismatch")
        _final_initial_size, _final_initial_digest, final_verification_identity = (
            _source_snapshot_checkpoint(
                final_verification_database,
                expected_sha256=target_backup_sha256,
                expected_identity=None,
                issue_prefix="final_target_verification",
            )
        )
        final_state_path = self.evidence / "final-target-migration-state.json"
        self._command(
            "final_target_migration_state",
            [
                str(config.python_executable),
                "-E",
                "-s",
                "-B",
                "-X",
                "utf8",
                "-c",
                MIGRATION_STATE_SCRIPT,
                str(final_state_path),
            ],
            env=_django_environment(
                config,
                self.root,
                final_verification_database,
            ),
        )
        final_state = _validate_migration_state(final_state_path)
        if (
            final_state["migration_runtime_sha256"]
            != policy["migration_runtime_sha256"]
            or final_state["applied_leaf_nodes"] != policy["target_leaf_nodes"]
            or final_state["applied"] != target_state["applied"]
        ):
            raise RehearsalBlocked("final_target_backup_migration_state_mismatch")
        self._record_artifact(
            "final_target_migration_state_verified",
            final_state_path,
            backup_sha256=target_backup_sha256,
            verification_copy_size=final_size,
        )

        final_target_export = self.artifacts / "final-target-export"
        final_export_argv, final_export_env = self._manage(
            final_verification_database,
            "export_site_data",
            str(final_target_export),
        )
        self._command(
            "final_target_dataset_exported",
            final_export_argv,
            env=final_export_env,
        )
        final_validation = self.evidence / "final-target-validation.json"
        final_validate_argv, final_validate_env = self._manage(
            final_verification_database,
            "validate_site_data",
            str(final_target_export),
            "--report",
            str(final_validation),
        )
        self._command(
            "final_target_dataset_validated",
            final_validate_argv,
            env=final_validate_env,
        )
        _validate_validation_report(final_validation)
        self._record_artifact(
            "final_target_dataset_validated",
            final_validation,
            backup_sha256=target_backup_sha256,
        )
        final_comparison = self.evidence / "final-target-site-data-comparison.json"
        self._command(
            "final_target_export_compared",
            [
                str(config.python_executable),
                "-E",
                "-s",
                "-B",
                "-X",
                "utf8",
                self._python_tool("Compare-SiteDataExports.py"),
                "--source",
                str(source_export),
                "--target",
                str(final_target_export),
                "--output",
                str(final_comparison),
            ],
            env=base_env,
            blocked_code="final_target_export_not_equivalent",
        )
        _validate_comparison(final_comparison)
        self._record_artifact(
            "final_target_export_compared",
            final_comparison,
            backup_sha256=target_backup_sha256,
        )
        final_restriction = self.evidence / "final-target-restriction-preflight.json"
        final_restriction_argv, final_restriction_env = self._manage(
            final_verification_database,
            "preflight_share_restrictions",
            "--strict",
            "--output",
            str(final_restriction),
        )
        self._command(
            "final_target_restriction_preflight",
            final_restriction_argv,
            env=final_restriction_env,
            blocked_code="final_target_restriction_preflight_requires_review",
        )
        _validate_restriction_preflight(final_restriction)
        self._record_artifact(
            "final_target_restriction_preflight",
            final_restriction,
            backup_sha256=target_backup_sha256,
        )
        final_checkpoint_size, final_checkpoint_digest, _final_identity = (
            _source_snapshot_checkpoint(
                final_verification_database,
                expected_sha256=target_backup_sha256,
                expected_identity=final_verification_identity,
                issue_prefix="final_target_verification",
            )
        )
        self.ledger.record(
            "final_target_verification_copy_unchanged",
            "passed",
            {
                "backup_sha256": target_backup_sha256,
                "size": final_checkpoint_size,
                "sha256": final_checkpoint_digest,
                "sqlite_sidecars_absent": True,
            },
        )

        target_media_manifest = self.artifacts / "target-media-manifest.json"
        self._command(
            "target_media_manifest_built",
            [
                str(config.python_executable),
                "-E",
                "-s",
                "-B",
                "-X",
                "utf8",
                self._python_tool("MediaManifest.py"),
                "build",
                "--root",
                str(config.target_media_root),
                "--output",
                str(target_media_manifest),
                "--snapshot-id",
                config.target_media_snapshot_id,
                "--confirm-offline-snapshot",
            ],
            env=base_env,
        )
        target_media_sha256, target_media_snapshot_id = (
            _source_media_manifest_identity(target_media_manifest)
        )
        if target_media_snapshot_id != config.target_media_snapshot_id:
            raise RehearsalBlocked("target_media_snapshot_id_mismatch")
        media_comparison = self.evidence / "media-comparison.json"
        self._command(
            "media_compared",
            [
                str(config.python_executable),
                "-E",
                "-s",
                "-B",
                "-X",
                "utf8",
                self._python_tool("MediaManifest.py"),
                "compare",
                "--source",
                str(media_manifest_copy),
                "--target",
                str(target_media_manifest),
                "--output",
                str(media_comparison),
            ],
            env=base_env,
            blocked_code="media_snapshot_mismatch",
        )
        _validate_media_comparison(media_comparison)
        original_media_hash, original_snapshot_id = _source_media_manifest_identity(
            config.source_media_manifest
        )
        copied_media_hash, copied_snapshot_id = _source_media_manifest_identity(
            media_manifest_copy
        )
        final_target_media_hash, final_target_snapshot_id = (
            _source_media_manifest_identity(target_media_manifest)
        )
        if (
            original_media_hash != copied_media_hash
            or original_media_hash != policy["source_media_manifest_sha256"]
            or original_snapshot_id != copied_snapshot_id
            or original_snapshot_id != policy["source_media_snapshot_id"]
        ):
            raise RehearsalBlocked("source_media_manifest_changed_during_rehearsal")
        if (
            final_target_media_hash != target_media_sha256
            or final_target_snapshot_id != target_media_snapshot_id
        ):
            raise RehearsalBlocked("target_media_manifest_changed_during_rehearsal")
        self._record_artifact(
            "media_verified",
            media_comparison,
            source_manifest_unchanged=True,
            source_manifest_sha256=original_media_hash,
            source_media_snapshot_id=original_snapshot_id,
            source_manifest=_artifact_reference(media_manifest_copy, self.root),
            target_manifest=_artifact_reference(target_media_manifest, self.root),
            target_manifest_sha256=target_media_sha256,
            target_media_snapshot_id=target_media_snapshot_id,
        )

        final_target_media_manifest = (
            self.artifacts / "target-media-manifest-final.json"
        )
        self._command(
            "target_media_manifest_final_built",
            [
                str(config.python_executable),
                "-E",
                "-s",
                "-B",
                "-X",
                "utf8",
                self._python_tool("MediaManifest.py"),
                "build",
                "--root",
                str(config.target_media_root),
                "--output",
                str(final_target_media_manifest),
                "--snapshot-id",
                config.target_media_snapshot_id,
                "--confirm-offline-snapshot",
            ],
            env=base_env,
        )
        final_target_media_sha256, final_target_media_snapshot_id = (
            _source_media_manifest_identity(final_target_media_manifest)
        )
        if final_target_media_snapshot_id != config.target_media_snapshot_id:
            raise RehearsalBlocked("final_target_media_snapshot_id_mismatch")
        final_media_comparison = self.evidence / "media-comparison-final.json"
        self._command(
            "final_media_compared",
            [
                str(config.python_executable),
                "-E",
                "-s",
                "-B",
                "-X",
                "utf8",
                self._python_tool("MediaManifest.py"),
                "compare",
                "--source",
                str(media_manifest_copy),
                "--target",
                str(final_target_media_manifest),
                "--output",
                str(final_media_comparison),
            ],
            env=base_env,
            blocked_code="final_media_snapshot_mismatch",
        )
        _validate_media_comparison(final_media_comparison)
        final_original_media_hash, final_original_snapshot_id = (
            _source_media_manifest_identity(config.source_media_manifest)
        )
        if (
            final_original_media_hash != original_media_hash
            or final_original_snapshot_id != original_snapshot_id
        ):
            raise RehearsalBlocked("source_media_manifest_changed_during_rehearsal")
        self._record_artifact(
            "final_media_verified",
            final_media_comparison,
            target_media_directory_rescanned=True,
            target_manifest=_artifact_reference(final_target_media_manifest, self.root),
            target_manifest_sha256=final_target_media_sha256,
            target_media_snapshot_id=final_target_media_snapshot_id,
        )
        target_backup_final_evidence = self.evidence / "target-backup-set-final.json"
        self._command(
            "target_snapshot_set_final_verified",
            [
                str(config.python_executable),
                "-E",
                "-s",
                "-B",
                "-X",
                "utf8",
                self._python_tool("Verify-SQLiteBackupSet.py"),
                "--database",
                str(target_backup),
                "--checksum",
                str(target_backup.with_name(f"{target_backup.name}.sha256")),
                "--metadata",
                str(target_backup.with_name(f"{target_backup.name}.metadata.json")),
                "--output",
                str(target_backup_final_evidence),
            ],
            env=base_env,
        )
        target_backup_final_report = _validate_backup_report(
            target_backup_final_evidence
        )
        target_backup_final_references = _backup_set_artifact_references(
            target_backup,
            self.root,
        )
        if (
            target_backup_final_report["artifact"]["sha256"]
            != target_backup_sha256
            or target_backup_final_report["artifact"]["size"]
            != target_backup_initial_references["database"]["size"]
            or target_backup_final_references != target_backup_initial_references
        ):
            raise RehearsalBlocked("target_backup_set_changed_during_rehearsal")
        self._record_artifact(
            "target_snapshot_set_final_verified",
            target_backup_final_evidence,
            backup_sha256=target_backup_sha256,
            backup_set=target_backup_final_references,
            backup_set_unchanged=True,
        )
        # This must remain the final child process in a successful rehearsal.
        # It proves the runtime identity/closure used by every preceding final
        # media and backup verification step remained unchanged.
        self._verify_runtime_fingerprint(
            "runtime_fingerprint_final",
            env=base_env,
            full_content_hash=True,
        )
        target_media_initial_reference = _artifact_reference(
            target_media_manifest,
            self.root,
        )
        if target_media_initial_reference["sha256"] != target_media_sha256:
            raise RehearsalBlocked("target_media_manifest_changed_during_rehearsal")
        target_media_final_reference = _artifact_reference(
            final_target_media_manifest,
            self.root,
        )
        self.deployment_candidate_details = {
            "backup_set": target_backup_final_references,
            "backup_sha256": target_backup_sha256,
            "backup_set_initial_verification": target_backup_initial_evidence_reference,
            "backup_set_final_verification": _artifact_reference(
                target_backup_final_evidence,
                self.root,
            ),
            "snapshot_inspection": _artifact_reference(
                target_inspection,
                self.root,
            ),
            "final_site_data_comparison": _artifact_reference(
                final_comparison,
                self.root,
            ),
            "final_restriction_preflight": _artifact_reference(
                final_restriction,
                self.root,
            ),
            "target_media_manifest": target_media_final_reference,
            "target_media_initial_manifest": target_media_initial_reference,
            "target_media_final_comparison": _artifact_reference(
                final_media_comparison,
                self.root,
            ),
            "target_media_directory_rescanned": True,
            "target_media_snapshot_id": final_target_media_snapshot_id,
            "source_media_manifest_sha256": original_media_hash,
            "source_media_snapshot_id": original_snapshot_id,
            "cutover_authorized": False,
        }

    def finalize_deployment_candidate(self) -> None:
        if self.deployment_candidate_details is None:
            raise RehearsalError("deployment_candidate_not_ready")
        self._record_source_final()
        if self.bootstrap_context is not None:
            # Seal the last external content read before publishing the candidate.
            self._verify_external_handoff_final()
        self.ledger.record(
            "deployment_candidate_verified",
            "passed",
            self.deployment_candidate_details,
        )

    def finish(self, status: str, issues: list[str]) -> Path:
        if status not in {"completed", "blocked", "failed", "interrupted"}:
            raise ValueError("Invalid terminal status")
        self._assert_run_tree_unchanged()
        self.ledger.record(
            status,
            "terminal",
            {
                "status": status,
                "issues": issues,
                "cutover_authorized": False,
                "network_isolation_enforced": False,
                "network_access_observation": "not_measured",
                "live_production_service_access_requested_by_orchestrator": False,
                "production_copy_read_performed": self.production_copy_read_performed,
                "contains_production_user_data": self.production_copy_read_performed,
                "retained_on_success": self.production_copy_read_performed,
                "secure_disposal_required": self.production_copy_read_performed,
            },
        )
        replay = self.ledger.verify_replay(expected_terminal=status)
        replay["ledger"]["path"] = "evidence/events.jsonl"
        result = {
            "format": REPORT_FORMAT,
            "format_version": REPORT_VERSION,
            "generated_at": _utc_now(),
            "status": status,
            "completed_stages": self.ledger.completed_stages,
            "issues": issues,
            "evidence_chain": {
                **replay,
                "verification": "self_consistent_local_chain",
                "tamper_proof": False,
            },
            "source_immutable_snapshot_attested": self.config.confirm_source_immutable,
            "source_database_unchanged": self.source_database_unchanged,
            "failed_artifacts_retained": status != "completed",
            "workspace_access_control": self.workspace_access_control,
            "network_isolation_enforced": False,
            "network_access_observation": "not_measured",
            "live_production_service_access_requested_by_orchestrator": False,
            "production_copy_read_performed": self.production_copy_read_performed,
            "contains_production_user_data": self.production_copy_read_performed,
            "retained_on_success": self.production_copy_read_performed,
            "secure_disposal_required": self.production_copy_read_performed,
            "sensitive_retention_scope": "entire_run_root",
            "sensitive_retention_directories": ["."],
            "cutover_authorized": False,
        }
        result_path = self.evidence / "result.json"
        _write_json_create_new(result_path, result)
        return result_path


def run_rehearsal(
    config: RehearsalConfig,
    *,
    runner: Runner | None = None,
    bootstrap_context: BootstrapInnerContext | None = None,
) -> tuple[str, Path]:
    prepared = _prepare_config(config, bootstrap_context=bootstrap_context)
    access_control = (
        _create_run_root(prepared)
        if bootstrap_context is None
        else bootstrap_context.workspace_access_control
    )
    rehearsal = Rehearsal(
        prepared,
        runner or SubprocessRunner(),
        workspace_access_control=access_control,
        bootstrap_context=bootstrap_context,
    )
    with _coherent_finalization_signals() as begin_finalization:
        try:
            status = "completed"
            issues: list[str] = []
            try:
                rehearsal.execute()
            except KeyboardInterrupt:
                begin_finalization()
                status = "interrupted"
                issues.append("keyboard_interrupt")
            except RehearsalInterrupted as exc:
                begin_finalization()
                status = "interrupted"
                issues.append(exc.code)
                if exc.stage is not None:
                    rehearsal.ledger.record(
                        exc.stage,
                        "interrupted",
                        {"issue": exc.code},
                    )
            except RehearsalBlocked as exc:
                begin_finalization()
                status = "blocked"
                issues.append(exc.code)
                if exc.stage is not None:
                    rehearsal.ledger.record(
                        exc.stage,
                        "blocked",
                        {"issue": exc.code},
                    )
            except RehearsalError as exc:
                begin_finalization()
                status = "failed"
                issues.append(exc.code)
                if exc.stage is not None:
                    rehearsal.ledger.record(
                        exc.stage,
                        "failed",
                        {"issue": exc.code},
                    )
            except Exception as exc:
                begin_finalization()
                status = "failed"
                issues.append(f"unexpected_{type(exc).__name__}")
            finally:
                # From this point until result.json is fsynced, subsequent Ctrl+C
                # signals are ignored so every retained run has one coherent
                # terminal event and result whenever the process remains alive.
                begin_finalization()

            if status == "completed":
                try:
                    rehearsal.finalize_deployment_candidate()
                except RehearsalBlocked as exc:
                    status = "blocked"
                    if exc.code not in issues:
                        issues.append(exc.code)
                    if exc.stage is not None:
                        rehearsal.ledger.record(
                            exc.stage,
                            "blocked",
                            {"issue": exc.code},
                        )
                except RehearsalError as exc:
                    status = "failed"
                    if exc.code not in issues:
                        issues.append(exc.code)
                    if exc.stage is not None:
                        rehearsal.ledger.record(
                            exc.stage,
                            "failed",
                            {"issue": exc.code},
                        )
            elif rehearsal.source_expected_sha256 is not None:
                try:
                    rehearsal._record_source_final()
                except RehearsalBlocked as exc:
                    if status != "interrupted":
                        status = "blocked"
                    if exc.code not in issues:
                        issues.append(exc.code)
            result = rehearsal.finish(status, issues)
            return status, result
        finally:
            rehearsal.ledger.close()


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rehearse a production SQLite backup copy through migration, lossless "
            "v3 export/import comparison, target backup inspection, and media "
            "comparison without accessing production services."
        )
    )
    parser.add_argument("--source-database", required=True)
    parser.add_argument("--source-checksum", required=True)
    parser.add_argument("--source-metadata", required=True)
    parser.add_argument("--source-proposal-run-root", required=True)
    parser.add_argument("--source-media-manifest", required=True)
    parser.add_argument("--target-media-root", required=True)
    parser.add_argument("--target-media-snapshot-id", required=True)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--confirm-source-immutable", action="store_true")
    parser.add_argument("--confirm-target-media-offline", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = _parse_arguments()
    try:
        context = _load_bootstrap_inner_context(arguments.run_root)
        config = RehearsalConfig(
            repository_root=context.run_root / "code",
            python_executable=Path(sys.executable),
            source_database=Path(arguments.source_database),
            source_checksum=Path(arguments.source_checksum),
            source_metadata=Path(arguments.source_metadata),
            source_upgrade_policy=context.policy_path,
            source_media_manifest=Path(arguments.source_media_manifest),
            target_media_root=Path(arguments.target_media_root),
            target_media_snapshot_id=arguments.target_media_snapshot_id,
            run_root=context.run_root,
            confirm_source_immutable=arguments.confirm_source_immutable,
            confirm_target_media_offline=arguments.confirm_target_media_offline,
            source_policy_proposal=context.proposal_path,
            source_review_record=context.review_path,
            source_proposal_run_root=Path(arguments.source_proposal_run_root),
        )
        status, result = run_rehearsal(config, bootstrap_context=context)
    except (ConfigurationError, RehearsalError) as exc:
        print(f"Production-copy rehearsal refused: {exc}", file=sys.stderr)
        return 1
    if status == "completed":
        print(f"Production-copy rehearsal completed; evidence: {result}")
        return 0
    print(
        f"Production-copy rehearsal {status}; retained evidence: {result}",
        file=sys.stderr,
    )
    if status == "interrupted":
        return 130
    return 2 if status == "blocked" else 1


if __name__ == "__main__":
    raise SystemExit(main())
