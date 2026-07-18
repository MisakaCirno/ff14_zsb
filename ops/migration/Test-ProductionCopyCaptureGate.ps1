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
        -Message 'Refusing to clean a capture-gate test root outside its exact parent.'
    Assert-Contract `
        -Condition ((Split-Path -Leaf $resolved) -match '^ffxivshare-capture-gate-[a-f0-9]{32}$') `
        -Message 'Refusing to clean a capture-gate test root with an unexpected name.'
    if (Test-Path -LiteralPath $resolved) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    $RepositoryRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
$RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path

if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $PythonExecutable = Join-Path $RepositoryRoot 'venv\Scripts\python.exe'
}
$PythonExecutable = (Resolve-Path -LiteralPath $PythonExecutable).Path

if ($env:OS -ne 'Windows_NT') {
    throw 'Production-copy capture-gate contracts require Windows NTFS and DACL APIs.'
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

$temporaryRoot = Join-Path `
    $RunParent `
    ('ffxivshare-capture-gate-' + [Guid]::NewGuid().ToString('N'))
[System.IO.Directory]::CreateDirectory($temporaryRoot) | Out-Null
$fixtureScript = Join-Path $temporaryRoot 'test_capture_gate.py'

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
from typing import Any


repository = Path(sys.argv[1]).resolve()
test_root = Path(sys.argv[2]).resolve()
gate_source = repository / "ops" / "migration" / "ProductionCopyCaptureGate.py"
handoff_source = repository / "ops" / "migration" / "ProductionCopyHandoff.py"
verifier_source = repository / "ops" / "migration" / "Verify-SQLiteBackupSet.py"
backup_source = repository / "shares" / "services" / "database_backup.py"


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


handoff = load_module("ffxivshare_capture_gate_test_handoff", handoff_source)
gate_module = load_module("ffxivshare_capture_gate_test_module", gate_source)
assert sys.flags.isolated
assert sys.flags.no_site
assert sys.flags.dont_write_bytecode
assert sys.flags.utf8_mode

for unsafe_path in (
    "relative\\capture",
    "C:drive-relative",
    "C:\\capture\\..\\escape",
    "\\\\server\\share\\capture",
    "\\\\?\\C:\\capture",
    "C:\\capture:stream",
):
    try:
        gate_module._raw_windows_path(unsafe_path, label="test path")
    except gate_module.CaptureGateError:
        pass
    else:
        raise AssertionError(f"Unsafe Windows path was accepted: {unsafe_path}")


class FakeKernel:
    def __init__(self, drive_type: int) -> None:
        self.drive_type = drive_type

    def GetDriveTypeW(self, _root: str) -> int:
        return self.drive_type


class FakeApi:
    def __init__(self, drive_type: int) -> None:
        self.kernel32 = FakeKernel(drive_type)

    def validate_volume(self, _path: str) -> None:
        pass


class FakeCore:
    DRIVE_FIXED = 3


gate_module._assert_fixed_ntfs(
    FakeApi(3), FakeCore(), r"C:\Capture", label="fixed test"
)
for rejected_drive_type in (0, 1, 2, 4, 5, 6):
    try:
        gate_module._assert_fixed_ntfs(
            FakeApi(rejected_drive_type),
            FakeCore(),
            r"C:\Capture",
            label="non-fixed test",
        )
    except gate_module.CaptureGateError:
        pass
    else:
        raise AssertionError(f"Non-fixed drive type was accepted: {rejected_drive_type}")

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
advapi32.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
advapi32.SetFileSecurityW.restype = wintypes.BOOL


def set_dacl(path: Path, sddl: str) -> None:
    descriptor = ctypes.c_void_p()
    size = wintypes.DWORD()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), ctypes.byref(size)
    ):
        raise OSError(ctypes.get_last_error(), "Cannot build test security descriptor")
    try:
        if not advapi32.SetFileSecurityW(str(path), 0x00000004, descriptor):
            raise OSError(ctypes.get_last_error(), f"Cannot apply test DACL: {path}")
    finally:
        kernel32.LocalFree(descriptor)


