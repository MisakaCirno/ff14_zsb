from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any


PROPOSAL_FORMAT = "ffxivshare-source-upgrade-policy-proposal"
PROPOSAL_VERSION = 2
PROPOSAL_BODY_FORMAT = "ffxivshare-source-upgrade-policy-proposal-body"
PROPOSAL_BODY_VERSION = 2
REVIEW_PLAN_FORMAT = "ffxivshare-migration-review-plan"
REVIEW_PLAN_VERSION = 1
POLICY_FORMAT = "ffxivshare-source-upgrade-policy"
POLICY_VERSION = 2
INNER_ENTRYPOINT = "ops/migration/Propose-ProductionCopyPolicy.py"
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


MIGRATION_REVIEW_PLAN_SCRIPT = r'''from __future__ import annotations
import json
import os
from pathlib import Path
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ffxivshare.settings")
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
executor = MigrationExecutor(connection)
targets = sorted(executor.loader.graph.leaf_nodes())
plan = executor.migration_plan(targets)
pending = []
for migration, backwards in plan:
    if backwards:
        raise RuntimeError("Policy proposal unexpectedly requires a backwards migration.")
    pending.append({
        "node": [migration.app_label, migration.name],
        "module": migration.__module__,
        "dependencies": sorted([list(node) for node in migration.dependencies]),
        "replaces": sorted([list(node) for node in (migration.replaces or [])]),
        "operations": [
            f"{operation.__class__.__module__}.{operation.__class__.__qualname__}"
            for operation in migration.operations
        ],
    })
pending.sort(key=lambda item: tuple(item["node"]))
payload = {
    "format": "ffxivshare-migration-review-plan",
    "format_version": 1,
    "database_vendor": connection.vendor,
    "source_applied_migrations": [
        list(node) for node in sorted(executor.loader.applied_migrations)
    ],
    "target_leaf_nodes": [list(node) for node in targets],
    "pending_migrations": pending,
}
destination = Path(__import__("sys").argv[1])
with destination.open("x", encoding="utf-8", newline="\n") as stream:
    json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
connection.close()
'''


def _load_rehearsal_module() -> Any:
    path = Path(__file__).resolve().with_name("Rehearse-ProductionCopy.py")
    spec = importlib.util.spec_from_file_location("ffxivshare_frozen_rehearsal", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Frozen rehearsal module cannot be loaded.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


core = _load_rehearsal_module()


def _load_handoff_module() -> Any:
    path = Path(__file__).resolve().with_name("ProductionCopyHandoff.py")
    spec = importlib.util.spec_from_file_location("ffxivshare_frozen_handoff", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Frozen handoff module cannot be loaded.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


handoff_core = _load_handoff_module()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect an immutable production backup copy and create a review-required "
            "migration policy proposal without applying any migration."
        )
    )
    parser.add_argument("--source-database", required=True)
    parser.add_argument("--source-checksum", required=True)
    parser.add_argument("--source-metadata", required=True)
    parser.add_argument("--source-media-manifest", required=True)
    parser.add_argument("--source-handoff-manifest", required=True)
    parser.add_argument("--policy-id", required=True)
    parser.add_argument("--proposal-id", required=True)
    parser.add_argument("--confirm-source-immutable", action="store_true")
    return parser.parse_args()


