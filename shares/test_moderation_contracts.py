from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Announcement, Report, Share, ShareLog, SiteMessage


class ModerationWorkflowContractTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(username='author', password='password123')
        self.reporter = User.objects.create_user(username='reporter', password='password123')
        self.second_reporter = User.objects.create_user(username='reporter2', password='password123')
        self.regular_user = User.objects.create_user(username='regular', password='password123')
        self.admin = User.objects.create_user(username='admin', password='password123', is_staff=True)

    def create_share(self, *, status=Share.Status.APPROVED, **overrides):
        data = {
            'title': '审核测试',
            'strategy_code': '[stgy:moderation]',
            'author': self.author,
            'visibility': Share.Visibility.PUBLIC,
            'status': status,
        }
        data.update(overrides)
        return Share.objects.create(
            **data,
        )

    def restriction_fields(self, *, state, reason, restricted_at=None, restricted_by=None):
        return {
            'restriction_state': state,
            'restriction_reason': reason,
            'restricted_at': restricted_at or timezone.now(),
            'restricted_by': restricted_by or self.admin,
        }

    def create_report(self, share, reporter=None):
        return Report.objects.create(
            share=share,
            reporter=reporter or self.reporter,
            reason='需要核查',
        )

    def test_admin_approve_records_review_metadata_and_log(self):
        share = self.create_share(status=Share.Status.PENDING)
        self.client.force_login(self.admin)

        response = self.client.post(reverse('admin_approve_share', args=[share.share_id]))

        self.assertRedirects(response, reverse('admin_review_list'))
        share.refresh_from_db()
        self.assertEqual(share.status, Share.Status.APPROVED)
        self.assertEqual(share.review_feedback, '')
        self.assertIsNotNone(share.reviewed_at)
        self.assertEqual(share.reviewed_by, self.admin)
        self.assertTrue(ShareLog.objects.filter(
            share=share,
            user=self.admin,
            action=ShareLog.ActionType.REVIEW_APPROVE,
        ).exists())

        self.client.post(reverse('admin_approve_share', args=[share.share_id]))

        self.assertEqual(ShareLog.objects.filter(
            share=share,
            user=self.admin,
            action=ShareLog.ActionType.REVIEW_APPROVE,
        ).count(), 1)

    def test_approve_restricted_pending_share_clears_restriction_and_audits_both_actions(self):
        share = self.create_share(
            status=Share.Status.PENDING,
            **self.restriction_fields(
                state=Share.RestrictionState.REPORT_TAKEDOWN,
                reason='举报下架后已修改',
            ),
        )
        self.client.force_login(self.admin)

        response = self.client.post(reverse('admin_approve_share', args=[share.share_id]))

        self.assertRedirects(response, reverse('admin_review_list'))
        share.refresh_from_db()
        self.assertEqual(share.status, Share.Status.APPROVED)
        self.assertEqual(share.restriction_state, Share.RestrictionState.CLEAR)
        self.assertEqual(share.restriction_reason, '')
        self.assertIsNone(share.restricted_at)
        self.assertIsNone(share.restricted_by)
        self.assertTrue(ShareLog.objects.filter(
            share=share,
            user=self.admin,
            action=ShareLog.ActionType.REVIEW_APPROVE,
        ).exists())
        self.assertTrue(ShareLog.objects.filter(
            share=share,
            user=self.admin,
            action=ShareLog.ActionType.RESTRICTION_RELEASE,
        ).exists())
        self.assertTrue(SiteMessage.objects.filter(
            recipient=self.author,
            related_share=share,
            message_type=SiteMessage.MessageType.SHARE_RESTORED,
        ).exists())

    def test_resolving_report_takes_share_down_and_notifies_both_sides(self):
        share = self.create_share()
        report = self.create_report(share)
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse('admin_resolve_report', args=[report.id, 'resolve']),
            {'reason': '确认存在违规内容'},
        )

        self.assertRedirects(response, reverse('admin_report_list'))
        share.refresh_from_db()
        report.refresh_from_db()
        self.assertEqual(share.visibility, Share.Visibility.PUBLIC)
        self.assertEqual(
            share.restriction_state,
            Share.RestrictionState.REPORT_TAKEDOWN,
        )
        self.assertEqual(share.restriction_reason, '确认存在违规内容')
        self.assertIsNotNone(share.restricted_at)
        self.assertEqual(share.restricted_by, self.admin)
        self.assertEqual(report.status, Report.Status.RESOLVED)
        self.assertEqual(report.resolution_reason, '确认存在违规内容')
        self.assertEqual(report.resolved_by, self.admin)
        self.assertIsNotNone(report.resolved_at)
        self.assertTrue(SiteMessage.objects.filter(
            recipient=self.reporter,
            message_type=SiteMessage.MessageType.REPORT_RESOLVED,
            related_report=report,
        ).exists())
        self.assertTrue(SiteMessage.objects.filter(
            recipient=self.author,
            message_type=SiteMessage.MessageType.SHARE_TAKEDOWN,
            related_report=report,
        ).exists())

        log_count = ShareLog.objects.filter(
            share=share,
            action=ShareLog.ActionType.REPORT_HANDLE,
        ).count()
        message_count = SiteMessage.objects.filter(related_report=report).count()
        self.client.post(
            reverse('admin_resolve_report', args=[report.id, 'resolve']),
            {'reason': '重复提交不应再次处理'},
        )

        self.assertEqual(ShareLog.objects.filter(
            share=share,
            action=ShareLog.ActionType.REPORT_HANDLE,
        ).count(), log_count)
        self.assertEqual(SiteMessage.objects.filter(related_report=report).count(), message_count)

    def test_batch_dismiss_marks_all_pending_reports_and_notifies_reporters(self):
        share = self.create_share()
        first_report = self.create_report(share)
        second_report = self.create_report(share, reporter=self.second_reporter)
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse('admin_resolve_share_reports', args=[share.share_id, 'dismiss']),
            {'reason': '未发现违规'},
        )

        self.assertRedirects(response, reverse('admin_report_list'))
        for report in (first_report, second_report):
            report.refresh_from_db()
            self.assertEqual(report.status, Report.Status.DISMISSED)
            self.assertEqual(report.resolution_reason, '未发现违规')
            self.assertEqual(report.resolved_by, self.admin)
            self.assertIsNotNone(report.resolved_at)
        share.refresh_from_db()
        self.assertEqual(share.visibility, Share.Visibility.PUBLIC)
        self.assertEqual(
            SiteMessage.objects.filter(message_type=SiteMessage.MessageType.REPORT_DISMISSED).count(),
            2,
        )

        self.client.post(
            reverse('admin_resolve_share_reports', args=[share.share_id, 'dismiss']),
            {'reason': '重复提交不应再次处理'},
        )

        self.assertEqual(
            ShareLog.objects.filter(
                share=share,
                action=ShareLog.ActionType.REPORT_HANDLE,
            ).count(),
            1,
        )
        self.assertEqual(
            SiteMessage.objects.filter(message_type=SiteMessage.MessageType.REPORT_DISMISSED).count(),
            2,
        )

    def test_approve_rolls_back_when_audit_log_fails(self):
        share = self.create_share(status=Share.Status.PENDING)
        self.client.force_login(self.admin)

        with patch(
            'shares.services.moderation.log_share_action',
            side_effect=RuntimeError('log failed'),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse('admin_approve_share', args=[share.share_id]))

        share.refresh_from_db()
        self.assertEqual(share.status, Share.Status.PENDING)
        self.assertIsNone(share.reviewed_at)
        self.assertFalse(ShareLog.objects.filter(share=share).exists())

    def test_reject_is_idempotent(self):
        share = self.create_share(status=Share.Status.PENDING)
        self.client.force_login(self.admin)
        url = reverse('admin_reject_share', args=[share.share_id])

        self.client.post(url, {'reason': '需要修改'})
        self.client.post(url, {'reason': '重复提交'})

        share.refresh_from_db()
        self.assertEqual(share.status, Share.Status.REJECTED)
        self.assertEqual(share.review_feedback, '需要修改')
        self.assertEqual(share.visibility, Share.Visibility.PUBLIC)
        self.assertEqual(
            share.restriction_state,
            Share.RestrictionState.REVIEW_REJECTED,
        )
        self.assertEqual(share.restriction_reason, '需要修改')
        self.assertIsNotNone(share.restricted_at)
        self.assertEqual(share.restricted_by, self.admin)
        self.assertEqual(ShareLog.objects.filter(
            share=share,
            action=ShareLog.ActionType.REVIEW_REJECT,
        ).count(), 1)
        self.assertEqual(SiteMessage.objects.filter(
            related_share=share,
            message_type=SiteMessage.MessageType.SHARE_REJECTED,
        ).count(), 1)

    def test_reject_rolls_back_when_notification_fails(self):
        share = self.create_share(status=Share.Status.PENDING)
        self.client.force_login(self.admin)

        with patch(
            'shares.services.moderation.send_site_message',
            side_effect=RuntimeError('message failed'),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse('admin_reject_share', args=[share.share_id]),
                    {'reason': '需要修改'},
                )

        share.refresh_from_db()
        self.assertEqual(share.status, Share.Status.PENDING)
        self.assertEqual(share.visibility, Share.Visibility.PUBLIC)
        self.assertEqual(share.restriction_state, Share.RestrictionState.CLEAR)
        self.assertFalse(ShareLog.objects.filter(share=share).exists())
        self.assertFalse(SiteMessage.objects.filter(related_share=share).exists())

    def test_report_resolution_rolls_back_when_notification_fails(self):
        share = self.create_share()
        report = self.create_report(share)
        self.client.force_login(self.admin)

        with patch(
            'shares.services.moderation.send_site_message',
            side_effect=RuntimeError('message failed'),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse('admin_resolve_report', args=[report.id, 'resolve']),
                    {'reason': '确认违规'},
                )

        share.refresh_from_db()
        report.refresh_from_db()
        self.assertEqual(share.visibility, Share.Visibility.PUBLIC)
        self.assertEqual(share.restriction_state, Share.RestrictionState.CLEAR)
        self.assertEqual(report.status, Report.Status.PENDING)
        self.assertIsNone(report.resolved_at)
        self.assertFalse(ShareLog.objects.filter(share=share).exists())
        self.assertFalse(SiteMessage.objects.filter(related_report=report).exists())

    def test_batch_resolution_rolls_back_when_notification_fails(self):
        share = self.create_share()
        first_report = self.create_report(share)
        second_report = self.create_report(share, reporter=self.second_reporter)
        self.client.force_login(self.admin)

        with patch(
            'shares.services.moderation.send_site_message',
            side_effect=RuntimeError('message failed'),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse('admin_resolve_share_reports', args=[share.share_id, 'resolve']),
                    {'reason': '确认违规'},
                )

        share.refresh_from_db()
        self.assertEqual(share.visibility, Share.Visibility.PUBLIC)
        self.assertEqual(share.restriction_state, Share.RestrictionState.CLEAR)
        for report in (first_report, second_report):
            report.refresh_from_db()
            self.assertEqual(report.status, Report.Status.PENDING)
            self.assertIsNone(report.resolved_at)
        self.assertFalse(ShareLog.objects.filter(share=share).exists())
        self.assertFalse(SiteMessage.objects.filter(related_share=share).exists())

    def test_reject_does_not_downgrade_existing_report_takedown_restriction(self):
        restricted_at = timezone.now()
        share = self.create_share(
            status=Share.Status.PENDING,
            **self.restriction_fields(
                state=Share.RestrictionState.REPORT_TAKEDOWN,
                reason='原举报下架原因',
                restricted_at=restricted_at,
            ),
        )
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse('admin_reject_share', args=[share.share_id]),
            {'reason': '复审仍未通过'},
        )

        self.assertRedirects(response, reverse('admin_review_list'))
        share.refresh_from_db()
        self.assertEqual(share.status, Share.Status.REJECTED)
        self.assertEqual(share.review_feedback, '复审仍未通过')
        self.assertEqual(
            share.restriction_state,
            Share.RestrictionState.REPORT_TAKEDOWN,
        )
        self.assertEqual(share.restriction_reason, '原举报下架原因')
        self.assertEqual(share.restricted_at, restricted_at)
        self.assertEqual(share.restricted_by, self.admin)

    def test_dismissing_report_preserves_existing_restriction(self):
        restricted_at = timezone.now()
        share = self.create_share(
            **self.restriction_fields(
                state=Share.RestrictionState.REVIEW_REJECTED,
                reason='已有审核限制',
                restricted_at=restricted_at,
            ),
        )
        report = self.create_report(share)
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse('admin_resolve_report', args=[report.pk, 'dismiss']),
            {'reason': '本次举报证据不足'},
        )

        self.assertRedirects(response, reverse('admin_report_list'))
        share.refresh_from_db()
        report.refresh_from_db()
        self.assertEqual(report.status, Report.Status.DISMISSED)
        self.assertEqual(
            share.restriction_state,
            Share.RestrictionState.REVIEW_REJECTED,
        )
        self.assertEqual(share.restriction_reason, '已有审核限制')
        self.assertEqual(share.restricted_at, restricted_at)
        self.assertEqual(share.restricted_by, self.admin)

    def test_deleted_reporter_does_not_delete_or_block_report_resolution(self):
        share = self.create_share()
        reporter = User.objects.create_user(username='deleted-report-reporter')
        report = Report.objects.create(
            share=share,
            reporter=reporter,
            reason='需要管理员核查',
        )
        reporter.delete()
        report.refresh_from_db()
        self.assertIsNone(report.reporter)
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse('admin_resolve_report', args=[report.pk, 'resolve']),
            {'reason': '核查后确认违规'},
        )

        self.assertRedirects(response, reverse('admin_report_list'))
        report.refresh_from_db()
        share.refresh_from_db()
        self.assertEqual(report.status, Report.Status.RESOLVED)
        self.assertEqual(
            share.restriction_state,
            Share.RestrictionState.REPORT_TAKEDOWN,
        )

    def test_approved_restriction_can_be_released_once_and_notifies_author(self):
        share = self.create_share(
            **self.restriction_fields(
                state=Share.RestrictionState.REPORT_TAKEDOWN,
                reason='旧限制原因',
            ),
        )
        self.client.force_login(self.admin)
        url = reverse('admin_release_share_restriction', args=[share.share_id])

        response = self.client.post(url, {'reason': '管理员复核确认可恢复'})

        self.assertRedirects(response, reverse('admin_review_list'))
        share.refresh_from_db()
        self.assertEqual(share.status, Share.Status.APPROVED)
        self.assertEqual(share.restriction_state, Share.RestrictionState.CLEAR)
        self.assertEqual(share.restriction_reason, '')
        self.assertIsNone(share.restricted_at)
        self.assertIsNone(share.restricted_by)
        self.assertEqual(ShareLog.objects.filter(
            share=share,
            action=ShareLog.ActionType.RESTRICTION_RELEASE,
        ).count(), 1)
        self.assertEqual(SiteMessage.objects.filter(
            recipient=self.author,
            related_share=share,
            message_type=SiteMessage.MessageType.SHARE_RESTORED,
        ).count(), 1)

        self.client.post(url, {'reason': '重复提交不应再次解除'})

        self.assertEqual(ShareLog.objects.filter(
            share=share,
            action=ShareLog.ActionType.RESTRICTION_RELEASE,
        ).count(), 1)
        self.assertEqual(SiteMessage.objects.filter(
            recipient=self.author,
            related_share=share,
            message_type=SiteMessage.MessageType.SHARE_RESTORED,
        ).count(), 1)

    def test_report_takedown_can_be_explicitly_confirmed_with_audit(self):
        original_time = timezone.now()
        share = self.create_share(
            **self.restriction_fields(
                state=Share.RestrictionState.REPORT_TAKEDOWN,
                reason='旧版下架原因',
                restricted_at=original_time,
            ),
        )
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse('admin_confirm_share_restriction', args=[share.share_id]),
            {
                'reason': '人工复核后确认继续下架',
                'version': share.updated_at.isoformat(),
            },
        )

        self.assertRedirects(response, reverse('admin_review_list'))
        share.refresh_from_db()
        self.assertEqual(
            share.restriction_state,
            Share.RestrictionState.REPORT_TAKEDOWN,
        )
        self.assertEqual(share.restriction_reason, '人工复核后确认继续下架')
        self.assertGreater(share.restricted_at, original_time)
        self.assertEqual(share.restricted_by, self.admin)
        log = ShareLog.objects.get(
            share=share,
            action=ShareLog.ActionType.RESTRICTION_CONFIRM,
        )
        self.assertIn('旧版下架原因', log.details)
        self.assertTrue(SiteMessage.objects.filter(
            recipient=self.author,
            related_share=share,
            message_type=SiteMessage.MessageType.SHARE_TAKEDOWN,
        ).exists())

    def test_restriction_confirmation_replay_does_not_duplicate_audit_or_message(self):
        share = self.create_share(
            **self.restriction_fields(
                state=Share.RestrictionState.REPORT_TAKEDOWN,
                reason='等待首次复核',
            ),
        )
        self.client.force_login(self.admin)
        url = reverse('admin_confirm_share_restriction', args=[share.share_id])
        payload = {
            'reason': '确认继续限制',
            'version': share.updated_at.isoformat(),
        }

        first = self.client.post(url, payload)
        replay = self.client.post(url, payload)

        self.assertRedirects(first, reverse('admin_review_list'))
        self.assertRedirects(replay, reverse('admin_review_list'))
        self.assertEqual(ShareLog.objects.filter(
            share=share,
            action=ShareLog.ActionType.RESTRICTION_CONFIRM,
        ).count(), 1)
        self.assertEqual(SiteMessage.objects.filter(
            recipient=self.author,
            related_share=share,
            message_type=SiteMessage.MessageType.SHARE_TAKEDOWN,
        ).count(), 1)

    def test_legacy_private_can_be_classified_without_approving_pending_content(self):
        share = self.create_share(
            status=Share.Status.PENDING,
            visibility=Share.Visibility.PRIVATE,
            **self.restriction_fields(
                state=Share.RestrictionState.LEGACY_PRIVATE,
                reason='历史私密状态来源待人工确认',
                restricted_by=None,
            ),
        )
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse('admin_release_share_restriction', args=[share.share_id]),
            {'reason': '确认这是作者主动设置的私密状态'},
        )

        self.assertRedirects(response, reverse('admin_review_list'))
        share.refresh_from_db()
        self.assertEqual(share.status, Share.Status.PENDING)
        self.assertEqual(share.visibility, Share.Visibility.PRIVATE)
        self.assertEqual(share.restriction_state, Share.RestrictionState.CLEAR)
        self.assertTrue(ShareLog.objects.filter(
            share=share,
            action=ShareLog.ActionType.RESTRICTION_RELEASE,
        ).exists())

    def test_release_requires_reason_and_preserves_restriction_on_invalid_post(self):
        share = self.create_share(
            **self.restriction_fields(
                state=Share.RestrictionState.REVIEW_REJECTED,
                reason='等待复核',
            ),
        )
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse('admin_release_share_restriction', args=[share.share_id]),
            {'reason': ''},
        )

        self.assertEqual(response.status_code, 400)
        self.assertContains(
            response,
            'data-moderation-invalid-modal',
            count=1,
            status_code=400,
        )
        share.refresh_from_db()
        self.assertEqual(
            share.restriction_state,
            Share.RestrictionState.REVIEW_REJECTED,
        )
        self.assertFalse(ShareLog.objects.filter(
            share=share,
            action=ShareLog.ActionType.RESTRICTION_RELEASE,
        ).exists())
        self.assertFalse(SiteMessage.objects.filter(
            related_share=share,
            message_type=SiteMessage.MessageType.SHARE_RESTORED,
        ).exists())

    def test_pending_and_rejected_restrictions_cannot_be_released_directly(self):
        self.client.force_login(self.admin)
        for status in (Share.Status.PENDING, Share.Status.REJECTED):
            with self.subTest(status=status):
                share = self.create_share(
                    status=status,
                    title=f'{status} 受限分享',
                    strategy_code=f'[stgy:{status}-restricted]',
                    **self.restriction_fields(
                        state=Share.RestrictionState.REVIEW_REJECTED,
                        reason='必须先经过审核',
                    ),
                )

                response = self.client.post(
                    reverse('admin_release_share_restriction', args=[share.share_id]),
                    {'reason': '尝试绕过审核'},
                )

                self.assertRedirects(response, reverse('admin_review_list'))
                share.refresh_from_db()
                self.assertEqual(share.status, status)
                self.assertEqual(
                    share.restriction_state,
                    Share.RestrictionState.REVIEW_REJECTED,
                )
                self.assertFalse(ShareLog.objects.filter(
                    share=share,
                    action=ShareLog.ActionType.RESTRICTION_RELEASE,
                ).exists())
                self.assertFalse(SiteMessage.objects.filter(
                    related_share=share,
                    message_type=SiteMessage.MessageType.SHARE_RESTORED,
                ).exists())

    def test_release_rolls_back_when_notification_fails(self):
        restricted_at = timezone.now()
        share = self.create_share(
            **self.restriction_fields(
                state=Share.RestrictionState.REPORT_TAKEDOWN,
                reason='回滚前限制',
                restricted_at=restricted_at,
            ),
        )
        self.client.force_login(self.admin)

        with patch(
            'shares.services.moderation.send_site_message',
            side_effect=RuntimeError('message failed'),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse('admin_release_share_restriction', args=[share.share_id]),
                    {'reason': '本次解除应回滚'},
                )

        share.refresh_from_db()
        self.assertEqual(
            share.restriction_state,
            Share.RestrictionState.REPORT_TAKEDOWN,
        )
        self.assertEqual(share.restriction_reason, '回滚前限制')
        self.assertEqual(share.restricted_at, restricted_at)
        self.assertEqual(share.restricted_by, self.admin)
        self.assertFalse(ShareLog.objects.filter(
            share=share,
            action=ShareLog.ActionType.RESTRICTION_RELEASE,
        ).exists())
        self.assertFalse(SiteMessage.objects.filter(
            related_share=share,
            message_type=SiteMessage.MessageType.SHARE_RESTORED,
        ).exists())

    def test_review_queue_exposes_audited_release_for_approved_restriction(self):
        share = self.create_share(
            **self.restriction_fields(
                state=Share.RestrictionState.REPORT_TAKEDOWN,
                reason='等待管理员人工复核',
            ),
        )
        deleted_moderator = User.objects.create_user(
            username='deleted-moderator',
            is_staff=True,
        )
        log = ShareLog.objects.create(
            share=share,
            user=deleted_moderator,
            action=ShareLog.ActionType.REPORT_HANDLE,
            details='历史下架记录',
        )
        deleted_moderator.delete()
        log.refresh_from_db()
        self.assertIsNone(log.user)
        self.client.force_login(self.admin)

        response = self.client.get(reverse('admin_review_list'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, share.title)
        self.assertContains(response, '等待管理员人工复核')
        self.assertContains(response, '已删除账户')
        self.assertContains(
            response,
            reverse('admin_confirm_share_restriction', args=[share.share_id]),
        )
        self.assertContains(
            response,
            reverse('admin_release_share_restriction', args=[share.share_id]),
        )
        self.assertContains(response, '举报下架限制')
        self.assertNotContains(response, '<i class="bi bi-hourglass-split"></i> 待审核')
        self.assertContains(response, '解除限制')
        self.assertEqual(response.context['pending_reviews_count'], 1)

    def test_non_staff_user_cannot_open_moderation_queue(self):
        self.client.force_login(self.regular_user)

        response = self.client.get(reverse('admin_review_list'))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse('login')))


