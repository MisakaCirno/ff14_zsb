from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import secrets
import signal
import stat
import subprocess
import sys
from typing import Any, Callable, Protocol, Sequence


BOOTSTRAP_FORMAT = "ffxivshare-production-copy-bootstrap"
BOOTSTRAP_VERSION = 1
COMPLETION_FORMAT = "ffxivshare-production-copy-bootstrap-completion"
COMPLETION_VERSION = 1
BUNDLE_MANIFEST_FORMAT = "ffxivshare-production-copy-execution-bundle"
BUNDLE_MANIFEST_VERSION = 1
POLICY_FORMAT = "ffxivshare-source-upgrade-policy"
POLICY_VERSION = 2
MAX_POLICY_BYTES = 4 * 1024 * 1024
MAX_PROPOSAL_BYTES = 32 * 1024 * 1024
MAX_REVIEW_BYTES = 4 * 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MIGRATION_PART_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
UTC_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
WINDOWS_CHILD_WRITE_ACCESS = (
    0x00000002  # FILE_WRITE_DATA / FILE_ADD_FILE
    | 0x00000004  # FILE_APPEND_DATA / FILE_ADD_SUBDIRECTORY
    | 0x00000010  # FILE_WRITE_EA
    | 0x00000040  # FILE_DELETE_CHILD
    | 0x00000100  # FILE_WRITE_ATTRIBUTES
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
    | 0x10000000  # GENERIC_ALL
    | 0x40000000  # GENERIC_WRITE
)
WINDOWS_PATH_COMPONENT_CONTROL_ACCESS = (
    0x00000040  # FILE_DELETE_CHILD
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
    | 0x10000000  # GENERIC_ALL
)
WINDOWS_SIMPLE_ALLOW_ACE_TYPE = 0
WINDOWS_NON_GRANT_ACE_TYPES = {
    1,
    2,
    3,
    6,
    7,
    8,
    10,
    12,
    13,
    14,
    15,
    16,
    17,
    18,
    19,
    20,
    21,
}
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
EXECUTION_PYTHON_DIRECTORIES = ("ffxivshare", "shares")
APPROVED_POLICY_KEYS = {
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
    "target_leaf_nodes",
}


class BootstrapConfigurationError(RuntimeError):
    pass


class BootstrapError(RuntimeError):
    pass


@dataclass(frozen=True)
class BootstrapConfig:
    repository_root: Path
    python_executable: Path
    run_root: Path
    mode: str
    inner_entrypoint: str
    inner_arguments: tuple[str, ...]
    policy_path: Path | None = None
    proposal_path: Path | None = None
    review_record_path: Path | None = None
    expected_execution_bundle_sha256: str | None = None


@dataclass(frozen=True)
class PreparedBootstrap:
    config: BootstrapConfig
    expected_execution_bundle_sha256: str | None
    policy_reference: dict[str, Any] | None
    policy_bytes: bytes | None
    proposal_reference: dict[str, Any] | None
    proposal_bytes: bytes | None
    review_reference: dict[str, Any] | None
    review_bytes: bytes | None


@dataclass(frozen=True)
class BootstrapOutcome:
    exit_code: int
    run_root: Path
    bootstrap_record: Path
    completion_record: Path


@dataclass(frozen=True)
class InnerRunResult:
    returncode: int
    stdout_path: Path
    stderr_path: Path


class InnerRunner(Protocol):
    def run(
        self,
        *,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        stdout_path: Path,
        stderr_path: Path,
    ) -> InnerRunResult: ...


class SubprocessInnerRunner:
    def run(
        self,
        *,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        stdout_path: Path,
        stderr_path: Path,
    ) -> InnerRunResult:
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
            )
            try:
                returncode = process.wait()
            except KeyboardInterrupt:
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                raise
            stdout.flush()
            stderr.flush()
            os.fsync(stdout.fileno())
            os.fsync(stderr.fileno())
        return InnerRunResult(returncode, stdout_path, stderr_path)


def _windows_ace_requires_conservative_rejection(ace_type: int, mask: int) -> bool:
    if not mask:
        return False
    if ace_type == WINDOWS_SIMPLE_ALLOW_ACE_TYPE:
        return False
    if ace_type in WINDOWS_NON_GRANT_ACE_TYPES:
        return False
    return True


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


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


def _write_json_create_new(path: Path, value: dict[str, Any]) -> None:
    _write_bytes_create_new(path, _canonical_json_bytes(value))


def _write_bytes_create_new(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}")
    published = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
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


