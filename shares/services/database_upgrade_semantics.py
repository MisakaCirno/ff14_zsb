"""Fail-closed semantic comparison for direct SQLite upgrades.

The direct Git deployment migrates a private copy before replacing the live
database.  This module proves that every pre-existing row and value survived
that migration and permits only the historical, explicitly reviewed backfills
needed between the production 0018 schema and the current schema.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any


class DatabaseUpgradeSemanticError(RuntimeError):
    pass


PERMISSION_NAME_CHANGES = {
    'add_announcement': ('Can add 公告', 'Can add 站点动态'),
    'change_announcement': ('Can change 公告', 'Can change 站点动态'),
    'delete_announcement': ('Can delete 公告', 'Can delete 站点动态'),
    'view_announcement': ('Can view 公告', 'Can view 站点动态'),
}

SITE_MESSAGE_PERMISSIONS = {
    'add_sitemessage': 'Can add 站内信',
    'change_sitemessage': 'Can change 站内信',
    'delete_sitemessage': 'Can delete 站内信',
    'view_sitemessage': 'Can view 站内信',
}

LEGACY_PRIVATE_DECISIONS = {
    '2k5d2w5w': (
        'confirm_restriction',
        'R19 历史状态人工复核：确认为历史下架并维持内容限制；'
        '依据为旧管理后台的可见性修改与疑似重复标记。',
    ),
    '4s2v4e9n': (
        'release_restriction',
        'R19 历史状态人工复核：确认为作者主动私密，解除迁移保护限制；'
        '可见性仍保持私有。',
    ),
    '8b8y9s3j': (
        'release_restriction',
        'R19 历史状态人工复核：确认为作者主动私密，解除迁移保护限制；'
        '可见性仍保持私有。',
    ),
    '8n9b6e6b': (
        'release_restriction',
        'R19 历史状态人工复核：确认为作者主动私密，解除迁移保护限制；'
        '可见性仍保持私有。',
    ),
}

ALLOWED_CANDIDATE_ONLY_TABLES = {'shares_sitemessage'}
ALLOWED_EXTRA_ROW_TABLES = {
    'auth_permission',
    'django_content_type',
    'django_migrations',
    'shares_sharelog',
    'shares_userprofile',
}


@dataclass(frozen=True)
class TableSnapshot:
    columns: tuple[str, ...]
    primary_key: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]


def _quote(identifier: str) -> str:
    return f'"{identifier.replace(chr(34), chr(34) * 2)}"'


def _database_uri(path: Path) -> str:
    return f'{path.as_uri()}?mode=ro&immutable=1'


def _open_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(_database_uri(path), uri=True)
    connection.execute('PRAGMA query_only = ON')
    return connection


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_database(path: str | Path, *, label: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise DatabaseUpgradeSemanticError(
            f'{label} must be an existing regular, non-symlink file.'
        )
    sidecars = [
        Path(f'{resolved}{suffix}')
        for suffix in ('-wal', '-shm', '-journal')
        if Path(f'{resolved}{suffix}').exists()
    ]
    if sidecars:
        raise DatabaseUpgradeSemanticError(
            f'{label} has SQLite sidecars; refuse an ambiguous comparison.'
        )
    return resolved


def _table_names(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    )


def _table_snapshot(
    connection: sqlite3.Connection,
    table: str,
) -> TableSnapshot:
    info = connection.execute(
        f'PRAGMA table_xinfo({_quote(table)})'
    ).fetchall()
    visible = [row for row in info if len(row) < 7 or row[6] == 0]
    columns = tuple(row[1] for row in visible)
    primary_key = tuple(
        row[1]
        for row in sorted(visible, key=lambda value: value[5])
        if row[5]
    )
    projection = ', '.join(_quote(column) for column in columns)
    rows = tuple(connection.execute(
        f'SELECT {projection} FROM {_quote(table)}'
    ).fetchall())
    return TableSnapshot(columns, primary_key, rows)


def _row_dict(snapshot: TableSnapshot, row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(snapshot.columns, row))


def _key(snapshot: TableSnapshot, row: tuple[Any, ...]) -> tuple[Any, ...]:
    values = _row_dict(snapshot, row)
    if snapshot.primary_key:
        return tuple(values[column] for column in snapshot.primary_key)
    return row


def _canonical_digest(rows: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        default=lambda value: {'bytes_sha256': sha256(value).hexdigest()},
    ).encode('utf-8')
    return sha256(encoded).hexdigest()


def _normalize_candidate_existing_row(
    table: str,
    source_row: dict[str, Any],
    candidate_row: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(candidate_row)
    if table == 'auth_permission':
        change = PERMISSION_NAME_CHANGES.get(source_row.get('codename'))
        if change and source_row.get('name') == change[0]:
            if normalized.get('name') != change[1]:
                raise DatabaseUpgradeSemanticError(
                    'An announcement permission did not reach its reviewed name.'
                )
            normalized['name'] = change[0]
    return normalized


def _compare_existing_rows(
    table: str,
    source: TableSnapshot,
    candidate: TableSnapshot,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    missing_columns = [
        column for column in source.columns if column not in candidate.columns
    ]
    if missing_columns:
        raise DatabaseUpgradeSemanticError(
            f'{table} lost pre-existing columns: {missing_columns!r}'
        )
    common_columns = source.columns
    candidate_indexes = [candidate.columns.index(column) for column in common_columns]
    projected_candidate = tuple(
        tuple(row[index] for index in candidate_indexes)
        for row in candidate.rows
    )

    if source.primary_key:
        source_by_key = {_key(source, row): row for row in source.rows}
        candidate_projection = TableSnapshot(
            common_columns,
            source.primary_key,
            projected_candidate,
        )
        candidate_by_key = {
            _key(candidate_projection, row): row for row in projected_candidate
        }
        if len(source_by_key) != len(source.rows):
            raise DatabaseUpgradeSemanticError(
                f'{table} source primary keys are not unique.'
            )
        missing_keys = set(source_by_key) - set(candidate_by_key)
        if missing_keys:
            raise DatabaseUpgradeSemanticError(
                f'{table} lost {len(missing_keys)} pre-existing row(s).'
            )
        for key, source_tuple in source_by_key.items():
            source_dict = _row_dict(source, source_tuple)
            candidate_dict = _row_dict(
                candidate_projection,
                candidate_by_key[key],
            )
            normalized = _normalize_candidate_existing_row(
                table,
                source_dict,
                candidate_dict,
            )
            if normalized != source_dict:
                raise DatabaseUpgradeSemanticError(
                    f'{table} changed a pre-existing row outside the allowlist.'
                )
        extra_keys = set(candidate_by_key) - set(source_by_key)
        extra_rows = [
            _row_dict(candidate_projection, candidate_by_key[key])
            for key in sorted(extra_keys, key=repr)
        ]
    else:
        source_counter = Counter(source.rows)
        candidate_counter = Counter(projected_candidate)
        missing = source_counter - candidate_counter
        if missing:
            raise DatabaseUpgradeSemanticError(
                f'{table} lost or changed {sum(missing.values())} pre-existing row(s).'
            )
        extras = candidate_counter - source_counter
        extra_rows = [
            _row_dict(source, row)
            for row, count in extras.items()
            for _item in range(count)
        ]

    source_rows = [_row_dict(source, row) for row in source.rows]
    return source_rows, extra_rows


def _expected_profile_backfills(
    source_tables: dict[str, TableSnapshot],
) -> dict[int, dict[str, Any]]:
    users = source_tables.get('auth_user')
    profiles = source_tables.get('shares_userprofile')
    if users is None or profiles is None:
        return {}
    profile_user_ids = {
        _row_dict(profiles, row)['user_id'] for row in profiles.rows
    }
    expected = {}
    for row in users.rows:
        user = _row_dict(users, row)
        if user['id'] not in profile_user_ids:
            expected[user['id']] = user
    return expected


def _validate_profile_extras(
    extras: list[dict[str, Any]],
    source_tables: dict[str, TableSnapshot],
) -> None:
    expected = _expected_profile_backfills(source_tables)
    if {row.get('user_id') for row in extras} != set(expected):
        raise DatabaseUpgradeSemanticError(
            'UserProfile backfill rows differ from the users missing profiles.'
        )
    for row in extras:
        user = expected[row['user_id']]
        required = {
            'nickname': '',
            'bio': '',
            'home_feed_mode': 'infinite',
            'created_at': user['date_joined'],
            'updated_at': user['date_joined'],
        }
        for field, value in required.items():
            if field in row and row[field] != value:
                raise DatabaseUpgradeSemanticError(
                    'A generated UserProfile row has unexpected business data.'
                )


def _validate_content_type_extras(extras: list[dict[str, Any]]) -> None:
    natural_keys = {(row.get('app_label'), row.get('model')) for row in extras}
    if natural_keys not in (set(), {('shares', 'sitemessage')}):
        raise DatabaseUpgradeSemanticError(
            'Unexpected content types were created during the database upgrade.'
        )


def _validate_permission_extras(
    extras: list[dict[str, Any]],
    candidate_tables: dict[str, TableSnapshot],
) -> None:
    content_types = candidate_tables['django_content_type']
    by_id = {
        _row_dict(content_types, row)['id']: _row_dict(content_types, row)
        for row in content_types.rows
    }
    observed = {}
    for row in extras:
        content_type = by_id.get(row.get('content_type_id'))
        if not content_type or (
            content_type.get('app_label'), content_type.get('model')
        ) != ('shares', 'sitemessage'):
            raise DatabaseUpgradeSemanticError(
                'Unexpected permission rows were created during the upgrade.'
            )
        observed[row.get('codename')] = row.get('name')
    if observed not in ({}, SITE_MESSAGE_PERMISSIONS):
        raise DatabaseUpgradeSemanticError(
            'SiteMessage permission backfill differs from the reviewed values.'
        )


def _validate_migration_extras(extras: list[dict[str, Any]]) -> None:
    names = [(row.get('app'), row.get('name')) for row in extras]
    if len(names) != len(set(names)) or any(
        not isinstance(app, str) or not isinstance(name, str) or not app or not name
        for app, name in names
    ):
        raise DatabaseUpgradeSemanticError(
            'New migration-history rows are malformed or duplicated.'
        )


def _validate_share_log_extras(
    extras: list[dict[str, Any]],
    candidate_tables: dict[str, TableSnapshot],
) -> None:
    shares = candidate_tables['shares_share']
    share_by_pk = {
        _row_dict(shares, row)['id']: _row_dict(shares, row)
        for row in shares.rows
    }
    observed = {}
    for row in extras:
        share = share_by_pk.get(row.get('share_id'))
        share_id = share.get('share_id') if share else None
        decision = LEGACY_PRIVATE_DECISIONS.get(share_id)
        if (
            decision is None
            or row.get('user_id') is not None
            or row.get('action') != decision[0]
            or row.get('details') != decision[1]
            or row.get('created_at') != share.get('updated_at')
            or share_id in observed
        ):
            raise DatabaseUpgradeSemanticError(
                'Unexpected ShareLog rows were created during the upgrade.'
            )
        observed[share_id] = row


def _validate_extra_rows(
    table: str,
    extras: list[dict[str, Any]],
    source_tables: dict[str, TableSnapshot],
    candidate_tables: dict[str, TableSnapshot],
) -> None:
    if not extras:
        return
    if table not in ALLOWED_EXTRA_ROW_TABLES:
        raise DatabaseUpgradeSemanticError(
            f'{table} gained {len(extras)} unexpected row(s).'
        )
    validators = {
        'auth_permission': lambda rows: _validate_permission_extras(
            rows, candidate_tables
        ),
        'django_content_type': _validate_content_type_extras,
        'django_migrations': _validate_migration_extras,
        'shares_sharelog': lambda rows: _validate_share_log_extras(
            rows, candidate_tables
        ),
        'shares_userprofile': lambda rows: _validate_profile_extras(
            rows, source_tables
        ),
    }
    validators[table](extras)


def _validate_new_columns(
    source_tables: dict[str, TableSnapshot],
    candidate_tables: dict[str, TableSnapshot],
) -> None:
    expected_defaults = {
        'shares_report': {
            'resolution_reason': '',
        },
        'shares_share': {
            'review_feedback': '',
            'reviewed_at': None,
            'reviewed_by_id': None,
            'deleted_at': None,
            'deleted_by_id': None,
            'deletion_origin': '',
            'deletion_reason': '',
        },
        'shares_collection': {
            'deleted_at': None,
            'deleted_by_id': None,
            'deletion_reason': '',
        },
    }
    for table, fields in expected_defaults.items():
        source = source_tables.get(table)
        candidate = candidate_tables.get(table)
        if source is None or candidate is None:
            continue
        new_fields = set(candidate.columns) - set(source.columns)
        relevant = new_fields & set(fields)
        if not relevant:
            continue
        for row in candidate.rows:
            values = _row_dict(candidate, row)
            for field in relevant:
                if values[field] != fields[field]:
                    raise DatabaseUpgradeSemanticError(
                        f'{table}.{field} has an unexpected migration value.'
                    )


def _validate_sequence_floors(
    source: sqlite3.Connection,
    candidate: sqlite3.Connection,
) -> dict[str, int]:
    def read(connection):
        present = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' "
            "AND name='sqlite_sequence'"
        ).fetchone()
        if not present:
            return {}
        rows = connection.execute(
            'SELECT name, seq FROM sqlite_sequence ORDER BY name'
        ).fetchall()
        if len(rows) != len({row[0] for row in rows}):
            raise DatabaseUpgradeSemanticError(
                'sqlite_sequence contains duplicate table names.'
            )
        return dict(rows)

    source_sequences = read(source)
    candidate_sequences = read(candidate)
    for table, floor in source_sequences.items():
        candidate_floor = candidate_sequences.get(table)
        if candidate_floor is None or candidate_floor < floor:
            raise DatabaseUpgradeSemanticError(
                f'SQLite sequence floor was lost for {table}.'
            )
    return candidate_sequences


def compare_sqlite_upgrade(
    source_database: str | Path,
    candidate_database: str | Path,
) -> dict[str, Any]:
    source_path = _validate_database(source_database, label='source database')
    candidate_path = _validate_database(candidate_database, label='candidate database')
    if source_path == candidate_path:
        raise DatabaseUpgradeSemanticError(
            'Source and candidate database paths must differ.'
        )

    with closing(_open_database(source_path)) as source, closing(
        _open_database(candidate_path)
    ) as candidate:
        for label, connection in (('source', source), ('candidate', candidate)):
            integrity = connection.execute('PRAGMA integrity_check').fetchall()
            if integrity != [('ok',)]:
                raise DatabaseUpgradeSemanticError(
                    f'{label} database integrity check failed.'
                )
            if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise DatabaseUpgradeSemanticError(
                    f'{label} database foreign-key check failed.'
                )

        source_names = set(_table_names(source))
        candidate_names = set(_table_names(candidate))
        missing_tables = source_names - candidate_names
        if missing_tables:
            raise DatabaseUpgradeSemanticError(
                f'Candidate lost source tables: {sorted(missing_tables)!r}'
            )
        unexpected_new = (
            candidate_names - source_names - ALLOWED_CANDIDATE_ONLY_TABLES
        )
        if unexpected_new:
            raise DatabaseUpgradeSemanticError(
                f'Candidate gained unexpected tables: {sorted(unexpected_new)!r}'
            )

        source_tables = {
            table: _table_snapshot(source, table) for table in source_names
        }
        candidate_tables = {
            table: _table_snapshot(candidate, table) for table in candidate_names
        }
        table_reports = {}
        for table in sorted(source_names):
            source_rows, extras = _compare_existing_rows(
                table,
                source_tables[table],
                candidate_tables[table],
            )
            _validate_extra_rows(
                table,
                extras,
                source_tables,
                candidate_tables,
            )
            table_reports[table] = {
                'source_rows': len(source_rows),
                'candidate_rows': len(candidate_tables[table].rows),
                'allowed_added_rows': len(extras),
                'preserved_projection_sha256': _canonical_digest(source_rows),
            }

        for table in sorted(candidate_names - source_names):
            row_count = len(candidate_tables[table].rows)
            if row_count:
                raise DatabaseUpgradeSemanticError(
                    f'New table {table} unexpectedly contains {row_count} row(s).'
                )
            table_reports[table] = {
                'source_rows': 0,
                'candidate_rows': 0,
                'allowed_added_rows': 0,
                'preserved_projection_sha256': _canonical_digest([]),
            }

        _validate_new_columns(source_tables, candidate_tables)
        sequences = _validate_sequence_floors(source, candidate)

    return {
        'format': 'ffxivshare-sqlite-upgrade-semantic-comparison',
        'format_version': 1,
        'status': 'passed',
        'source': {
            'sha256': _file_sha256(source_path),
            'size': source_path.stat().st_size,
        },
        'candidate': {
            'sha256': _file_sha256(candidate_path),
            'size': candidate_path.stat().st_size,
        },
        'checks': {
            'integrity': True,
            'foreign_keys': True,
            'source_tables_preserved': True,
            'preexisting_rows_preserved': True,
            'new_rows_allowlisted': True,
            'new_columns_validated': True,
            'sequence_floors_preserved': True,
        },
        'tables': table_reports,
        'candidate_sequence_floors': sequences,
    }
