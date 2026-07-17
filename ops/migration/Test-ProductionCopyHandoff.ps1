[CmdletBinding()]
param(
    [string]$RepositoryRoot = '',
    [string]$PythonExecutable = '',
    [string]$RunParent = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-Contract {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

function Remove-UniqueTestRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedParent
    )
    $resolved = [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    $parent = [System.IO.Path]::GetFullPath($ExpectedParent).TrimEnd('\', '/')
    $actualParent = [System.IO.Path]::GetDirectoryName($resolved).TrimEnd('\', '/')
    Assert-Contract `
        -Condition ($actualParent.Equals(
            $parent,
            [System.StringComparison]::OrdinalIgnoreCase
        )) `
        -Message 'Refusing to clean a handoff test root outside its exact parent.'
    Assert-Contract `
        -Condition ((Split-Path -Leaf $resolved) -match '^ffxivshare-handoff-[a-f0-9]{32}$') `
        -Message 'Refusing to clean a handoff test root with an unexpected name.'
    if (Test-Path -LiteralPath $resolved) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    $RepositoryRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
$RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path

if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $venvPython = Join-Path $RepositoryRoot 'venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "Handoff contract requires the project virtual environment: $venvPython"
    }
    $PythonExecutable = (Resolve-Path -LiteralPath $venvPython).Path
}
else {
    $PythonExecutable = (Resolve-Path -LiteralPath $PythonExecutable).Path
}

if ($env:OS -ne 'Windows_NT') {
    throw 'Production-copy handoff live contracts require Windows NTFS and DACL APIs.'
}

if ([string]::IsNullOrWhiteSpace($RunParent)) {
    $localApplicationData = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::LocalApplicationData
    )
    if ([string]::IsNullOrWhiteSpace($localApplicationData)) {
        throw 'The current-user private application-data directory is unavailable.'
    }
    $RunParent = Join-Path $localApplicationData 'FFXIVShare\MigrationContractTests'
}
[System.IO.Directory]::CreateDirectory($RunParent) | Out-Null
$RunParent = (Resolve-Path -LiteralPath $RunParent).Path

$handoffTool = Join-Path $PSScriptRoot 'ProductionCopyHandoff.py'
$bootstrapTool = Join-Path $PSScriptRoot 'ProductionCopyBootstrap.py'
Assert-Contract `
    -Condition (Test-Path -LiteralPath $handoffTool -PathType Leaf) `
    -Message "Production-copy handoff tool is missing: $handoffTool"

$temporaryRoot = Join-Path `
    $RunParent `
    ('ffxivshare-handoff-' + [Guid]::NewGuid().ToString('N'))
$fixtureScript = Join-Path $temporaryRoot 'test_handoff.py'
[System.IO.Directory]::CreateDirectory($temporaryRoot) | Out-Null

$fixtureSource = @'
from __future__ import annotations

from contextlib import redirect_stderr
from copy import deepcopy
import ctypes
from ctypes import wintypes
from hashlib import sha256
import importlib.util
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from typing import Any


handoff_path = Path(sys.argv[1]).resolve()
bootstrap_path = Path(sys.argv[2]).resolve()
repository = Path(sys.argv[3]).resolve()
test_root = Path(sys.argv[4]).resolve()


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


handoff = load_module("ffxivshare_handoff_contract", handoff_path)
bootstrap = load_module("ffxivshare_handoff_bootstrap_contract", bootstrap_path)
media = handoff._media_module()

assert sys.flags.isolated
assert sys.flags.no_site
assert sys.flags.dont_write_bytecode
assert sys.flags.utf8_mode
assert sys.flags.optimize == 0
assert handoff.HANDOFF_FORMAT == "ffxivshare-production-copy-handoff"
assert handoff.HANDOFF_VERSION == 1
assert handoff.MAX_MANIFEST_BYTES == 32 * 1024 * 1024
assert handoff.SCOPE_ROLES == (
    "database_backup_set",
    "source_media_root",
    "source_media_manifest",
    "target_media_root_1",
    "target_media_root_2",
)

for first, second in (
    (r"C:\Production\Database", r"c:\production\database"),
    (r"C:\Production\Database", r"C:\Production\Database\Backup"),
    (r"C:\Production\Database\Backup", r"C:\Production\Database"),
    (r"C:\Production\Database\.\Backup", r"C:\Production\Database\Backup"),
):
    assert handoff._windows_paths_overlap(first, second)
for first, second in (
    (r"C:\Production\Database", r"C:\Production\Database-Archive"),
    (r"C:\Production\Database", r"D:\Production\Database"),
):
    assert not handoff._windows_paths_overlap(first, second)

handoff._assert_nonoverlapping_paths(
    [r"C:\External\Database", r"C:\External\SourceMedia"],
    [r"C:\Repository"],
)
for external_roots, forbidden_roots in (
    ([r"C:\External\Data", r"C:\External\Data\Child"], []),
    ([r"C:\External\Data\Child"], [r"C:\External\Data"]),
    ([r"C:\External\Data"], [r"C:\External\Data\Child"]),
):
    try:
        handoff._assert_nonoverlapping_paths(external_roots, forbidden_roots)
    except handoff.HandoffError:
        pass
    else:
        raise AssertionError("Overlapping handoff scope was accepted.")

oversized_manifest = test_root / "oversized-media-manifest.json"
with oversized_manifest.open("wb") as stream:
    stream.seek(handoff.MAX_MANIFEST_BYTES)
    stream.write(b"\0")
try:
    handoff._load_authoritative_media_manifest(str(oversized_manifest))
except handoff.HandoffError as exc:
    assert "too large" in str(exc)
else:
    raise AssertionError("Oversized media manifest was accepted by handoff.")
oversized_manifest.unlink()


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32.LocalFree.argtypes = [ctypes.c_void_p]
kernel32.LocalFree.restype = ctypes.c_void_p
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


def set_dacl(path: Path, sddl: str) -> None:
    descriptor = ctypes.c_void_p()
    size = wintypes.DWORD()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        1,
        ctypes.byref(descriptor),
        ctypes.byref(size),
    ):
        raise OSError(ctypes.get_last_error(), "Cannot build test security descriptor")
    try:
        if not advapi32.SetFileSecurityW(str(path), 0x00000004, descriptor):
            raise OSError(
                ctypes.get_last_error(),
                f"Cannot apply test DACL: {path}",
            )
    finally:
        kernel32.LocalFree(descriptor)


