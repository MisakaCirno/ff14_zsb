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
