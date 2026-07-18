from importlib import import_module
from types import SimpleNamespace
from unittest import skipUnless

from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.recorder import MigrationRecorder
from django.test import SimpleTestCase, TransactionTestCase
from django.utils import timezone


MIGRATION_0019 = import_module(
    'shares.migrations.'
    '0019_report_resolution_reason_share_review_feedback_and_more'
)
MIGRATION_0025 = import_module(
    'shares.migrations.0025_add_collection_owner_index'
)
FLOOR_TABLE = MIGRATION_0019.SQLITE_SEQUENCE_FLOOR_TABLE


class SQLiteSequenceRowValidationTests(SimpleTestCase):
    def test_both_migrations_reject_invalid_or_duplicate_rows(self):
        invalid_cases = (
            [('', 0)],
            [(7, 0)],
            [('shares_share', -1)],
            [('shares_share', True)],
            [('shares_share', '1')],
            [('shares_share', 1), ('SHARES_SHARE', 2)],
        )
        for migration_module in (MIGRATION_0019, MIGRATION_0025):
            for rows in invalid_cases:
                with self.subTest(
                    migration=migration_module.__name__,
                    rows=rows,
                ):
                    with self.assertRaises(RuntimeError):
                        migration_module._validate_sequence_rows(
                            rows,
                            source='test rows',
                        )

    def test_non_sqlite_connections_are_explicit_noops(self):
        schema_editor = SimpleNamespace(
            connection=SimpleNamespace(vendor='postgresql')
        )

        MIGRATION_0019.capture_sqlite_sequence_floors(None, schema_editor)
        MIGRATION_0019.cleanup_sqlite_sequence_floors_on_reverse(
            None,
            schema_editor,
        )
        MIGRATION_0025.restore_sqlite_sequence_floors(None, schema_editor)


