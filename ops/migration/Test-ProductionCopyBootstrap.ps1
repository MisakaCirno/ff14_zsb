[CmdletBinding()]
param(
    [string]$RepositoryRoot = '',
    [string]$PythonExecutable = ''
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

function Remove-TestRoot {
    param([Parameter(Mandatory = $true)][string]$Path)
    $temp = [System.IO.Path]::GetFullPath(
        [System.IO.Path]::GetTempPath()
    ).TrimEnd('\', '/')
    $resolved = [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    Assert-Contract `
        -Condition ($resolved.StartsWith(
            $temp + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase
        )) `
        -Message 'Refusing to clean outside the system temp directory.'
    Assert-Contract `
        -Condition ((Split-Path -Leaf $resolved) -match '^ffxivshare-bootstrap-[a-f0-9]{32}$') `
        -Message 'Refusing to clean an unexpected test directory.'
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
        throw "Bootstrap contract requires the project virtual environment: $venvPython"
    }
    $PythonExecutable = (Resolve-Path -LiteralPath $venvPython).Path
}
else {
    $PythonExecutable = (Resolve-Path -LiteralPath $PythonExecutable).Path
}

$bootstrap = Join-Path $PSScriptRoot 'ProductionCopyBootstrap.py'
Assert-Contract `
    -Condition (Test-Path -LiteralPath $bootstrap -PathType Leaf) `
    -Message "Production-copy bootstrap is missing: $bootstrap"

$temporaryRoot = Join-Path `
    ([System.IO.Path]::GetTempPath()) `
    ('ffxivshare-bootstrap-' + [Guid]::NewGuid().ToString('N'))
$fixtureScript = Join-Path $temporaryRoot 'test_bootstrap.py'
New-Item -ItemType Directory -Path $temporaryRoot | Out-Null

$fixtureSource = @'
from __future__ import annotations

from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import sys


bootstrap_path = Path(sys.argv[1]).resolve()
test_root = Path(sys.argv[2]).resolve()

spec = importlib.util.spec_from_file_location(
    "ffxivshare_production_copy_bootstrap",
    bootstrap_path,
)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

assert sys.flags.isolated
assert sys.flags.no_site
assert sys.flags.dont_write_bytecode
assert sys.flags.utf8_mode
assert module._windows_ace_requires_conservative_rejection(4, 0x40000000)
assert module._windows_ace_requires_conservative_rejection(99, 0x00000040)
assert not module._windows_ace_requires_conservative_rejection(1, 0x40000000)
assert not module._windows_ace_requires_conservative_rejection(0, 0x40000000)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def file_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


inner_source = r'''from __future__ import annotations
import json
import os
from pathlib import Path
import stat
import sys

output = Path(sys.argv[1])
exit_code = int(sys.argv[2])
opaque_source_argument = sys.argv[3]
tamper = sys.argv[4] == "tamper"
payload = {
    "cwd": str(Path.cwd()),
    "environment": dict(sorted(os.environ.items())),
    "flags": {
        "dont_write_bytecode": bool(sys.flags.dont_write_bytecode),
        "ignore_environment": bool(sys.flags.ignore_environment),
        "no_site": bool(sys.flags.no_site),
        "no_user_site": bool(sys.flags.no_user_site),
        "utf8_mode": bool(sys.flags.utf8_mode),
    },
    "opaque_source_argument": opaque_source_argument,
}
output.write_text(
    json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
    encoding="utf-8",
    newline="\n",
)
if tamper:
    own_path = Path(__file__)
    os.chmod(own_path, stat.S_IWRITE)
    with own_path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("# tampered by contract fixture\n")
raise SystemExit(exit_code)
'''


repository = test_root / "repository"
repository.mkdir()
for relative in module.EXECUTION_FIXED_FILES:
    path = repository / Path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    if relative == "ops/migration/ProductionCopyBootstrap.py":
        shutil.copyfile(bootstrap_path, path)
    elif relative == "ops/migration/Rehearse-ProductionCopy.py":
        path.write_text(inner_source, encoding="utf-8", newline="\n")
    elif relative == "requirements.txt":
        path.write_text("Django==5.2.16\n", encoding="utf-8", newline="\n")
    else:
        path.write_text(
            "from __future__ import annotations\n",
            encoding="utf-8",
            newline="\n",
        )
for directory_name in module.EXECUTION_PYTHON_DIRECTORIES:
    directory = repository / directory_name
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text(
        "from __future__ import annotations\n",
        encoding="utf-8",
        newline="\n",
    )
    nested = directory / "nested"
    nested.mkdir()
    (nested / "module.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
        newline="\n",
    )

entrypoint = "ops/migration/Rehearse-ProductionCopy.py"
bundle_sha256 = module._execution_bundle_sha256(
    repository,
    inner_entrypoint=entrypoint,
)


def approved_policy(
    execution_bundle_sha256: str,
    proposal_sha256: str,
    review_sha256: str,
) -> dict[str, object]:
    digest = "a" * 64
    return {
        "approved": True,
        "approved_at": "2026-07-17T00:00:00.000000Z",
        "approval_tool_sha256": "b" * 64,
        "execution_bundle_sha256": execution_bundle_sha256,
        "format": module.POLICY_FORMAT,
        "format_version": module.POLICY_VERSION,
        "lossless_reviewed": True,
        "migration_plan_sha256": digest,
        "migration_runtime_sha256": digest,
        "policy_id": "bootstrap-fixture-policy",
        "proposal_body_sha256": "c" * 64,
        "proposal_bootstrap_completion_sha256": "d" * 64,
        "proposal_bootstrap_nonce": "e" * 64,
        "proposal_evidence_set_sha256": "f" * 64,
        "proposal_id": "bootstrap-fixture-proposal",
        "proposal_ledger_event_count": 9,
        "proposal_ledger_head_sha256": "1" * 64,
        "proposal_run_id": "bootstrap-fixture-run",
        "proposal_sha256": proposal_sha256,
        "review_id": "bootstrap-fixture-review",
        "review_record_sha256": review_sha256,
        "reviewed_at": "2026-07-17T00:00:01.000000Z",
        "reviewer": "bootstrap-contract",
        "reviewer_identity_verification": "operator_asserted_not_cryptographically_verified",
        "runtime_fingerprint_sha256": digest,
        "source_applied_migrations_sha256": digest,
        "source_database_sha256": digest,
        "source_leaf_nodes": [["shares", "0001_initial"]],
        "source_media_manifest_sha256": digest,
        "source_media_snapshot_id": "bootstrap-source-snapshot",
        "source_sqlite_schema_sha256": digest,
        "target_leaf_nodes": [["shares", "0002_target"]],
    }


proposal = test_root / "policies" / "approved-proposal.json"
review = test_root / "policies" / "approved-review.json"
write_json(proposal, {"format": "bootstrap-fixture-proposal", "version": 1})
write_json(review, {"format": "bootstrap-fixture-review", "version": 1})
policy = test_root / "policies" / "approved-policy.json"
write_json(
    policy,
    approved_policy(bundle_sha256, file_hash(proposal), file_hash(review)),
)
runs = test_root / "runs"
runs.mkdir()
outputs = test_root / "inner-outputs"
outputs.mkdir()
opaque_source = test_root / "does-not-exist" / "production.sqlite3"
os.environ["FFXIVSHARE_PRODUCTION_SECRET"] = "must-not-reach-inner"


def config_for(name: str, exit_code: int, *, tamper: bool = False):
    output = outputs / f"{name}.json"
    return (
        module.BootstrapConfig(
            repository_root=repository,
            python_executable=Path(sys.executable).resolve(),
            run_root=runs / name,
            mode="approved-rehearsal",
            inner_entrypoint=entrypoint,
            inner_arguments=(
                str(output),
                str(exit_code),
                str(opaque_source),
                "tamper" if tamper else "clean",
            ),
            policy_path=policy,
            proposal_path=proposal,
            review_record_path=review,
        ),
        output,
    )


for expected_exit in (0, 2, 130):
    config, output = config_for(f"exit-{expected_exit}", expected_exit)
    outcome = module.run_bootstrap(
        config,
        secure_run_root=lambda _path: "test_only_private_root",
    )
    assert outcome.exit_code == expected_exit
    assert output.is_file()
    inner = json.loads(output.read_text(encoding="utf-8"))
    assert inner["opaque_source_argument"] == str(opaque_source)
    assert inner["flags"] == {
        "dont_write_bytecode": True,
        "ignore_environment": True,
        "no_site": False,
        "no_user_site": True,
        "utf8_mode": True,
    }
    environment = inner["environment"]
    assert "FFXIVSHARE_PRODUCTION_SECRET" not in environment
    assert environment["FFXIVSHARE_BOOTSTRAP_RUN_ROOT"] == str(config.run_root)
    allowed = {
        "FFXIVSHARE_BOOTSTRAP_NONCE",
        "FFXIVSHARE_BOOTSTRAP_POLICY",
        "FFXIVSHARE_BOOTSTRAP_PROPOSAL",
        "FFXIVSHARE_BOOTSTRAP_RECORD",
        "FFXIVSHARE_BOOTSTRAP_REVIEW",
        "FFXIVSHARE_BOOTSTRAP_RUN_ID",
        "FFXIVSHARE_BOOTSTRAP_RUN_ROOT",
        "FFXIVSHARE_ENV_FILE",
        "PATH",
        "TEMP",
        "TMP",
    }
    if os.name == "nt":
        allowed.update({"COMSPEC", "PATHEXT", "SYSTEMROOT", "WINDIR"})
    assert {key.upper() for key in environment} == {key.upper() for key in allowed}, environment
    frozen_policy = config.run_root / "evidence" / "approved-policy.json"
    frozen_proposal = config.run_root / "evidence" / "approved-proposal.json"
    frozen_review = config.run_root / "evidence" / "approved-review.json"
    assert environment["FFXIVSHARE_BOOTSTRAP_POLICY"] == str(frozen_policy)
    assert environment["FFXIVSHARE_BOOTSTRAP_PROPOSAL"] == str(frozen_proposal)
    assert environment["FFXIVSHARE_BOOTSTRAP_REVIEW"] == str(frozen_review)
    assert frozen_policy.read_bytes() == policy.read_bytes()
    assert frozen_proposal.read_bytes() == proposal.read_bytes()
    assert frozen_review.read_bytes() == review.read_bytes()
    bootstrap_record = json.loads(outcome.bootstrap_record.read_text(encoding="utf-8"))
    assert bootstrap_record["workspace_access_control"] == "test_only_private_root"
    assert bootstrap_record["source_data_read_by_bootstrap"] is False
    assert bootstrap_record["media_read_by_bootstrap"] is False
    assert re.fullmatch(r"[0-9a-f]{64}", bootstrap_record["bootstrap_nonce"])
    assert bootstrap_record["configuration"]["inner_arguments"] == list(
        config.inner_arguments
    )
    assert bootstrap_record["execution_bundle"]["frozen_sha256"] == bundle_sha256
    assert bootstrap_record["policy"]["source"]["policy_id"] == "bootstrap-fixture-policy"
    assert bootstrap_record["policy"]["frozen"]["sha256"] == module._hash_stable(policy)[1]
    assert bootstrap_record["approval_inputs"]["proposal"]["frozen"]["sha256"] == file_hash(proposal)
    assert bootstrap_record["approval_inputs"]["review"]["frozen"]["sha256"] == file_hash(review)
    assert bootstrap_record["bootstrap_trusted_not_frozen"] is False
    layout = {
        item["path"]: item["kind"] for item in bootstrap_record["run_layout"]
    }
    assert layout == {
        ".": "directory",
        "approval": "directory",
        "artifacts": "directory",
        "code": "directory",
        "evidence": "directory",
        "logs": "directory",
        "runtime-empty.env": "file",
        "scratch-media": "directory",
        "target": "directory",
        "tmp": "directory",
        "work": "directory",
    }
    assert (config.run_root / "runtime-empty.env").read_bytes() == b""
    completion = json.loads(outcome.completion_record.read_text(encoding="utf-8"))
    assert completion["inner_exit_code"] == expected_exit
    assert completion["execution_bundle_unchanged"] is True
    assert completion["frozen_policy_unchanged"] is True
    assert completion["frozen_proposal_unchanged"] is True
    assert completion["frozen_review_unchanged"] is True
    assert not any((config.run_root / "code").rglob("*.pyc"))
    assert not any((config.run_root / "code").rglob("__pycache__"))


class InterruptRunner:
    def run(self, **_kwargs):
        raise KeyboardInterrupt


interrupt_config, _interrupt_output = config_for("runner-interrupted", 0)
interrupt_outcome = module.run_bootstrap(
    interrupt_config,
    runner=InterruptRunner(),
    secure_run_root=lambda _path: "test_only_private_root",
)
assert interrupt_outcome.exit_code == 130
interrupt_completion = json.loads(
    interrupt_outcome.completion_record.read_text(encoding="utf-8")
)
assert interrupt_completion["inner_exit_code"] == 130
assert interrupt_completion["execution_bundle_unchanged"] is True
assert (interrupt_config.run_root / "logs" / "inner.stdout.log").read_bytes() == b""
assert (interrupt_config.run_root / "logs" / "inner.stderr.log").read_bytes() == b""


post_run_state = {"returned": False, "injected": False}


class PostRunSignalRunner:
    def run(self, *, stdout_path, stderr_path, **_kwargs):
        stdout_path.write_bytes(b"completed before deferred signal\n")
        stderr_path.write_bytes(b"")
        post_run_state["returned"] = True
        return module.InnerRunResult(0, stdout_path, stderr_path)


original_assert_run_directories = module._assert_run_directories


def inject_post_run_signal(identities):
    original_assert_run_directories(identities)
    if post_run_state["returned"] and not post_run_state["injected"]:
        post_run_state["injected"] = True
        signal.raise_signal(signal.SIGINT)


post_run_config, _post_run_output = config_for("post-run-interrupted", 0)
module._assert_run_directories = inject_post_run_signal
try:
    post_run_outcome = module.run_bootstrap(
        post_run_config,
        runner=PostRunSignalRunner(),
        secure_run_root=lambda _path: "test_only_private_root",
    )
finally:
    module._assert_run_directories = original_assert_run_directories
assert post_run_state["injected"] is True
assert post_run_outcome.exit_code == 130
post_run_completion = json.loads(
    post_run_outcome.completion_record.read_text(encoding="utf-8")
)
assert post_run_completion["inner_exit_code"] == 130


completion_signal_state = {"injected": False}
original_write_json_create_new = module._write_json_create_new


def inject_after_completion_publish(path, value):
    result = original_write_json_create_new(path, value)
    if path.name == "completion.json" and not completion_signal_state["injected"]:
        completion_signal_state["injected"] = True
        signal.raise_signal(signal.SIGINT)
    return result


completion_signal_config, _completion_signal_output = config_for(
    "completion-publish-interrupted",
    0,
)
previous_sigint_handler = signal.getsignal(signal.SIGINT)
module._write_json_create_new = inject_after_completion_publish
try:
    completion_signal_outcome = module.run_bootstrap(
        completion_signal_config,
        runner=PostRunSignalRunner(),
        secure_run_root=lambda _path: "test_only_private_root",
    )
finally:
    module._write_json_create_new = original_write_json_create_new
assert completion_signal_state["injected"] is True
assert completion_signal_outcome.exit_code == 130
assert signal.getsignal(signal.SIGINT) == previous_sigint_handler
completion_signal_payload = json.loads(
    completion_signal_outcome.completion_record.read_text(encoding="utf-8")
)
assert completion_signal_payload["inner_exit_code"] == 130

# A wrong authority is rejected before RunRoot creation.
mismatch_root = runs / "bundle-mismatch"
mismatch = module.BootstrapConfig(
    repository_root=repository,
    python_executable=Path(sys.executable).resolve(),
    run_root=mismatch_root,
    mode="pinned-bundle",
    inner_entrypoint=entrypoint,
    inner_arguments=(),
    expected_execution_bundle_sha256="0" * 64,
)
try:
    module.run_bootstrap(
        mismatch,
        secure_run_root=lambda _path: "test_only_private_root",
    )
except module.BootstrapError:
    pass
else:
    raise AssertionError("Mismatched bundle authority was accepted")
assert not mismatch_root.exists()

# Proposal bootstrap does not need a pre-existing policy or bundle digest. Its
# authority is the stable pre-freeze/post-freeze repository consistency check.
proposal_config = module.BootstrapConfig(
    repository_root=repository,
    python_executable=Path(sys.executable).resolve(),
    run_root=runs / "policy-proposal",
    mode="policy-proposal",
    inner_entrypoint="ops/migration/Propose-ProductionCopyPolicy.py",
    inner_arguments=(),
)
proposal_outcome = module.run_bootstrap(
    proposal_config,
    secure_run_root=lambda _path: "test_only_private_root",
)
assert proposal_outcome.exit_code == 0
proposal_record = json.loads(
    proposal_outcome.bootstrap_record.read_text(encoding="utf-8")
)
assert proposal_record["policy"] is None
assert proposal_record["approval_inputs"] is None
assert proposal_record["bootstrap_trusted_not_frozen"] is True
assert proposal_record["execution_bundle"]["authority"] == "stable_repository_consistency"

# Approved policy JSON is duplicate-key strict and cannot be supplemented by a
# second execution_bundle_sha256 value.
duplicate_policy = test_root / "policies" / "duplicate-policy.json"
valid_text = json.dumps(
    approved_policy(bundle_sha256, file_hash(proposal), file_hash(review)),
    sort_keys=True,
)
duplicate_policy.write_text(
    valid_text[:-1] + ',"execution_bundle_sha256":"' + bundle_sha256 + '"}\n',
    encoding="utf-8",
    newline="\n",
)
duplicate_root = runs / "duplicate-policy"
duplicate_config = module.BootstrapConfig(
    repository_root=repository,
    python_executable=Path(sys.executable).resolve(),
    run_root=duplicate_root,
    mode="approved-rehearsal",
    inner_entrypoint=entrypoint,
    inner_arguments=(),
    policy_path=duplicate_policy,
    proposal_path=proposal,
    review_record_path=review,
)
try:
    module.run_bootstrap(
        duplicate_config,
        secure_run_root=lambda _path: "test_only_private_root",
    )
except module.BootstrapConfigurationError:
    pass
else:
    raise AssertionError("Duplicate-key policy was accepted")
assert not duplicate_root.exists()

# Existing RunRoot content is never reused or removed.
reuse_config, _reuse_output = config_for("reuse", 0)
reuse_config.run_root.mkdir()
marker = reuse_config.run_root / "user-marker.txt"
marker.write_text("keep\n", encoding="utf-8", newline="\n")
try:
    module.run_bootstrap(
        reuse_config,
        secure_run_root=lambda _path: "test_only_private_root",
    )
except module.BootstrapConfigurationError:
    pass
else:
    raise AssertionError("Existing RunRoot was reused")
assert marker.read_text(encoding="utf-8") == "keep\n"

# Any inner mutation of frozen code is recorded and fails closed.
tamper_config, tamper_output = config_for("inner-tamper", 0, tamper=True)
try:
    module.run_bootstrap(
        tamper_config,
        secure_run_root=lambda _path: "test_only_private_root",
    )
except module.BootstrapError:
    pass
else:
    raise AssertionError("Frozen bundle mutation was accepted")
tamper_completion = json.loads(
    (tamper_config.run_root / "evidence" / "completion.json").read_text(
        encoding="utf-8"
    )
)
assert tamper_completion["execution_bundle_unchanged"] is False
assert tamper_output.is_file()

# The ordinary private user-profile chain is a positive regression for the
# default C:\ Authenticated Users:(AD) ACE. A shared temporary parent remains a
# deliberate fail-closed case because another principal could affect a new child.
if os.name == "nt":
    positive_parent = Path.home() / "ffxivshare-bootstrap-prospective-run"
    assert (
        module._secure_run_root(positive_parent, parent_only=True)
        == "windows_parent_chain_delete_write_acl_review_passed"
    )
    acl_config, _acl_output = config_for("windows-acl-fail-closed", 0)
    try:
        module.run_bootstrap(acl_config)
    except module.BootstrapConfigurationError:
        pass
    else:
        raise AssertionError("Windows bootstrap claimed an unproved private RunRoot")
    if acl_config.run_root.exists():
        assert acl_config.run_root.is_dir()
        assert not (acl_config.run_root / "code").exists()

# Propose and Approve are mandatory bundle members, not optional follow-up tools.
propose = repository / "ops" / "migration" / "Propose-ProductionCopyPolicy.py"
propose.unlink()
try:
    module._execution_bundle_sha256(repository, inner_entrypoint=entrypoint)
except module.BootstrapError:
    pass
else:
    raise AssertionError("Bundle without Propose-ProductionCopyPolicy.py was accepted")

print("Production-copy bootstrap contract tests passed.")
'@

try {
    Set-Content -LiteralPath $fixtureScript -Value $fixtureSource -Encoding UTF8
    & $PythonExecutable -I -S -B -X utf8 $fixtureScript $bootstrap $temporaryRoot
    $exitCode = $LASTEXITCODE
    $global:LASTEXITCODE = 0
    Assert-Contract `
        -Condition ($exitCode -eq 0) `
        -Message "Production-copy bootstrap contract failed with exit code $exitCode."
}
finally {
    Remove-TestRoot -Path $temporaryRoot
}
