from dataclasses import FrozenInstanceError
from urllib.parse import quote

from django.contrib.auth.models import AnonymousUser, User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .models import Collection, Share, ShareLog
from .presentation import (
    ShareDetailActionsViewModel,
    build_share_detail_view_model,
)
from .selectors import share_detail_queryset


class ShareDetailSelectorTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(
            username='detail-author',
            password='password123',
        )
        self.author.profile.nickname = '详情作者'
        self.author.profile.bio = '作者简介'
        self.author.profile.save(update_fields=['nickname', 'bio'])
        self.viewer = User.objects.create_user(
            username='detail-viewer',
            password='password123',
        )
        self.other = User.objects.create_user(
            username='detail-other',
            password='password123',
        )
        self.share = Share.objects.create(
            title='详情查询契约',
            strategy_code='[stgy:detail-query]',
            author=self.author,
        )
        self.share.likes.add(self.viewer, self.other)
        self.share.favorites.add(self.viewer)

    def test_authenticated_detail_selector_loads_all_main_fields_in_one_query(self):
        with CaptureQueriesContext(connection) as captured:
            share = share_detail_queryset(self.viewer).get(pk=self.share.pk)
            detail = build_share_detail_view_model(share, self.viewer)
            snapshot = (
                detail.author.display_name,
                detail.author.bio,
                detail.likes_count,
                detail.favorites_count,
                detail.is_liked,
                detail.is_favorited,
            )

        self.assertEqual(len(captured), 1)
        self.assertEqual(snapshot, ('详情作者', '作者简介', 2, 1, True, True))

    def test_anonymous_detail_selector_annotates_false_interaction_state(self):
        with CaptureQueriesContext(connection) as captured:
            share = share_detail_queryset(AnonymousUser()).get(pk=self.share.pk)
            snapshot = (
                share.author.profile.get_display_name(),
                share.likes_count,
                share.favorites_count,
                share.is_liked,
                share.is_favorited,
            )

        self.assertEqual(len(captured), 1)
        self.assertEqual(snapshot, ('详情作者', 2, 1, False, False))

    def test_detail_counts_do_not_create_a_likes_times_favorites_join(self):
        sql = str(share_detail_queryset(AnonymousUser()).query)

        self.assertNotIn('LEFT OUTER JOIN "shares_share_likes"', sql)
        self.assertNotIn('LEFT OUTER JOIN "shares_share_favorites"', sql)


class ShareDetailPresentationTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(
            username='presentation-author',
            password='password123',
        )
        self.author.profile.nickname = '展示作者'
        self.author.profile.bio = '展示简介'
        self.author.profile.save(update_fields=['nickname', 'bio'])
        self.other = User.objects.create_user(
            username='presentation-other',
            password='password123',
        )
        self.staff = User.objects.create_user(
            username='presentation-staff',
            password='password123',
            is_staff=True,
        )
        self.staff_author = User.objects.create_user(
            username='presentation-staff-author',
            password='password123',
            is_staff=True,
        )
        self.share = Share.objects.create(
            title='详情展示契约',
            strategy_code='[stgy:detail-presentation]',
            author=self.author,
            category=Share.Category.COMBAT,
            is_original=True,
        )
        self.staff_share = Share.objects.create(
            title='管理员自己的分享',
            strategy_code='[stgy:staff-owner]',
            author=self.staff_author,
        )

    def detail_for(self, share, viewer):
        selected = share_detail_queryset(viewer).get(pk=share.pk)
        return build_share_detail_view_model(selected, viewer)

    def test_action_matrix_matches_existing_detail_permissions(self):
        cases = (
            (
                'anonymous',
                self.share,
                AnonymousUser(),
                ShareDetailActionsViewModel(False, False, False, False, False),
            ),
            (
                'owner',
                self.share,
                self.author,
                ShareDetailActionsViewModel(True, True, True, False, False),
            ),
            (
                'authenticated viewer',
                self.share,
                self.other,
                ShareDetailActionsViewModel(False, False, False, True, False),
            ),
            (
                'moderator',
                self.share,
                self.staff,
                ShareDetailActionsViewModel(False, True, False, True, True),
            ),
            (
                'moderator owner',
                self.staff_share,
                self.staff_author,
                ShareDetailActionsViewModel(True, True, True, False, True),
            ),
        )

        for label, share, viewer, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(self.detail_for(share, viewer).actions, expected)

    def test_author_and_badges_are_normalized_for_the_template(self):
        detail = self.detail_for(self.share, AnonymousUser())

        self.assertEqual(detail.author.display_name, '展示作者')
        self.assertEqual(detail.author.username, self.author.username)
        self.assertEqual(
            detail.author.profile_url,
            reverse('user_public_profile', args=[self.author.username]),
        )
        self.assertEqual(detail.author.bio, '展示简介')
        self.assertFalse(detail.author.is_anonymous)
        self.assertEqual(
            [badge.key for badge in detail.badges],
            ['combat', 'original'],
        )
        with self.assertRaises(FrozenInstanceError):
            detail.likes_count = 99

    def test_anonymous_author_has_no_profile_target(self):
        anonymous_share = Share.objects.create(
            title='匿名分享',
            strategy_code='[stgy:anonymous-author]',
            author=None,
        )

        detail = self.detail_for(anonymous_share, AnonymousUser())

        self.assertEqual(detail.author.display_name, '匿名用户')
        self.assertIsNone(detail.author.username)
        self.assertIsNone(detail.author.profile_url)
        self.assertEqual(detail.author.bio, '')
        self.assertTrue(detail.author.is_anonymous)

    def test_pending_notice_is_public_but_rejected_notice_is_privileged(self):
        pending = Share.objects.create(
            title='待审核详情',
            strategy_code='[stgy:pending-detail]',
            author=self.author,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.PENDING,
        )
        rejected = Share.objects.create(
            title='审核失败详情',
            strategy_code='[stgy:rejected-detail]',
            author=self.author,
            visibility=Share.Visibility.PRIVATE,
            status=Share.Status.REJECTED,
            review_feedback='',
        )

        pending_detail = self.detail_for(pending, AnonymousUser())
        self.assertEqual(pending_detail.notice.key, 'pending')
        self.assertIn('pending', [badge.key for badge in pending_detail.badges])

        for viewer in (self.author, self.staff):
            with self.subTest(viewer=viewer.username):
                rejected_detail = self.detail_for(rejected, viewer)
                self.assertEqual(rejected_detail.notice.key, 'rejected')
                self.assertEqual(rejected_detail.notice.feedback, '')
                self.assertIn('请修改内容后重新提交', rejected_detail.notice.message)
                self.assertIn(
                    'rejected',
                    [badge.key for badge in rejected_detail.badges],
                )

        unauthorized_detail = self.detail_for(rejected, self.other)
        self.assertIsNone(unauthorized_detail.notice)
        self.assertNotIn(
            'rejected',
            [badge.key for badge in unauthorized_detail.badges],
        )

        rejected.review_feedback = '请补充必要说明'
        rejected.save(update_fields=['review_feedback'])
        feedback_detail = self.detail_for(rejected, self.author)
        self.assertEqual(feedback_detail.notice.feedback, '请补充必要说明')

    def test_preview_url_encodes_strategy_code_as_one_path_segment(self):
        special_code = '[stgy:a/b?c#d+e"f]'
        share = Share.objects.create(
            title='特殊字符预览',
            strategy_code=special_code,
            author=self.author,
            is_spoiler=True,
            is_nsfw=True,
        )

        detail = self.detail_for(share, AnonymousUser())

        self.assertEqual(
            detail.preview_url,
            f'/n/board/{quote(special_code, safe="")}',
        )
        encoded_segment = detail.preview_url.removeprefix('/n/board/')
        for delimiter in ('/', '?', '#', '+', '"'):
            self.assertNotIn(delimiter, encoded_segment)
        self.assertEqual(detail.content_warning.key, 'nsfw-spoiler')
        self.assertIn('令人不适和剧透', detail.content_warning.message)

    def test_detail_view_uses_actions_to_load_logs_and_owner_collections(self):
        collection = Collection.objects.create(
            title='作者合集',
            author=self.author,
        )
        log = ShareLog.objects.create(
            share=self.share,
            user=self.staff,
            action=ShareLog.ActionType.EDIT,
            details='展示测试日志',
        )

        self.client.force_login(self.author)
        owner_response = self.client.get(
            reverse('share_detail', args=[self.share.share_id]),
        )
        self.assertEqual(owner_response.status_code, 200)
        self.assertEqual(list(owner_response.context['user_collections']), [collection])
        self.assertIsNone(owner_response.context['share_logs'])
        self.assertTrue(owner_response.context['detail'].actions.can_add_to_collection)

        self.client.force_login(self.staff)
        staff_response = self.client.get(
            reverse('share_detail', args=[self.share.share_id]),
        )
        self.assertEqual(staff_response.status_code, 200)
        self.assertEqual(staff_response.context['user_collections'], [])
        self.assertEqual(list(staff_response.context['share_logs']), [log])
        self.assertTrue(staff_response.context['detail'].actions.can_view_logs)

        self.client.force_login(self.other)
        viewer_response = self.client.get(
            reverse('share_detail', args=[self.share.share_id]),
        )
        self.assertEqual(viewer_response.status_code, 200)
        self.assertEqual(viewer_response.context['user_collections'], [])
        self.assertIsNone(viewer_response.context['share_logs'])