current_sid = handoff._Win32Api().current_user_sid
private_sddl = (
    "D:P"
    f"(A;OICI;FA;;;{current_sid})"
    "(A;OICI;FA;;;S-1-5-18)"
    "(A;OICI;FA;;;S-1-5-32-544)"
)
controlled_sddl = (
    "D:P"
    f"(A;;GRGX;;;{current_sid})"
    "(A;;FA;;;S-1-5-18)"
    "(A;;FA;;;S-1-5-32-544)"
)
unsafe_output_sddl = (
    "D:P"
    f"(A;OICI;FA;;;{current_sid})"
    "(A;OICI;FA;;;S-1-5-18)"
    "(A;OICI;FA;;;S-1-5-32-544)"
    "(A;OICI;GW;;;S-1-1-0)"
)


def file_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def apply_tree_dacl(path: Path, sddl: str) -> None:
    rows = [path]
    if path.is_dir():
        rows.extend(sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True))
    for item in rows:
        set_dacl(item, sddl)


def new_tool_directory(name: str) -> Path:
    root = test_root / name
    root.mkdir()
    shutil.copy2(gate_source, root / "ProductionCopyCaptureGate.py")
    shutil.copy2(handoff_source, root / "ProductionCopyHandoff.py")
    shutil.copy2(verifier_source, root / "Verify-SQLiteBackupSet.py")
    shutil.copy2(backup_source, root / "database_backup.py")
    apply_tree_dacl(root, controlled_sddl)
    return root


def new_capture(name: str, *, unsafe_database_acl: bool = False) -> tuple[Path, Path, Path]:
    root = test_root / name
    database = root / "Database"
    audit = root / "Audit"
    database.mkdir(parents=True)
    audit.mkdir()
    set_dacl(root, private_sddl)
    set_dacl(database, unsafe_output_sddl if unsafe_database_acl else private_sddl)
    set_dacl(audit, private_sddl)
    return root, database, audit


