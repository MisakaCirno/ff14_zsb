from django.contrib import admin
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.urls import reverse

from .models import Report, Share, ShareLog, UserProfile


class DjangoAdminModerationSafetyTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(
            username='admin-safety-root',
            email='root@example.com',
            password='password123',
        )
        self.author = User.objects.create_user(username='admin-safety-author')
        self.share = Share.objects.create(
            title='后台审核安全测试',
            strategy_code='[stgy:admin-moderation-safety]',
            author=self.author,
            status=Share.Status.APPROVED,
        )
        self.client.force_login(self.superuser)

    def test_share_change_form_cannot_bypass_moderation_service(self):
        response = self.client.post(
            reverse('admin:shares_share_change', args=[self.share.pk]),
            {
                'title': self.share.title,
                'author': self.author.pk,
                'visibility': self.share.visibility,
                'strategy_code': self.share.strategy_code,
                'description': self.share.description,
                'status': Share.Status.REJECTED,
                'review_feedback': '试图绕过审核服务',
                'reviewed_by': self.superuser.pk,
                '_save': '保存',
            },
        )

        self.assertEqual(response.status_code, 302)
        self.share.refresh_from_db()
        self.assertEqual(self.share.status, Share.Status.APPROVED)
        self.assertEqual(self.share.review_feedback, '')
        self.assertIsNone(self.share.reviewed_at)
        self.assertIsNone(self.share.reviewed_by)
        self.assertFalse(ShareLog.objects.filter(
            share=self.share,
            action__in=[
                ShareLog.ActionType.REVIEW_APPROVE,
                ShareLog.ActionType.REVIEW_REJECT,
            ],
        ).exists())

    def test_share_author_display_falls_back_when_profile_is_missing(self):
        UserProfile.objects.filter(user=self.author).delete()
        share_admin = admin.site._registry[Share]

        self.assertEqual(
            share_admin.get_author_display(self.share),
            self.author.username,
        )

    def test_report_admin_is_read_only_for_moderation_evidence(self):
        report_admin = admin.site._registry[Report]
        request = RequestFactory().get('/admin/shares/report/')
        request.user = self.superuser

        self.assertFalse(report_admin.has_add_permission(request))
        self.assertFalse(report_admin.has_delete_permission(request))
        self.assertTrue({
            'share',
            'reporter',
            'reason',
            'status',
            'resolved_at',
            'resolved_by',
            'resolution_reason',
        }.issubset(set(report_admin.readonly_fields)))
