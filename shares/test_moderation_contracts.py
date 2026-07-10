from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Announcement, Report, Share, ShareLog, SiteMessage


class ModerationWorkflowContractTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(username='author', password='password123')
        self.reporter = User.objects.create_user(username='reporter', password='password123')
        self.second_reporter = User.objects.create_user(username='reporter2', password='password123')
        self.regular_user = User.objects.create_user(username='regular', password='password123')
        self.admin = User.objects.create_user(username='admin', password='password123', is_staff=True)

    def create_share(self, *, status=Share.Status.APPROVED):
        return Share.objects.create(
            title='审核测试',
            strategy_code='[stgy:moderation]',
            author=self.author,
            visibility=Share.Visibility.PUBLIC,
            status=status,
        )

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
        self.assertEqual(share.visibility, Share.Visibility.PRIVATE)
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