def _inner_context() -> dict[str, Any]:
    if (
        not sys.flags.ignore_environment
        or not sys.flags.no_user_site
        or not sys.flags.dont_write_bytecode
        or not sys.flags.utf8_mode
    ):
        raise core.ConfigurationError(
            "Proposal inner must run with -E -s -B -X utf8."
        )
    run_root_raw = os.environ.get("FFXIVSHARE_BOOTSTRAP_RUN_ROOT", "")
    record_raw = os.environ.get("FFXIVSHARE_BOOTSTRAP_RECORD", "")
    nonce = os.environ.get("FFXIVSHARE_BOOTSTRAP_NONCE", "")
    run_id = os.environ.get("FFXIVSHARE_BOOTSTRAP_RUN_ID", "")
    if not all((run_root_raw, record_raw, nonce, run_id)):
        raise core.ConfigurationError("Proposal inner requires a bootstrap record.")
    run_root = core._existing_path(
        run_root_raw,
        label="Bootstrap RunRoot",
        directory=True,
    )
    record_path = core._existing_path(
        record_raw,
        label="Bootstrap record",
        directory=False,
    )
    code_root = run_root / "code"
    expected_self = code_root / Path(INNER_ENTRYPOINT)
    if (
        core._canonical_key(Path(__file__).resolve())
        != core._canonical_key(expected_self.resolve(strict=True))
        or core._canonical_key(Path.cwd().resolve()) != core._canonical_key(code_root)
        or core._canonical_key(record_path)
        != core._canonical_key(run_root / "evidence" / "bootstrap.json")
    ):
        raise core.ConfigurationError("Proposal inner escaped its frozen bootstrap root.")
    record = core._load_json(record_path)
    expected_record_keys = {
        "approval_inputs",
        "bootstrap_nonce",
        "bootstrap_trusted_not_frozen",
        "configuration",
        "execution_bundle",
        "format",
        "format_version",
        "generated_at",
        "media_read_by_bootstrap",
        "policy",
        "python",
        "run_id",
        "run_layout",
        "source_data_read_by_bootstrap",
        "workspace_access_control",
    }
    if (
        not isinstance(record, dict)
        or set(record) != expected_record_keys
        or record["format"] != "ffxivshare-production-copy-bootstrap"
        or record["format_version"] != 1
        or record["run_id"] != run_id
        or record["bootstrap_nonce"] != nonce
        or record["policy"] is not None
        or record["approval_inputs"] is not None
        or record["bootstrap_trusted_not_frozen"] is not True
        or record["source_data_read_by_bootstrap"] is not False
        or record["media_read_by_bootstrap"] is not False
        or not isinstance(record["workspace_access_control"], str)
        or not record["workspace_access_control"]
        or not isinstance(record["generated_at"], str)
        or core.UTC_TIMESTAMP_PATTERN.fullmatch(record["generated_at"]) is None
        or core.POLICY_ID_PATTERN.fullmatch(run_id) is None
        or core.SHA256_PATTERN.fullmatch(nonce) is None
    ):
        raise core.ConfigurationError("Proposal bootstrap identity is invalid.")
    configuration = record.get("configuration")
    bundle = record.get("execution_bundle")
    if (
        not isinstance(configuration, dict)
        or set(configuration)
        != {"inner_arguments", "inner_entrypoint", "mode", "repository_root", "run_root"}
        or configuration["mode"] != "policy-proposal"
        or configuration["inner_entrypoint"] != INNER_ENTRYPOINT
        or configuration["inner_arguments"] != sys.argv[1:]
        or not isinstance(configuration["run_root"], str)
        or not isinstance(configuration["repository_root"], str)
        or core._canonical_key(Path(configuration["run_root"]))
        != core._canonical_key(run_root)
        or not isinstance(bundle, dict)
        or set(bundle)
        != {"authority", "expected_sha256", "frozen_sha256", "manifest"}
        or bundle["authority"] != "stable_repository_consistency"
        or bundle["expected_sha256"] != bundle["frozen_sha256"]
        or not isinstance(bundle["frozen_sha256"], str)
        or core.SHA256_PATTERN.fullmatch(bundle["frozen_sha256"]) is None
    ):
        raise core.ConfigurationError("Proposal bootstrap authority is invalid.")
    manifest_path = core._bootstrap_artifact(
        bundle["manifest"],
        run_root=run_root,
        label="proposal execution bundle manifest",
        expected_relative="evidence/execution-bundle.json",
    )
    manifest = core._load_json(manifest_path, maximum_size=32 * 1024 * 1024)
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"execution_bundle_sha256", "files", "format", "format_version"}
        or manifest["format"] != "ffxivshare-production-copy-execution-bundle"
        or manifest["format_version"] != 1
        or manifest["execution_bundle_sha256"] != bundle["frozen_sha256"]
        or not isinstance(files, list)
        or not files
        or not all(
            isinstance(item, dict)
            and set(item) == {"path", "size", "sha256"}
            and isinstance(item["path"], str)
            and "\\" not in item["path"]
            and not core.PurePosixPath(item["path"]).is_absolute()
            and ".." not in core.PurePosixPath(item["path"]).parts
            and core.PurePosixPath(item["path"]).as_posix() == item["path"]
            and isinstance(item["size"], int)
            and not isinstance(item["size"], bool)
            and item["size"] >= 0
            and isinstance(item["sha256"], str)
            and core.SHA256_PATTERN.fullmatch(item["sha256"]) is not None
            for item in files
        )
    ):
        raise core.ConfigurationError("Proposal execution inventory is invalid.")
    expected_files = tuple(item["path"] for item in files)
    if (
        list(expected_files) != sorted(set(expected_files))
        or "ops/migration/ProductionCopyHandoff.py" not in expected_files
        or "ops/migration/Verify-ProductionCopyRehearsalPair.py" not in expected_files
        or core._canonical_json_sha256(files) != bundle["frozen_sha256"]
        or core._execution_snapshot_sha256(code_root, expected_files)
        != bundle["frozen_sha256"]
    ):
        raise core.ConfigurationError("Proposal frozen execution bundle changed.")
    repository_root = core._absolute_path(
        configuration["repository_root"],
        label="Bootstrap repository root",
    )
    return {
        "run_root": run_root,
        "record_path": record_path,
        "record": record,
        "run_id": run_id,
        "nonce": nonce,
        "bundle_sha256": bundle["frozen_sha256"],
        "bundle_files": expected_files,
        "manifest_path": manifest_path,
        "repository_root": repository_root,
        "workspace_access_control": record.get("workspace_access_control"),
    }


