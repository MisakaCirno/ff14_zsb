[CmdletBinding()]
param(
    [switch]$IncludeSlow,
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
        [Parameter(Mandatory = $true)][string]$ExpectedParent,
        [Parameter(Mandatory = $true)][string]$LeafPattern
    )
    $resolved = [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    $parent = [System.IO.Path]::GetFullPath($ExpectedParent).TrimEnd('\', '/')
    $actualParent = [System.IO.Path]::GetDirectoryName($resolved).TrimEnd('\', '/')
    Assert-Contract `
        -Condition ($actualParent.Equals(
            $parent,
            [System.StringComparison]::OrdinalIgnoreCase
        )) `
        -Message 'Refusing to clean a test directory outside its exact parent.'
    Assert-Contract `
        -Condition ((Split-Path -Leaf $resolved) -match $LeafPattern) `
        -Message 'Refusing to clean a test directory with an unexpected name.'
    if (Test-Path -LiteralPath $resolved) {
        if ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) {
            $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
            & icacls.exe $resolved /grant:r "${identity}:F" /T /C /Q | Out-Null
            Assert-Contract `
                -Condition ($LASTEXITCODE -eq 0) `
                -Message 'Failed to restore test-only cleanup access.'
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
        throw "Policy-proposal contract requires the project virtual environment: $venvPython"
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
        throw 'The current-user private application-data directory is unavailable.'
    }
    $RunParent = Join-Path $localApplicationData 'FFXIVShare\MigrationContractTests'
}
[System.IO.Directory]::CreateDirectory($RunParent) | Out-Null
$RunParent = (Resolve-Path -LiteralPath $RunParent).Path

$bootstrap = Join-Path $PSScriptRoot 'ProductionCopyBootstrap.py'
Assert-Contract `
    -Condition (Test-Path -LiteralPath $bootstrap -PathType Leaf) `
    -Message "Production-copy bootstrap is missing: $bootstrap"

$testId = [Guid]::NewGuid().ToString('N')
$temporaryRoot = Join-Path $RunParent ('ffxivshare-policy-proposal-' + $testId)
$systemTemp = [System.IO.Path]::GetFullPath(
    [System.IO.Path]::GetTempPath()
).TrimEnd('\', '/')
$aclProbeRoot = Join-Path $systemTemp ('ffxivshare-policy-proposal-acl-' + $testId)
$fixtureScript = Join-Path $temporaryRoot 'test_policy_proposal.py'
[System.IO.Directory]::CreateDirectory($temporaryRoot) | Out-Null

$fixtureSource = @'
from __future__ import annotations

import ast
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


bootstrap_path = Path(sys.argv[1]).resolve()
repository = Path(sys.argv[2]).resolve()
test_root = Path(sys.argv[3]).resolve()
acl_probe_root = Path(sys.argv[4]).resolve()
include_slow = sys.argv[5] == "1"

spec = importlib.util.spec_from_file_location(
    "ffxivshare_policy_proposal_bootstrap_contract",
    bootstrap_path,
)
assert spec is not None and spec.loader is not None
bootstrap = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bootstrap
spec.loader.exec_module(bootstrap)

handoff_path = repository / "ops" / "migration" / "ProductionCopyHandoff.py"
handoff_spec = importlib.util.spec_from_file_location(
    "ffxivshare_policy_proposal_handoff_contract",
    handoff_path,
)
assert handoff_spec is not None and handoff_spec.loader is not None
handoff = importlib.util.module_from_spec(handoff_spec)
sys.modules[handoff_spec.name] = handoff
handoff_spec.loader.exec_module(handoff)

assert sys.flags.isolated
assert sys.flags.no_site
assert sys.flags.no_user_site
assert sys.flags.dont_write_bytecode
assert sys.flags.utf8_mode

ENTRYPOINT = "ops/migration/Propose-ProductionCopyPolicy.py"
REQUIRED_EVIDENCE = (
    "bootstrap",
    "execution_inventory",
    "migration_plan",
    "migration_review_plan",
    "runtime_fingerprint",
    "source_backup_verification",
    "source_handoff_manifest",
    "source_media_manifest",
    "source_migration_state",
    "source_snapshot_inspection",
)
HANDOFF_SCOPE_ROLES = (
    "database_backup_set",
    "source_media_root",
    "source_media_manifest",
    "target_media_root_1",
    "target_media_root_2",
)
PENDING_NODE = ("shares", "0025_add_collection_owner_index")


def assert_final_handoff_checkpoint_order() -> None:
    tree = ast.parse((repository / ENTRYPOINT).read_text(encoding="utf-8"))
    execute = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_execute_proposal"
    )
    final_live_calls = [
        node
        for node in ast.walk(execute)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_verify_live_handoff"
        and any(
            keyword.arg == "verify_content"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        )
    ]
    final_event_calls = [
        node
        for node in ast.walk(execute)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "record"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "source_handoff_final_verified"
    ]
    assert final_live_calls
    assert len(final_event_calls) == 1
    final_live_call = max(final_live_calls, key=lambda node: node.lineno)
    final_event_call = final_event_calls[0]
    checkpoint_calls = [
        node
        for node in ast.walk(execute)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_regular_file_checkpoint"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "source_handoff_manifest"
        and {
            keyword.arg: keyword.value.id
            for keyword in node.keywords
            if keyword.arg in {"expected_sha256", "expected_identity"}
            and isinstance(keyword.value, ast.Name)
        }
        == {
            "expected_sha256": "handoff_sha256",
            "expected_identity": "handoff_identity",
        }
    ]
    assert any(
        final_live_call.lineno < checkpoint.lineno < final_event_call.lineno
        for checkpoint in checkpoint_calls
    )


assert_final_handoff_checkpoint_order()


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


def assert_no_sidecars(database: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(f"{database}{suffix}").exists(), (database, suffix)


if os.name == "nt":
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
    assert os.name == "nt"
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
            raise OSError(ctypes.get_last_error(), f"Cannot apply test DACL: {path}")
    finally:
        kernel32.LocalFree(descriptor)


def iter_tree(path: Path) -> list[Path]:
    rows = [path]
    if path.is_dir():
        rows.extend(
            sorted(path.rglob("*"), key=lambda value: len(value.parts), reverse=True)
        )
    return rows


def apply_tree_dacl(path: Path, sddl: str) -> None:
    for item in iter_tree(path):
        set_dacl(item, sddl)


def run_checked(
    argv: list[str],
    *,
    label: str,
    cwd: Path,
    env: dict[str, str] | None = None,
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
    if result.returncode != 0:
        raise AssertionError(
            f"{label} failed with exit code {result.returncode}.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def proposal_arguments(
    database: Path,
    checksum: Path,
    metadata: Path,
    media_manifest: Path,
    handoff_manifest: Path,
    *,
    policy_id: str,
    proposal_id: str,
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
        str(handoff_manifest),
        "--policy-id",
        policy_id,
        "--proposal-id",
        proposal_id,
        "--confirm-source-immutable",
    )


def config_for(run_root: Path, arguments: tuple[str, ...]) -> Any:
    return bootstrap.BootstrapConfig(
        repository_root=repository,
        python_executable=Path(sys.executable).resolve(),
        run_root=run_root,
        mode="policy-proposal",
        inner_entrypoint=ENTRYPOINT,
        inner_arguments=arguments,
    )


expected_bundle_sha256 = bootstrap._execution_bundle_sha256(
    repository,
    inner_entrypoint=ENTRYPOINT,
)


def assert_exact_frozen_bundle(run_root: Path, arguments: tuple[str, ...]) -> dict[str, Any]:
    record_path = run_root / "evidence" / "bootstrap.json"
    manifest_path = run_root / "evidence" / "execution-bundle.json"
    record = load_json(record_path)
    manifest = load_json(manifest_path)
    assert record["configuration"]["mode"] == "policy-proposal"
    assert record["configuration"]["inner_entrypoint"] == ENTRYPOINT
    assert record["configuration"]["inner_arguments"] == list(arguments)
    assert record["policy"] is None
    assert record["approval_inputs"] is None
    assert record["bootstrap_trusted_not_frozen"] is True
    assert record["source_data_read_by_bootstrap"] is False
    assert record["media_read_by_bootstrap"] is False
    bundle = record["execution_bundle"]
    assert bundle["authority"] == "stable_repository_consistency"
    assert bundle["expected_sha256"] == expected_bundle_sha256
    assert bundle["frozen_sha256"] == expected_bundle_sha256
    assert manifest["execution_bundle_sha256"] == expected_bundle_sha256
    assert bootstrap._canonical_json_sha256(manifest["files"]) == expected_bundle_sha256
    frozen_paths = {item["path"] for item in manifest["files"]}
    assert {
        "ops/migration/ProductionCopyBootstrap.py",
        "ops/migration/ProductionCopyHandoff.py",
        "ops/migration/Propose-ProductionCopyPolicy.py",
        "ops/migration/Rehearse-ProductionCopy.py",
    }.issubset(frozen_paths)
    for item in manifest["files"]:
        source = repository / Path(item["path"])
        frozen = run_root / "code" / Path(item["path"])
        assert frozen.read_bytes() == source.read_bytes(), item["path"]
        assert item["size"] == frozen.stat().st_size
        assert item["sha256"] == file_hash(frozen)
    assert not any((run_root / "code").rglob("*.pyc"))
    assert not any((run_root / "code").rglob("__pycache__"))
    return record


def assert_completion(run_root: Path, expected_exit: int) -> dict[str, Any]:
    completion = load_json(run_root / "evidence" / "completion.json")
    assert completion["inner_exit_code"] == expected_exit
    assert completion["execution_bundle_sha256"] == expected_bundle_sha256
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


runs = test_root / "runs"
runs.mkdir()
missing = test_root / "missing-inputs" / "production.sqlite3"
invalid_handoff = test_root / "missing-inputs" / "invalid-handoff.json"
invalid_handoff.parent.mkdir()
invalid_handoff.write_bytes(b"{}\n")
oversized_handoff = test_root / "missing-inputs" / "oversized-handoff.json"
with oversized_handoff.open("wb") as stream:
    stream.seek(handoff.MAX_HANDOFF_BYTES)
    stream.write(b"\0")
assert oversized_handoff.stat().st_size == handoff.MAX_HANDOFF_BYTES + 1
fast_arguments = proposal_arguments(
    missing,
    Path(f"{missing}.sha256"),
    Path(f"{missing}.metadata.json"),
    test_root / "missing-inputs" / "media-manifest.json",
    invalid_handoff,
    policy_id="proposal-contract-fast-policy",
    proposal_id="proposal-contract-fast-proposal",
)

# The real Windows ACL hook must remain fail-closed for the shared temporary
# parent used by Codex. Positive test runs use a callback scoped to their unique
# synthetic RunRoot and its approval directory only.
if os.name == "nt":
    real_acl_config = config_for(acl_probe_root, fast_arguments)
    try:
        bootstrap.run_bootstrap(real_acl_config)
    except bootstrap.BootstrapConfigurationError:
        pass
    else:
        raise AssertionError("The production Windows ACL hook did not fail closed")
    if acl_probe_root.exists():
        assert not (acl_probe_root / "code").exists()
        assert not (acl_probe_root / "evidence" / "bootstrap.json").exists()


def scoped_acl(run_root: Path):
    calls: list[Path] = []

    def secure(path: Path) -> str:
        resolved = path.resolve()
        assert resolved in {run_root.resolve(), (run_root / "approval").resolve()}
        calls.append(resolved)
        return "test_only_scoped_private_root"

    return secure, calls


fast_root = runs / "fast"
fast_secure, fast_acl_calls = scoped_acl(fast_root)
fast_outcome = bootstrap.run_bootstrap(
    config_for(fast_root, fast_arguments),
    secure_run_root=fast_secure,
)
assert fast_outcome.exit_code == 1
assert fast_acl_calls == [fast_root.resolve(), (fast_root / "approval").resolve()]
fast_record = assert_exact_frozen_bundle(fast_root, fast_arguments)
assert fast_record["workspace_access_control"] == "test_only_scoped_private_root"
assert_completion(fast_root, 1)
fast_stdout = (fast_root / "logs" / "inner.stdout.log").read_text(
    encoding="utf-8"
)
fast_stderr = (fast_root / "logs" / "inner.stderr.log").read_text(
    encoding="utf-8"
)
assert not fast_stdout.strip()
assert "source_handoff_manifest_invalid" in fast_stderr
assert "Source database " not in fast_stderr
for handshake_failure in (
    "Proposal inner requires a bootstrap record",
    "Proposal inner escaped its frozen bootstrap root",
    "Proposal bootstrap identity is invalid",
    "Proposal bootstrap authority is invalid",
    "Proposal execution inventory is invalid",
    "Proposal frozen execution bundle changed",
):
    assert handshake_failure not in fast_stderr, handshake_failure
assert not (fast_root / "evidence" / "policy-proposal.json").exists()
assert not (fast_root / "evidence" / "policy-proposal-body.json").exists()
print("Fast production-copy policy-proposal contract passed.")

# An oversized authority must be rejected from metadata alone. All DB/media
# arguments intentionally remain absent so a regression into source reads is
# also observable in the failure reason and the lack of proposal evidence.
oversized_arguments = proposal_arguments(
    missing,
    Path(f"{missing}.sha256"),
    Path(f"{missing}.metadata.json"),
    test_root / "missing-inputs" / "media-manifest.json",
    oversized_handoff,
    policy_id="proposal-contract-oversized-policy",
    proposal_id="proposal-contract-oversized-proposal",
)
oversized_root = runs / "oversized-handoff"
oversized_secure, oversized_acl_calls = scoped_acl(oversized_root)
oversized_outcome = bootstrap.run_bootstrap(
    config_for(oversized_root, oversized_arguments),
    secure_run_root=oversized_secure,
)
assert oversized_outcome.exit_code == 1
assert oversized_acl_calls == [
    oversized_root.resolve(),
    (oversized_root / "approval").resolve(),
]
assert_exact_frozen_bundle(oversized_root, oversized_arguments)
assert_completion(oversized_root, 1)
oversized_stderr = (
    oversized_root / "logs" / "inner.stderr.log"
).read_text(encoding="utf-8")
assert "source_handoff_manifest_too_large" in oversized_stderr
assert "Source database " not in oversized_stderr
assert "Source media " not in oversized_stderr
assert not (oversized_root / "evidence" / "policy-proposal.json").exists()
assert not (oversized_root / "evidence" / "policy-proposal-body.json").exists()
print("Oversized handoff fail-fast proposal contract passed.")

# argparse must keep the handoff authority mandatory, independently of the
# trust-before-read case above where an invalid authority is supplied.
handoff_flag = fast_arguments.index("--source-handoff-manifest")
missing_handoff_arguments = (
    fast_arguments[:handoff_flag] + fast_arguments[handoff_flag + 2 :]
)
missing_handoff_root = runs / "missing-handoff-parameter"
missing_handoff_secure, missing_handoff_acl_calls = scoped_acl(missing_handoff_root)
missing_handoff_outcome = bootstrap.run_bootstrap(
    config_for(missing_handoff_root, missing_handoff_arguments),
    secure_run_root=missing_handoff_secure,
)
assert missing_handoff_outcome.exit_code == 2
assert missing_handoff_acl_calls == [
    missing_handoff_root.resolve(),
    (missing_handoff_root / "approval").resolve(),
]
assert_exact_frozen_bundle(missing_handoff_root, missing_handoff_arguments)
assert_completion(missing_handoff_root, 2)
missing_handoff_stderr = (
    missing_handoff_root / "logs" / "inner.stderr.log"
).read_text(encoding="utf-8")
assert "--source-handoff-manifest" in missing_handoff_stderr
assert "required" in missing_handoff_stderr
assert not (missing_handoff_root / "evidence" / "policy-proposal.json").exists()
assert not (missing_handoff_root / "evidence" / "policy-proposal-body.json").exists()
print("Required source-handoff proposal parameter contract passed.")


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
            "APP_VERSION": "proposal-contract",
            "FFXIVSHARE_ENV_FILE": str(env_file),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
        }
    )
    return env


def applied_migrations(database: Path) -> set[tuple[str, str]]:
    uri = database.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        return {
            (str(app), str(name))
            for app, name in connection.execute(
                "SELECT app, name FROM django_migrations ORDER BY app, name"
            )
        }
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


def verify_ledger(run_root: Path, proposal: dict[str, Any]) -> list[dict[str, Any]]:
    ledger_path = assert_artifact_reference(run_root, proposal["ledger"]["artifact"])
    raw = ledger_path.read_bytes()
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
    assert proposal["ledger"]["event_count"] == len(events)
    assert proposal["ledger"]["head_event_sha256"] == previous
    assert proposal["ledger"]["terminal_status"] == "review_required"
    terminal = events[-1]
    assert terminal["stage"] == "review_required"
    assert terminal["outcome"] == "terminal"
    details = terminal["details"]
    assert details["status"] == "review_required"
    assert details["lossless_reviewed"] is False
    assert details["migration_applied"] is False
    assert details["cutover_authorized"] is False
    assert details["contains_production_user_data"] is True
    assert details["retained_on_success"] is True
    assert details["secure_disposal_required"] is True
    assert details["sensitive_retention_scope"] == "entire_run_root"

    def single(stage: str) -> tuple[int, dict[str, Any]]:
        matches = [
            (index, event)
            for index, event in enumerate(events)
            if event["stage"] == stage
        ]
        assert len(matches) == 1, stage
        return matches[0]

    initial_index, initial = single("source_handoff_verified")
    body_index, _body_event = single("policy_proposal_body_created")
    final_index, final = single("source_handoff_final_verified")
    source_index, _source_event = single("source_final_verified")
    bundle_index, _bundle_event = single("execution_bundle_final_verified")
    terminal_index = len(events) - 1
    assert initial_index < body_index < final_index < source_index < bundle_index < terminal_index
    handoff_reference = proposal["body"]["evidence"]["source_handoff_manifest"]
    access_snapshot_sha256 = load_json(
        assert_artifact_reference(run_root, handoff_reference)
    )["access_baseline"]["snapshot_sha256"]
    assert initial["outcome"] == "passed"
    assert set(initial["details"]) == {
        "artifact",
        "access_snapshot_sha256",
        "scope_roles",
    }
    assert initial["details"] == {
        "artifact": handoff_reference,
        "access_snapshot_sha256": access_snapshot_sha256,
        "scope_roles": list(HANDOFF_SCOPE_ROLES),
    }
    assert final["outcome"] == "passed"
    assert set(final["details"]) == {
        "artifact",
        "access_snapshot_sha256",
        "scope_roles",
        "content_verified",
    }
    assert final["details"] == {
        "artifact": handoff_reference,
        "access_snapshot_sha256": access_snapshot_sha256,
        "scope_roles": list(HANDOFF_SCOPE_ROLES),
        "content_verified": True,
    }
    return events


if include_slow:
    if os.name != "nt":
        raise AssertionError(
            "Slow proposal contract requires Windows NTFS/DACL handoff verification."
        )
    slow_fixture = test_root / "slow-fixture"
    source_directory = slow_fixture / "source"
    backup_directory = slow_fixture / "backup"
    offline_media = slow_fixture / "offline-media"
    manifest_directory = slow_fixture / "manifest"
    target_one = slow_fixture / "target-one"
    target_two = slow_fixture / "target-two"
    handoff_directory = slow_fixture / "handoff"
    for directory in (
        source_directory,
        backup_directory,
        offline_media,
        manifest_directory,
        handoff_directory,
    ):
        directory.mkdir(parents=True)
    current_sid = handoff._Win32Api().current_user_sid
    private_sddl = (
        "D:P"
        f"(A;OICI;FA;;;{current_sid})"
        "(A;OICI;FA;;;S-1-5-18)"
        "(A;OICI;FA;;;S-1-5-32-544)"
    )
    sealed_sddl = (
        "D:P"
        f"(A;;GRGX;;;{current_sid})"
        "(A;;FA;;;S-1-5-18)"
        "(A;;FA;;;S-1-5-32-544)"
    )
    set_dacl(slow_fixture, private_sddl)
    set_dacl(handoff_directory, private_sddl)
    env_file = slow_fixture / "empty.env"
    env_file.write_bytes(b"")
    source_database = source_directory / "source.sqlite3"
    setup_env = django_environment(source_database, offline_media, env_file)
    manage = [
        str(Path(sys.executable).resolve()),
        "-E",
        "-s",
        "-B",
        "-X",
        "utf8",
        str(repository / "manage.py"),
    ]
    run_checked(
        [*manage, "migrate", "--noinput", "--verbosity", "0"],
        label="initial Django migration",
        cwd=repository,
        env=setup_env,
    )
    run_checked(
        [*manage, "migrate", "shares", "0024", "--noinput", "--verbosity", "0"],
        label="Django rollback to shares 0024",
        cwd=repository,
        env=setup_env,
    )
    source_applied = applied_migrations(source_database)
    assert ("shares", "0024_widen_site_message_titles") in source_applied
    assert PENDING_NODE not in source_applied
    assert_no_sidecars(source_database)

    source_backup = backup_directory / "production.sqlite3"
    source_checksum = Path(f"{source_backup}.sha256")
    source_metadata = Path(f"{source_backup}.metadata.json")
    run_checked(
        [*manage, "backup_database", str(source_backup)],
        label="real backup_database",
        cwd=repository,
        env=setup_env,
    )
    assert source_backup.is_file()
    assert source_checksum.is_file()
    assert source_metadata.is_file()
    assert applied_migrations(source_backup) == source_applied
    assert_no_sidecars(source_backup)

    source_media_manifest = manifest_directory / "source-media-manifest.json"
    run_checked(
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
            str(offline_media),
            "--output",
            str(source_media_manifest),
            "--snapshot-id",
            "proposal-contract-source",
            "--confirm-offline-snapshot",
        ],
        label="real offline media manifest",
        cwd=repository,
    )
    media_manifest = load_json(source_media_manifest)
    assert media_manifest["source_snapshot"] == {
        "id": "proposal-contract-source",
        "offline_confirmed": True,
    }
    assert media_manifest["file_count"] == 0
    assert media_manifest["total_size"] == 0

    shutil.copytree(offline_media, target_one)
    shutil.copytree(offline_media, target_two)
    for scope in (
        backup_directory,
        offline_media,
        source_media_manifest,
        target_one,
        target_two,
    ):
        apply_tree_dacl(scope, sealed_sddl)

    source_handoff_manifest = handoff_directory / "source-handoff-manifest.json"
    run_checked(
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
            str(offline_media),
            "--source-media-manifest",
            str(source_media_manifest),
            "--target-media-root-one",
            str(target_one),
            "--target-media-root-one-snapshot-id",
            "proposal-contract-target-one",
            "--target-media-root-two",
            str(target_two),
            "--target-media-root-two-snapshot-id",
            "proposal-contract-target-two",
            "--source-host",
            "proposal-contract-production-host",
            "--operator",
            "fixture-operator",
            "--expected-application-version",
            "proposal-contract",
            "--output",
            str(source_handoff_manifest),
            "--confirm-source-immutable",
            "--confirm-target-media-offline",
            "--confirm-database-media-consistent",
            "--confirm-operator-identity-asserted",
        ],
        label="production-copy handoff capture",
        cwd=repository,
    )
    captured_handoff = handoff.load_handoff(source_handoff_manifest)
    assert captured_handoff["format_version"] == 1
    assert [
        scope["role"] for scope in captured_handoff["access_baseline"]["scopes"]
    ] == list(handoff.SCOPE_ROLES)

    protected_paths = (
        source_database,
        source_backup,
        source_checksum,
        source_metadata,
        source_media_manifest,
        source_handoff_manifest,
    )
    before = {path: protected_snapshot(path) for path in protected_paths}
    slow_arguments = proposal_arguments(
        source_backup,
        source_checksum,
        source_metadata,
        source_media_manifest,
        source_handoff_manifest,
        policy_id="proposal-contract-slow-policy",
        proposal_id="proposal-contract-slow-proposal",
    )
    slow_root = runs / "slow"
    slow_secure, slow_acl_calls = scoped_acl(slow_root)
    slow_outcome = bootstrap.run_bootstrap(
        config_for(slow_root, slow_arguments),
        secure_run_root=slow_secure,
    )
    assert slow_outcome.exit_code == 0
    assert slow_acl_calls == [slow_root.resolve(), (slow_root / "approval").resolve()]
    slow_record = assert_exact_frozen_bundle(slow_root, slow_arguments)
    assert slow_record["workspace_access_control"] == "test_only_scoped_private_root"
    completion = assert_completion(slow_root, 0)
    assert completion["run_id"] == slow_record["run_id"]

    after = {path: protected_snapshot(path) for path in protected_paths}
    assert after == before
    assert_no_sidecars(source_database)
    assert_no_sidecars(source_backup)
    assert applied_migrations(source_database) == source_applied
    assert applied_migrations(source_backup) == source_applied

    body_path = slow_root / "evidence" / "policy-proposal-body.json"
    proposal_path = slow_root / "evidence" / "policy-proposal.json"
    body = load_json(body_path)
    proposal = load_json(proposal_path)
    assert proposal["format"] == "ffxivshare-source-upgrade-policy-proposal"
    assert proposal["format_version"] == 2
    assert proposal["state"] == "review_required"
    assert proposal["proposal_id"] == "proposal-contract-slow-proposal"
    assert proposal["run_id"] == slow_record["run_id"]
    assert proposal["bootstrap_nonce"] == slow_record["bootstrap_nonce"]
    assert proposal["body"] == body
    assert proposal["body_sha256"] == file_hash(body_path)
    assert_artifact_reference(slow_root, proposal["body_artifact"])
    assert body["format"] == "ffxivshare-source-upgrade-policy-proposal-body"
    assert body["format_version"] == 2
    assert body["proposal_id"] == "proposal-contract-slow-proposal"
    assert body["run_id"] == slow_record["run_id"]
    assert body["bootstrap_nonce"] == slow_record["bootstrap_nonce"]
    requirements = body["review_requirements"]
    assert requirements["lossless_review_status"] == "not_reviewed"
    assert tuple(requirements["required_evidence"]) == REQUIRED_EVIDENCE
    assert PENDING_NODE in {
        tuple(node) for node in requirements["pending_migration_nodes"]
    }
    evidence = body["evidence"]
    assert tuple(evidence) == REQUIRED_EVIDENCE
    evidence_paths = {
        key: assert_artifact_reference(slow_root, reference)
        for key, reference in evidence.items()
    }
    assert body["evidence_set_sha256"] == bootstrap._canonical_json_sha256(evidence)
    frozen_handoff = load_json(evidence_paths["source_handoff_manifest"])
    assert frozen_handoff == captured_handoff
    assert evidence_paths["source_handoff_manifest"].read_bytes() == (
        source_handoff_manifest.read_bytes()
    )
    review_plan = load_json(evidence_paths["migration_review_plan"])
    pending_nodes = {
        tuple(item["node"]) for item in review_plan["pending_migrations"]
    }
    assert PENDING_NODE in pending_nodes
    migration_state = load_json(evidence_paths["source_migration_state"])
    state_applied = {tuple(node) for node in migration_state["applied"]}
    assert ("shares", "0024_widen_site_message_titles") in state_applied
    assert PENDING_NODE not in state_applied
    projection = body["policy_projection"]
    assert projection["source_database_sha256"] == file_hash(source_backup)
    assert projection["source_leaf_nodes"] != projection["target_leaf_nodes"]

    events = verify_ledger(slow_root, proposal)
    assert any(event["stage"] == "policy_proposal_body_created" for event in events)
    assert not any(
        event["stage"] in {"source_schema_migrate", "target_schema_migrate"}
        for event in events
    )
    work_database = slow_root / "work" / "proposal-source.sqlite3"
    assert work_database.is_file()
    assert file_hash(work_database) == file_hash(source_backup)
    assert applied_migrations(work_database) == source_applied
    assert not any(
        "source_schema_migrate" in path.name or "target_schema_migrate" in path.name
        for path in (slow_root / "logs").iterdir()
    )
    assert (
        "state=review_required"
        in (slow_root / "logs" / "inner.stdout.log").read_text(encoding="utf-8")
    )
    print(
        "Slow real offline policy proposal passed: pending shares/0025, "
        "migration not applied, cutover false, entire RunRoot marked sensitive."
    )
