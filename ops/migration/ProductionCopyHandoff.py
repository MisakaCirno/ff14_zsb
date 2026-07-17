from __future__ import annotations

"""Create and verify a fail-closed production-copy handoff manifest.

Public API:

* ``load_handoff(path)`` loads canonical UTF-8 JSON and validates schema v1.
* ``validate_handoff(value)`` validates and returns a handoff object.
* ``verify_live_handoff(handoff, repository_root, disallowed_roots=(),
  verify_content=True)`` rechecks the recorded content and Windows ACL snapshot.
* ``capture_access_baseline(...)`` and ``verify_access_baseline(...)`` expose the
  access-only checks used by the rehearsal pre/post gates.

The live operations are Windows-only, read-only, and deliberately conservative:
they never change an ACL and never create a probe in an external scope.
"""

import argparse
import ctypes
from ctypes import wintypes
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import importlib.util
import json
import ntpath
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import sys
from typing import Any, Iterable, Mapping, Sequence
import unicodedata
from uuid import uuid4


HANDOFF_FORMAT = "ffxivshare-production-copy-handoff"
HANDOFF_VERSION = 1
MAX_HANDOFF_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
HASH_CHUNK_SIZE = 1024 * 1024
ACCESS_VERIFICATION = "windows_acl_snapshot"
ACCESS_SCOPE_POLICY = "sealed_read_only_v1"
SCOPE_ROLES = (
    "database_backup_set",
    "source_media_root",
    "source_media_manifest",
    "target_media_root_1",
    "target_media_root_2",
)
PLACEHOLDER_APPLICATION_VERSIONS = frozenset({
    "unknown",
    "unset",
    "none",
    "null",
    "n/a",
    "na",
    "replace-me",
    "replace_with_deployed_release_id",
    "replace-with-deployed-release-id",
})

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SID_PATTERN = re.compile(r"^S-1-(?:\d+-)+\d+$")
SNAPSHOT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
UTC_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
VOLUME_SERIAL_PATTERN = re.compile(r"^[0-9a-f]{8}$")
FILE_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")

SOURCE_KEYS = {
    "host",
    "captured_at",
    "operator",
    "operator_identity_verification",
    "release_application_version",
    "database_media_consistency",
    "source_immutable",
    "target_media_offline",
}
ARTIFACT_KEYS = {"path", "sha256", "size"}
MEDIA_MANIFEST_KEYS = {
    "path",
    "sha256",
    "size",
    "generated_at",
    "snapshot_id",
    "file_count",
    "total_size",
}
ROOT_IDENTITY_KEYS = {
    "volume_serial_number",
    "file_id",
    "owner_sid",
    "dacl_protected",
    "dacl_sha256",
}
ACCESS_SCOPE_KEYS = {
    "role",
    "path",
    "root",
    "ancestor_chain",
    "ancestor_chain_sha256",
    "dacl_inventory",
    "node_inventory",
    "owner_inventory",
    "tree_sha256",
    "entry_count",
    "directory_count",
    "file_count",
    "total_size",
}
ANCESTOR_KEYS = {
    "path",
    "volume_serial_number",
    "file_id",
    "owner_sid",
    "dacl_protected",
    "dacl_sha256",
    "aces",
}
NODE_INVENTORY_KEYS = {
    "relative_path",
    "kind",
    "volume_serial_number",
    "file_id",
    "attributes",
    "last_write_time",
    "size",
    "link_count",
    "owner_sid",
    "dacl_protected",
    "dacl_sha256",
}
TOP_LEVEL_KEYS = {
    "format",
    "format_version",
    "generated_at",
    "source",
    "database_backup_set",
    "source_media",
    "rehearsal_targets",
    "access_baseline",
    "limitations",
}

OPERATOR_IDENTITY_ATTESTATION = "operator_asserted_not_cryptographically_verified"
DATABASE_MEDIA_ATTESTATION = (
    "operator_asserted_same_capture_window_not_cryptographically_verified"
)
OPERATOR_ASSERTED = "operator_asserted"

LIMITATIONS = {
    "tamper_proof": False,
    "continuous_acl_stability_proven": False,
    "offline_process_state": "operator_asserted",
    "trusted_operator_can_override_acl": True,
}

# Win32 constants. No mutation-oriented access is requested anywhere below.
GENERIC_READ = 0x80000000
GENERIC_ALL = 0x10000000
FILE_ALL_ACCESS = 0x001F01FF
FILE_READ_ATTRIBUTES = 0x00000080
READ_CONTROL = 0x00020000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
MOVEFILE_WRITE_THROUGH = 0x00000008
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
SE_FILE_OBJECT = 1
OWNER_SECURITY_INFORMATION = 0x00000001
DACL_SECURITY_INFORMATION = 0x00000004
SE_DACL_PROTECTED = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_USER_CLASS = 1
ACL_SIZE_INFORMATION_CLASS = 2
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400

SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
CREATOR_OWNER_SID = "S-1-3-0"
TRUSTED_INSTALLER_SID = (
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
)
SIMPLE_ALLOW_ACE_TYPE = 0
ALLOW_ACE_TYPES = frozenset({0, 5, 9, 11})
NON_GRANT_ACE_TYPES = frozenset({1, 2, 3, 6, 7, 8, 10, 12, 13, 14,
                                 15, 16, 17, 18, 19, 20, 21})
CURRENT_WRITE_ACCESS = (
    0x00000002  # FILE_WRITE_DATA / FILE_ADD_FILE
    | 0x00000004  # FILE_APPEND_DATA / FILE_ADD_SUBDIRECTORY
    | 0x00000010  # FILE_WRITE_EA
    | 0x00000040  # FILE_DELETE_CHILD
    | 0x00000100  # FILE_WRITE_ATTRIBUTES
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
    | GENERIC_ALL
    | 0x40000000  # GENERIC_WRITE
)
PATH_COMPONENT_CONTROL_ACCESS = (
    0x00000040 | 0x00010000 | 0x00040000 | 0x00080000 | GENERIC_ALL
)
CHILD_WRITE_ACCESS = CURRENT_WRITE_ACCESS
DENY_ACE_TYPES = frozenset({1, 6, 10, 12})


class HandoffError(RuntimeError):
    """A handoff or its live evidence failed a closed-world check."""


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("User", _SidAndAttributes)]


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    ]


class _AceHeader(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", wintypes.WORD),
    ]


class _FileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD)]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", _FileTime),
        ("ftLastAccessTime", _FileTime),
        ("ftLastWriteTime", _FileTime),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


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
        raise HandoffError("Handoff cannot be rendered as canonical JSON.") from exc
    return (rendered + "\n").encode("utf-8")


def _pretty_json_bytes(value: Any) -> bytes:
    """Canonical bytes used by the existing MediaManifest.py publisher."""

    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise HandoffError("Evidence cannot be rendered as canonical JSON.") from exc
    return (rendered + "\n").encode("utf-8")


def _canonical_sha256(value: Any) -> str:
    return sha256(_canonical_json_bytes(value)).hexdigest()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_number(value: str) -> None:
    raise ValueError(f"unsupported JSON number: {value}")


def _strict_json_bytes(payload: bytes, *, label: str, maximum_size: int) -> Any:
    if len(payload) > maximum_size:
        raise HandoffError(f"{label} is too large.")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise HandoffError(f"{label} is not strict UTF-8 JSON.") from exc
    return value


def _expect_exact_keys(value: Any, keys: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise HandoffError(f"{label} keys do not match handoff schema v1.")
    return value


def _expect_string(value: Any, *, label: str, maximum: int = 1024) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 0x20 for character in value)
    ):
        raise HandoffError(f"{label} must be a non-empty canonical string.")
    return value


