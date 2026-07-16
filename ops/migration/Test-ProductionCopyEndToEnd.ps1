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

spec = importlib.util.spec_from_file_location(
    "ffxivshare_production_copy_e2e_bootstrap",
    bootstrap_path,
)
assert spec is not None and spec.loader is not None
bootstrap = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bootstrap
spec.loader.exec_module(bootstrap)

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
PENDING_MIGRATION = ("shares", "0025_add_collection_owner_index")
EXPECTED_ENTITY_COUNTS = {
    "groups": 1,
    "users": 3,
    "user_profiles": 3,
    "shares": 1,
    "collections": 1,
    "collection_items": 1,
    "reports": 1,
    "share_logs": 1,
    "announcements": 1,
    "site_messages": 1,
    "admin_log_entries": 1,
}


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
for directory in (
    source_directory,
    backup_directory,
    source_media_root,
    manifest_directory,
    tmp_directory,
    runs,
    targets,
):
    directory.mkdir(parents=True)

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

run_command(
    [*manage, "migrate", "--noinput", "--verbosity", "0"],
    label="initial complete source migration",
    cwd=repository,
    env=setup_env,
)
run_command(
    [*manage, "migrate", "shares", "0024", "--noinput", "--verbosity", "0"],
    label="source rollback to shares 0024",
    cwd=repository,
    env=setup_env,
)

SEED_SCRIPT = r'''
from __future__ import annotations
import json
import os
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ffxivshare.settings")
django.setup()
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from shares.models import (
    Announcement,
    Collection,
    CollectionItem,
    Report,
    Share,
    ShareLog,
    SiteMessage,
    UserProfile,
)

admin = User.objects.create_superuser(
    username="e2e-admin",
    email="admin@example.invalid",
    password="e2e-admin-password",
)
author = User.objects.create_user(
    username="e2e-author",
    email="author@example.invalid",
    password="e2e-author-password",
)
reader = User.objects.create_user(
    username="e2e-reader",
    email="reader@example.invalid",
    password="e2e-reader-password",
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
    review_feedback="Approved by the migration fixture moderator.",
    reviewed_at=now,
    reviewed_by=admin,
    restriction_state="clear",
    restriction_reason="",
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
    resolution_reason="Reviewed and dismissed without an active restriction.",
)
SiteMessage.objects.create(
    recipient=author,
    sender=admin,
    message_type="report_dismissed",
    title="E2E moderation notice " + ("x" * 210),
    content="The representative report was reviewed and dismissed.",
    related_share=share,
    related_report=report,
    metadata={"fixture": "production-copy-e2e", "tags": ["review", "migration"]},
    read_at=now,
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
    action_flag=CHANGE,
    change_message=json.dumps(
        [{"changed": {"fields": ["status", "review_feedback"]}}],
        separators=(",", ":"),
    ),
)
assert User.objects.count() == 3
assert UserProfile.objects.count() == 3
assert Share.objects.count() == 1
assert share.likes.count() == 2
assert share.favorites.count() == 1
assert Report.objects.count() == 1
assert SiteMessage.objects.count() == 1
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

source_applied = applied_migrations(source_database)
assert ("shares", "0024_widen_site_message_titles") in source_applied
assert PENDING_MIGRATION not in source_applied
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

protected_paths = (
    source_database,
    source_backup,
    source_checksum,
    source_metadata,
    source_media_manifest,
    media_file,
)
protected_before = {path: protected_snapshot(path) for path in protected_paths}
source_media_tree_before = tree_snapshot(source_media_root)

proposal_root = runs / "proposal"
proposal_args = proposal_arguments(
    source_backup,
    source_checksum,
    source_metadata,
    source_media_manifest,
)
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
assert proposal["body"]["policy_projection"]["source_database_sha256"] == file_hash(
    source_backup
)
assert PENDING_MIGRATION in {
    tuple(node)
    for node in proposal["body"]["review_requirements"]["pending_migration_nodes"]
}

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
        "Reviewed shares/0025 AddIndex as lossless for this synthetic E2E source.",
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
assert PENDING_MIGRATION in {tuple(node) for node in review["migrations_reviewed"]}

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
approval_before_rehearsals = {
    path: protected_snapshot(path)
    for path in (proposal_path, review_path, policy_path)
}


def assert_entity_counts(manifest: dict[str, Any]) -> None:
    entities = manifest["entities"]
    for name, expected in EXPECTED_ENTITY_COUNTS.items():
        assert entities[name]["count"] == expected, (name, entities[name])


def run_approved_rehearsal(name: str, media_snapshot_id: str) -> dict[str, str]:
    target_media_root = targets / f"{name}-media"
    shutil.copytree(source_media_root, target_media_root)
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
    final_state = load_json(
        run_root / "evidence" / "final-target-migration-state.json"
    )
    assert PENDING_MIGRATION in {tuple(node) for node in final_state["applied"]}

    final_target_manifest = load_json(
        run_root / "artifacts" / "target-media-manifest-final.json"
    )
    assert final_target_manifest["file_count"] == 1
    assert final_target_manifest["source_snapshot"]["id"] == media_snapshot_id
    events = read_ledger(run_root)
    assert events[0]["stage"] == "created"
    assert events[1]["stage"] == "runtime_fingerprint_initial_verified"
    stage_events = {event["stage"]: event for event in events}
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
    stage_sequence = [event["stage"] for event in events]
    assert stage_sequence.index("runtime_fingerprint_initial_verified") < (
        stage_sequence.index("approved_policy_evidence_verified")
    )
    assert stage_sequence.index("runtime_fingerprint_pre_migrate_verified") < (
        stage_sequence.index("runtime_fingerprint_post_migrate_verified")
    )
    assert stage_sequence.index("target_snapshot_set_final_verified") < (
        stage_sequence.index("runtime_fingerprint_final_verified")
    )
    assert stage_sequence.index("runtime_fingerprint_final_verified") < (
        stage_sequence.index("deployment_candidate_verified")
    )
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
    assert_artifact_reference(run_root, candidate["final_site_data_comparison"])
    assert_artifact_reference(run_root, candidate["final_restriction_preflight"])
    assert_artifact_reference(run_root, candidate["target_media_final_comparison"])

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


first_summary = run_approved_rehearsal(
    "approved-rehearsal-one",
    "production-copy-e2e-target-media-one",
)
second_summary = run_approved_rehearsal(
    "approved-rehearsal-two",
    "production-copy-e2e-target-media-two",
)
assert first_summary == second_summary
assert {path: protected_snapshot(path) for path in protected_paths} == protected_before
assert tree_snapshot(source_media_root) == source_media_tree_before
assert {
    path: protected_snapshot(path)
    for path in (proposal_path, review_path, policy_path)
} == approval_before_rehearsals
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
