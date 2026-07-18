from datetime import timedelta
from importlib import import_module

from django.db import connection, migrations
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase
from django.utils import timezone


class ModerationMetadataMigrationTests(TransactionTestCase):
    migrate_from = ('shares', '0018_default_home_feed_waterfall')
    migrate_to = (
        'shares',
        '0019_report_resolution_reason_share_review_feedback_and_more',
    )

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        self.addCleanup(self._restore_leaf_migrations)
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        self.fixture = self._create_old_fixture(old_apps)
        self.old_snapshot = self._snapshot_old_data(old_apps)

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    @staticmethod
    def _restore_leaf_migrations():
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def _create_old_fixture(self, apps):
        User = apps.get_model('auth', 'User')
        Share = apps.get_model('shares', 'Share')
        Report = apps.get_model('shares', 'Report')

        author = User.objects.create(
            username='migration-0019-author',
            email='author@example.test',
        )
        reporter = User.objects.create(
            username='migration-0019-reporter',
            email='reporter@example.test',
        )
        moderator = User.objects.create(
            username='migration-0019-moderator',
            email='moderator@example.test',
        )
        share = Share.objects.create(
            share_id='0019snapshotshare0001',
            title='0019 snapshot title',
            strategy_code='[stgy:0019-snapshot]',
            description='All pre-0019 fields must survive unchanged.',
            author_id=author.pk,
            visibility='unlisted',
            status='rejected',
            category='combat',
            is_spoiler=True,
            is_nsfw=True,
            is_original=True,
            views=37,
            copies=19,
        )
        share.likes.add(reporter)
        share.favorites.add(moderator)
        report = Report.objects.create(
            share_id=share.pk,
            reporter_id=reporter.pk,
            reason='Preserve the original report reason.',
            status='resolved',
            resolved_at=timezone.now() - timedelta(days=1),
            resolved_by_id=moderator.pk,
        )
        return {
            'author_id': author.pk,
            'moderator_id': moderator.pk,
            'report_id': report.pk,
            'share_id': share.pk,
        }

    @staticmethod
    def _concrete_field_names(model):
        return tuple(field.name for field in model._meta.concrete_fields)

    def _snapshot_old_data(self, apps):
        Share = apps.get_model('shares', 'Share')
        Report = apps.get_model('shares', 'Report')
        share_fields = self._concrete_field_names(Share)
        report_fields = self._concrete_field_names(Report)
        return {
            'share_fields': share_fields,
            'report_fields': report_fields,
            'shares': list(Share.objects.order_by('pk').values(*share_fields)),
            'reports': list(Report.objects.order_by('pk').values(*report_fields)),
            'likes': list(
                Share.likes.through.objects.order_by('pk').values(
                    'share_id',
                    'user_id',
                )
            ),
            'favorites': list(
                Share.favorites.through.objects.order_by('pk').values(
                    'share_id',
                    'user_id',
                )
            ),
        }

    def _assert_old_snapshot_unchanged(self, apps):
        Share = apps.get_model('shares', 'Share')
        Report = apps.get_model('shares', 'Report')
        self.assertEqual(
            list(
                Share.objects.order_by('pk').values(
                    *self.old_snapshot['share_fields']
                )
            ),
            self.old_snapshot['shares'],
        )
        self.assertEqual(
            list(
                Report.objects.order_by('pk').values(
                    *self.old_snapshot['report_fields']
                )
            ),
            self.old_snapshot['reports'],
        )
        self.assertEqual(
            list(
                Share.likes.through.objects.order_by('pk').values(
                    'share_id',
                    'user_id',
                )
            ),
            self.old_snapshot['likes'],
        )
        self.assertEqual(
            list(
                Share.favorites.through.objects.order_by('pk').values(
                    'share_id',
                    'user_id',
                )
            ),
            self.old_snapshot['favorites'],
        )

    def _assert_schema_is_still_at_0019(self):
        self.assertTrue(
            MigrationRecorder(connection).migration_qs.filter(
                app='shares',
                name=self.migrate_to[1],
            ).exists()
        )
        table_names = set(connection.introspection.table_names())
        self.assertIn('shares_sitemessage', table_names)
        with connection.cursor() as cursor:
            share_columns = {
                column.name
                for column in connection.introspection.get_table_description(
                    cursor,
                    'shares_share',
                )
            }
            report_columns = {
                column.name
                for column in connection.introspection.get_table_description(
                    cursor,
                    'shares_report',
                )
            }
        self.assertTrue(
            {'review_feedback', 'reviewed_at', 'reviewed_by_id'}
            <= share_columns
        )
        self.assertIn('resolution_reason', report_columns)

    def _attempt_reverse(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])

    def test_reverse_guard_is_the_final_forward_operation(self):
        migration_module = import_module(
            'shares.migrations.'
            '0019_report_resolution_reason_share_review_feedback_and_more'
        )
        operation = migration_module.Migration.operations[-1]

        self.assertIsInstance(operation, migrations.RunPython)
        self.assertIs(operation.code, migrations.RunPython.noop)
        self.assertIs(
            operation.reverse_code,
            migration_module.guard_moderation_data_before_reverse,
        )

    def test_forward_adds_empty_defaults_without_changing_old_fields(self):
        Share = self.apps.get_model('shares', 'Share')
        Report = self.apps.get_model('shares', 'Report')
        SiteMessage = self.apps.get_model('shares', 'SiteMessage')

        self._assert_old_snapshot_unchanged(self.apps)
        share = Share.objects.get(pk=self.fixture['share_id'])
        report = Report.objects.get(pk=self.fixture['report_id'])
        self.assertEqual(share.review_feedback, '')
        self.assertIsNone(share.reviewed_at)
        self.assertIsNone(share.reviewed_by_id)
        self.assertEqual(report.resolution_reason, '')
        self.assertFalse(SiteMessage.objects.exists())

    def test_reverse_rejects_each_kind_of_non_default_moderation_data(self):
        Share = self.apps.get_model('shares', 'Share')
        Report = self.apps.get_model('shares', 'Report')
        SiteMessage = self.apps.get_model('shares', 'SiteMessage')

        cases = (
            (
                'site message',
                lambda: SiteMessage.objects.create(
                    recipient_id=self.fixture['author_id'],
                    message_type='share_rejected',
                    title='Preserve message',
                    content='This row must not be dropped.',
                ),
                lambda: SiteMessage.objects.all().delete(),
                lambda: self.assertEqual(SiteMessage.objects.count(), 1),
            ),
            (
                'report resolution reason',
                lambda: Report.objects.filter(pk=self.fixture['report_id']).update(
                    resolution_reason='Preserve resolution reason'
                ),
                lambda: Report.objects.filter(pk=self.fixture['report_id']).update(
                    resolution_reason=''
                ),
                lambda: self.assertEqual(
                    Report.objects.get(pk=self.fixture['report_id']).resolution_reason,
                    'Preserve resolution reason',
                ),
            ),
            (
                'share review feedback',
                lambda: Share.objects.filter(pk=self.fixture['share_id']).update(
                    review_feedback='Preserve review feedback'
                ),
                lambda: Share.objects.filter(pk=self.fixture['share_id']).update(
                    review_feedback=''
                ),
                lambda: self.assertEqual(
                    Share.objects.get(pk=self.fixture['share_id']).review_feedback,
                    'Preserve review feedback',
                ),
            ),
            (
                'share reviewed time',
                lambda: Share.objects.filter(pk=self.fixture['share_id']).update(
                    reviewed_at=timezone.now()
                ),
                lambda: Share.objects.filter(pk=self.fixture['share_id']).update(
                    reviewed_at=None
                ),
                lambda: self.assertIsNotNone(
                    Share.objects.get(pk=self.fixture['share_id']).reviewed_at
                ),
            ),
            (
                'share reviewer',
                lambda: Share.objects.filter(pk=self.fixture['share_id']).update(
                    reviewed_by_id=self.fixture['moderator_id']
                ),
                lambda: Share.objects.filter(pk=self.fixture['share_id']).update(
                    reviewed_by_id=None
                ),
                lambda: self.assertEqual(
                    Share.objects.get(pk=self.fixture['share_id']).reviewed_by_id,
                    self.fixture['moderator_id'],
                ),
            ),
        )

        for label, arrange, cleanup, assert_preserved in cases:
            with self.subTest(label=label):
                arrange()
                with self.assertRaises(IrreversibleError):
                    self._attempt_reverse()
                self._assert_schema_is_still_at_0019()
                assert_preserved()
                cleanup()

    def test_reverse_succeeds_when_all_new_data_is_empty_or_default(self):
        self._attempt_reverse()
        reversed_apps = MigrationExecutor(connection).loader.project_state(
            [self.migrate_from]
        ).apps

        self._assert_old_snapshot_unchanged(reversed_apps)
        self.assertFalse(
            MigrationRecorder(connection).migration_qs.filter(
                app='shares',
                name=self.migrate_to[1],
            ).exists()
        )
        self.assertNotIn(
            'shares_sitemessage',
            set(connection.introspection.table_names()),
        )
        Share = reversed_apps.get_model('shares', 'Share')
        Report = reversed_apps.get_model('shares', 'Report')
        self.assertNotIn(
            'review_feedback',
            {field.name for field in Share._meta.get_fields()},
        )
        self.assertNotIn(
            'reviewed_at',
            {field.name for field in Share._meta.get_fields()},
        )
        self.assertNotIn(
            'reviewed_by',
            {field.name for field in Share._meta.get_fields()},
        )
        self.assertNotIn(
            'resolution_reason',
            {field.name for field in Report._meta.get_fields()},
        )
