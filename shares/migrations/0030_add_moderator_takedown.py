from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError


SEQUENCE_FLOOR_TABLE = 'shares_migration_0030_share_sequence_floor'
SEQUENCE_FLOOR_OWNER = 'ffxivshare.shares.0030.share-sequence-floor.v1'
SHARE_TABLE = 'shares_share'


def _floor_table_sql(connection):
    quoted_table = connection.ops.quote_name(SEQUENCE_FLOOR_TABLE)
    return (
        f'CREATE TABLE {quoted_table} ('
        '"sequence_present" INTEGER NOT NULL CHECK '
        '("sequence_present" IN (0, 1)), '
        '"sequence_floor" INTEGER NOT NULL CHECK '
        '(typeof("sequence_floor") = \'integer\' AND "sequence_floor" >= 0), '
        '"migration_owner" TEXT NOT NULL CHECK '
        f'("migration_owner" = \'{SEQUENCE_FLOOR_OWNER}\'))'
    )


def _schema_objects(cursor, name):
    cursor.execute(
        'SELECT type, name, tbl_name FROM sqlite_schema '
        'WHERE lower(name) = lower(%s) OR lower(tbl_name) = lower(%s) '
        'ORDER BY type, name',
        [name, name],
    )
    return cursor.fetchall()


def _validate_share_table(cursor):
    cursor.execute(
        'SELECT type, name, tbl_name, sql FROM sqlite_schema '
        'WHERE lower(name) = lower(%s) ORDER BY type, name',
        [SHARE_TABLE],
    )
    objects = cursor.fetchall()
    if (
        len(objects) != 1
        or objects[0][0:3] != ('table', SHARE_TABLE, SHARE_TABLE)
        or not isinstance(objects[0][3], str)
        or 'AUTOINCREMENT' not in objects[0][3].upper()
    ):
        raise RuntimeError(
            f'Unexpected share table during 0030 sequence preservation: {objects!r}'
        )


def _read_share_sequence(cursor):
    cursor.execute(
        'SELECT name, seq FROM sqlite_sequence WHERE lower(name) = lower(%s)',
        [SHARE_TABLE],
    )
    rows = cursor.fetchall()
    if not rows:
        return None
    if (
        len(rows) != 1
        or rows[0][0] != SHARE_TABLE
        or isinstance(rows[0][1], bool)
        or not isinstance(rows[0][1], int)
        or rows[0][1] < 0
    ):
        raise RuntimeError(
            f'Unexpected share SQLite sequence during 0030: {rows!r}'
        )
    return rows[0][1]


def capture_share_sequence_floor(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != 'sqlite':
        return

    quoted_floor = connection.ops.quote_name(SEQUENCE_FLOOR_TABLE)
    with connection.cursor() as cursor:
        conflicts = _schema_objects(cursor, SEQUENCE_FLOOR_TABLE)
        if conflicts:
            raise RuntimeError(
                'Refusing to overwrite the 0030 share sequence-floor object: '
                f'{conflicts!r}'
            )
        _validate_share_table(cursor)
        sequence_floor = _read_share_sequence(cursor)
        cursor.execute(_floor_table_sql(connection))
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


def _captured_share_sequence(cursor):
    objects = _schema_objects(cursor, SEQUENCE_FLOOR_TABLE)
    expected = [('table', SEQUENCE_FLOOR_TABLE, SEQUENCE_FLOOR_TABLE)]
    if objects != expected:
        raise RuntimeError(
            'The 0030 share sequence-floor object changed during migration: '
            f'{objects!r}'
        )
    quoted_floor = cursor.db.ops.quote_name(SEQUENCE_FLOOR_TABLE)
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
            f'The 0030 share sequence-floor evidence is invalid: {rows!r}'
        )
    return rows[0][0], rows[0][1], quoted_floor