def _is_reparse_point(path: Path) -> bool:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        return True
    return bool(
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
                raise BootstrapConfigurationError(
                    "Bootstrap paths must not traverse symlinks or reparse points."
                )
        except FileNotFoundError:
            if include_leaf or current != path:
                raise BootstrapConfigurationError(
                    f"Bootstrap path component does not exist: {current}"
                )
            break
        except OSError as exc:
            raise BootstrapConfigurationError(
                f"Bootstrap path component cannot be inspected: {current}"
            ) from exc


def _absolute_local_path(raw: str | Path, *, label: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise BootstrapConfigurationError(f"{label} must be an absolute path.")
    if ".." in path.parts or path == Path(path.anchor):
        raise BootstrapConfigurationError(f"{label} is not a safe absolute path.")
    if os.name == "nt":
        if path.drive.startswith("\\\\"):
            raise BootstrapConfigurationError(f"{label} must not use a UNC path.")
        _drive, tail = os.path.splitdrive(os.fspath(path))
        if ":" in tail:
            raise BootstrapConfigurationError(
                f"{label} must not use a Windows alternate data stream."
            )
        try:
            import ctypes

            get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
            get_drive_type.argtypes = [ctypes.c_wchar_p]
            get_drive_type.restype = ctypes.c_uint
            drive_type = get_drive_type(path.anchor)
        except (AttributeError, OSError) as exc:
            raise BootstrapConfigurationError(
                f"{label} drive type cannot be inspected."
            ) from exc
        if drive_type == 4:
            raise BootstrapConfigurationError(
                f"{label} must not use a mapped network drive."
            )
    return path


def _existing_path(raw: str | Path, *, label: str, directory: bool) -> Path:
    path = _absolute_local_path(raw, label=label)
    _assert_no_reparse_components(path, include_leaf=True)
    try:
        resolved = path.resolve(strict=True)
        metadata = os.lstat(resolved)
    except OSError as exc:
        raise BootstrapConfigurationError(
            f"{label} does not exist or cannot be inspected."
        ) from exc
    expected = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if not expected or _is_reparse_point(resolved):
        expected_label = "directory" if directory else "single-link regular file"
        raise BootstrapConfigurationError(f"{label} must be an existing {expected_label}.")
    if not directory and metadata.st_nlink != 1:
        raise BootstrapConfigurationError(f"{label} must not have hard-link aliases.")
    return resolved


def _canonical_key(path: Path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _is_within(path: Path, directory: Path) -> bool:
    try:
        return os.path.commonpath((_canonical_key(path), _canonical_key(directory))) == _canonical_key(directory)
    except ValueError:
        return False


def _stat_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _hash_stable(path: Path) -> tuple[int, str]:
    digest = sha256()
    try:
        path_metadata = os.lstat(path)
        if (
            not stat.S_ISREG(path_metadata.st_mode)
            or _is_reparse_point(path)
            or path_metadata.st_nlink != 1
        ):
            raise BootstrapError(f"Unsafe execution-bundle file: {path}")
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        current = os.stat(path)
    except BootstrapError:
        raise
    except OSError as exc:
        raise BootstrapError(f"Execution-bundle file cannot be hashed: {path}") from exc
    if (
        _stat_identity(path_metadata) != _stat_identity(before)
        or _stat_identity(before) != _stat_identity(after)
        or _stat_identity(after) != _stat_identity(current)
    ):
        raise BootstrapError(f"Execution-bundle file changed while hashing: {path}")
    return after.st_size, digest.hexdigest()


def _stable_bytes(path: Path, *, maximum_size: int) -> tuple[bytes, str]:
    size, first_digest = _hash_stable(path)
    if size > maximum_size:
        raise BootstrapConfigurationError("Approved policy exceeds the size limit.")
    try:
        first = path.read_bytes()
        second_size, second_digest = _hash_stable(path)
        second = path.read_bytes()
        final_size, final_digest = _hash_stable(path)
    except OSError as exc:
        raise BootstrapConfigurationError("Approved policy cannot be read.") from exc
    if (
        size != second_size
        or size != final_size
        or first_digest != second_digest
        or first_digest != final_digest
        or first != second
        or len(first) != size
    ):
        raise BootstrapConfigurationError("Approved policy changed while it was read.")
    return first, first_digest


def _validate_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise BootstrapConfigurationError(f"{label} must be a lowercase SHA-256 digest.")
    return value


def _validate_identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise BootstrapConfigurationError(f"{label} is invalid.")
    return value


def _validate_timestamp(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or UTC_TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise BootstrapConfigurationError(f"{label} must be a UTC timestamp.")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise BootstrapConfigurationError(f"{label} is not a real timestamp.") from exc
    if parsed.tzinfo != UTC:
        raise BootstrapConfigurationError(f"{label} must use UTC.")
    return value


def _validate_nodes(value: Any, *, label: str) -> list[list[str]]:
    if not isinstance(value, list) or not value:
        raise BootstrapConfigurationError(f"{label} must be a non-empty list.")
    nodes: list[list[str]] = []
    for node in value:
        if (
            not isinstance(node, list)
            or len(node) != 2
            or not all(isinstance(part, str) for part in node)
            or not all(MIGRATION_PART_PATTERN.fullmatch(part) for part in node)
        ):
            raise BootstrapConfigurationError(f"{label} contains an invalid node.")
        nodes.append([node[0], node[1]])
    canonical = [list(node) for node in sorted({tuple(node) for node in nodes})]
    if nodes != canonical:
        raise BootstrapConfigurationError(f"{label} must be canonical.")
    return nodes


def _load_approved_policy(
    path: Path,
) -> tuple[str, dict[str, Any], bytes, dict[str, Any]]:
    raw, policy_sha256 = _stable_bytes(path, maximum_size=MAX_POLICY_BYTES)
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise BootstrapConfigurationError(
            "Approved policy must be strict UTF-8 JSON."
        ) from exc
    if not isinstance(value, dict) or set(value) != APPROVED_POLICY_KEYS:
        raise BootstrapConfigurationError("Approved policy has an invalid exact shape.")
    if value["format"] != POLICY_FORMAT or value["format_version"] != POLICY_VERSION:
        raise BootstrapConfigurationError("Approved policy format is unsupported.")
    if value["approved"] is not True or value["lossless_reviewed"] is not True:
        raise BootstrapConfigurationError("Policy is not approved for lossless rehearsal.")
    for name in (
        "execution_bundle_sha256",
        "approval_tool_sha256",
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
    ):
        _validate_sha256(value[name], label=name)
    for name in (
        "policy_id",
        "proposal_id",
        "proposal_run_id",
        "review_id",
        "reviewer",
        "source_media_snapshot_id",
    ):
        _validate_identifier(value[name], label=name)
    _validate_timestamp(value["approved_at"], label="approved_at")
    _validate_timestamp(value["reviewed_at"], label="reviewed_at")
    if (
        value["reviewer_identity_verification"]
        != "operator_asserted_not_cryptographically_verified"
    ):
        raise BootstrapConfigurationError(
            "Approved policy reviewer identity claim is unsupported."
        )
    if (
        not isinstance(value["proposal_ledger_event_count"], int)
        or isinstance(value["proposal_ledger_event_count"], bool)
        or value["proposal_ledger_event_count"] <= 0
    ):
        raise BootstrapConfigurationError(
            "Approved policy proposal ledger count is invalid."
        )
    _validate_nodes(value["source_leaf_nodes"], label="source_leaf_nodes")
    _validate_nodes(value["target_leaf_nodes"], label="target_leaf_nodes")
    reference = {
        "path": str(path),
        "size": len(raw),
        "sha256": policy_sha256,
        "policy_id": value["policy_id"],
        "review_id": value["review_id"],
    }
    return value["execution_bundle_sha256"], reference, raw, value


def _load_bound_json_input(
    path: Path,
    *,
    label: str,
    maximum_size: int,
    expected_sha256: str,
) -> tuple[dict[str, Any], bytes]:
    raw, digest = _stable_bytes(path, maximum_size=maximum_size)
    if digest != expected_sha256:
        raise BootstrapConfigurationError(
            f"{label} does not match the approved policy binding."
        )
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise BootstrapConfigurationError(f"{label} must be strict UTF-8 JSON.") from exc
    if not isinstance(value, dict):
        raise BootstrapConfigurationError(f"{label} must be a JSON object.")
    return value, raw


def _safe_relative_python_path(raw: str, *, label: str) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise BootstrapConfigurationError(f"{label} must be a canonical POSIX path.")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != raw:
        raise BootstrapConfigurationError(f"{label} must be a safe relative path.")
    if path.suffix != ".py":
        raise BootstrapConfigurationError(f"{label} must name a Python source file.")
    return raw


def _assert_outer_isolation_flags() -> dict[str, bool]:
    flags = {
        "isolated": bool(sys.flags.isolated),
        "ignore_environment": bool(sys.flags.ignore_environment),
        "no_site": bool(sys.flags.no_site),
        "no_user_site": bool(sys.flags.no_user_site),
        "dont_write_bytecode": bool(sys.flags.dont_write_bytecode),
        "safe_path": bool(getattr(sys.flags, "safe_path", 0)),
        "utf8_mode": bool(sys.flags.utf8_mode),
    }
    if not all(flags.values()):
        raise BootstrapConfigurationError(
            "Bootstrap must run with: python -I -S -B -X utf8."
        )
    return flags


def _prepare_config(config: BootstrapConfig) -> PreparedBootstrap:
    _assert_outer_isolation_flags()
    if config.mode not in {
        "approved-rehearsal",
        "pinned-bundle",
        "policy-proposal",
    }:
        raise BootstrapConfigurationError("Bootstrap mode is unsupported.")
    repository_root = _existing_path(
        config.repository_root,
        label="Repository root",
        directory=True,
    )
    python_executable = _existing_path(
        config.python_executable,
        label="Python executable",
        directory=False,
    )
    current_python = _existing_path(
        Path(sys.executable),
        label="Bootstrap Python executable",
        directory=False,
    )
    if _canonical_key(python_executable) != _canonical_key(current_python):
        raise BootstrapConfigurationError(
            "Inner Python must be the exact interpreter running the bootstrap."
        )
    run_root = _absolute_local_path(config.run_root, label="Run root")
    if os.path.lexists(run_root):
        raise BootstrapConfigurationError("Run root must be create-new.")
    _assert_no_reparse_components(run_root, include_leaf=False)
    try:
        run_parent = run_root.parent.resolve(strict=True)
    except OSError as exc:
        raise BootstrapConfigurationError("Run root parent must exist.") from exc
    if not run_parent.is_dir() or _is_within(run_root, repository_root):
        raise BootstrapConfigurationError("Run root is not separated from the repository.")
    entrypoint = _safe_relative_python_path(
        config.inner_entrypoint,
        label="Inner entrypoint",
    )
    fixed_entrypoints = {
        "approved-rehearsal": "ops/migration/Rehearse-ProductionCopy.py",
        "policy-proposal": "ops/migration/Propose-ProductionCopyPolicy.py",
    }
    required_entrypoint = fixed_entrypoints.get(config.mode)
    if required_entrypoint is not None and entrypoint != required_entrypoint:
        raise BootstrapConfigurationError(
            f"{config.mode} must launch {required_entrypoint}."
        )
    arguments: list[str] = []
    for argument in config.inner_arguments:
        if not isinstance(argument, str) or "\x00" in argument:
            raise BootstrapConfigurationError("Inner arguments must be NUL-free strings.")
        arguments.append(argument)

    policy_reference: dict[str, Any] | None = None
    policy_bytes: bytes | None = None
    proposal_reference: dict[str, Any] | None = None
    proposal_bytes: bytes | None = None
    review_reference: dict[str, Any] | None = None
    review_bytes: bytes | None = None
    if config.mode == "approved-rehearsal":
        if (
            config.policy_path is None
            or config.proposal_path is None
            or config.review_record_path is None
            or config.expected_execution_bundle_sha256 is not None
        ):
            raise BootstrapConfigurationError(
                "Approved rehearsal requires policy, proposal, and review inputs."
            )
        policy_path = _existing_path(
            config.policy_path,
            label="Approved policy",
            directory=False,
        )
        if _is_within(run_root, policy_path.parent):
            raise BootstrapConfigurationError(
                "Run root must be outside the approved-policy directory."
            )
        expected_digest, policy_reference, policy_bytes, policy_value = (
            _load_approved_policy(policy_path)
        )
        proposal_path = _existing_path(
            config.proposal_path,
            label="Approved policy proposal",
            directory=False,
        )
        review_path = _existing_path(
            config.review_record_path,
            label="Approved policy review record",
            directory=False,
        )
        if len({_canonical_key(policy_path), _canonical_key(proposal_path), _canonical_key(review_path)}) != 3:
            raise BootstrapConfigurationError(
                "Approved policy, proposal, and review must be distinct files."
            )
        for path, label in (
            (proposal_path, "proposal"),
            (review_path, "review-record"),
        ):
            if _is_within(run_root, path.parent):
                raise BootstrapConfigurationError(
                    f"Run root must be outside the {label} directory."
                )
        _proposal_value, proposal_bytes = _load_bound_json_input(
            proposal_path,
            label="Policy proposal",
            maximum_size=MAX_PROPOSAL_BYTES,
            expected_sha256=policy_value["proposal_sha256"],
        )
        _review_value, review_bytes = _load_bound_json_input(
            review_path,
            label="Policy review record",
            maximum_size=MAX_REVIEW_BYTES,
            expected_sha256=policy_value["review_record_sha256"],
        )
        proposal_reference = {
            "path": str(proposal_path),
            "size": len(proposal_bytes),
            "sha256": policy_value["proposal_sha256"],
        }
        review_reference = {
            "path": str(review_path),
            "size": len(review_bytes),
            "sha256": policy_value["review_record_sha256"],
        }
        forbidden_argument_keys = {
            _canonical_key(policy_path),
            _canonical_key(proposal_path),
            _canonical_key(review_path),
        }
        if any(
            os.path.normcase(os.path.abspath(argument)) in forbidden_argument_keys
            for argument in arguments
            if os.path.isabs(argument)
        ):
            raise BootstrapConfigurationError(
                "Inner arguments must not reference external approval inputs; "
                "use the FFXIVSHARE_BOOTSTRAP_* environment bindings."
            )
    elif config.mode == "pinned-bundle":
        if any(
            value is not None
            for value in (
                config.policy_path,
                config.proposal_path,
                config.review_record_path,
            )
        ):
            raise BootstrapConfigurationError(
                "Pinned-bundle mode must not read approval inputs."
            )
        expected_digest = _validate_sha256(
            config.expected_execution_bundle_sha256,
            label="Expected execution bundle sha256",
        )
    else:
        if (
            config.policy_path is not None
            or config.proposal_path is not None
            or config.review_record_path is not None
            or config.expected_execution_bundle_sha256 is not None
        ):
            raise BootstrapConfigurationError(
                "Policy-proposal mode derives authority only from a stable repository snapshot."
            )
        expected_digest = None

    prepared_config = BootstrapConfig(
        repository_root=repository_root,
        python_executable=python_executable,
        run_root=run_root,
        mode=config.mode,
        inner_entrypoint=entrypoint,
        inner_arguments=tuple(arguments),
        policy_path=(
            Path(policy_reference["path"]) if policy_reference is not None else None
        ),
        proposal_path=(
            Path(proposal_reference["path"])
            if proposal_reference is not None
            else None
        ),
        review_record_path=(
            Path(review_reference["path"])
            if review_reference is not None
            else None
        ),
        expected_execution_bundle_sha256=(
            expected_digest if config.mode == "pinned-bundle" else None
        ),
    )
    return PreparedBootstrap(
        prepared_config,
        expected_digest,
        policy_reference,
        policy_bytes,
        proposal_reference,
        proposal_bytes,
        review_reference,
        review_bytes,
    )


def _iter_execution_bundle_files(
    root: Path,
    *,
    inner_entrypoint: str,
) -> list[tuple[str, Path]]:
    required = set(EXECUTION_FIXED_FILES)
    required.add(inner_entrypoint)
    files: dict[str, Path] = {}
    for relative in sorted(required):
        path = root / Path(relative)
        try:
            metadata = os.lstat(path)
        except OSError as exc:
            raise BootstrapError(f"Required execution-bundle file is missing: {relative}") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or _is_reparse_point(path)
        ):
            raise BootstrapError(f"Required execution-bundle file is unsafe: {relative}")
        files[relative] = path

    for directory_name in EXECUTION_PYTHON_DIRECTORIES:
        directory = root / directory_name
        try:
            directory_metadata = os.lstat(directory)
        except OSError as exc:
            raise BootstrapError(
                f"Execution-bundle directory is missing: {directory_name}"
            ) from exc
        if not stat.S_ISDIR(directory_metadata.st_mode) or _is_reparse_point(directory):
            raise BootstrapError(
                f"Execution-bundle directory is unsafe: {directory_name}"
            )
        pending = [directory]
        while pending:
            current = pending.pop()
            try:
                entries = sorted(os.scandir(current), key=lambda entry: entry.name)
            except OSError as exc:
                raise BootstrapError(
                    f"Execution-bundle directory cannot be enumerated: {current}"
                ) from exc
            for entry in entries:
                path = Path(entry.path)
                if _is_reparse_point(path):
                    raise BootstrapError(f"Execution-bundle path is unsafe: {path}")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False) and path.suffix == ".py":
                    relative = path.relative_to(root).as_posix()
                    files[relative] = path
    return sorted(files.items())


def _bundle_projection(files: Sequence[tuple[str, Path]]) -> list[dict[str, Any]]:
    projection = []
    for relative, path in files:
        size, digest = _hash_stable(path)
        projection.append({"path": relative, "size": size, "sha256": digest})
    return projection


def _execution_bundle_sha256(root: Path, *, inner_entrypoint: str) -> str:
    return _canonical_json_sha256(
        _bundle_projection(
            _iter_execution_bundle_files(root, inner_entrypoint=inner_entrypoint)
        )
    )


def _copy_stable(source: Path, destination: Path) -> tuple[int, str]:
    digest = sha256()
    try:
        source_metadata = os.lstat(source)
        if (
            not stat.S_ISREG(source_metadata.st_mode)
            or source_metadata.st_nlink != 1
            or _is_reparse_point(source)
        ):
            raise BootstrapError(f"Execution-bundle source is unsafe: {source}")
        with source.open("rb") as source_stream:
            before = os.fstat(source_stream.fileno())
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o400,
            )
            with os.fdopen(descriptor, "wb") as destination_stream:
                for chunk in iter(lambda: source_stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                    destination_stream.write(chunk)
                destination_stream.flush()
                os.fsync(destination_stream.fileno())
            after = os.fstat(source_stream.fileno())
        current = os.stat(source)
    except BootstrapError:
        raise
    except Exception:
        if os.path.lexists(destination):
            try:
                destination.unlink()
            except OSError:
                pass
        raise
    if (
        _stat_identity(source_metadata) != _stat_identity(before)
        or _stat_identity(before) != _stat_identity(after)
        or _stat_identity(after) != _stat_identity(current)
    ):
        destination.unlink(missing_ok=True)
        raise BootstrapError(f"Execution-bundle source changed while copying: {source}")
    copied_size, copied_digest = _hash_stable(destination)
    if copied_size != after.st_size or copied_digest != digest.hexdigest():
        raise BootstrapError(f"Frozen execution-bundle copy is invalid: {destination}")
    os.chmod(destination, stat.S_IREAD)
    return copied_size, copied_digest


def _expected_snapshot_directories(expected_files: Sequence[str]) -> set[str]:
    expected_directories: set[str] = set()
    for relative in expected_files:
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    return expected_directories


def _snapshot_projection(
    root: Path,
    *,
    expected_files: Sequence[str],
) -> list[dict[str, Any]]:
    expected_file_set = set(expected_files)
    expected_directories = _expected_snapshot_directories(expected_files)
    discovered_files: dict[str, Path] = {}
    discovered_directories: set[str] = set()
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError as exc:
            raise BootstrapError("Frozen execution snapshot cannot be enumerated.") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            if _is_reparse_point(path):
                raise BootstrapError("Frozen execution snapshot contains a link.")
            if entry.is_dir(follow_symlinks=False):
                discovered_directories.add(relative)
                pending.append(path)
            elif entry.is_file(follow_symlinks=False):
                discovered_files[relative] = path
            else:
                raise BootstrapError("Frozen execution snapshot contains a special file.")
    if set(discovered_files) != expected_file_set or discovered_directories != expected_directories:
        raise BootstrapError("Frozen execution snapshot exact closure changed.")
    return _bundle_projection(sorted(discovered_files.items()))


def _freeze_execution_bundle(
    source_root: Path,
    target_root: Path,
    *,
    inner_entrypoint: str,
    expected_sha256: str,
) -> tuple[list[dict[str, Any]], str]:
    source_files = _iter_execution_bundle_files(
        source_root,
        inner_entrypoint=inner_entrypoint,
    )
    first_projection = _bundle_projection(source_files)
    first_digest = _canonical_json_sha256(first_projection)
    if first_digest != expected_sha256:
        raise BootstrapError("Repository execution bundle does not match its authority.")
    expected_files = tuple(relative for relative, _path in source_files)
    for relative, source in source_files:
        destination = target_root / Path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _copy_stable(source, destination)
    second_files = _iter_execution_bundle_files(
        source_root,
        inner_entrypoint=inner_entrypoint,
    )
    if tuple(relative for relative, _path in second_files) != expected_files:
        raise BootstrapError("Repository execution-bundle membership changed while freezing.")
    second_digest = _canonical_json_sha256(_bundle_projection(second_files))
    snapshot_projection = _snapshot_projection(
        target_root,
        expected_files=expected_files,
    )
    snapshot_digest = _canonical_json_sha256(snapshot_projection)
    if second_digest != expected_sha256 or snapshot_digest != expected_sha256:
        raise BootstrapError("Execution bundle changed while it was frozen.")
    return snapshot_projection, snapshot_digest


def _secure_run_root(path: Path, *, parent_only: bool = False) -> str:
    if os.name != "nt":
        if parent_only:
            return "posix_parent_acl_not_proven"
        os.chmod(path, 0o700)
        metadata = os.stat(path)
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise BootstrapConfigurationError("Run root permissions are not private.")
        return "posix_mode_0700_parent_acl_not_proven"

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
    advapi32.GetSecurityDescriptorOwner.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorOwner.restype = wintypes.BOOL
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

    def sid_to_string(sid_pointer: int) -> str:
        output = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(
            ctypes.c_void_p(sid_pointer), ctypes.byref(output)
        ):
            raise BootstrapConfigurationError("Windows SID conversion failed.")
        try:
            return output.value
        finally:
            kernel32.LocalFree(output)

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        0x0008,
        ctypes.byref(token),
    ):
        raise BootstrapConfigurationError("Current Windows token cannot be inspected.")
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
        if not required.value:
            raise BootstrapConfigurationError(
                "Current Windows user SID size cannot be inspected."
            )
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            1,
            buffer,
            required.value,
            ctypes.byref(required),
        ):
            raise BootstrapConfigurationError(
                "Current Windows user SID cannot be inspected."
            )
        token_user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
        current_user_sid = sid_to_string(token_user.User.Sid)
    finally:
        kernel32.CloseHandle(token)

    trusted_sids = {
        current_user_sid,
        "S-1-5-18",
        "S-1-5-32-544",
        # Windows Modules Installer owns protected operating-system ancestors.
        "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
    }

    def read_descriptor(candidate: Path, security_information: int):
        required_size = wintypes.DWORD()
        advapi32.GetFileSecurityW(
            str(candidate),
            security_information,
            None,
            0,
            ctypes.byref(required_size),
        )
        if not required_size.value:
            raise BootstrapConfigurationError(
                f"Windows ACL cannot be sized: {candidate}"
            )
        descriptor = ctypes.create_string_buffer(required_size.value)
        if not advapi32.GetFileSecurityW(
            str(candidate),
            security_information,
            descriptor,
            required_size.value,
            ctypes.byref(required_size),
        ):
            raise BootstrapConfigurationError(
                f"Windows ACL cannot be read: {candidate}"
            )
        return descriptor

    def descriptor_dacl(descriptor, *, candidate: Path):
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
            raise BootstrapConfigurationError(
                f"Windows ACL is missing or null: {candidate}"
            )
        return acl

    def verify_parent(candidate: Path, *, direct_parent: bool) -> None:
        descriptor = read_descriptor(candidate, 0x00000001 | 0x00000004)
        owner = ctypes.c_void_p()
        owner_defaulted = wintypes.BOOL()
        if not advapi32.GetSecurityDescriptorOwner(
            descriptor,
            ctypes.byref(owner),
            ctypes.byref(owner_defaulted),
        ) or not owner.value:
            raise BootstrapConfigurationError(
                f"Windows ACL owner cannot be read: {candidate}"
            )
        if sid_to_string(owner.value) not in trusted_sids:
            raise BootstrapConfigurationError(
                f"Windows RunRoot ancestor has an untrusted owner: {candidate}"
            )
        acl = descriptor_dacl(descriptor, candidate=candidate)
        acl_info = AclSizeInformation()
        if not advapi32.GetAclInformation(
            acl,
            ctypes.byref(acl_info),
            ctypes.sizeof(acl_info),
            2,
        ):
            raise BootstrapConfigurationError(
                f"Windows ACL cannot be enumerated: {candidate}"
            )
        for index in range(acl_info.AceCount):
            ace_pointer = ctypes.c_void_p()
            if not advapi32.GetAce(acl, index, ctypes.byref(ace_pointer)):
                raise BootstrapConfigurationError(
                    f"Windows ACL ACE cannot be read: {candidate}"
                )
            header = ctypes.cast(ace_pointer, ctypes.POINTER(AceHeader)).contents
            if header.AceSize < 8:
                raise BootstrapConfigurationError(
                    f"Windows RunRoot ancestor has a malformed ACE: {candidate}"
                )
            mask = ctypes.c_uint32.from_address(ace_pointer.value + 4).value
            effective_on_candidate = not header.AceFlags & 0x08
            inherited_by_new_child = bool(header.AceFlags & 0x03)
            relevant_mask = 0
            if effective_on_candidate:
                relevant_mask |= mask & WINDOWS_PATH_COMPONENT_CONTROL_ACCESS
                if direct_parent:
                    relevant_mask |= mask & WINDOWS_CHILD_WRITE_ACCESS
            if direct_parent and inherited_by_new_child:
                relevant_mask |= mask & WINDOWS_CHILD_WRITE_ACCESS
            if not relevant_mask:
                continue
            if _windows_ace_requires_conservative_rejection(
                header.AceType,
                relevant_mask,
            ):
                raise BootstrapConfigurationError(
                    f"Windows RunRoot ancestor has an unverifiable allow ACE: {candidate}"
                )
            if header.AceType in WINDOWS_NON_GRANT_ACE_TYPES:
                continue
            sid = sid_to_string(ace_pointer.value + 8)
            ace_trusted_sids = set(trusted_sids)
            if header.AceFlags & 0x08:
                # CREATOR OWNER inherits as the current user on the new RunRoot.
                ace_trusted_sids.add("S-1-3-0")
            if sid not in ace_trusted_sids:
                raise BootstrapConfigurationError(
                    f"Windows RunRoot ancestor grants delete or ACL rights to {sid}: {candidate}"
                )

    current = path.parent
    direct_parent = True
    while True:
        verify_parent(current, direct_parent=direct_parent)
        if current == Path(current.anchor):
            break
        current = current.parent
        direct_parent = False

    if parent_only:
        return "windows_parent_chain_delete_write_acl_review_passed"

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
        raise BootstrapConfigurationError(
            "Private Windows security descriptor is invalid."
        )
    try:
        if not advapi32.SetFileSecurityW(str(path), 0x00000004, security_descriptor):
            error_code = ctypes.get_last_error()
            raise BootstrapConfigurationError(
                f"Private Windows DACL could not be applied (Win32 {error_code})."
            )
    finally:
        kernel32.LocalFree(security_descriptor)

    descriptor = read_descriptor(path, 0x00000004)
    control = wintypes.WORD()
    revision = wintypes.DWORD()
    if (
        not advapi32.GetSecurityDescriptorControl(
            descriptor,
            ctypes.byref(control),
            ctypes.byref(revision),
        )
        or not control.value & 0x1000
    ):
        raise BootstrapConfigurationError("Private Windows DACL is not protected.")
    acl = descriptor_dacl(descriptor, candidate=path)
    acl_info = AclSizeInformation()
    if not advapi32.GetAclInformation(
        acl,
        ctypes.byref(acl_info),
        ctypes.sizeof(acl_info),
        2,
    ) or acl_info.AceCount != 3:
        raise BootstrapConfigurationError("Private Windows DACL is not exact.")
    expected_sids = {current_user_sid, "S-1-5-18", "S-1-5-32-544"}
    actual_sids = set()
    for index in range(acl_info.AceCount):
        ace_pointer = ctypes.c_void_p()
        if not advapi32.GetAce(acl, index, ctypes.byref(ace_pointer)):
            raise BootstrapConfigurationError("Private Windows DACL ACE cannot be read.")
        header = ctypes.cast(ace_pointer, ctypes.POINTER(AceHeader)).contents
        mask = ctypes.c_uint32.from_address(ace_pointer.value + 4).value
        if header.AceType != 0 or header.AceFlags != 0x03 or mask != 0x001F01FF:
            raise BootstrapConfigurationError("Private Windows DACL ACE is not exact.")
        sid = sid_to_string(ace_pointer.value + 8)
        if sid not in expected_sids or sid in actual_sids:
            raise BootstrapConfigurationError("Private Windows DACL trustee is unexpected.")
        actual_sids.add(sid)
    if actual_sids != expected_sids:
        raise BootstrapConfigurationError("Private Windows DACL trustees are incomplete.")
    return (
        "windows_protected_dacl_current_user_system_administrators_full_control_"
        "with_parent_chain_delete_acl_review"
    )