else:
    print("Slow real offline proposal skipped; pass -IncludeSlow to run it.")

if os.name == "nt":
    print(
        "Windows ACL note: the real production hook was verified fail-closed; "
        "only unique synthetic RunRoots used the scoped test callback."
    )
print("Production-copy policy-proposal contract tests passed.")
'@

$scriptExitCode = 1
try {
    Set-Content -LiteralPath $fixtureScript -Value $fixtureSource -Encoding UTF8
    & $PythonExecutable `
        -I -S -B -X utf8 `
        $fixtureScript `
        $bootstrap `
        $RepositoryRoot `
        $temporaryRoot `
        $aclProbeRoot `
        $(if ($IncludeSlow) { '1' } else { '0' })
    $pythonExitCode = $LASTEXITCODE
    $global:LASTEXITCODE = 0
    Assert-Contract `
        -Condition ($pythonExitCode -eq 0) `
        -Message "Policy-proposal contract failed with exit code $pythonExitCode."
    $scriptExitCode = 0
}
catch {
    [Console]::Error.WriteLine(
        "Production-copy policy-proposal contract failed: {0}",
        $_.Exception.Message
    )
    $scriptExitCode = 1
}
finally {
    try {
        Remove-UniqueTestRoot `
            -Path $temporaryRoot `
            -ExpectedParent $RunParent `
            -LeafPattern '^ffxivshare-policy-proposal-[a-f0-9]{32}$'
        Remove-UniqueTestRoot `
            -Path $aclProbeRoot `
            -ExpectedParent $systemTemp `
            -LeafPattern '^ffxivshare-policy-proposal-acl-[a-f0-9]{32}$'
    }
    catch {
        [Console]::Error.WriteLine(
            "Production-copy policy-proposal cleanup failed: {0}",
            $_.Exception.Message
        )
        $scriptExitCode = 1
    }
}

exit $scriptExitCode
