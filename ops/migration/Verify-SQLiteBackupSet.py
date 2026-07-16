from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any
from uuid import uuid4


REPORT_FORMAT = "ffxivshare-sqlite-backup-set-verification"
REPORT_FORMAT_VERSION = 1
BACKUP_METADATA_SCHEMA_VERSION = 1
BACKUP_METHOD = "sqlite_backup_api"
DATABASE_VENDOR = "sqlite"
SQLITE_MAGIC = b"SQLite format 3\x00"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
UTC_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z$"
)
REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
METADATA_KEYS = {
    "application_version",
    "backup_method",
    "database_vendor",
    "foreign_key_check",
    "generated_at",
    "integrity_check",
    "schema_version",
    "sha256",
    "size",
}
MAX_CHECKSUM_BYTES = 16 * 1024
MAX_METADATA_BYTES = 64 * 1024


class VerificationError(RuntimeError):
    pass


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the three-file evidence set created by backup_database without "
            "opening the database through Django or SQLite."
        )
    )
    parser.add_argument("--database", required=True)
    parser.add_argument("--checksum", required=True)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def _absolute_path(raw_value: str, *, label: str) -> Path:
    path = Path(raw_value)
    if not path.is_absolute():
        raise VerificationError(f"{label} must be an absolute path.")
    if ".." in path.parts:
        raise VerificationError(f"{label} must not contain parent traversal.")
    if os.name == "nt":
        if path.drive.startswith("\\\\"):
            raise VerificationError(f"{label} must be on a local drive, not UNC.")
        without_drive = os.fspath(path)[len(path.drive) :]
        if ":" in without_drive:
            raise VerificationError(
                f"{label} must not use a Windows alternate data stream."
            )
    return path


def _is_reparse_point(path: Path) -> bool:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & REPARSE_POINT_ATTRIBUTE)


def _assert_no_reparse_components(path: Path, *, include_leaf: bool) -> None:
    candidate = path if include_leaf else path.parent
    current = Path(candidate.anchor)
    parts = candidate.parts[1:] if candidate.anchor else candidate.parts
    for part in parts:
        current /= part
        if _is_reparse_point(current):
            raise VerificationError(
                "Paths must not traverse symlinks or reparse points."
            )


def _canonical_path(path: Path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _is_sqlite_sidecar_name(name: str) -> bool:
    lowered = name.casefold()
    return lowered.endswith(("-wal", "-shm", "-journal"))


def _is_within(path: Path, root: Path) -> bool:
    path_key = _canonical_path(path)
    root_key = _canonical_path(root)
    try:
        return os.path.commonpath((path_key, root_key)) == root_key
    except ValueError:
        return False


def _resolve_regular_file(raw_value: str, *, label: str) -> Path:
    candidate = _absolute_path(raw_value, label=label)
    try:
        _assert_no_reparse_components(candidate, include_leaf=True)
        resolved = candidate.resolve(strict=True)
        metadata = os.stat(resolved)
    except FileNotFoundError:
        raise VerificationError(f"{label} must be an existing regular file.") from None
    except OSError:
        raise VerificationError(f"{label} could not be inspected safely.") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or _is_reparse_point(resolved)
    ):
        raise VerificationError(f"{label} must be an existing regular file.")
    return resolved


def _resolve_paths(
    arguments: argparse.Namespace,
) -> tuple[Path, Path, Path, Path, tuple[int, int, int, int, int, int]]:
    database = _resolve_regular_file(arguments.database, label="Database")
    checksum = _resolve_regular_file(arguments.checksum, label="Checksum")
    metadata = _resolve_regular_file(arguments.metadata, label="Metadata")

    if _is_sqlite_sidecar_name(database.name):
        raise VerificationError("Database must not be a SQLite sidecar file.")

    input_directory_key = _canonical_path(database.parent)
    if any(
        _canonical_path(path.parent) != input_directory_key
        for path in (checksum, metadata)
    ):
        raise VerificationError("All three backup inputs must be in one directory.")
    if checksum.name != f"{database.name}.sha256":
        raise VerificationError("Checksum filename does not match the database name.")
    if metadata.name != f"{database.name}.metadata.json":
        raise VerificationError("Metadata filename does not match the database name.")
    if len({_canonical_path(path) for path in (database, checksum, metadata)}) != 3:
        raise VerificationError("Backup inputs must be three distinct files.")

    output_input = _absolute_path(arguments.output, label="Output")
    if os.path.lexists(output_input):
        raise VerificationError("Output already exists; refusing to overwrite it.")
    try:
        _assert_no_reparse_components(output_input, include_leaf=False)
        output_parent = output_input.parent.resolve(strict=True)
        output_parent_metadata = os.stat(output_parent)
    except FileNotFoundError:
        raise VerificationError("Output parent must be an existing directory.") from None
    except OSError:
        raise VerificationError("Output path could not be inspected safely.") from None
    if (
        not stat.S_ISDIR(output_parent_metadata.st_mode)
        or _is_reparse_point(output_parent)
    ):
        raise VerificationError("Output parent must be an existing regular directory.")
    output = output_parent / output_input.name
    if _is_sqlite_sidecar_name(output.name):
        raise VerificationError("Output must not use a SQLite sidecar filename.")
    if _is_within(output, database.parent):
        raise VerificationError("Output must be outside the backup input directory.")

    return (
        database,
        checksum,
        metadata,
        output,
        _stat_identity(output_parent_metadata),
    )


