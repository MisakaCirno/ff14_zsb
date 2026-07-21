import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError


SEQUENCE_FLOOR_TABLE = 'shares_migration_0029_sequence_floors'
SEQUENCE_FLOOR_OWNER = 'ffxivshare.shares.0029.sequence-floors.v1'
SEQUENCE_TABLES = ('shares_collection', 'shares_share')


def _schema_objects(cursor, name):
    cursor.execute(
        'SELECT type, name, tbl_name FROM sqlite_schema '
        'WHERE lower(name) = lower(%s) OR lower(tbl_name) = lower(%s) '
        'ORDER BY type, name',
        [name, name],
    )
    return cursor.fetchall()


def _validate_sequence_target(cursor, table_name):
    cursor.execute(
        'SELECT type, name, tbl_name, sql FROM sqlite_schema '
        'WHERE lower(name) = lower(%s) ORDER BY type, name',
        [table_name],
    )
    objects = cursor.fetchall()
    if (
        len(objects) != 1
        or objects[0][0:3] != ('table', table_name, table_name)
        or not isinstance(objects[0][3], str)
        or 'AUTOINCREMENT' not in objects[0][3].upper()
    ):
        raise RuntimeError(
            f'Unexpected sequence target during 0029: {objects!r}'
        )


def _read_sequence(cursor, table_name):
    cursor.execute(
        'SELECT name, seq FROM sqlite_sequence WHERE lower(name) = lower(%s)',
        [table_name],
    )
    rows = cursor.fetchall()
    if not rows:
        return None
    if (
        len(rows) != 1
        or rows[0][0] != table_name
        or isinstance(rows[0][1], bool)
        or not isinstance(rows[0][1], int)
        or rows[0][1] < 0
    ):
        raise RuntimeError(
            f'Unexpected SQLite sequence for {table_name!r} during 0029: '
            f'{rows!r}'
        )
    return rows[0][1]


