from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3

from django.db.migrations.loader import MigrationLoader


REPORT_FORMAT = 'ffxivshare-deployment-schema-status'
REPORT_FORMAT_VERSION = 1
SQLITE_SIDECAR_SUFFIXES = ('-wal', '-shm', '-journal')


class DeploymentSchemaInspectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class FileSnapshot:
    size: int
    modified_ns: int
    link_count: int


def _snapshot_regular_file(path: Path) -> FileSnapshot:
    try:
        metadata = path.stat()
    except OSError as exc:
        raise DeploymentSchemaInspectionError(
            'The SQLite database could not be inspected.'
        ) from exc
    if not path.is_file() or path.is_symlink() or metadata.st_nlink != 1:
        raise DeploymentSchemaInspectionError(
            'The SQLite database must be one regular, non-linked file.'
        )
    return FileSnapshot(
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        link_count=metadata.st_nlink,
    )


def _migration_plan(loader: MigrationLoader):
    ordered = []
    seen = set()
    leaf_nodes = sorted(loader.graph.leaf_nodes())
    for target in leaf_nodes:
        for migration in loader.graph.forwards_plan(target):
            if migration not in seen:
                seen.add(migration)
                ordered.append(migration)
    return leaf_nodes, ordered


def _read_applied_migrations(database_path: Path):
    database_uri = f'{database_path.as_uri()}?mode=ro&immutable=1'
    connection = None
    try:
        connection = sqlite3.connect(database_uri, uri=True)
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",
            ('django_migrations',),
        ).fetchone()
        if table_exists is None:
            return set()
        return {
            (str(app), str(name))
            for app, name in connection.execute(
                'SELECT app, name FROM django_migrations'
            )
        }
    except sqlite3.Error as exc:
        raise DeploymentSchemaInspectionError(
            'The SQLite migration history could not be read safely.'
        ) from exc
    finally:
        if connection is not None:
            try:
                connection.rollback()
            finally:
                connection.close()


def inspect_sqlite_deployment_schema(database_path, *, loader=None):
    candidate = Path(database_path).expanduser()
    if candidate.is_symlink():
        raise DeploymentSchemaInspectionError(
            'The SQLite database path must not be a symbolic link.'
        )
    path = candidate.resolve()
    initial_snapshot = _snapshot_regular_file(path)
    sidecars = [
        str(Path(f'{path}{suffix}'))
        for suffix in SQLITE_SIDECAR_SUFFIXES
        if Path(f'{path}{suffix}').exists()
    ]
    if sidecars:
        raise DeploymentSchemaInspectionError(
            'SQLite sidecars are present. Stop all writers and resolve the '
            'database state before checking or starting the application.'
        )

    migration_loader = loader or MigrationLoader(
        None,
        ignore_no_migrations=True,
    )
    leaf_nodes, expected_order = _migration_plan(migration_loader)
    expected = set(expected_order)
    applied = _read_applied_migrations(path)

    pending = [migration for migration in expected_order if migration not in applied]
    unknown = sorted(applied - expected)
    dependency_gaps = []
    for migration in sorted(applied & expected):
        node = migration_loader.graph.node_map[migration]
        for parent in sorted(parent.key for parent in node.parents):
            if parent in expected and parent not in applied:
                dependency_gaps.append({
                    'migration': list(migration),
                    'missing_parent': list(parent),
                })

    final_snapshot = _snapshot_regular_file(path)
    if final_snapshot != initial_snapshot:
        raise DeploymentSchemaInspectionError(
            'The SQLite database changed during the read-only schema check.'
        )
    if any(Path(f'{path}{suffix}').exists() for suffix in SQLITE_SIDECAR_SUFFIXES):
        raise DeploymentSchemaInspectionError(
            'A SQLite sidecar appeared during the read-only schema check.'
        )

    invalid = bool(unknown or dependency_gaps)
    schema_current = not invalid and not pending
    status = (
        'invalid_history'
        if invalid
        else 'current'
        if schema_current
        else 'upgrade_required'
    )
    return {
        'format': REPORT_FORMAT,
        'format_version': REPORT_FORMAT_VERSION,
        'status': status,
        'read_only': True,
        'database_unchanged': True,
        'cutover_authorized': False,
        'database_path': str(path),
        'database_size': initial_snapshot.size,
        'schema_current': schema_current,
        'upgrade_required': status == 'upgrade_required',
        'safe_to_start': schema_current,
        'leaf_nodes': [list(node) for node in leaf_nodes],
        'expected_migration_count': len(expected),
        'applied_migration_count': len(applied),
        'pending_migrations': [list(node) for node in pending],
        'unknown_applied_migrations': [list(node) for node in unknown],
        'dependency_gaps': dependency_gaps,
    }