def _create_run_root(
    path: Path,
    *,
    secure_run_root: Callable[[Path], str],
) -> tuple[str, dict[Path, tuple[str, int, int]]]:
    if os.name == "nt" and secure_run_root is _secure_run_root:
        _secure_run_root(path, parent_only=True)
    try:
        os.mkdir(path, mode=0o700)
    except FileExistsError as exc:
        raise BootstrapConfigurationError("Run root appeared concurrently.") from exc
    access_control = secure_run_root(path)
    if not isinstance(access_control, str) or not access_control:
        raise BootstrapConfigurationError("Run root security verifier returned no status.")
    directory_names = (
        "approval",
        "artifacts",
        "code",
        "evidence",
        "logs",
        "scratch-media",
        "target",
        "tmp",
        "work",
    )
    for name in directory_names:
        (path / name).mkdir()
    # The approval publisher verifies this directory independently and requires
    # its own protected DACL; inherited trustees alone are insufficient proof.
    secure_run_root(path / "approval")
    runtime_environment = path / "runtime-empty.env"
    _write_bytes_create_new(runtime_environment, b"")
    identities = {
        candidate: ("directory", os.stat(candidate).st_dev, os.stat(candidate).st_ino)
        for candidate in (
            path,
            *(path / name for name in directory_names),
        )
    }
    runtime_metadata = os.stat(runtime_environment)
    identities[runtime_environment] = (
        "file",
        runtime_metadata.st_dev,
        runtime_metadata.st_ino,
    )
    return access_control, identities


