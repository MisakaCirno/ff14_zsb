from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from typing import Iterable
from uuid import uuid4

from django.db import connections


BACKUP_METHOD = 'sqlite_backup_api'
BACKUP_METADATA_SCHEMA_VERSION = 1


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


def _publish_output_set(
    outputs: Iterable[tuple[Path, Path]],
    *,
    overwrite: bool,
) -> None:
    output_set = tuple(outputs)
    displaced: list[tuple[Path, Path]] = []
    published: list[tuple[Path, Path]] = []
    succeeded = False
    try:
        for _staged, destination in output_set:
            if destination.exists() and not overwrite:
                raise DatabaseBackupError(
                    'A backup output appeared while the backup was being created. '
                    'Use --overwrite explicitly after reviewing it.'
                )

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


def _metadata_payload(*, digest: str, size: int) -> dict[str, object]:
    return {
        'schema_version': BACKUP_METADATA_SCHEMA_VERSION,
        'generated_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        'backup_method': BACKUP_METHOD,
        'database_vendor': 'sqlite',
        'application_version': os.environ.get('APP_VERSION', 'unknown'),
        'sha256': digest,
        'size': size,
        'integrity_check': 'ok',
        'foreign_key_check': 'ok',
    }


def backup_sqlite_database(
    output_file: str | Path,
    *,
    overwrite: bool = False,
    database_alias: str = 'default',
) -> dict[str, object]:
    database = connections[database_alias]
    if database.vendor != 'sqlite':
        raise DatabaseBackupError(
            'This command only supports SQLite. Use pg_dump for PostgreSQL.'
        )

    output = Path(output_file).expanduser().resolve()
    checksum_path = output.with_name(f'{output.name}.sha256')
    metadata_path = output.with_name(f'{output.name}.metadata.json')
    output_paths = (output, checksum_path, metadata_path)
    if any(path.exists() for path in output_paths) and not overwrite:
        raise DatabaseBackupError(
            'One or more backup outputs already exist. '
            'Use --overwrite explicitly after reviewing all three files.'
        )

    database.ensure_connection()
    source = database.connection
    if source is None or not hasattr(source, 'backup'):
        raise DatabaseBackupError('The active SQLite connection cannot create backups.')
    source_path = _sqlite_main_database_path(source)
    if source_path is not None:
        protected_source_paths = {
            source_path,
            source_path.with_name(f'{source_path.name}-wal'),
            source_path.with_name(f'{source_path.name}-shm'),
            source_path.with_name(f'{source_path.name}-journal'),
        }
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
        metadata = _metadata_payload(digest=digest, size=size)
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
        if destination is not None:
            destination.close()
        if temporary.exists():
            temporary.unlink()
        if staged_checksum is not None and staged_checksum.exists():
            staged_checksum.unlink()
        if staged_metadata is not None and staged_metadata.exists():
            staged_metadata.unlink()
