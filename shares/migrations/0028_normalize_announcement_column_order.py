from django.db import migrations


SEQUENCE_FLOOR_TABLE = 'shares_migration_0028_announcement_sequence_floor'
SEQUENCE_FLOOR_OWNER = 'ffxivshare.shares.0028.announcement-sequence-floor.v1'
ANNOUNCEMENT_TABLE = 'shares_announcement'
REBUILD_TABLE = 'shares_announcement_r19_rebuild'
LEGACY_TABLE_SQL = (
    'CREATE TABLE "shares_announcement" '
    '("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, '
    '"title" varchar(200) NOT NULL, "is_active" bool NOT NULL, '
    '"created_at" datetime NOT NULL, "updated_at" datetime NOT NULL, '
    '"content" text NOT NULL)'
)
CANONICAL_TABLE_SQL = (
    'CREATE TABLE "shares_announcement" '
    '("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, '
    '"title" varchar(200) NOT NULL, "content" text NOT NULL, '
    '"is_active" bool NOT NULL, "created_at" datetime NOT NULL, '
    '"updated_at" datetime NOT NULL)'
)
ANNOUNCEMENT_INDEX_SQL = (
    'CREATE INDEX "announcement_active_idx" ON '
    '"shares_announcement" ("is_active", "created_at" DESC)'
)


def _validate_announcement_table(cursor):
    cursor.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema WHERE name = %s",
        [ANNOUNCEMENT_TABLE],
    )
    rows = cursor.fetchall()
    if (
        len(rows) != 1
        or rows[0][0:3]
        != ('table', ANNOUNCEMENT_TABLE, ANNOUNCEMENT_TABLE)
        or not isinstance(rows[0][3], str)
        or 'AUTOINCREMENT' not in rows[0][3].upper()
    ):
        raise RuntimeError(
            f'Unexpected announcement table before R19 normalization: {rows!r}'
        )


def _read_sequence(cursor):
    cursor.execute(
        'SELECT name, seq FROM sqlite_sequence WHERE lower(name) = lower(%s)',
        [ANNOUNCEMENT_TABLE],
    )
    rows = cursor.fetchall()
    if not rows:
        return None
    if (
        len(rows) != 1
        or rows[0][0] != ANNOUNCEMENT_TABLE
        or isinstance(rows[0][1], bool)
        or not isinstance(rows[0][1], int)
        or rows[0][1] < 0
    ):
        raise RuntimeError(
            f'Unexpected announcement SQLite sequence: {rows!r}'
        )
    return rows[0][1]


def capture_announcement_sequence_floor(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != 'sqlite':
        return

    quoted_floor = connection.ops.quote_name(SEQUENCE_FLOOR_TABLE)
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT type, name, tbl_name FROM sqlite_schema '
            'WHERE lower(name) = lower(%s) OR lower(tbl_name) = lower(%s) '
            'ORDER BY type, name',
            [SEQUENCE_FLOOR_TABLE, SEQUENCE_FLOOR_TABLE],
        )
        conflicts = cursor.fetchall()
        if conflicts:
            raise RuntimeError(
                'Refusing to overwrite the R19 announcement sequence-floor '
                f'object: {conflicts!r}'
            )

        _validate_announcement_table(cursor)
        sequence_floor = _read_sequence(cursor)
        cursor.execute(
            f'CREATE TABLE {quoted_floor} ('
            '"sequence_present" INTEGER NOT NULL CHECK '
            '("sequence_present" IN (0, 1)), '
            '"sequence_floor" INTEGER NOT NULL CHECK '
            '(typeof("sequence_floor") = \'integer\' AND "sequence_floor" >= 0), '
            '"migration_owner" TEXT NOT NULL CHECK '
            f'("migration_owner" = \'{SEQUENCE_FLOOR_OWNER}\'))'
        )
        cursor.execute(
            f'INSERT INTO {quoted_floor} '
            '("sequence_present", "sequence_floor", "migration_owner") '
            'VALUES (%s, %s, %s)',
            [
                int(sequence_floor is not None),
                sequence_floor if sequence_floor is not None else 0,
                SEQUENCE_FLOOR_OWNER,
            ],
        )


def restore_announcement_sequence_floor(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != 'sqlite':
        return

    quoted_floor = connection.ops.quote_name(SEQUENCE_FLOOR_TABLE)
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT type, name, tbl_name FROM sqlite_schema '
            'WHERE lower(name) = lower(%s) OR lower(tbl_name) = lower(%s) '
            'ORDER BY type, name',
            [SEQUENCE_FLOOR_TABLE, SEQUENCE_FLOOR_TABLE],
        )
        objects = cursor.fetchall()
        expected = [('table', SEQUENCE_FLOOR_TABLE, SEQUENCE_FLOOR_TABLE)]
        if objects != expected:
            raise RuntimeError(
                'R19 announcement sequence-floor object changed during '
                f'normalization: {objects!r}'
            )
        cursor.execute(
            f'SELECT "sequence_present", "sequence_floor", "migration_owner" '
            f'FROM {quoted_floor}'
        )
        rows = cursor.fetchall()
        if (
            len(rows) != 1
            or rows[0][0] not in (0, 1)
            or isinstance(rows[0][1], bool)
            or not isinstance(rows[0][1], int)
            or rows[0][1] < 0
            or rows[0][2] != SEQUENCE_FLOOR_OWNER
        ):
            raise RuntimeError(
                f'R19 announcement sequence-floor evidence is invalid: {rows!r}'
            )
        sequence_present, sequence_floor, _owner = rows[0]

        _validate_announcement_table(cursor)
        current_sequence = _read_sequence(cursor)
        desired_sequence = max(current_sequence or 0, sequence_floor)
        if current_sequence is None and (sequence_present or desired_sequence):
            cursor.execute(
                'INSERT INTO sqlite_sequence (name, seq) VALUES (%s, %s)',
                [ANNOUNCEMENT_TABLE, desired_sequence],
            )
        elif current_sequence is not None and current_sequence != desired_sequence:
            cursor.execute(
                'UPDATE sqlite_sequence SET seq = %s WHERE name = %s',
                [desired_sequence, ANNOUNCEMENT_TABLE],
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    'Failed to restore the announcement SQLite sequence floor.'
                )
        restored_sequence = _read_sequence(cursor)
        if restored_sequence is not None and restored_sequence < sequence_floor:
            raise RuntimeError(
                'Announcement SQLite sequence floor was lowered during '
                'normalization.'
            )
        cursor.execute(f'DROP TABLE {quoted_floor}')


