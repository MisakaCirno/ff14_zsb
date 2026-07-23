from unittest import skipUnless

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.exceptions import IrreversibleError
from django.test import TransactionTestCase
from django.utils import timezone


class RecoverableDeletionMigrationTests(TransactionTestCase):
    migrate_from = ('shares', '0028_normalize_announcement_column_order')
    migrate_to = ('shares', '0029_add_recoverable_content_deletion')

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        apps = executor.loader.project_state([self.migrate_to]).apps
        User = apps.get_model('auth', 'User')
        Share = apps.get_model('shares', 'Share')
        author = User.objects.create(username='deleted-migration-author')
        share = Share.objects.create(
            title='不能丢失删除状态的分享',
            strategy_code='[stgy:recoverable-migration]',
            author=author,
        )
        Share.objects.filter(pk=share.pk).update(
            deleted_at=timezone.now(),
            deleted_by=author,
            deletion_origin='owner',
            deletion_reason='作者主动将分享移入回收站。',
        )

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_reverse_refuses_to_drop_live_recycle_bin_metadata(self):
        with self.assertRaisesRegex(
            IrreversibleError,
            'recycle bin contains user content',
        ):
            MigrationExecutor(connection).migrate([self.migrate_from])


@skipUnless(connection.vendor == 'sqlite', 'SQLite-specific sequence contract')
class RecoverableDeletionSequenceMigrationTests(TransactionTestCase):
    migrate_from = ('shares', '0028_normalize_announcement_column_order')
    migrate_to = ('shares', '0029_add_recoverable_content_deletion')

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        self.addCleanup(self._restore_leaf_migrations)
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        User = old_apps.get_model('auth', 'User')
        Share = old_apps.get_model('shares', 'Share')
        Collection = old_apps.get_model('shares', 'Collection')
        author = User.objects.create(username='sequence-migration-author')
        self.share = Share.objects.create(
            title='share sequence migration fixture',
            strategy_code='[stgy:sequence-migration]',
            author=author,
        )
        self.collection = Collection.objects.create(
            title='collection sequence migration fixture',
            author=author,
        )

    @staticmethod
    def _restore_leaf_migrations():
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def _sequence_floors(self):
        return {
            'shares_collection': max(9_200_002, self.collection.pk + 1_000_000),
            'shares_share': max(9_300_003, self.share.pk + 1_000_000),
        }

    def _set_sequence_floors(self, floors):
        with connection.cursor() as cursor:
            for table_name, floor in floors.items():
                cursor.execute(
                    'UPDATE sqlite_sequence SET seq = %s WHERE name = %s',
                    [floor, table_name],
                )
                self.assertEqual(cursor.rowcount, 1)

    def _sequence_values(self):
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT name, seq FROM sqlite_sequence '
                'WHERE name IN (%s, %s) ORDER BY name',
                ['shares_collection', 'shares_share'],
            )
            return dict(cursor.fetchall())

    def test_forward_preserves_share_and_collection_sequence_floors(self):
        floors = self._sequence_floors()
        self._set_sequence_floors(floors)

        MigrationExecutor(connection).migrate([self.migrate_to])

        self.assertEqual(self._sequence_values(), floors)

    def test_reverse_preserves_share_and_collection_sequence_floors(self):
        MigrationExecutor(connection).migrate([self.migrate_to])
        floors = self._sequence_floors()
        self._set_sequence_floors(floors)

        MigrationExecutor(connection).migrate([self.migrate_from])

        self.assertEqual(self._sequence_values(), floors)
