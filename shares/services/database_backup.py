"""Create verified SQLite backup sets for Django and isolated legacy hosts.

When this file is executed directly with ``python -I -S``, it uses only the
standard library and does not import project settings, models, or migrations.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Iterable
from uuid import uuid4


BACKUP_METHOD = 'sqlite_backup_api'
BACKUP_METADATA_SCHEMA_VERSION = 1
PLACEHOLDER_APPLICATION_VERSIONS = frozenset({
    'unknown',
    'unset',
    'none',
    'null',
    'n/a',
    'na',
    'replace-me',
    'replace_with_deployed_release_id',
    'replace-with-deployed-release-id',
    'immutable-production-release-id',
    'head',
    'main',
    'master',
    'latest',
    'dev',
    'development',
})


class DatabaseBackupError(RuntimeError):
    pass


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_main_database_path(source: sqlite3.Connection) -> Path | None:
    rows = source.execute('PRAGMA database_list').fetchall()
    main_rows = [row for row in rows if len(row) >= 3 and row[1] == 'main']
    if len(main_rows) != 1:
        raise DatabaseBackupError(
            'Could not identify the active SQLite main database path.'
        )
    raw_path = main_rows[0][2]
    if raw_path == '':
        return None
    if not isinstance(raw_path, str):
        raise DatabaseBackupError(
            'The active SQLite main database path has an unexpected type.'
        )
    return Path(raw_path).expanduser().resolve()


def _sqlite_artifact_paths(database_path: Path) -> tuple[Path, ...]:
    return (
        database_path,
        database_path.with_name(f'{database_path.name}-wal'),
        database_path.with_name(f'{database_path.name}-shm'),
        database_path.with_name(f'{database_path.name}-journal'),
    )


def _write_staged_text(path: Path, content: str) -> Path:
    temporary = path.with_name(f'.{path.name}.tmp-{uuid4().hex}')
    try:
        with temporary.open('w', encoding='utf-8', newline='\n') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _restore_output_set(
    published: list[tuple[Path, Path]],
    displaced: list[tuple[Path, Path]],
) -> list[str]:
    errors: list[str] = []
    for staged, destination in reversed(published):
        try:
            if destination.exists():
                os.replace(destination, staged)
        except OSError:
            errors.append(destination.name)
    for destination, rollback in reversed(displaced):
        try:
            if rollback.exists():
                os.replace(rollback, destination)
        except OSError:
            errors.append(destination.name)
    return errors


def _new_output_set_error() -> str:
    return (
        'One or more backup outputs already exist. Preserve the entire output '
        'directory because it may contain partial or concurrent evidence, then '
        'choose a completely new output path.'
    )


def _publish_new_output_set(output_set: tuple[tuple[Path, Path], ...]) -> None:
    for staged, destination in output_set:
        try:
            os.link(staged, destination)
        except FileExistsError as exc:
            raise DatabaseBackupError(_new_output_set_error()) from exc

    for staged, _destination in output_set:
        staged.unlink()


def _publish_output_set(
    outputs: Iterable[tuple[Path, Path]],
    *,
    overwrite: bool,
) -> None:
    output_set = tuple(outputs)
    if not overwrite:
        _publish_new_output_set(output_set)
        return

    displaced: list[tuple[Path, Path]] = []
    published: list[tuple[Path, Path]] = []
    succeeded = False
    try:
        for _staged, destination in output_set:
            if not destination.exists():
                continue
            rollback = destination.with_name(
                f'.{destination.name}.rollback-{uuid4().hex}'
            )
            os.replace(destination, rollback)
            displaced.append((destination, rollback))

        for staged, destination in output_set:
            os.replace(staged, destination)
            published.append((staged, destination))
        succeeded = True
    except Exception as exc:
        rollback_errors = _restore_output_set(published, displaced)
        if rollback_errors:
            names = ', '.join(sorted(set(rollback_errors)))
            raise DatabaseBackupError(
                'Backup publication failed and automatic rollback was incomplete '
                f'for: {names}.'
            ) from exc
        raise
    finally:
        if succeeded:
            for _destination, rollback in displaced:
                rollback.unlink(missing_ok=True)


def _metadata_payload(
    *,
    digest: str,
    size: int,
    application_version: str,
) -> dict[str, object]:
    return {
        'schema_version': BACKUP_METADATA_SCHEMA_VERSION,
        'generated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'backup_method': BACKUP_METHOD,
        'database_vendor': 'sqlite',
        'application_version': application_version,
        'sha256': digest,
        'size': size,
        'integrity_check': 'ok',
        'foreign_key_check': 'ok',
    }


def _backup_sqlite_connection(
    source: sqlite3.Connection,
    output_file: str | Path,
    *,
    overwrite: bool = False,
    application_version: str,
) -> dict[str, object]:
    output = Path(output_file).expanduser().resolve()
    checksum_path = output.with_name(f'{output.name}.sha256')
    metadata_path = output.with_name(f'{output.name}.metadata.json')
    output_paths = (output, checksum_path, metadata_path)
    if any(path.exists() for path in output_paths) and not overwrite:
        raise DatabaseBackupError(_new_output_set_error())

    if not hasattr(source, 'backup'):
        raise DatabaseBackupError('The active SQLite connection cannot create backups.')
    source_path = _sqlite_main_database_path(source)
    if source_path is not None:
        protected_source_paths = set(_sqlite_artifact_paths(source_path))
        if any(path in protected_source_paths for path in output_paths):
            raise DatabaseBackupError(
                'Backup outputs must differ from the live SQLite database and '
                'its journal sidecars.'
            )
    output.parent.mkdir(parents=True, exist_ok=True)

    temporary = output.with_name(f'.{output.name}.tmp-{uuid4().hex}')
    staged_checksum: Path | None = None
    staged_metadata: Path | None = None
    destination = None
    try:
        destination = sqlite3.connect(temporary)
        source.backup(destination, pages=1000, sleep=0.05)
        integrity_rows = destination.execute('PRAGMA integrity_check').fetchall()
        if integrity_rows != [('ok',)]:
            raise DatabaseBackupError(
                f'Backup integrity check failed: {integrity_rows!r}'
            )
        foreign_key_violation = destination.execute(
            'PRAGMA foreign_key_check'
        ).fetchone()
        if foreign_key_violation is not None:
            raise DatabaseBackupError(
                'Backup foreign key check failed; one or more violations were found.'
            )
        destination.close()
        destination = None

        digest = _file_sha256(temporary)
        size = temporary.stat().st_size
        metadata = _metadata_payload(
            digest=digest,
            size=size,
            application_version=application_version,
        )
        staged_checksum = _write_staged_text(
            checksum_path,
            f'{digest}  {output.name}\n',
        )
        staged_metadata = _write_staged_text(
            metadata_path,
            json.dumps(
                metadata,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + '\n',
        )
        _publish_output_set(
            (
                (temporary, output),
                (staged_checksum, checksum_path),
                (staged_metadata, metadata_path),
            ),
            overwrite=overwrite,
        )
        return {
            'path': str(output),
            'checksum_path': str(checksum_path),
            'metadata_path': str(metadata_path),
            'sha256': digest,
            'size': size,
        }
    finally:
        cleanup_errors: list[str] = []
        if destination is not None:
            try:
                destination.close()
            except sqlite3.Error:
                cleanup_errors.append(temporary.name)
        cleanup_paths = list(_sqlite_artifact_paths(temporary))
        if staged_checksum is not None:
            cleanup_paths.append(staged_checksum)
        if staged_metadata is not None:
            cleanup_paths.append(staged_metadata)
        for cleanup_path in cleanup_paths:
            try:
                cleanup_path.unlink(missing_ok=True)
            except OSError:
                cleanup_errors.append(cleanup_path.name)
        if cleanup_errors:
            names = ', '.join(sorted(set(cleanup_errors)))
            raise DatabaseBackupError(
                f'Backup cleanup left sensitive temporary files: {names}.'
            )


def backup_sqlite_database(
    output_file: str | Path,
    *,
    overwrite: bool = False,
    database_alias: str = 'default',
) -> dict[str, object]:
    from django.db import connections

    database = connections[database_alias]
    if database.vendor != 'sqlite':
        raise DatabaseBackupError(
            'This command only supports SQLite. Use pg_dump for PostgreSQL.'
        )

    database.ensure_connection()
    source = database.connection
    if source is None:
        raise DatabaseBackupError('The active SQLite connection cannot create backups.')
    return _backup_sqlite_connection(
        source,
        output_file,
        overwrite=overwrite,
        application_version=os.environ.get('APP_VERSION', 'unknown'),
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def backup_sqlite_path(
    source_database: str | Path,
    output_file: str | Path,
    *,
    application_version: str,
) -> dict[str, object]:
    source_input = Path(source_database).expanduser()
    output_input = Path(output_file).expanduser()
    if not source_input.is_absolute() or not output_input.is_absolute():
        raise DatabaseBackupError(
            'Source database and output file must use absolute paths.'
        )
    if os.name == 'nt' and (
        source_input.anchor.startswith('\\\\')
        or output_input.anchor.startswith('\\\\')
    ):
        raise DatabaseBackupError(
            'Source database and output file must use local, non-UNC paths.'
        )
    if source_input.is_symlink():
        raise DatabaseBackupError(
            'Source database must be the original file, not a symbolic link.'
        )

    source_path = source_input.resolve()
    output_path = output_input.resolve()
    if not source_path.is_file():
        raise DatabaseBackupError('Source database is not an existing regular file.')
    if source_path.stat().st_nlink != 1:
        raise DatabaseBackupError(
            'Source database must be the original single-link file; hard-link '
            'aliases can omit live SQLite journal data.'
        )
    if _is_relative_to(output_path, source_path.parent):
        raise DatabaseBackupError(
            'The sidecar backup output must be outside the live database directory.'
        )

    release = application_version.strip()
    if (
        not release
        or len(release) > 255
        or release.casefold() in PLACEHOLDER_APPLICATION_VERSIONS
        or any(ord(character) < 32 or ord(character) == 127 for character in release)
    ):
        raise DatabaseBackupError(
            'A real immutable application version is required for the backup metadata.'
        )

    source = sqlite3.connect(
        f'{source_path.as_uri()}?mode=ro',
        uri=True,
        timeout=30.0,
    )
    try:
        source.execute('PRAGMA query_only = ON')
        return _backup_sqlite_connection(
            source,
            output_path,
            application_version=release,
        )
    finally:
        source.close()


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            'Create a read-only SQLite Backup API snapshot without loading '
            'Django settings, models, or migrations.'
        ),
    )
    parser.add_argument('source_database')
    parser.add_argument('output_file')
    parser.add_argument('--application-version', required=True)
    options = parser.parse_args(argv)

    try:
        result = backup_sqlite_path(
            options.source_database,
            options.output_file,
            application_version=options.application_version,
        )
    except (DatabaseBackupError, OSError, sqlite3.Error) as exc:
        print(f'Backup failed: {exc}', file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(_main())
