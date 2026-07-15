import json
from datetime import timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from .models import Report, Share, ShareLog
from .services.moderation import (
    confirm_share_restriction,
    release_share_restriction,
)


class ShareRestrictionPreflightTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(username='preflight-author')
        self.admin = User.objects.create_user(username='preflight-admin', is_staff=True)

    def create_share(self, suffix, **overrides):
        fields = {
            'title': f'预检分享 {suffix}',
            'strategy_code': f'[stgy:preflight-{suffix}]',
            'author': self.author,
        }
        fields.update(overrides)
        return Share.objects.create(**fields)

    def create_resolved_report(self, share, *, resolved_at):
        return Report.objects.create(
            share=share,
            reporter=self.author,
            reason='历史举报',
            status=Report.Status.RESOLVED,
            resolved_at=resolved_at,
            resolved_by=self.admin,
            resolution_reason='确认违规',
        )

    def create_log(self, share, action, *, created_at):
        log = ShareLog.objects.create(
            share=share,
            user=self.admin,
            action=action,
            details='预检时序证据',
        )
        ShareLog.objects.filter(pk=log.pk).update(created_at=created_at)
        log.refresh_from_db()
        return log

    def command_payload(self, stdout):
        return json.JSONDecoder().raw_decode(stdout.getvalue())[0]

    def test_legacy_private_is_fail_closed_until_explicitly_classified_clear(self):
        restricted_at = timezone.now() - timedelta(days=1)
        legacy = self.create_share(
            'legacy-private',
            visibility=Share.Visibility.PRIVATE,
            restriction_state=Share.RestrictionState.LEGACY_PRIVATE,
            restriction_reason='历史私密状态来源待人工确认',
            restricted_at=restricted_at,
        )

        stdout = StringIO()
        call_command('preflight_share_restrictions', stdout=stdout, stderr=StringIO())
        payload = self.command_payload(stdout)

        self.assertTrue(payload['valid'])
        self.assertFalse(payload['ready_for_cutover'])
        self.assertEqual(payload['counts']['legacy_private_reviews'], 1)
        self.assertEqual(payload['manual_review']['share_ids'], [legacy.share_id])
        with self.assertRaises(CommandError):
            call_command(
                'preflight_share_restrictions',
                strict=True,
                stdout=StringIO(),
                stderr=StringIO(),
            )

        result = release_share_restriction(
            share_id=legacy.share_id,
            moderator=self.admin,
            reason='确认这是作者主动设置的私密内容',
        )
        self.assertTrue(result.changed)
        call_command(
            'preflight_share_restrictions',
            strict=True,
            stdout=StringIO(),
            stderr=StringIO(),
        )

    def test_missing_takedown_is_blocking_and_report_is_written_first(self):
        share = self.create_share('missing-restriction')
        self.create_resolved_report(
            share,
            resolved_at=timezone.now() - timedelta(days=1),
        )
        with TemporaryDirectory() as temporary:
            report_path = Path(temporary) / 'preflight.json'
            with self.assertRaises(CommandError):
                call_command(
                    'preflight_share_restrictions',
                    output=str(report_path),
                )
            payload = json.loads(report_path.read_text(encoding='utf-8'))

        self.assertFalse(payload['valid'])
        self.assertEqual(
            payload['blocking_errors'][0]['check'],
            'resolved_report_without_takedown',
        )
        self.assertEqual(
            payload['blocking_errors'][0]['share_ids'],
            [share.share_id],
        )

    def test_approval_after_takedown_can_be_audited_as_keep_or_release(self):
        resolved_at = timezone.now() - timedelta(days=2)
        share = self.create_share(
            'ambiguous',
            restriction_state=Share.RestrictionState.REPORT_TAKEDOWN,
            restriction_reason='确认违规',
            restricted_at=resolved_at,
            restricted_by=self.admin,
        )
        self.create_resolved_report(share, resolved_at=resolved_at)
        self.create_log(
            share,
            ShareLog.ActionType.REVIEW_APPROVE,
            created_at=resolved_at + timedelta(days=1),
        )

        stdout = StringIO()
        call_command('preflight_share_restrictions', stdout=stdout, stderr=StringIO())
        payload = self.command_payload(stdout)
        self.assertEqual(payload['manual_review']['count'], 1)
        with self.assertRaises(CommandError):
            call_command(
                'preflight_share_restrictions',
                strict=True,
                stdout=StringIO(),
                stderr=StringIO(),
            )

        result = confirm_share_restriction(
            share_id=share.share_id,
            moderator=self.admin,
            reason='人工复核后确认继续下架',
            expected_version=share.updated_at,
        )
        self.assertTrue(result.changed)
        call_command(
            'preflight_share_restrictions',
            strict=True,
            stdout=StringIO(),
            stderr=StringIO(),
        )

        release_share_restriction(
            share_id=share.share_id,
            moderator=self.admin,
            reason='再次复核后解除限制',
        )
        call_command(
            'preflight_share_restrictions',
            strict=True,
            stdout=StringIO(),
            stderr=StringIO(),
        )

    def test_reject_after_historical_report_release_is_still_active_evidence(self):
        base_time = timezone.now() - timedelta(days=3)
        share = self.create_share('released-then-rejected', status=Share.Status.PENDING)
        self.create_resolved_report(share, resolved_at=base_time)
        self.create_log(
            share,
            ShareLog.ActionType.RESTRICTION_RELEASE,
            created_at=base_time + timedelta(days=1),
        )
        self.create_log(
            share,
            ShareLog.ActionType.REVIEW_REJECT,
            created_at=base_time + timedelta(days=2),
        )

        with self.assertRaises(CommandError):
            stdout = StringIO()
            call_command('preflight_share_restrictions', strict=True, stdout=stdout)
        payload = self.command_payload(stdout)
        check = next(
            item for item in payload['blocking_errors']
            if item['check'] == 'active_reject_log_without_restriction'
        )
        self.assertEqual(check['share_ids'], [share.share_id])

    def test_restriction_overridden_by_later_approval_is_blocking(self):
        base_time = timezone.now() - timedelta(days=2)
        share = self.create_share(
            'stale-review-restriction',
            restriction_state=Share.RestrictionState.REVIEW_REJECTED,
            restriction_reason='已被后续通过覆盖',
            restricted_at=base_time,
            restricted_by=self.admin,
        )
        self.create_log(
            share,
            ShareLog.ActionType.REVIEW_REJECT,
            created_at=base_time,
        )
        self.create_log(
            share,
            ShareLog.ActionType.REVIEW_APPROVE,
            created_at=base_time + timedelta(days=1),
        )

        with self.assertRaises(CommandError):
            stdout = StringIO()
            call_command('preflight_share_restrictions', strict=True, stdout=stdout)
        payload = self.command_payload(stdout)
        check = next(
            item for item in payload['blocking_errors']
            if item['check'] == 'restriction_without_active_evidence'
        )
        self.assertEqual(check['share_ids'], [share.share_id])

    def test_report_contains_every_unclassified_private_share_id(self):
        shares = [
            self.create_share(f'private-{index}', visibility=Share.Visibility.PRIVATE)
            for index in range(55)
        ]

        with self.assertRaises(CommandError):
            stdout = StringIO()
            call_command('preflight_share_restrictions', stdout=stdout)
        payload = self.command_payload(stdout)
        check = next(
            item for item in payload['blocking_errors']
            if item['check'] == 'private_clear_without_classification'
        )
        self.assertEqual(check['count'], 55)
        self.assertEqual(
            set(check['share_ids']),
            {share.share_id for share in shares},
        )

    def test_whitespace_only_restriction_reason_is_blocking(self):
        restricted_at = timezone.now() - timedelta(days=1)
        share = self.create_share(
            'blank-reason',
            restriction_state=Share.RestrictionState.REPORT_TAKEDOWN,
            restriction_reason='   ',
            restricted_at=restricted_at,
            restricted_by=self.admin,
        )
        self.create_resolved_report(share, resolved_at=restricted_at)

        with self.assertRaises(CommandError):
            stdout = StringIO()
            call_command('preflight_share_restrictions', strict=True, stdout=stdout)
        payload = self.command_payload(stdout)
        check = next(
            item for item in payload['blocking_errors']
            if item['check'] == 'invalid_restriction_metadata'
        )
        self.assertEqual(check['share_ids'], [share.share_id])