def _stat_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def _assert_no_live_sidecars(database: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        if os.path.lexists(Path(f"{database}{suffix}")):
            raise VerificationError("Backup database has a live SQLite sidecar.")


def _read_stable_bytes(
    path: Path,
    *,
    maximum_size: int,
) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise VerificationError("Backup input is not a regular file.")
            if before.st_size > maximum_size:
                raise VerificationError("Backup evidence file is too large.")
            payload = stream.read(maximum_size + 1)
            after = os.fstat(stream.fileno())
        path_after = os.stat(path)
    except VerificationError:
        raise
    except OSError:
        raise VerificationError("Backup evidence file could not be read safely.") from None
    if len(payload) > maximum_size:
        raise VerificationError("Backup evidence file is too large.")
    identity = _stat_identity(before)
    if identity != _stat_identity(after) or identity != _stat_identity(path_after):
        raise VerificationError("Backup input changed while it was being read.")
    return payload, identity


def _hash_stable_database(
    path: Path,
) -> tuple[str, int, tuple[int, int, int, int, int, int]]:
    digest = sha256()
    byte_count = 0
    prefix = bytearray()
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise VerificationError("Database is not a regular file.")
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                if len(prefix) < len(SQLITE_MAGIC):
                    remaining = len(SQLITE_MAGIC) - len(prefix)
                    prefix.extend(chunk[:remaining])
                digest.update(chunk)
                byte_count += len(chunk)
            after = os.fstat(stream.fileno())
        path_after = os.stat(path)
    except VerificationError:
        raise
    except OSError:
        raise VerificationError("Database could not be read safely.") from None

    identity = _stat_identity(before)
    if (
        identity != _stat_identity(after)
        or identity != _stat_identity(path_after)
        or byte_count != before.st_size
    ):
        raise VerificationError("Database changed while it was being hashed.")
    if bytes(prefix) != SQLITE_MAGIC:
        raise VerificationError("Database does not have a SQLite 3 header.")
    return digest.hexdigest(), byte_count, identity


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _read_metadata(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise VerificationError("Metadata is not strict UTF-8 JSON.") from None
    if not isinstance(value, dict):
        raise VerificationError("Metadata must be a JSON object.")
    if set(value) != METADATA_KEYS:
        raise VerificationError("Metadata keys do not match schema version 1.")
    return value


def _is_utc_timestamp(value: Any) -> bool:
    if (
        not isinstance(value, str)
        or UTC_TIMESTAMP_PATTERN.fullmatch(value) is None
    ):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)


def _validate_metadata(metadata: dict[str, Any]) -> None:
    if type(metadata["schema_version"]) is not int or metadata["schema_version"] != 1:
        raise VerificationError("Metadata schema_version must be 1.")
    if metadata["backup_method"] != BACKUP_METHOD:
        raise VerificationError("Metadata backup_method is invalid.")
    if metadata["database_vendor"] != DATABASE_VENDOR:
        raise VerificationError("Metadata database_vendor is invalid.")
    if not _is_utc_timestamp(metadata["generated_at"]):
        raise VerificationError("Metadata generated_at must be an ISO UTC timestamp.")
    if not isinstance(metadata["application_version"], str):
        raise VerificationError("Metadata application_version must be a string.")
    if (
        not isinstance(metadata["sha256"], str)
        or SHA256_PATTERN.fullmatch(metadata["sha256"]) is None
    ):
        raise VerificationError("Metadata sha256 is invalid.")
    if type(metadata["size"]) is not int or metadata["size"] < 0:
        raise VerificationError("Metadata size must be a non-negative integer.")
    if metadata["integrity_check"] != "ok":
        raise VerificationError("Metadata integrity_check is invalid.")
    if metadata["foreign_key_check"] != "ok":
        raise VerificationError("Metadata foreign_key_check is invalid.")


def _assert_inputs_unchanged(
    paths: tuple[Path, Path, Path],
    identities: tuple[
        tuple[int, int, int, int, int, int],
        tuple[int, int, int, int, int, int],
        tuple[int, int, int, int, int, int],
    ],
) -> None:
    try:
        for path, expected in zip(paths, identities, strict=True):
            if _is_reparse_point(path):
                raise VerificationError("Backup input became a reparse point.")
            current = os.stat(path)
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or _stat_identity(current) != expected
            ):
                raise VerificationError("Backup input changed during verification.")
    except VerificationError:
        raise
    except OSError:
        raise VerificationError("Backup input changed during verification.") from None