current_sid = handoff._Win32Api().current_user_sid
private_sddl = (
    "D:P"
    f"(A;OICI;FA;;;{current_sid})"
    "(A;OICI;FA;;;S-1-5-18)"
    "(A;OICI;FA;;;S-1-5-32-544)"
)
weak_parent_output_sddl = (
    "D:P"
    f"(A;OICI;GRGWGX;;;{current_sid})"
    "(A;OICI;FA;;;S-1-5-18)"
    "(A;OICI;FA;;;S-1-5-32-544)"
)
weak_leaf_output_sddl = (
    "D:P"
    f"(A;;FA;;;{current_sid})"
    f"(A;OIIO;GRGWGX;;;{current_sid})"
    "(A;OICI;FA;;;S-1-5-18)"
    "(A;OICI;FA;;;S-1-5-32-544)"
)
weak_system_parent_output_sddl = (
    "D:P"
    f"(A;OICI;FA;;;{current_sid})"
    "(A;;GRGWGX;;;S-1-5-18)"
    "(A;OIIO;FA;;;S-1-5-18)"
    "(A;OICI;FA;;;S-1-5-32-544)"
)
weak_administrators_leaf_output_sddl = (
    "D:P"
    f"(A;OICI;FA;;;{current_sid})"
    "(A;OICI;FA;;;S-1-5-18)"
    "(A;;FA;;;S-1-5-32-544)"
    "(A;OIIO;GRGWGX;;;S-1-5-32-544)"
)
sealed_sddl = (
    "D:P"
    f"(A;;GRGX;;;{current_sid})"
    "(A;;FA;;;S-1-5-18)"
    "(A;;FA;;;S-1-5-32-544)"
)
unsafe_everyone_sddl = (
    "D:P"
    f"(A;;GRGX;;;{current_sid})"
    "(A;;FA;;;S-1-5-18)"
    "(A;;FA;;;S-1-5-32-544)"
    "(A;;GR;;;S-1-1-0)"
)
unsafe_direct_parent_sddl = (
    "D:P"
    f"(A;OICI;FA;;;{current_sid})"
    "(A;OICI;FA;;;S-1-5-18)"
    "(A;OICI;FA;;;S-1-5-32-544)"
    "(A;;GW;;;S-1-1-0)"
)


