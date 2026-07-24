from unittest import skipUnless

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.exceptions import IrreversibleError
from django.test import TransactionTestCase, tag
from django.utils import timezone


@tag('slow')
class ModeratorTakedownMigrationTests(TransactionTestCase):
    migrate_from = ('shares', '0029_add_recoverable_content_deletion')
    migrate_to = ('shares', '0030_add_moderator_takedown')
    sequence_floor = 9_300_003

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        self.addCleanup(self._restore_leaf_migrations)
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        User = old_apps.get_model('auth', 'User')
        Share = old_apps.get_model('shares', 'Share')
        self.author = User.objects.create(username='takedown-migration-author')
        self.share = Share.objects.create(
            share_id='3m4g5t6n',
            title='迁移前审核结果应完整保留',
            strategy_code='[stgy:takedown-migration]',
            description='迁移前内容',
            author_id=self.author.pk,
            category='combat',
            visibility='public',
            status='approved',
            review_feedback='此前已经审核通过',
            views=7,
            copies=3,
        )

    @staticmethod
    def _restore_leaf_migrations():
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def _set_share_sequence(self, value):
        with connection.cursor() as cursor:
            cursor.execute(
                'UPDATE sqlite_sequence SET seq = %s WHERE name = %s',
                [value, 'shares_share'],
            )
            self.assertEqual(cursor.rowcount, 1)

    def _share_sequence(self):
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT seq FROM sqlite_sequence WHERE name = %s',
                ['shares_share'],
            )
            row = cursor.fetchone()
        self.assertIsNotNone(row)
        return row[0]

    def _unused_sequence_floor(self):
        return max(self.sequence_floor, self.share.pk + 1_000_000)

    def test_forward_migration_preserves_every_existing_share_value(self):
        before = tuple(
            type(self.share).objects.filter(pk=self.share.pk).values().get().items()
        )

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        new_apps = executor.loader.project_state([self.migrate_to]).apps
        Share = new_apps.get_model('shares', 'Share')
        after = tuple(Share.objects.filter(pk=self.share.pk).values().get().items())

        self.assertEqual(after, before)
        choices = dict(Share._meta.get_field('restriction_state').choices)
        self.assertEqual(choices['moderator_takedown'], '管理员下架限制')

    @skipUnless(connection.vendor == 'sqlite', 'SQLite-specific sequence contract')
    def test_forward_migration_preserves_share_sequence_high_water_mark(self):
        sequence_floor = self._unused_sequence_floor()
        self._set_share_sequence(sequence_floor)

        MigrationExecutor(connection).migrate([self.migrate_to])

        self.assertEqual(self._share_sequence(), sequence_floor)

    @skipUnless(connection.vendor == 'sqlite', 'SQLite-specific sequence contract')
    def test_reverse_migration_preserves_share_sequence_high_water_mark(self):
        MigrationExecutor(connection).migrate([self.migrate_to])
        sequence_floor = self._unused_sequence_floor()
        self._set_share_sequence(sequence_floor)

        MigrationExecutor(connection).migrate([self.migrate_from])

        self.assertEqual(self._share_sequence(), sequence_floor)

    def test_reverse_refuses_to_discard_an_active_moderator_takedown(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        new_apps = executor.loader.project_state([self.migrate_to]).apps
        Share = new_apps.get_model('shares', 'Share')
        Share.objects.filter(pk=self.share.pk).update(
            restriction_state='moderator_takedown',
            restriction_reason='仍在使用的管理员下架说明',
            restricted_at=timezone.now(),
            restricted_by_id=self.author.pk,
        )

        with self.assertRaisesRegex(
            IrreversibleError,
            'moderator takedown semantics',
        ):
            MigrationExecutor(connection).migrate([self.migrate_from])

    def test_reverse_refuses_to_make_a_released_takedown_log_unknown(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        new_apps = executor.loader.project_state([self.migrate_to]).apps
        ShareLog = new_apps.get_model('shares', 'ShareLog')
        ShareLog.objects.create(
            share_id=self.share.pk,
            user_id=self.author.pk,
            action='moderator_takedown',
            details='限制后来虽已解除，审计事实仍必须可读。',
        )

        with self.assertRaisesRegex(
            IrreversibleError,
            'moderator takedown semantics',
        ):
            MigrationExecutor(connection).migrate([self.migrate_from])