def restore_share_sequence_floor(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != 'sqlite':
        return

    with connection.cursor() as cursor:
        sequence_present, sequence_floor, quoted_floor = (
            _captured_share_sequence(cursor)
        )
        _validate_share_table(cursor)
        current_sequence = _read_share_sequence(cursor)
        desired_sequence = max(current_sequence or 0, sequence_floor)
        if current_sequence is None and (sequence_present or desired_sequence):
            cursor.execute(
                'INSERT INTO sqlite_sequence (name, seq) VALUES (%s, %s)',
                [SHARE_TABLE, desired_sequence],
            )
        elif current_sequence is not None and current_sequence != desired_sequence:
            cursor.execute(
                'UPDATE sqlite_sequence SET seq = %s WHERE name = %s',
                [desired_sequence, SHARE_TABLE],
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    'Failed to restore the 0030 share SQLite sequence floor.'
                )
        restored_sequence = _read_share_sequence(cursor)
        if sequence_present and (
            restored_sequence is None or restored_sequence < sequence_floor
        ):
            raise RuntimeError(
                'The share SQLite sequence floor was lowered during 0030.'
            )
        cursor.execute(f'DROP TABLE {quoted_floor}')


def prevent_lossy_reverse(apps, schema_editor):
    database = schema_editor.connection.alias
    Share = apps.get_model('shares', 'Share')
    ShareLog = apps.get_model('shares', 'ShareLog')
    if (
        Share.objects.using(database).filter(
            restriction_state='moderator_takedown',
        ).exists()
        or ShareLog.objects.using(database).filter(
            action='moderator_takedown',
        ).exists()
    ):
        raise IrreversibleError(
            'Cannot reverse moderator takedown semantics after they have been '
            'used. Restore the pre-migration database backup instead of '
            'making active restrictions or audit records unreadable.'
        )


class Migration(migrations.Migration):
    atomic = True

    dependencies = [
        ('shares', '0029_add_recoverable_content_deletion'),
    ]

    operations = [
        migrations.RunPython(
            capture_share_sequence_floor,
            restore_share_sequence_floor,
        ),
        migrations.RemoveConstraint(
            model_name='share',
            name='share_restriction_state_valid',
        ),
        migrations.AlterField(
            model_name='share',
            name='restriction_state',
            field=models.CharField(
                choices=[
                    ('clear', '无限制'),
                    ('review_rejected', '审核拒绝限制'),
                    ('report_takedown', '举报下架限制'),
                    ('moderator_takedown', '管理员下架限制'),
                    ('legacy_private', '历史私密待确认'),
                ],
                default='clear',
                max_length=20,
                verbose_name='内容限制状态',
            ),
        ),
        migrations.AlterField(
            model_name='sharelog',
            name='action',
            field=models.CharField(
                choices=[
                    ('create', '创建分享'),
                    ('edit', '编辑分享'),
                    ('approve', '审核通过'),
                    ('reject', '审核拒绝'),
                    ('moderator_takedown', '管理员下架'),
                    ('confirm_restriction', '确认维持内容限制'),
                    ('release_restriction', '解除内容限制'),
                    ('add_collection', '加入合集'),
                    ('remove_collection', '移出合集'),
                    ('report_handle', '处理举报'),
                    ('delete', '删除分享'),
                    ('restore', '恢复分享'),
                    ('other', '其他操作'),
                ],
                max_length=20,
                verbose_name='操作类型',
            ),
        ),
        migrations.AddConstraint(
            model_name='share',
            constraint=models.CheckConstraint(
                condition=models.Q(restriction_state__in=[
                    'clear',
                    'review_rejected',
                    'report_takedown',
                    'moderator_takedown',
                    'legacy_private',
                ]),
                name='share_restriction_state_valid',
            ),
        ),
        migrations.RunPython(
            restore_share_sequence_floor,
            capture_share_sequence_floor,
        ),
        migrations.RunPython(
            migrations.RunPython.noop,
            prevent_lossy_reverse,
        ),
    ]