def _assert_run_directories(
    identities: dict[Path, tuple[str, int, int]],
) -> None:
    for path, (expected_kind, expected_device, expected_inode) in identities.items():
        try:
            metadata = os.lstat(path)
        except OSError as exc:
            raise BootstrapError("Bootstrap run directory disappeared.") from exc
        if (
            (
                expected_kind == "directory"
                and not stat.S_ISDIR(metadata.st_mode)
            )
            or (
                expected_kind == "file"
                and not stat.S_ISREG(metadata.st_mode)
            )
            or _is_reparse_point(path)
            or (metadata.st_dev, metadata.st_ino)
            != (expected_device, expected_inode)
        ):
            raise BootstrapError("Bootstrap run directory identity changed.")


def _python_identity(path: Path, isolation_flags: dict[str, bool]) -> dict[str, Any]:
    size, digest = _hash_stable(path)
    return {
        "cache_tag": sys.implementation.cache_tag,
        "executable": str(path),
        "executable_sha256": digest,
        "executable_size": size,
        "implementation": platform.python_implementation(),
        "isolation_flags": isolation_flags,
        "version": platform.python_version(),
    }


def _artifact_reference(path: Path, run_root: Path) -> dict[str, Any]:
    try:
        relative = path.relative_to(run_root).as_posix()
    except ValueError as exc:
        raise BootstrapError("Bootstrap artifact escaped RunRoot.") from exc
    size, digest = _hash_stable(path)
    return {"path": relative, "size": size, "sha256": digest}


