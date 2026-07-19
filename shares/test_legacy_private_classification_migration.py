from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from shares.models import Share, ShareLog, SiteMessage
from shares.services.restriction_preflight import build_share_restriction_preflight


class LegacyPrivateClassificationMigrationTests(TransactionTestCase):
    migrate_from = ('shares', '0026_sync_announcement_permission_names')
    migrate_to = ('shares', '0027_classify_legacy_private_shares')
    decisions = {
        '2k5d2w5w': 'confirm_restriction',
        '4s2v4e9n': 'release_restriction',
        '8b8y9s3j': 'release_restriction',
        '8n9b6e6b': 'release_restriction',
    }

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        User = old_apps.get_model('auth', 'User')
        OldShare = old_apps.get_model('shares', 'Share')
        author = User.objects.create(username='legacy-private-author')
        base_time = timezone.now().replace(microsecond=123000) - timedelta(days=1)
        self.expected_updated_at = {}

        for offset, share_id in enumerate(self.decisions):
            updated_at = base_time + timedelta(minutes=offset)
            share = OldShare.objects.create(
                share_id=share_id,
                title=f'legacy-private-{share_id}',
                strategy_code='[stgy:legacy-private-classification]',
                author=author,
                status='approved',
                visibility='private',
                restriction_state='legacy_private',
                restriction_reason='历史私密状态来源待人工确认',
                restricted_at=updated_at,
            )
            OldShare.objects.filter(pk=share.pk).update(
                created_at=updated_at,
                updated_at=updated_at,
                restricted_at=updated_at,
            )
            self.expected_updated_at[share_id] = updated_at

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])

    def tearDown(self):
        MigrationExecutor(connection).migrate([self.migrate_to])
        super().tearDown()

    def test_classifies_records_without_changing_private_visibility(self):
        for share_id, expected_action in self.decisions.items():
            share = Share.objects.get(share_id=share_id)
            self.assertEqual(share.visibility, Share.Visibility.PRIVATE)
            self.assertEqual(share.status, Share.Status.APPROVED)
            self.assertEqual(share.updated_at, self.expected_updated_at[share_id])

            log = ShareLog.objects.get(
                share=share,
                action=expected_action,
            )
            self.assertIsNone(log.user)
            self.assertEqual(log.created_at, self.expected_updated_at[share_id])

            if expected_action == ShareLog.ActionType.RESTRICTION_CONFIRM:
                self.assertEqual(
                    share.restriction_state,
                    Share.RestrictionState.REPORT_TAKEDOWN,
                )
                self.assertIn('疑似重复', share.restriction_reason)
                self.assertEqual(
                    share.restricted_at,
                    self.expected_updated_at[share_id],
                )
            else:
                self.assertEqual(
                    share.restriction_state,
                    Share.RestrictionState.CLEAR,
                )
                self.assertEqual(share.restriction_reason, '')
                self.assertIsNone(share.restricted_at)

        self.assertEqual(SiteMessage.objects.count(), 0)
        preflight = build_share_restriction_preflight()
        self.assertEqual(preflight['blocking_errors'], [])
        self.assertEqual(preflight['manual_review']['count'], 0)
        self.assertTrue(preflight['ready_for_cutover'])

    def test_reverse_restores_legacy_state_and_removes_decision_logs(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        OldShare = old_apps.get_model('shares', 'Share')
        OldShareLog = old_apps.get_model('shares', 'ShareLog')

        for share_id in self.decisions:
            share = OldShare.objects.get(share_id=share_id)
            self.assertEqual(share.restriction_state, 'legacy_private')
            self.assertEqual(
                share.restriction_reason,
                '历史私密状态来源待人工确认',
            )
            self.assertEqual(
                share.restricted_at,
                self.expected_updated_at[share_id],
            )
            self.assertFalse(
                OldShareLog.objects.filter(
                    share_id=share.pk,
                    action__in=('confirm_restriction', 'release_restriction'),
                ).exists()
            )
