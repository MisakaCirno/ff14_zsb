from __future__ import annotations

import argparse
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from typing import Any
from uuid import uuid4


REPORT_FORMAT = "ffxivshare-sqlite-snapshot-inspection"
REPORT_FORMAT_VERSION = 1
SQLITE_HEADER = b"SQLite format 3\x00"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class InspectionError(RuntimeError):
    pass


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect an immutable, offline SQLite snapshot without modifying it."
        )
    )
    parser.add_argument("--database", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


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
        current = current / part
        if _is_reparse_point(current):
            raise InspectionError(f"Path contains a reparse point: {current}")


def _absolute_path(raw_value: str, *, description: str) -> Path:
    path = Path(raw_value)
    if not path.is_absolute():
        raise InspectionError(f"{description} must be an absolute path.")
    if ".." in path.parts:
        raise InspectionError(f"{description} must not contain parent traversal.")
    if os.name == "nt":
        if path.drive.startswith("\\\\"):
            raise InspectionError(
                f"{description} must be on a local drive, not a UNC path."
            )
        path_without_drive = os.fspath(path)[len(path.drive) :]
        if ":" in path_without_drive:
            raise InspectionError(
                f"{description} must not use a Windows alternate data stream."
            )
    return path


def _canonical_key(path: Path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _validate_paths(database_value: str, output_value: str) -> tuple[Path, Path]:
    database_input = _absolute_path(database_value, description="Database")
    try:
        _assert_no_reparse_components(database_input, include_leaf=True)
        database = database_input.resolve(strict=True)
        database_metadata = os.stat(database)
    except FileNotFoundError:
        raise InspectionError("Database must be an existing regular file.") from None
    except OSError as exc:
        raise InspectionError(f"Database path could not be inspected: {exc}") from None

    if not stat.S_ISREG(database_metadata.st_mode):
        raise InspectionError("Database must be an existing regular file.")

    output_input = _absolute_path(output_value, description="Output")
    if os.path.lexists(output_input):
        raise InspectionError("Output already exists; refusing to overwrite it.")
    try:
        _assert_no_reparse_components(output_input, include_leaf=False)
        output_parent = output_input.parent.resolve(strict=True)
        output_parent_metadata = os.stat(output_parent)
    except FileNotFoundError:
        raise InspectionError("Output parent must be an existing directory.") from None
    except OSError as exc:
        raise InspectionError(f"Output path could not be inspected: {exc}") from None

    if not stat.S_ISDIR(output_parent_metadata.st_mode):
        raise InspectionError("Output parent must be an existing directory.")
    output = output_parent / output_input.name

    if _canonical_key(database) == _canonical_key(output):
        raise InspectionError("Database and output paths must differ.")
    forbidden_outputs = {
        _canonical_key(Path(f"{database}-wal")),
        _canonical_key(Path(f"{database}-shm")),
    }
    if _canonical_key(output) in forbidden_outputs:
        raise InspectionError("Output must not use a SQLite sidecar path.")

    return database, output


def _assert_no_live_sidecars(database: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{database}{suffix}")
        if os.path.lexists(sidecar):
            raise InspectionError(
                f"Offline snapshot has a live SQLite sidecar: {sidecar.name}"
            )


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_sqlite_header(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        header = stream.read(100)
    if len(header) < 100 or header[:16] != SQLITE_HEADER:
        raise InspectionError("Database does not have a valid SQLite 3 header.")

    page_size = int.from_bytes(header[16:18], "big")
    if page_size == 1:
        page_size = 65536
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
        raise InspectionError("SQLite header contains an invalid page size.")

    text_encoding_code = int.from_bytes(header[56:60], "big")
    text_encodings = {
        0: "unspecified",
        1: "UTF-8",
        2: "UTF-16le",
        3: "UTF-16be",
    }
    if text_encoding_code not in text_encodings:
        raise InspectionError("SQLite header contains an invalid text encoding.")

    return {
        "magic": "SQLite format 3",
        "page_size": page_size,
        "write_version": header[18],
        "read_version": header[19],
        "reserved_space": header[20],
        "file_change_counter": int.from_bytes(header[24:28], "big"),
        "database_page_count": int.from_bytes(header[28:32], "big"),
        "schema_cookie": int.from_bytes(header[40:44], "big"),
        "schema_format": int.from_bytes(header[44:48], "big"),
        "text_encoding": text_encodings[text_encoding_code],
        "user_version": int.from_bytes(header[60:64], "big"),
        "application_id": int.from_bytes(header[68:72], "big"),
    }


def _quoted_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_inventory(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM main.sqlite_schema "
            "WHERE type = 'table' ORDER BY name COLLATE BINARY"
        )
    ]
    inventory = []
    for name in names:
        count = connection.execute(
            f"SELECT COUNT(*) FROM main.{_quoted_identifier(name)}"
        ).fetchone()[0]
        inventory.append({"name": name, "row_count": int(count)})
    return inventory


def _django_migrations(
    connection: sqlite3.Connection,
    table_names: set[str],
) -> dict[str, Any]:
    if "django_migrations" not in table_names:
        return {"present": False, "count": 0, "applied": []}

    applied = [
        {
            "app": row[0],
            "name": row[1],
            "applied": row[2],
        }
        for row in connection.execute(
            "SELECT app, name, applied FROM main.django_migrations "
            "ORDER BY app COLLATE BINARY, name COLLATE BINARY, id"
        )
    ]
    return {"present": True, "count": len(applied), "applied": applied}


def _sqlite_sequence(
    connection: sqlite3.Connection,
    table_names: set[str],
) -> dict[str, Any]:
    if "sqlite_sequence" not in table_names:
        return {"present": False, "count": 0, "high_water_marks": []}

    high_water_marks = [
        {"table": row[0], "sequence": row[1]}
        for row in connection.execute(
            "SELECT name, seq FROM main.sqlite_sequence ORDER BY name COLLATE BINARY"
        )
    ]
    return {
        "present": True,
        "count": len(high_water_marks),
        "high_water_marks": high_water_marks,
    }


def _count_foreign_key_violations(connection: sqlite3.Connection) -> int:
    count = 0
    cursor = connection.execute("PRAGMA main.foreign_key_check")
    while True:
        rows = cursor.fetchmany(1000)
        if not rows:
            return count
        count += len(rows)


def _inspect_database(database: Path, header: dict[str, Any]) -> dict[str, Any]:
    uri = database.as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, timeout=0)
    try:
        connection.execute("PRAGMA query_only=ON")
        query_only = connection.execute("PRAGMA query_only").fetchone()[0]
        if query_only != 1:
            raise InspectionError("SQLite connection is not query-only.")

        integrity_rows = [
            row[0] for row in connection.execute("PRAGMA main.integrity_check")
        ]
        if integrity_rows != ["ok"]:
            raise InspectionError("PRAGMA integrity_check failed.")

        foreign_key_violations = _count_foreign_key_violations(connection)
        if foreign_key_violations:
            raise InspectionError(
                "PRAGMA foreign_key_check found "
                f"{foreign_key_violations} violation(s)."
            )

        sqlite_version = connection.execute("SELECT sqlite_version()").fetchone()[0]
        user_version = int(connection.execute("PRAGMA main.user_version").fetchone()[0])
        page_count = int(connection.execute("PRAGMA main.page_count").fetchone()[0])
        if user_version != header["user_version"]:
            raise InspectionError("SQLite header and PRAGMA user_version disagree.")

        tables = _table_inventory(connection)
        table_names = {item["name"] for item in tables}
        return {
            "sqlite_version": sqlite_version,
            "python_sqlite_version": sqlite3.sqlite_version,
            "query_only": True,
            "integrity_check": "ok",
            "foreign_key_check": {"status": "ok", "violations": 0},
            "user_version": user_version,
            "page_count": page_count,
            "tables": tables,
            "django_migrations": _django_migrations(connection, table_names),
            "sqlite_sequence": _sqlite_sequence(connection, table_names),
        }
    finally:
        connection.close()


def _write_json_atomic(output: Path, payload: dict[str, Any]) -> None:
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

        # Creating a hard link publishes the complete file atomically and fails
        # instead of replacing an output that appeared after validation.
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


def inspect_snapshot(
    database_value: str,
    expected_sha256: str,
    output_value: str,
) -> Path:
    if SHA256_PATTERN.fullmatch(expected_sha256) is None:
        raise InspectionError(
            "Expected SHA256 must be exactly 64 lowercase hexadecimal characters."
        )

    database, output = _validate_paths(database_value, output_value)
    _assert_no_live_sidecars(database)
    before_hash = _file_sha256(database)
    if before_hash != expected_sha256:
        raise InspectionError("Database SHA256 does not match the expected value.")

    before_size = database.stat().st_size
    header = _read_sqlite_header(database)
    inspection = _inspect_database(database, header)

    _assert_no_live_sidecars(database)
    after_hash = _file_sha256(database)
    after_size = database.stat().st_size
    if before_hash != after_hash or before_size != after_size:
        raise InspectionError("Database changed while it was being inspected.")

    report = {
        "format": REPORT_FORMAT,
        "format_version": REPORT_FORMAT_VERSION,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "database": {
            "path": str(database),
            "size_bytes": before_size,
            "sha256": before_hash,
            "sha256_before": before_hash,
            "sha256_after": after_hash,
            "source_unchanged": True,
            "header": header,
        },
        "inspection": inspection,
    }
    _write_json_atomic(output, report)
    return output


def main() -> int:
    arguments = _parse_arguments()
    try:
        output = inspect_snapshot(
            arguments.database,
            arguments.expected_sha256,
            arguments.output,
        )
    except (InspectionError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"Inspection failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"Inspection failed with an unexpected {type(exc).__name__}.",
            file=sys.stderr,
        )
        return 1

    print(f"Inspection report created: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