def _validate_nodes(
    value: Any,
    *,
    label: str,
    allow_empty: bool,
) -> list[list[str]]:
    if not isinstance(value, list):
        raise core.RehearsalError(f"{label}_invalid")
    nodes: list[list[str]] = []
    for node in value:
        if (
            not isinstance(node, list)
            or len(node) != 2
            or not all(isinstance(part, str) for part in node)
            or not all(core.MIGRATION_PART_PATTERN.fullmatch(part) for part in node)
        ):
            raise core.RehearsalError(f"{label}_invalid")
        nodes.append([node[0], node[1]])
    canonical = [list(node) for node in sorted({tuple(node) for node in nodes})]
    if nodes != canonical or (not nodes and not allow_empty):
        raise core.RehearsalError(f"{label}_not_canonical")
    return nodes


def _validate_review_plan(path: Path) -> dict[str, Any]:
    value = core._load_json(path)
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "database_vendor",
            "format",
            "format_version",
            "pending_migrations",
            "source_applied_migrations",
            "target_leaf_nodes",
        }
        or value["format"] != REVIEW_PLAN_FORMAT
        or value["format_version"] != REVIEW_PLAN_VERSION
        or value["database_vendor"] != "sqlite"
        or not isinstance(value["pending_migrations"], list)
    ):
        raise core.RehearsalError("migration_review_plan_invalid")
    value["source_applied_migrations"] = _validate_nodes(
        value["source_applied_migrations"],
        label="proposal_source_applied",
        allow_empty=False,
    )
    value["target_leaf_nodes"] = _validate_nodes(
        value["target_leaf_nodes"],
        label="proposal_target_leaf_nodes",
        allow_empty=False,
    )
    nodes: list[list[str]] = []
    for item in value["pending_migrations"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"dependencies", "module", "node", "operations", "replaces"}
            or not isinstance(item["module"], str)
            or not isinstance(item["operations"], list)
            or not all(isinstance(operation, str) for operation in item["operations"])
        ):
            raise core.RehearsalError("migration_review_plan_invalid")
        node = _validate_nodes(
            [item["node"]], label="proposal_pending", allow_empty=False
        )
        nodes.extend(node)
        _validate_nodes(
            item["dependencies"],
            label="proposal_pending_dependencies",
            allow_empty=True,
        )
        _validate_nodes(
            item["replaces"],
            label="proposal_pending_replaces",
            allow_empty=True,
        )
    canonical_nodes = [list(node) for node in sorted({tuple(node) for node in nodes})]
    if nodes != canonical_nodes:
        raise core.RehearsalError("migration_review_plan_not_canonical")
    value["pending_migration_nodes"] = nodes
    return value


def _lexical_path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(os.fspath(path)))


def _lexical_paths_overlap(first: str | Path, second: str | Path) -> bool:
    first_key = _lexical_path_key(first)
    second_key = _lexical_path_key(second)
    try:
        common = os.path.commonpath((first_key, second_key))
    except ValueError:
        return False
    return common in {first_key, second_key}


def _handoff_paths_match_config(handoff: dict[str, Any], config: Any) -> bool:
    database_backup_set = handoff["database_backup_set"]
    expected_database_paths = {
        "database": config.source_database,
        "checksum": config.source_checksum,
        "metadata": config.source_metadata,
    }
    if any(
        _lexical_path_key(database_backup_set[role]["path"])
        != _lexical_path_key(path)
        for role, path in expected_database_paths.items()
    ):
        return False
    return (
        _lexical_path_key(handoff["source_media"]["manifest"]["path"])
        == _lexical_path_key(config.source_media_manifest)
    )