@skipUnless(connection.vendor == 'sqlite', 'SQLite-specific migration contract')
class SQLiteSequenceFloorMigrationTests(TransactionTestCase):
    migrate_from = ('shares', '0018_default_home_feed_waterfall')
    migrate_capture = (
        'shares',
        '0019_report_resolution_reason_share_review_feedback_and_more',
    )
    migrate_before_restore = ('shares', '0024_widen_site_message_titles')
    migrate_to = ('shares', '0025_add_collection_owner_index')

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        self.addCleanup(self._restore_leaf_migrations)
        self.old_apps = executor.loader.project_state([self.migrate_from]).apps

    @staticmethod
    def _reserved_objects():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT type, name
                FROM sqlite_schema
                WHERE lower(name) = lower(%s) OR lower(tbl_name) = lower(%s)
                ORDER BY CASE type
                    WHEN 'trigger' THEN 1
                    WHEN 'index' THEN 2
                    WHEN 'view' THEN 3
                    ELSE 4
                END,
                name
                """,
                [FLOOR_TABLE, FLOOR_TABLE],
            )
            return cursor.fetchall()

    @classmethod
    def _drop_reserved_objects(cls):
        for object_type, object_name in cls._reserved_objects():
            quoted_name = connection.ops.quote_name(object_name)
            with connection.cursor() as cursor:
                if object_type == 'table':
                    cursor.execute(f'DROP TABLE {quoted_name}')
                elif object_type == 'view':
                    cursor.execute(f'DROP VIEW {quoted_name}')
                elif object_type == 'index':
                    cursor.execute(f'DROP INDEX {quoted_name}')
                elif object_type == 'trigger':
                    cursor.execute(f'DROP TRIGGER {quoted_name}')
                else:
                    raise AssertionError(
                        f'Unexpected reserved object type {object_type!r}'
                    )

    @classmethod
    def _restore_leaf_migrations(cls):
        cls._drop_reserved_objects()
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    @staticmethod
    def _sequence_rows():
        with connection.cursor() as cursor:
            cursor.execute('SELECT name, seq FROM sqlite_sequence ORDER BY name')
            return dict(cursor.fetchall())

    @staticmethod
    def _has_collection_owner_index():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM sqlite_schema
                WHERE type = 'index' AND name = 'collection_owner_updated_idx'
                """
            )
            return cursor.fetchone()[0] == 1

    def _create_production_0018_fixture(self):
        User = self.old_apps.get_model('auth', 'User')
        UserProfile = self.old_apps.get_model('shares', 'UserProfile')
        Share = self.old_apps.get_model('shares', 'Share')
        Report = self.old_apps.get_model('shares', 'Report')
        Collection = self.old_apps.get_model('shares', 'Collection')
        CollectionItem = self.old_apps.get_model('shares', 'CollectionItem')
        ShareLog = self.old_apps.get_model('shares', 'ShareLog')

        author = User.objects.create(username='sequence-floor-author')
        reporter = User.objects.create(username='sequence-floor-reporter')
        UserProfile.objects.create(
            user_id=author.pk,
            nickname='sequence floor author',
            home_feed_mode='paginated',
        )
        UserProfile.objects.create(
            user_id=reporter.pk,
            nickname='sequence floor reporter',
            home_feed_mode='infinite',
        )
        share = Share.objects.create(
            share_id='seqfloor0018',
            title='sequence floor source share',
            strategy_code='[stgy:sequence-floor]',
            description='This row forces the production table rebuild path.',
            author_id=author.pk,
            category='combat',
            visibility='public',
            status='approved',
            views=3,
            copies=2,
        )
        Report.objects.create(
            share_id=share.pk,
            reporter_id=reporter.pk,
            reason='resolved historical report',
            status='resolved',
            resolved_at=timezone.now(),
            resolved_by_id=author.pk,
        )
        collection = Collection.objects.create(
            author_id=author.pk,
            title='sequence floor collection',
            description='production 0018 fixture',
            is_public=True,
        )
        CollectionItem.objects.create(
            collection_id=collection.pk,
            share_id=share.pk,
            order=0,
        )
        ShareLog.objects.create(
            share_id=share.pk,
            user_id=author.pk,
            action='approve',
            details='sequence floor production-path fixture',
        )

    def _raise_after_capture(self):
        schema_editor = SimpleNamespace(connection=connection)
        with transaction.atomic():
            MIGRATION_0019.capture_sqlite_sequence_floors(
                None,
                schema_editor,
            )
            self.assertEqual(
                self._reserved_objects(),
                [('table', FLOOR_TABLE)],
            )
            raise RuntimeError('force transaction rollback')

    def test_capture_creation_rolls_back_with_its_migration_transaction(self):
        with self.assertRaisesMessage(
            RuntimeError,
            'force transaction rollback',
        ):
            self._raise_after_capture()

        self.assertEqual(self._reserved_objects(), [])

    def test_capture_refuses_an_existing_table_view_or_related_object(self):
        quoted_table = connection.ops.quote_name(FLOOR_TABLE)
        with connection.cursor() as cursor:
            cursor.execute(
                f'CREATE VIEW {quoted_table} AS SELECT 1 AS sentinel'
            )

        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(RuntimeError, 'Refusing to overwrite'):
            executor.migrate([self.migrate_capture])

        self.assertEqual(self._reserved_objects(), [('view', FLOOR_TABLE)])
        self.assertFalse(
            MigrationRecorder(connection).migration_qs.filter(
                app='shares',
                name=self.migrate_capture[1],
            ).exists()
        )
        self.assertNotIn(
            'shares_sitemessage',
            set(connection.introspection.table_names()),
        )

    def test_production_0018_path_preserves_every_sequence_floor(self):
        self._create_production_0018_fixture()
        raised_floors = {}
        with connection.cursor() as cursor:
            for offset, table_name in enumerate(
                (
                    # All five source tables rebuilt between 0018 and 0025.
                    'shares_share',
                    'shares_report',
                    'shares_collectionitem',
                    'shares_userprofile',
                    'shares_sharelog',
                    # A source table that is not rebuilt is the control case.
                    'shares_collection',
                ),
                start=1,
            ):
                quoted_table = connection.ops.quote_name(table_name)
                cursor.execute(f'SELECT MAX("id") FROM {quoted_table}')
                maximum_id = cursor.fetchone()[0]
                sequence_floor = maximum_id + 10000 + offset
                self.assertGreater(sequence_floor, maximum_id)
                cursor.execute(
                    'UPDATE sqlite_sequence SET seq = %s WHERE name = %s',
                    [sequence_floor, table_name],
                )
                self.assertEqual(cursor.rowcount, 1)
                raised_floors[table_name] = sequence_floor

        source_rows = self._sequence_rows()
        for table_name, sequence_floor in raised_floors.items():
            self.assertGreater(sequence_floor, 0)
            self.assertEqual(source_rows[table_name], sequence_floor)

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_capture])
        with connection.cursor() as cursor:
            quoted_floor_table = connection.ops.quote_name(FLOOR_TABLE)
            cursor.execute(
                f'SELECT "table_name", "sequence_floor" '
                f'FROM {quoted_floor_table} ORDER BY "table_name"'
            )
            self.assertEqual(dict(cursor.fetchall()), source_rows)

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_before_restore])
        current_dominant_value = source_rows['shares_collection'] + 7777
        with connection.cursor() as cursor:
            cursor.execute(
                'UPDATE sqlite_sequence SET seq = %s WHERE name = %s',
                [current_dominant_value, 'shares_collection'],
            )
            self.assertEqual(cursor.rowcount, 1)
        before_restore_rows = self._sequence_rows()
        expected_business_rows = {
            table_name: max(before_restore_rows[table_name], source_rows[table_name])
            for table_name in raised_floors
        }

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        final_rows = self._sequence_rows()

        for table_name, source_floor in source_rows.items():
            with self.subTest(table=table_name):
                self.assertIn(table_name, final_rows)
                self.assertGreaterEqual(final_rows[table_name], source_floor)
        for table_name, sequence_floor in raised_floors.items():
            self.assertEqual(
                final_rows[table_name],
                expected_business_rows[table_name],
            )
            self.assertGreaterEqual(final_rows[table_name], sequence_floor)
        self.assertEqual(
            final_rows['shares_collection'],
            current_dominant_value,
        )
        self.assertEqual(self._reserved_objects(), [])

    def test_restore_failure_rolls_back_index_and_retains_capture_for_retry(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_before_restore])
        self.assertFalse(self._has_collection_owner_index())

        quoted_floor_table = connection.ops.quote_name(FLOOR_TABLE)
        with connection.cursor() as cursor:
            cursor.execute(
                f'INSERT INTO {quoted_floor_table} '
                '("table_name", "sequence_floor", "migration_owner") '
                'VALUES (%s, %s, %s)',
                [
                    'missing_sequence_target',
                    7,
                    MIGRATION_0019.SQLITE_SEQUENCE_FLOOR_OWNER,
                ],
            )

        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(RuntimeError, 'missing or ambiguous'):
            executor.migrate([self.migrate_to])

        self.assertFalse(self._has_collection_owner_index())
        self.assertEqual(self._reserved_objects(), [('table', FLOOR_TABLE)])
        self.assertFalse(
            MigrationRecorder(connection).migration_qs.filter(
                app='shares',
                name=self.migrate_to[1],
            ).exists()
        )
        with connection.cursor() as cursor:
            cursor.execute(
                f'DELETE FROM {quoted_floor_table} WHERE "table_name" = %s',
                ['missing_sequence_target'],
            )

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.assertTrue(self._has_collection_owner_index())
        self.assertEqual(self._reserved_objects(), [])

    def test_restore_refuses_a_historical_lookalike_table_without_deleting_it(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_before_restore])
        self._drop_reserved_objects()
        quoted_floor_table = connection.ops.quote_name(FLOOR_TABLE)
        with connection.cursor() as cursor:
            cursor.execute(
                f'CREATE TABLE {quoted_floor_table} ('
                '"table_name" TEXT NOT NULL, '
                '"sequence_floor" INTEGER NOT NULL)'
            )
            cursor.execute(
                f'INSERT INTO {quoted_floor_table} VALUES (%s, %s)',
                ['user_owned_data', 41],
            )

        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(RuntimeError, 'unexpected schema'):
            executor.migrate([self.migrate_to])

        self.assertFalse(self._has_collection_owner_index())
        self.assertEqual(self._reserved_objects(), [('table', FLOOR_TABLE)])
        with connection.cursor() as cursor:
            cursor.execute(f'SELECT * FROM {quoted_floor_table}')
            self.assertEqual(cursor.fetchall(), [('user_owned_data', 41)])
        self.assertFalse(
            MigrationRecorder(connection).migration_qs.filter(
                app='shares',
                name=self.migrate_to[1],
            ).exists()
        )

    def test_reverse_0019_removes_only_its_owned_capture_table(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_capture])
        self.assertEqual(self._reserved_objects(), [('table', FLOOR_TABLE)])

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])

        self.assertEqual(self._reserved_objects(), [])
        self.assertFalse(
            MigrationRecorder(connection).migration_qs.filter(
                app='shares',
                name=self.migrate_capture[1],
            ).exists()
        )

    def test_reverse_refuses_a_historical_lookalike_table_without_deleting_it(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_capture])
        self._drop_reserved_objects()
        quoted_floor_table = connection.ops.quote_name(FLOOR_TABLE)
        with connection.cursor() as cursor:
            cursor.execute(
                f'CREATE TABLE {quoted_floor_table} ('
                '"table_name" TEXT NOT NULL, '
                '"sequence_floor" INTEGER NOT NULL)'
            )
            cursor.execute(
                f'INSERT INTO {quoted_floor_table} VALUES (%s, %s)',
                ['user_owned_reverse_data', 73],
            )

        executor = MigrationExecutor(connection)
        with self.assertRaises(IrreversibleError):
            executor.migrate([self.migrate_from])

        self.assertEqual(self._reserved_objects(), [('table', FLOOR_TABLE)])
        with connection.cursor() as cursor:
            cursor.execute(f'SELECT * FROM {quoted_floor_table}')
            self.assertEqual(
                cursor.fetchall(),
                [('user_owned_reverse_data', 73)],
            )
        self.assertTrue(
            MigrationRecorder(connection).migration_qs.filter(
                app='shares',
                name=self.migrate_capture[1],
            ).exists()
        )
        self.assertIn(
            'shares_sitemessage',
            set(connection.introspection.table_names()),
        )

    def test_legacy_local_path_without_capture_is_a_repeatable_noop(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_before_restore])
        self.assertEqual(self._reserved_objects(), [('table', FLOOR_TABLE)])
        self._drop_reserved_objects()

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.assertTrue(self._has_collection_owner_index())
        self.assertEqual(self._reserved_objects(), [])

        before = self._sequence_rows()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.assertEqual(self._sequence_rows(), before)
        self.assertEqual(self._reserved_objects(), [])
