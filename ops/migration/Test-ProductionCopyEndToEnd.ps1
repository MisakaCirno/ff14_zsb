[CmdletBinding()]
param(
    [switch]$IncludeSlow,
    [string]$RepositoryRoot = '',
    [string]$PythonExecutable = '',
    [string]$RunParent = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $IncludeSlow.IsPresent) {
    Write-Output (
        'Production-copy end-to-end test skipped. ' +
        'Pass -IncludeSlow to run the real offline Proposal -> approval -> ' +
        'two approved rehearsals workflow.'
    )
    exit 0
}

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
        -Message 'Refusing to clean an E2E directory outside its exact parent.'
    Assert-Contract `
        -Condition ((Split-Path -Leaf $resolved) -match `
            '^ffxivshare-production-e2e-[a-f0-9]{32}$') `
        -Message 'Refusing to clean an E2E directory with an unexpected name.'
    if (Test-Path -LiteralPath $resolved) {
        if ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) {
            $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
            $icacls = Join-Path $env:SystemRoot 'System32\icacls.exe'
            Assert-Contract `
                -Condition (Test-Path -LiteralPath $icacls -PathType Leaf) `
                -Message 'Trusted Windows icacls executable is missing.'
            & $icacls $resolved /grant:r "${identity}:F" /T /C /Q | Out-Null
            Assert-Contract `
                -Condition ($LASTEXITCODE -eq 0) `
                -Message 'Failed to restore E2E test-only cleanup access.'
        }
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
        throw "Production-copy E2E requires the project virtual environment: $venvPython"
    }
    $PythonExecutable = (Resolve-Path -LiteralPath $venvPython).Path
}
else {
    $PythonExecutable = (Resolve-Path -LiteralPath $PythonExecutable).Path
}

if ([string]::IsNullOrWhiteSpace($RunParent)) {
    $localApplicationData = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::LocalApplicationData
    )
    if ([string]::IsNullOrWhiteSpace($localApplicationData)) {
        throw 'The current-user LocalAppData directory is unavailable.'
    }
    $RunParent = Join-Path `
        $localApplicationData `
        'FFXIVShare\MigrationE2EContractTests'
}
[System.IO.Directory]::CreateDirectory($RunParent) | Out-Null
$RunParent = (Resolve-Path -LiteralPath $RunParent).Path

$bootstrap = Join-Path $PSScriptRoot 'ProductionCopyBootstrap.py'
Assert-Contract `
    -Condition (Test-Path -LiteralPath $bootstrap -PathType Leaf) `
    -Message "Production-copy bootstrap is missing: $bootstrap"

$temporaryRoot = Join-Path `
    $RunParent `
    ('ffxivshare-production-e2e-' + [Guid]::NewGuid().ToString('N'))
$fixtureScript = Join-Path $temporaryRoot 'test_production_copy_e2e.py'
Assert-Contract `
    -Condition (-not (Test-Path -LiteralPath $temporaryRoot)) `
    -Message 'The create-new E2E root already exists before DACL preflight.'

# This short program is supplied through -c and loads only the trusted repository
# bootstrap.  It validates the parent chain before the unique leaf exists, creates
# that leaf atomically, applies the production DACL, and proves the directory is
# still empty.  No generated fixture bytes are published until it returns zero.
$trustedDaclPreflight = (
    'import importlib.util,os,sys; from pathlib import Path; ' +
    'p=Path(sys.argv[1]).resolve(); r=Path(sys.argv[2]); ' +
    's=importlib.util.spec_from_file_location(''ffxivshare_e2e_dacl_preflight'',p); ' +
    'm=importlib.util.module_from_spec(s); sys.modules[s.name]=m; ' +
    's.loader.exec_module(m); ' +
    'm._assert_no_reparse_components(r,include_leaf=False); ' +
    'parent=m._secure_run_root(r,parent_only=True); ' +
    'assert parent==''windows_parent_chain_delete_write_acl_review_passed'',parent; ' +
    'os.mkdir(r,0o700); status=m._secure_run_root(r); ' +
    'assert status==(''windows_protected_dacl_current_user_system_''' +
    '+''administrators_full_control_with_parent_chain_delete_acl_review''),status; ' +
    'assert r.is_dir() and not m._is_reparse_point(r); ' +
    'assert not any(r.iterdir()),''secured E2E root is not empty''; print(status)'
)
& $PythonExecutable `
    -I -S -B -X utf8 `
    -c $trustedDaclPreflight `
    $bootstrap `
    $temporaryRoot
$daclPreflightExitCode = $LASTEXITCODE
$global:LASTEXITCODE = 0
if ($daclPreflightExitCode -ne 0) {
    if (Test-Path -LiteralPath $fixtureScript) {
        [Console]::Error.WriteLine(
            'SECURITY CONTRACT VIOLATION: fixture bytes appeared after a failed DACL preflight.'
        )
    }
    [Console]::Error.WriteLine(
        'DACL preflight failed before fixture publication. Any created failure root is retained at: {0}',
        $temporaryRoot
    )
    [Console]::Out.WriteLine('DACL_FAILURE_ROOT={0}', $temporaryRoot)
    exit $daclPreflightExitCode
}
Assert-Contract `
    -Condition (Test-Path -LiteralPath $temporaryRoot -PathType Container) `
    -Message 'DACL preflight did not create the unique E2E root.'
Assert-Contract `
    -Condition (-not (Test-Path -LiteralPath $fixtureScript)) `
    -Message 'Fixture bytes appeared before the trusted DACL preflight completed.'
Assert-Contract `
    -Condition (@(Get-ChildItem -LiteralPath $temporaryRoot -Force).Count -eq 0) `
    -Message 'The secured E2E root is not empty before fixture publication.'

$fixtureSource = @'
from __future__ import annotations

import ctypes
from ctypes import wintypes
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Any


e2e_started_at = time.perf_counter()
stage_timings: dict[str, float] = {}
bootstrap_path = Path(sys.argv[1]).resolve()
repository = Path(sys.argv[2]).resolve()
test_root = Path(sys.argv[3]).resolve()

spec = importlib.util.spec_from_file_location(
    "ffxivshare_production_copy_e2e_bootstrap",
    bootstrap_path,
)
assert spec is not None and spec.loader is not None
bootstrap = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bootstrap
spec.loader.exec_module(bootstrap)

handoff_path = repository / "ops" / "migration" / "ProductionCopyHandoff.py"
handoff_spec = importlib.util.spec_from_file_location(
    "ffxivshare_production_copy_e2e_handoff",
    handoff_path,
)
assert handoff_spec is not None and handoff_spec.loader is not None
handoff = importlib.util.module_from_spec(handoff_spec)
sys.modules[handoff_spec.name] = handoff
handoff_spec.loader.exec_module(handoff)

assert os.name == "nt", "This contract currently proves the Windows production DACL path."
assert sys.flags.isolated
assert sys.flags.no_site
assert sys.flags.no_user_site
assert sys.flags.dont_write_bytecode
assert sys.flags.utf8_mode

PRIVATE_DACL_STATUS = (
    "windows_protected_dacl_current_user_system_administrators_full_control_"
    "with_parent_chain_delete_acl_review"
)
PROPOSAL_ENTRYPOINT = "ops/migration/Propose-ProductionCopyPolicy.py"
REHEARSAL_ENTRYPOINT = "ops/migration/Rehearse-ProductionCopy.py"
SOURCE_MIGRATION = ("shares", "0018_default_home_feed_waterfall")
SOURCE_FORWARD_MIGRATION_TARGETS = (
    ("contenttypes", "0002_remove_content_type_name"),
    ("auth", "0012_alter_user_first_name_max_length"),
    ("admin", "0003_logentry_add_action_flag_choices"),
    ("sessions", "0001_initial"),
    SOURCE_MIGRATION,
)
PENDING_MIGRATIONS = (
    ("shares", "0019_report_resolution_reason_share_review_feedback_and_more"),
    ("shares", "0020_replace_ckeditor_field"),
    ("shares", "0021_add_data_integrity_constraints"),
    ("shares", "0022_add_share_restrictions"),
    ("shares", "0023_userprofile_integrity"),
    ("shares", "0024_widen_site_message_titles"),
    ("shares", "0025_add_collection_owner_index"),
    ("shares", "0026_sync_announcement_permission_names"),
    ("shares", "0027_classify_legacy_private_shares"),
    ("shares", "0028_normalize_announcement_column_order"),
)
SQLITE_SEQUENCE_MINIMUM_HEADROOM = 1_000_000
SQLITE_SEQUENCE_REBUILD_FLOORS = {
    "shares_announcement": 9_600_006,
    "shares_collectionitem": 9_100_001,
    "shares_report": 9_200_002,
    "shares_share": 9_300_003,
    "shares_sharelog": 9_400_004,
    "shares_userprofile": 9_500_005,
}
SQLITE_SEQUENCE_CONTROL_FLOORS = {}
SQLITE_SEQUENCE_SOURCE_FLOORS = {
    **SQLITE_SEQUENCE_REBUILD_FLOORS,
    **SQLITE_SEQUENCE_CONTROL_FLOORS,
}
# shares/0023 creates one missing UserProfile, but SQLite may assign an unused
# primary key below the preserved AUTOINCREMENT high-water mark.  Therefore its
# sequence is required to stay at or above the source floor, not to increase.
# No pending migration or dataset import allocates a fresh primary key for the
# tables below, so their exact source floors are stable contract fixtures.
SQLITE_SEQUENCE_EXACT_DESTINATION_TABLES = (
    "shares_announcement",
    "shares_collectionitem",
    "shares_report",
    "shares_share",
    "shares_sharelog",
)
LEGACY_TABLES = (
    "auth_group",
    "auth_group_permissions",
    "auth_user",
    "auth_user_groups",
    "shares_announcement",
    "shares_collection",
    "shares_collectionitem",
    "shares_report",
    "shares_share",
    "shares_share_favorites",
    "shares_share_likes",
    "shares_sharelog",
    "shares_userprofile",
)
EXPECTED_ENTITY_COUNTS = {
    "groups": 1,
    "users": 4,
    "user_profiles": 4,
    "shares": 1,
    "collections": 1,
    "collection_items": 1,
    "reports": 1,
    "share_logs": 1,
    "announcements": 1,
    "site_messages": 0,
    "admin_log_entries": 1,
}

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
        raise OSError(ctypes.get_last_error(), "Cannot build E2E security descriptor")
    try:
        if not advapi32.SetFileSecurityW(str(path), 0x00000004, descriptor):
            raise OSError(ctypes.get_last_error(), f"Cannot apply E2E DACL: {path}")
    finally:
        kernel32.LocalFree(descriptor)


def apply_tree_dacl(path: Path, sddl: str) -> None:
    rows = [path]
    if path.is_dir():
        rows.extend(sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True))
    for item in rows:
        set_dacl(item, sddl)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


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


def protected_snapshot(path: Path) -> tuple[str, tuple[int, int, int, int, int]]:
    return file_hash(path), file_identity(path)


def tree_snapshot(root: Path) -> dict[str, tuple[str, tuple[int, int, int, int, int]]]:
    return {
        path.relative_to(root).as_posix(): protected_snapshot(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def assert_no_sidecars(database: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(f"{database}{suffix}").exists(), (database, suffix)


def run_command(
    argv: list[str],
    *,
    label: str,
    cwd: Path,
    env: dict[str, str] | None = None,
    expected_exit: int = 0,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != expected_exit:
        raise AssertionError(
            f"{label} returned {result.returncode}, expected {expected_exit}.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def django_environment(database: Path, media_root: Path, env_file: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "APP_ENV": "development",
            "DEBUG": "0",
            "DATABASE_ENGINE": "sqlite",
            "DATABASE_PATH": str(database),
            "SQLITE_TIMEOUT": "30",
            "SQLITE_TRANSACTION_MODE": "IMMEDIATE",
            "SQLITE_JOURNAL_MODE": "DELETE",
            "SQLITE_SYNCHRONOUS": "FULL",
            "MEDIA_ROOT": str(media_root),
            "APP_VERSION": "production-copy-e2e-contract",
            "FFXIVSHARE_ENV_FILE": str(env_file),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
        }
    )
    return env


def applied_migrations(database: Path) -> set[tuple[str, str]]:
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return {
            (str(app), str(name))
            for app, name in connection.execute(
                "SELECT app, name FROM django_migrations ORDER BY app, name"
            )
        }
    finally:
        connection.close()


def assert_forward_only_migration_recorder(database: Path) -> None:
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        count, minimum_id, maximum_id = connection.execute(
            "SELECT COUNT(*), MIN(id), MAX(id) FROM django_migrations"
        ).fetchone()
        sequence = connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'django_migrations'"
        ).fetchone()
        assert type(count) is int and count > 0, count
        assert minimum_id == 1, minimum_id
        assert maximum_id == count, (maximum_id, count)
        assert sequence == (maximum_id,), (sequence, maximum_id)
    finally:
        connection.close()


def _sqlite_sequence_values(connection: sqlite3.Connection) -> dict[str, int]:
    objects = connection.execute(
        "SELECT type FROM sqlite_schema WHERE name = 'sqlite_sequence'"
    ).fetchall()
    assert objects == [("table",)], objects
    rows = connection.execute(
        "SELECT name, seq FROM sqlite_sequence ORDER BY name COLLATE BINARY"
    ).fetchall()
    values: dict[str, int] = {}
    for table, sequence in rows:
        assert isinstance(table, str) and table
        assert type(sequence) is int and sequence >= 0, (table, sequence)
        assert table not in values, table
        values[table] = sequence
    assert list(values) == sorted(values)
    return values


def readonly_sqlite_sequence_values(database: Path) -> dict[str, int]:
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        assert connection.execute("PRAGMA query_only").fetchone() == (1,)
        return _sqlite_sequence_values(connection)
    finally:
        connection.close()


def inspection_sqlite_sequence_values(report: dict[str, Any]) -> dict[str, int]:
    inventory = report["inspection"]["sqlite_sequence"]
    assert set(inventory) == {"present", "count", "high_water_marks"}
    assert inventory["present"] is True
    marks = inventory["high_water_marks"]
    assert type(inventory["count"]) is int
    assert inventory["count"] == len(marks)
    values: dict[str, int] = {}
    for item in marks:
        assert set(item) == {"table", "sequence"}
        table = item["table"]
        sequence = item["sequence"]
        assert isinstance(table, str) and table
        assert type(sequence) is int and sequence >= 0, (table, sequence)
        assert table not in values, table
        values[table] = sequence
    assert list(values) == sorted(values)
    return values


def assert_sqlite_sequence_floors(
    values: dict[str, int],
    source_floors: dict[str, int],
    *,
    label: str,
    exact_tables: tuple[str, ...] = (),
) -> None:
    for table, source_floor in sorted(source_floors.items()):
        destination_value = values.get(table)
        assert destination_value is not None, (label, table, values)
        assert destination_value >= source_floor, (
            label,
            table,
            source_floor,
            destination_value,
        )
    for table in exact_tables:
        assert values[table] == source_floors[table], (
            label,
            table,
            source_floors[table],
            values[table],
        )


def set_source_sqlite_sequence_floors(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for table, source_floor in sorted(SQLITE_SEQUENCE_SOURCE_FLOORS.items()):
            quoted_table = '"' + table.replace('"', '""') + '"'
            table_object = connection.execute(
                "SELECT type FROM sqlite_schema WHERE name = ?",
                (table,),
            ).fetchall()
            assert table_object == [("table",)], (table, table_object)
            maximum_id = connection.execute(
                f'SELECT MAX("id") FROM {quoted_table}'
            ).fetchone()[0]
            assert type(maximum_id) is int, (table, maximum_id)
            assert source_floor - maximum_id >= SQLITE_SEQUENCE_MINIMUM_HEADROOM, (
                table,
                source_floor,
                maximum_id,
            )
            update = connection.execute(
                "UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
                (source_floor, table),
            )
            assert update.rowcount == 1, (table, update.rowcount)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def assert_source_sqlite_sequence_fixture(database: Path) -> dict[str, int]:
    values = readonly_sqlite_sequence_values(database)
    assert_sqlite_sequence_floors(
        values,
        SQLITE_SEQUENCE_SOURCE_FLOORS,
        label="deployed shares/0018 source fixture",
        exact_tables=tuple(SQLITE_SEQUENCE_SOURCE_FLOORS),
    )
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        assert connection.execute("PRAGMA query_only").fetchone() == (1,)
        for table, source_floor in sorted(SQLITE_SEQUENCE_SOURCE_FLOORS.items()):
            quoted_table = '"' + table.replace('"', '""') + '"'
            maximum_id = connection.execute(
                f'SELECT MAX("id") FROM {quoted_table}'
            ).fetchone()[0]
            assert type(maximum_id) is int, (table, maximum_id)
            assert source_floor - maximum_id >= SQLITE_SEQUENCE_MINIMUM_HEADROOM, (
                table,
                source_floor,
                maximum_id,
            )
    finally:
        connection.close()
    return values


def legacy_table_snapshot(database: Path) -> dict[str, dict[str, Any]]:
    """Capture every column and value owned by the deployed 0018 schema."""
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        snapshot: dict[str, dict[str, Any]] = {}
        for table in LEGACY_TABLES:
            columns = tuple(
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            )
            assert columns, table
            quoted_columns = ", ".join(f'"{column}"' for column in columns)
            rows = tuple(
                tuple(row)
                for row in connection.execute(
                    f'SELECT {quoted_columns} FROM "{table}" ORDER BY "id"'
                )
            )
            snapshot[table] = {"columns": columns, "rows": rows}
        return snapshot
    finally:
        connection.close()


def assert_legacy_table_snapshot_preserved(
    database: Path,
    expected: dict[str, dict[str, Any]],
) -> None:
    """Prove all pre-existing 0018 rows retain every old field and relation."""
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        for table, table_snapshot in expected.items():
            columns = table_snapshot["columns"]
            expected_rows = table_snapshot["rows"]
            quoted_columns = ", ".join(f'"{column}"' for column in columns)
            actual_rows = set(
                tuple(row)
                for row in connection.execute(
                    f'SELECT {quoted_columns} FROM "{table}"'
                )
            )
            missing = [row for row in expected_rows if row not in actual_rows]
            assert not missing, (table, missing)
    finally:
        connection.close()


def assert_expected_migration_outcome(database: Path) -> None:
    """Verify the only permitted additions and derivations after 0018."""
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        profiles = {
            username: (nickname, bio, mode)
            for username, nickname, bio, mode in connection.execute(
                """
                SELECT auth_user.username, shares_userprofile.nickname,
                       shares_userprofile.bio, shares_userprofile.home_feed_mode
                FROM auth_user
                JOIN shares_userprofile
                  ON shares_userprofile.user_id = auth_user.id
                ORDER BY auth_user.username
                """
            )
        }
        assert profiles == {
            "e2e-admin": (
                "E2E Admin",
                "Migration profile for e2e-admin",
                "paginated",
            ),
            "e2e-author": (
                "E2E Author",
                "Migration profile for e2e-author",
                "infinite",
            ),
            "e2e-missing-profile": ("", "", "infinite"),
            "e2e-reader": (
                "E2E Reader",
                "Migration profile for e2e-reader",
                "paginated",
            ),
        }
        missing_profile_times = connection.execute(
            """
            SELECT shares_userprofile.created_at, shares_userprofile.updated_at,
                   auth_user.date_joined
            FROM auth_user
            JOIN shares_userprofile
              ON shares_userprofile.user_id = auth_user.id
            WHERE auth_user.username = 'e2e-missing-profile'
            """
        ).fetchone()
        assert missing_profile_times is not None
        assert missing_profile_times[0] == missing_profile_times[1]
        assert missing_profile_times[0] == missing_profile_times[2]
        restriction = connection.execute(
            """
            SELECT restriction_state, restriction_reason, restricted_at,
                   restricted_by_id, review_feedback, reviewed_at, reviewed_by_id
            FROM shares_share WHERE share_id = '2a3b4c5d'
            """
        ).fetchone()
        assert restriction == ("clear", "", None, None, "", None, None)
        resolution_reason = connection.execute(
            "SELECT resolution_reason FROM shares_report"
        ).fetchone()
        assert resolution_reason == ("",)
        assert connection.execute(
            "SELECT COUNT(*) FROM shares_sitemessage"
        ).fetchone() == (0,)
    finally:
        connection.close()


def assert_artifact_reference(run_root: Path, reference: dict[str, Any]) -> Path:
    assert set(reference) == {"path", "size", "sha256"}
    relative = Path(reference["path"])
    assert not relative.is_absolute()
    assert ".." not in relative.parts
    artifact = run_root / relative
    assert artifact.is_file(), reference
    assert artifact.stat().st_size == reference["size"]
    assert file_hash(artifact) == reference["sha256"]
    return artifact


def read_ledger(run_root: Path) -> list[dict[str, Any]]:
    raw = (run_root / "evidence" / "events.jsonl").read_bytes()
    assert raw.endswith(b"\n")
    events = [json.loads(line) for line in raw.splitlines()]
    previous = "0" * 64
    for sequence, event in enumerate(events, start=1):
        assert event["sequence"] == sequence
        assert event["previous_event_sha256"] == previous
        declared = event["event_sha256"]
        unsigned = dict(event)
        del unsigned["event_sha256"]
        assert bootstrap._canonical_json_sha256(unsigned) == declared
        previous = declared
    return events


def assert_completion(run_root: Path, expected_exit: int) -> dict[str, Any]:
    completion = load_json(run_root / "evidence" / "completion.json")
    assert completion["inner_exit_code"] == expected_exit
    for key in (
        "execution_bundle_unchanged",
        "bootstrap_record_unchanged",
        "bundle_manifest_unchanged",
        "frozen_policy_unchanged",
        "frozen_proposal_unchanged",
        "frozen_review_unchanged",
    ):
        assert completion[key] is True, key
    return completion


def assert_bootstrap_success(outcome: Any, *, label: str) -> None:
    if outcome.exit_code == 0:
        return
    stderr = outcome.run_root / "logs" / "inner.stderr.log"
    details = stderr.read_text(encoding="utf-8", errors="replace") if stderr.exists() else ""
    raise AssertionError(
        f"{label} inner failed with exit code {outcome.exit_code}.\n{details}"
    )


def proposal_arguments(
    database: Path,
    checksum: Path,
    metadata: Path,
    media_manifest: Path,
    source_handoff_manifest: Path,
) -> tuple[str, ...]:
    return (
        "--source-database",
        str(database),
        "--source-checksum",
        str(checksum),
        "--source-metadata",
        str(metadata),
        "--source-media-manifest",
        str(media_manifest),
        "--source-handoff-manifest",
        str(source_handoff_manifest),
        "--policy-id",
        "production-copy-e2e-policy",
        "--proposal-id",
        "production-copy-e2e-proposal",
        "--confirm-source-immutable",
    )


root_access_control = bootstrap._secure_run_root(test_root)
assert root_access_control == PRIVATE_DACL_STATUS

inputs = test_root / "inputs"
source_directory = inputs / "source"
backup_directory = inputs / "backup"
source_media_root = inputs / "offline-media"
manifest_directory = inputs / "manifest"
tmp_directory = test_root / "tmp"
runs = test_root / "runs"
targets = test_root / "targets"
pair_verification_directory = test_root / "pair-verification"
for directory in (
    source_directory,
    backup_directory,
    source_media_root,
    manifest_directory,
    tmp_directory,
    runs,
    targets,
    pair_verification_directory,
):
    directory.mkdir(parents=True)
assert bootstrap._secure_run_root(pair_verification_directory) == PRIVATE_DACL_STATUS

env_file = inputs / "runtime-empty.env"
env_file.write_bytes(b"")
source_database = source_directory / "source.sqlite3"
setup_env = django_environment(source_database, source_media_root, env_file)
manage = [
    str(Path(sys.executable).resolve()),
    "-E",
    "-s",
    "-B",
    "-X",
    "utf8",
    str(repository / "manage.py"),
]

for app_label, migration_name in SOURCE_FORWARD_MIGRATION_TARGETS:
    run_command(
        [
            *manage,
            "migrate",
            app_label,
            migration_name,
            "--noinput",
            "--verbosity",
            "0",
        ],
        label=f"forward source migration {app_label}.{migration_name}",
        cwd=repository,
        env=setup_env,
    )
assert_forward_only_migration_recorder(source_database)

SEED_SCRIPT = r'''
from __future__ import annotations
import json
import os
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ffxivshare.settings")
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

executor = MigrationExecutor(connection)
historical_targets = [
    node for node in executor.loader.graph.leaf_nodes() if node[0] != "shares"
]
historical_targets.append(("shares", "0018_default_home_feed_waterfall"))
historical_apps = executor.loader.project_state(historical_targets).apps
LogEntry = historical_apps.get_model("admin", "LogEntry")
Group = historical_apps.get_model("auth", "Group")
Permission = historical_apps.get_model("auth", "Permission")
User = historical_apps.get_model("auth", "User")
ContentType = historical_apps.get_model("contenttypes", "ContentType")
Announcement = historical_apps.get_model("shares", "Announcement")
Collection = historical_apps.get_model("shares", "Collection")
CollectionItem = historical_apps.get_model("shares", "CollectionItem")
Report = historical_apps.get_model("shares", "Report")
Share = historical_apps.get_model("shares", "Share")
ShareLog = historical_apps.get_model("shares", "ShareLog")
UserProfile = historical_apps.get_model("shares", "UserProfile")

admin = User.objects.create(
    username="e2e-admin",
    email="admin@example.invalid",
    password="!e2e-unusable",
    is_staff=True,
    is_superuser=True,
    is_active=True,
)
author = User.objects.create(
    username="e2e-author",
    email="author@example.invalid",
    password="!e2e-unusable",
    is_active=True,
)
reader = User.objects.create(
    username="e2e-reader",
    email="reader@example.invalid",
    password="!e2e-unusable",
    is_active=True,
)
User.objects.create(
    username="e2e-missing-profile",
    email="missing-profile@example.invalid",
    password="!e2e-unusable",
    is_active=True,
)
for user, nickname, mode in (
    (admin, "E2E Admin", "paginated"),
    (author, "E2E Author", "infinite"),
    (reader, "E2E Reader", "paginated"),
):
    UserProfile.objects.update_or_create(
        user=user,
        defaults={
            "nickname": nickname,
            "bio": f"Migration profile for {user.username}",
            "home_feed_mode": mode,
        },
    )

moderators = Group.objects.create(name="e2e-moderators")
moderators.permissions.add(
    Permission.objects.get(
        content_type__app_label="shares",
        codename="change_share",
    )
)
admin.groups.add(moderators)
now = timezone.now()
share = Share.objects.create(
    share_id="2a3b4c5d",
    title="E2E representative combat strategy",
    strategy_code="{\"markers\":[1,2,3],\"version\":1}",
    description="<p>Representative migration content.</p>",
    author=author,
    category="combat",
    visibility="public",
    status="approved",
    is_spoiler=True,
    is_nsfw=False,
    is_original=True,
    views=42,
    copies=7,
)
share.likes.add(admin, reader)
share.favorites.add(reader)
report = Report.objects.create(
    share=share,
    reporter=reader,
    reason="Representative moderation report.",
    status="dismissed",
    resolved_at=now,
    resolved_by=admin,
)
Announcement.objects.create(
    title="E2E migration announcement",
    content="<p>Representative announcement content.</p>",
    is_active=True,
)
collection = Collection.objects.create(
    title="E2E collection",
    description="Representative collection relationship.",
    author=author,
    is_public=True,
)
CollectionItem.objects.create(collection=collection, share=share, order=0)
ShareLog.objects.create(
    share=share,
    user=admin,
    action="approve",
    details="Representative review audit log.",
)
LogEntry.objects.create(
    user=admin,
    content_type=ContentType.objects.get_for_model(Share),
    object_id=str(share.pk),
    object_repr=share.title,
    action_flag=2,
    change_message=json.dumps(
        [{"changed": {"fields": ["status", "review_feedback"]}}],
        separators=(",", ":"),
    ),
)
assert User.objects.count() == 4
assert UserProfile.objects.count() == 3
assert Share.objects.count() == 1
assert share.likes.count() == 2
assert share.favorites.count() == 1
assert Report.objects.count() == 1
assert Collection.objects.count() == 1
assert CollectionItem.objects.count() == 1
assert ShareLog.objects.count() == 1
assert Announcement.objects.count() == 1
assert LogEntry.objects.count() == 1
'''
run_command(
    [
        str(Path(sys.executable).resolve()),
        "-E",
        "-s",
        "-B",
        "-X",
        "utf8",
        "-c",
        SEED_SCRIPT,
    ],
    label="representative source fixture creation",
    cwd=repository,
    env=setup_env,
)
set_source_sqlite_sequence_floors(source_database)
source_sequence_fixture = assert_source_sqlite_sequence_fixture(source_database)

source_applied = applied_migrations(source_database)
assert SOURCE_MIGRATION in source_applied
assert not set(PENDING_MIGRATIONS).intersection(source_applied)
legacy_snapshot = legacy_table_snapshot(source_database)
assert_no_sidecars(source_database)

source_backup = backup_directory / "production.sqlite3"
source_checksum = Path(f"{source_backup}.sha256")
source_metadata = Path(f"{source_backup}.metadata.json")
run_command(
    [*manage, "backup_database", str(source_backup)],
    label="real source backup_database",
    cwd=repository,
    env=setup_env,
)
assert applied_migrations(source_backup) == source_applied
assert readonly_sqlite_sequence_values(source_backup) == source_sequence_fixture
assert_no_sidecars(source_backup)

media_file = source_media_root / "uploads" / "e2e" / "strategy-preview.bin"
media_file.parent.mkdir(parents=True)
media_file.write_bytes(
    b"\x00FFXIVShare-production-copy-e2e\xff\x10representative-media\n"
)
source_media_manifest = manifest_directory / "source-media-manifest.json"
run_command(
    [
        str(Path(sys.executable).resolve()),
        "-E",
        "-s",
        "-B",
        "-X",
        "utf8",
        str(repository / "ops" / "migration" / "MediaManifest.py"),
        "build",
        "--root",
        str(source_media_root),
        "--output",
        str(source_media_manifest),
        "--snapshot-id",
        "production-copy-e2e-source-media",
        "--confirm-offline-snapshot",
    ],
    label="real source media manifest",
    cwd=repository,
)
media_manifest = load_json(source_media_manifest)
assert media_manifest["source_snapshot"] == {
    "id": "production-copy-e2e-source-media",
    "offline_confirmed": True,
}
assert media_manifest["file_count"] == 1
assert media_manifest["files"][0]["path"] == "uploads/e2e/strategy-preview.bin"

target_media_roots = {
    "approved-rehearsal-one": targets / "approved-rehearsal-one-media",
    "approved-rehearsal-two": targets / "approved-rehearsal-two-media",
}
target_media_snapshot_ids = {
    "approved-rehearsal-one": "production-copy-e2e-target-media-one",
    "approved-rehearsal-two": "production-copy-e2e-target-media-two",
}
for target_media_root in target_media_roots.values():
    shutil.copytree(source_media_root, target_media_root)
    target_media_file = target_media_root / "uploads" / "e2e" / "strategy-preview.bin"
    assert target_media_file.read_bytes() == media_file.read_bytes()
    assert file_identity(target_media_file)[:2] != file_identity(media_file)[:2]

current_sid = handoff._Win32Api().current_user_sid
sealed_sddl = (
    "D:P"
    f"(A;;GRGX;;;{current_sid})"
    "(A;;FA;;;S-1-5-18)"
    "(A;;FA;;;S-1-5-32-544)"
)
for sealed_scope in (
    backup_directory,
    source_media_root,
    source_media_manifest,
    *target_media_roots.values(),
):
    apply_tree_dacl(sealed_scope, sealed_sddl)

handoff_directory = test_root / "handoff"
handoff_directory.mkdir()
assert bootstrap._secure_run_root(handoff_directory) == PRIVATE_DACL_STATUS
source_handoff_manifest = handoff_directory / "source-handoff-manifest.json"
run_command(
    [
        str(Path(sys.executable).resolve()),
        "-I",
        "-S",
        "-B",
        "-X",
        "utf8",
        str(handoff_path),
        "create",
        "--repository-root",
        str(repository),
        "--source-database",
        str(source_backup),
        "--source-checksum",
        str(source_checksum),
        "--source-metadata",
        str(source_metadata),
        "--source-media-root",
        str(source_media_root),
        "--source-media-manifest",
        str(source_media_manifest),
        "--target-media-root-one",
        str(target_media_roots["approved-rehearsal-one"]),
        "--target-media-root-one-snapshot-id",
        target_media_snapshot_ids["approved-rehearsal-one"],
        "--target-media-root-two",
        str(target_media_roots["approved-rehearsal-two"]),
        "--target-media-root-two-snapshot-id",
        target_media_snapshot_ids["approved-rehearsal-two"],
        "--source-host",
        "production-copy-e2e-source-host",
        "--operator",
        "production-copy-e2e-operator",
        "--expected-application-version",
        "production-copy-e2e-contract",
        "--output",
        str(source_handoff_manifest),
        "--confirm-source-immutable",
        "--confirm-target-media-offline",
        "--confirm-database-media-consistent",
        "--confirm-operator-identity-asserted",
    ],
    label="real production-copy handoff",
    cwd=repository,
)
handoff_payload = handoff.load_handoff(source_handoff_manifest)
assert handoff_payload["source"]["release_application_version"] == (
    "production-copy-e2e-contract"
)
assert [target["path"] for target in handoff_payload["rehearsal_targets"]] == [
    str(target_media_roots["approved-rehearsal-one"]),
    str(target_media_roots["approved-rehearsal-two"]),
]

protected_paths = (
    source_database,
    source_backup,
    source_checksum,
    source_metadata,
    source_media_manifest,
    media_file,
    source_handoff_manifest,
)
protected_before = {path: protected_snapshot(path) for path in protected_paths}
source_media_tree_before = tree_snapshot(source_media_root)

proposal_root = runs / "proposal"
proposal_args = proposal_arguments(
    source_backup,
    source_checksum,
    source_metadata,
    source_media_manifest,
    source_handoff_manifest,
)
stage_started_at = time.perf_counter()
proposal_outcome = bootstrap.run_bootstrap(
    bootstrap.BootstrapConfig(
        repository_root=repository,
        python_executable=Path(sys.executable).resolve(),
        run_root=proposal_root,
        mode="policy-proposal",
        inner_entrypoint=PROPOSAL_ENTRYPOINT,
        inner_arguments=proposal_args,
    )
)
assert_bootstrap_success(proposal_outcome, label="policy proposal")
assert_completion(proposal_root, 0)
proposal_bootstrap = load_json(proposal_root / "evidence" / "bootstrap.json")
assert proposal_bootstrap["workspace_access_control"] == PRIVATE_DACL_STATUS
assert proposal_bootstrap["configuration"]["inner_arguments"] == list(proposal_args)
proposal_path = proposal_root / "evidence" / "policy-proposal.json"
proposal = load_json(proposal_path)
proposal_sha256 = file_hash(proposal_path)
assert proposal["state"] == "review_required"
assert proposal["format_version"] == 2
assert proposal["body"]["format_version"] == 2
assert proposal["body"]["policy_projection"]["source_database_sha256"] == file_hash(
    source_backup
)
frozen_handoff = assert_artifact_reference(
    proposal_root,
    proposal["body"]["evidence"]["source_handoff_manifest"],
)
assert file_hash(frozen_handoff) == file_hash(source_handoff_manifest)
frozen_source_inspection = assert_artifact_reference(
    proposal_root,
    proposal["body"]["evidence"]["source_snapshot_inspection"],
)
source_inspection = load_json(frozen_source_inspection)
proposal_source_sequence = inspection_sqlite_sequence_values(source_inspection)
assert proposal_source_sequence == source_sequence_fixture
source_sqlite_schema_sha256 = source_inspection["inspection"]["sqlite_schema"][
    "sha256"
]
assert (
    proposal["body"]["policy_projection"]["source_sqlite_schema_sha256"]
    == source_sqlite_schema_sha256
)
proposal_stages = [event["stage"] for event in read_ledger(proposal_root)]
assert proposal_stages.index("source_handoff_verified") < proposal_stages.index(
    "policy_proposal_body_created"
)
assert proposal_stages.index(
    "policy_proposal_body_created"
) < proposal_stages.index("source_handoff_final_verified")
assert proposal_stages.index("source_handoff_final_verified") < proposal_stages.index(
    "source_final_verified"
)
assert set(PENDING_MIGRATIONS) == {
    tuple(node)
    for node in proposal["body"]["review_requirements"]["pending_migration_nodes"]
}
stage_timings["proposal"] = time.perf_counter() - stage_started_at

stage_started_at = time.perf_counter()
approval_cli = (
    proposal_root
    / "code"
    / "ops"
    / "migration"
    / "Approve-ProductionCopyPolicy.py"
)
approval_prefix = [
    str(Path(sys.executable).resolve()),
    "-I",
    "-S",
    "-B",
    "-X",
    "utf8",
    str(approval_cli),
]
approval_environment = os.environ.copy()
approval_environment.update({"TEMP": str(tmp_directory), "TMP": str(tmp_directory)})
reviewer = "production-copy-e2e-reviewer"
negative_review = proposal_root / "approval" / "wrong-hash-review.json"
negative = run_command(
    [
        *approval_prefix,
        "record-review",
        "--proposal",
        str(proposal_path),
        "--proposal-run-root",
        str(proposal_root),
        "--expected-proposal-sha256",
        "0" * 64,
        "--review-id",
        "production-copy-e2e-negative-review",
        "--reviewer",
        reviewer,
        "--notes",
        "This negative path must not publish a review.",
        "--output",
        str(negative_review),
        "--confirm-lossless-reviewed",
        "--confirm-reviewer-operator-asserted",
    ],
    label="wrong proposal approval hash negative",
    cwd=proposal_root / "code",
    env=approval_environment,
    expected_exit=1,
)
assert "SHA-256 does not match the expected value" in negative.stderr
assert not negative_review.exists()

review_path = proposal_root / "approval" / "lossless-review.json"
run_command(
    [
        *approval_prefix,
        "record-review",
        "--proposal",
        str(proposal_path),
        "--proposal-run-root",
        str(proposal_root),
        "--expected-proposal-sha256",
        proposal_sha256,
        "--review-id",
        "production-copy-e2e-lossless-review",
        "--reviewer",
        reviewer,
        "--notes",
        (
            "Reviewed the complete shares/0019 through shares/0028 plan as "
            "lossless for this deployed-0018 synthetic E2E source."
        ),
        "--output",
        str(review_path),
        "--confirm-lossless-reviewed",
        "--confirm-reviewer-operator-asserted",
    ],
    label="frozen proposal record-review",
    cwd=proposal_root / "code",
    env=approval_environment,
)
review = load_json(review_path)
review_sha256 = file_hash(review_path)
assert review["conclusion"] == "lossless"
assert review["proposal_sha256"] == proposal_sha256
assert set(PENDING_MIGRATIONS) == {
    tuple(node) for node in review["migrations_reviewed"]
}

policy_path = proposal_root / "approval" / "approved-policy.json"
run_command(
    [
        *approval_prefix,
        "approve",
        "--proposal",
        str(proposal_path),
        "--proposal-run-root",
        str(proposal_root),
        "--expected-proposal-sha256",
        proposal_sha256,
        "--review",
        str(review_path),
        "--expected-review-sha256",
        review_sha256,
        "--reviewer",
        reviewer,
        "--output",
        str(policy_path),
        "--confirm-lossless-reviewed",
        "--confirm-reviewer-operator-asserted",
    ],
    label="frozen proposal approve",
    cwd=proposal_root / "code",
    env=approval_environment,
)
policy = load_json(policy_path)
assert policy["approved"] is True
assert policy["lossless_reviewed"] is True
assert policy["proposal_sha256"] == proposal_sha256
assert policy["review_record_sha256"] == review_sha256
assert policy["source_sqlite_schema_sha256"] == source_sqlite_schema_sha256
approval_before_rehearsals = {
    path: protected_snapshot(path)
    for path in (proposal_path, review_path, policy_path)
}
stage_timings["review_and_approval"] = time.perf_counter() - stage_started_at


def assert_entity_counts(manifest: dict[str, Any]) -> None:
    entities = manifest["entities"]
    for name, expected in EXPECTED_ENTITY_COUNTS.items():
        assert entities[name]["count"] == expected, (name, entities[name])


def run_approved_rehearsal(name: str, media_snapshot_id: str) -> dict[str, str]:
    target_media_root = target_media_roots[name]
    assert media_snapshot_id == target_media_snapshot_ids[name]
    target_media_before = tree_snapshot(target_media_root)
    target_media_file = target_media_root / "uploads" / "e2e" / "strategy-preview.bin"
    assert target_media_file.read_bytes() == media_file.read_bytes()
    assert file_identity(target_media_file)[:2] != file_identity(media_file)[:2]

    run_root = runs / name
    inner_arguments = (
        "--source-database",
        str(source_backup),
        "--source-checksum",
        str(source_checksum),
        "--source-metadata",
        str(source_metadata),
        "--source-proposal-run-root",
        str(proposal_root),
        "--source-media-manifest",
        str(source_media_manifest),
        "--target-media-root",
        str(target_media_root),
        "--target-media-snapshot-id",
        media_snapshot_id,
        "--run-root",
        str(run_root),
        "--confirm-source-immutable",
        "--confirm-target-media-offline",
    )
    outcome = bootstrap.run_bootstrap(
        bootstrap.BootstrapConfig(
            repository_root=repository,
            python_executable=Path(sys.executable).resolve(),
            run_root=run_root,
            mode="approved-rehearsal",
            inner_entrypoint=REHEARSAL_ENTRYPOINT,
            inner_arguments=inner_arguments,
            policy_path=policy_path,
            proposal_path=proposal_path,
            review_record_path=review_path,
        )
    )
    assert_bootstrap_success(outcome, label=name)
    completion = assert_completion(run_root, 0)
    record = load_json(run_root / "evidence" / "bootstrap.json")
    assert record["workspace_access_control"] == PRIVATE_DACL_STATUS
    assert record["configuration"]["inner_arguments"] == list(inner_arguments)
    assert record["policy"]["source"]["sha256"] == file_hash(policy_path)
    assert record["approval_inputs"]["proposal"]["source"]["sha256"] == proposal_sha256
    assert record["approval_inputs"]["review"]["source"]["sha256"] == review_sha256
    assert completion["run_id"] == record["run_id"]

    result = load_json(run_root / "evidence" / "result.json")
    assert result["status"] == "completed"
    assert result["issues"] == []
    assert result["source_database_unchanged"] is True
    assert result["cutover_authorized"] is False
    assert result["retained_on_success"] is True
    assert result["sensitive_retention_scope"] == "entire_run_root"
    required_stages = {
        "approved_policy_evidence_verified",
        "database_structure_preserved",
        "import_verified",
        "idempotence_verified",
        "target_export_compared",
        "restriction_preflight",
        "target_snapshot_verified",
        "target_snapshot_set_final_verified",
        "final_target_dataset_validated",
        "final_target_export_compared",
        "final_target_restriction_preflight",
        "final_media_verified",
        "deployment_candidate_verified",
    }
    assert required_stages.issubset(set(result["completed_stages"]))

    first_import = load_json(run_root / "evidence" / "target-import.json")
    assert first_import["status"] == "imported"
    assert first_import["target_state"] == "empty"
    assert first_import["database_state"] == "complete"
    assert first_import["data_stage"] == "verified"
    assert first_import["sequence_stage"] == "verified"
    assert first_import["target_session_row_count"] == 0
    assert first_import["cutover_authorized"] is False
    idempotence = load_json(
        run_root / "evidence" / "target-import-idempotence.json"
    )
    assert idempotence["status"] == "already_imported"
    assert idempotence["target_state"] == "complete"
    assert idempotence["database_state"] == "complete"
    assert idempotence["data_stage"] == "verified"
    assert idempotence["sequence_stage"] == "verified"
    assert idempotence["target_session_row_count"] == 0
    assert idempotence["cutover_authorized"] is False

    for validation_name in (
        "source-validation.json",
        "target-validation.json",
        "final-target-validation.json",
    ):
        validation = load_json(run_root / "evidence" / validation_name)
        assert validation["valid"] is True
        assert validation["errors"] == []
        assert validation["quarantined_records"] == []

    initial_comparison = load_json(
        run_root / "evidence" / "site-data-comparison.json"
    )
    final_comparison = load_json(
        run_root / "evidence" / "final-target-site-data-comparison.json"
    )
    for comparison in (initial_comparison, final_comparison):
        assert comparison["equivalent"] is True
        assert comparison["issues"] == []
        assert comparison["cutover_authorized"] is False

    restriction = load_json(run_root / "evidence" / "restriction-preflight.json")
    final_restriction = load_json(
        run_root / "evidence" / "final-target-restriction-preflight.json"
    )
    for preflight in (restriction, final_restriction):
        assert preflight["valid"] is True
        assert preflight["ready_for_cutover"] is True
        assert preflight["blocking_errors"] == []
        assert preflight["manual_review"]["count"] == 0
        assert preflight["manual_review"]["share_ids"] == []

    backup_initial = load_json(run_root / "evidence" / "target-backup-set.json")
    backup_final = load_json(
        run_root / "evidence" / "target-backup-set-final.json"
    )
    for backup_report in (backup_initial, backup_final):
        assert backup_report["verified"] is True
        assert backup_report["cutover_authorized"] is False
        assert backup_report["checks"] == {
            "checksum_bytes_exact": True,
            "input_set_unchanged": True,
            "metadata_contract": True,
            "sqlite_magic": True,
        }
    assert backup_initial["artifact"]["sha256"] == backup_final["artifact"]["sha256"]
    inspection = load_json(
        run_root / "evidence" / "target-backup-inspection.json"
    )
    assert inspection["database"]["sha256"] == backup_initial["artifact"]["sha256"]
    assert inspection["database"]["source_unchanged"] is True
    assert inspection["inspection"]["integrity_check"] == "ok"
    assert inspection["inspection"]["foreign_key_check"] == {
        "status": "ok",
        "violations": 0,
    }
    rehearsal_source_inspection = load_json(
        run_root / "evidence" / "source-inspection.json"
    )
    upgraded_source_inspection = load_json(
        run_root / "evidence" / "upgraded-source-inspection.json"
    )
    source_sequence_values = inspection_sqlite_sequence_values(
        rehearsal_source_inspection
    )
    upgraded_sequence_values = inspection_sqlite_sequence_values(
        upgraded_source_inspection
    )
    target_sequence_values = inspection_sqlite_sequence_values(inspection)
    assert source_sequence_values == proposal_source_sequence
    assert_sqlite_sequence_floors(
        source_sequence_values,
        SQLITE_SEQUENCE_SOURCE_FLOORS,
        label=f"{name} source inspection",
        exact_tables=tuple(SQLITE_SEQUENCE_SOURCE_FLOORS),
    )
    for sequence_label, sequence_values in (
        ("upgraded source inspection", upgraded_sequence_values),
        ("target inspection", target_sequence_values),
    ):
        assert_sqlite_sequence_floors(
            sequence_values,
            source_sequence_values,
            label=f"{name} {sequence_label}",
            exact_tables=SQLITE_SEQUENCE_EXACT_DESTINATION_TABLES,
        )
    for table in SQLITE_SEQUENCE_SOURCE_FLOORS:
        assert target_sequence_values[table] >= upgraded_sequence_values[table], (
            name,
            table,
            upgraded_sequence_values[table],
            target_sequence_values[table],
        )

    structure_report_path = (
        run_root / "evidence" / "database-structure-preservation.json"
    )
    structure_report = load_json(structure_report_path)
    structure_projection = structure_report["projection"]
    assert structure_projection["format"] == (
        "ffxivshare-database-structure-preservation"
    )
    assert structure_projection["format_version"] == 1
    assert (
        "sqlite_sequence original-source and upgraded-source effective floors"
        in structure_projection["automated_coverage"]
    )
    assert structure_projection["cross_destination_schema_equal"] is True
    assert structure_projection["preserved"] is True
    assert structure_projection["issues"] == []
    reported_source_floors = {
        item["table"]: item["sequence"]
        for item in structure_projection["source"]["sequence"]
    }
    assert len(reported_source_floors) == len(
        structure_projection["source"]["sequence"]
    )
    for table in SQLITE_SEQUENCE_SOURCE_FLOORS:
        assert reported_source_floors[table] == source_sequence_values[table]
    final_sequence = structure_projection["destinations"]["final_target"]
    for destination_name, destination_values in (
        ("upgraded_source", upgraded_sequence_values),
        ("final_target", target_sequence_values),
    ):
        destination = structure_projection["destinations"][destination_name]
        assert destination["preserved"] is True
        assert destination["issues"] == []
        sequence_checks = {
            item["table"]: item for item in destination["sequence_checks"]
        }
        assert len(sequence_checks) == len(destination["sequence_checks"])
        upgraded_floors = (
            upgraded_sequence_values if destination_name == "final_target" else None
        )
        sequence_scope = destination["sequence_scope"]
        floor_tables = set(source_sequence_values)
        if upgraded_floors is None:
            expected_tables = floor_tables
            assert sequence_scope == {
                "mode": "all_original_source_entries",
                "declared_tables": None,
                "checked_tables": sorted(expected_tables),
                "observed_excluded_entries": [],
            }
        else:
            floor_tables.update(upgraded_floors)
            assert set(sequence_scope) == {
                "mode",
                "declared_tables",
                "checked_tables",
                "observed_excluded_entries",
            }
            assert sequence_scope["mode"] == (
                "v3_direct_portable_entity_tables"
            )
            declared_tables = sequence_scope["declared_tables"]
            assert declared_tables == sorted(set(declared_tables))
            declared_keys = set(declared_tables)
            expected_tables = {
                table for table in floor_tables if table in declared_keys
            }
            assert sequence_scope["checked_tables"] == sorted(expected_tables)
            excluded_sequence_items = sequence_scope[
                "observed_excluded_entries"
            ]
            assert all(
                set(item) == {
                    "table",
                    "original_source_value",
                    "upgraded_source_value",
                    "destination_value",
                    "reason",
                }
                for item in excluded_sequence_items
            )
            excluded_entries = {
                item["table"]: item
                for item in excluded_sequence_items
            }
            assert len(excluded_entries) == len(excluded_sequence_items)
            observed_tables = floor_tables | set(destination_values)
            expected_excluded = {
                table
                for table in observed_tables
                if table not in declared_keys
            }
            assert set(excluded_entries) == expected_excluded
            for table in sorted(expected_excluded):
                excluded = excluded_entries[table]
                assert excluded["original_source_value"] == (
                    source_sequence_values.get(table)
                )
                assert excluded["upgraded_source_value"] == (
                    upgraded_sequence_values.get(table)
                )
                assert excluded["destination_value"] == (
                    destination_values.get(table)
                )
                assert isinstance(excluded["reason"], str) and excluded["reason"]
        assert set(sequence_checks) == expected_tables
        for table in sorted(expected_tables):
            original_source_floor = source_sequence_values.get(table)
            upgraded_source_floor = (
                None if upgraded_floors is None else upgraded_floors.get(table)
            )
            effective_floor = max(
                floor
                for floor in (original_source_floor, upgraded_source_floor)
                if floor is not None
            )
            assert sequence_checks[table] == {
                "table": table,
                "original_source_floor": original_source_floor,
                "upgraded_source_floor": upgraded_source_floor,
                "effective_floor": effective_floor,
                "destination_value": destination_values[table],
                "preserved": True,
            }

    media_comparison = load_json(run_root / "evidence" / "media-comparison.json")
    final_media_comparison = load_json(
        run_root / "evidence" / "media-comparison-final.json"
    )
    for comparison in (media_comparison, final_media_comparison):
        assert comparison["matched"] is True
        assert comparison["missing_paths"] == []
        assert comparison["unexpected_paths"] == []
        assert comparison["changed_paths"] == []
    assert tree_snapshot(target_media_root) == target_media_before

    source_export_manifest = load_json(
        run_root / "artifacts" / "source-export" / "manifest.json"
    )
    final_export_manifest = load_json(
        run_root / "artifacts" / "final-target-export" / "manifest.json"
    )
    assert_entity_counts(source_export_manifest)
    assert_entity_counts(final_export_manifest)
    assert source_export_manifest["entities"] == final_export_manifest["entities"]
    portable_sequence_tables = {
        metadata["table"]
        for metadata in source_export_manifest["identity"]["sequences"].values()
    }
    assert portable_sequence_tables == set(
        final_sequence["sequence_scope"]["declared_tables"]
    )
    assert portable_sequence_tables == set(
        final_sequence["sequence_scope"]["checked_tables"]
    )
    assert portable_sequence_tables == {
        item["table"] for item in final_sequence["sequence_checks"]
    }
    assert portable_sequence_tables == {
        metadata["table"]
        for metadata in final_export_manifest["identity"]["sequences"].values()
    }
    target_database = run_root / "target" / "ffxivshare.sqlite3"
    assert_legacy_table_snapshot_preserved(target_database, legacy_snapshot)
    assert_expected_migration_outcome(target_database)
    final_state = load_json(
        run_root / "evidence" / "final-target-migration-state.json"
    )
    assert set(PENDING_MIGRATIONS).issubset(
        {tuple(node) for node in final_state["applied"]}
    )

    final_target_manifest = load_json(
        run_root / "artifacts" / "target-media-manifest-final.json"
    )
    assert final_target_manifest["file_count"] == 1
    assert final_target_manifest["source_snapshot"]["id"] == media_snapshot_id
    events = read_ledger(run_root)
    assert events[0]["stage"] == "created"
    assert events[1]["stage"] == "runtime_fingerprint_initial_verified"
    stage_events = {event["stage"]: event for event in events}
    structure_event = stage_events["database_structure_preserved"]
    assert structure_event["outcome"] == "passed"
    assert (
        assert_artifact_reference(
            run_root,
            structure_event["details"]["artifact"],
        )
        == structure_report_path
    )
    initial_runtime = stage_events["runtime_fingerprint_initial_verified"]
    pre_migrate_runtime = stage_events[
        "runtime_fingerprint_pre_migrate_verified"
    ]
    post_migrate_runtime = stage_events[
        "runtime_fingerprint_post_migrate_verified"
    ]
    final_runtime = stage_events["runtime_fingerprint_final_verified"]
    assert initial_runtime["details"]["content_rehashed"] is True
    assert final_runtime["details"]["content_rehashed"] is True
    assert pre_migrate_runtime["details"]["content_rehashed"] is False
    assert post_migrate_runtime["details"]["content_rehashed"] is False
    runtime_fingerprint = initial_runtime["details"][
        "runtime_fingerprint_sha256"
    ]
    assert final_runtime["details"]["runtime_fingerprint_sha256"] == runtime_fingerprint
    assert (
        pre_migrate_runtime["details"]["runtime_fingerprint_sha256"]
        == runtime_fingerprint
    )
    assert (
        post_migrate_runtime["details"]["runtime_fingerprint_sha256"]
        == runtime_fingerprint
    )
    runtime_report = load_json(
        run_root / "evidence" / "runtime-fingerprint-initial.json"
    )
    python_projection = runtime_report["projection"]["python"]
    projected_sys_paths = {
        item["path"]
        for item in python_projection["sys_path"]
        if item["exists"] is True
    }
    excluded_base_sites = python_projection["base_runtime_closure"][
        "excluded_inactive_site_package_roots"
    ]
    active_venv_sites = runtime_report["projection"]["site_packages"]["roots"]
    identity_paths = {
        item["path"]
        for item in runtime_report["checkpoint"]["identity_inventory"]
    }
    assert excluded_base_sites
    assert active_venv_sites
    assert all(path.startswith("$BASE_PREFIX/") for path in excluded_base_sites)
    assert all(
        path == "$PREFIX" or path.startswith("$PREFIX/")
        for path in active_venv_sites
    )
    assert all(path in projected_sys_paths for path in active_venv_sites)
    assert all(
        any(item.startswith(f"{path}/") for item in identity_paths)
        for path in active_venv_sites
    )
    for path in excluded_base_sites:
        assert not any(
            item == path or item.startswith(f"{path}/")
            for item in projected_sys_paths
        )
        assert not any(
            item == path or item.startswith(f"{path}/")
            for item in identity_paths
        )
    stage_sequence = [event["stage"] for event in events]
    assert stage_sequence.index("runtime_fingerprint_initial_verified") < (
        stage_sequence.index("approved_policy_evidence_verified")
    )
    assert stage_sequence.index("approved_policy_evidence_verified") < (
        stage_sequence.index("external_handoff_preflight_verified")
    )
    assert stage_sequence.index("runtime_fingerprint_pre_migrate_verified") < (
        stage_sequence.index("runtime_fingerprint_post_migrate_verified")
    )
    assert stage_sequence.index("target_snapshot_set_final_verified") < (
        stage_sequence.index("runtime_fingerprint_final_verified")
    )
    assert stage_sequence.index("runtime_fingerprint_final_verified") < (
        stage_sequence.index("external_handoff_final_verified")
    )
    assert stage_sequence.index("external_handoff_final_verified") < (
        stage_sequence.index("deployment_candidate_verified")
    )
    active_slot = "first" if name == "approved-rehearsal-one" else "second"
    for phase in ("preflight", "final"):
        report = load_json(
            run_root / "evidence" / f"external-handoff-{phase}.json"
        )
        assert report["handoff_sha256"] == file_hash(frozen_handoff)
        event = stage_events[f"external_handoff_{phase}_verified"]
        assert event["details"]["active_target_slot"] == active_slot
        assert event["details"]["handoff_sha256"] == file_hash(frozen_handoff)
    assert events[-1]["stage"] == "completed"
    assert events[-1]["outcome"] == "terminal"
    assert events[-1]["details"]["cutover_authorized"] is False
    candidates = [
        event for event in events if event["stage"] == "deployment_candidate_verified"
    ]
    assert len(candidates) == 1
    candidate = candidates[0]["details"]
    assert candidate["cutover_authorized"] is False
    assert candidate["target_media_directory_rescanned"] is True
    assert candidate["backup_sha256"] == backup_initial["artifact"]["sha256"]
    assert candidate["backup_set"]["database"]["sha256"] == candidate["backup_sha256"]
    assert_artifact_reference(run_root, candidate["snapshot_inspection"])
    assert candidate["database_structure_preservation"] == structure_event[
        "details"
    ]["artifact"]
    assert (
        assert_artifact_reference(
            run_root,
            candidate["database_structure_preservation"],
        )
        == structure_report_path
    )
    assert_artifact_reference(run_root, candidate["final_site_data_comparison"])
    assert_artifact_reference(run_root, candidate["final_restriction_preflight"])
    assert_artifact_reference(run_root, candidate["target_media_final_comparison"])
    final_backup_database = assert_artifact_reference(
        run_root,
        candidate["backup_set"]["database"],
    )
    final_backup_before = protected_snapshot(final_backup_database)
    assert_no_sidecars(final_backup_database)
    final_backup_sequence_values = readonly_sqlite_sequence_values(
        final_backup_database
    )
    assert_sqlite_sequence_floors(
        final_backup_sequence_values,
        source_sequence_values,
        label=f"{name} final backup read-only inspection",
        exact_tables=SQLITE_SEQUENCE_EXACT_DESTINATION_TABLES,
    )
    for table in SQLITE_SEQUENCE_SOURCE_FLOORS:
        assert final_backup_sequence_values[table] == target_sequence_values[table]
    assert protected_snapshot(final_backup_database) == final_backup_before
    assert_no_sidecars(final_backup_database)

    assert {path: protected_snapshot(path) for path in protected_paths} == protected_before
    assert tree_snapshot(source_media_root) == source_media_tree_before
    assert {
        path: protected_snapshot(path)
        for path in (proposal_path, review_path, policy_path)
    } == approval_before_rehearsals
    assert_no_sidecars(source_database)
    assert_no_sidecars(source_backup)
    assert applied_migrations(source_database) == source_applied
    assert applied_migrations(source_backup) == source_applied

    summary = {
        "entity_inventory_sha256": bootstrap._canonical_json_sha256(
            final_export_manifest["entities"]
        ),
        "media_inventory_sha256": bootstrap._canonical_json_sha256(
            final_target_manifest["files"]
        ),
        "applied_migrations_sha256": bootstrap._canonical_json_sha256(
            final_state["applied"]
        ),
    }
    print(
        f"Approved rehearsal {name} completed: "
        f"entities={summary['entity_inventory_sha256']}; "
        f"media={summary['media_inventory_sha256']}; "
        f"migrations={summary['applied_migrations_sha256']}"
    )
    return summary


stage_started_at = time.perf_counter()
first_summary = run_approved_rehearsal(
    "approved-rehearsal-one",
    "production-copy-e2e-target-media-one",
)
stage_timings["approved_rehearsal_one"] = time.perf_counter() - stage_started_at
stage_started_at = time.perf_counter()
second_summary = run_approved_rehearsal(
    "approved-rehearsal-two",
    "production-copy-e2e-target-media-two",
)
stage_timings["approved_rehearsal_two"] = time.perf_counter() - stage_started_at
assert first_summary == second_summary

stage_started_at = time.perf_counter()
first_run_root = runs / "approved-rehearsal-one"
second_run_root = runs / "approved-rehearsal-two"
pair_verifier = (
    first_run_root
    / "code"
    / "ops"
    / "migration"
    / "Verify-ProductionCopyRehearsalPair.py"
)
pair_verification = pair_verification_directory / "pair-verification.json"
run_command(
    [
        str(Path(sys.executable).resolve()),
        "-I",
        "-S",
        "-B",
        "-X",
        "utf8",
        str(pair_verifier),
        "--first-run-root",
        str(first_run_root),
        "--second-run-root",
        str(second_run_root),
        "--proposal-run-root",
        str(proposal_root),
        "--policy",
        str(policy_path),
        "--proposal",
        str(proposal_path),
        "--review",
        str(review_path),
        "--expected-policy-sha256",
        file_hash(policy_path),
        "--expected-proposal-sha256",
        proposal_sha256,
        "--expected-review-sha256",
        review_sha256,
        "--output",
        str(pair_verification),
    ],
    label="frozen two-rehearsal pair verification",
    cwd=first_run_root / "code",
    env=approval_environment,
)
pair_report = load_json(pair_verification)
assert pair_report["format"] == (
    "ffxivshare-production-copy-rehearsal-pair-verification"
)
assert pair_report["format_version"] == 1
assert pair_report["status"] == "verified"
assert set(pair_report["runs"]) == {"first", "second"}
pair_authority = pair_report["authority"]
assert pair_authority["policy_sha256"] == file_hash(policy_path)
assert pair_authority["proposal_sha256"] == proposal_sha256
assert pair_authority["review_sha256"] == review_sha256
assert pair_authority["policy_id"] == policy["policy_id"]
assert pair_authority["proposal_id"] == policy["proposal_id"]
assert pair_authority["review_id"] == policy["review_id"]
assert pair_report["runs"]["first"]["target_slot"] == "first"
assert pair_report["runs"]["second"]["target_slot"] == "second"
assert (
    pair_report["runs"]["first"]["bootstrap_nonce"]
    != pair_report["runs"]["second"]["bootstrap_nonce"]
)
assert (
    pair_report["runs"]["first"]["target_media_root"]
    != pair_report["runs"]["second"]["target_media_root"]
)
assert (
    pair_report["runs"]["first"]["semantic_projection_sha256"]
    == pair_report["runs"]["second"]["semantic_projection_sha256"]
    == pair_report["comparison"]["semantic_projection_sha256"]
)
first_business = pair_report["runs"]["first"]["business_summary"]
second_business = pair_report["runs"]["second"]["business_summary"]
assert first_business == second_business
matched_projections = pair_report["comparison"]["matched_projections"]
assert matched_projections["source_sqlite_schema_sha256"] == pair_authority[
    "source_sqlite_schema_sha256"
]
assert matched_projections["database_structure_preservation_sha256"] == (
    pair_authority["database_structure_preservation_sha256"]
)
for name in (
    "entity_inventory_sha256",
    "media_inventory_sha256",
    "applied_migrations_sha256",
    "target_backup_semantics_sha256",
):
    assert matched_projections[name] == first_business[name]
assert pair_report["comparison"]["matched"] is True
assert pair_report["comparison"]["issues"] == []
assert pair_report["comparison"]["unexplained_differences"] == []
assert set(pair_report["comparison"]["allowed_difference_values"]) == set(
    pair_report["comparison"]["allowed_differences"]
)
assert pair_report["verification"] == "self_consistent_local_chain"
assert pair_report["tamper_proof"] is False
assert pair_report["contains_production_user_data"] is True
assert pair_report["retained_on_success"] is True
assert pair_report["secure_disposal_required"] is True
assert pair_report["sensitive_retention_scope"] == (
    "pair_report_and_referenced_run_roots"
)
live_handoff = pair_report["live_handoff_final_verification"]
assert live_handoff["content_reverified"] is True
assert live_handoff["access_baseline_matches_approved_handoff"] is True
assert (
    live_handoff["access_baseline_sha256"]
    == pair_authority["live_handoff_access_baseline_sha256"]
)
assert pair_report["cutover_authorized"] is False
assert {path: protected_snapshot(path) for path in protected_paths} == protected_before
assert tree_snapshot(source_media_root) == source_media_tree_before
assert {
    path: protected_snapshot(path)
    for path in (proposal_path, review_path, policy_path)
} == approval_before_rehearsals
stage_timings["pair_verifier"] = time.perf_counter() - stage_started_at
timing_summary = {
    "diagnostic_only": True,
    "format": "ffxivshare-production-copy-e2e-timing",
    "format_version": 1,
    "stages_seconds": {
        name: round(seconds, 6)
        for name, seconds in stage_timings.items()
    },
    "total_seconds": round(time.perf_counter() - e2e_started_at, 6),
}
print(
    "PRODUCTION_COPY_E2E_TIMING_JSON="
    + json.dumps(timing_summary, sort_keys=True, separators=(",", ":"))
)
print(
    "Production-copy E2E passed: Proposal -> record-review -> approve -> "
    "two independent approved rehearsals; critical semantic digests match."
)
'@

$scriptExitCode = 1
$completedSuccessfully = $false
try {
    Set-Content -LiteralPath $fixtureScript -Value $fixtureSource -Encoding UTF8
    & $PythonExecutable `
        -I -S -B -X utf8 `
        $fixtureScript `
        $bootstrap `
        $RepositoryRoot `
        $temporaryRoot
    $pythonExitCode = $LASTEXITCODE
    $global:LASTEXITCODE = 0
    Assert-Contract `
        -Condition ($pythonExitCode -eq 0) `
        -Message "Production-copy E2E failed with exit code $pythonExitCode."
    $completedSuccessfully = $true
    $scriptExitCode = 0
}
catch {
    [Console]::Error.WriteLine(
        "Production-copy E2E failed: {0}",
        $_.Exception.Message
    )
    [Console]::Error.WriteLine(
        "Failure evidence retained at the unique test root: {0}",
        $temporaryRoot
    )
    $scriptExitCode = 1
}
finally {
    if ($completedSuccessfully) {
        try {
            Remove-UniqueTestRoot `
                -Path $temporaryRoot `
                -ExpectedParent $RunParent
            Write-Output 'Unique production-copy E2E test root cleaned after success.'
        }
        catch {
            [Console]::Error.WriteLine(
                "Production-copy E2E cleanup failed; inspect the unique root: {0}",
                $_.Exception.Message
            )
            $scriptExitCode = 1
        }
    }
}

exit $scriptExitCode