class SiteMessageAccessContractTests(TestCase):
    def setUp(self):
        self.first_user = User.objects.create_user(username='first', password='password123')
        self.second_user = User.objects.create_user(username='second', password='password123')

    def create_message(self, recipient, title):
        return SiteMessage.objects.create(
            recipient=recipient,
            message_type=SiteMessage.MessageType.REPORT_RESOLVED,
            title=title,
            content='消息正文',
        )

    def test_user_cannot_open_another_users_site_message(self):
        message = self.create_message(self.first_user, '仅第一位用户可见')
        self.client.force_login(self.second_user)

        response = self.client.get(reverse('site_message_detail', args=[message.id]))

        self.assertEqual(response.status_code, 404)

    def test_mark_all_read_only_updates_current_users_messages(self):
        first_message = self.create_message(self.first_user, '第一位用户的消息')
        second_message = self.create_message(self.second_user, '第二位用户的消息')
        self.client.force_login(self.first_user)

        response = self.client.post(reverse('mark_all_site_messages_read'))

        self.assertRedirects(response, reverse('site_message_list'))
        first_message.refresh_from_db()
        second_message.refresh_from_db()
        self.assertIsNotNone(first_message.read_at)
        self.assertIsNone(second_message.read_at)


class AnnouncementVisibilityContractTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='admin', password='password123', is_staff=True)
        self.active = Announcement.objects.create(title='公开动态', content='公开内容', is_active=True)
        self.inactive = Announcement.objects.create(title='隐藏动态', content='隐藏内容', is_active=False)

    def test_anonymous_list_only_contains_active_announcements(self):
        response = self.client.get(reverse('announcement_list'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.active.title)
        self.assertNotContains(response, self.inactive.title)

    def test_staff_list_contains_active_and_inactive_announcements(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse('announcement_list'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.active.title)
        self.assertContains(response, self.inactive.title)

    def test_setting_visibility_is_idempotent(self):
        self.client.force_login(self.admin)
        url = reverse('toggle_announcement_visibility', args=[self.active.id])

        self.client.post(url, {'is_active': '0'})
        self.client.post(url, {'is_active': '0'})

        self.active.refresh_from_db()
        self.assertFalse(self.active.is_active)