def capture_sequence_floors(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != 'sqlite':
        return

    quoted_floor = connection.ops.quote_name(SEQUENCE_FLOOR_TABLE)
    with connection.cursor() as cursor:
        conflicts = _schema_objects(cursor, SEQUENCE_FLOOR_TABLE)
        if conflicts:
            raise RuntimeError(
                'Refusing to overwrite the 0029 sequence-floor object: '
                f'{conflicts!r}'
            )
        for table_name in SEQUENCE_TABLES:
            _validate_sequence_target(cursor, table_name)
        cursor.execute(
            f'CREATE TABLE {quoted_floor} ('
            '"table_name" TEXT NOT NULL CHECK '
            '("table_name" IN (\'shares_collection\', \'shares_share\')), '
            '"sequence_present" INTEGER NOT NULL CHECK '
            '("sequence_present" IN (0, 1)), '
            '"sequence_floor" INTEGER NOT NULL CHECK '
            '(typeof("sequence_floor") = \'integer\' AND "sequence_floor" >= 0), '
            '"migration_owner" TEXT NOT NULL CHECK '
            f'("migration_owner" = \'{SEQUENCE_FLOOR_OWNER}\'))'
        )
        for table_name in SEQUENCE_TABLES:
            sequence_floor = _read_sequence(cursor, table_name)
            cursor.execute(
                f'INSERT INTO {quoted_floor} '
                '("table_name", "sequence_present", "sequence_floor", '
                '"migration_owner") VALUES (%s, %s, %s, %s)',
                [
                    table_name,
                    int(sequence_floor is not None),
                    sequence_floor if sequence_floor is not None else 0,
                    SEQUENCE_FLOOR_OWNER,
                ],
            )


def _captured_sequence_floors(cursor):
    objects = _schema_objects(cursor, SEQUENCE_FLOOR_TABLE)
    expected = [('table', SEQUENCE_FLOOR_TABLE, SEQUENCE_FLOOR_TABLE)]
    if objects != expected:
        raise RuntimeError(
            'The 0029 sequence-floor object changed during migration: '
            f'{objects!r}'
        )
    quoted_floor = cursor.db.ops.quote_name(SEQUENCE_FLOOR_TABLE)
    cursor.execute(
        f'SELECT "table_name", "sequence_present", "sequence_floor", '
        f'"migration_owner" FROM {quoted_floor} ORDER BY "table_name"'
    )
    rows = cursor.fetchall()
    if (
        [row[0] for row in rows] != list(SEQUENCE_TABLES)
        or any(
            len(row) != 4
            or row[1] not in (0, 1)
            or isinstance(row[2], bool)
            or not isinstance(row[2], int)
            or row[2] < 0
            or row[3] != SEQUENCE_FLOOR_OWNER
            for row in rows
        )
    ):
        raise RuntimeError(
            f'The 0029 sequence-floor evidence is invalid: {rows!r}'
        )
    return rows, quoted_floor


def restore_sequence_floors(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != 'sqlite':
        return

    with connection.cursor() as cursor:
        rows, quoted_floor = _captured_sequence_floors(cursor)
        for table_name, sequence_present, sequence_floor, _owner in rows:
            _validate_sequence_target(cursor, table_name)
            current_sequence = _read_sequence(cursor, table_name)
            desired_sequence = max(current_sequence or 0, sequence_floor)
            if current_sequence is None and (sequence_present or desired_sequence):
                cursor.execute(
                    'INSERT INTO sqlite_sequence (name, seq) VALUES (%s, %s)',
                    [table_name, desired_sequence],
                )
            elif current_sequence is not None and current_sequence != desired_sequence:
                cursor.execute(
                    'UPDATE sqlite_sequence SET seq = %s WHERE name = %s',
                    [desired_sequence, table_name],
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        f'Failed to restore the 0029 SQLite sequence floor for '
                        f'{table_name!r}.'
                    )
            restored_sequence = _read_sequence(cursor, table_name)
            if sequence_present and (
                restored_sequence is None or restored_sequence < sequence_floor
            ):
                raise RuntimeError(
                    f'The SQLite sequence floor for {table_name!r} was lowered '
                    'during 0029.'
                )
        cursor.execute(f'DROP TABLE {quoted_floor}')


def prevent_lossy_reverse(apps, schema_editor):
    database = schema_editor.connection.alias
    Share = apps.get_model('shares', 'Share')
    Collection = apps.get_model('shares', 'Collection')
    if (
        Share.objects.using(database).filter(deleted_at__isnull=False).exists()
        or Collection.objects.using(database).filter(
            deleted_at__isnull=False,
        ).exists()
    ):
        raise IrreversibleError(
            'Cannot reverse recoverable deletion fields while the recycle bin '
            'contains user content. Restore the pre-migration database backup '
            'instead of dropping deletion metadata.'
        )


class Migration(migrations.Migration):
    atomic = True

    dependencies = [
        ('shares', '0028_normalize_announcement_column_order'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(
            capture_sequence_floors,
            restore_sequence_floors,
        ),
        migrations.AddField(
            model_name='share',
            name='deleted_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='移入回收站时间'),
        ),
        migrations.AddField(
            model_name='share',
            name='deleted_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='deleted_shares', to=settings.AUTH_USER_MODEL, verbose_name='删除操作人'),
        ),
        migrations.AddField(
            model_name='share',
            name='deletion_origin',
            field=models.CharField(blank=True, choices=[('owner', '作者删除'), ('moderator', '管理员删除')], default='', max_length=10, verbose_name='删除来源'),
        ),
        migrations.AddField(
            model_name='share',
            name='deletion_reason',
            field=models.TextField(blank=True, verbose_name='删除说明'),
        ),
        migrations.AddField(
            model_name='collection',
            name='deleted_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='移入回收站时间'),
        ),
        migrations.AddField(
            model_name='collection',
            name='deleted_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='deleted_collections', to=settings.AUTH_USER_MODEL, verbose_name='删除操作人'),
        ),
        migrations.AddField(
            model_name='collection',
            name='deletion_reason',
            field=models.TextField(blank=True, verbose_name='删除说明'),
        ),
        migrations.AlterField(
            model_name='sharelog',
            name='action',
            field=models.CharField(choices=[('create', '创建分享'), ('edit', '编辑分享'), ('approve', '审核通过'), ('reject', '审核拒绝'), ('confirm_restriction', '确认维持内容限制'), ('release_restriction', '解除内容限制'), ('add_collection', '加入合集'), ('remove_collection', '移出合集'), ('report_handle', '处理举报'), ('delete', '删除分享'), ('restore', '恢复分享'), ('other', '其他操作')], max_length=20, verbose_name='操作类型'),
        ),
        migrations.AddIndex(
            model_name='share',
            index=models.Index(fields=['deleted_at', '-created_at'], name='share_deleted_idx'),
        ),
        migrations.AddIndex(
            model_name='collection',
            index=models.Index(fields=['deleted_at', '-updated_at'], name='collection_deleted_idx'),
        ),
        migrations.AddConstraint(
            model_name='share',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('deleted_at__isnull', True), ('deleted_by__isnull', True), ('deletion_origin', ''), ('deletion_reason', '')), models.Q(('deleted_at__isnull', False), ('deletion_origin__in', ['owner', 'moderator']), models.Q(('deletion_reason', ''), _negated=True)), _connector='OR'), name='share_deletion_metadata'),
        ),
        migrations.AddConstraint(
            model_name='collection',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('deleted_at__isnull', True), ('deleted_by__isnull', True), ('deletion_reason', '')), models.Q(('deleted_at__isnull', False), models.Q(('deletion_reason', ''), _negated=True)), _connector='OR'), name='collection_deletion_metadata'),
        ),
        migrations.RunPython(
            restore_sequence_floors,
            capture_sequence_floors,
        ),
        migrations.RunPython(
            migrations.RunPython.noop,
            prevent_lossy_reverse,
        ),
    ]
