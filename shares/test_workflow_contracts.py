import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import close_old_connections, connection
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from .models import Collection, CollectionItem, Report, Share, ShareLog, UserProfile
from .services import interactions as interaction_service


class InteractionServiceBoundaryTests(SimpleTestCase):
    @staticmethod
    def operational_error(sqlite_errorcode):
        cause = sqlite3.OperationalError('database table is locked')
        cause.sqlite_errorcode = sqlite_errorcode
        error = interaction_service.OperationalError(str(cause))
        error.__cause__ = cause
        return error

    def test_only_shared_cache_lock_conflicts_are_retryable(self):
        shared_cache_lock = self.operational_error(sqlite3.SQLITE_LOCKED_SHAREDCACHE)
        busy_timeout = self.operational_error(sqlite3.SQLITE_BUSY)

        with patch.object(
            interaction_service,
            'connection',
            SimpleNamespace(vendor='sqlite'),
        ):
            self.assertTrue(
                interaction_service._is_transient_sqlite_lock(shared_cache_lock),
            )
            self.assertFalse(interaction_service._is_transient_sqlite_lock(busy_timeout))

        with patch.object(
            interaction_service,
            'connection',
            SimpleNamespace(vendor='postgresql'),
        ):
            self.assertFalse(
                interaction_service._is_transient_sqlite_lock(shared_cache_lock),
            )

    def test_shared_cache_lock_retries_the_idempotent_target_operation(self):
        shared_cache_lock = self.operational_error(sqlite3.SQLITE_LOCKED_SHAREDCACHE)
        expected = object()

        with (
            patch.object(
                interaction_service,
                'connection',
                SimpleNamespace(vendor='sqlite'),
            ),
            patch.object(
                interaction_service,
                '_set_interaction_state_once',
                side_effect=[shared_cache_lock, expected],
            ) as mutate,
            patch.object(interaction_service, 'sleep') as retry_sleep,
        ):
            result = interaction_service.set_like_state(
                share_id='share-id',
                user=object(),
                target_active=True,
            )

        self.assertIs(result, expected)
        self.assertEqual(mutate.call_count, 2)
        retry_sleep.assert_called_once_with(
            interaction_service.SQLITE_LOCK_RETRY_DELAYS[0],
        )

    def test_busy_timeout_failure_is_not_retried(self):
        busy_timeout = self.operational_error(sqlite3.SQLITE_BUSY)

        with (
            patch.object(
                interaction_service,
                'connection',
                SimpleNamespace(vendor='sqlite'),
            ),
            patch.object(
                interaction_service,
                '_set_interaction_state_once',
                side_effect=busy_timeout,
            ) as mutate,
            patch.object(interaction_service, 'sleep') as retry_sleep,
        ):
            with self.assertRaises(interaction_service.OperationalError):
                interaction_service.set_like_state(
                    share_id='share-id',
                    user=object(),
                    target_active=True,
                )

        mutate.assert_called_once()
        retry_sleep.assert_not_called()

    def test_integrity_error_only_becomes_unavailable_when_share_was_deleted(self):
        integrity_error = interaction_service.IntegrityError('foreign key failed')

        for share_exists, expected_error in (
            (False, interaction_service.ShareInteractionUnavailableError),
            (True, interaction_service.IntegrityError),
        ):
            with (
                self.subTest(share_exists=share_exists),
                patch.object(
                    interaction_service,
                    '_set_interaction_state_once',
                    side_effect=integrity_error,
                ),
                patch.object(
                    interaction_service.Share.objects,
                    'filter',
                    return_value=SimpleNamespace(
                        exists=lambda: share_exists,
                    ),
                ),
            ):
                with self.assertRaises(expected_error):
                    interaction_service.set_like_state(
                        share_id='share-id',
                        user=object(),
                        target_active=True,
                    )


class ShareWriteWorkflowTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(username='author', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')
        self.admin = User.objects.create_user(username='admin', password='password123', is_staff=True)

    def share_form_data(self, **overrides):
        data = {
            'title': '测试分享',
            'strategy_code': '[stgy:test-code]',
            'description': '',
            'category': Share.Category.ENTERTAINMENT,
            'visibility': Share.Visibility.PUBLIC,
        }
        data.update(overrides)
        return data

    def create_share(self, **overrides):
        data = {
            'title': '已有分享',
            'strategy_code': '[stgy:existing]',
            'author': self.author,
            'visibility': Share.Visibility.PUBLIC,
            'status': Share.Status.APPROVED,
        }
        data.update(overrides)
        return Share.objects.create(**data)

    def edit_form_data(self, share, **overrides):
        data = self.share_form_data(
            title=share.title,
            strategy_code=share.strategy_code,
            description=share.description,
            category=share.category,
            visibility=share.visibility,
            version=share.updated_at.isoformat(),
        )
        data.update(overrides)
        return data

    def test_anonymous_create_forces_unlisted_approved_share(self):
        response = self.client.post(reverse('create_share'), self.share_form_data())

        self.assertEqual(response.status_code, 302)
        share = Share.objects.get()
        self.assertIsNone(share.author)
        self.assertEqual(share.visibility, Share.Visibility.UNLISTED)
        self.assertEqual(share.status, Share.Status.APPROVED)

    def test_regular_user_public_create_requires_review(self):
        self.client.force_login(self.author)

        response = self.client.post(reverse('create_share'), self.share_form_data())

        self.assertEqual(response.status_code, 302)
        share = Share.objects.get()
        self.assertEqual(share.author, self.author)
        self.assertEqual(share.visibility, Share.Visibility.PUBLIC)
        self.assertEqual(share.status, Share.Status.PENDING)

    def test_staff_public_create_is_approved_immediately(self):
        self.client.force_login(self.admin)

        response = self.client.post(reverse('create_share'), self.share_form_data())

        self.assertEqual(response.status_code, 302)
        share = Share.objects.get()
        self.assertEqual(share.author, self.admin)
        self.assertEqual(share.status, Share.Status.APPROVED)

    def test_regular_user_unlisted_create_does_not_require_review(self):
        self.client.force_login(self.author)

        response = self.client.post(
            reverse('create_share'),
            self.share_form_data(visibility=Share.Visibility.UNLISTED),
        )

        self.assertEqual(response.status_code, 302)
        share = Share.objects.get()
        self.assertEqual(share.visibility, Share.Visibility.UNLISTED)
        self.assertEqual(share.status, Share.Status.APPROVED)

    def test_create_adds_share_to_owned_collection_with_stable_next_order(self):
        collection = Collection.objects.create(title='目标合集', author=self.author)
        existing = self.create_share(title='合集已有分享')
        CollectionItem.objects.create(collection=collection, share=existing, order=7)
        self.client.force_login(self.author)

        response = self.client.post(
            reverse('create_share'),
            self.share_form_data(
                title='新合集分享',
                collection_id=str(collection.pk),
            ),
        )

        self.assertEqual(response.status_code, 302)
        created = Share.objects.get(title='新合集分享')
        self.assertTrue(CollectionItem.objects.filter(
            collection=collection,
            share=created,
            order=8,
        ).exists())

    def test_create_rejects_malformed_missing_or_foreign_collection(self):
        foreign_collection = Collection.objects.create(
            title='他人的合集',
            author=self.other_user,
        )
        self.client.force_login(self.author)

        for collection_id in ('not-a-number', '999999', str(foreign_collection.pk)):
            with self.subTest(collection_id=collection_id):
                response = self.client.post(
                    reverse('create_share'),
                    self.share_form_data(collection_id=collection_id),
                )

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, '选择一个有效的选项')
                self.assertFalse(Share.objects.exists())

    def test_create_form_preserves_selected_collection_after_other_field_error(self):
        collection = Collection.objects.create(title='保留选择的合集', author=self.author)
        self.client.force_login(self.author)

        response = self.client.post(
            reverse('create_share'),
            self.share_form_data(
                strategy_code='invalid-code',
                collection_id=str(collection.pk),
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f'<option value="{collection.pk}" selected>保留选择的合集</option>',
            html=True,
        )
        self.assertFalse(Share.objects.exists())

    def test_create_rolls_back_share_when_audit_log_fails(self):
        self.client.force_login(self.author)

        with patch(
            'shares.services.shares.log_share_action',
            side_effect=RuntimeError('injected audit failure'),
        ):
            with self.assertRaisesRegex(RuntimeError, 'injected audit failure'):
                self.client.post(reverse('create_share'), self.share_form_data())

        self.assertFalse(Share.objects.exists())
        self.assertFalse(ShareLog.objects.exists())

    def test_editing_public_share_restarts_review_and_clears_old_feedback(self):
        share = self.create_share(
            review_feedback='旧反馈',
            reviewed_at=timezone.now(),
            reviewed_by=self.admin,
        )
        self.client.force_login(self.author)

        response = self.client.post(
            reverse('edit_share', args=[share.share_id]),
            self.edit_form_data(share, title='修改后的分享'),
        )

        self.assertEqual(response.status_code, 302)
        share.refresh_from_db()
        self.assertEqual(share.title, '修改后的分享')
        self.assertEqual(share.status, Share.Status.PENDING)
        self.assertEqual(share.review_feedback, '')
        self.assertIsNone(share.reviewed_at)
        self.assertIsNone(share.reviewed_by)

    def test_restricted_edit_preserves_restriction_and_requires_review_for_all_authors_and_visibilities(self):
        cases = (
            ('regular-public', self.author, Share.Visibility.PUBLIC, Share.Visibility.UNLISTED),
            ('regular-unlisted', self.author, Share.Visibility.UNLISTED, Share.Visibility.PRIVATE),
            ('regular-private', self.author, Share.Visibility.PRIVATE, Share.Visibility.PUBLIC),
            ('staff-public', self.admin, Share.Visibility.PUBLIC, Share.Visibility.PRIVATE),
            ('staff-unlisted', self.admin, Share.Visibility.UNLISTED, Share.Visibility.PUBLIC),
            ('staff-private', self.admin, Share.Visibility.PRIVATE, Share.Visibility.UNLISTED),
        )
        for case_name, actor, visibility, target_visibility in cases:
            with self.subTest(case=case_name):
                restricted_at = timezone.now()
                share = self.create_share(
                    title=f'{case_name} 原标题',
                    strategy_code=f'[stgy:{case_name}]',
                    author=actor,
                    visibility=visibility,
                    review_feedback='旧审核反馈',
                    reviewed_at=restricted_at,
                    reviewed_by=self.admin,
                    restriction_state=Share.RestrictionState.REPORT_TAKEDOWN,
                    restriction_reason='举报下架限制必须保留',
                    restricted_at=restricted_at,
                    restricted_by=self.admin,
                )
                self.client.force_login(actor)

                response = self.client.post(
                    reverse('edit_share', args=[share.share_id]),
                    self.edit_form_data(
                        share,
                        title=f'{case_name} 修改后',
                        visibility=target_visibility,
                    ),
                )

                self.assertEqual(response.status_code, 302)
                share.refresh_from_db()
                self.assertEqual(share.title, f'{case_name} 修改后')
                self.assertEqual(share.visibility, target_visibility)
                self.assertEqual(share.status, Share.Status.PENDING)
                self.assertEqual(
                    share.restriction_state,
                    Share.RestrictionState.REPORT_TAKEDOWN,
                )
                self.assertEqual(
                    share.restriction_reason,
                    '举报下架限制必须保留',
                )
                self.assertEqual(share.restricted_at, restricted_at)
                self.assertEqual(share.restricted_by, self.admin)
                self.assertEqual(share.review_feedback, '')
                self.assertIsNone(share.reviewed_at)
                self.assertIsNone(share.reviewed_by)

    def test_legacy_private_cannot_use_private_classification_after_visibility_change(self):
        restricted_at = timezone.now()
        share = self.create_share(
            visibility=Share.Visibility.PRIVATE,
            restriction_state=Share.RestrictionState.LEGACY_PRIVATE,
            restriction_reason='历史私密状态来源待人工确认',
            restricted_at=restricted_at,
        )
        self.client.force_login(self.author)

        response = self.client.post(
            reverse('edit_share', args=[share.share_id]),
            self.edit_form_data(
                share,
                visibility=Share.Visibility.PUBLIC,
            ),
        )

        self.assertEqual(response.status_code, 302)
        share.refresh_from_db()
        self.assertEqual(share.visibility, Share.Visibility.PUBLIC)
        self.assertEqual(share.status, Share.Status.PENDING)
        self.assertEqual(
            share.restriction_state,
            Share.RestrictionState.LEGACY_PRIVATE,
        )

        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('admin_release_share_restriction', args=[share.share_id]),
            {'reason': '不能把公开内容分类为作者私密'},
        )

        self.assertEqual(response.status_code, 302)
        share.refresh_from_db()
        self.assertEqual(
            share.restriction_state,
            Share.RestrictionState.LEGACY_PRIVATE,
        )
        self.assertFalse(ShareLog.objects.filter(
            share=share,
            action=ShareLog.ActionType.RESTRICTION_RELEASE,
        ).exists())

    def test_no_change_edit_preserves_review_state_and_timestamp(self):
        reviewed_at = timezone.now()
        share = self.create_share(
            description='<p>原描述</p>',
            review_feedback='保留的审核说明',
            reviewed_at=reviewed_at,
            reviewed_by=self.admin,
        )
        original_updated_at = share.updated_at
        self.client.force_login(self.author)

        response = self.client.post(
            reverse('edit_share', args=[share.share_id]),
            self.edit_form_data(share),
        )

        self.assertRedirects(response, reverse('share_detail', args=[share.share_id]))
        share.refresh_from_db()
        self.assertEqual(share.status, Share.Status.APPROVED)
        self.assertEqual(share.review_feedback, '保留的审核说明')
        self.assertEqual(share.reviewed_at, reviewed_at)
        self.assertEqual(share.reviewed_by, self.admin)
        self.assertEqual(share.updated_at, original_updated_at)
        self.assertFalse(ShareLog.objects.filter(
            share=share,
            action=ShareLog.ActionType.EDIT,
        ).exists())

    def test_normalized_equivalent_input_is_not_logged_as_an_edit(self):
        share = self.create_share()
        original_updated_at = share.updated_at
        self.client.force_login(self.author)

        response = self.client.post(
            reverse('edit_share', args=[share.share_id]),
            self.edit_form_data(
                share,
                strategy_code=f'导出内容：{share.strategy_code} 请复制',
            ),
        )

        self.assertEqual(response.status_code, 302)
        share.refresh_from_db()
        self.assertEqual(share.status, Share.Status.APPROVED)
        self.assertEqual(share.updated_at, original_updated_at)
        self.assertFalse(ShareLog.objects.filter(
            share=share,
            action=ShareLog.ActionType.EDIT,
        ).exists())

    def test_title_edit_does_not_rewrite_unchanged_legacy_description(self):
        share = self.create_share()
        legacy_description = '<p data-legacy="kept">旧版 <strong>描述</strong></p>'
        Share.objects.filter(pk=share.pk).update(description=legacy_description)
        share.refresh_from_db()
        self.client.force_login(self.author)

        response = self.client.post(
            reverse('edit_share', args=[share.share_id]),
            self.edit_form_data(
                share,
                title='只修改标题',
                description=legacy_description,
            ),
        )

        self.assertEqual(response.status_code, 302)
        share.refresh_from_db()
        self.assertEqual(share.title, '只修改标题')
        self.assertEqual(share.description, legacy_description)

    def test_edit_rolls_back_all_fields_when_audit_log_fails(self):
        share = self.create_share(
            review_feedback='旧反馈',
            reviewed_at=timezone.now(),
            reviewed_by=self.admin,
        )
        self.client.force_login(self.author)

        with patch(
            'shares.services.shares.log_share_action',
            side_effect=RuntimeError('injected audit failure'),
        ):
            with self.assertRaisesRegex(RuntimeError, 'injected audit failure'):
                self.client.post(
                    reverse('edit_share', args=[share.share_id]),
                    self.edit_form_data(share, title='不应保存的标题'),
                )

        share.refresh_from_db()
        self.assertEqual(share.title, '已有分享')
        self.assertEqual(share.status, Share.Status.APPROVED)
        self.assertEqual(share.review_feedback, '旧反馈')
        self.assertEqual(share.reviewed_by, self.admin)

    def test_stale_editor_cannot_overwrite_newer_moderation_state(self):
        share = self.create_share()
        self.client.force_login(self.author)
        page = self.client.get(reverse('edit_share', args=[share.share_id]))
        editor_version = page.context['form']['version'].value()
        moderated_at = timezone.now()
        Share.objects.filter(pk=share.pk).update(
            status=Share.Status.REJECTED,
            review_feedback='并发审核结果',
            reviewed_at=moderated_at,
            reviewed_by=self.admin,
            restriction_state=Share.RestrictionState.REVIEW_REJECTED,
            restriction_reason='并发审核结果',
            restricted_at=moderated_at,
            restricted_by=self.admin,
            updated_at=moderated_at,
        )

        response = self.client.post(
            reverse('edit_share', args=[share.share_id]),
            self.share_form_data(
                title='过期页面的修改',
                version=editor_version,
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '该分享已被其他操作更新')
        share.refresh_from_db()
        self.assertEqual(share.title, '已有分享')
        self.assertEqual(share.status, Share.Status.REJECTED)
        self.assertEqual(share.visibility, Share.Visibility.PUBLIC)
        self.assertEqual(share.review_feedback, '并发审核结果')
        self.assertEqual(
            share.restriction_state,
            Share.RestrictionState.REVIEW_REJECTED,
        )
        self.assertEqual(share.restriction_reason, '并发审核结果')

    def test_missing_or_invalid_edit_version_cannot_write(self):
        share = self.create_share()
        self.client.force_login(self.author)

        for version, expected_error in (
            (None, '编辑页面缺少版本信息'),
            ('not-a-version', '编辑页面版本无效'),
        ):
            with self.subTest(version=version):
                data = self.edit_form_data(share, title='不应保存的修改')
                if version is None:
                    data.pop('version')
                else:
                    data['version'] = version

                response = self.client.post(
                    reverse('edit_share', args=[share.share_id]),
                    data,
                )

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, expected_error)
                share.refresh_from_db()
                self.assertEqual(share.title, '已有分享')

    def test_non_owner_cannot_delete_share(self):
        share = self.create_share()
        self.client.force_login(self.other_user)

        response = self.client.post(reverse('delete_share', args=[share.share_id]))

        self.assertRedirects(response, reverse('share_detail', args=[share.share_id]))
        self.assertTrue(Share.objects.filter(pk=share.pk).exists())

    def test_author_can_delete_share(self):
        share = self.create_share()
        self.client.force_login(self.author)

        response = self.client.post(reverse('delete_share', args=[share.share_id]))

        self.assertRedirects(response, reverse('my_shares'))
        self.assertFalse(Share.objects.filter(pk=share.pk).exists())


class InteractionWorkflowTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(username='author', password='password123')
        self.user = User.objects.create_user(username='user', password='password123')
        self.share = Share.objects.create(
            title='互动测试',
            strategy_code='[stgy:interaction]',
            author=self.author,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )

    def test_like_endpoint_sets_explicit_state_idempotently(self):
        self.client.force_login(self.user)

        url = reverse('toggle_like', args=[self.share.share_id])
        add_responses = [
            self.client.post(url, {'target_state': 'active'}),
            self.client.post(url, {'target_state': 'active'}),
        ]
        remove_responses = [
            self.client.post(url, {'target_state': 'inactive'}),
            self.client.post(url, {'target_state': 'inactive'}),
        ]

        for response in add_responses:
            self.assertEqual(response.json(), {
                'status': 'success',
                'is_liked': True,
                'likes_count': 1,
            })
        for response in remove_responses:
            self.assertEqual(response.json(), {
                'status': 'success',
                'is_liked': False,
                'likes_count': 0,
            })
        self.assertFalse(self.share.likes.filter(pk=self.user.pk).exists())
        self.assertIn('HX-Request', add_responses[0].headers['Vary'])
        self.assertIn('no-store', add_responses[0].headers['Cache-Control'])

    def test_hx_like_endpoint_returns_reusable_card_button(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('toggle_like', args=[self.share.share_id]) + '?fragment=card',
            {'target_state': 'active'},
            HTTP_HX_REQUEST='true',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers['Content-Type'].startswith('text/html'))
        self.assertContains(response, 'btn-danger')
        self.assertContains(response, 'bi-heart-fill')
        self.assertContains(response, 'hx-post=')
        self.assertContains(response, 'aria-label="点赞，当前 1 个点赞"')
        self.assertContains(response, 'aria-pressed="true"')
        self.assertContains(response, 'hx-vals=\'{"target_state":"inactive"}\'')
        self.assertContains(response, 'hx-sync="this:drop"')
        self.assertContains(response, 'hx-disabled-elt="this"')
        self.assertContains(response, '>1</span>')
        self.assertIn('HX-Request', response.headers['Vary'])
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertTrue(self.share.likes.filter(pk=self.user.pk).exists())

    def test_hx_like_endpoint_returns_detail_button(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('toggle_like', args=[self.share.share_id]) + '?fragment=detail',
            {'target_state': 'active'},
            HTTP_HX_REQUEST='true',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'btn-danger')
        self.assertContains(response, 'bi-heart-fill')
        self.assertContains(response, 'fragment=detail')
        self.assertContains(response, 'me-2')
        self.assertContains(response, '>1</span>')
        self.assertNotContains(response, 'w-50')
        self.assertTrue(self.share.likes.filter(pk=self.user.pk).exists())

    def test_hx_inactive_interactions_emit_relation_specific_refresh_events(self):
        self.client.force_login(self.user)

        cases = (
            ('toggle_like', 'likes', 'share-like-removed'),
            ('toggle_favorite', 'favorites', 'share-favorite-removed'),
        )
        for endpoint, relation_name, event_name in cases:
            with self.subTest(endpoint=endpoint):
                relation = getattr(self.share, relation_name)
                relation.add(self.user)
                url = reverse(endpoint, args=[self.share.share_id]) + '?fragment=card'

                inactive_response = self.client.post(
                    url,
                    {'target_state': 'inactive'},
                    HTTP_HX_REQUEST='true',
                )

                self.assertEqual(inactive_response.status_code, 200)
                self.assertEqual(
                    json.loads(inactive_response.headers['HX-Trigger-After-Swap']),
                    {event_name: {'shareId': self.share.share_id}},
                )
                self.assertFalse(relation.filter(pk=self.user.pk).exists())

                active_response = self.client.post(
                    url,
                    {'target_state': 'active'},
                    HTTP_HX_REQUEST='true',
                )
                self.assertEqual(active_response.status_code, 200)
                self.assertNotIn('HX-Trigger-After-Swap', active_response.headers)
                self.assertTrue(relation.filter(pk=self.user.pk).exists())

    def test_favorite_endpoint_sets_explicit_state_idempotently(self):
        self.client.force_login(self.user)

        url = reverse('toggle_favorite', args=[self.share.share_id])
        add_responses = [
            self.client.post(url, {'target_state': 'active'}),
            self.client.post(url, {'target_state': 'active'}),
        ]
        remove_responses = [
            self.client.post(url, {'target_state': 'inactive'}),
            self.client.post(url, {'target_state': 'inactive'}),
        ]

        for response in add_responses:
            self.assertEqual(response.json(), {
                'status': 'success',
                'is_favorited': True,
                'favorites_count': 1,
            })
        for response in remove_responses:
            self.assertEqual(response.json(), {
                'status': 'success',
                'is_favorited': False,
                'favorites_count': 0,
            })
        self.assertFalse(self.share.favorites.filter(pk=self.user.pk).exists())

    def test_plain_form_interactions_redirect_and_remain_idempotent(self):
        self.client.force_login(self.user)
        return_url = f'{self.share.get_absolute_url()}?source=interaction#actions'

        for url_name, relation_name in (
            ('toggle_like', 'likes'),
            ('toggle_favorite', 'favorites'),
        ):
            relation = getattr(self.share, relation_name)
            for target_state, expected_active in (
                ('active', True),
                ('active', True),
                ('inactive', False),
                ('inactive', False),
            ):
                with self.subTest(endpoint=url_name, target_state=target_state):
                    response = self.client.post(
                        reverse(url_name, args=[self.share.share_id]),
                        {'target_state': target_state, 'next': return_url},
                    )

                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.headers['Location'], return_url)
                    self.assertEqual(
                        relation.filter(pk=self.user.pk).exists(),
                        expected_active,
                    )
                    self.assertIn('HX-Request', response.headers['Vary'])
                    self.assertIn('Cookie', response.headers['Vary'])
                    self.assertIn('no-store', response.headers['Cache-Control'])

    def test_plain_form_interaction_normalizes_same_origin_https_next(self):
        self.client.force_login(self.user)
        return_url = f'{self.share.get_absolute_url()}?source=absolute#actions'

        response = self.client.post(
            reverse('toggle_like', args=[self.share.share_id]),
            {
                'target_state': 'active',
                'next': f'https://testserver{return_url}',
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], return_url)
        self.assertTrue(self.share.likes.filter(pk=self.user.pk).exists())

    def test_plain_form_interaction_rejects_unsafe_next(self):
        self.client.force_login(self.user)
        action_url = reverse('toggle_like', args=[self.share.share_id])
        canonical_url = self.share.get_absolute_url()
        unsafe_urls = (
            'https://example.invalid/phishing',
            '//example.invalid/phishing',
            '///example.invalid/phishing',
            'relative/path',
            '/\\example.invalid/phishing',
            'https://testserver.example.invalid/phishing',
            'javascript:alert(1)',
            'https://[invalid',
            f'{canonical_url}\r\nX-Injected: true',
        )

        for unsafe_url in unsafe_urls:
            with self.subTest(next=unsafe_url):
                response = self.client.post(
                    action_url,
                    {'target_state': 'active', 'next': unsafe_url},
                )

                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers['Location'], canonical_url)

        insecure_response = self.client.post(
            action_url,
            {
                'target_state': 'active',
                'next': f'http://testserver{canonical_url}',
            },
            secure=True,
        )
        self.assertEqual(insecure_response.status_code, 302)
        self.assertEqual(insecure_response.headers['Location'], canonical_url)

    def test_hx_interaction_ignores_form_next_and_returns_fragment(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('toggle_favorite', args=[self.share.share_id]) + '?fragment=card',
            {
                'target_state': 'active',
                'next': self.share.get_absolute_url(),
            },
            HTTP_HX_REQUEST='true',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers['Content-Type'].startswith('text/html'))
        self.assertNotIn('Location', response.headers)
        self.assertContains(response, 'hx-post=')
        self.assertTrue(self.share.favorites.filter(pk=self.user.pk).exists())

    def test_plain_form_login_redirect_uses_a_validated_source_page(self):
        action_url = reverse('toggle_like', args=[self.share.share_id])
        detail_url = self.share.get_absolute_url()
        form_source = f'{detail_url}?source=form'
        referer_source = f'{detail_url}?source=referer'

        response = self.client.post(
            action_url,
            {'target_state': 'active', 'next': form_source},
            HTTP_REFERER=f'http://testserver{referer_source}',
        )
        redirect_query = parse_qs(urlsplit(response.headers['Location']).query)
        self.assertEqual(redirect_query['next'], [form_source])
        self.assertNotEqual(redirect_query['next'], [action_url])

        unsafe_response = self.client.post(
            action_url,
            {
                'target_state': 'active',
                'next': 'https://example.invalid/phishing',
            },
            HTTP_REFERER=f'http://testserver{referer_source}',
        )
        unsafe_redirect_query = parse_qs(
            urlsplit(unsafe_response.headers['Location']).query,
        )
        self.assertEqual(unsafe_redirect_query['next'], [referer_source])
        self.assertNotEqual(unsafe_redirect_query['next'], [action_url])

        no_referer_response = self.client.post(
            action_url,
            {
                'target_state': 'active',
                'next': 'javascript:alert(1)',
            },
        )
        no_referer_redirect_query = parse_qs(
            urlsplit(no_referer_response.headers['Location']).query,
        )
        self.assertEqual(no_referer_redirect_query['next'], [detail_url])
        self.assertNotEqual(no_referer_redirect_query['next'], [action_url])
        self.assertFalse(self.share.likes.exists())

    def test_plain_interaction_without_next_preserves_legacy_login_return_url(self):
        action_url = reverse('toggle_favorite', args=[self.share.share_id])

        response = self.client.post(action_url, {'target_state': 'active'})

        redirect_query = parse_qs(urlsplit(response.headers['Location']).query)
        self.assertEqual(redirect_query['next'], [action_url])
        self.assertFalse(self.share.favorites.exists())

    def test_plain_form_interaction_requires_csrf_token(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        action_url = reverse('toggle_like', args=[self.share.share_id])
        detail_url = self.share.get_absolute_url()

        denied = csrf_client.post(
            action_url,
            {'target_state': 'active', 'next': detail_url},
        )
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(self.share.likes.filter(pk=self.user.pk).exists())

        page = csrf_client.get(detail_url)
        csrf_token = page.cookies['csrftoken'].value
        allowed = csrf_client.post(
            action_url,
            {
                'target_state': 'active',
                'next': detail_url,
                'csrfmiddlewaretoken': csrf_token,
            },
        )

        self.assertEqual(allowed.status_code, 302)
        self.assertEqual(allowed.headers['Location'], detail_url)
        self.assertTrue(self.share.likes.filter(pk=self.user.pk).exists())

    def test_hx_favorite_endpoint_returns_reusable_card_button(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('toggle_favorite', args=[self.share.share_id]) + '?fragment=card',
            {'target_state': 'active'},
            HTTP_HX_REQUEST='true',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'btn-warning')
        self.assertContains(response, 'bi-star-fill')
        self.assertContains(response, 'hx-post=')
        self.assertContains(response, 'aria-label="收藏，当前 1 个收藏"')
        self.assertContains(response, 'aria-pressed="true"')
        self.assertContains(response, 'hx-vals=\'{"target_state":"inactive"}\'')
        self.assertContains(response, '>1</span>')
        self.assertTrue(self.share.favorites.filter(pk=self.user.pk).exists())

    def test_hx_favorite_endpoint_returns_detail_button(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('toggle_favorite', args=[self.share.share_id]) + '?fragment=detail',
            {'target_state': 'active'},
            HTTP_HX_REQUEST='true',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'btn-warning')
        self.assertContains(response, 'bi-star-fill')
        self.assertContains(response, 'fragment=detail')
        self.assertContains(response, '>1</span>')
        self.assertNotContains(response, 'w-50')
        self.assertTrue(self.share.favorites.filter(pk=self.user.pk).exists())

    def test_hx_interaction_rejects_unknown_or_missing_fragment_without_mutating(self):
        self.client.force_login(self.user)

        for query in ('', '?fragment=unknown'):
            with self.subTest(query=query):
                response = self.client.post(
                    reverse('toggle_like', args=[self.share.share_id]) + query,
                    {'target_state': 'active'},
                    HTTP_HX_REQUEST='true',
                )

                self.assertEqual(response.status_code, 400)
                self.assertFalse(self.share.likes.filter(pk=self.user.pk).exists())

    def test_interaction_requires_valid_target_state_without_mutating(self):
        self.client.force_login(self.user)

        for url_name, relation_name in (
            ('toggle_like', 'likes'),
            ('toggle_favorite', 'favorites'),
        ):
            url = reverse(url_name, args=[self.share.share_id])
            relation = getattr(self.share, relation_name)
            for payload in ({}, {'target_state': ''}, {'target_state': 'toggle'}):
                with self.subTest(endpoint=url_name, payload=payload):
                    response = self.client.post(url, payload)

                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()['status'], 'error')
                    self.assertFalse(relation.filter(pk=self.user.pk).exists())

        response = self.client.post(
            reverse('toggle_like', args=[self.share.share_id]) + '?fragment=card',
            {'target_state': 'toggle'},
            HTTP_HX_REQUEST='true',
        )
        self.assertContains(
            response,
            'target_state must be active or inactive.',
            status_code=400,
        )
        self.assertFalse(self.share.likes.filter(pk=self.user.pk).exists())

    def test_interaction_rolls_back_if_final_permission_check_fails(self):
        self.client.force_login(self.user)

        with patch.object(
            interaction_service,
            'can_view_share',
            side_effect=(True, False),
        ) as permission_check:
            response = self.client.post(
                reverse('toggle_like', args=[self.share.share_id]),
                {'target_state': 'active'},
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(permission_check.call_count, 2)
        self.assertFalse(self.share.likes.filter(pk=self.user.pk).exists())

    def test_expired_hx_interaction_redirects_the_full_page_to_login(self):
        detail_url = reverse('share_detail', args=[self.share.share_id])
        response = self.client.post(
            reverse('toggle_like', args=[self.share.share_id]) + '?fragment=card',
            {'target_state': 'active'},
            HTTP_HX_REQUEST='true',
            HTTP_HX_CURRENT_URL=f'http://testserver{detail_url}',
        )

        self.assertEqual(response.status_code, 204)
        redirect_query = parse_qs(urlsplit(response.headers['HX-Redirect']).query)
        self.assertEqual(redirect_query['next'], [detail_url])
        self.assertNotIn('Location', response.headers)
        self.assertFalse(self.share.likes.exists())

    def test_expired_hx_interaction_rejects_external_current_url(self):
        action_url = reverse('toggle_like', args=[self.share.share_id]) + '?fragment=card'
        detail_url = self.share.get_absolute_url()

        response = self.client.post(
            action_url,
            {'target_state': 'active'},
            HTTP_HX_REQUEST='true',
            HTTP_HX_CURRENT_URL='https://example.invalid/phishing',
        )

        redirect_query = parse_qs(urlsplit(response.headers['HX-Redirect']).query)
        self.assertEqual(redirect_query['next'], [detail_url])

    def test_expired_hx_interaction_uses_safe_form_next_when_current_url_is_missing(self):
        detail_url = self.share.get_absolute_url()
        form_source = f'{detail_url}?source=hx-form'

        response = self.client.post(
            reverse('toggle_like', args=[self.share.share_id]) + '?fragment=card',
            {'target_state': 'active', 'next': form_source},
            HTTP_HX_REQUEST='true',
        )

        redirect_query = parse_qs(urlsplit(response.headers['HX-Redirect']).query)
        self.assertEqual(redirect_query['next'], [form_source])
        self.assertFalse(self.share.likes.exists())

    def test_expired_hx_interaction_falls_back_to_canonical_detail(self):
        detail_url = self.share.get_absolute_url()

        response = self.client.post(
            reverse('toggle_favorite', args=[self.share.share_id]) + '?fragment=detail',
            {
                'target_state': 'active',
                'next': 'javascript:alert(1)',
            },
            HTTP_HX_REQUEST='true',
            HTTP_HX_CURRENT_URL='https://example.invalid/phishing',
            HTTP_REFERER='//example.invalid/phishing',
        )

        redirect_query = parse_qs(urlsplit(response.headers['HX-Redirect']).query)
        self.assertEqual(redirect_query['next'], [detail_url])
        self.assertFalse(self.share.favorites.exists())

    def test_hx_detail_interaction_requires_csrf_token(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        action_url = reverse('toggle_like', args=[self.share.share_id]) + '?fragment=detail'

        denied = csrf_client.post(
            action_url,
            {'target_state': 'active'},
            HTTP_HX_REQUEST='true',
        )

        self.assertEqual(denied.status_code, 403)
        self.assertFalse(self.share.likes.filter(pk=self.user.pk).exists())

        page = csrf_client.get(reverse('share_detail', args=[self.share.share_id]))
        csrf_token = page.cookies['csrftoken'].value
        allowed = csrf_client.post(
            action_url,
            {'target_state': 'active'},
            HTTP_HX_REQUEST='true',
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        self.assertEqual(allowed.status_code, 200)
        self.assertTrue(self.share.likes.filter(pk=self.user.pk).exists())

    def test_copy_counter_only_increments_once_per_client_cookie(self):
        first_response = self.client.post(reverse('record_copy', args=[self.share.share_id]))
        second_response = self.client.post(reverse('record_copy', args=[self.share.share_id]))

        self.share.refresh_from_db()
        self.assertEqual(first_response.json()['copies_count'], 1)
        self.assertEqual(second_response.json()['copies_count'], 1)
        self.assertEqual(self.share.copies, 1)

        another_client = Client()
        third_response = another_client.post(reverse('record_copy', args=[self.share.share_id]))
        self.share.refresh_from_db()
        self.assertEqual(third_response.json()['copies_count'], 2)
        self.assertEqual(self.share.copies, 2)

    def test_authenticated_user_can_report_share(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('report_share', args=[self.share.share_id]),
            {'reason': '需要管理员核查'},
        )

        self.assertRedirects(response, reverse('share_detail', args=[self.share.share_id]))
        report = Report.objects.get()
        self.assertEqual(report.share, self.share)
        self.assertEqual(report.reporter, self.user)
        self.assertEqual(report.reason, '需要管理员核查')


class InteractionConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.author = User.objects.create_user(username='author', password='password123')
        self.user = User.objects.create_user(username='user', password='password123')
        self.share = Share.objects.create(
            title='并发互动测试',
            strategy_code='[stgy:interaction-concurrency]',
            author=self.author,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )

    def _post_concurrently(self, url, target_state):
        clients = [Client(), Client()]
        for client in clients:
            client.force_login(self.user)
        barrier = Barrier(len(clients))

        def send(client):
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                response = client.post(url, {'target_state': target_state})
                return response.status_code, response.json()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=len(clients)) as executor:
            futures = [executor.submit(send, client) for client in clients]
            return [future.result(timeout=20) for future in futures]

    def _post_like_while_share_changes(self, mutate_share):
        client = Client()
        client.force_login(self.user)
        first_permission_check = Event()
        continue_interaction = Event()
        original_can_view_share = interaction_service.can_view_share

        def pause_after_first_check(user, share):
            allowed = original_can_view_share(user, share)
            if not first_permission_check.is_set():
                first_permission_check.set()
                if not continue_interaction.wait(timeout=10):
                    raise AssertionError('Timed out waiting for concurrent share change.')
            return allowed

        def send_interaction():
            close_old_connections()
            try:
                return client.post(
                    reverse('toggle_like', args=[self.share.share_id]),
                    {'target_state': 'active'},
                )
            finally:
                close_old_connections()

        with (
            patch.object(
                interaction_service,
                'can_view_share',
                side_effect=pause_after_first_check,
            ),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            future = executor.submit(send_interaction)
            try:
                self.assertTrue(first_permission_check.wait(timeout=10))
                mutate_share()
            finally:
                continue_interaction.set()
            return future.result(timeout=20)

    def test_concurrent_duplicate_target_requests_converge(self):
        cases = (
            ('toggle_like', 'likes', 'is_liked', 'likes_count'),
            ('toggle_favorite', 'favorites', 'is_favorited', 'favorites_count'),
        )

        for url_name, relation_name, state_key, count_key in cases:
            url = reverse(url_name, args=[self.share.share_id])
            relation = getattr(self.share, relation_name)
            with self.subTest(endpoint=url_name, target_state='active'):
                relation.clear()
                responses = self._post_concurrently(url, 'active')

                self.assertEqual([status for status, _ in responses], [200, 200])
                self.assertTrue(all(payload[state_key] for _, payload in responses))
                self.assertTrue(all(payload[count_key] == 1 for _, payload in responses))
                self.assertEqual(relation.filter(pk=self.user.pk).count(), 1)

            with self.subTest(endpoint=url_name, target_state='inactive'):
                relation.add(self.user)
                responses = self._post_concurrently(url, 'inactive')

                self.assertEqual([status for status, _ in responses], [200, 200])
                self.assertTrue(all(not payload[state_key] for _, payload in responses))
                self.assertTrue(all(payload[count_key] == 0 for _, payload in responses))
                self.assertFalse(relation.filter(pk=self.user.pk).exists())

    @skipUnless(
        connection.vendor == 'postgresql',
        'PostgreSQL-specific permission race contract',
    )
    def test_concurrent_visibility_change_rolls_back_interaction(self):
        response = self._post_like_while_share_changes(
            lambda: Share.objects.filter(pk=self.share.pk).update(
                visibility=Share.Visibility.PRIVATE,
            ),
        )

        self.share.refresh_from_db()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.share.visibility, Share.Visibility.PRIVATE)
        self.assertFalse(self.share.likes.filter(pk=self.user.pk).exists())

    @skipUnless(
        connection.vendor == 'postgresql',
        'PostgreSQL-specific deletion race contract',
    )
    def test_concurrent_share_deletion_returns_not_found(self):
        share_pk = self.share.pk
        through_model = Share.likes.through

        response = self._post_like_while_share_changes(
            lambda: Share.objects.filter(pk=share_pk).delete(),
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Share.objects.filter(pk=share_pk).exists())
        self.assertFalse(through_model.objects.filter(share_id=share_pk).exists())


class CollectionAndProfileWorkflowTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(username='author', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')
        self.share = Share.objects.create(
            title='合集测试',
            strategy_code='[stgy:collection]',
            author=self.author,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )
        self.collection = Collection.objects.create(title='我的合集', author=self.author)

    def test_user_creation_automatically_creates_profile(self):
        self.assertTrue(UserProfile.objects.filter(user=self.author).exists())
        self.assertEqual(self.author.profile.get_display_name(), 'author')

    def test_author_can_add_own_share_to_collection(self):
        self.client.force_login(self.author)

        response = self.client.post(
            reverse('add_share_to_collection', args=[self.share.share_id]),
            {'collection_id': self.collection.id},
        )

        self.assertRedirects(response, reverse('share_detail', args=[self.share.share_id]))
        item = CollectionItem.objects.get()
        self.assertEqual(item.collection, self.collection)
        self.assertEqual(item.share, self.share)

    def test_other_user_cannot_add_someone_elses_share_to_collection(self):
        other_collection = Collection.objects.create(title='其他合集', author=self.other_user)
        self.client.force_login(self.other_user)

        response = self.client.post(
            reverse('add_share_to_collection', args=[self.share.share_id]),
            {'collection_id': other_collection.id},
        )

        self.assertRedirects(response, reverse('share_detail', args=[self.share.share_id]))
        self.assertFalse(CollectionItem.objects.exists())