def _verify_live_handoff(
    handoff: dict[str, Any],
    config: Any,
    source_handoff_manifest: Path,
    original_repository_root: Path,
    *,
    verify_content: bool,
) -> dict[str, Any]:
    try:
        access_baseline = handoff_core.verify_live_handoff(
            handoff,
            original_repository_root,
            disallowed_roots=(
                config.repository_root,
                config.run_root,
                source_handoff_manifest,
            ),
            verify_content=verify_content,
        )
    except handoff_core.HandoffError as exc:
        raise core.RehearsalBlocked("source_handoff_live_verification_failed") from exc
    scope_roles = tuple(scope["role"] for scope in access_baseline["scopes"])
    if scope_roles != HANDOFF_SCOPE_ROLES:
        raise core.RehearsalBlocked("source_handoff_scope_roles_invalid")
    return access_baseline


def _proposal_config(
    arguments: argparse.Namespace,
    context: dict[str, Any],
) -> tuple[Any, Path]:
    if not arguments.confirm_source_immutable:
        raise core.ConfigurationError("--confirm-source-immutable is required.")
    if core.POLICY_ID_PATTERN.fullmatch(arguments.policy_id or "") is None:
        raise core.ConfigurationError("Policy id is invalid.")
    if core.POLICY_ID_PATTERN.fullmatch(arguments.proposal_id or "") is None:
        raise core.ConfigurationError("Proposal id is invalid.")
    source_database = core._absolute_path(
        arguments.source_database, label="Source database"
    )
    source_checksum = core._absolute_path(
        arguments.source_checksum, label="Source checksum"
    )
    source_metadata = core._absolute_path(
        arguments.source_metadata, label="Source metadata"
    )
    source_media_manifest = core._absolute_path(
        arguments.source_media_manifest,
        label="Source media manifest",
    )
    source_handoff_manifest = core._existing_path(
        arguments.source_handoff_manifest,
        label="Source handoff manifest",
        directory=False,
    )
    source_input_paths = (
        source_database,
        source_checksum,
        source_metadata,
        source_media_manifest,
        source_handoff_manifest,
    )
    if len({_lexical_path_key(path) for path in source_input_paths}) != len(
        source_input_paths
    ):
        raise core.ConfigurationError("Source handoff inputs must be distinct files.")
    if _lexical_path_key(source_checksum) != _lexical_path_key(
        source_database.with_name(source_database.name + ".sha256")
    ):
        raise core.ConfigurationError("Source checksum name is invalid.")
    if _lexical_path_key(source_metadata) != _lexical_path_key(
        source_database.with_name(source_database.name + ".metadata.json")
    ):
        raise core.ConfigurationError("Source metadata name is invalid.")
    run_root = context["run_root"]
    if any(
        _lexical_paths_overlap(source_handoff_manifest, forbidden)
        for forbidden in (
            context["repository_root"],
            run_root,
            run_root / "code",
        )
    ):
        raise core.ConfigurationError(
            "Source handoff manifest overlaps a trusted code or RunRoot scope."
        )
    return (
        core.RehearsalConfig(
            repository_root=run_root / "code",
            python_executable=Path(sys.executable).resolve(),
            source_database=source_database,
            source_checksum=source_checksum,
            source_metadata=source_metadata,
            source_upgrade_policy=context["record_path"],
            source_media_manifest=source_media_manifest,
            target_media_root=run_root / "target",
            target_media_snapshot_id="proposal-not-applicable",
            run_root=run_root,
            confirm_source_immutable=True,
            confirm_target_media_offline=True,
        ),
        source_handoff_manifest,
    )


