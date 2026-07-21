from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Share, ShareLog, SiteMessage
from .services.moderation import takedown_share


class ModeratorTakedownWorkflowTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(
            username='takedown-author',
            password='password123',
        )
        self.viewer = User.objects.create_user(
            username='takedown-viewer',
            password='password123',
        )
        self.moderator = User.objects.create_user(
            username='takedown-moderator',
            password='password123',
            is_staff=True,
        )
        self.share = Share.objects.create(
            title='管理员下架测试',
            strategy_code='[stgy:moderator-takedown]',
            author=self.author,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )
        self.detail_url = reverse('share_detail', args=[self.share.share_id])
        self.takedown_url = reverse(
            'admin_takedown_share',
            args=[self.share.share_id],
        )

    def test_moderator_can_open_takedown_form_from_share_detail(self):
        self.client.force_login(self.moderator)

        detail = self.client.get(self.detail_url)
        form = self.client.get(self.takedown_url)

        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, self.takedown_url)
        self.assertContains(detail, '管理员下架')
        self.assertEqual(form.status_code, 200)
        self.assertContains(form, '确认下架并通知作者')
        self.assertContains(form, '下架会立即停止对外访问')

    def test_non_moderator_cannot_open_or_submit_takedown(self):
        self.client.force_login(self.viewer)

        detail = self.client.get(self.detail_url)
        form = self.client.get(self.takedown_url)
        submit = self.client.post(self.takedown_url, {'reason': '无权操作'})

        self.assertEqual(detail.status_code, 200)
        self.assertNotContains(detail, self.takedown_url)
        self.assertEqual(form.status_code, 302)
        self.assertTrue(form.url.startswith(reverse('login')))
        self.assertEqual(submit.status_code, 302)
        self.share.refresh_from_db()
        self.assertEqual(
            self.share.restriction_state,
            Share.RestrictionState.CLEAR,
        )

    def test_takedown_preserves_review_result_and_notifies_author(self):
        self.client.force_login(self.moderator)

        response = self.client.post(
            self.takedown_url,
            {'reason': '说明中缺少必要的来源标注，请补充后重新提交。'},
        )

        self.assertRedirects(response, self.detail_url)
        self.share.refresh_from_db()
        self.assertEqual(self.share.status, Share.Status.APPROVED)
        self.assertEqual(self.share.visibility, Share.Visibility.PUBLIC)
        self.assertEqual(
            self.share.restriction_state,
            Share.RestrictionState.MODERATOR_TAKEDOWN,
        )
        self.assertEqual(
            self.share.restriction_reason,
            '说明中缺少必要的来源标注，请补充后重新提交。',
        )
        self.assertIsNotNone(self.share.restricted_at)
        self.assertEqual(self.share.restricted_by, self.moderator)
        self.assertTrue(ShareLog.objects.filter(
            share=self.share,
            user=self.moderator,
            action=ShareLog.ActionType.MODERATOR_TAKEDOWN,
            details__contains='缺少必要的来源标注',
        ).exists())
        message = SiteMessage.objects.get(
            recipient=self.author,
            related_share=self.share,
            message_type=SiteMessage.MessageType.SHARE_TAKEDOWN,
        )
        self.assertIsNone(message.related_report)
        self.assertIn('管理员主动下架', message.content)
        self.assertIn('缺少必要的来源标注', message.content)

    def test_takedown_hides_share_from_readers_but_explains_it_to_author(self):
        takedown_share(
            share_id=self.share.share_id,
            moderator=self.moderator,
            reason='请移除不合适的内容后重新提交。',
        )

        anonymous_response = self.client.get(self.detail_url)
        self.client.force_login(self.viewer)
        viewer_response = self.client.get(self.detail_url)
        self.client.force_login(self.author)
        author_response = self.client.get(self.detail_url)

        self.assertRedirects(anonymous_response, reverse('index'))
        self.assertRedirects(viewer_response, reverse('index'))
        self.assertEqual(author_response.status_code, 200)
        self.assertContains(author_response, '分享已由管理员下架')
        self.assertContains(author_response, '请移除不合适的内容后重新提交。')
        self.assertContains(author_response, '管理员下架')

    def test_invalid_reason_is_redisplayed_without_changing_share(self):
        self.client.force_login(self.moderator)

        response = self.client.post(self.takedown_url, {'reason': '短'})

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, '至少包含 2 字符', status_code=400)
        self.share.refresh_from_db()
        self.assertEqual(
            self.share.restriction_state,
            Share.RestrictionState.CLEAR,
        )
        self.assertFalse(ShareLog.objects.filter(share=self.share).exists())
        self.assertFalse(SiteMessage.objects.filter(related_share=self.share).exists())

    def test_pending_share_stays_in_review_workflow(self):
        self.share.status = Share.Status.PENDING
        self.share.save(update_fields=['status'])
        self.client.force_login(self.moderator)

        get_response = self.client.get(self.takedown_url)
        post_response = self.client.post(
            self.takedown_url,
            {'reason': '不应绕过审核流程'},
        )

        self.assertRedirects(get_response, reverse('admin_review_list'))
        self.assertRedirects(post_response, self.detail_url)
        self.share.refresh_from_db()
        self.assertEqual(self.share.status, Share.Status.PENDING)
        self.assertEqual(
            self.share.restriction_state,
            Share.RestrictionState.CLEAR,
        )

    def test_repeated_takedown_does_not_replace_existing_restriction_reason(self):
        first = takedown_share(
            share_id=self.share.share_id,
            moderator=self.moderator,
            reason='第一次下架说明',
        )
        second = takedown_share(
            share_id=self.share.share_id,
            moderator=self.moderator,
            reason='重复请求不应覆盖',
        )

        self.assertTrue(first.changed)
        self.assertEqual(second.outcome, 'already_restricted')
        self.share.refresh_from_db()
        self.assertEqual(self.share.restriction_reason, '第一次下架说明')
        self.assertEqual(ShareLog.objects.filter(
            share=self.share,
            action=ShareLog.ActionType.MODERATOR_TAKEDOWN,
        ).count(), 1)
        self.assertEqual(SiteMessage.objects.filter(
            related_share=self.share,
            message_type=SiteMessage.MessageType.SHARE_TAKEDOWN,
        ).count(), 1)

    def test_notification_failure_rolls_back_restriction_and_audit_log(self):
        with patch(
            'shares.services.moderation.send_site_message',
            side_effect=RuntimeError('message failed'),
        ):
            with self.assertRaises(RuntimeError):
                takedown_share(
                    share_id=self.share.share_id,
                    moderator=self.moderator,
                    reason='事务必须完整回滚',
                )

        self.share.refresh_from_db()
        self.assertEqual(
            self.share.restriction_state,
            Share.RestrictionState.CLEAR,
        )
        self.assertFalse(ShareLog.objects.filter(share=self.share).exists())

    def test_existing_release_workflow_restores_moderator_takedown(self):
        takedown_share(
            share_id=self.share.share_id,
            moderator=self.moderator,
            reason='先下架复核',
        )
        self.client.force_login(self.moderator)

        response = self.client.post(
            reverse(
                'admin_release_share_restriction',
                args=[self.share.share_id],
            ),
            {'reason': '复核完成，可以恢复访问'},
        )

        self.assertRedirects(response, reverse('admin_review_list'))
        self.share.refresh_from_db()
        self.assertEqual(self.share.status, Share.Status.APPROVED)
        self.assertEqual(
            self.share.restriction_state,
            Share.RestrictionState.CLEAR,
        )
        self.assertTrue(ShareLog.objects.filter(
            share=self.share,
            action=ShareLog.ActionType.RESTRICTION_RELEASE,
        ).exists())
        self.assertTrue(SiteMessage.objects.filter(
            recipient=self.author,
            related_share=self.share,
            message_type=SiteMessage.MessageType.SHARE_RESTORED,
        ).exists())