def _assert_output_parent_unchanged(
    output: Path,
    expected_identity: tuple[int, int, int, int, int, int],
) -> None:
    try:
        _assert_no_reparse_components(output, include_leaf=False)
        current = os.stat(output.parent)
    except (FileNotFoundError, OSError):
        raise VerificationError("Output parent changed during verification.") from None
    if (
        not stat.S_ISDIR(current.st_mode)
        or _stat_identity(current) != expected_identity
    ):
        raise VerificationError("Output parent changed during verification.")


def _write_json_create_new(output: Path, payload: dict[str, Any]) -> None:
    temporary = output.parent / f".{output.name}.tmp-{uuid4().hex}"
    published = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output)
        published = True
        temporary.unlink()
    except Exception:
        if published and os.path.lexists(output):
            try:
                output.unlink()
            except OSError:
                pass
        raise
    finally:
        if os.path.lexists(temporary):
            try:
                temporary.unlink()
            except OSError:
                pass


def verify_backup_set(arguments: argparse.Namespace) -> Path:
    database, checksum, metadata_path, output, output_parent_identity = (
        _resolve_paths(arguments)
    )
    _assert_no_live_sidecars(database)

    digest, size, database_identity = _hash_stable_database(database)
    checksum_payload, checksum_identity = _read_stable_bytes(
        checksum,
        maximum_size=MAX_CHECKSUM_BYTES,
    )
    metadata_payload, metadata_identity = _read_stable_bytes(
        metadata_path,
        maximum_size=MAX_METADATA_BYTES,
    )

    expected_checksum = f"{digest}  {database.name}\n".encode("utf-8")
    if checksum_payload != expected_checksum:
        raise VerificationError("Checksum file bytes do not match the database.")

    metadata = _read_metadata(metadata_payload)
    _validate_metadata(metadata)
    if metadata["sha256"] != digest or metadata["size"] != size:
        raise VerificationError("Metadata hash or size does not match the database.")

    _assert_no_live_sidecars(database)

    second_digest, second_size, second_database_identity = _hash_stable_database(
        database
    )
    second_checksum_payload, second_checksum_identity = _read_stable_bytes(
        checksum,
        maximum_size=MAX_CHECKSUM_BYTES,
    )
    second_metadata_payload, second_metadata_identity = _read_stable_bytes(
        metadata_path,
        maximum_size=MAX_METADATA_BYTES,
    )
    second_metadata = _read_metadata(second_metadata_payload)
    _validate_metadata(second_metadata)
    second_expected_checksum = (
        f"{second_digest}  {database.name}\n".encode("utf-8")
    )
    if second_checksum_payload != second_expected_checksum:
        raise VerificationError("Checksum file bytes do not match the database.")
    if (
        second_metadata["sha256"] != second_digest
        or second_metadata["size"] != second_size
    ):
        raise VerificationError("Metadata hash or size does not match the database.")
    if (
        second_digest != digest
        or second_size != size
        or second_checksum_payload != checksum_payload
        or second_metadata_payload != metadata_payload
        or second_database_identity != database_identity
        or second_checksum_identity != checksum_identity
        or second_metadata_identity != metadata_identity
    ):
        raise VerificationError("Backup input set changed during verification.")

    _assert_no_live_sidecars(database)
    _assert_inputs_unchanged(
        (database, checksum, metadata_path),
        (
            second_database_identity,
            second_checksum_identity,
            second_metadata_identity,
        ),
    )
    _assert_output_parent_unchanged(output, output_parent_identity)

    report = {
        "format": REPORT_FORMAT,
        "format_version": REPORT_FORMAT_VERSION,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "verified": True,
        "cutover_authorized": False,
        "inspection_required": True,
        "artifact": {
            "producer_generated_at": metadata["generated_at"],
            "sha256": digest,
            "size": size,
        },
        "checks": {
            "checksum_bytes_exact": True,
            "input_set_unchanged": True,
            "metadata_contract": True,
            "sqlite_magic": True,
        },
    }
    _write_json_create_new(output, report)
    return output


def main() -> int:
    try:
        verify_backup_set(_parse_arguments())
    except VerificationError as exc:
        print(f"Backup-set verification failed: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print(
            "Backup-set verification failed during a filesystem operation.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(
            "Backup-set verification failed with an unexpected "
            f"{type(exc).__name__}.",
            file=sys.stderr,
        )
        return 1

    print("Backup-set verification evidence created.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