def _execute_proposal(
    arguments: argparse.Namespace,
    context: dict[str, Any],
) -> tuple[dict[str, Any], Any]:
    config, source_handoff_manifest = _proposal_config(arguments, context)
    rehearsal = core.Rehearsal(
        config,
        core.SubprocessRunner(),
        workspace_access_control=str(context["workspace_access_control"]),
    )
    rehearsal.execution_bundle_expected = context["bundle_sha256"]
    rehearsal.execution_bundle_files = context["bundle_files"]
    rehearsal.ledger.record(
        "proposal_started",
        "passed",
        {
            "run_id": context["run_id"],
            "bootstrap_nonce": context["nonce"],
            "source_data_read_by_bootstrap": False,
            "live_production_service_access_requested": False,
            "network_isolation_enforced": False,
            "network_access_observation": "not_measured",
        },
    )

    handoff_size, handoff_sha256, handoff_identity = core._regular_file_checkpoint(
        source_handoff_manifest,
        issue_prefix="source_handoff_manifest",
        maximum_size=handoff_core.MAX_HANDOFF_BYTES,
    )
    try:
        handoff = handoff_core.load_handoff(source_handoff_manifest)
    except handoff_core.HandoffError as exc:
        raise core.RehearsalBlocked("source_handoff_manifest_invalid") from exc
    if not _handoff_paths_match_config(handoff, config):
        raise core.RehearsalBlocked("source_handoff_manifest_paths_mismatch")
    handoff_backup_set = handoff["database_backup_set"]
    config = replace(
        config,
        source_database=Path(handoff_backup_set["database"]["path"]),
        source_checksum=Path(handoff_backup_set["checksum"]["path"]),
        source_metadata=Path(handoff_backup_set["metadata"]["path"]),
        source_media_manifest=Path(handoff["source_media"]["manifest"]["path"]),
    )
    rehearsal.config = config
    base_env = core._base_environment(config, config.run_root)
    rehearsal.production_copy_read_performed = True
    access_baseline = _verify_live_handoff(
        handoff,
        config,
        source_handoff_manifest,
        context["repository_root"],
        verify_content=True,
    )
    for suffix in ("-wal", "-shm", "-journal"):
        if os.path.lexists(Path(f"{config.source_database}{suffix}")):
            raise core.RehearsalBlocked("source_database_has_live_sqlite_sidecar")
    handoff_copy = rehearsal.artifacts / "source-handoff-manifest.json"
    copied_handoff_size, copied_handoff_sha256 = core._copy_stable(
        source_handoff_manifest,
        handoff_copy,
    )
    if (
        copied_handoff_size != handoff_size
        or copied_handoff_sha256 != handoff_sha256
    ):
        raise core.RehearsalBlocked("source_handoff_manifest_copy_mismatch")
    core._regular_file_checkpoint(
        source_handoff_manifest,
        issue_prefix="source_handoff_manifest",
        expected_sha256=handoff_sha256,
        expected_identity=handoff_identity,
    )
    try:
        frozen_handoff = handoff_core.load_handoff(handoff_copy)
    except handoff_core.HandoffError as exc:
        raise core.RehearsalBlocked("frozen_source_handoff_manifest_invalid") from exc
    if frozen_handoff != handoff:
        raise core.RehearsalBlocked("source_handoff_manifest_copy_semantics_mismatch")
    handoff_reference = core._artifact_reference(handoff_copy, config.run_root)
    rehearsal.ledger.record(
        "source_handoff_verified",
        "passed",
        {
            "artifact": handoff_reference,
            "access_snapshot_sha256": access_baseline["snapshot_sha256"],
            "scope_roles": list(HANDOFF_SCOPE_ROLES),
        },
    )

    runtime = rehearsal._verify_runtime_fingerprint(
        "proposal_runtime_fingerprint",
        env=base_env,
        establish_expected=True,
    )
    runtime_path = rehearsal.runtime_fingerprint_report_path
    if runtime_path is None:
        raise core.RehearsalError("proposal_runtime_fingerprint_authority_missing")
    rehearsal._command(
        "proposal_runtime_dependencies_check",
        [
            str(config.python_executable),
            "-E",
            "-s",
            "-B",
            "-X",
            "utf8",
            "-m",
            "pip",
            "check",
        ],
        env=base_env,
        blocked_code="runtime_dependencies_inconsistent",
    )

    media_copy = rehearsal.artifacts / "source-media-manifest.json"
    media_size, copied_media_sha256 = core._copy_stable(
        config.source_media_manifest,
        media_copy,
    )
    media_sha256, media_snapshot_id = core._source_media_manifest_identity(media_copy)
    handoff_media = handoff["source_media"]["manifest"]
    if (
        media_size != handoff_media["size"]
        or copied_media_sha256 != handoff_media["sha256"]
        or media_sha256 != handoff_media["sha256"]
        or media_snapshot_id != handoff_media["snapshot_id"]
    ):
        raise core.RehearsalBlocked("source_handoff_media_manifest_mismatch")

    rehearsal.source_expected_sha256 = core._hash_stable(config.source_database)[1]
    rehearsal.production_copy_read_performed = True
    source_size, source_sha256, source_identity = core._source_snapshot_checkpoint(
        config.source_database,
        expected_sha256=rehearsal.source_expected_sha256,
        expected_identity=None,
    )
    if (
        source_size != handoff_backup_set["database"]["size"]
        or source_sha256 != handoff_backup_set["database"]["sha256"]
    ):
        raise core.RehearsalBlocked("source_handoff_database_mismatch")
    source_directory = config.run_root / "work" / "source-input"
    source_directory.mkdir()
    private_database = source_directory / config.source_database.name
    private_checksum = source_directory / config.source_checksum.name
    private_metadata = source_directory / config.source_metadata.name
    copied_size, copied_sha256 = core._copy_stable(config.source_database, private_database)
    if copied_size != source_size or copied_sha256 != source_sha256:
        raise core.RehearsalBlocked("proposal_private_database_copy_mismatch")
    sidecar_checkpoints: dict[
        str, tuple[int, str, tuple[int, int, int, int, int]]
    ] = {}
    for label, source, destination in (
        ("checksum", config.source_checksum, private_checksum),
        ("metadata", config.source_metadata, private_metadata),
    ):
        size, digest, identity = core._regular_file_checkpoint(
            source, issue_prefix=f"source_{label}"
        )
        sidecar_checkpoints[label] = (size, digest, identity)
        if (
            size != handoff_backup_set[label]["size"]
            or digest != handoff_backup_set[label]["sha256"]
        ):
            raise core.RehearsalBlocked(f"source_handoff_{label}_mismatch")
        copied_sidecar_size, copied_sidecar_sha = core._copy_stable(source, destination)
        if copied_sidecar_size != size or copied_sidecar_sha != digest:
            raise core.RehearsalBlocked(f"proposal_private_{label}_copy_mismatch")
    core._source_snapshot_checkpoint(
        config.source_database,
        expected_sha256=source_sha256,
        expected_identity=source_identity,
    )
    for label, path in (
        ("checksum", config.source_checksum),
        ("metadata", config.source_metadata),
    ):
        expected = sidecar_checkpoints[label]
        core._regular_file_checkpoint(
            path,
            issue_prefix=f"source_{label}",
            expected_sha256=expected[1],
            expected_identity=expected[2],
        )
    rehearsal.source_initial_identity = source_identity
    rehearsal.source_sidecar_checkpoints = sidecar_checkpoints

    backup_report_path = rehearsal.evidence / "proposal-source-backup-set.json"
    rehearsal._command(
        "proposal_source_backup_verified",
        [
            str(config.python_executable), "-E", "-s", "-B", "-X", "utf8",
            rehearsal._python_tool("Verify-SQLiteBackupSet.py"),
            "--database", str(private_database),
            "--checksum", str(private_checksum),
            "--metadata", str(private_metadata),
            "--output", str(backup_report_path),
        ],
        env=base_env,
    )
    backup_report = core._validate_backup_report(backup_report_path)
    if backup_report["artifact"]["sha256"] != source_sha256:
        raise core.RehearsalBlocked("proposal_source_backup_sha256_mismatch")

    inspection_path = rehearsal.evidence / "proposal-source-inspection.json"
    rehearsal._command(
        "proposal_source_inspected",
        [
            str(config.python_executable), "-E", "-s", "-B", "-X", "utf8",
            rehearsal._python_tool("Inspect-SQLiteSnapshot.py"),
            "--database", str(private_database),
            "--expected-sha256", source_sha256,
            "--output", str(inspection_path),
        ],
        env=base_env,
    )
    _inspection, inspected_applied, source_sqlite_schema_sha256 = (
        core._validate_inspection_report(
            inspection_path,
            expected_sha256=source_sha256,
        )
    )
    work_database = config.run_root / "work" / "proposal-source.sqlite3"
    core._copy_stable(private_database, work_database)
    work_size, work_sha256, work_identity = core._source_snapshot_checkpoint(
        work_database,
        expected_sha256=source_sha256,
        expected_identity=None,
        issue_prefix="proposal_work_database",
    )
    if work_size != source_size or work_sha256 != source_sha256:
        raise core.RehearsalBlocked("proposal_work_database_copy_mismatch")

    state_path = rehearsal.evidence / "proposal-source-migration-state.json"
    rehearsal._command(
        "proposal_source_migration_state",
        [
            str(config.python_executable), "-E", "-s", "-B", "-X", "utf8",
            "-c", core.MIGRATION_STATE_SCRIPT, str(state_path),
        ],
        env=core._django_environment(config, config.run_root, work_database),
    )
    state = core._validate_migration_state(state_path)
    if state["applied"] != inspected_applied or state["unknown_applied_nodes"]:
        raise core.RehearsalBlocked("proposal_source_migration_state_mismatch")

    plan_argv, plan_env = rehearsal._manage(
        work_database,
        "migrate",
        "--plan",
        "--noinput",
        "--verbosity",
        "1",
    )
    plan_result = rehearsal._command(
        "proposal_migration_plan",
        plan_argv,
        env=plan_env,
    )
    plan_sha256 = core._hash_stable(plan_result.stdout_path)[1]

    review_plan_path = rehearsal.evidence / "proposal-migration-review-plan.json"
    rehearsal._command(
        "proposal_migration_review_plan",
        [
            str(config.python_executable), "-E", "-s", "-B", "-X", "utf8",
            "-c", MIGRATION_REVIEW_PLAN_SCRIPT, str(review_plan_path),
        ],
        env=core._django_environment(config, config.run_root, work_database),
    )
    review_plan = _validate_review_plan(review_plan_path)
    if (
        review_plan["source_applied_migrations"] != state["applied"]
        or review_plan["target_leaf_nodes"] != state["repository_leaf_nodes"]
    ):
        raise core.RehearsalBlocked("proposal_structured_plan_mismatch")
    if (
        state["applied_leaf_nodes"] != state["repository_leaf_nodes"]
        and not review_plan["pending_migration_nodes"]
    ):
        raise core.RehearsalBlocked("proposal_pending_migrations_missing")
    final_state_path = config.run_root / "work" / "proposal-migration-state-final.json"
    rehearsal._command(
        "proposal_source_migration_state_final",
        [
            str(config.python_executable), "-E", "-s", "-B", "-X", "utf8",
            "-c", core.MIGRATION_STATE_SCRIPT, str(final_state_path),
        ],
        env=core._django_environment(config, config.run_root, work_database),
    )
    final_state = core._validate_migration_state(final_state_path)
    if final_state != state:
        raise core.RehearsalBlocked("proposal_migration_state_changed_during_review")
    core._source_snapshot_checkpoint(
        work_database,
        expected_sha256=work_sha256,
        expected_identity=work_identity,
        issue_prefix="proposal_work_database",
    )
    rehearsal._verify_runtime_fingerprint(
        "proposal_runtime_fingerprint_final",
        env=base_env,
        full_content_hash=True,
    )

    evidence = {
        "bootstrap": core._artifact_reference(context["record_path"], config.run_root),
        "execution_inventory": core._artifact_reference(
            context["manifest_path"], config.run_root
        ),
        "migration_plan": core._artifact_reference(
            plan_result.stdout_path, config.run_root
        ),
        "migration_review_plan": core._artifact_reference(
            review_plan_path, config.run_root
        ),
        "runtime_fingerprint": core._artifact_reference(runtime_path, config.run_root),
        "source_backup_verification": core._artifact_reference(
            backup_report_path, config.run_root
        ),
        "source_handoff_manifest": handoff_reference,
        "source_media_manifest": core._artifact_reference(media_copy, config.run_root),
        "source_migration_state": core._artifact_reference(state_path, config.run_root),
        "source_snapshot_inspection": core._artifact_reference(
            inspection_path, config.run_root
        ),
    }
    if tuple(sorted(evidence)) != tuple(sorted(REQUIRED_EVIDENCE)):
        raise core.RehearsalError("proposal_evidence_set_invalid")
    evidence_set_sha256 = core._canonical_json_sha256(evidence)
    policy_projection = {
        "format": POLICY_FORMAT,
        "format_version": POLICY_VERSION,
        "policy_id": arguments.policy_id,
        "source_database_sha256": source_sha256,
        "source_media_manifest_sha256": media_sha256,
        "source_media_snapshot_id": media_snapshot_id,
        "source_applied_migrations_sha256": core._canonical_json_sha256(state["applied"]),
        "source_sqlite_schema_sha256": source_sqlite_schema_sha256,
        "migration_runtime_sha256": state["migration_runtime_sha256"],
        "runtime_fingerprint_sha256": runtime["fingerprint_sha256"],
        "execution_bundle_sha256": context["bundle_sha256"],
        "source_leaf_nodes": state["applied_leaf_nodes"],
        "target_leaf_nodes": state["repository_leaf_nodes"],
        "migration_plan_sha256": plan_sha256,
    }
    body = {
        "format": PROPOSAL_BODY_FORMAT,
        "format_version": PROPOSAL_BODY_VERSION,
        "proposal_id": arguments.proposal_id,
        "run_id": context["run_id"],
        "bootstrap_nonce": context["nonce"],
        "policy_projection": policy_projection,
        "evidence": evidence,
        "evidence_set_sha256": evidence_set_sha256,
        "review_requirements": {
            "lossless_review_status": "not_reviewed",
            "pending_migration_nodes": review_plan["pending_migration_nodes"],
            "required_evidence": list(REQUIRED_EVIDENCE),
        },
    }
    body_path = rehearsal.evidence / "policy-proposal-body.json"
    core._write_json_create_new(body_path, body)
    body_reference = core._artifact_reference(body_path, config.run_root)
    rehearsal.ledger.record(
        "policy_proposal_body_created",
        "passed",
        {
            "proposal_id": arguments.proposal_id,
            "run_id": context["run_id"],
            "body": body_reference,
            "evidence_set_sha256": evidence_set_sha256,
            "migration_applied": False,
            "review_required": True,
        },
    )
    original_media_sha, original_media_id = core._source_media_manifest_identity(
        config.source_media_manifest
    )
    if original_media_sha != media_sha256 or original_media_id != media_snapshot_id:
        raise core.RehearsalBlocked("source_media_manifest_changed_during_proposal")
    core._regular_file_checkpoint(
        source_handoff_manifest,
        issue_prefix="source_handoff_manifest",
        expected_sha256=handoff_sha256,
        expected_identity=handoff_identity,
    )
    try:
        final_handoff = handoff_core.load_handoff(source_handoff_manifest)
    except handoff_core.HandoffError as exc:
        raise core.RehearsalBlocked("source_handoff_manifest_changed") from exc
    if final_handoff != handoff:
        raise core.RehearsalBlocked("source_handoff_manifest_changed")
    final_access_baseline = _verify_live_handoff(
        final_handoff,
        config,
        source_handoff_manifest,
        context["repository_root"],
        verify_content=True,
    )
    if final_access_baseline != access_baseline:
        raise core.RehearsalBlocked("source_handoff_access_baseline_changed")
    core._regular_file_checkpoint(
        source_handoff_manifest,
        issue_prefix="source_handoff_manifest",
        expected_sha256=handoff_sha256,
        expected_identity=handoff_identity,
    )
    rehearsal.ledger.record(
        "source_handoff_final_verified",
        "passed",
        {
            "artifact": handoff_reference,
            "access_snapshot_sha256": final_access_baseline["snapshot_sha256"],
            "scope_roles": list(HANDOFF_SCOPE_ROLES),
            "content_verified": True,
        },
    )
    rehearsal._record_source_final()
    rehearsal.ledger.record(
        "review_required",
        "terminal",
        {
            "status": "review_required",
            "proposal_body_sha256": body_reference["sha256"],
            "lossless_reviewed": False,
            "migration_applied": False,
            "cutover_authorized": False,
            "contains_production_user_data": True,
            "retained_on_success": True,
            "secure_disposal_required": True,
            "sensitive_retention_scope": "entire_run_root",
        },
    )
    replay = rehearsal.ledger.verify_replay(expected_terminal="review_required")
    rehearsal.ledger.close()
    ledger_path = rehearsal.evidence / "events.jsonl"
    proposal = {
        "format": PROPOSAL_FORMAT,
        "format_version": PROPOSAL_VERSION,
        "generated_at": _utc_now(),
        "proposal_id": arguments.proposal_id,
        "run_id": context["run_id"],
        "bootstrap_nonce": context["nonce"],
        "state": "review_required",
        "body": body,
        "body_artifact": body_reference,
        "body_sha256": body_reference["sha256"],
        "ledger": {
            "artifact": core._artifact_reference(ledger_path, config.run_root),
            "event_count": replay["event_count"],
            "head_event_sha256": replay["head_event_sha256"],
            "terminal_status": "review_required",
        },
    }
    proposal_path = rehearsal.evidence / "policy-proposal.json"
    core._write_json_create_new(proposal_path, proposal)
    return proposal, proposal_path


def main() -> int:
    try:
        arguments = _parse_arguments()
        context = _inner_context()
        proposal, path = _execute_proposal(arguments, context)
    except KeyboardInterrupt:
        print("Production-copy policy proposal interrupted.", file=sys.stderr)
        return 130
    except (core.ConfigurationError, core.RehearsalError, OSError, ValueError) as exc:
        print(f"Production-copy policy proposal refused: {exc}", file=sys.stderr)
        return 1
    print(
        "Production-copy policy proposal requires human review; "
        f"proposal={path}; sha256={core._hash_stable(path)[1]}; "
        f"state={proposal['state']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
