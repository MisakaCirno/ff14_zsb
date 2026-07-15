from django.contrib import admin
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth.models import Permission, User
from django.contrib.messages import get_messages
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Share, ShareLog


class ShareAdminActionTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(
            username='share-action-root',
            email='root@example.com',
            password='password123',
        )
        self.staff = User.objects.create_user(
            username='share-action-staff',
            password='password123',
            is_staff=True,
        )
        self.viewer = User.objects.create_user(
            username='share-action-viewer',
            password='password123',
            is_staff=True,
        )
        self.author = User.objects.create_user(username='share-action-author')
        self.staff.user_permissions.add(
            Permission.objects.get(codename='change_share'),
        )
        self.viewer.user_permissions.add(
            Permission.objects.get(codename='view_share'),
        )
        self.model_admin = admin.site._registry[Share]
        self.changelist_url = reverse('admin:shares_share_changelist')

    def create_share(
        self,
        title,
        *,
        visibility=Share.Visibility.UNLISTED,
        status=Share.Status.APPROVED,
        restriction_state=Share.RestrictionState.CLEAR,
    ):
        restriction = {}
        if restriction_state != Share.RestrictionState.CLEAR:
            restriction = {
                'restriction_reason': f'{title} 的持久限制',
                'restricted_at': timezone.now(),
                'restricted_by': self.superuser,
            }
        return Share.objects.create(
            title=title,
            strategy_code=f'[stgy:{title}]',
            author=self.author,
            visibility=visibility,
            status=status,
            restriction_state=restriction_state,
            **restriction,
        )

    def run_action(self, user, action, shares):
        self.client.force_login(user)
        response = self.client.post(
            self.changelist_url,
            {
                'action': action,
                ACTION_CHECKBOX_NAME: [share.pk for share in shares],
                'select_across': '0',
                'index': '0',
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        return [str(message) for message in get_messages(response.wsgi_request)]

    @staticmethod
    def moderation_snapshot(share):
        return (
            share.status,
            share.review_feedback,
            share.reviewed_at,
            share.reviewed_by_id,
            share.restriction_state,
            share.restriction_reason,
            share.restricted_at,
            share.restricted_by_id,
        )

    def test_superuser_public_action_only_publishes_approved_clear_shares(self):
        eligible = self.create_share('eligible')
        already_public = self.create_share(
            'already-public',
            visibility=Share.Visibility.PUBLIC,
        )
        pending = self.create_share('pending', status=Share.Status.PENDING)
        restricted = self.create_share(
            'restricted',
            visibility=Share.Visibility.PRIVATE,
            restriction_state=Share.RestrictionState.REPORT_TAKEDOWN,
        )
        rejected = self.create_share(
            'rejected',
            status=Share.Status.REJECTED,
            restriction_state=Share.RestrictionState.REVIEW_REJECTED,
        )
        shares = [eligible, already_public, pending, restricted, rejected]
        before = {share.pk: self.moderation_snapshot(share) for share in shares}

        messages = self.run_action(self.superuser, 'make_public', shares)

        for share in shares:
            share.refresh_from_db()
            self.assertEqual(self.moderation_snapshot(share), before[share.pk])
        self.assertEqual(eligible.visibility, Share.Visibility.PUBLIC)
        self.assertEqual(already_public.visibility, Share.Visibility.PUBLIC)
        self.assertEqual(pending.visibility, Share.Visibility.UNLISTED)
        self.assertEqual(restricted.visibility, Share.Visibility.PRIVATE)
        self.assertEqual(rejected.visibility, Share.Visibility.UNLISTED)
        self.assertEqual(len(messages), 1)
        self.assertIn('已更新 1 个', messages[0])
        self.assertIn('已是公开 1 个', messages[0])
        self.assertIn('仍有内容限制而跳过 2 个', messages[0])
        self.assertIn('尚未审核通过而跳过 1 个', messages[0])
        self.assertIn('审核中心', messages[0])

        share_logs = ShareLog.objects.filter(action=ShareLog.ActionType.EDIT)
        self.assertEqual(list(share_logs.values_list('share_id', flat=True)), [eligible.pk])
        self.assertEqual(share_logs.get().user, self.superuser)
        self.assertIn('可见性改为“公开”', share_logs.get().details)
        admin_logs = LogEntry.objects.filter(
            action_flag=CHANGE,
            user=self.superuser,
        )
        self.assertEqual(list(admin_logs.values_list('object_id', flat=True)), [str(eligible.pk)])

        messages = self.run_action(self.superuser, 'make_public', shares)

        self.assertIn('已更新 0 个', messages[0])
        self.assertIn('已是公开 2 个', messages[0])
        self.assertEqual(share_logs.count(), 1)
        self.assertEqual(admin_logs.count(), 1)

    def test_change_staff_private_action_is_safe_and_idempotent(self):
        public = self.create_share('public', visibility=Share.Visibility.PUBLIC)
        pending = self.create_share('pending', status=Share.Status.PENDING)
        restricted = self.create_share(
            'restricted',
            visibility=Share.Visibility.PUBLIC,
            restriction_state=Share.RestrictionState.REPORT_TAKEDOWN,
        )
        already_private = self.create_share(
            'already-private',
            visibility=Share.Visibility.PRIVATE,
            status=Share.Status.REJECTED,
            restriction_state=Share.RestrictionState.REVIEW_REJECTED,
        )
        shares = [public, pending, restricted, already_private]
        before = {share.pk: self.moderation_snapshot(share) for share in shares}

        messages = self.run_action(self.staff, 'make_private', shares)

        for share in shares:
            share.refresh_from_db()
            self.assertEqual(share.visibility, Share.Visibility.PRIVATE)
            self.assertEqual(self.moderation_snapshot(share), before[share.pk])
        self.assertEqual(len(messages), 1)
        self.assertIn('已更新 3 个', messages[0])
        self.assertIn('已是私有 1 个', messages[0])

        share_logs = ShareLog.objects.filter(action=ShareLog.ActionType.EDIT)
        self.assertEqual(share_logs.count(), 3)
        self.assertEqual(set(share_logs.values_list('user_id', flat=True)), {self.staff.pk})
        self.assertTrue(all(
            '可见性改为“私有”' in details
            for details in share_logs.values_list('details', flat=True)
        ))
        admin_logs = LogEntry.objects.filter(action_flag=CHANGE, user=self.staff)
        self.assertEqual(admin_logs.count(), 3)

        messages = self.run_action(self.staff, 'make_private', shares)

        self.assertIn('已更新 0 个', messages[0])
        self.assertIn('已是私有 4 个', messages[0])
        self.assertEqual(share_logs.count(), 3)
        self.assertEqual(admin_logs.count(), 3)

    def test_actions_require_share_change_permission(self):
        request_factory = RequestFactory()

        viewer_request = request_factory.get(self.changelist_url)
        viewer_request.user = self.viewer
        viewer_actions = self.model_admin.get_actions(viewer_request)
        self.assertNotIn('make_public', viewer_actions)
        self.assertNotIn('make_private', viewer_actions)

        staff_request = request_factory.get(self.changelist_url)
        staff_request.user = self.staff
        staff_actions = self.model_admin.get_actions(staff_request)
        self.assertIn('make_public', staff_actions)
        self.assertIn('make_private', staff_actions)

        superuser_request = request_factory.get(self.changelist_url)
        superuser_request.user = self.superuser
        superuser_actions = self.model_admin.get_actions(superuser_request)
        self.assertIn('make_public', superuser_actions)
        self.assertIn('make_private', superuser_actions)

        share = self.create_share('forged-action', visibility=Share.Visibility.PUBLIC)
        messages = self.run_action(self.viewer, 'make_private', [share])
        share.refresh_from_db()
        self.assertEqual(share.visibility, Share.Visibility.PUBLIC)
        self.assertFalse(ShareLog.objects.filter(share=share).exists())
        self.assertFalse(LogEntry.objects.filter(user=self.viewer).exists())
        self.assertEqual(messages, [])