def iter_tree(path: Path) -> list[Path]:
    rows = [path]
    if path.is_dir():
        rows.extend(sorted(path.rglob("*"), key=lambda value: len(value.parts), reverse=True))
    return rows


def apply_tree_dacl(path: Path, sddl: str) -> None:
    for item in iter_tree(path):
        set_dacl(item, sddl)


def file_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def file_identity(path: Path) -> tuple[int, int, int, int, int]:
    metadata = path.stat()
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def external_snapshot(paths: list[Path]) -> dict[str, tuple[str, tuple[int, int, int, int, int]]]:
    return {
        str(path): (file_hash(path), file_identity(path))
        for path in paths
        if path.is_file()
    }


def run_cli(arguments: list[str], *, expected: int = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            str(Path(sys.executable).resolve()),
            "-I",
            "-S",
            "-B",
            "-X",
            "utf8",
            str(handoff_path),
            *arguments,
        ],
        cwd=repository,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != expected:
        raise AssertionError(
            f"Unexpected handoff exit {result.returncode}; expected {expected}.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def write_backup_set(directory: Path, *, application_version: str) -> tuple[Path, Path, Path]:
    directory.mkdir()
    database = directory / "production.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE fixture (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO fixture(value) VALUES ('handoff-contract')")
        connection.commit()
    finally:
        connection.close()
    digest = file_hash(database)
    checksum = Path(f"{database}.sha256")
    metadata = Path(f"{database}.metadata.json")
    checksum.write_text(
        f"{digest}  {database.name}\n",
        encoding="utf-8",
        newline="\n",
    )
    metadata.write_text(
        json.dumps(
            {
                "application_version": application_version,
                "backup_method": "sqlite_backup_api",
                "database_vendor": "sqlite",
                "foreign_key_check": "ok",
                "generated_at": "2026-07-17T00:00:00.000000Z",
                "integrity_check": "ok",
                "schema_version": 1,
                "sha256": digest,
                "size": database.stat().st_size,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return database, checksum, metadata


set_dacl(test_root, private_sddl)
database_dir = test_root / "Database"
manifest_dir = test_root / "Manifest"
source_media_root = test_root / "SourceMedia"
target_one = test_root / "TargetOne"
target_two = test_root / "TargetTwo"
output_dir = test_root / "Handoff"
weak_parent_output_dir = test_root / "WeakParentHandoff"
weak_leaf_output_dir = test_root / "WeakLeafHandoff"
weak_system_parent_output_dir = test_root / "WeakSystemParentHandoff"
weak_administrators_leaf_output_dir = test_root / "WeakAdministratorsLeafHandoff"
unknown_dir = test_root / "UnknownVersion"
placeholder_dir = test_root / "PlaceholderVersion"

try:
    database, checksum, metadata = write_backup_set(
        database_dir,
        application_version="release-contract-20260717",
    )
    unknown_database, unknown_checksum, unknown_metadata = write_backup_set(
        unknown_dir,
        application_version="unknown",
    )
    placeholder_database, placeholder_checksum, placeholder_metadata = write_backup_set(
        placeholder_dir,
        application_version="replace-with-deployed-release-id",
    )
    try:
        handoff._verify_backup_content(
            str(unknown_database),
            str(unknown_checksum),
            str(unknown_metadata),
        )
    except handoff.HandoffError:
        pass
    else:
        raise AssertionError("Unknown application_version was accepted.")
    try:
        handoff._verify_backup_content(
            str(placeholder_database),
            str(placeholder_checksum),
            str(placeholder_metadata),
        )
    except handoff.HandoffError as exc:
        assert "placeholder" in str(exc)
    else:
        raise AssertionError(
            "replace-with-deployed-release-id application_version was accepted."
        )

    media_file = source_media_root / "uploads" / "handoff.bin"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"FFXIVShare handoff contract\x00\xff\n")
    shutil.copytree(source_media_root, target_one)
    shutil.copytree(source_media_root, target_two)
    manifest_dir.mkdir()
    source_manifest = manifest_dir / "source-media-manifest.json"
    media._write_json_atomic(
        source_manifest,
        media.build_manifest(source_media_root, snapshot_id="handoff-source-media"),
    )
    output_dir.mkdir()
    set_dacl(output_dir, private_sddl)
    weak_parent_output_dir.mkdir()
    set_dacl(weak_parent_output_dir, weak_parent_output_sddl)
    weak_leaf_output_dir.mkdir()
    set_dacl(weak_leaf_output_dir, weak_leaf_output_sddl)
    weak_system_parent_output_dir.mkdir()
    set_dacl(weak_system_parent_output_dir, weak_system_parent_output_sddl)
    weak_administrators_leaf_output_dir.mkdir()
    set_dacl(
        weak_administrators_leaf_output_dir,
        weak_administrators_leaf_output_sddl,
    )

    for scope in (
        database_dir,
        source_media_root,
        source_manifest,
        target_one,
        target_two,
    ):
        apply_tree_dacl(scope, sealed_sddl)

    input_files = [
        database,
        checksum,
        metadata,
        source_manifest,
        media_file,
        target_one / "uploads" / "handoff.bin",
        target_two / "uploads" / "handoff.bin",
    ]
    bytes_before = external_snapshot(input_files)

    api = handoff._Win32Api()
    live_paths = handoff._resolve_live_paths(
        api,
        repository_root=repository,
        database=database,
        checksum=checksum,
        metadata=metadata,
        source_media_root=source_media_root,
        source_media_manifest=source_manifest,
        target_media_root_one=target_one,
        target_media_root_one_snapshot_id="handoff-target-one",
        target_media_root_two=target_two,
        target_media_root_two_snapshot_id="handoff-target-two",
    )
    access_before = handoff._capture_access_from_paths(api, live_paths)

    set_dacl(test_root, unsafe_direct_parent_sddl)
    try:
        try:
            handoff._capture_access_from_paths(api, live_paths)
        except handoff.HandoffError as exc:
            assert "ancestor grants mutation rights" in str(exc)
        else:
            raise AssertionError("Untrusted direct-parent write access was accepted.")
    finally:
        set_dacl(test_root, private_sddl)

    output = output_dir / "production-copy-handoff.json"
    create_arguments = [
        "create",
        "--repository-root", str(repository),
        "--source-database", str(database),
        "--source-checksum", str(checksum),
        "--source-metadata", str(metadata),
        "--source-media-root", str(source_media_root),
        "--source-media-manifest", str(source_manifest),
        "--target-media-root-one", str(target_one),
        "--target-media-root-one-snapshot-id", "handoff-target-one",
        "--target-media-root-two", str(target_two),
        "--target-media-root-two-snapshot-id", "handoff-target-two",
        "--source-host", "production-contract-host",
        "--operator", "DOMAIN.contract-operator",
        "--expected-application-version", "release-contract-20260717",
        "--output", str(output),
        "--confirm-source-immutable",
        "--confirm-target-media-offline",
        "--confirm-database-media-consistent",
        "--confirm-operator-identity-asserted",
    ]
    placeholder_expected_output = output_dir / "placeholder-release-handoff.json"
    placeholder_expected_arguments = list(create_arguments)
    placeholder_expected_arguments[
        placeholder_expected_arguments.index("--expected-application-version") + 1
    ] = "replace-with-deployed-release-id"
    placeholder_expected_arguments[
        placeholder_expected_arguments.index("--output") + 1
    ] = str(placeholder_expected_output)
    placeholder_expected_result = run_cli(placeholder_expected_arguments, expected=1)
    assert "placeholder" in placeholder_expected_result.stderr
    assert not placeholder_expected_output.exists()

    weak_parent_output = weak_parent_output_dir / "production-copy-handoff.json"
    weak_parent_arguments = list(create_arguments)
    weak_parent_arguments[weak_parent_arguments.index("--output") + 1] = str(
        weak_parent_output
    )
    weak_parent_result = run_cli(weak_parent_arguments, expected=1)
    assert (
        "full control" in weak_parent_result.stderr
        or "rollback" in weak_parent_result.stderr
    )
    assert not weak_parent_output.exists()
    assert not any(weak_parent_output_dir.iterdir()), (
        "Weak parent access created an output or temporary publication file."
    )

    weak_leaf_output = weak_leaf_output_dir / "production-copy-handoff.json"
    weak_leaf_arguments = list(create_arguments)
    weak_leaf_arguments[weak_leaf_arguments.index("--output") + 1] = str(
        weak_leaf_output
    )
    weak_leaf_result = run_cli(weak_leaf_arguments, expected=1)
    assert "full control" in weak_leaf_result.stderr
    assert not weak_leaf_output.exists()
    assert not any(weak_leaf_output_dir.iterdir()), (
        "Weak inherited leaf access created an output or temporary publication file."
    )

    weak_system_parent_output = (
        weak_system_parent_output_dir / "production-copy-handoff.json"
    )
    weak_system_parent_arguments = list(create_arguments)
    weak_system_parent_arguments[
        weak_system_parent_arguments.index("--output") + 1
    ] = str(weak_system_parent_output)
    weak_system_parent_result = run_cli(weak_system_parent_arguments, expected=1)
    assert "SYSTEM" in weak_system_parent_result.stderr
    assert "full control" in weak_system_parent_result.stderr
    assert not weak_system_parent_output.exists()
    assert not any(weak_system_parent_output_dir.iterdir())

    weak_administrators_leaf_output = (
        weak_administrators_leaf_output_dir / "production-copy-handoff.json"
    )
    weak_administrators_leaf_arguments = list(create_arguments)
    weak_administrators_leaf_arguments[
        weak_administrators_leaf_arguments.index("--output") + 1
    ] = str(weak_administrators_leaf_output)
    weak_administrators_leaf_result = run_cli(
        weak_administrators_leaf_arguments,
        expected=1,
    )
    assert "Administrators" in weak_administrators_leaf_result.stderr
    assert "full control" in weak_administrators_leaf_result.stderr
    assert not weak_administrators_leaf_output.exists()
    assert not any(weak_administrators_leaf_output_dir.iterdir())

    nested_output = source_media_root / "must-not-be-published.json"
    nested_output_arguments = list(create_arguments)
    nested_output_arguments[nested_output_arguments.index("--output") + 1] = str(
        nested_output
    )
    parsed_nested_output = handoff._parser().parse_args(nested_output_arguments)
    original_validate_output_parent = handoff._validate_output_parent
    handoff._validate_output_parent = lambda _api, _path: str(nested_output)
    try:
        try:
            handoff._create_command(parsed_nested_output)
        except handoff.HandoffError as exc:
            assert "must not overlap" in str(exc)
        else:
            raise AssertionError("Handoff output nested inside source media was accepted.")
    finally:
        handoff._validate_output_parent = original_validate_output_parent
    assert not nested_output.exists()

    run_cli(create_arguments)
    assert output.is_file()
    payload = handoff.load_handoff(output)
    assert payload["format"] == "ffxivshare-production-copy-handoff"
    assert payload["format_version"] == 1
    assert payload["source"]["release_application_version"] == "release-contract-20260717"
    assert payload["access_baseline"] == access_before
    assert [row["role"] for row in payload["access_baseline"]["scopes"]] == list(
        handoff.SCOPE_ROLES
    )
    for scope in payload["access_baseline"]["scopes"]:
        assert scope["ancestor_chain"]
        assert scope["dacl_inventory"]
        assert scope["node_inventory"]
        assert scope["owner_inventory"]
        assert sum(row["node_count"] for row in scope["dacl_inventory"]) == scope[
            "entry_count"
        ]
        assert sum(row["node_count"] for row in scope["owner_inventory"]) == scope[
            "entry_count"
        ]
        assert all(row["aces"] for row in scope["dacl_inventory"])
        assert len(scope["node_inventory"]) == scope["entry_count"]
        assert scope["node_inventory"][0]["relative_path"] == "."
    assert output.read_bytes() == handoff._canonical_json_bytes(payload)
    assert external_snapshot(input_files) == bytes_before

    current_content = handoff._verify_content_from_paths(live_paths)
    handoff._compare_recorded_content(payload, current_content)
    content_drift_cases = []
    database_drift = deepcopy(current_content)
    database_drift["database_backup_set"]["database"]["sha256"] = "0" * 64
    content_drift_cases.append(database_drift)
    media_drift = deepcopy(current_content)
    media_drift["source_media"]["manifest"]["sha256"] = "0" * 64
    content_drift_cases.append(media_drift)
    capture_drift = deepcopy(current_content)
    capture_drift["captured_at"] = "2026-07-18T00:00:00.000000Z"
    content_drift_cases.append(capture_drift)
    version_drift = deepcopy(current_content)
    version_drift["application_version"] = "release-contract-drift"
    content_drift_cases.append(version_drift)
    for drifted_content in content_drift_cases:
        try:
            handoff._compare_recorded_content(payload, drifted_content)
        except handoff.HandoffError:
            pass
        else:
            raise AssertionError("Live handoff content drift was accepted.")

    content_compare_calls = 0
    original_compare_recorded_content = handoff._compare_recorded_content

    def trace_recorded_content(*args: object, **kwargs: object) -> None:
        global content_compare_calls
        content_compare_calls += 1
        original_compare_recorded_content(*args, **kwargs)

    handoff._compare_recorded_content = trace_recorded_content
    try:
        handoff.verify_live_handoff(payload, repository)
    finally:
        handoff._compare_recorded_content = original_compare_recorded_content
    assert content_compare_calls == 1

    cleanup_identity_probe = output_dir / "cleanup-identity-probe.tmp"
    cleanup_identity_probe.write_bytes(b"cleanup identity probe")
    cleanup_identity = handoff._path_identity(str(cleanup_identity_probe))
    cleanup_identity_problem = handoff._unlink_if_identity(
        str(cleanup_identity_probe),
        (cleanup_identity[0], cleanup_identity[1] + 1),
        label="Cleanup identity probe",
    )
    assert cleanup_identity_problem is not None
    assert "retained" in cleanup_identity_problem
    assert "quarantine required" in cleanup_identity_problem
    assert "identity" in cleanup_identity_problem
    assert cleanup_identity_probe.exists()
    cleanup_identity_probe.unlink()

    temp_cleanup_output = output_dir / "temp-cleanup-failure.json"
    retained_temporary_paths: list[Path] = []
    original_move_create_new = handoff._move_create_new_write_through
    original_os_unlink = os.unlink

    def fail_publication_after_temp(source: str, _destination: str) -> None:
        retained_temporary_paths.append(Path(source))
        raise handoff.HandoffError("Simulated publication failure.")

    def refuse_temporary_unlink(path: object) -> None:
        candidate = Path(os.fspath(path))
        if retained_temporary_paths and candidate == retained_temporary_paths[0]:
            raise PermissionError(5, "Simulated temporary cleanup failure")
        original_os_unlink(path)

    temp_cleanup_stderr = StringIO()
    handoff._move_create_new_write_through = fail_publication_after_temp
    handoff.os.unlink = refuse_temporary_unlink
    try:
        with redirect_stderr(temp_cleanup_stderr):
            try:
                handoff._write_create_new(temp_cleanup_output, {"fixture": True})
            except handoff.HandoffError:
                pass
            else:
                raise AssertionError("Simulated publication failure was accepted.")
    finally:
        handoff._move_create_new_write_through = original_move_create_new
        handoff.os.unlink = original_os_unlink
    assert not temp_cleanup_output.exists()
    assert len(retained_temporary_paths) == 1
    assert retained_temporary_paths[0].exists()
    assert "retained" in temp_cleanup_stderr.getvalue()
    assert "quarantine required" in temp_cleanup_stderr.getvalue()
    original_os_unlink(retained_temporary_paths[0])

    hardlink_alias = output_dir / "handoff-hardlink-alias.json"
    os.link(output, hardlink_alias)
    try:
        try:
            handoff.load_handoff(hardlink_alias)
        except handoff.HandoffError:
            pass
        else:
            raise AssertionError("Hard-linked handoff authority was accepted.")
    finally:
        hardlink_alias.unlink()

    run_cli(
        [
            "verify",
            "--handoff", str(output),
            "--repository-root", str(repository),
            "--check-live",
        ]
    )
    stable_output_bytes = output.read_bytes()
    original_live_verify = handoff.verify_live_handoff

    def mutate_handoff_during_live_verify(*_args: object, **_kwargs: object) -> object:
        output.write_bytes(stable_output_bytes + b" ")
        return payload["access_baseline"]

    handoff.verify_live_handoff = mutate_handoff_during_live_verify
    try:
        try:
            handoff._verify_command(
                SimpleNamespace(
                    check_live=True,
                    handoff=str(output),
                    repository_root=str(repository),
                )
            )
        except handoff.HandoffError:
            pass
        else:
            raise AssertionError("Handoff mutation during live verification was accepted.")
    finally:
        handoff.verify_live_handoff = original_live_verify
        output.write_bytes(stable_output_bytes)
    wrong_repository_output = output_dir / "wrong-repository-handoff.json"
    wrong_repository_arguments = list(create_arguments)
    wrong_repository_arguments[
        wrong_repository_arguments.index("--repository-root") + 1
    ] = str(output_dir)
    wrong_repository_arguments[
        wrong_repository_arguments.index("--output") + 1
    ] = str(wrong_repository_output)
    run_cli(wrong_repository_arguments, expected=1)
    assert not wrong_repository_output.exists()
    original_output = output.read_bytes()
    run_cli(create_arguments, expected=1)
    assert output.read_bytes() == original_output

    extra = deepcopy(payload)
    extra["unexpected"] = True
    try:
        handoff.validate_handoff(extra)
    except handoff.HandoffError:
        pass
    else:
        raise AssertionError("Extra handoff field was accepted.")
    placeholder = deepcopy(payload)
    placeholder["source"]["release_application_version"] = "replace-me"
    try:
        handoff.validate_handoff(placeholder)
    except handoff.HandoffError:
        pass
    else:
        raise AssertionError("Placeholder release was accepted.")
    duplicate_target = deepcopy(payload)
    duplicate_target["rehearsal_targets"][1]["path"] = duplicate_target[
        "rehearsal_targets"
    ][0]["path"]
    try:
        handoff.validate_handoff(duplicate_target)
    except handoff.HandoffError:
        pass
    else:
        raise AssertionError("Duplicate target path was accepted.")
    node_inventory_tamper = deepcopy(payload)
    node_inventory_tamper["access_baseline"]["scopes"][0]["node_inventory"][0][
        "last_write_time"
    ] += 1
    try:
        handoff.validate_handoff(node_inventory_tamper)
    except handoff.HandoffError:
        pass
    else:
        raise AssertionError("Tampered archived node inventory was accepted.")

    noncanonical = output_dir / "noncanonical.json"
    noncanonical.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    try:
        handoff.load_handoff(noncanonical)
    except handoff.HandoffError:
        pass
    else:
        raise AssertionError("Noncanonical handoff bytes were accepted.")

    interrupted_output = output_dir / "interrupted-handoff.json"
    interrupted_arguments = list(create_arguments)
    interrupted_arguments[interrupted_arguments.index("--output") + 1] = str(
        interrupted_output
    )
    parsed_interrupted = handoff._parser().parse_args(interrupted_arguments)
    original_load_checkpoint = handoff._load_handoff_checkpoint

    def interrupt_after_publish(_path: object) -> object:
        raise KeyboardInterrupt

    handoff._load_handoff_checkpoint = interrupt_after_publish
    try:
        try:
            handoff._create_command(parsed_interrupted)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("Interrupted publication did not propagate interruption.")
    finally:
        handoff._load_handoff_checkpoint = original_load_checkpoint
    assert not interrupted_output.exists(), (
        "Interrupted handoff publication left an unverified manifest."
    )

    retained_output = output_dir / "retained-interrupted-handoff.json"
    retained_arguments = list(create_arguments)
    retained_arguments[retained_arguments.index("--output") + 1] = str(
        retained_output
    )
    parsed_retained = handoff._parser().parse_args(retained_arguments)
    retained_stderr = StringIO()
    original_os_unlink = os.unlink

    def refuse_published_unlink(path: object) -> None:
        if Path(os.fspath(path)) == retained_output:
            raise PermissionError(5, "Simulated published cleanup failure")
        original_os_unlink(path)

    handoff._load_handoff_checkpoint = interrupt_after_publish
    handoff.os.unlink = refuse_published_unlink
    try:
        with redirect_stderr(retained_stderr):
            try:
                handoff._create_command(parsed_retained)
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError("Retained publication did not propagate interruption.")
    finally:
        handoff._load_handoff_checkpoint = original_load_checkpoint
        handoff.os.unlink = original_os_unlink
    assert retained_output.exists()
    assert "Published handoff retained" in retained_stderr.getvalue()
    assert "quarantine required" in retained_stderr.getvalue()
    original_os_unlink(retained_output)

    original_argv = sys.argv
    original_load_handoff = handoff.load_handoff
    handoff.load_handoff = interrupt_after_publish
    try:
        sys.argv = [
            str(handoff_path),
            "verify",
            "--handoff",
            str(output),
            "--repository-root",
            str(repository),
        ]
        assert handoff.main() == 130
    finally:
        sys.argv = original_argv
        handoff.load_handoff = original_load_handoff

    set_dacl(media_file, unsafe_everyone_sddl)
    rejected = run_cli(
        [
            "verify",
            "--handoff", str(output),
            "--repository-root", str(repository),
            "--check-live",
        ],
        expected=1,
    )
    assert "untrusted SID" in rejected.stderr or "access baseline" in rejected.stderr
    set_dacl(media_file, sealed_sddl)
    run_cli(
        [
            "verify",
            "--handoff", str(output),
            "--repository-root", str(repository),
            "--check-live",
        ]
    )
    assert external_snapshot(input_files) == bytes_before

    original_os_name = handoff.os.name
    try:
        handoff.os.name = "posix"
        try:
            handoff._create_command(SimpleNamespace())
        except handoff.HandoffError as exc:
            assert "require Windows" in str(exc)
        else:
            raise AssertionError("Non-Windows create did not fail before arguments/input reads.")
    finally:
        handoff.os.name = original_os_name

    print("Production-copy handoff contracts passed.")
finally:
    if test_root.exists():
        for path in sorted(
            [test_root, *test_root.rglob("*")],
            key=lambda value: len(value.parts),
            reverse=True,
        ):
            try:
                set_dacl(path, private_sddl)
            except OSError:
                pass
'@

[System.IO.File]::WriteAllText(
    $fixtureScript,
    $fixtureSource,
    [System.Text.UTF8Encoding]::new($false)
)

try {
    & $PythonExecutable `
        -I -S -B -X utf8 `
        $fixtureScript `
        $handoffTool `
        $bootstrapTool `
        $RepositoryRoot `
        $temporaryRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Production-copy handoff contracts failed: $LASTEXITCODE"
    }
}
finally {
    Remove-UniqueTestRoot -Path $temporaryRoot -ExpectedParent $RunParent
}
