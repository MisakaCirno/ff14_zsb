"""Fail-closed comparison of two independently validated immutable v3 exports."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any
from uuid import uuid4


DATASET_FORMAT = "ffxivshare-jsonl"
DATASET_VERSION = 3
DATASET_CODEC = "canonical-jsonl-utc-microseconds"
SCHEMA_FINGERPRINT = (
    "5748cb65c7617cef02e2141435c80530b6736b1bd4c5ab91419772a374ad55c2"
)
MODEL_SCHEMA_SIGNATURE = (
    "9b91a3b943d2986115508db51c216d94040053ec2c8e19b900acd2e0ddfdd685"
)
REPORT_FORMAT = "ffxivshare-site-data-export-comparison"
REPORT_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
VALIDATION_REPORT_FILENAME = "validation-report.json"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
UTC_MICROSECONDS_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
)
REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

ENTITY_CONTRACT = {
    "groups": ("auth.group", "groups.jsonl", "auth_group", "id"),
    "users": ("auth.user", "users.jsonl", "auth_user", "id"),
    "user_profiles": (
        "shares.userprofile",
        "user_profiles.jsonl",
        "shares_userprofile",
        "id",
    ),
    "shares": ("shares.share", "shares.jsonl", "shares_share", "id"),
    "collections": (
        "shares.collection",
        "collections.jsonl",
        "shares_collection",
        "id",
    ),
    "collection_items": (
        "shares.collectionitem",
        "collection_items.jsonl",
        "shares_collectionitem",
        "id",
    ),
    "reports": ("shares.report", "reports.jsonl", "shares_report", "id"),
    "share_logs": (
        "shares.sharelog",
        "share_logs.jsonl",
        "shares_sharelog",
        "id",
    ),
    "announcements": (
        "shares.announcement",
        "announcements.jsonl",
        "shares_announcement",
        "id",
    ),
    "site_messages": (
        "shares.sitemessage",
        "site_messages.jsonl",
        "shares_sitemessage",
        "id",
    ),
    "admin_log_entries": (
        "admin.logentry",
        "admin_log_entries.jsonl",
        "django_admin_log",
        "id",
    ),
}
DIRECT_TABLES = sorted(item[2] for item in ENTITY_CONTRACT.values())
EMBEDDED_TABLES = [
    "auth_group_permissions",
    "auth_user_groups",
    "auth_user_user_permissions",
    "shares_share_favorites",
    "shares_share_likes",
]
REGENERATED_TABLES = [
    "auth_permission",
    "django_content_type",
    "django_migrations",
]
SQLITE_INTERNAL_TABLES = {
    "sqlite_sequence",
    "sqlite_stat1",
    "sqlite_stat4",
}
REQUIRED_FILES = {
    MANIFEST_FILENAME,
    *(item[1] for item in ENTITY_CONTRACT.values()),
}
ALLOWED_FILES = {*REQUIRED_FILES, VALIDATION_REPORT_FILENAME}


class ComparisonError(RuntimeError):
    pass


class ContractError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DatasetSummary:
    manifest_sha256: str
    validation_report_sha256: str | None
    application_version: str
    exported_at: str
    source_database: str
    record_count: int
    entities: dict[str, Any]
    dependencies: dict[str, Any]
    table_semantics: dict[str, Any]
    migration_nodes: list[tuple[str, str]]
    migration_leaf_nodes: list[tuple[str, str]]
    sequences: dict[str, Any]
    session: dict[str, Any]


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ContractError(code)


def _exact_keys(value: Any, expected: set[str], code: str) -> dict[str, Any]:
    _require(isinstance(value, dict) and set(value) == expected, code)
    return value


def _is_nonnegative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_v3_timestamp(value: Any) -> bool:
    if (
        not isinstance(value, str)
        or UTC_MICROSECONDS_PATTERN.fullmatch(value) is None
    ):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return False
    return True


def _is_reparse_point(path: Path) -> bool:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & REPARSE_POINT_ATTRIBUTE)


def _absolute_path(raw_value: str, *, label: str) -> Path:
    path = Path(raw_value).expanduser()
    if not path.is_absolute():
        raise ComparisonError(f"{label} must be an absolute path.")
    if ".." in path.parts:
        raise ComparisonError(f"{label} must not contain parent traversal.")
    if os.name == "nt":
        if path.drive.startswith("\\\\"):
            raise ComparisonError(f"{label} must be on a local drive, not UNC.")
        without_drive = os.fspath(path)[len(path.drive) :]
        if ":" in without_drive:
            raise ComparisonError(
                f"{label} must not use a Windows alternate data stream."
            )
    return path


def _assert_no_reparse_components(path: Path, *, include_leaf: bool) -> None:
    candidate = path if include_leaf else path.parent
    current = Path(candidate.anchor)
    parts = candidate.parts[1:] if candidate.anchor else candidate.parts
    for part in parts:
        current /= part
        if _is_reparse_point(current):
            raise ComparisonError("Paths must not traverse symlinks or reparse points.")


def _resolve_dataset(raw_value: str, *, label: str) -> Path:
    candidate = _absolute_path(raw_value, label=label)
    try:
        _assert_no_reparse_components(candidate, include_leaf=True)
        resolved = candidate.resolve(strict=True)
        metadata = os.stat(resolved)
    except FileNotFoundError:
        raise ComparisonError(f"{label} must be an existing directory.") from None
    except OSError:
        raise ComparisonError(f"{label} could not be inspected safely.") from None
    if not stat.S_ISDIR(metadata.st_mode) or _is_reparse_point(resolved):
        raise ComparisonError(f"{label} must be an existing regular directory.")
    return resolved


def _canonical_path(path: Path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_paths(arguments: argparse.Namespace) -> tuple[Path, Path, Path]:
    source = _resolve_dataset(arguments.source, label="Source dataset")
    target = _resolve_dataset(arguments.target, label="Target dataset")
    if _canonical_path(source) == _canonical_path(target):
        raise ComparisonError("Source and target datasets must differ.")

    output_input = _absolute_path(arguments.output, label="Output")
    if os.path.lexists(output_input):
        raise ComparisonError("Output already exists; refusing to overwrite it.")
    try:
        _assert_no_reparse_components(output_input, include_leaf=False)
        output_parent = output_input.parent.resolve(strict=True)
        parent_metadata = os.stat(output_parent)
    except FileNotFoundError:
        raise ComparisonError("Output parent must be an existing directory.") from None
    except OSError:
        raise ComparisonError("Output path could not be inspected safely.") from None
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise ComparisonError("Output parent must be an existing directory.")
    output = output_parent / output_input.name
    if _is_within(output, source) or _is_within(output, target):
        raise ComparisonError("Output must be outside both immutable datasets.")
    return source, target, output


def _stat_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _directory_inventory(root: Path) -> dict[str, tuple[int, int, int, int, int]]:
    inventory: dict[str, tuple[int, int, int, int, int]] = {}
    try:
        entries = list(os.scandir(root))
    except OSError:
        raise ContractError("directory_unreadable") from None
    for entry in entries:
        path = Path(entry.path)
        try:
            if _is_reparse_point(path) or not entry.is_file(follow_symlinks=False):
                raise ContractError("non_regular_entry")
            inventory[entry.name] = _stat_identity(entry.stat(follow_symlinks=False))
        except OSError:
            raise ContractError("entry_unreadable") from None
    names = set(inventory)
    _require(REQUIRED_FILES <= names <= ALLOWED_FILES, "file_inventory")
    return inventory


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _read_stable_bytes(path: Path, *, maximum_size: int | None = None) -> bytes:
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if maximum_size is not None and before.st_size > maximum_size:
                raise ContractError("metadata_file_too_large")
            payload = stream.read()
            after = os.fstat(stream.fileno())
        path_after = path.stat()
    except ContractError:
        raise
    except OSError:
        raise ContractError("file_unreadable") from None
    _require(
        _stat_identity(before) == _stat_identity(after)
        and _stat_identity(after) == _stat_identity(path_after),
        "file_changed",
    )
    return payload


def _read_json(path: Path) -> tuple[Any, str]:
    payload = _read_stable_bytes(path, maximum_size=16 * 1024 * 1024)
    return _decode_json(payload, code="invalid_json"), sha256(payload).hexdigest()


def _decode_json(payload: bytes, *, code: str) -> Any:
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ContractError(code) from None


def _hash_jsonl(path: Path, *, expected_model: str) -> tuple[str, int, int]:
    digest = sha256()
    line_count = 0
    max_pk = 0
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            for raw_line in stream:
                digest.update(raw_line)
                line_count += 1
                _require(raw_line.endswith(b"\n"), "entity_record_termination")
                value = _decode_json(raw_line, code="entity_record_json")
                record = _exact_keys(
                    value,
                    {"model", "pk", "fields"},
                    "entity_record_shape",
                )
                _require(record["model"] == expected_model, "entity_record_model")
                pk = record["pk"]
                _require(
                    isinstance(pk, int)
                    and not isinstance(pk, bool)
                    and pk > max_pk,
                    "entity_record_primary_key",
                )
                _require(isinstance(record["fields"], dict), "entity_record_fields")
                try:
                    canonical = (
                        json.dumps(
                            record,
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ).encode("utf-8")
                        + b"\n"
                    )
                except (
                    TypeError,
                    ValueError,
                    UnicodeEncodeError,
                    RecursionError,
                ):
                    raise ContractError("entity_record_json") from None
                _require(raw_line == canonical, "entity_record_canonical")
                max_pk = pk
            after = os.fstat(stream.fileno())
        path_after = path.stat()
    except OSError:
        raise ContractError("entity_file_unreadable") from None
    _require(
        _stat_identity(before) == _stat_identity(after)
        and _stat_identity(after) == _stat_identity(path_after),
        "entity_file_changed",
    )
    return digest.hexdigest(), line_count, max_pk


def _canonical_string_tuples(value: Any, width: int, code: str) -> list[tuple[str, ...]]:
    _require(isinstance(value, list), code)
    result: list[tuple[str, ...]] = []
    for item in value:
        _require(
            isinstance(item, list)
            and len(item) == width
            and all(isinstance(part, str) for part in item),
            code,
        )
        result.append(tuple(item))
    _require(result == sorted(set(result)), code)
    return result


def _validate_dependencies(value: Any) -> dict[str, Any]:
    dependencies = _exact_keys(
        value,
        {"content_types", "permissions", "references"},
        "dependencies_shape",
    )
    content_types = _canonical_string_tuples(
        dependencies["content_types"], 2, "content_type_catalog"
    )
    _require(isinstance(dependencies["permissions"], list), "permission_catalog")
    permission_keys: list[tuple[str, str, str]] = []
    for permission in dependencies["permissions"]:
        item = _exact_keys(
            permission, {"natural_key", "name"}, "permission_catalog"
        )
        key = _canonical_string_tuples(
            [item["natural_key"]], 3, "permission_catalog"
        )[0]
        _require(isinstance(item["name"], str), "permission_catalog")
        permission_keys.append(key)
    _require(permission_keys == sorted(set(permission_keys)), "permission_catalog")

    references = _exact_keys(
        dependencies["references"],
        {"content_types", "permissions"},
        "dependency_references_shape",
    )
    referenced_content_types = _canonical_string_tuples(
        references["content_types"], 2, "content_type_references"
    )
    referenced_permissions = _canonical_string_tuples(
        references["permissions"], 3, "permission_references"
    )
    _require(set(referenced_content_types) <= set(content_types), "content_type_references")
    _require(set(referenced_permissions) <= set(permission_keys), "permission_references")
    _require(
        all((key[1], key[2]) in set(content_types) for key in permission_keys),
        "permission_content_type_mapping",
    )
    return dependencies


def _canonical_string_list(value: Any, code: str) -> list[str]:
    _require(
        isinstance(value, list) and all(isinstance(item, str) for item in value),
        code,
    )
    _require(value == sorted(set(value)), code)
    return value


def _validate_session(value: Any) -> dict[str, Any]:
    session = _exact_keys(
        value,
        {
            "table",
            "policy",
            "source_row_count",
            "source_unexpired_count",
            "source_latest_expiry",
            "target_required_row_count",
        },
        "session_shape",
    )
    _require(session["table"] == "django_session", "session_table")
    _require(
        session["policy"] == "force_logout_at_cutover", "session_policy"
    )
    _require(
        _is_nonnegative_integer(session["target_required_row_count"])
        and session["target_required_row_count"] == 0,
        "session_target_requirement",
    )
    row_count = session["source_row_count"]
    unexpired_count = session["source_unexpired_count"]
    _require(_is_nonnegative_integer(row_count), "session_row_count")
    _require(_is_nonnegative_integer(unexpired_count), "session_unexpired_count")
    _require(unexpired_count <= row_count, "session_count_invariant")
    latest_expiry = session["source_latest_expiry"]
    _require(
        latest_expiry is None
        or _is_v3_timestamp(latest_expiry),
        "session_latest_expiry",
    )
    if row_count == 0:
        _require(
            unexpired_count == 0 and latest_expiry is None,
            "empty_session_invariant",
        )
    else:
        _require(latest_expiry is not None, "nonempty_session_invariant")
    return session


def _validate_table_projection(
    value: Any, *, source_database: str, session: dict[str, Any]
) -> dict[str, Any]:
    projection = _exact_keys(
        value,
        {
            "direct",
            "embedded",
            "regenerated",
            "excluded",
            "internal",
            "unknown_empty",
            "unknown_nonempty",
            "unsupported_objects",
        },
        "table_projection_shape",
    )
    _require(projection["direct"] == DIRECT_TABLES, "direct_table_projection")
    _require(projection["embedded"] == EMBEDDED_TABLES, "embedded_table_projection")
    _require(
        projection["regenerated"] == REGENERATED_TABLES,
        "regenerated_table_projection",
    )
    excluded = _exact_keys(
        projection["excluded"], {"django_session"}, "excluded_table_projection"
    )
    _require(excluded["django_session"] == session, "excluded_session_projection")
    internal = _canonical_string_list(projection["internal"], "internal_tables")
    if source_database == "sqlite":
        _require(set(internal) <= SQLITE_INTERNAL_TABLES, "internal_tables")
    else:
        _require(internal == [], "internal_tables")
    unknown_empty = _canonical_string_list(
        projection["unknown_empty"], "unknown_empty_tables"
    )
    _require(projection["unknown_nonempty"] == {}, "unknown_nonempty_tables")
    _require(projection["unsupported_objects"] == {}, "unsupported_objects")
    return {
        "direct": projection["direct"],
        "embedded": projection["embedded"],
        "regenerated": projection["regenerated"],
        "excluded": {
            "django_session": {
                "table": session["table"],
                "policy": session["policy"],
                "target_required_row_count": session["target_required_row_count"],
            }
        },
    }


def _validate_migrations(value: Any) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    projection = _exact_keys(
        value, {"table", "applied", "leaf_nodes"}, "migration_projection_shape"
    )
    _require(projection["table"] == "django_migrations", "migration_table")
    _require(isinstance(projection["applied"], list), "applied_migrations")
    nodes: list[tuple[str, str]] = []
    for raw_item in projection["applied"]:
        item = _exact_keys(
            raw_item, {"app", "name", "applied_at"}, "applied_migrations"
        )
        _require(
            isinstance(item["app"], str)
            and isinstance(item["name"], str)
            and _is_v3_timestamp(item["applied_at"]),
            "applied_migrations",
        )
        nodes.append((item["app"], item["name"]))
    _require(nodes == sorted(set(nodes)), "applied_migrations")
    leaf_nodes = _canonical_string_tuples(
        projection["leaf_nodes"], 2, "migration_leaf_nodes"
    )
    _require(bool(leaf_nodes), "migration_leaf_nodes")
    _require(set(leaf_nodes) <= set(nodes), "migration_leaf_mapping")
    return nodes, leaf_nodes


def _validate_sequences(value: Any, max_primary_keys: dict[str, int]) -> dict[str, Any]:
    identity = _exact_keys(value, {"sequences"}, "identity_shape")
    sequences = _exact_keys(
        identity["sequences"], set(ENTITY_CONTRACT), "sequence_entity_mapping"
    )
    for entity_name, (model, filename, table, pk_field) in ENTITY_CONTRACT.items():
        del model, filename
        item = _exact_keys(
            sequences[entity_name],
            {"table", "pk_field", "max_live_pk", "next_value_floor"},
            "sequence_shape",
        )
        max_live_pk = item["max_live_pk"]
        next_value_floor = item["next_value_floor"]
        _require(
            item["table"] == table and item["pk_field"] == pk_field,
            "sequence_entity_mapping",
        )
        _require(_is_nonnegative_integer(max_live_pk), "sequence_max_live_pk")
        _require(_is_nonnegative_integer(next_value_floor), "sequence_next_value_floor")
        _require(next_value_floor >= max_live_pk + 1, "sequence_next_value_floor")
        _require(
            max_live_pk == max_primary_keys[entity_name],
            "sequence_max_live_pk",
        )
    return sequences


def _validate_generated_at(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == UTC.utcoffset(parsed)


def _validate_validation_report(value: Any, counts: dict[str, int]) -> None:
    report = _exact_keys(
        value,
        {
            "format",
            "format_version",
            "generated_at",
            "dataset",
            "valid",
            "entity_counts",
            "errors",
            "warnings",
            "quarantined_records",
        },
        "validation_report_shape",
    )
    _require(report["format"] == DATASET_FORMAT, "validation_report_format")
    _require(
        _is_nonnegative_integer(report["format_version"])
        and report["format_version"] == DATASET_VERSION,
        "validation_report_version",
    )
    _require(_validate_generated_at(report["generated_at"]), "validation_report_time")
    _require(isinstance(report["dataset"], str), "validation_report_dataset")
    _require(report["valid"] is True, "validation_report_invalid")
    _require(report["entity_counts"] == counts, "validation_report_counts")
    _require(report["errors"] == [], "validation_report_errors")
    _require(
        isinstance(report["warnings"], list)
        and all(isinstance(item, str) for item in report["warnings"]),
        "validation_report_warnings",
    )
    _require(report["quarantined_records"] == [], "validation_report_quarantine")


def _inspect_dataset(root: Path) -> DatasetSummary:
    inventory_before = _directory_inventory(root)
    manifest, manifest_sha256 = _read_json(root / MANIFEST_FILENAME)
    validation_report_sha256 = None
    validation_report = None
    if VALIDATION_REPORT_FILENAME in inventory_before:
        validation_report, validation_report_sha256 = _read_json(
            root / VALIDATION_REPORT_FILENAME
        )
    manifest = _exact_keys(
        manifest,
        {
            "format",
            "format_version",
            "codec",
            "schema_fingerprint",
            "model_schema_signature",
            "application_version",
            "exported_at",
            "source_database",
            "entities",
            "dependencies",
            "migration_projection",
            "identity",
            "table_projection",
            "session_projection",
        },
        "manifest_shape",
    )
    _require(manifest["format"] == DATASET_FORMAT, "dataset_format")
    _require(
        _is_nonnegative_integer(manifest["format_version"])
        and manifest["format_version"] == DATASET_VERSION,
        "dataset_version",
    )
    _require(manifest["codec"] == DATASET_CODEC, "dataset_codec")
    _require(manifest["schema_fingerprint"] == SCHEMA_FINGERPRINT, "schema_fingerprint")
    _require(
        manifest["model_schema_signature"] == MODEL_SCHEMA_SIGNATURE,
        "model_schema_signature",
    )
    _require(isinstance(manifest["application_version"], str), "application_version")
    _require(_is_v3_timestamp(manifest["exported_at"]), "exported_at")
    source_database = manifest["source_database"]
    _require(
        isinstance(source_database, str)
        and source_database in {"sqlite", "postgresql"},
        "source_database",
    )

    entities = _exact_keys(
        manifest["entities"], set(ENTITY_CONTRACT), "entity_projection"
    )
    counts: dict[str, int] = {}
    max_primary_keys: dict[str, int] = {}
    for entity_name, (model, filename, _table, _pk) in ENTITY_CONTRACT.items():
        metadata = _exact_keys(
            entities[entity_name], {"model", "file", "count", "sha256"}, "entity_metadata"
        )
        _require(
            metadata["model"] == model and metadata["file"] == filename,
            "entity_mapping",
        )
        count = metadata["count"]
        expected_sha256 = metadata["sha256"]
        _require(_is_nonnegative_integer(count), "entity_count")
        _require(
            isinstance(expected_sha256, str)
            and SHA256_PATTERN.fullmatch(expected_sha256) is not None,
            "entity_sha256",
        )
        actual_sha256, line_count, max_pk = _hash_jsonl(
            root / filename,
            expected_model=model,
        )
        _require(actual_sha256 == expected_sha256, "entity_file_sha256")
        _require(line_count == count, "entity_file_count")
        counts[entity_name] = count
        max_primary_keys[entity_name] = max_pk

    if validation_report is not None:
        _validate_validation_report(validation_report, counts)

    dependencies = _validate_dependencies(manifest["dependencies"])
    session = _validate_session(manifest["session_projection"])
    table_semantics = _validate_table_projection(
        manifest["table_projection"],
        source_database=source_database,
        session=session,
    )
    migration_nodes, migration_leaf_nodes = _validate_migrations(
        manifest["migration_projection"]
    )
    sequences = _validate_sequences(manifest["identity"], max_primary_keys)
    inventory_after = _directory_inventory(root)
    _require(inventory_after == inventory_before, "dataset_changed")
    return DatasetSummary(
        manifest_sha256=manifest_sha256,
        validation_report_sha256=validation_report_sha256,
        application_version=manifest["application_version"],
        exported_at=manifest["exported_at"],
        source_database=source_database,
        record_count=sum(counts.values()),
        entities=entities,
        dependencies=dependencies,
        table_semantics=table_semantics,
        migration_nodes=migration_nodes,
        migration_leaf_nodes=migration_leaf_nodes,
        sequences=sequences,
        session=session,
    )


def _compare_summaries(
    source: DatasetSummary, target: DatasetSummary
) -> tuple[list[str], dict[str, bool]]:
    issues: list[str] = []
    checks = {
        "frozen_v3_contract": True,
        "entity_projection": source.entities == target.entities,
        "dependency_projection": True,
        "table_projection": source.table_semantics == target.table_semantics,
        "migration_projection": (
            set(source.migration_nodes) <= set(target.migration_nodes)
            and set(source.migration_leaf_nodes) <= set(target.migration_nodes)
        ),
        "sequence_projection": True,
        "session_projection": True,
    }
    source_content_types = {
        tuple(item) for item in source.dependencies["content_types"]
    }
    target_content_types = {
        tuple(item) for item in target.dependencies["content_types"]
    }
    source_permissions = {
        tuple(item["natural_key"]): item["name"]
        for item in source.dependencies["permissions"]
    }
    target_permissions = {
        tuple(item["natural_key"]): item["name"]
        for item in target.dependencies["permissions"]
    }
    if (
        source.dependencies["references"] != target.dependencies["references"]
        or not source_content_types <= target_content_types
        or any(target_permissions.get(key) != name for key, name in source_permissions.items())
    ):
        checks["dependency_projection"] = False

    for check_name in (
        "entity_projection",
        "dependency_projection",
        "table_projection",
        "migration_projection",
    ):
        if not checks[check_name]:
            issues.append(f"comparison.{check_name}")

    for entity_name in ENTITY_CONTRACT:
        source_sequence = source.sequences[entity_name]
        target_sequence = target.sequences[entity_name]
        if (
            source_sequence["table"] != target_sequence["table"]
            or source_sequence["pk_field"] != target_sequence["pk_field"]
            or source_sequence["max_live_pk"] != target_sequence["max_live_pk"]
            or target_sequence["next_value_floor"]
            < source_sequence["next_value_floor"]
        ):
            checks["sequence_projection"] = False
            issues.append(f"comparison.sequence_projection.{entity_name}")

    if (
        target.session["source_row_count"] != 0
        or target.session["source_unexpired_count"] != 0
        or target.session["source_latest_expiry"] is not None
    ):
        checks["session_projection"] = False
        issues.append("comparison.target_sessions_not_empty")
    return issues, checks


def _artifact(summary: DatasetSummary | None) -> dict[str, Any]:
    if summary is None:
        return {"contract_valid": False}
    artifact = {
        "contract_valid": True,
        "manifest_sha256": summary.manifest_sha256,
        "application_version": summary.application_version,
        "exported_at": summary.exported_at,
        "source_database": summary.source_database,
        "entity_count": len(summary.entities),
        "record_count": summary.record_count,
    }
    if summary.validation_report_sha256 is not None:
        artifact["validation_report_sha256"] = summary.validation_report_sha256
    return artifact


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


def compare_exports(source_root: Path, target_root: Path, output: Path) -> bool:
    issues: list[str] = []
    source: DatasetSummary | None = None
    target: DatasetSummary | None = None
    for label, root in (("source", source_root), ("target", target_root)):
        try:
            summary = _inspect_dataset(root)
        except ContractError as exc:
            issues.append(f"{label}.contract.{exc.code}")
            summary = None
        if label == "source":
            source = summary
        else:
            target = summary

    checks = {
        "source_contract": source is not None,
        "target_contract": target is not None,
        "frozen_v3_contract": False,
        "entity_projection": False,
        "dependency_projection": False,
        "table_projection": False,
        "migration_projection": False,
        "sequence_projection": False,
        "session_projection": False,
    }
    if source is not None and target is not None:
        comparison_issues, comparison_checks = _compare_summaries(source, target)
        issues.extend(comparison_issues)
        checks.update(comparison_checks)

    equivalent = not issues and source is not None and target is not None
    evidence = {
        "format": REPORT_FORMAT,
        "format_version": REPORT_VERSION,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "source": _artifact(source),
        "target": _artifact(target),
        "checks": checks,
        "issues": issues,
        "equivalent": equivalent,
        "cutover_authorized": False,
    }
    _write_json_create_new(output, evidence)
    return equivalent


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare two independently validated, immutable v3 site-data exports."
        )
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> int:
    try:
        source, target, output = _resolve_paths(_parse_arguments())
        equivalent = compare_exports(source, target, output)
    except (ComparisonError, OSError) as exc:
        print(f"Site-data export comparison failed: {exc}", file=sys.stderr)
        return 1
    if equivalent:
        print(f"Equivalent site-data exports; evidence created: {output}")
        return 0
    print(f"Site-data exports are not equivalent; evidence created: {output}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