def _sanitized_environment(
    config: BootstrapConfig,
    *,
    run_id: str,
    nonce: str,
    bootstrap_record: Path,
    frozen_policy: Path | None,
    frozen_proposal: Path | None,
    frozen_review: Path | None,
) -> dict[str, str]:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetWindowsDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
        kernel32.GetWindowsDirectoryW.restype = ctypes.c_uint
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel32.GetWindowsDirectoryW(buffer, len(buffer))
        if not length or length >= len(buffer):
            raise BootstrapConfigurationError(
                "Trusted Windows directory cannot be determined."
            )
        system_root = buffer.value
        system32 = str(Path(system_root) / "System32")
        path_entries = [system32]
    else:
        system_root = ""
        path_entries = ["/usr/bin", "/bin"]
    env = {
        "FFXIVSHARE_BOOTSTRAP_NONCE": nonce,
        "FFXIVSHARE_BOOTSTRAP_RECORD": str(bootstrap_record),
        "FFXIVSHARE_BOOTSTRAP_RUN_ID": run_id,
        "FFXIVSHARE_BOOTSTRAP_RUN_ROOT": str(config.run_root),
        "FFXIVSHARE_ENV_FILE": str(config.run_root / "runtime-empty.env"),
        "PATH": os.pathsep.join(path_entries),
        "TEMP": str(config.run_root / "tmp"),
        "TMP": str(config.run_root / "tmp"),
    }
    if frozen_policy is not None:
        env["FFXIVSHARE_BOOTSTRAP_POLICY"] = str(frozen_policy)
    if frozen_proposal is not None:
        env["FFXIVSHARE_BOOTSTRAP_PROPOSAL"] = str(frozen_proposal)
    if frozen_review is not None:
        env["FFXIVSHARE_BOOTSTRAP_REVIEW"] = str(frozen_review)
    if os.name == "nt":
        env.update(
            {
                "COMSPEC": str(Path(system32) / "cmd.exe"),
                "PATHEXT": ".COM;.EXE;.BAT;.CMD",
                "SystemRoot": system_root,
                "WINDIR": system_root,
            }
        )
    return env


