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
SCHEMA_INVENTORY_FORMAT = "ffxivshare-sqlite-schema-inventory"
SCHEMA_INVENTORY_FORMAT_VERSION = 1
SCHEMA_OBJECT_TYPES = ("index", "table", "trigger", "view")
SCHEMA_INTERNAL_NAME_PREFIX = "sqlite_"
SCHEMA_INVENTORY_KEYS = frozenset(
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
SCHEMA_OBJECT_KEYS = frozenset({"type", "name", "tbl_name", "sql"})
TABLE_STRUCTURE_FORMAT = "ffxivshare-sqlite-table-structure-inventory"
TABLE_STRUCTURE_FORMAT_VERSION = 1
TABLE_STRUCTURE_KEYS = frozenset(
    {"format", "format_version", "schema", "table_count", "tables", "sha256"}
)
TABLE_ENTRY_KEYS = frozenset(
    {"name", "column_count", "columns", "foreign_keys", "unique_constraints"}
)
TABLE_COLUMN_KEYS = frozenset(
    {"cid", "name", "type", "notnull", "default", "primary_key", "hidden"}
)
TABLE_FOREIGN_KEY_KEYS = frozenset(
    {"id", "sequence", "table", "from", "to", "on_update", "on_delete", "match"}
)
TABLE_UNIQUE_CONSTRAINT_KEYS = frozenset({"columns", "partial"})
TABLE_UNIQUE_COLUMN_KEYS = frozenset(
    {"cid", "name", "descending", "collation"}
)
SQLITE_SEQUENCE_KEYS = frozenset({"present", "count", "high_water_marks"})
SQLITE_SEQUENCE_ENTRY_KEYS = frozenset({"table", "sequence"})
SQLITE_ASCII_IDENTIFIER_FOLD = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
)
SQLITE_HEADER = b"SQLite format 3\x00"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class InspectionError(RuntimeError):
    pass


def _sqlite_identifier_key(value: str) -> str:
    return value.translate(SQLITE_ASCII_IDENTIFIER_FOLD)


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


def _file_identity(
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

    if (
        not stat.S_ISREG(database_metadata.st_mode)
        or database_metadata.st_nlink != 1
    ):
        raise InspectionError(
            "Database must be a single-link existing regular file."
        )

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
        _canonical_key(Path(f"{database}{suffix}"))
        for suffix in SQLITE_SIDECAR_SUFFIXES
    }
    if _canonical_key(output) in forbidden_outputs:
        raise InspectionError("Output must not use a SQLite sidecar path.")

    return database, output


def _assert_no_live_sidecars(database: Path) -> None:
    for suffix in SQLITE_SIDECAR_SUFFIXES:
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