def run_process(arguments: list[str], *, expected: int) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(arguments, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != expected:
        raise AssertionError(
            f"exit={result.returncode}, expected={expected}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def gate_arguments(tool_root: Path, capture_root: Path) -> list[str]:
    gate = tool_root / "ProductionCopyCaptureGate.py"
    handoff_tool = tool_root / "ProductionCopyHandoff.py"
    verifier = tool_root / "Verify-SQLiteBackupSet.py"
    backup = tool_root / "database_backup.py"
    return [
        str(Path(sys.executable).resolve()),
        "-I", "-S", "-B", "-X", "utf8",
        str(gate),
        "preflight",
        "--expected-gate-sha256", file_hash(gate),
        "--handoff-core", str(handoff_tool),
        "--expected-handoff-core-sha256", file_hash(handoff_tool),
        "--backup-verifier", str(verifier),
        "--expected-backup-verifier-sha256", file_hash(verifier),
        "--backup-tool", str(backup),
        "--expected-backup-tool-sha256", file_hash(backup),
        "--production-repository-root", str(production_root),
        "--source-database", str(source_database),
        "--output-database", str(capture_root / "Database" / "production.sqlite3"),
        "--application-version", application_version,
        "--output-report", str(capture_root / "Audit" / "capture-preflight.json"),
        "--confirm-dedicated-new-empty-output-directory",
    ]


def run_preflight(tool_root: Path, capture_root: Path, *, expected: int = 0):
    return run_process(gate_arguments(tool_root, capture_root), expected=expected)


def run_capture(tool_root: Path, capture_root: Path, *, expected: int = 0):
    preflight = capture_root / "Audit" / "capture-preflight.json"
    return run_process(
        [
            str(Path(sys.executable).resolve()),
            "-I", "-S", "-B", "-X", "utf8",
            str(tool_root / "ProductionCopyCaptureGate.py"),
            "capture",
            "--expected-gate-sha256",
            file_hash(tool_root / "ProductionCopyCaptureGate.py"),
            "--expected-handoff-core-sha256",
            file_hash(tool_root / "ProductionCopyHandoff.py"),
            "--expected-backup-verifier-sha256",
            file_hash(tool_root / "Verify-SQLiteBackupSet.py"),
            "--expected-backup-tool-sha256",
            file_hash(tool_root / "database_backup.py"),
            "--preflight-report", str(preflight),
            "--expected-preflight-sha256", file_hash(preflight),
            "--output-report", str(capture_root / "Audit" / "capture-final.json"),
        ],
        expected=expected,
    )


def assert_canonical_json(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    value = json.loads(payload.decode("utf-8"), object_pairs_hook=dict)
    expected = (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    assert payload == expected
    return value


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


set_dacl(test_root, private_sddl)
production_root = test_root / "Production"
production_root.mkdir()
set_dacl(production_root, private_sddl)
source_database = production_root / "db.sqlite3"
application_version = "244c32734e9fab5af05bf544a654615eeab31404"
connection = sqlite3.connect(source_database)
connection.execute("PRAGMA journal_mode=WAL")
connection.execute("CREATE TABLE capture_fixture(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
connection.execute("INSERT INTO capture_fixture(value) VALUES ('committed-in-wal')")
connection.commit()

try:
    tool_root = new_tool_directory("Tools")
    tool_hashes_before = {
        path.name: file_hash(path) for path in tool_root.iterdir() if path.is_file()
    }
    source_bytes_before = source_database.read_bytes()
    wal_path = Path(str(source_database) + "-wal")
    wal_bytes_before = wal_path.read_bytes()

    capture_root, database_dir, audit_dir = new_capture("CaptureSuccess")
    preflight_result = run_preflight(tool_root, capture_root)
    preflight_stdout = json.loads(preflight_result.stdout)
    assert preflight_stdout["ready_for_capture"] is True
    assert preflight_stdout["cutover_authorized"] is False
    assert list(database_dir.iterdir()) == []
    preflight = audit_dir / "capture-preflight.json"
    preflight_value = assert_canonical_json(preflight)
    assert preflight_value["ready_for_capture"] is True
    assert preflight_value["capture_set_complete"] is False
    assert preflight_value["cutover_authorized"] is False

    capture_result = run_capture(tool_root, capture_root)
    assert sorted(path.name for path in database_dir.iterdir()) == [
        "production.sqlite3",
        "production.sqlite3.metadata.json",
        "production.sqlite3.sha256",
    ]
    capture_stdout = json.loads(capture_result.stdout)
    assert capture_stdout["capture_set_complete"] is True
    assert capture_stdout["backup_set_contract_verified"] is True
    assert capture_stdout["cutover_authorized"] is False
    final_value = assert_canonical_json(audit_dir / "capture-final.json")
    assert final_value["phase"] == "capture"
    assert final_value["capture_set_complete"] is True
    assert final_value["backup_set_contract_verified"] is True
    assert final_value["cutover_authorized"] is False
    assert final_value["database_backup_set"]["execution"]["method"] == (
        "verified_backup_sqlite_path_in_process"
    )
    assert final_value["limitations"]["sqlite_pragmas_independently_rechecked"] is False
    assert final_value["limitations"]["source_database_content_stability_proven"] is False
    assert final_value["observations"]["runtime"] == preflight_value["observations"][
        "runtime"
    ]
    assert final_value["database_backup_set"]["execution"]["result"]["sha256"] == (
        final_value["database_backup_set"]["content"]["database"]["sha256"]
    )
    assert final_value["database_backup_set"]["execution"]["result"]["size"] == (
        final_value["database_backup_set"]["content"]["database"]["size"]
    )
    with sqlite3.connect(database_dir / "production.sqlite3") as backup_connection:
        assert backup_connection.execute("SELECT value FROM capture_fixture").fetchone() == (
            "committed-in-wal",
        )
    assert source_database.read_bytes() == source_bytes_before
    assert wal_path.read_bytes() == wal_bytes_before
    assert {
        path.name: file_hash(path) for path in tool_root.iterdir() if path.is_file()
    } == tool_hashes_before
    assert not any(path.name == "__pycache__" for path in tool_root.iterdir())

    _, bad_hash_database, bad_hash_audit = new_capture("CaptureBadHash")
    bad_hash_arguments = gate_arguments(tool_root, bad_hash_database.parent)
    digest_index = bad_hash_arguments.index("--expected-backup-tool-sha256") + 1
    bad_hash_arguments[digest_index] = "0" * 64
    run_process(bad_hash_arguments, expected=1)
    assert list(bad_hash_database.iterdir()) == []
    assert list(bad_hash_audit.iterdir()) == []

    _, nonempty_database, nonempty_audit = new_capture("CaptureNonEmpty")
    sentinel = nonempty_database / ".stale.tmp"
    sentinel.write_bytes(b"preserve-partial-evidence")
    run_preflight(tool_root, nonempty_database.parent, expected=1)
    assert sentinel.read_bytes() == b"preserve-partial-evidence"
    assert list(nonempty_audit.iterdir()) == []

    _, weak_database, weak_audit = new_capture(
        "CaptureWeakAcl", unsafe_database_acl=True
    )
    run_preflight(tool_root, weak_database.parent, expected=1)
    assert list(weak_database.iterdir()) == []
    assert list(weak_audit.iterdir()) == []

    _, hardlink_database, hardlink_audit = new_capture("CaptureSourceHardlink")
    source_alias = production_root / "db-hardlink.sqlite3"
    os.link(source_database, source_alias)
    try:
        run_preflight(tool_root, hardlink_database.parent, expected=1)
    finally:
        source_alias.unlink()
    assert list(hardlink_database.iterdir()) == []
    assert list(hardlink_audit.iterdir()) == []

    malicious_tool_root = new_tool_directory("ToolsMalicious")
    marker = test_root / "malicious-import-marker.txt"
    malicious_handoff = malicious_tool_root / "ProductionCopyHandoff.py"
    apply_tree_dacl(malicious_tool_root, private_sddl)
    malicious_handoff.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
        newline="\n",
    )
    apply_tree_dacl(malicious_tool_root, controlled_sddl)
    malicious_capture, malicious_database, malicious_audit = new_capture(
        "CaptureMaliciousCore"
    )
    malicious_arguments = gate_arguments(malicious_tool_root, malicious_capture)
    trusted_handoff_index = malicious_arguments.index(
        "--expected-handoff-core-sha256"
    ) + 1
    malicious_arguments[trusted_handoff_index] = file_hash(handoff_source)
    run_process(malicious_arguments, expected=1)
    assert not marker.exists()
    assert list(malicious_database.iterdir()) == []
    assert list(malicious_audit.iterdir()) == []

    malicious_verifier_tool_root = new_tool_directory("ToolsMaliciousVerifier")
    verifier_marker = test_root / "malicious-verifier-marker.txt"
    malicious_verifier = malicious_verifier_tool_root / "Verify-SQLiteBackupSet.py"
    apply_tree_dacl(malicious_verifier_tool_root, private_sddl)
    malicious_verifier.write_text(
        "from pathlib import Path\n"
        f"Path({str(verifier_marker)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
        newline="\n",
    )
    apply_tree_dacl(malicious_verifier_tool_root, controlled_sddl)
    verifier_capture, verifier_database, verifier_audit = new_capture(
        "CaptureMaliciousVerifier"
    )
    verifier_arguments = gate_arguments(
        malicious_verifier_tool_root, verifier_capture
    )
    trusted_verifier_index = verifier_arguments.index(
        "--expected-backup-verifier-sha256"
    ) + 1
    verifier_arguments[trusted_verifier_index] = file_hash(verifier_source)
    run_process(verifier_arguments, expected=1)
    assert not verifier_marker.exists()
    assert list(verifier_database.iterdir()) == []
    assert list(verifier_audit.iterdir()) == []

    forged_capture, forged_database, forged_audit = new_capture(
        "CaptureForgedPreflight"
    )
    run_preflight(tool_root, forged_capture)
    forged_preflight = forged_audit / "capture-preflight.json"
    forged_value = json.loads(forged_preflight.read_text(encoding="utf-8"))
    forged_authority = forged_value["authority"]
    forged_authority["tooling"]["directory"] = str(malicious_tool_root)
    for row in forged_authority["tooling"]["files"]:
        if row["role"] == "handoff_core":
            row["path"] = str(malicious_handoff)
            row["sha256"] = file_hash(malicious_handoff)
            row["size"] = malicious_handoff.stat().st_size
    forged_value["authority_sha256"] = sha256(
        canonical_json_bytes(forged_authority)
    ).hexdigest()
    forged_preflight.write_bytes(canonical_json_bytes(forged_value))
    run_capture(tool_root, forged_capture, expected=1)
    assert not marker.exists()
    assert list(forged_database.iterdir()) == []
    assert sorted(path.name for path in forged_audit.iterdir()) == [
        "capture-preflight.json"
    ]

    runtime_capture, runtime_database, runtime_audit = new_capture(
        "CaptureRuntimeMismatch"
    )
    run_preflight(tool_root, runtime_capture)
    runtime_preflight = runtime_audit / "capture-preflight.json"
    runtime_value = json.loads(runtime_preflight.read_text(encoding="utf-8"))
    runtime_value["observations"]["runtime"]["python_version"] += "-different"
    runtime_preflight.write_bytes(canonical_json_bytes(runtime_value))
    run_capture(tool_root, runtime_capture, expected=1)
    assert list(runtime_database.iterdir()) == []
    assert sorted(path.name for path in runtime_audit.iterdir()) == [
        "capture-preflight.json"
    ]

    extra_capture, extra_database, extra_audit = new_capture("CaptureExtra")
    run_preflight(tool_root, extra_capture)
    extra = extra_database / "unexpected.bin"
    extra.write_bytes(b"must-be-preserved")
    run_capture(tool_root, extra_capture, expected=1)
    assert extra.read_bytes() == b"must-be-preserved"
    assert sorted(path.name for path in extra_audit.iterdir()) == [
        "capture-preflight.json"
    ]

    linked_capture, linked_database, linked_audit = new_capture(
        "CaptureHardlinkedPreflight"
    )
    run_preflight(tool_root, linked_capture)
    preflight_link = linked_audit / "capture-preflight-hardlink.json"
    os.link(linked_audit / "capture-preflight.json", preflight_link)
    run_capture(tool_root, linked_capture, expected=1)
    assert not (linked_audit / "capture-final.json").exists()
    assert list(linked_database.iterdir()) == []

    print("Production-copy capture-gate contracts passed.")
finally:
    connection.close()
    for tool_directory in (
        test_root / "Tools",
        test_root / "ToolsMalicious",
        test_root / "ToolsMaliciousVerifier",
    ):
        if tool_directory.exists():
            apply_tree_dacl(tool_directory, private_sddl)
'@

try {
    [System.IO.File]::WriteAllText(
        $fixtureScript,
        $fixtureSource,
        [System.Text.UTF8Encoding]::new($false)
    )
    & $PythonExecutable -I -S -B -X utf8 `
        $fixtureScript `
        $RepositoryRoot `
        $temporaryRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Capture-gate Python contracts failed with exit code $LASTEXITCODE."
    }
}
finally {
    Remove-UniqueTestRoot -Path $temporaryRoot -ExpectedParent $RunParent
    $global:LASTEXITCODE = 0
}