def _run_bootstrap_impl(
    config: BootstrapConfig,
    *,
    runner: InnerRunner | None = None,
    secure_run_root: Callable[[Path], str] | None = None,
    begin_finalization: Callable[[], None],
    interruption_pending: Callable[[], bool],
) -> BootstrapOutcome:
    prepared = _prepare_config(config)
    config = prepared.config
    source_digest = _execution_bundle_sha256(
        config.repository_root,
        inner_entrypoint=config.inner_entrypoint,
    )
    if (
        prepared.expected_execution_bundle_sha256 is not None
        and source_digest != prepared.expected_execution_bundle_sha256
    ):
        raise BootstrapError("Repository execution bundle does not match its authority.")
    bundle_authority = prepared.expected_execution_bundle_sha256 or source_digest

    access_control, run_directories = _create_run_root(
        config.run_root,
        secure_run_root=secure_run_root or _secure_run_root,
    )
    _assert_run_directories(run_directories)
    frozen_policy: Path | None = None
    frozen_policy_reference: dict[str, Any] | None = None
    frozen_proposal: Path | None = None
    frozen_proposal_reference: dict[str, Any] | None = None
    frozen_review: Path | None = None
    frozen_review_reference: dict[str, Any] | None = None
    if prepared.policy_bytes is not None:
        frozen_policy = config.run_root / "evidence" / "approved-policy.json"
        _write_bytes_create_new(frozen_policy, prepared.policy_bytes)
        frozen_policy_reference = _artifact_reference(frozen_policy, config.run_root)
        if (
            prepared.policy_reference is None
            or frozen_policy_reference["sha256"]
            != prepared.policy_reference["sha256"]
            or frozen_policy_reference["size"] != prepared.policy_reference["size"]
        ):
            raise BootstrapError("Frozen approved policy does not match its input.")
        os.chmod(frozen_policy, stat.S_IREAD)
    if prepared.proposal_bytes is not None:
        frozen_proposal = config.run_root / "evidence" / "approved-proposal.json"
        _write_bytes_create_new(frozen_proposal, prepared.proposal_bytes)
        frozen_proposal_reference = _artifact_reference(
            frozen_proposal,
            config.run_root,
        )
        if (
            prepared.proposal_reference is None
            or frozen_proposal_reference["sha256"]
            != prepared.proposal_reference["sha256"]
            or frozen_proposal_reference["size"]
            != prepared.proposal_reference["size"]
        ):
            raise BootstrapError("Frozen proposal does not match its input.")
        os.chmod(frozen_proposal, stat.S_IREAD)
    if prepared.review_bytes is not None:
        frozen_review = config.run_root / "evidence" / "approved-review.json"
        _write_bytes_create_new(frozen_review, prepared.review_bytes)
        frozen_review_reference = _artifact_reference(frozen_review, config.run_root)
        if (
            prepared.review_reference is None
            or frozen_review_reference["sha256"]
            != prepared.review_reference["sha256"]
            or frozen_review_reference["size"] != prepared.review_reference["size"]
        ):
            raise BootstrapError("Frozen review record does not match its input.")
        os.chmod(frozen_review, stat.S_IREAD)
    snapshot_projection, snapshot_digest = _freeze_execution_bundle(
        config.repository_root,
        config.run_root / "code",
        inner_entrypoint=config.inner_entrypoint,
        expected_sha256=bundle_authority,
    )
    bundle_manifest = config.run_root / "evidence" / "execution-bundle.json"
    _write_json_create_new(
        bundle_manifest,
        {
            "format": BUNDLE_MANIFEST_FORMAT,
            "format_version": BUNDLE_MANIFEST_VERSION,
            "execution_bundle_sha256": snapshot_digest,
            "files": snapshot_projection,
        },
    )
    bundle_manifest_reference = _artifact_reference(bundle_manifest, config.run_root)
    os.chmod(bundle_manifest, stat.S_IREAD)

    isolation_flags = _assert_outer_isolation_flags()
    nonce = secrets.token_hex(32)
    run_id = (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        + "-"
        + nonce[:16]
    )
    bootstrap_record = config.run_root / "evidence" / "bootstrap.json"
    bootstrap_payload = {
        "format": BOOTSTRAP_FORMAT,
        "format_version": BOOTSTRAP_VERSION,
        "generated_at": _utc_now(),
        "run_id": run_id,
        "bootstrap_nonce": nonce,
        "workspace_access_control": access_control,
        "python": _python_identity(config.python_executable, isolation_flags),
        "configuration": {
            "inner_arguments": list(config.inner_arguments),
            "inner_entrypoint": config.inner_entrypoint,
            "mode": config.mode,
            "repository_root": str(config.repository_root),
            "run_root": str(config.run_root),
        },
        "run_layout": [
            {
                "path": (
                    "."
                    if candidate == config.run_root
                    else candidate.relative_to(config.run_root).as_posix()
                ),
                "kind": kind,
                "device": device,
                "inode": inode,
            }
            for candidate, (kind, device, inode) in sorted(
                run_directories.items(),
                key=lambda item: str(item[0]),
            )
        ],
        "policy": (
            {
                "source": prepared.policy_reference,
                "frozen": frozen_policy_reference,
            }
            if prepared.policy_reference is not None
            else None
        ),
        "approval_inputs": (
            {
                "proposal": {
                    "source": prepared.proposal_reference,
                    "frozen": frozen_proposal_reference,
                },
                "review": {
                    "source": prepared.review_reference,
                    "frozen": frozen_review_reference,
                },
            }
            if prepared.proposal_reference is not None
            and prepared.review_reference is not None
            else None
        ),
        "execution_bundle": {
            "authority": (
                "stable_repository_consistency"
                if config.mode == "policy-proposal"
                else "external_digest"
            ),
            "expected_sha256": bundle_authority,
            "frozen_sha256": snapshot_digest,
            "manifest": bundle_manifest_reference,
        },
        "bootstrap_trusted_not_frozen": config.mode == "policy-proposal",
        "source_data_read_by_bootstrap": False,
        "media_read_by_bootstrap": False,
    }
    _write_json_create_new(bootstrap_record, bootstrap_payload)
    bootstrap_record_reference = _artifact_reference(bootstrap_record, config.run_root)
    os.chmod(bootstrap_record, stat.S_IREAD)
    _assert_run_directories(run_directories)
    if (
        _canonical_json_sha256(
            _snapshot_projection(
                config.run_root / "code",
                expected_files=tuple(item["path"] for item in snapshot_projection),
            )
        )
        != snapshot_digest
    ):
        raise BootstrapError("Frozen execution bundle changed before inner launch.")

    inner_argv = [
        str(config.python_executable),
        "-E",
        "-s",
        "-B",
        "-X",
        "utf8",
        str(config.run_root / "code" / Path(config.inner_entrypoint)),
        *config.inner_arguments,
    ]
    environment = _sanitized_environment(
        config,
        run_id=run_id,
        nonce=nonce,
        bootstrap_record=bootstrap_record,
        frozen_policy=frozen_policy,
        frozen_proposal=frozen_proposal,
        frozen_review=frozen_review,
    )
    stdout_path = config.run_root / "logs" / "inner.stdout.log"
    stderr_path = config.run_root / "logs" / "inner.stderr.log"
    try:
        result = (runner or SubprocessInnerRunner()).run(
            argv=inner_argv,
            cwd=config.run_root / "code",
            env=environment,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        begin_finalization()
    except KeyboardInterrupt:
        # The first cancellation is honored as exit 130. Keep the retained
        # RunRoot coherent by finishing the immutable-bundle checks and writing
        # completion.json; subsequent Ctrl+C is deferred through that short
        # finalization window and keeps the final exit code at 130.
        begin_finalization()
        for log_path in (stdout_path, stderr_path):
            if not os.path.lexists(log_path):
                _write_bytes_create_new(log_path, b"")
            else:
                metadata = os.lstat(log_path)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or _is_reparse_point(log_path)
                ):
                    raise BootstrapError(
                        "Interrupted inner log has an unsafe identity."
                    )
                _hash_stable(log_path)
        result = InnerRunResult(130, stdout_path, stderr_path)
    _assert_run_directories(run_directories)
    final_projection = _snapshot_projection(
        config.run_root / "code",
        expected_files=tuple(item["path"] for item in snapshot_projection),
    )
    final_digest = _canonical_json_sha256(final_projection)
    bundle_unchanged = final_digest == snapshot_digest
    bootstrap_record_unchanged = (
        _artifact_reference(bootstrap_record, config.run_root)
        == bootstrap_record_reference
    )
    bundle_manifest_unchanged = (
        _artifact_reference(bundle_manifest, config.run_root)
        == bundle_manifest_reference
    )
    policy_unchanged = True
    if frozen_policy is not None and frozen_policy_reference is not None:
        policy_unchanged = (
            _artifact_reference(frozen_policy, config.run_root)
            == frozen_policy_reference
        )
    proposal_unchanged = True
    if frozen_proposal is not None and frozen_proposal_reference is not None:
        proposal_unchanged = (
            _artifact_reference(frozen_proposal, config.run_root)
            == frozen_proposal_reference
        )
    review_unchanged = True
    if frozen_review is not None and frozen_review_reference is not None:
        review_unchanged = (
            _artifact_reference(frozen_review, config.run_root)
            == frozen_review_reference
        )
    evidence_unchanged = (
        bootstrap_record_unchanged
        and bundle_manifest_unchanged
        and policy_unchanged
        and proposal_unchanged
        and review_unchanged
    )
    completion_record = config.run_root / "evidence" / "completion.json"
    _write_json_create_new(
        completion_record,
        {
            "format": COMPLETION_FORMAT,
            "format_version": COMPLETION_VERSION,
            "generated_at": _utc_now(),
            "run_id": run_id,
            "inner_exit_code": (
                130 if interruption_pending() else result.returncode
            ),
            "execution_bundle_sha256": final_digest,
            "execution_bundle_unchanged": bundle_unchanged,
            "bootstrap_record_unchanged": bootstrap_record_unchanged,
            "bundle_manifest_unchanged": bundle_manifest_unchanged,
            "frozen_policy_unchanged": policy_unchanged,
            "frozen_proposal_unchanged": proposal_unchanged,
            "frozen_review_unchanged": review_unchanged,
            "stdout": _artifact_reference(result.stdout_path, config.run_root),
            "stderr": _artifact_reference(result.stderr_path, config.run_root),
        },
    )
    if not bundle_unchanged or not evidence_unchanged:
        raise BootstrapError(
            "Frozen execution bundle or bootstrap evidence changed during inner execution."
        )
    outcome = BootstrapOutcome(
        130 if interruption_pending() else result.returncode,
        config.run_root,
        bootstrap_record,
        completion_record,
    )
    return outcome


