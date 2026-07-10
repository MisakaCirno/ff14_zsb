from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from django.db import connections


class DatabaseBackupError(RuntimeError):
    pass


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _write_text_atomic(path: Path, content: str) -> None:
    temporary = path.with_name(f'.{path.name}.tmp-{uuid4().hex}')
    try:
        temporary.write_text(content, encoding='utf-8', newline='\n')
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


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
    if (output.exists() or checksum_path.exists()) and not overwrite:
        raise DatabaseBackupError(
            f'Backup output already exists: {output}. Use --overwrite explicitly.'
        )
    output.parent.mkdir(parents=True, exist_ok=True)

    configured_name = str(database.settings_dict['NAME'])
    if not configured_name.startswith('file:') and configured_name != ':memory:':
        source_path = Path(configured_name).expanduser().resolve()
        if output == source_path:
            raise DatabaseBackupError('Backup output must differ from the live database path.')

    temporary = output.with_name(f'.{output.name}.tmp-{uuid4().hex}')
    destination = None
    try:
        database.ensure_connection()
        source = database.connection
        if source is None or not hasattr(source, 'backup'):
            raise DatabaseBackupError('The active SQLite connection cannot create backups.')
        destination = sqlite3.connect(temporary)
        source.backup(destination, pages=1000, sleep=0.05)
        integrity_rows = destination.execute('PRAGMA integrity_check').fetchall()
        if integrity_rows != [('ok',)]:
            raise DatabaseBackupError(
                f'Backup integrity check failed: {integrity_rows!r}'
            )
        destination.close()
        destination = None

        digest = _file_sha256(temporary)
        size = temporary.stat().st_size
        os.replace(temporary, output)
        _write_text_atomic(checksum_path, f'{digest}  {output.name}\n')
        return {
            'path': str(output),
            'checksum_path': str(checksum_path),
            'sha256': digest,
            'size': size,
        }
    finally:
        if destination is not None:
            destination.close()
        if temporary.exists():
            temporary.unlink()
