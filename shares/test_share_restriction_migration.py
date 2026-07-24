from datetime import timedelta
from importlib import import_module

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.exceptions import IrreversibleError
from django.test import TransactionTestCase, tag
from django.utils import timezone


@tag('slow')
class ShareRestrictionMigrationTests(TransactionTestCase):
    """Protect the historical moderation-state recovery in migration 0022."""

    migrate_from = ('shares', '0021_add_data_integrity_constraints')
    migrate_to = ('shares', '0022_add_share_restrictions')

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        self.fixture = self._create_pre_migration_fixture(old_apps)

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def _create_pre_migration_fixture(self, apps):
        User = apps.get_model('auth', 'User')
        Share = apps.get_model('shares', 'Share')
        Report = apps.get_model('shares', 'Report')
        ShareLog = apps.get_model('shares', 'ShareLog')

        author = User.objects.create(username='migration-author')
        reporter = User.objects.create(username='migration-reporter')
        moderator = User.objects.create(username='migration-moderator')
        second_moderator = User.objects.create(username='migration-moderator-2')

        base_time = timezone.now() - timedelta(days=30)
        shares = {}

        def create_share(key, **overrides):
            sequence = len(shares) + 1
            values = {
                'share_id': f'2a3b4c{sequence:02d}',
                'title': f'Migration fixture {key}',
                'strategy_code': f'[stgy:{key}]',
                'description': f'Preserve description {key}',
                'author_id': author.pk,
                'category': 'combat' if sequence % 2 else 'entertainment',
                'visibility': 'public',
                'status': 'approved',
                'review_feedback': '',
                'reviewed_at': None,
                'reviewed_by_id': None,
                'is_spoiler': sequence % 2 == 0,
                'is_nsfw': sequence % 3 == 0,
                'is_original': sequence % 2 == 1,
                'views': sequence * 11,
                'copies': sequence * 7,
            }
            values.update(overrides)
            share = Share.objects.create(**values)
            fixture_time = base_time + timedelta(hours=sequence)
            Share.objects.filter(pk=share.pk).update(
                created_at=fixture_time,
                updated_at=fixture_time + timedelta(minutes=5),
            )
            share.refresh_from_db()
            shares[key] = share
            return share

        create_share('clear')
        create_share(
            'legacy_private',
            status='approved',
            visibility='private',
        )
        create_share(
            'current_rejected',
            status='rejected',
            visibility='private',
            review_feedback='Current review reason',
            reviewed_at=base_time + timedelta(days=1),
            reviewed_by_id=moderator.pk,
        )
        bypassed = create_share(
            'author_bypassed_rejection',
            status='approved',
            visibility='unlisted',
        )
        approved_after_rejection = create_share(
            'approved_after_rejection',
            status='approved',
            visibility='public',
            review_feedback='Old feedback intentionally preserved',
            reviewed_at=base_time + timedelta(days=5),
            reviewed_by_id=second_moderator.pk,
        )
        reported_then_approved = create_share(
            'reported_then_approved',
            status='approved',
            visibility='public',
            reviewed_at=base_time + timedelta(days=8),
            reviewed_by_id=second_moderator.pk,
        )
        multiple_reports = create_share(
            'multiple_reports',
            status='approved',
            visibility='unlisted',
        )
        create_share(
            'missing_review_metadata',
            status='rejected',
            visibility='private',
        )
        missing_report_metadata = create_share(
            'missing_report_metadata',
            status='approved',
            visibility='public',
        )

        def create_log(share, action, details, created_at, user=moderator):
            log = ShareLog.objects.create(
                share_id=share.pk,
                user_id=user.pk,
                action=action,
                details=details,
            )
            ShareLog.objects.filter(pk=log.pk).update(created_at=created_at)
            log.refresh_from_db()
            return log

        bypass_log = create_log(
            bypassed,
            'reject',
            'Historical rejection survives an author edit',
            base_time + timedelta(days=3),
        )
        create_log(
            approved_after_rejection,
            'reject',
            'Superseded rejection',
            base_time + timedelta(days=4),
        )
        approval_log = create_log(
            approved_after_rejection,
            'approve',
            'Moderator explicitly approved the resubmission',
            base_time + timedelta(days=5),
            user=second_moderator,
        )

        report = Report.objects.create(
            share_id=reported_then_approved.pk,
            reporter_id=reporter.pk,
            reason='Report fixture',
            status='resolved',
            resolved_at=base_time + timedelta(days=6),
            resolved_by_id=moderator.pk,
            resolution_reason='Resolved report remains authoritative',
        )
        Report.objects.filter(pk=report.pk).update(
            created_at=base_time + timedelta(days=5),
        )
        later_report_approval_log = create_log(
            reported_then_approved,
            'approve',
            'Approval after report resolution does not erase a takedown',
            base_time + timedelta(days=8),
            user=second_moderator,
        )

        old_report = Report.objects.create(
            share_id=multiple_reports.pk,
            reporter_id=reporter.pk,
            reason='Older report',
            status='resolved',
            resolved_at=base_time + timedelta(days=9),
            resolved_by_id=moderator.pk,
            resolution_reason='Older resolution',
        )
        Report.objects.filter(pk=old_report.pk).update(
            created_at=base_time + timedelta(days=8),
        )
        latest_report = Report.objects.create(
            share_id=multiple_reports.pk,
            reporter_id=reporter.pk,
            reason='Latest report',
            status='resolved',
            resolved_at=base_time + timedelta(days=10),
            resolved_by_id=second_moderator.pk,
            resolution_reason='Latest resolution wins',
        )
        Report.objects.filter(pk=latest_report.pk).update(
            created_at=base_time + timedelta(days=9),
        )

        missing_report = Report.objects.create(
            share_id=missing_report_metadata.pk,
            reporter_id=reporter.pk,
            reason='Report without retained resolution metadata',
            status='resolved',
            resolved_at=base_time + timedelta(days=11),
            resolved_by_id=None,
            resolution_reason='',
        )
        Report.objects.filter(pk=missing_report.pk).update(
            created_at=base_time + timedelta(days=10),
        )

        # Snapshot every pre-existing concrete Share field. The data migration
        # may add restriction metadata, but must not rewrite user-owned data or
        # legacy moderation fields while recovering that metadata.
        old_share_field_names = [
            field.attname
            for field in Share._meta.concrete_fields
        ]
        old_share_values = {
            key: {
                field_name: getattr(share, field_name)
                for field_name in old_share_field_names
            }
            for key, share in shares.items()
        }

        return {
            'shares': {key: share.pk for key, share in shares.items()},
            'old_share_values': old_share_values,
            'moderator_id': moderator.pk,
            'reporter_id': reporter.pk,
            'report_ids': list(Report.objects.values_list('pk', flat=True)),
            'second_moderator_id': second_moderator.pk,
            'bypass_log_id': bypass_log.pk,
            'approval_log_id': approval_log.pk,
            'later_report_approval_log_id': later_report_approval_log.pk,
            'latest_report_time': latest_report.resolved_at,
            'missing_report_time': missing_report.resolved_at,
        }

    def test_backfill_recovers_active_restrictions_without_rewriting_shares(self):
        Share = self.apps.get_model('shares', 'Share')
        ShareLog = self.apps.get_model('shares', 'ShareLog')
        User = self.apps.get_model('auth', 'User')

        shares = {
            key: Share.objects.get(pk=share_id)
            for key, share_id in self.fixture['shares'].items()
        }

        self.assertEqual(shares['clear'].restriction_state, 'clear')
        self.assertEqual(shares['clear'].restriction_reason, '')
        self.assertIsNone(shares['clear'].restricted_at)
        self.assertIsNone(shares['clear'].restricted_by_id)

        legacy_private = shares['legacy_private']
        self.assertEqual(legacy_private.restriction_state, 'legacy_private')
        self.assertEqual(
            legacy_private.restriction_reason,
            '历史私密状态来源待人工确认',
        )
        self.assertIsNotNone(legacy_private.restricted_at)
        self.assertIsNone(legacy_private.restricted_by_id)

        current_rejected = shares['current_rejected']
        self.assertEqual(current_rejected.restriction_state, 'review_rejected')
        self.assertEqual(
            current_rejected.restriction_reason,
            'Current review reason',
        )
        self.assertEqual(
            current_rejected.restricted_by_id,
            self.fixture['moderator_id'],
        )

        bypassed = shares['author_bypassed_rejection']
        self.assertEqual(bypassed.restriction_state, 'review_rejected')
        self.assertEqual(
            bypassed.restriction_reason,
            'Historical rejection survives an author edit',
        )
        self.assertEqual(
            bypassed.restricted_by_id,
            self.fixture['moderator_id'],
        )

        approved_after_rejection = shares['approved_after_rejection']
        self.assertEqual(approved_after_rejection.restriction_state, 'clear')
        self.assertEqual(approved_after_rejection.restriction_reason, '')
        self.assertIsNone(approved_after_rejection.restricted_at)
        self.assertIsNone(approved_after_rejection.restricted_by_id)

        reported_then_approved = shares['reported_then_approved']
        self.assertEqual(
            reported_then_approved.restriction_state,
            'report_takedown',
        )
        self.assertEqual(
            reported_then_approved.restriction_reason,
            'Resolved report remains authoritative',
        )
        self.assertEqual(
            reported_then_approved.restricted_by_id,
            self.fixture['moderator_id'],
        )

        multiple_reports = shares['multiple_reports']
        self.assertEqual(multiple_reports.restriction_state, 'report_takedown')
        self.assertEqual(
            multiple_reports.restriction_reason,
            'Latest resolution wins',
        )
        self.assertEqual(
            multiple_reports.restricted_at,
            self.fixture['latest_report_time'],
        )
        self.assertEqual(
            multiple_reports.restricted_by_id,
            self.fixture['second_moderator_id'],
        )

        missing_review = shares['missing_review_metadata']
        self.assertEqual(missing_review.restriction_state, 'review_rejected')
        self.assertTrue(missing_review.restriction_reason.strip())
        self.assertIsNotNone(missing_review.restricted_at)
        self.assertIsNone(missing_review.restricted_by_id)

        missing_report = shares['missing_report_metadata']
        self.assertEqual(missing_report.restriction_state, 'report_takedown')
        self.assertTrue(missing_report.restriction_reason.strip())
        self.assertEqual(
            missing_report.restricted_at,
            self.fixture['missing_report_time'],
        )
        self.assertIsNone(missing_report.restricted_by_id)

        for key, share in shares.items():
            with self.subTest(share=key, invariant='legacy fields preserved'):
                old_values = self.fixture['old_share_values'][key]
                for field_name, old_value in old_values.items():
                    self.assertEqual(getattr(share, field_name), old_value)

        moderator_log_ids = set(
            ShareLog.objects.filter(
                user_id=self.fixture['moderator_id'],
            ).values_list('pk', flat=True)
        )
        self.assertIn(self.fixture['bypass_log_id'], moderator_log_ids)

        User.objects.get(pk=self.fixture['moderator_id']).delete()

        self.assertEqual(
            ShareLog.objects.filter(pk__in=moderator_log_ids).count(),
            len(moderator_log_ids),
        )
        self.assertFalse(
            ShareLog.objects.filter(
                pk__in=moderator_log_ids,
                user_id__isnull=False,
            ).exists(),
        )
        bypassed.refresh_from_db()
        self.assertEqual(bypassed.restriction_state, 'review_rejected')
        self.assertIsNone(bypassed.restricted_by_id)

        Report = self.apps.get_model('shares', 'Report')
        User.objects.get(pk=self.fixture['reporter_id']).delete()
        self.assertEqual(
            Report.objects.filter(pk__in=self.fixture['report_ids']).count(),
            len(self.fixture['report_ids']),
        )
        self.assertFalse(
            Report.objects.filter(
                pk__in=self.fixture['report_ids'],
                reporter_id__isnull=False,
            ).exists(),
        )

        migration = import_module('shares.migrations.0022_add_share_restrictions')
        with self.assertRaises(IrreversibleError):
            migration.reverse_share_restrictions(self.apps, None)