def _rewrite_completion_as_interrupted(
    outcome: BootstrapOutcome,
) -> BootstrapOutcome:
    raw, _digest = _stable_bytes(
        outcome.completion_record,
        maximum_size=4 * 1024 * 1024,
    )
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise BootstrapError("Bootstrap completion cannot be finalized as interrupted.") from exc
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("inner_exit_code"), int)
        or isinstance(payload.get("inner_exit_code"), bool)
    ):
        raise BootstrapError("Bootstrap completion has an invalid inner exit code.")
    if payload["inner_exit_code"] != 130:
        before = os.lstat(outcome.completion_record)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or _is_reparse_point(outcome.completion_record)
        ):
            raise BootstrapError("Bootstrap completion identity is unsafe.")
        payload["inner_exit_code"] = 130
        payload["generated_at"] = _utc_now()
        outcome.completion_record.unlink()
        _write_json_create_new(outcome.completion_record, payload)
    return BootstrapOutcome(
        130,
        outcome.run_root,
        outcome.bootstrap_record,
        outcome.completion_record,
    )


def run_bootstrap(
    config: BootstrapConfig,
    *,
    runner: InnerRunner | None = None,
    secure_run_root: Callable[[Path], str] | None = None,
) -> BootstrapOutcome:
    state = {"finalizing": False, "interruption_pending": False}
    previous_handler: Any | None = None
    installed = False

    def handle_sigint(_signum: int, _frame: Any) -> None:
        state["interruption_pending"] = True
        if not state["finalizing"]:
            raise KeyboardInterrupt

    def begin_finalization() -> None:
        state["finalizing"] = True

    try:
        previous_handler = signal.getsignal(signal.SIGINT)
        if previous_handler != signal.SIG_IGN:
            signal.signal(signal.SIGINT, handle_sigint)
            installed = True
    except (AttributeError, OSError, ValueError):
        pass

    try:
        outcome = _run_bootstrap_impl(
            config,
            runner=runner,
            secure_run_root=secure_run_root,
            begin_finalization=begin_finalization,
            interruption_pending=lambda: bool(state["interruption_pending"]),
        )
        if installed:
            # Seal the tiny handler-restoration window. Any earlier deferred
            # cancellation is now stable and can atomically downgrade a just-
            # published success completion to exit 130.
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        if state["interruption_pending"]:
            outcome = _rewrite_completion_as_interrupted(outcome)
        return outcome
    finally:
        if installed:
            signal.signal(signal.SIGINT, previous_handler)


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze the exact production-copy execution bundle into a create-new "
            "private RunRoot, then launch its inner entrypoint with a sanitized environment."
        )
    )
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--python-executable", required=True)
    parser.add_argument("--run-root", required=True)
    parser.add_argument(
        "--mode",
        choices=("approved-rehearsal", "pinned-bundle", "policy-proposal"),
        required=True,
    )
    parser.add_argument("--policy")
    parser.add_argument("--proposal")
    parser.add_argument("--review-record")
    parser.add_argument("--expected-execution-bundle-sha256")
    parser.add_argument(
        "--inner-entrypoint",
        default="ops/migration/Rehearse-ProductionCopy.py",
    )
    parser.add_argument("inner_arguments", nargs=argparse.REMAINDER)
    return parser.parse_args()