def _canonical_json_sha256(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(serialized.encode("utf-8")).hexdigest()


def _schema_inventory_projection(inventory: dict[str, Any]) -> dict[str, Any]:
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


def _schema_object_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    sql = item["sql"]
    return (
        item["type"],
        item["name"],
        item["tbl_name"],
        sql is not None,
        "" if sql is None else sql,
    )


def _validate_schema_inventory(inventory: dict[str, Any]) -> None:
    if not isinstance(inventory, dict) or set(inventory) != SCHEMA_INVENTORY_KEYS:
        raise InspectionError("SQLite schema inventory has an invalid structure.")
    if (
        inventory["format"] != SCHEMA_INVENTORY_FORMAT
        or type(inventory["format_version"]) is not int
        or inventory["format_version"] != SCHEMA_INVENTORY_FORMAT_VERSION
        or inventory["schema"] != "main"
        or inventory["included_object_types"] != list(SCHEMA_OBJECT_TYPES)
        or inventory["excluded_objects"]
        != {
            "name_prefix": SCHEMA_INTERNAL_NAME_PREFIX,
            "comparison": "SQLite ASCII case-insensitive prefix/identifier comparison",
            "reason": "SQLite-reserved internal and automatically generated objects",
        }
        or inventory["normalization"]
        != {
            "object_order": ["type", "name", "tbl_name", "sql (NULL first)"],
            "string_order": "Unicode code-point order",
            "sql": "verbatim sqlite_schema.sql with NULL preserved",
            "digest": "SHA-256 of canonical UTF-8 JSON excluding sha256",
            "canonical_json": "sorted object keys; no insignificant whitespace",
        }
    ):
        raise InspectionError("SQLite schema inventory metadata is invalid.")

    objects = inventory["objects"]
    object_count = inventory["object_count"]
    if (
        not isinstance(objects, list)
        or not isinstance(object_count, int)
        or isinstance(object_count, bool)
        or object_count != len(objects)
    ):
        raise InspectionError("SQLite schema inventory count is invalid.")

    seen: set[tuple[str, str, str]] = set()
    for item in objects:
        if not isinstance(item, dict) or set(item) != SCHEMA_OBJECT_KEYS:
            raise InspectionError("SQLite schema inventory object is invalid.")
        object_type = item["type"]
        name = item["name"]
        table_name = item["tbl_name"]
        sql = item["sql"]
        if (
            object_type not in SCHEMA_OBJECT_TYPES
            or not isinstance(name, str)
            or not name
            or _sqlite_identifier_key(name).startswith(SCHEMA_INTERNAL_NAME_PREFIX)
            or not isinstance(table_name, str)
            or not table_name
            or not isinstance(sql, str)
            or not sql
        ):
            raise InspectionError("SQLite schema inventory object value is invalid.")
        identity = (
            object_type,
            _sqlite_identifier_key(name),
            _sqlite_identifier_key(table_name),
        )
        if identity in seen:
            raise InspectionError("SQLite schema inventory contains duplicate objects.")
        seen.add(identity)

    if objects != sorted(objects, key=_schema_object_sort_key):
        raise InspectionError("SQLite schema inventory is not canonically ordered.")
    digest = inventory["sha256"]
    if (
        not isinstance(digest, str)
        or SHA256_PATTERN.fullmatch(digest) is None
        or digest != _canonical_json_sha256(_schema_inventory_projection(inventory))
    ):
        raise InspectionError("SQLite schema inventory SHA256 is invalid.")


def _sqlite_schema_inventory(connection: sqlite3.Connection) -> dict[str, Any]:
    objects = []
    for object_type, name, table_name, sql in connection.execute(
        "SELECT type, name, tbl_name, sql FROM main.sqlite_schema "
        "WHERE type IN ('table', 'index', 'trigger', 'view')"
    ):
        # SQLite reserves this prefix for its internal tables and automatically
        # generated objects (for example sqlite_sequence and sqlite_autoindex_*).
        # Keep those out of the human review surface; sqlite_sequence values are
        # recorded separately by _sqlite_sequence().
        if _sqlite_identifier_key(name).startswith(SCHEMA_INTERNAL_NAME_PREFIX):
            continue
        objects.append(
            {
                "type": object_type,
                "name": name,
                "tbl_name": table_name,
                "sql": sql,
            }
        )
    objects.sort(key=_schema_object_sort_key)
    inventory = {
        "format": SCHEMA_INVENTORY_FORMAT,
        "format_version": SCHEMA_INVENTORY_FORMAT_VERSION,
        "schema": "main",
        "included_object_types": list(SCHEMA_OBJECT_TYPES),
        "excluded_objects": {
            "name_prefix": SCHEMA_INTERNAL_NAME_PREFIX,
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
        "object_count": len(objects),
        "objects": objects,
    }
    inventory["sha256"] = _canonical_json_sha256(inventory)
    _validate_schema_inventory(inventory)
    return inventory


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


def _table_structure_projection(inventory: dict[str, Any]) -> dict[str, Any]:
    return {
        key: inventory[key]
        for key in ("format", "format_version", "schema", "table_count", "tables")
    }


def _validate_table_structures(inventory: dict[str, Any]) -> None:
    if not isinstance(inventory, dict) or set(inventory) != TABLE_STRUCTURE_KEYS:
        raise InspectionError("SQLite table structure inventory is invalid.")
    if (
        inventory["format"] != TABLE_STRUCTURE_FORMAT
        or type(inventory["format_version"]) is not int
        or inventory["format_version"] != TABLE_STRUCTURE_FORMAT_VERSION
        or inventory["schema"] != "main"
    ):
        raise InspectionError("SQLite table structure metadata is invalid.")
    tables = inventory["tables"]
    if (
        not isinstance(tables, list)
        or type(inventory["table_count"]) is not int
        or inventory["table_count"] != len(tables)
    ):
        raise InspectionError("SQLite table structure count is invalid.")
    table_names: list[str] = []
    for table in tables:
        if not isinstance(table, dict) or set(table) != TABLE_ENTRY_KEYS:
            raise InspectionError("SQLite table structure entry is invalid.")
        name = table["name"]
        columns = table["columns"]
        if (
            not isinstance(name, str)
            or not name
            or _sqlite_identifier_key(name).startswith(SCHEMA_INTERNAL_NAME_PREFIX)
            or not isinstance(columns, list)
            or type(table["column_count"]) is not int
            or table["column_count"] != len(columns)
            or not columns
        ):
            raise InspectionError("SQLite table structure value is invalid.")
        table_names.append(name)
        column_names: list[str] = []
        column_ids: list[int] = []
        for column in columns:
            if not isinstance(column, dict) or set(column) != TABLE_COLUMN_KEYS:
                raise InspectionError("SQLite table column structure is invalid.")
            if (
                type(column["cid"]) is not int
                or column["cid"] < 0
                or not isinstance(column["name"], str)
                or not column["name"]
                or not isinstance(column["type"], str)
                or type(column["notnull"]) is not int
                or column["notnull"] not in (0, 1)
                or (
                    column["default"] is not None
                    and not isinstance(column["default"], str)
                )
                or type(column["primary_key"]) is not int
                or column["primary_key"] < 0
                or type(column["hidden"]) is not int
                or column["hidden"] not in (0, 1, 2, 3)
            ):
                raise InspectionError("SQLite table column value is invalid.")
            column_names.append(column["name"])
            column_ids.append(column["cid"])
        if len({_sqlite_identifier_key(name) for name in column_names}) != len(
            column_names
        ):
            raise InspectionError("SQLite table columns contain duplicate names.")
        if column_ids != sorted(set(column_ids)):
            raise InspectionError("SQLite table columns are not canonically ordered.")
        foreign_keys = table["foreign_keys"]
        if not isinstance(foreign_keys, list):
            raise InspectionError("SQLite table foreign keys are invalid.")
        foreign_key_order: list[tuple[int, int]] = []
        for foreign_key in foreign_keys:
            if (
                not isinstance(foreign_key, dict)
                or set(foreign_key) != TABLE_FOREIGN_KEY_KEYS
                or type(foreign_key["id"]) is not int
                or foreign_key["id"] < 0
                or type(foreign_key["sequence"]) is not int
                or foreign_key["sequence"] < 0
                or not isinstance(foreign_key["table"], str)
                or not foreign_key["table"]
                or not isinstance(foreign_key["from"], str)
                or not foreign_key["from"]
                or (
                    foreign_key["to"] is not None
                    and (
                        not isinstance(foreign_key["to"], str)
                        or not foreign_key["to"]
                    )
                )
                or not all(
                    isinstance(foreign_key[key], str) and foreign_key[key]
                    for key in ("on_update", "on_delete", "match")
                )
            ):
                raise InspectionError("SQLite table foreign-key value is invalid.")
            foreign_key_order.append((foreign_key["id"], foreign_key["sequence"]))
        if foreign_key_order != sorted(set(foreign_key_order)):
            raise InspectionError("SQLite table foreign keys are not canonical.")
        unique_constraints = table["unique_constraints"]
        if not isinstance(unique_constraints, list):
            raise InspectionError("SQLite table unique constraints are invalid.")
        unique_order: list[str] = []
        for constraint in unique_constraints:
            if (
                not isinstance(constraint, dict)
                or set(constraint) != TABLE_UNIQUE_CONSTRAINT_KEYS
                or type(constraint["partial"]) is not int
                or constraint["partial"] not in (0, 1)
                or not isinstance(constraint["columns"], list)
                or not constraint["columns"]
            ):
                raise InspectionError("SQLite unique-constraint value is invalid.")
            for column in constraint["columns"]:
                if (
                    not isinstance(column, dict)
                    or set(column) != TABLE_UNIQUE_COLUMN_KEYS
                    or type(column["cid"]) is not int
                    or column["cid"] < -2
                    or (column["name"] is not None and not isinstance(column["name"], str))
                    or type(column["descending"]) is not int
                    or column["descending"] not in (0, 1)
                    or (
                        column["collation"] is not None
                        and not isinstance(column["collation"], str)
                    )
                ):
                    raise InspectionError("SQLite unique-constraint column is invalid.")
            unique_order.append(
                json.dumps(
                    constraint,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        if unique_order != sorted(set(unique_order)):
            raise InspectionError("SQLite table unique constraints are not canonical.")
    if len({_sqlite_identifier_key(name) for name in table_names}) != len(table_names):
        raise InspectionError("SQLite table structures contain duplicate names.")
    if table_names != sorted(table_names):
        raise InspectionError("SQLite table structures are not canonically ordered.")
    digest = inventory["sha256"]
    if (
        not isinstance(digest, str)
        or SHA256_PATTERN.fullmatch(digest) is None
        or digest != _canonical_json_sha256(_table_structure_projection(inventory))
    ):
        raise InspectionError("SQLite table structure SHA256 is invalid.")


def _table_structures(
    connection: sqlite3.Connection,
    schema_inventory: dict[str, Any],
) -> dict[str, Any]:
    table_names = sorted(
        item["name"]
        for item in schema_inventory["objects"]
        if item["type"] == "table"
    )
    tables = []
    for table_name in table_names:
        columns = []
        for cid, name, declared_type, notnull, default, primary_key, hidden in (
            connection.execute(
                f"PRAGMA main.table_xinfo({_quoted_identifier(table_name)})"
            )
        ):
            columns.append(
                {
                    "cid": cid,
                    "name": name,
                    "type": declared_type,
                    "notnull": notnull,
                    "default": default,
                    "primary_key": primary_key,
                    "hidden": hidden,
                }
            )
        foreign_keys = [
            {
                "id": row[0],
                "sequence": row[1],
                "table": row[2],
                "from": row[3],
                "to": row[4],
                "on_update": row[5],
                "on_delete": row[6],
                "match": row[7],
            }
            for row in connection.execute(
                f"PRAGMA main.foreign_key_list({_quoted_identifier(table_name)})"
            )
        ]
        foreign_keys.sort(key=lambda item: (item["id"], item["sequence"]))
        unique_constraints = []
        for _sequence, index_name, unique, _origin, partial in connection.execute(
            f"PRAGMA main.index_list({_quoted_identifier(table_name)})"
        ):
            if unique != 1:
                continue
            key_columns = []
            for _seqno, cid, name, descending, collation, key in connection.execute(
                f"PRAGMA main.index_xinfo({_quoted_identifier(index_name)})"
            ):
                if key != 1:
                    continue
                key_columns.append(
                    {
                        "cid": cid,
                        "name": name,
                        "descending": descending,
                        "collation": collation,
                    }
                )
            unique_constraints.append({"columns": key_columns, "partial": partial})
        unique_constraints.sort(
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        tables.append(
            {
                "name": table_name,
                "column_count": len(columns),
                "columns": columns,
                "foreign_keys": foreign_keys,
                "unique_constraints": unique_constraints,
            }
        )
    inventory = {
        "format": TABLE_STRUCTURE_FORMAT,
        "format_version": TABLE_STRUCTURE_FORMAT_VERSION,
        "schema": "main",
        "table_count": len(tables),
        "tables": tables,
    }
    inventory["sha256"] = _canonical_json_sha256(inventory)
    _validate_table_structures(inventory)
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
        value = {"present": False, "count": 0, "high_water_marks": []}
        _validate_sqlite_sequence(value)
        return value

    high_water_marks = [
        {"table": row[0], "sequence": row[1]}
        for row in connection.execute(
            "SELECT name, seq FROM main.sqlite_sequence ORDER BY name COLLATE BINARY"
        )
    ]
    value = {
        "present": True,
        "count": len(high_water_marks),
        "high_water_marks": high_water_marks,
    }
    _validate_sqlite_sequence(value)
    return value


def _validate_sqlite_sequence(value: dict[str, Any]) -> None:
    if not isinstance(value, dict) or set(value) != SQLITE_SEQUENCE_KEYS:
        raise InspectionError("SQLite sequence inventory is invalid.")
    if not isinstance(value["present"], bool):
        raise InspectionError("SQLite sequence presence is invalid.")
    marks = value["high_water_marks"]
    if (
        type(value["count"]) is not int
        or not isinstance(marks, list)
        or value["count"] != len(marks)
        or (value["present"] is False and (value["count"] != 0 or marks != []))
    ):
        raise InspectionError("SQLite sequence count is invalid.")
    tables: list[str] = []
    for item in marks:
        if (
            not isinstance(item, dict)
            or set(item) != SQLITE_SEQUENCE_ENTRY_KEYS
            or not isinstance(item["table"], str)
            or not item["table"]
            or _sqlite_identifier_key(item["table"]).startswith(
                SCHEMA_INTERNAL_NAME_PREFIX
            )
            or type(item["sequence"]) is not int
            or item["sequence"] < 0
        ):
            raise InspectionError("SQLite sequence entry is invalid.")
        tables.append(item["table"])
    if len({_sqlite_identifier_key(table) for table in tables}) != len(tables):
        raise InspectionError("SQLite sequence inventory contains duplicate tables.")
    if tables != sorted(tables):
        raise InspectionError("SQLite sequence inventory is not canonical.")


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
        schema_inventory = _sqlite_schema_inventory(connection)
        return {
            "sqlite_version": sqlite_version,
            "python_sqlite_version": sqlite3.sqlite_version,
            "query_only": True,
            "integrity_check": "ok",
            "foreign_key_check": {"status": "ok", "violations": 0},
            "user_version": user_version,
            "page_count": page_count,
            "tables": tables,
            "sqlite_schema": schema_inventory,
            "table_structures": _table_structures(connection, schema_inventory),
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
    initial_identity = _file_identity(os.stat(database))
    if initial_identity[-1] != 1:
        raise InspectionError("Database became hard-linked before inspection.")
    before_hash = _file_sha256(database)
    if before_hash != expected_sha256:
        raise InspectionError("Database SHA256 does not match the expected value.")

    before_size = database.stat().st_size
    header = _read_sqlite_header(database)
    inspection = _inspect_database(database, header)

    _assert_no_live_sidecars(database)
    after_hash = _file_sha256(database)
    final_metadata = os.stat(database)
    after_size = final_metadata.st_size
    if (
        before_hash != after_hash
        or before_size != after_size
        or _file_identity(final_metadata) != initial_identity
        or final_metadata.st_nlink != 1
    ):
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