def _expect_integer(value: Any, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise HandoffError(f"{label} must be a non-negative integer.")
    return value


def _expect_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise HandoffError(f"{label} must be a lowercase SHA-256 digest.")
    return value


def _expect_timestamp(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or UTC_TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise HandoffError(f"{label} must be an ISO UTC timestamp.")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise HandoffError(f"{label} must be an ISO UTC timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise HandoffError(f"{label} must be an ISO UTC timestamp.")
    return value


def _expect_snapshot_id(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or SNAPSHOT_ID_PATTERN.fullmatch(value) is None:
        raise HandoffError(f"{label} is not a canonical snapshot ID.")
    return value


def _expect_windows_path(
    value: Any,
    *,
    label: str,
    allow_volume_root: bool = False,
) -> str:
    if not isinstance(value, str) or len(value) > 32767 or not value:
        raise HandoffError(f"{label} must be a canonical absolute DOS path.")
    if value.startswith(("\\\\", "\\?\\", "\\.\\")) or "/" in value:
        raise HandoffError(f"{label} must be a canonical absolute DOS path.")
    path = PureWindowsPath(value)
    if (
        path.drive != path.drive.upper()
        or re.fullmatch(r"[A-Z]:", path.drive or "") is None
        or path.root != "\\"
        or ".." in path.parts
        or ntpath.normpath(value) != value
        or (not allow_volume_root and value == f"{path.drive}\\")
    ):
        raise HandoffError(f"{label} must be a canonical absolute DOS path.")
    without_drive = value[len(path.drive):]
    if ":" in without_drive:
        raise HandoffError(f"{label} must not use an alternate data stream.")
    return value


def _windows_path_key(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(value))


def _windows_paths_overlap(first: str, second: str) -> bool:
    first_key = _windows_path_key(first)
    second_key = _windows_path_key(second)
    try:
        common = ntpath.commonpath((first_key, second_key))
    except ValueError:
        return False
    return common in {first_key, second_key}


def _validate_artifact(value: Any, *, label: str) -> dict[str, Any]:
    artifact = _expect_exact_keys(value, ARTIFACT_KEYS, label=label)
    _expect_windows_path(artifact["path"], label=f"{label}.path")
    _expect_sha256(artifact["sha256"], label=f"{label}.sha256")
    _expect_integer(artifact["size"], label=f"{label}.size")
    return artifact


def _validate_archived_aces(value: Any, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise HandoffError(f"{label} must be an ACE list.")
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise HandoffError(f"{label}[{index}] must be an ACE object.")
        if set(raw) not in ({"type", "flags", "mask", "sid"}, {
            "type", "flags", "mask", "raw_sha256"
        }):
            raise HandoffError(f"{label}[{index}] has an invalid exact shape.")
        for name, maximum in (("type", 255), ("flags", 255), ("mask", 0xFFFFFFFF)):
            number = raw[name]
            if (
                not isinstance(number, int)
                or isinstance(number, bool)
                or number < 0
                or number > maximum
            ):
                raise HandoffError(f"{label}[{index}].{name} is invalid.")
        if "sid" in raw:
            if (
                not isinstance(raw["sid"], str)
                or SID_PATTERN.fullmatch(raw["sid"]) is None
            ):
                raise HandoffError(f"{label}[{index}].sid is invalid.")
        else:
            _expect_sha256(
                raw["raw_sha256"], label=f"{label}[{index}].raw_sha256"
            )
        rows.append(raw)
    return rows


def _validate_scope(value: Any, *, expected_role: str) -> dict[str, Any]:
    scope = _expect_exact_keys(value, ACCESS_SCOPE_KEYS, label=f"Access scope {expected_role}")
    if scope["role"] != expected_role:
        raise HandoffError("Access scopes are not in their canonical role order.")
    _expect_windows_path(scope["path"], label=f"{expected_role}.path")
    root = _expect_exact_keys(scope["root"], ROOT_IDENTITY_KEYS,
                              label=f"{expected_role}.root")
    if (not isinstance(root["volume_serial_number"], str)
            or VOLUME_SERIAL_PATTERN.fullmatch(root["volume_serial_number"]) is None):
        raise HandoffError(f"{expected_role}.root volume serial is invalid.")
    if (not isinstance(root["file_id"], str)
            or FILE_ID_PATTERN.fullmatch(root["file_id"]) is None):
        raise HandoffError(f"{expected_role}.root file ID is invalid.")
    if not isinstance(root["owner_sid"], str) or SID_PATTERN.fullmatch(root["owner_sid"]) is None:
        raise HandoffError(f"{expected_role}.root owner SID is invalid.")
    if root["dacl_protected"] is not True:
        raise HandoffError(f"{expected_role}.root DACL must be protected.")
    _expect_sha256(root["dacl_sha256"], label=f"{expected_role}.root.dacl_sha256")
    ancestors = scope["ancestor_chain"]
    if not isinstance(ancestors, list) or not ancestors:
        raise HandoffError(f"{expected_role}.ancestor_chain must not be empty.")
    for index, raw in enumerate(ancestors):
        ancestor = _expect_exact_keys(
            raw, ANCESTOR_KEYS, label=f"{expected_role}.ancestor_chain[{index}]"
        )
        _expect_windows_path(
            ancestor["path"],
            label=f"{expected_role}.ancestor_chain[{index}].path",
            allow_volume_root=True,
        )
        if (
            not isinstance(ancestor["volume_serial_number"], str)
            or VOLUME_SERIAL_PATTERN.fullmatch(ancestor["volume_serial_number"])
            is None
            or not isinstance(ancestor["file_id"], str)
            or FILE_ID_PATTERN.fullmatch(ancestor["file_id"]) is None
            or not isinstance(ancestor["owner_sid"], str)
            or SID_PATTERN.fullmatch(ancestor["owner_sid"]) is None
            or not isinstance(ancestor["dacl_protected"], bool)
        ):
            raise HandoffError(f"{expected_role} ancestor identity is invalid.")
        _expect_sha256(
            ancestor["dacl_sha256"],
            label=f"{expected_role}.ancestor_chain[{index}].dacl_sha256",
        )
        aces = _validate_archived_aces(
            ancestor["aces"], label=f"{expected_role}.ancestor_chain[{index}].aces"
        )
        if ancestor["dacl_sha256"] != _canonical_sha256(aces):
            raise HandoffError(f"{expected_role} ancestor DACL digest is invalid.")
    if scope["ancestor_chain_sha256"] != _canonical_sha256(ancestors):
        raise HandoffError(f"{expected_role} ancestor-chain digest is invalid.")
    _expect_sha256(scope["tree_sha256"], label=f"{expected_role}.tree_sha256")
    entry_count = _expect_integer(scope["entry_count"], label=f"{expected_role}.entry_count")
    directory_count = _expect_integer(
        scope["directory_count"], label=f"{expected_role}.directory_count"
    )
    file_count = _expect_integer(scope["file_count"], label=f"{expected_role}.file_count")
    total_size = _expect_integer(
        scope["total_size"], label=f"{expected_role}.total_size"
    )
    if entry_count != directory_count + file_count or entry_count < 1:
        raise HandoffError(f"{expected_role} access totals are inconsistent.")
    dacl_inventory = scope["dacl_inventory"]
    if not isinstance(dacl_inventory, list) or not dacl_inventory:
        raise HandoffError(f"{expected_role}.dacl_inventory must not be empty.")
    dacl_keys: list[str] = []
    dacl_aces: dict[str, list[dict[str, Any]]] = {}
    dacl_counts: dict[str, int] = {}
    dacl_nodes = 0
    for index, raw in enumerate(dacl_inventory):
        row = _expect_exact_keys(
            raw,
            {"dacl_sha256", "aces", "node_count"},
            label=f"{expected_role}.dacl_inventory[{index}]",
        )
        digest = _expect_sha256(
            row["dacl_sha256"],
            label=f"{expected_role}.dacl_inventory[{index}].dacl_sha256",
        )
        aces = _validate_archived_aces(
            row["aces"], label=f"{expected_role}.dacl_inventory[{index}].aces"
        )
        if digest != _canonical_sha256(aces):
            raise HandoffError(f"{expected_role} archived DACL digest is invalid.")
        node_count = _expect_integer(
            row["node_count"],
            label=f"{expected_role}.dacl_inventory[{index}].node_count",
        )
        if node_count < 1:
            raise HandoffError(f"{expected_role} archived DACL count is invalid.")
        dacl_keys.append(digest)
        dacl_aces[digest] = aces
        dacl_counts[digest] = node_count
        dacl_nodes += node_count
    if dacl_keys != sorted(set(dacl_keys)) or dacl_nodes != entry_count:
        raise HandoffError(f"{expected_role} DACL inventory is not canonical.")
    if root["dacl_sha256"] not in dacl_keys:
        raise HandoffError(f"{expected_role} root DACL is not archived.")
    owner_inventory = scope["owner_inventory"]
    if not isinstance(owner_inventory, list) or not owner_inventory:
        raise HandoffError(f"{expected_role}.owner_inventory must not be empty.")
    owner_keys: list[str] = []
    owner_counts: dict[str, int] = {}
    owner_nodes = 0
    for index, raw in enumerate(owner_inventory):
        row = _expect_exact_keys(
            raw,
            {"owner_sid", "node_count"},
            label=f"{expected_role}.owner_inventory[{index}]",
        )
        owner_sid = row["owner_sid"]
        if not isinstance(owner_sid, str) or SID_PATTERN.fullmatch(owner_sid) is None:
            raise HandoffError(f"{expected_role} archived owner SID is invalid.")
        node_count = _expect_integer(
            row["node_count"],
            label=f"{expected_role}.owner_inventory[{index}].node_count",
        )
        if node_count < 1:
            raise HandoffError(f"{expected_role} archived owner count is invalid.")
        owner_keys.append(owner_sid)
        owner_counts[owner_sid] = node_count
        owner_nodes += node_count
    if owner_keys != sorted(set(owner_keys)) or owner_nodes != entry_count:
        raise HandoffError(f"{expected_role} owner inventory is not canonical.")
    if root["owner_sid"] not in owner_keys:
        raise HandoffError(f"{expected_role} root owner is not archived.")

    nodes = scope["node_inventory"]
    if not isinstance(nodes, list) or len(nodes) != entry_count:
        raise HandoffError(f"{expected_role}.node_inventory has invalid closure.")
    reconstructed: list[dict[str, Any]] = []
    relative_paths: list[str] = []
    actual_dacl_counts: dict[str, int] = {}
    actual_owner_counts: dict[str, int] = {}
    actual_directory_count = 0
    actual_file_count = 0
    actual_total_size = 0
    for index, raw in enumerate(nodes):
        node = _expect_exact_keys(
            raw,
            NODE_INVENTORY_KEYS,
            label=f"{expected_role}.node_inventory[{index}]",
        )
        relative = node["relative_path"]
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or PurePosixPath(relative).is_absolute()
            or ".." in PurePosixPath(relative).parts
            or PurePosixPath(relative).as_posix() != relative
        ):
            raise HandoffError(f"{expected_role} archived relative path is invalid.")
        kind = node["kind"]
        if kind not in {"directory", "file"}:
            raise HandoffError(f"{expected_role} archived node kind is invalid.")
        if (
            not isinstance(node["volume_serial_number"], str)
            or VOLUME_SERIAL_PATTERN.fullmatch(node["volume_serial_number"]) is None
            or not isinstance(node["file_id"], str)
            or FILE_ID_PATTERN.fullmatch(node["file_id"]) is None
            or not isinstance(node["owner_sid"], str)
            or SID_PATTERN.fullmatch(node["owner_sid"]) is None
            or not isinstance(node["dacl_protected"], bool)
        ):
            raise HandoffError(f"{expected_role} archived node identity is invalid.")
        attributes = _expect_integer(
            node["attributes"],
            label=f"{expected_role}.node_inventory[{index}].attributes",
        )
        _expect_integer(
            node["last_write_time"],
            label=f"{expected_role}.node_inventory[{index}].last_write_time",
        )
        size = _expect_integer(
            node["size"], label=f"{expected_role}.node_inventory[{index}].size"
        )
        link_count = _expect_integer(
            node["link_count"],
            label=f"{expected_role}.node_inventory[{index}].link_count",
        )
        dacl_digest = _expect_sha256(
            node["dacl_sha256"],
            label=f"{expected_role}.node_inventory[{index}].dacl_sha256",
        )
        if (
            dacl_digest not in dacl_aces
            or node["owner_sid"] not in owner_counts
            or link_count < 1
            or (kind == "directory" and (not attributes & 0x10 or size != 0))
            or (kind == "file" and (attributes & 0x10 or link_count != 1))
        ):
            raise HandoffError(f"{expected_role} archived node semantics are invalid.")
        relative_paths.append(relative)
        actual_dacl_counts[dacl_digest] = actual_dacl_counts.get(dacl_digest, 0) + 1
        owner_sid = node["owner_sid"]
        actual_owner_counts[owner_sid] = actual_owner_counts.get(owner_sid, 0) + 1
        actual_directory_count += kind == "directory"
        actual_file_count += kind == "file"
        actual_total_size += size
        reconstructed.append({**node, "aces": dacl_aces[dacl_digest]})
    if (
        relative_paths[0] != "."
        or len(set(relative_paths)) != entry_count
        or actual_dacl_counts != dacl_counts
        or actual_owner_counts != owner_counts
        or actual_directory_count != directory_count
        or actual_file_count != file_count
        or actual_total_size != total_size
        or scope["tree_sha256"] != _canonical_sha256(reconstructed)
    ):
        raise HandoffError(f"{expected_role} archived node inventory is inconsistent.")
    first = nodes[0]
    if (
        first["volume_serial_number"] != root["volume_serial_number"]
        or first["file_id"] != root["file_id"]
        or first["owner_sid"] != root["owner_sid"]
        or first["dacl_protected"] != root["dacl_protected"]
        or first["dacl_sha256"] != root["dacl_sha256"]
    ):
        raise HandoffError(f"{expected_role} root identity is not archived exactly.")
    return scope


def validate_handoff(value: Any) -> dict[str, Any]:
    """Validate the exact handoff v1 shape and return ``value`` unchanged."""

    handoff = _expect_exact_keys(value, TOP_LEVEL_KEYS, label="Handoff")
    if handoff["format"] != HANDOFF_FORMAT or handoff["format_version"] != HANDOFF_VERSION:
        raise HandoffError("Unsupported production-copy handoff format.")
    _expect_timestamp(handoff["generated_at"], label="generated_at")

    source = _expect_exact_keys(handoff["source"], SOURCE_KEYS, label="source")
    _expect_string(source["host"], label="source.host", maximum=255)
    _expect_timestamp(source["captured_at"], label="source.captured_at")
    _expect_string(source["operator"], label="source.operator", maximum=255)
    if source["operator_identity_verification"] != OPERATOR_IDENTITY_ATTESTATION:
        raise HandoffError("source.operator_identity_verification is invalid.")
    application_version = _expect_string(
        source["release_application_version"],
        label="source.release_application_version",
        maximum=255,
    )
    if application_version.casefold() in PLACEHOLDER_APPLICATION_VERSIONS:
        raise HandoffError("source.release_application_version is a placeholder.")
    if source["database_media_consistency"] != DATABASE_MEDIA_ATTESTATION:
        raise HandoffError("source.database_media_consistency is invalid.")
    if source["source_immutable"] != OPERATOR_ASSERTED:
        raise HandoffError("source.source_immutable is invalid.")
    if source["target_media_offline"] != OPERATOR_ASSERTED:
        raise HandoffError("source.target_media_offline is invalid.")

    backup = _expect_exact_keys(
        handoff["database_backup_set"],
        {"database", "checksum", "metadata"},
        label="database_backup_set",
    )
    database = _validate_artifact(backup["database"], label="database_backup_set.database")
    checksum = _validate_artifact(backup["checksum"], label="database_backup_set.checksum")
    metadata = _validate_artifact(backup["metadata"], label="database_backup_set.metadata")
    database_path = database["path"]
    if checksum["path"] != f"{database_path}.sha256":
        raise HandoffError("Backup checksum sidecar path is not canonical.")
    if metadata["path"] != f"{database_path}.metadata.json":
        raise HandoffError("Backup metadata sidecar path is not canonical.")

    source_media = _expect_exact_keys(
        handoff["source_media"], {"root", "snapshot_id", "manifest"},
        label="source_media",
    )
    _expect_windows_path(source_media["root"], label="source_media.root")
    source_snapshot_id = _expect_snapshot_id(
        source_media["snapshot_id"], label="source_media.snapshot_id"
    )
    manifest = _expect_exact_keys(
        source_media["manifest"], MEDIA_MANIFEST_KEYS, label="source_media.manifest"
    )
    _expect_windows_path(manifest["path"], label="source_media.manifest.path")
    _expect_sha256(manifest["sha256"], label="source_media.manifest.sha256")
    _expect_integer(manifest["size"], label="source_media.manifest.size")
    _expect_timestamp(manifest["generated_at"], label="source_media.manifest.generated_at")
    if _expect_snapshot_id(manifest["snapshot_id"],
                           label="source_media.manifest.snapshot_id") != source_snapshot_id:
        raise HandoffError("Source media snapshot IDs do not match.")
    _expect_integer(manifest["file_count"], label="source_media.manifest.file_count")
    _expect_integer(manifest["total_size"], label="source_media.manifest.total_size")

    targets = handoff["rehearsal_targets"]
    if not isinstance(targets, list) or len(targets) != 2:
        raise HandoffError("rehearsal_targets must contain exactly two rows.")
    for row, slot in zip(targets, ("first", "second"), strict=True):
        target = _expect_exact_keys(
            row, {"slot", "path", "snapshot_id"}, label=f"rehearsal_targets.{slot}"
        )
        if target["slot"] != slot:
            raise HandoffError("Rehearsal target slots are not canonical.")
        _expect_windows_path(target["path"], label=f"rehearsal_targets.{slot}.path")
        _expect_snapshot_id(target["snapshot_id"],
                            label=f"rehearsal_targets.{slot}.snapshot_id")

    access = _expect_exact_keys(
        handoff["access_baseline"],
        {"verification", "scope_policy", "snapshot_sha256", "scopes"},
        label="access_baseline",
    )
    if (access["verification"] != ACCESS_VERIFICATION
            or access["scope_policy"] != ACCESS_SCOPE_POLICY):
        raise HandoffError("Unsupported handoff access policy.")
    _expect_sha256(access["snapshot_sha256"], label="access_baseline.snapshot_sha256")
    scopes = access["scopes"]
    if not isinstance(scopes, list) or len(scopes) != len(SCOPE_ROLES):
        raise HandoffError("access_baseline.scopes does not have exact closure.")
    scope_by_role = {
        role: _validate_scope(scope, expected_role=role)
        for role, scope in zip(SCOPE_ROLES, scopes, strict=True)
    }
    expected_access_digest = _canonical_sha256({
        "verification": access["verification"],
        "scope_policy": access["scope_policy"],
        "scopes": scopes,
    })
    if access["snapshot_sha256"] != expected_access_digest:
        raise HandoffError("access_baseline snapshot digest does not match its scopes.")

    if handoff["limitations"] != LIMITATIONS:
        raise HandoffError("Handoff limitations are not exact.")

    database_root = ntpath.dirname(database_path)
    expected_scope_paths = {
        "database_backup_set": database_root,
        "source_media_root": source_media["root"],
        "source_media_manifest": manifest["path"],
        "target_media_root_1": targets[0]["path"],
        "target_media_root_2": targets[1]["path"],
    }
    for role, expected_path in expected_scope_paths.items():
        scope = scope_by_role[role]
        if scope["path"] != expected_path:
            raise HandoffError(f"{role} access path does not match its artifact.")

    if scope_by_role["database_backup_set"]["entry_count"] != 4:
        raise HandoffError("Database access scope must contain parent plus three files.")
    if (scope_by_role["database_backup_set"]["directory_count"] != 1
            or scope_by_role["database_backup_set"]["file_count"] != 3):
        raise HandoffError("Database access scope totals are invalid.")
    if scope_by_role["database_backup_set"]["total_size"] != (
        database["size"] + checksum["size"] + metadata["size"]
    ):
        raise HandoffError("Database access scope byte total is invalid.")
    if (scope_by_role["source_media_manifest"]["entry_count"] != 1
            or scope_by_role["source_media_manifest"]["directory_count"] != 0
            or scope_by_role["source_media_manifest"]["file_count"] != 1
            or scope_by_role["source_media_manifest"]["total_size"] != manifest["size"]):
        raise HandoffError("Manifest access scope totals are invalid.")
    for role in ("source_media_root", "target_media_root_1", "target_media_root_2"):
        scope = scope_by_role[role]
        if (scope["file_count"] != manifest["file_count"]
                or scope["total_size"] != manifest["total_size"]
                or scope["directory_count"] < 1):
            raise HandoffError(f"{role} access totals do not match the media manifest.")

    external_roots = [
        database_root,
        source_media["root"],
        manifest["path"],
        targets[0]["path"],
        targets[1]["path"],
    ]
    for index, first in enumerate(external_roots):
        for second in external_roots[index + 1:]:
            if _windows_paths_overlap(first, second):
                raise HandoffError("External handoff scopes must not overlap.")
    return handoff


def _is_reparse_or_symlink(path: Path) -> bool:
    metadata = os.lstat(path)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    )


def _assert_no_link_components(path: Path) -> None:
    candidate = Path(os.path.abspath(path))
    current = Path(candidate.anchor)
    parts = candidate.parts[1:] if candidate.anchor else candidate.parts
    for part in parts:
        current /= part
        try:
            if _is_reparse_or_symlink(current):
                raise HandoffError(
                    "Handoff path must not traverse a symlink or reparse point."
                )
        except FileNotFoundError as exc:
            raise HandoffError("Handoff path component does not exist.") from exc
        except HandoffError:
            raise
        except OSError as exc:
            raise HandoffError("Handoff path component cannot be inspected.") from exc


def _load_handoff_checkpoint(
    path: os.PathLike[str] | str,
) -> tuple[
    dict[str, Any],
    Path,
    int,
    str,
    tuple[int, int, int, int, int, int],
]:
    candidate = Path(os.path.abspath(os.fspath(path)))
    _assert_no_link_components(candidate)
    payload, size, digest, identity = _read_stable_file(
        os.fspath(candidate),
        maximum_size=MAX_HANDOFF_BYTES,
        label="Handoff file",
    )
    _assert_no_link_components(candidate)
    value = _strict_json_bytes(payload, label="Handoff", maximum_size=MAX_HANDOFF_BYTES)
    handoff = validate_handoff(value)
    if payload != _canonical_json_bytes(handoff):
        raise HandoffError("Handoff file is not canonical UTF-8/LF JSON.")
    return handoff, candidate, size, digest, identity


def _checkpoint_handoff_file(
    candidate: Path,
    *,
    expected_size: int,
    expected_digest: str,
    expected_identity: tuple[int, int, int, int, int, int],
) -> None:
    _assert_no_link_components(candidate)
    _payload, size, digest, identity = _read_stable_file(
        os.fspath(candidate),
        maximum_size=MAX_HANDOFF_BYTES,
        label="Handoff file",
    )
    _assert_no_link_components(candidate)
    if (
        size != expected_size
        or digest != expected_digest
        or identity != expected_identity
    ):
        raise HandoffError("Handoff file changed during live verification.")


def load_handoff(path: os.PathLike[str] | str) -> dict[str, Any]:
    """Load a canonical handoff file without performing any live checks."""

    handoff, _candidate, _size, _digest, _identity = _load_handoff_checkpoint(path)
    return handoff


def _require_windows_live_check() -> None:
    if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
        raise HandoffError(
            "Production-copy handoff live checks require Windows security APIs."
        )


class _Win32Api:
    def __init__(self) -> None:
        try:
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        except (AttributeError, OSError) as exc:
            raise HandoffError("Required Windows security APIs are unavailable.") from exc

        kernel32 = self.kernel32
        advapi32 = self.advapi32
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        kernel32.GetFinalPathNameByHandleW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetDriveTypeW.restype = wintypes.UINT
        kernel32.GetVolumeInformationW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        kernel32.GetVolumeInformationW.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
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
        advapi32.GetSecurityInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.c_uint,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi32.GetSecurityInfo.restype = wintypes.DWORD
        advapi32.GetSecurityDescriptorControl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
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
        self.current_user_sid = self._read_current_user_sid()

    def _error(self, message: str) -> HandoffError:
        return HandoffError(f"{message} (Win32 {ctypes.get_last_error()}).")

    def close_handle(self, handle: int) -> None:
        if handle not in (None, 0, INVALID_HANDLE_VALUE):
            self.kernel32.CloseHandle(handle)

    def sid_to_string(self, sid_pointer: int) -> str:
        output = wintypes.LPWSTR()
        if not self.advapi32.ConvertSidToStringSidW(
            ctypes.c_void_p(sid_pointer), ctypes.byref(output)
        ):
            raise self._error("Windows SID conversion failed")
        try:
            value = output.value
            if not value or SID_PATTERN.fullmatch(value) is None:
                raise HandoffError("Windows returned a malformed SID.")
            return value
        finally:
            self.kernel32.LocalFree(output)

    def _read_current_user_sid(self) -> str:
        token = wintypes.HANDLE()
        if not self.advapi32.OpenProcessToken(
            self.kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
        ):
            raise self._error("Current Windows token cannot be opened")
        try:
            required = wintypes.DWORD()
            self.advapi32.GetTokenInformation(
                token, TOKEN_USER_CLASS, None, 0, ctypes.byref(required)
            )
            if not required.value:
                raise self._error("Current Windows user SID cannot be sized")
            buffer = ctypes.create_string_buffer(required.value)
            if not self.advapi32.GetTokenInformation(
                token,
                TOKEN_USER_CLASS,
                buffer,
                required.value,
                ctypes.byref(required),
            ):
                raise self._error("Current Windows user SID cannot be read")
            token_user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
            return self.sid_to_string(token_user.User.Sid)
        finally:
            self.close_handle(token)

    def open_path(self, path: str) -> int:
        handle = self.kernel32.CreateFileW(
            path,
            FILE_READ_ATTRIBUTES | READ_CONTROL,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            raise self._error(f"Path cannot be opened read-only: {path}")
        return handle

    def final_path(self, handle: int) -> str:
        capacity = 32768
        buffer = ctypes.create_unicode_buffer(capacity)
        length = self.kernel32.GetFinalPathNameByHandleW(handle, buffer, capacity, 0)
        if not length or length >= capacity:
            raise self._error("Final DOS path cannot be read")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            raise HandoffError("UNC or mapped-drive paths are not permitted.")
        if value.startswith("\\\\?\\"):
            value = value[4:]
        elif value.startswith("\\\\"):
            raise HandoffError("UNC or mapped-drive paths are not permitted.")
        if re.match(r"^[A-Za-z]:\\", value) is None:
            raise HandoffError("Windows did not return a canonical DOS path.")
        return value[0].upper() + value[1:]

    def file_information(self, handle: int) -> _ByHandleFileInformation:
        information = _ByHandleFileInformation()
        if not self.kernel32.GetFileInformationByHandle(
            handle, ctypes.byref(information)
        ):
            raise self._error("File identity cannot be read")
        return information

    def validate_volume(self, canonical_path: str) -> None:
        root = canonical_path[:3]
        drive_type = self.kernel32.GetDriveTypeW(root)
        if drive_type not in {DRIVE_REMOVABLE, DRIVE_FIXED}:
            raise HandoffError("Handoff scopes require a local fixed/removable drive.")
        filesystem = ctypes.create_unicode_buffer(64)
        serial = wintypes.DWORD()
        maximum_component = wintypes.DWORD()
        flags = wintypes.DWORD()
        if not self.kernel32.GetVolumeInformationW(
            root,
            None,
            0,
            ctypes.byref(serial),
            ctypes.byref(maximum_component),
            ctypes.byref(flags),
            filesystem,
            len(filesystem),
        ):
            raise self._error("Volume information cannot be read")
        if filesystem.value.casefold() != "ntfs":
            raise HandoffError("Handoff scopes require NTFS volumes.")

    def security(self, handle: int) -> dict[str, Any]:
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        result = self.advapi32.GetSecurityInfo(
            handle,
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if result != 0 or not descriptor.value or not owner.value or not dacl.value:
            if descriptor.value:
                self.kernel32.LocalFree(descriptor)
            raise HandoffError(
                f"Windows owner/DACL cannot be read (Win32 {result})."
            )
        try:
            control = wintypes.WORD()
            revision = wintypes.DWORD()
            if not self.advapi32.GetSecurityDescriptorControl(
                descriptor, ctypes.byref(control), ctypes.byref(revision)
            ):
                raise self._error("Windows security descriptor control cannot be read")
            acl_info = _AclSizeInformation()
            if not self.advapi32.GetAclInformation(
                dacl,
                ctypes.byref(acl_info),
                ctypes.sizeof(acl_info),
                ACL_SIZE_INFORMATION_CLASS,
            ):
                raise self._error("Windows DACL cannot be enumerated")
            aces: list[dict[str, Any]] = []
            for index in range(acl_info.AceCount):
                ace_pointer = ctypes.c_void_p()
                if not self.advapi32.GetAce(dacl, index, ctypes.byref(ace_pointer)):
                    raise self._error("Windows DACL ACE cannot be read")
                header = ctypes.cast(
                    ace_pointer, ctypes.POINTER(_AceHeader)
                ).contents
                if header.AceSize < 8 or header.AceSize > 65535:
                    raise HandoffError("Windows DACL contains a malformed ACE.")
                raw = ctypes.string_at(ace_pointer.value, header.AceSize)
                mask = int.from_bytes(raw[4:8], "little")
                ace: dict[str, Any] = {
                    "type": int(header.AceType),
                    "flags": int(header.AceFlags),
                    "mask": mask,
                }
                if header.AceType in {SIMPLE_ALLOW_ACE_TYPE, 1, 2, 3}:
                    ace["sid"] = self.sid_to_string(ace_pointer.value + 8)
                else:
                    ace["raw_sha256"] = sha256(raw).hexdigest()
                aces.append(ace)
            return {
                "owner_sid": self.sid_to_string(owner.value),
                "dacl_protected": bool(control.value & SE_DACL_PROTECTED),
                "dacl_sha256": _canonical_sha256(aces),
                "aces": aces,
            }
        finally:
            self.kernel32.LocalFree(descriptor)


def _runtime_input_shape(raw_path: os.PathLike[str] | str, *, label: str) -> str:
    value = os.fspath(raw_path)
    if not isinstance(value, str) or not value:
        raise HandoffError(f"{label} must be an absolute local Windows path.")
    if value.startswith(("\\\\", "\\?\\", "\\.\\")):
        raise HandoffError(f"{label} must not be UNC or device syntax.")
    candidate = Path(value)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise HandoffError(f"{label} must be an absolute path without traversal.")
    drive, tail = ntpath.splitdrive(value)
    if re.fullmatch(r"[A-Za-z]:", drive or "") is None or ":" in tail:
        raise HandoffError(f"{label} must not use a mapped/UNC/ADS path.")
    normalized = ntpath.normpath(os.path.abspath(value))
    if normalized == f"{drive}\\":
        raise HandoffError(f"{label} must not be a volume root.")
    return normalized[0].upper() + normalized[1:]


def _open_inspected_path(api: _Win32Api, path: str) -> tuple[int, str, _ByHandleFileInformation]:
    handle = api.open_path(path)
    try:
        final = api.final_path(handle)
        api.validate_volume(final)
        information = api.file_information(handle)
        if information.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise HandoffError("Handoff paths must not use reparse points.")
        return handle, final, information
    except Exception:
        api.close_handle(handle)
        raise


def _resolve_existing_path(
    api: _Win32Api,
    raw_path: os.PathLike[str] | str,
    *,
    label: str,
    require_directory: bool | None,
) -> str:
    candidate = _runtime_input_shape(raw_path, label=label)
    drive_root = candidate[:3]
    relative_parts = PureWindowsPath(candidate).parts[1:]
    current = drive_root.rstrip("\\")
    final_value = ""
    final_information: _ByHandleFileInformation | None = None
    for part in relative_parts:
        current = current + "\\" + part
        handle, final_value, final_information = _open_inspected_path(api, current)
        api.close_handle(handle)
        if _windows_path_key(final_value) != _windows_path_key(current):
            raise HandoffError(f"{label} traverses an alias, mapped drive, or reparse path.")
    if final_information is None:
        raise HandoffError(f"{label} must not be a volume root.")
    is_directory = bool(final_information.dwFileAttributes & 0x10)
    if require_directory is True and not is_directory:
        raise HandoffError(f"{label} must be an existing directory.")
    if require_directory is False and is_directory:
        raise HandoffError(f"{label} must be an existing regular file.")
    if not is_directory and final_information.nNumberOfLinks != 1:
        raise HandoffError(f"{label} must not be a hard-linked file.")
    return final_value


def _filetime_value(value: _FileTime) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _node_snapshot(
    api: _Win32Api,
    path: str,
    *,
    relative: str,
    require_directory: bool | None = None,
) -> dict[str, Any]:
    handle, final, information = _open_inspected_path(api, path)
    try:
        if _windows_path_key(final) != _windows_path_key(path):
            raise HandoffError("A handoff scope changed path identity.")
        is_directory = bool(information.dwFileAttributes & 0x10)
        if require_directory is True and not is_directory:
            raise HandoffError("A required scope directory became a file.")
        if require_directory is False and is_directory:
            raise HandoffError("A required scope file became a directory.")
        if not is_directory and information.nNumberOfLinks != 1:
            raise HandoffError("Handoff scope contains a hard-linked file.")
        security = api.security(handle)
        trusted_owners = {api.current_user_sid, SYSTEM_SID, ADMINISTRATORS_SID}
        if security["owner_sid"] not in trusted_owners:
            raise HandoffError(f"Handoff scope node has untrusted owner: {final}")
        for ace in security["aces"]:
            ace_type = ace["type"]
            if ace_type in ALLOW_ACE_TYPES:
                if ace_type != SIMPLE_ALLOW_ACE_TYPE or "sid" not in ace:
                    raise HandoffError(
                        f"Handoff scope has an uninterpretable grant ACE: {final}"
                    )
                sid = ace["sid"]
                if sid not in trusted_owners:
                    raise HandoffError(
                        f"Handoff scope grants access to untrusted SID {sid}: {final}"
                    )
                if sid == api.current_user_sid and ace["mask"] & CURRENT_WRITE_ACCESS:
                    raise HandoffError(
                        f"Current user retains write/delete/ACL rights: {final}"
                    )
            elif ace_type not in NON_GRANT_ACE_TYPES:
                raise HandoffError(f"Handoff scope has an unknown ACE type: {final}")
        size = 0 if is_directory else (
            (int(information.nFileSizeHigh) << 32) | int(information.nFileSizeLow)
        )
        return {
            "relative_path": relative,
            "kind": "directory" if is_directory else "file",
            "volume_serial_number": f"{int(information.dwVolumeSerialNumber):08x}",
            "file_id": (
                f"{((int(information.nFileIndexHigh) << 32) | int(information.nFileIndexLow)):016x}"
            ),
            "attributes": int(information.dwFileAttributes),
            "last_write_time": _filetime_value(information.ftLastWriteTime),
            "size": size,
            "link_count": int(information.nNumberOfLinks),
            "owner_sid": security["owner_sid"],
            "dacl_protected": security["dacl_protected"],
            "dacl_sha256": security["dacl_sha256"],
            "aces": security["aces"],
        }
    finally:
        api.close_handle(handle)


def _verify_ancestor(
    api: _Win32Api,
    path: str,
    *,
    direct_parent: bool,
) -> dict[str, Any]:
    handle, final, information = _open_inspected_path(api, path)
    try:
        if not information.dwFileAttributes & 0x10:
            raise HandoffError("Scope ancestor is not a directory.")
        security = api.security(handle)
        trusted_owners = {
            api.current_user_sid,
            SYSTEM_SID,
            ADMINISTRATORS_SID,
            TRUSTED_INSTALLER_SID,
        }
        if security["owner_sid"] not in trusted_owners:
            raise HandoffError(f"Scope ancestor has untrusted owner: {final}")
        for ace in security["aces"]:
            flags = ace["flags"]
            effective = not flags & 0x08  # INHERIT_ONLY_ACE
            inherited_by_child = bool(flags & 0x03)
            relevant = 0
            if effective:
                relevant |= ace["mask"] & PATH_COMPONENT_CONTROL_ACCESS
                if direct_parent:
                    relevant |= ace["mask"] & CHILD_WRITE_ACCESS
            if direct_parent and inherited_by_child:
                relevant |= ace["mask"] & CHILD_WRITE_ACCESS
            if not relevant:
                continue
            if ace["type"] in ALLOW_ACE_TYPES:
                if ace["type"] != SIMPLE_ALLOW_ACE_TYPE or "sid" not in ace:
                    raise HandoffError(
                        f"Scope ancestor has an uninterpretable grant ACE: {final}"
                    )
                trusted_grants = set(trusted_owners)
                if flags & 0x08:
                    trusted_grants.add(CREATOR_OWNER_SID)
                if ace["sid"] not in trusted_grants:
                    raise HandoffError(
                        f"Scope ancestor grants mutation rights to {ace['sid']}: {final}"
                    )
            elif ace["type"] not in NON_GRANT_ACE_TYPES:
                raise HandoffError(f"Scope ancestor has an unknown ACE type: {final}")
        return {
            "path": final,
            "volume_serial_number": f"{int(information.dwVolumeSerialNumber):08x}",
            "file_id": (
                f"{((int(information.nFileIndexHigh) << 32) | int(information.nFileIndexLow)):016x}"
            ),
            "owner_sid": security["owner_sid"],
            "dacl_protected": security["dacl_protected"],
            "dacl_sha256": security["dacl_sha256"],
            "aces": security["aces"],
        }
    finally:
        api.close_handle(handle)


def _ancestor_projection(api: _Win32Api, root: str) -> list[dict[str, Any]]:
    current = ntpath.dirname(root)
    rows: list[dict[str, Any]] = []
    direct_parent = True
    while current:
        rows.append(_verify_ancestor(api, current, direct_parent=direct_parent))
        if current == current[:3]:
            break
        parent = ntpath.dirname(current)
        if parent == current:
            break
        current = parent
        direct_parent = False
    if not rows or rows[-1]["path"] != root[:3]:
        raise HandoffError("Scope ancestor chain is incomplete.")
    return rows


def _iter_media_paths(root: str) -> list[tuple[str, str, bool]]:
    rows: list[tuple[str, str, bool]] = [(root, ".", True)]
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = sorted(
                os.scandir(current),
                key=lambda entry: (entry.name.casefold(), entry.name),
                reverse=True,
            )
        except OSError as exc:
            raise HandoffError("Media scope cannot be enumerated safely.") from exc
        for entry in entries:
            path = ntpath.normpath(entry.path)
            relative = ntpath.relpath(path, root).replace("\\", "/")
            if entry.is_symlink():
                raise HandoffError("Media scope contains a symlink/reparse point.")
            if entry.is_dir(follow_symlinks=False):
                rows.append((path, relative, True))
                pending.append(path)
            elif entry.is_file(follow_symlinks=False):
                rows.append((path, relative, False))
            else:
                raise HandoffError("Media scope contains a non-regular entry.")
    return sorted(rows, key=lambda row: (row[1].casefold(), row[1]))


def _scope_projection_once(
    api: _Win32Api,
    *,
    role: str,
    scope_path: str,
    database_files: Sequence[str] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if role == "database_backup_set":
        try:
            actual_names = {
                entry.name for entry in os.scandir(scope_path)
            }
        except OSError as exc:
            raise HandoffError("Database backup-set directory cannot be enumerated.") from exc
        expected_names = {ntpath.basename(path) for path in database_files}
        if actual_names != expected_names:
            raise HandoffError(
                "Database backup-set directory must contain exactly the three evidence files."
            )
        paths = [(scope_path, ".", True)] + [
            (path, ntpath.basename(path), False) for path in database_files
        ]
    elif role == "source_media_manifest":
        paths = [(scope_path, ".", False)]
    else:
        paths = _iter_media_paths(scope_path)
    nodes = [
        _node_snapshot(
            api,
            path,
            relative=relative,
            require_directory=is_directory,
        )
        for path, relative, is_directory in paths
    ]
    ancestors = _ancestor_projection(api, scope_path)
    return nodes, ancestors


def _capture_scope(
    api: _Win32Api,
    *,
    role: str,
    scope_path: str,
    database_files: Sequence[str] = (),
) -> dict[str, Any]:
    first_nodes, first_ancestors = _scope_projection_once(
        api, role=role, scope_path=scope_path, database_files=database_files
    )
    second_nodes, second_ancestors = _scope_projection_once(
        api, role=role, scope_path=scope_path, database_files=database_files
    )
    if first_nodes != second_nodes or first_ancestors != second_ancestors:
        raise HandoffError("A handoff ACL/tree scope changed during its stable scan.")
    root_node = first_nodes[0]
    if not root_node["dacl_protected"]:
        raise HandoffError(f"Scope root DACL is not protected: {scope_path}")
    directory_count = sum(row["kind"] == "directory" for row in first_nodes)
    file_count = sum(row["kind"] == "file" for row in first_nodes)
    total_size = sum(int(row["size"]) for row in first_nodes)
    dacl_rows: dict[str, dict[str, Any]] = {}
    owner_counts: dict[str, int] = {}
    for node in first_nodes:
        dacl_digest = node["dacl_sha256"]
        existing = dacl_rows.get(dacl_digest)
        if existing is None:
            dacl_rows[dacl_digest] = {
                "dacl_sha256": dacl_digest,
                "aces": node["aces"],
                "node_count": 1,
            }
        else:
            if existing["aces"] != node["aces"]:
                raise HandoffError("A DACL digest collision was observed.")
            existing["node_count"] += 1
        owner_counts[node["owner_sid"]] = owner_counts.get(node["owner_sid"], 0) + 1
    return {
        "role": role,
        "path": scope_path,
        "root": {
            "volume_serial_number": root_node["volume_serial_number"],
            "file_id": root_node["file_id"],
            "owner_sid": root_node["owner_sid"],
            "dacl_protected": root_node["dacl_protected"],
            "dacl_sha256": root_node["dacl_sha256"],
        },
        "ancestor_chain": first_ancestors,
        "ancestor_chain_sha256": _canonical_sha256(first_ancestors),
        "dacl_inventory": [dacl_rows[key] for key in sorted(dacl_rows)],
        "node_inventory": [
            {key: value for key, value in node.items() if key != "aces"}
            for node in first_nodes
        ],
        "owner_inventory": [
            {"owner_sid": key, "node_count": owner_counts[key]}
            for key in sorted(owner_counts)
        ],
        "tree_sha256": _canonical_sha256(first_nodes),
        "entry_count": len(first_nodes),
        "directory_count": directory_count,
        "file_count": file_count,
        "total_size": total_size,
    }


def _build_access_baseline(scopes: list[dict[str, Any]]) -> dict[str, Any]:
    projection = {
        "verification": ACCESS_VERIFICATION,
        "scope_policy": ACCESS_SCOPE_POLICY,
        "scopes": scopes,
    }
    return {**projection, "snapshot_sha256": _canonical_sha256(projection)}


def _grants_full_file_control(mask: int) -> bool:
    return bool(mask & GENERIC_ALL) or (mask & FILE_ALL_ACCESS) == FILE_ALL_ACCESS


def _validate_output_parent(api: _Win32Api, output: str) -> str:
    if os.path.lexists(output):
        raise HandoffError("Handoff output already exists; refusing to overwrite it.")
    parent = _resolve_existing_path(
        api, ntpath.dirname(output), label="Output parent", require_directory=True
    )
    handle, final, _information = _open_inspected_path(api, parent)
    try:
        security = api.security(handle)
    finally:
        api.close_handle(handle)
    if not security["dacl_protected"]:
        raise HandoffError("Output parent must have a protected private DACL.")
    trustee_labels = (
        (api.current_user_sid, "Current user"),
        (SYSTEM_SID, "SYSTEM"),
        (ADMINISTRATORS_SID, "Administrators"),
    )
    trusted = {sid for sid, _label in trustee_labels}
    if security["owner_sid"] not in trusted:
        raise HandoffError("Output parent has an untrusted owner.")
    parent_masks = {sid: 0 for sid in trusted}
    leaf_masks = {sid: 0 for sid in trusted}
    for ace in security["aces"]:
        if ace["type"] in ALLOW_ACE_TYPES:
            if ace["type"] != SIMPLE_ALLOW_ACE_TYPE or "sid" not in ace:
                raise HandoffError("Output parent has an uninterpretable grant ACE.")
            if ace["sid"] not in trusted:
                raise HandoffError("Output parent grants an untrusted SID.")
            if not ace["flags"] & 0x08:  # INHERIT_ONLY_ACE
                parent_masks[ace["sid"]] |= ace["mask"]
            if ace["flags"] & 0x01:  # OBJECT_INHERIT_ACE
                leaf_masks[ace["sid"]] |= ace["mask"]
        elif ace["type"] in DENY_ACE_TYPES:
            if not ace["flags"] & 0x08 or ace["flags"] & 0x01:
                raise HandoffError(
                    "Output parent has a deny ACE, so rollback access cannot be proven."
                )
        elif ace["type"] not in NON_GRANT_ACE_TYPES:
            raise HandoffError("Output parent has an unknown ACE type.")
    for sid, label in trustee_labels:
        if not _grants_full_file_control(parent_masks[sid]):
            raise HandoffError(
                f"{label} lacks full control of the output parent required for rollback."
            )
        if not _grants_full_file_control(leaf_masks[sid]):
            raise HandoffError(
                f"Output parent does not propagate {label} full control to output files."
            )
    _ancestor_projection(api, parent)
    return ntpath.join(final, ntpath.basename(output))


def _validate_published_output(api: _Win32Api, path: str) -> None:
    handle, final, information = _open_inspected_path(api, path)
    try:
        if (
            _windows_path_key(final) != _windows_path_key(path)
            or information.dwFileAttributes & 0x10
            or information.nNumberOfLinks != 1
        ):
            raise HandoffError("Published handoff path identity is invalid.")
        security = api.security(handle)
    finally:
        api.close_handle(handle)
    trusted = {api.current_user_sid, SYSTEM_SID, ADMINISTRATORS_SID}
    if security["owner_sid"] not in trusted:
        raise HandoffError("Published handoff has an untrusted owner.")
    for ace in security["aces"]:
        if ace["type"] in ALLOW_ACE_TYPES:
            if (
                ace["type"] != SIMPLE_ALLOW_ACE_TYPE
                or "sid" not in ace
                or ace["sid"] not in trusted
            ):
                raise HandoffError("Published handoff DACL grants an untrusted subject.")
        elif ace["type"] not in NON_GRANT_ACE_TYPES:
            raise HandoffError("Published handoff DACL has an unknown ACE type.")


_MEDIA_MODULE: Any | None = None
_BACKUP_MODULE: Any | None = None


def _load_sibling_module(module_name: str, filename: str) -> Any:
    path = Path(__file__).resolve().parent / filename
    try:
        specification = importlib.util.spec_from_file_location(module_name, path)
        if specification is None or specification.loader is None:
            raise ImportError("module specification has no loader")
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        return module
    except Exception as exc:
        raise HandoffError(f"Required verifier module cannot be loaded: {filename}") from exc


def _media_module() -> Any:
    global _MEDIA_MODULE
    if _MEDIA_MODULE is None:
        _MEDIA_MODULE = _load_sibling_module(
            "ffxivshare_production_copy_media_manifest", "MediaManifest.py"
        )
    return _MEDIA_MODULE


def _backup_module() -> Any:
    global _BACKUP_MODULE
    if _BACKUP_MODULE is None:
        _BACKUP_MODULE = _load_sibling_module(
            "ffxivshare_production_copy_backup_verifier", "Verify-SQLiteBackupSet.py"
        )
    return _BACKUP_MODULE


def _stat_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def _read_stable_file(
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
                raise HandoffError(f"{label} is not a unique regular file.")
            if before.st_size > maximum_size:
                raise HandoffError(f"{label} is too large.")
            while True:
                chunk = stream.read(HASH_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum_size:
                    raise HandoffError(f"{label} is too large.")
                digest.update(chunk)
                chunks.append(chunk)
            after = os.fstat(stream.fileno())
        current = os.stat(path)
    except HandoffError:
        raise
    except OSError as exc:
        raise HandoffError(f"{label} could not be read stably.") from exc
    identity = _stat_identity(before)
    if (
        identity != _stat_identity(after)
        or identity != _stat_identity(current)
        or total != before.st_size
    ):
        raise HandoffError(f"{label} changed while it was read.")
    return b"".join(chunks), total, digest.hexdigest(), identity


def _verify_backup_content(
    database: str,
    checksum: str,
    metadata_path: str,
) -> dict[str, Any]:
    verifier = _backup_module()
    try:
        verifier._assert_no_live_sidecars(Path(database))
        digest, size, database_identity = verifier._hash_stable_database(Path(database))
        checksum_payload, checksum_identity = verifier._read_stable_bytes(
            Path(checksum), maximum_size=verifier.MAX_CHECKSUM_BYTES
        )
        metadata_payload, metadata_identity = verifier._read_stable_bytes(
            Path(metadata_path), maximum_size=verifier.MAX_METADATA_BYTES
        )
        expected_checksum = f"{digest}  {ntpath.basename(database)}\n".encode("utf-8")
        if checksum_payload != expected_checksum:
            raise HandoffError("Backup checksum bytes do not match the database.")
        metadata = verifier._read_metadata(metadata_payload)
        verifier._validate_metadata(metadata)
        if metadata["sha256"] != digest or metadata["size"] != size:
            raise HandoffError("Backup metadata does not match the database bytes.")
        application_version = metadata["application_version"]
        if (
            not isinstance(application_version, str)
            or not application_version
            or application_version != application_version.strip()
            or len(application_version) > 255
            or application_version.casefold() in PLACEHOLDER_APPLICATION_VERSIONS
        ):
            raise HandoffError("Backup metadata application_version is a placeholder.")
        verifier._assert_no_live_sidecars(Path(database))
        second_digest, second_size, second_database_identity = (
            verifier._hash_stable_database(Path(database))
        )
        second_checksum_payload, second_checksum_identity = verifier._read_stable_bytes(
            Path(checksum), maximum_size=verifier.MAX_CHECKSUM_BYTES
        )
        second_metadata_payload, second_metadata_identity = verifier._read_stable_bytes(
            Path(metadata_path), maximum_size=verifier.MAX_METADATA_BYTES
        )
        second_metadata = verifier._read_metadata(second_metadata_payload)
        verifier._validate_metadata(second_metadata)
        if (
            second_digest != digest
            or second_size != size
            or second_checksum_payload != checksum_payload
            or second_metadata_payload != metadata_payload
            or second_database_identity != database_identity
            or second_checksum_identity != checksum_identity
            or second_metadata_identity != metadata_identity
        ):
            raise HandoffError("Backup set changed during handoff verification.")
        verifier._assert_no_live_sidecars(Path(database))
        verifier._assert_inputs_unchanged(
            (Path(database), Path(checksum), Path(metadata_path)),
            (second_database_identity, second_checksum_identity, second_metadata_identity),
        )
    except HandoffError:
        raise
    except Exception as exc:
        raise HandoffError("SQLite backup-set verification failed closed.") from exc
    return {
        "database": {"path": database, "sha256": digest, "size": size},
        "checksum": {
            "path": checksum,
            "sha256": sha256(checksum_payload).hexdigest(),
            "size": len(checksum_payload),
        },
        "metadata": {
            "path": metadata_path,
            "sha256": sha256(metadata_payload).hexdigest(),
            "size": len(metadata_payload),
        },
        "captured_at": metadata["generated_at"],
        "application_version": application_version,
    }


def _media_path_key(value: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFD", value).casefold())


def _validate_media_manifest_value(value: Any) -> dict[str, Any]:
    manifest = _expect_exact_keys(
        value,
        {
            "format",
            "format_version",
            "generated_at",
            "hash_algorithm",
            "path_normalization",
            "source_snapshot",
            "file_count",
            "total_size",
            "files",
        },
        label="Source media manifest",
    )
    if (
        manifest["format"] != "ffxivshare-media-manifest"
        or manifest["format_version"] != 2
        or manifest["hash_algorithm"] != "sha256"
        or manifest["path_normalization"]
        != "unicode_nfc_canonical_caseless_unique"
    ):
        raise HandoffError("Source media manifest format is unsupported.")
    _expect_timestamp(manifest["generated_at"], label="Media manifest generated_at")
    snapshot = _expect_exact_keys(
        manifest["source_snapshot"], {"id", "offline_confirmed"},
        label="Media manifest source_snapshot",
    )
    _expect_snapshot_id(snapshot["id"], label="Media manifest snapshot ID")
    if snapshot["offline_confirmed"] is not True:
        raise HandoffError("Source media manifest is not an offline snapshot.")
    file_count = _expect_integer(manifest["file_count"], label="Media manifest file_count")
    total_size = _expect_integer(manifest["total_size"], label="Media manifest total_size")
    files = manifest["files"]
    if not isinstance(files, list) or len(files) != file_count:
        raise HandoffError("Source media manifest row count is invalid.")
    seen: set[str] = set()
    ordered: list[str] = []
    calculated_size = 0
    for row in files:
        item = _expect_exact_keys(
            row, {"path", "size", "sha256"}, label="Source media manifest row"
        )
        relative = item["path"]
        if (
            not isinstance(relative, str)
            or not relative
            or relative != unicodedata.normalize("NFC", relative)
            or "\\" in relative
            or PurePosixPath(relative).is_absolute()
            or PurePosixPath(relative).as_posix() != relative
            or any(part in {"", ".", ".."} for part in PurePosixPath(relative).parts)
        ):
            raise HandoffError("Source media manifest contains an invalid path.")
        key = _media_path_key(relative)
        if key in seen:
            raise HandoffError("Source media manifest paths collide canonically.")
        seen.add(key)
        ordered.append(relative)
        calculated_size += _expect_integer(item["size"], label="Media row size")
        _expect_sha256(item["sha256"], label="Media row sha256")
    if ordered != sorted(ordered, key=lambda item: (_media_path_key(item), item)):
        raise HandoffError("Source media manifest rows are not canonical.")
    if calculated_size != total_size:
        raise HandoffError("Source media manifest total_size is invalid.")
    return manifest


def _load_authoritative_media_manifest(path: str) -> tuple[dict[str, Any], int, str]:
    payload, size, digest, _identity = _read_stable_file(
        path,
        maximum_size=MAX_MANIFEST_BYTES,
        label="Source media manifest",
    )
    strict_value = _strict_json_bytes(
        payload, label="Source media manifest", maximum_size=MAX_MANIFEST_BYTES
    )
    loaded = _validate_media_manifest_value(strict_value)
    if payload != _pretty_json_bytes(strict_value):
        raise HandoffError("Source media manifest is not canonical strict JSON.")
    second_payload, second_size, second_digest, _second_identity = _read_stable_file(
        path,
        maximum_size=MAX_MANIFEST_BYTES,
        label="Source media manifest",
    )
    if (second_payload != payload or second_size != size or second_digest != digest):
        raise HandoffError("Source media manifest changed during verification.")
    return loaded, size, digest


def _manifest_content_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "file_count": value["file_count"],
        "total_size": value["total_size"],
        "files": value["files"],
    }


def _build_stable_live_manifest(
    root: str,
    *,
    snapshot_id: str,
    label: str,
) -> dict[str, Any]:
    media = _media_module()
    try:
        first = media.build_manifest(Path(root), snapshot_id=snapshot_id)
        second = media.build_manifest(Path(root), snapshot_id=snapshot_id)
    except Exception as exc:
        raise HandoffError(f"{label} could not be inventoried stably.") from exc
    if _manifest_content_projection(first) != _manifest_content_projection(second):
        raise HandoffError(f"{label} changed during its repeated inventory.")
    return second


def _assert_manifest_content_equal(
    authoritative: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    label: str,
) -> None:
    if _manifest_content_projection(authoritative) != _manifest_content_projection(candidate):
        raise HandoffError(f"{label} does not exactly match the source media manifest.")


def _verify_content_from_paths(paths: Mapping[str, Any]) -> dict[str, Any]:
    backup = _verify_backup_content(
        paths["database"], paths["checksum"], paths["metadata"]
    )
    authoritative, manifest_size, manifest_digest = (
        _load_authoritative_media_manifest(paths["source_media_manifest"])
    )
    source_snapshot_id = authoritative["source_snapshot"]["id"]
    source_live = _build_stable_live_manifest(
        paths["source_media_root"],
        snapshot_id=source_snapshot_id,
        label="Source media root",
    )
    target_one_live = _build_stable_live_manifest(
        paths["target_media_root_one"],
        snapshot_id=paths["target_media_root_one_snapshot_id"],
        label="First rehearsal media target",
    )
    target_two_live = _build_stable_live_manifest(
        paths["target_media_root_two"],
        snapshot_id=paths["target_media_root_two_snapshot_id"],
        label="Second rehearsal media target",
    )
    _assert_manifest_content_equal(authoritative, source_live, label="Source media root")
    _assert_manifest_content_equal(
        authoritative, target_one_live, label="First rehearsal media target"
    )
    _assert_manifest_content_equal(
        authoritative, target_two_live, label="Second rehearsal media target"
    )
    return {
        "database_backup_set": {
            "database": backup["database"],
            "checksum": backup["checksum"],
            "metadata": backup["metadata"],
        },
        "captured_at": backup["captured_at"],
        "application_version": backup["application_version"],
        "source_media": {
            "root": paths["source_media_root"],
            "snapshot_id": source_snapshot_id,
            "manifest": {
                "path": paths["source_media_manifest"],
                "sha256": manifest_digest,
                "size": manifest_size,
                "generated_at": authoritative["generated_at"],
                "snapshot_id": source_snapshot_id,
                "file_count": authoritative["file_count"],
                "total_size": authoritative["total_size"],
            },
        },
    }


def _assert_nonoverlapping_paths(
    external_roots: Sequence[str],
    forbidden_roots: Sequence[str],
) -> None:
    for index, first in enumerate(external_roots):
        for second in external_roots[index + 1:]:
            if _windows_paths_overlap(first, second):
                raise HandoffError("External handoff scopes must not overlap.")
        for forbidden in forbidden_roots:
            if _windows_paths_overlap(first, forbidden):
                raise HandoffError("External handoff scope overlaps a forbidden root.")


def _resolve_live_paths(
    api: _Win32Api,
    *,
    repository_root: os.PathLike[str] | str,
    database: os.PathLike[str] | str,
    checksum: os.PathLike[str] | str,
    metadata: os.PathLike[str] | str,
    source_media_root: os.PathLike[str] | str,
    source_media_manifest: os.PathLike[str] | str,
    target_media_root_one: os.PathLike[str] | str,
    target_media_root_one_snapshot_id: str,
    target_media_root_two: os.PathLike[str] | str,
    target_media_root_two_snapshot_id: str,
    disallowed_roots: Iterable[os.PathLike[str] | str] = (),
) -> dict[str, Any]:
    repository = _resolve_existing_path(
        api, repository_root, label="Repository root", require_directory=True
    )
    resolved = {
        "repository_root": repository,
        "database": _resolve_existing_path(
            api, database, label="Source database", require_directory=False
        ),
        "checksum": _resolve_existing_path(
            api, checksum, label="Source checksum", require_directory=False
        ),
        "metadata": _resolve_existing_path(
            api, metadata, label="Source metadata", require_directory=False
        ),
        "source_media_root": _resolve_existing_path(
            api, source_media_root, label="Source media root", require_directory=True
        ),
        "source_media_manifest": _resolve_existing_path(
            api,
            source_media_manifest,
            label="Source media manifest",
            require_directory=False,
        ),
        "target_media_root_one": _resolve_existing_path(
            api,
            target_media_root_one,
            label="First rehearsal media target",
            require_directory=True,
        ),
        "target_media_root_two": _resolve_existing_path(
            api,
            target_media_root_two,
            label="Second rehearsal media target",
            require_directory=True,
        ),
        "target_media_root_one_snapshot_id": _expect_snapshot_id(
            target_media_root_one_snapshot_id,
            label="First rehearsal media target snapshot ID",
        ),
        "target_media_root_two_snapshot_id": _expect_snapshot_id(
            target_media_root_two_snapshot_id,
            label="Second rehearsal media target snapshot ID",
        ),
    }
    if resolved["checksum"] != f"{resolved['database']}.sha256":
        raise HandoffError("Checksum filename does not match the source database.")
    if resolved["metadata"] != f"{resolved['database']}.metadata.json":
        raise HandoffError("Metadata filename does not match the source database.")
    database_parent = ntpath.dirname(resolved["database"])
    if any(
        ntpath.dirname(resolved[name]) != database_parent
        for name in ("checksum", "metadata")
    ):
        raise HandoffError("The database backup set must share one directory.")
    resolved_disallowed = [repository]
    for index, raw_root in enumerate(disallowed_roots):
        resolved_disallowed.append(_resolve_existing_path(
            api,
            raw_root,
            label=f"Disallowed root {index + 1}",
            require_directory=None,
        ))
    external_roots = [
        database_parent,
        resolved["source_media_root"],
        resolved["source_media_manifest"],
        resolved["target_media_root_one"],
        resolved["target_media_root_two"],
    ]
    _assert_nonoverlapping_paths(external_roots, resolved_disallowed)
    resolved["database_parent"] = database_parent
    return resolved


def _assert_cli_repository_authority(
    api: _Win32Api,
    repository_root: os.PathLike[str] | str,
) -> str:
    try:
        executing_root = Path(__file__).resolve(strict=True).parents[2]
    except (OSError, IndexError) as exc:
        raise HandoffError("Executing handoff repository root cannot be identified.") from exc
    supplied = _resolve_existing_path(
        api,
        repository_root,
        label="Repository root",
        require_directory=True,
    )
    authority = _resolve_existing_path(
        api,
        executing_root,
        label="Executing repository root",
        require_directory=True,
    )
    if _windows_path_key(supplied) != _windows_path_key(authority):
        raise HandoffError(
            "CLI repository root must be the repository containing this handoff tool."
        )
    return authority


def _capture_access_from_paths(api: _Win32Api, paths: Mapping[str, Any]) -> dict[str, Any]:
    database_files = (paths["database"], paths["checksum"], paths["metadata"])
    scopes = [
        _capture_scope(
            api,
            role="database_backup_set",
            scope_path=paths["database_parent"],
            database_files=database_files,
        ),
        _capture_scope(
            api, role="source_media_root", scope_path=paths["source_media_root"]
        ),
        _capture_scope(
            api,
            role="source_media_manifest",
            scope_path=paths["source_media_manifest"],
        ),
        _capture_scope(
            api,
            role="target_media_root_1",
            scope_path=paths["target_media_root_one"],
        ),
        _capture_scope(
            api,
            role="target_media_root_2",
            scope_path=paths["target_media_root_two"],
        ),
    ]
    return _build_access_baseline(scopes)


def _paths_from_handoff(
    api: _Win32Api,
    handoff: Mapping[str, Any],
    repository_root: os.PathLike[str] | str,
    disallowed_roots: Iterable[os.PathLike[str] | str],
) -> dict[str, Any]:
    backup = handoff["database_backup_set"]
    source_media = handoff["source_media"]
    targets = handoff["rehearsal_targets"]
    paths = _resolve_live_paths(
        api,
        repository_root=repository_root,
        database=backup["database"]["path"],
        checksum=backup["checksum"]["path"],
        metadata=backup["metadata"]["path"],
        source_media_root=source_media["root"],
        source_media_manifest=source_media["manifest"]["path"],
        target_media_root_one=targets[0]["path"],
        target_media_root_one_snapshot_id=targets[0]["snapshot_id"],
        target_media_root_two=targets[1]["path"],
        target_media_root_two_snapshot_id=targets[1]["snapshot_id"],
        disallowed_roots=disallowed_roots,
    )
    recorded_paths = {
        "database": backup["database"]["path"],
        "checksum": backup["checksum"]["path"],
        "metadata": backup["metadata"]["path"],
        "source_media_root": source_media["root"],
        "source_media_manifest": source_media["manifest"]["path"],
        "target_media_root_one": targets[0]["path"],
        "target_media_root_two": targets[1]["path"],
    }
    for name, recorded in recorded_paths.items():
        if paths[name] != recorded:
            raise HandoffError(f"Recorded path no longer has canonical identity: {name}")
    return paths


def capture_access_baseline(
    handoff: Mapping[str, Any],
    repository_root: os.PathLike[str] | str,
    disallowed_roots: Iterable[os.PathLike[str] | str] = (),
) -> dict[str, Any]:
    """Capture a stable live access baseline without checking file content."""

    _require_windows_live_check()
    validated = validate_handoff(handoff)
    api = _Win32Api()
    paths = _paths_from_handoff(api, validated, repository_root, disallowed_roots)
    return _capture_access_from_paths(api, paths)


def compare_access_baselines(
    expected: Mapping[str, Any], actual: Mapping[str, Any]
) -> None:
    """Raise ``HandoffError`` unless two exact access snapshots match."""

    if expected != actual:
        raise HandoffError("Live access baseline does not match the handoff snapshot.")


def verify_access_baseline(
    handoff: Mapping[str, Any],
    repository_root: os.PathLike[str] | str,
    disallowed_roots: Iterable[os.PathLike[str] | str] = (),
) -> dict[str, Any]:
    """Capture and compare the access-only pre/post rehearsal gate."""

    validated = validate_handoff(handoff)
    actual = capture_access_baseline(validated, repository_root, disallowed_roots)
    compare_access_baselines(validated["access_baseline"], actual)
    return actual


def _compare_recorded_content(
    handoff: Mapping[str, Any], content: Mapping[str, Any]
) -> None:
    if handoff["database_backup_set"] != content["database_backup_set"]:
        raise HandoffError("Live database backup set does not match the handoff.")
    if handoff["source_media"] != content["source_media"]:
        raise HandoffError("Live source media evidence does not match the handoff.")
    if handoff["source"]["captured_at"] != content["captured_at"]:
        raise HandoffError("Backup capture timestamp does not match the handoff.")
    if (
        handoff["source"]["release_application_version"]
        != content["application_version"]
    ):
        raise HandoffError("Backup application version does not match the handoff.")


def verify_live_handoff(
    handoff: Mapping[str, Any],
    repository_root: os.PathLike[str] | str,
    disallowed_roots: Iterable[os.PathLike[str] | str] = (),
    verify_content: bool = True,
) -> dict[str, Any]:
    """Recheck live content/access and return the exact current access baseline.

    Every external scope is rejected if it overlaps the repository or either
    direction of any existing absolute ``disallowed_roots`` entry.
    """

    _require_windows_live_check()
    validated = validate_handoff(handoff)
    api = _Win32Api()
    paths = _paths_from_handoff(api, validated, repository_root, disallowed_roots)
    if verify_content:
        _compare_recorded_content(validated, _verify_content_from_paths(paths))
    actual = _capture_access_from_paths(api, paths)
    compare_access_baselines(validated["access_baseline"], actual)
    return actual


def _path_identity(path: str) -> tuple[int, int]:
    metadata = os.stat(path, follow_symlinks=False)
    return (metadata.st_dev, metadata.st_ino)


def _cleanup_retained_problem(path: str, *, label: str, reason: str) -> str:
    return f"{label} retained at {path}; quarantine required: {reason}."


def _unlink_if_identity(
    path: str,
    expected: tuple[int, int] | None,
    *,
    label: str,
) -> str | None:
    try:
        actual = _path_identity(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return _cleanup_retained_problem(
            path,
            label=label,
            reason=f"identity inspection failed with {type(exc).__name__}",
        )
    if expected is None:
        return _cleanup_retained_problem(
            path,
            label=label,
            reason="created-file identity was not captured",
        )
    if actual != expected:
        return _cleanup_retained_problem(
            path,
            label=label,
            reason="path identity no longer matches this invocation",
        )
    try:
        os.unlink(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return _cleanup_retained_problem(
            path,
            label=label,
            reason=f"unlink failed with {type(exc).__name__}",
        )
    return None


def _report_cleanup_problem(problem: str | None) -> None:
    if problem is not None:
        print(f"Production-copy handoff cleanup warning: {problem}", file=sys.stderr)


def _move_create_new_write_through(source: str, destination: str) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.MoveFileExW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
    ]
    kernel32.MoveFileExW.restype = wintypes.BOOL
    if not kernel32.MoveFileExW(source, destination, MOVEFILE_WRITE_THROUGH):
        error_code = ctypes.get_last_error()
        if os.path.lexists(destination):
            raise HandoffError("Handoff output appeared concurrently.")
        raise HandoffError(
            f"Handoff output could not be published durably (Win32 {error_code})."
        )


def _write_create_new(
    path: str,
    payload: Mapping[str, Any],
    *,
    publication_identity: list[tuple[int, int]] | None = None,
) -> tuple[int, int]:
    serialized = _canonical_json_bytes(payload)
    temporary = ntpath.join(
        ntpath.dirname(path), f".{ntpath.basename(path)}.tmp-{uuid4().hex}"
    )
    temporary_identity: tuple[int, int] | None = None
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        metadata = os.fstat(descriptor)
        temporary_identity = (metadata.st_dev, metadata.st_ino)
        if publication_identity is not None:
            publication_identity.append(temporary_identity)
        stream = os.fdopen(descriptor, "wb")
        descriptor = None
        with stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        if _path_identity(temporary) != temporary_identity:
            raise HandoffError("Handoff temporary file changed path identity.")
        _move_create_new_write_through(temporary, path)
        return temporary_identity
    except BaseException:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as exc:
                _report_cleanup_problem(
                    "Handoff temporary-file descriptor close failed with "
                    f"{type(exc).__name__}; path removal will still be verified."
                )
            descriptor = None
        _report_cleanup_problem(
            _unlink_if_identity(
                temporary,
                temporary_identity,
                label="Handoff temporary file",
            )
        )
        raise


def _confirmation_gate(arguments: argparse.Namespace) -> None:
    confirmations = (
        arguments.confirm_source_immutable,
        arguments.confirm_target_media_offline,
        arguments.confirm_database_media_consistent,
        arguments.confirm_operator_identity_asserted,
    )
    if not all(confirmations):
        raise HandoffError(
            "Create requires all four explicit source/operator/offline confirmations."
        )


def _create_command(arguments: argparse.Namespace) -> int:
    # This guard intentionally precedes every read of repository/external inputs.
    _require_windows_live_check()
    _confirmation_gate(arguments)
    source_host = _expect_string(arguments.source_host, label="Source host", maximum=255)
    operator = _expect_string(arguments.operator, label="Operator", maximum=255)
    expected_application_version = _expect_string(
        arguments.expected_application_version,
        label="Expected application version",
        maximum=255,
    )
    if expected_application_version.casefold() in PLACEHOLDER_APPLICATION_VERSIONS:
        raise HandoffError("Expected application version must not be a placeholder.")

    api = _Win32Api()
    repository_root = _assert_cli_repository_authority(api, arguments.repository_root)
    paths = _resolve_live_paths(
        api,
        repository_root=repository_root,
        database=arguments.source_database,
        checksum=arguments.source_checksum,
        metadata=arguments.source_metadata,
        source_media_root=arguments.source_media_root,
        source_media_manifest=arguments.source_media_manifest,
        target_media_root_one=arguments.target_media_root_one,
        target_media_root_one_snapshot_id=arguments.target_media_root_one_snapshot_id,
        target_media_root_two=arguments.target_media_root_two,
        target_media_root_two_snapshot_id=arguments.target_media_root_two_snapshot_id,
    )
    output_candidate = _runtime_input_shape(arguments.output, label="Handoff output")
    output = _validate_output_parent(api, output_candidate)
    forbidden_for_output = [
        paths["repository_root"],
        paths["database_parent"],
        paths["source_media_root"],
        paths["source_media_manifest"],
        paths["target_media_root_one"],
        paths["target_media_root_two"],
    ]
    if any(_windows_paths_overlap(output, root) for root in forbidden_for_output):
        raise HandoffError("Handoff output must not overlap inputs or repository.")

    first_content = _verify_content_from_paths(paths)
    if first_content["application_version"] != expected_application_version:
        raise HandoffError(
            "Backup metadata application_version does not exactly match the expected release."
        )
    first_access = _capture_access_from_paths(api, paths)

    # Repeat all expensive content/access gates before publishing. This binds a
    # stable capture rather than a single point-in-time observation.
    second_content = _verify_content_from_paths(paths)
    if second_content != first_content:
        raise HandoffError("Production-copy content changed across repeated verification.")
    second_access = _capture_access_from_paths(api, paths)
    compare_access_baselines(first_access, second_access)

    handoff = {
        "format": HANDOFF_FORMAT,
        "format_version": HANDOFF_VERSION,
        "generated_at": _utc_now(),
        "source": {
            "host": source_host,
            "captured_at": second_content["captured_at"],
            "operator": operator,
            "operator_identity_verification": OPERATOR_IDENTITY_ATTESTATION,
            "release_application_version": second_content["application_version"],
            "database_media_consistency": DATABASE_MEDIA_ATTESTATION,
            "source_immutable": OPERATOR_ASSERTED,
            "target_media_offline": OPERATOR_ASSERTED,
        },
        "database_backup_set": second_content["database_backup_set"],
        "source_media": second_content["source_media"],
        "rehearsal_targets": [
            {
                "slot": "first",
                "path": paths["target_media_root_one"],
                "snapshot_id": paths["target_media_root_one_snapshot_id"],
            },
            {
                "slot": "second",
                "path": paths["target_media_root_two"],
                "snapshot_id": paths["target_media_root_two_snapshot_id"],
            },
        ],
        "access_baseline": second_access,
        "limitations": dict(LIMITATIONS),
    }
    validate_handoff(handoff)
    if output != _validate_output_parent(api, output):
        raise HandoffError("Handoff output path identity changed before publication.")

    publication_identity: list[tuple[int, int]] = []
    try:
        _write_create_new(
            output,
            handoff,
            publication_identity=publication_identity,
        )
        _validate_published_output(api, output)
        (
            published,
            published_path,
            published_size,
            published_digest,
            published_identity,
        ) = _load_handoff_checkpoint(output)
        if published != handoff:
            raise HandoffError("Published handoff bytes do not match the verified payload.")
        after_access = _capture_access_from_paths(api, paths)
        compare_access_baselines(second_access, after_access)
        _checkpoint_handoff_file(
            published_path,
            expected_size=published_size,
            expected_digest=published_digest,
            expected_identity=published_identity,
        )
    except BaseException:
        _report_cleanup_problem(
            _unlink_if_identity(
                output,
                publication_identity[0] if publication_identity else None,
                label="Published handoff",
            )
        )
        raise
    print(f"Production-copy handoff created: {output}")
    return 0


def _verify_command(arguments: argparse.Namespace) -> int:
    # check-live is fail-closed on non-Windows before even the handoff is read.
    repository_root = arguments.repository_root
    if arguments.check_live:
        _require_windows_live_check()
        repository_root = _assert_cli_repository_authority(
            _Win32Api(), arguments.repository_root
        )
    if arguments.check_live:
        handoff, handoff_path, handoff_size, handoff_digest, handoff_identity = (
            _load_handoff_checkpoint(arguments.handoff)
        )
    else:
        handoff = load_handoff(arguments.handoff)
    if arguments.check_live:
        verify_live_handoff(handoff, repository_root)
        _checkpoint_handoff_file(
            handoff_path,
            expected_size=handoff_size,
            expected_digest=handoff_digest,
            expected_identity=handoff_identity,
        )
        print("Production-copy handoff schema, content, and access baseline verified.")
    else:
        print("Production-copy handoff canonical schema verified.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or verify a fail-closed FFXIVShare production-copy handoff."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--repository-root", required=True)
    create.add_argument("--source-database", required=True)
    create.add_argument("--source-checksum", required=True)
    create.add_argument("--source-metadata", required=True)
    create.add_argument("--source-media-root", required=True)
    create.add_argument("--source-media-manifest", required=True)
    create.add_argument("--target-media-root-one", required=True)
    create.add_argument("--target-media-root-one-snapshot-id", required=True)
    create.add_argument("--target-media-root-two", required=True)
    create.add_argument("--target-media-root-two-snapshot-id", required=True)
    create.add_argument("--source-host", required=True)
    create.add_argument("--operator", required=True)
    create.add_argument("--expected-application-version", required=True)
    create.add_argument("--output", required=True)
    create.add_argument("--confirm-source-immutable", action="store_true")
    create.add_argument("--confirm-target-media-offline", action="store_true")
    create.add_argument("--confirm-database-media-consistent", action="store_true")
    create.add_argument("--confirm-operator-identity-asserted", action="store_true")
    create.set_defaults(handler=_create_command)

    verify = commands.add_parser("verify")
    verify.add_argument("--handoff", required=True)
    verify.add_argument("--repository-root", required=True)
    verify.add_argument("--check-live", action="store_true")
    verify.set_defaults(handler=_verify_command)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        return arguments.handler(arguments)
    except KeyboardInterrupt:
        print("Production-copy handoff interrupted.", file=sys.stderr)
        return 130
    except HandoffError as exc:
        print(f"Production-copy handoff failed: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(
            f"Production-copy handoff failed during a filesystem operation: {exc}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(
            "Production-copy handoff failed closed with unexpected "
            f"{type(exc).__name__}.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