def _config_from_arguments(arguments: argparse.Namespace) -> BootstrapConfig:
    inner_arguments = list(arguments.inner_arguments)
    if inner_arguments and inner_arguments[0] == "--":
        inner_arguments.pop(0)
    return BootstrapConfig(
        repository_root=Path(arguments.repository_root),
        python_executable=Path(arguments.python_executable),
        run_root=Path(arguments.run_root),
        mode=arguments.mode,
        inner_entrypoint=arguments.inner_entrypoint,
        inner_arguments=tuple(inner_arguments),
        policy_path=Path(arguments.policy) if arguments.policy else None,
        proposal_path=Path(arguments.proposal) if arguments.proposal else None,
        review_record_path=(
            Path(arguments.review_record) if arguments.review_record else None
        ),
        expected_execution_bundle_sha256=(
            arguments.expected_execution_bundle_sha256
        ),
    )


def main() -> int:
    try:
        arguments = _parse_arguments()
        config = _config_from_arguments(arguments)
        repository_root = _existing_path(
            config.repository_root,
            label="Repository root",
            directory=True,
        )
        loaded_bootstrap = _existing_path(
            Path(__file__),
            label="Loaded bootstrap",
            directory=False,
        )
        repository_bootstrap = _existing_path(
            repository_root / "ops" / "migration" / "ProductionCopyBootstrap.py",
            label="Repository bootstrap",
            directory=False,
        )
        if _canonical_key(loaded_bootstrap) != _canonical_key(repository_bootstrap):
            raise BootstrapConfigurationError(
                "Loaded bootstrap does not belong to --repository-root."
            )
        outcome = run_bootstrap(config)
    except (BootstrapConfigurationError, BootstrapError, OSError) as exc:
        print(f"Production-copy bootstrap refused: {exc}", file=sys.stderr)
        return 1
    print(
        "Production-copy bootstrap completed inner launch; "
        f"exit={outcome.exit_code}; evidence={outcome.completion_record}"
    )
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