def _rebuild_announcement_table(schema_editor, *, expected_sql, target_sql):
    connection = schema_editor.connection
    if connection.vendor != 'sqlite':
        return

    columns = '"id", "title", "content", "is_active", "created_at", "updated_at"'
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT sql FROM sqlite_schema WHERE type = \'table\' AND name = %s',
            [ANNOUNCEMENT_TABLE],
        )
        table_rows = cursor.fetchall()
        if len(table_rows) != 1 or table_rows[0][0] != expected_sql:
            if len(table_rows) == 1 and table_rows[0][0] == target_sql:
                return
            raise RuntimeError(
                'Announcement table SQL changed before R19 normalization: '
                f'{table_rows!r}'
            )
        cursor.execute(
            'SELECT type, name, tbl_name, sql FROM sqlite_schema '
            'WHERE tbl_name = %s AND type != \'table\' AND sql IS NOT NULL '
            'ORDER BY type, name',
            [ANNOUNCEMENT_TABLE],
        )
        related_objects = cursor.fetchall()
        expected_objects = [(
            'index',
            'announcement_active_idx',
            ANNOUNCEMENT_TABLE,
            ANNOUNCEMENT_INDEX_SQL,
        )]
        if related_objects != expected_objects:
            raise RuntimeError(
                'Announcement table has unexpected schema objects before R19 '
                f'normalization: {related_objects!r}'
            )
        cursor.execute(
            'SELECT type, name, tbl_name FROM sqlite_schema '
            'WHERE lower(name) = lower(%s) OR lower(tbl_name) = lower(%s) '
            'ORDER BY type, name',
            [REBUILD_TABLE, REBUILD_TABLE],
        )
        conflicts = cursor.fetchall()
        if conflicts:
            raise RuntimeError(
                f'Announcement rebuild table name is occupied: {conflicts!r}'
            )

        cursor.execute(
            f'ALTER TABLE "{ANNOUNCEMENT_TABLE}" '
            f'RENAME TO "{REBUILD_TABLE}"'
        )
        cursor.execute(target_sql)
        cursor.execute(
            f'INSERT INTO "{ANNOUNCEMENT_TABLE}" ({columns}) '
            f'SELECT {columns} FROM "{REBUILD_TABLE}"'
        )
        for source_table, destination_table in (
            (REBUILD_TABLE, ANNOUNCEMENT_TABLE),
            (ANNOUNCEMENT_TABLE, REBUILD_TABLE),
        ):
            cursor.execute(
                f'SELECT {columns} FROM "{source_table}" '
                f'EXCEPT SELECT {columns} FROM "{destination_table}" LIMIT 1'
            )
            if cursor.fetchone() is not None:
                raise RuntimeError(
                    'Announcement data changed during R19 column-order '
                    'normalization.'
                )
        cursor.execute(f'DROP TABLE "{REBUILD_TABLE}"')
        cursor.execute(ANNOUNCEMENT_INDEX_SQL)

        cursor.execute(
            'SELECT sql FROM sqlite_schema WHERE type = \'table\' AND name = %s',
            [ANNOUNCEMENT_TABLE],
        )
        if cursor.fetchall() != [(target_sql,)]:
            raise RuntimeError(
                'Announcement table did not reach the expected normalized SQL.'
            )


def normalize_announcement_column_order(apps, schema_editor):
    _rebuild_announcement_table(
        schema_editor,
        expected_sql=LEGACY_TABLE_SQL,
        target_sql=CANONICAL_TABLE_SQL,
    )


def restore_announcement_column_order(apps, schema_editor):
    _rebuild_announcement_table(
        schema_editor,
        expected_sql=CANONICAL_TABLE_SQL,
        target_sql=LEGACY_TABLE_SQL,
    )


class Migration(migrations.Migration):
    atomic = True

    dependencies = [
        ('shares', '0027_classify_legacy_private_shares'),
    ]

    operations = [
        migrations.RunPython(
            capture_announcement_sequence_floor,
            restore_announcement_sequence_floor,
        ),
        migrations.RunPython(
            normalize_announcement_column_order,
            restore_announcement_column_order,
        ),
        migrations.RunPython(
            restore_announcement_sequence_floor,
            capture_announcement_sequence_floor,
        ),
    ]
