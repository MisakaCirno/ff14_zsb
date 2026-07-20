import re
from unittest import mock
from urllib.parse import urlencode

from django.contrib import admin
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.admin.models import CHANGE, LogEntry, LogEntryManager
from django.contrib.auth.models import Permission, User
from django.contrib.messages import get_messages
from django.db import connection
from django.db.models import Count
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .admin import ADMIN_ACTION_BATCH_SIZE
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

    def run_select_across_action(
        self,
        user,
        action,
        *,
        filters,
        selected_share,
    ):
        self.client.force_login(user)
        action_url = f'{self.changelist_url}?{urlencode(filters)}'
        response = self.client.post(
            action_url,
            {
                'action': action,
                ACTION_CHECKBOX_NAME: [selected_share.pk],
                'select_across': '1',
                'index': '0',
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        return [str(message) for message in get_messages(response.wsgi_request)]

    def create_share_batch(
        self,
        count,
        *,
        prefix,
        visibility,
        status=Share.Status.APPROVED,
    ):
        prefix = prefix[:8]
        return Share.objects.bulk_create([
            Share(
                share_id=f'{prefix}{index:013d}',
                title=f'{prefix}-{index:03d}',
                strategy_code=f'[stgy:{prefix}-{index:03d}]',
                author=self.author,
                visibility=visibility,
                status=status,
                restriction_state=Share.RestrictionState.CLEAR,
            )
            for index in range(count)
        ])

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

    def test_select_across_filtered_action_batches_without_skipping_or_duplicate_logs(self):
        self.assertEqual(ADMIN_ACTION_BATCH_SIZE, 100)
        selected = self.create_share_batch(
            205,
            prefix='selected',
            visibility=Share.Visibility.UNLISTED,
        )
        excluded = self.create_share_batch(
            5,
            prefix='excluded',
            visibility=Share.Visibility.UNLISTED,
            status=Share.Status.PENDING,
        )
        before_updated_at = {share.pk: share.updated_at for share in selected}
        before_moderation = {
            share.pk: self.moderation_snapshot(share)
            for share in selected
        }

        with CaptureQueriesContext(connection) as captured:
            messages = self.run_select_across_action(
                self.superuser,
                'make_public',
                filters={
                    'visibility__exact': Share.Visibility.UNLISTED,
                    'status__exact': Share.Status.APPROVED,
                },
                selected_share=selected[0],
            )

        self.assertEqual(len(messages), 1)
        self.assertIn('已更新 205 个', messages[0])
        self.assertIn('已是公开 0 个', messages[0])
        self.assertIn('尚未审核通过而跳过 0 个', messages[0])
        refreshed = Share.objects.in_bulk(share.pk for share in selected)
        for share in selected:
            current = refreshed[share.pk]
            self.assertEqual(current.visibility, Share.Visibility.PUBLIC)
            self.assertGreater(current.updated_at, before_updated_at[share.pk])
            self.assertEqual(
                self.moderation_snapshot(current),
                before_moderation[share.pk],
            )
        self.assertTrue(all(
            share.visibility == Share.Visibility.UNLISTED
            for share in Share.objects.filter(pk__in=[item.pk for item in excluded])
        ))

        share_logs = ShareLog.objects.filter(action=ShareLog.ActionType.EDIT)
        self.assertEqual(share_logs.count(), 205)
        self.assertEqual(
            set(share_logs.values_list('share_id', flat=True)),
            {share.pk for share in selected},
        )
        admin_logs = LogEntry.objects.filter(
            action_flag=CHANGE,
            user=self.superuser,
        )
        self.assertEqual(admin_logs.count(), 205)
        self.assertEqual(
            set(admin_logs.values_list('object_id', flat=True)),
            {str(share.pk) for share in selected},
        )

        numeric_in_sizes = []
        in_pattern = re.compile(
            r'"shares_share"\."id" IN \(([0-9, ]+)\)',
        )
        for query in captured.captured_queries:
            for match in in_pattern.finditer(query['sql']):
                numeric_in_sizes.append(len(match.group(1).split(',')))
        self.assertTrue(numeric_in_sizes)
        self.assertIn(ADMIN_ACTION_BATCH_SIZE, numeric_in_sizes)
        self.assertLessEqual(max(numeric_in_sizes), ADMIN_ACTION_BATCH_SIZE)

        retry_messages = self.run_select_across_action(
            self.superuser,
            'make_public',
            filters={'status__exact': Share.Status.APPROVED},
            selected_share=selected[0],
        )

        self.assertIn('已更新 0 个', retry_messages[0])
        self.assertIn('已是公开 205 个', retry_messages[0])
        self.assertEqual(share_logs.count(), 205)
        self.assertEqual(admin_logs.count(), 205)

    def test_second_batch_failure_rolls_back_that_batch_and_retry_is_idempotent(self):
        selected = self.create_share_batch(
            205,
            prefix='rollback',
            visibility=Share.Visibility.PUBLIC,
        )
        selected_ids = [share.pk for share in selected]
        before_updated_at = {share.pk: share.updated_at for share in selected}
        before_moderation = {
            share.pk: self.moderation_snapshot(share)
            for share in selected
        }
        original_log_actions = LogEntryManager.log_actions
        log_action_calls = 0

        def fail_second_log_batch(manager, *args, **kwargs):
            nonlocal log_action_calls
            log_action_calls += 1
            if log_action_calls == 2:
                raise RuntimeError('injected second-batch failure')
            return original_log_actions(manager, *args, **kwargs)

        with (
            mock.patch.object(
                LogEntryManager,
                'log_actions',
                new=fail_second_log_batch,
            ),
            self.assertLogs('shares.admin', level='ERROR'),
        ):
            failed_messages = self.run_select_across_action(
                self.staff,
                'make_private',
                filters={'status__exact': Share.Status.APPROVED},
                selected_share=selected[0],
            )

        self.assertEqual(log_action_calls, 2)
        self.assertIn('已更新 100 个', failed_messages[0])
        self.assertIn('第 2 批处理失败且已回滚', failed_messages[0])
        self.assertIn('可安全重试', failed_messages[0])
        visibility_by_id = dict(
            Share.objects.filter(pk__in=selected_ids)
            .order_by('pk')
            .values_list('pk', 'visibility')
        )
        self.assertTrue(all(
            visibility_by_id[share.pk] == Share.Visibility.PRIVATE
            for share in selected[:100]
        ))
        self.assertTrue(all(
            visibility_by_id[share.pk] == Share.Visibility.PUBLIC
            for share in selected[100:]
        ))
        self.assertEqual(ShareLog.objects.filter(
            share_id__in=selected_ids,
            action=ShareLog.ActionType.EDIT,
        ).count(), 100)
        self.assertEqual(LogEntry.objects.filter(
            action_flag=CHANGE,
            user=self.staff,
            object_id__in=[str(pk) for pk in selected_ids],
        ).count(), 100)

        retry_messages = self.run_select_across_action(
            self.staff,
            'make_private',
            filters={'status__exact': Share.Status.APPROVED},
            selected_share=selected[0],
        )

        self.assertIn('已更新 105 个', retry_messages[0])
        self.assertIn('已是私有 100 个', retry_messages[0])
        refreshed = Share.objects.in_bulk(selected_ids)
        for share in selected:
            current = refreshed[share.pk]
            self.assertEqual(current.visibility, Share.Visibility.PRIVATE)
            self.assertGreater(current.updated_at, before_updated_at[share.pk])
            self.assertEqual(
                self.moderation_snapshot(current),
                before_moderation[share.pk],
            )
        share_log_counts = dict(
            ShareLog.objects.filter(
                share_id__in=selected_ids,
                action=ShareLog.ActionType.EDIT,
            ).values('share_id').annotate(total=Count('pk'))
            .values_list('share_id', 'total')
        )
        self.assertEqual(share_log_counts, {pk: 1 for pk in selected_ids})
        admin_log_counts = dict(
            LogEntry.objects.filter(
                action_flag=CHANGE,
                user=self.staff,
                object_id__in=[str(pk) for pk in selected_ids],
            ).values('object_id').annotate(total=Count('pk'))
            .values_list('object_id', 'total')
        )
        self.assertEqual(
            admin_log_counts,
            {str(pk): 1 for pk in selected_ids},
        )

    def test_visibility_action_does_not_process_primary_keys_created_after_start(self):
        selected = self.create_share_batch(
            150,
            prefix='highwater',
            visibility=Share.Visibility.UNLISTED,
        )
        original_apply_batch = self.model_admin._apply_visibility_batch
        created_after_start = []

        def apply_and_insert(*args, **kwargs):
            result = original_apply_batch(*args, **kwargs)
            if not created_after_start:
                created_after_start.append(self.create_share('created-after-start'))
            return result

        with mock.patch.object(
            self.model_admin,
            '_apply_visibility_batch',
            side_effect=apply_and_insert,
        ):
            messages = self.run_select_across_action(
                self.superuser,
                'make_public',
                filters={'visibility__exact': Share.Visibility.UNLISTED},
                selected_share=selected[0],
            )

        self.assertIn('已更新 150 个', messages[0])
        self.assertEqual(
            Share.objects.filter(
                pk__in=[share.pk for share in selected],
                visibility=Share.Visibility.PUBLIC,
            ).count(),
            150,
        )
        late_share = created_after_start[0]
        late_share.refresh_from_db()
        self.assertEqual(late_share.visibility, Share.Visibility.UNLISTED)
        self.assertFalse(ShareLog.objects.filter(share=late_share).exists())

    def test_actions_require_share_change_permission(self):
        request_factory = RequestFactory()

        viewer_request = request_factory.get(self.changelist_url)
        viewer_request.user = self.viewer
        viewer_actions = self.model_admin.get_actions(viewer_request)
        self.assertNotIn('make_public', viewer_actions)
        self.assertNotIn('make_private', viewer_actions)
        self.assertNotIn('restore_deleted_shares', viewer_actions)

        staff_request = request_factory.get(self.changelist_url)
        staff_request.user = self.staff
        staff_actions = self.model_admin.get_actions(staff_request)
        self.assertIn('make_public', staff_actions)
        self.assertIn('make_private', staff_actions)
        self.assertIn('restore_deleted_shares', staff_actions)

        superuser_request = request_factory.get(self.changelist_url)
        superuser_request.user = self.superuser
        superuser_actions = self.model_admin.get_actions(superuser_request)
        self.assertIn('make_public', superuser_actions)
        self.assertIn('make_private', superuser_actions)
        self.assertIn('restore_deleted_shares', superuser_actions)

        share = self.create_share('forged-action', visibility=Share.Visibility.PUBLIC)
        messages = self.run_action(self.viewer, 'make_private', [share])
        share.refresh_from_db()
        self.assertEqual(share.visibility, Share.Visibility.PUBLIC)
        self.assertFalse(ShareLog.objects.filter(share=share).exists())
        self.assertFalse(LogEntry.objects.filter(user=self.viewer).exists())
        self.assertEqual(messages, [])
