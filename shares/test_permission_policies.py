from datetime import timedelta

from django.contrib.auth.models import AnonymousUser, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Collection, CollectionItem, Share
from .policies import (
    can_view_collection,
    can_view_share,
    is_moderator,
    public_share_queryset,
    share_api_denial_status,
    viewable_share_queryset,
)


class SharePermissionPolicyTests(TestCase):
    def setUp(self):
        self.anonymous = AnonymousUser()
        self.author = User.objects.create_user(username='author', password='password123')
        self.other = User.objects.create_user(username='other', password='password123')
        self.staff = User.objects.create_user(username='staff', password='password123', is_staff=True)

    def make_share(self, *, visibility, status):
        return Share.objects.create(
            title=f'{visibility}-{status}',
            strategy_code='[stgy:policy]',
            author=self.author,
            visibility=visibility,
            status=status,
        )

    def test_direct_link_share_access_matrix(self):
        expected_for_visitors = {
            (Share.Visibility.PUBLIC, Share.Status.APPROVED): True,
            (Share.Visibility.PUBLIC, Share.Status.PENDING): True,
            (Share.Visibility.PUBLIC, Share.Status.REJECTED): False,
            (Share.Visibility.UNLISTED, Share.Status.APPROVED): True,
            (Share.Visibility.UNLISTED, Share.Status.PENDING): True,
            (Share.Visibility.UNLISTED, Share.Status.REJECTED): False,
            (Share.Visibility.PRIVATE, Share.Status.APPROVED): False,
            (Share.Visibility.PRIVATE, Share.Status.PENDING): False,
            (Share.Visibility.PRIVATE, Share.Status.REJECTED): False,
        }

        for (visibility, status), visitor_can_view in expected_for_visitors.items():
            share = self.make_share(visibility=visibility, status=status)
            for user in (self.anonymous, self.other):
                with self.subTest(visibility=visibility, status=status, user=str(user)):
                    self.assertEqual(can_view_share(user, share), visitor_can_view)
                    self.assertEqual(
                        viewable_share_queryset(user).filter(pk=share.pk).exists(),
                        visitor_can_view,
                    )
            self.assertTrue(can_view_share(self.author, share))
            self.assertTrue(can_view_share(self.staff, share))
            self.assertTrue(
                viewable_share_queryset(self.author).filter(pk=share.pk).exists(),
            )
            self.assertTrue(
                viewable_share_queryset(self.staff).filter(pk=share.pk).exists(),
            )

    def test_only_public_approved_shares_enter_public_querysets(self):
        public = self.make_share(
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )
        self.make_share(
            visibility=Share.Visibility.UNLISTED,
            status=Share.Status.APPROVED,
        )
        self.make_share(
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.PENDING,
        )

        self.assertEqual(list(public_share_queryset()), [public])

    def test_private_collection_is_visible_to_owner_and_moderator(self):
        private = Collection.objects.create(title='私有合集', author=self.author, is_public=False)
        public = Collection.objects.create(title='公开合集', author=self.author, is_public=True)

        self.assertFalse(can_view_collection(self.anonymous, private))
        self.assertFalse(can_view_collection(self.other, private))
        self.assertTrue(can_view_collection(self.author, private))
        self.assertTrue(can_view_collection(self.staff, private))
        self.assertTrue(can_view_collection(self.anonymous, public))

    def test_private_api_denial_remains_forbidden_while_moderated_content_is_hidden(self):
        private = self.make_share(
            visibility=Share.Visibility.PRIVATE,
            status=Share.Status.APPROVED,
        )
        rejected = self.make_share(
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.REJECTED,
        )

        self.assertEqual(share_api_denial_status(private), 403)
        self.assertEqual(share_api_denial_status(rejected), 404)
        self.assertTrue(is_moderator(self.staff))
        self.assertFalse(is_moderator(self.other))


class PermissionEnforcementTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(username='author', password='password123')
        self.other = User.objects.create_user(username='other', password='password123')
        self.staff = User.objects.create_user(username='staff', password='password123', is_staff=True)

    def make_share(self, title, *, visibility, status=Share.Status.APPROVED):
        return Share.objects.create(
            title=title,
            strategy_code=f'[stgy:{title}]',
            author=self.author,
            visibility=visibility,
            status=status,
        )

    def test_hidden_share_cannot_be_read_or_mutated_by_other_user(self):
        private = self.make_share('private', visibility=Share.Visibility.PRIVATE)
        rejected = self.make_share(
            'rejected',
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.REJECTED,
        )
        self.client.force_login(self.other)

        self.assertEqual(
            self.client.get(reverse('share_detail', args=[private.share_id])).status_code,
            302,
        )
        self.assertEqual(
            self.client.get(reverse('get_share_code', args=[private.share_id])).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse('get_share_code', args=[rejected.share_id])).status_code,
            404,
        )

        for url_name, method in (
            ('report_share', self.client.get),
            ('record_copy', self.client.post),
            ('toggle_like', self.client.post),
            ('toggle_favorite', self.client.post),
        ):
            with self.subTest(endpoint=url_name):
                response = method(reverse(url_name, args=[private.share_id]))
                self.assertEqual(response.status_code, 404)

        private.refresh_from_db()
        self.assertEqual(private.copies, 0)
        self.assertFalse(private.likes.filter(pk=self.other.pk).exists())
        self.assertFalse(private.favorites.filter(pk=self.other.pk).exists())

    def test_unlisted_share_uses_the_same_policy_across_endpoints(self):
        share = self.make_share('unlisted', visibility=Share.Visibility.UNLISTED)
        self.client.force_login(self.other)

        detail = self.client.get(reverse('share_detail', args=[share.share_id]))
        code = self.client.get(reverse('get_share_code', args=[share.share_id]))
        report = self.client.get(reverse('report_share', args=[share.share_id]))
        copy = self.client.post(reverse('record_copy', args=[share.share_id]))
        like = self.client.post(reverse('toggle_like', args=[share.share_id]))
        favorite = self.client.post(reverse('toggle_favorite', args=[share.share_id]))

        self.assertEqual(detail.status_code, 200)
        self.assertEqual(code.status_code, 200)
        self.assertEqual(report.status_code, 200)
        self.assertEqual(copy.status_code, 200)
        self.assertEqual(like.status_code, 200)
        self.assertEqual(favorite.status_code, 200)

    def test_collection_page_and_api_filter_items_with_same_policy(self):
        public = self.make_share('public', visibility=Share.Visibility.PUBLIC)
        unlisted = self.make_share('unlisted', visibility=Share.Visibility.UNLISTED)
        pending = self.make_share(
            'pending',
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.PENDING,
        )
        rejected = self.make_share(
            'rejected',
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.REJECTED,
        )
        private = self.make_share('private', visibility=Share.Visibility.PRIVATE)
        collection = Collection.objects.create(title='公开合集', author=self.author, is_public=True)
        for order, share in enumerate((public, unlisted, pending, rejected, private), start=1):
            CollectionItem.objects.create(collection=collection, share=share, order=order)

        page = self.client.get(reverse('collection_detail', args=[collection.pk]))
        api = self.client.get(reverse('get_collection_codes', args=[collection.pk]))

        self.assertContains(page, public.title)
        self.assertContains(page, unlisted.title)
        self.assertContains(page, pending.title)
        self.assertNotContains(page, rejected.title)
        self.assertNotContains(page, private.title)
        self.assertEqual(api.json(), [
            {'title': public.title, 'code': public.strategy_code},
            {'title': unlisted.title, 'code': unlisted.strategy_code},
            {'title': pending.title, 'code': pending.strategy_code},
        ])

    def test_share_detail_collection_preview_filters_hidden_items_and_count(self):
        current = self.make_share('current', visibility=Share.Visibility.PUBLIC)
        private = self.make_share('related private', visibility=Share.Visibility.PRIVATE)
        rejected = self.make_share(
            'related rejected',
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.REJECTED,
        )
        visible = [
            current,
            self.make_share('related visible 2', visibility=Share.Visibility.PUBLIC),
            self.make_share('related visible 3', visibility=Share.Visibility.UNLISTED),
            self.make_share('related visible 4', visibility=Share.Visibility.PUBLIC),
            self.make_share('related visible 5', visibility=Share.Visibility.PUBLIC),
        ]
        collection = Collection.objects.create(
            title='related collection',
            author=self.author,
            is_public=True,
        )
        ordered_shares = [
            current,
            private,
            rejected,
            *visible[1:],
        ]
        for order, share in enumerate(ordered_shares, start=1):
            CollectionItem.objects.create(
                collection=collection,
                share=share,
                order=order,
            )
        newer_collection = Collection.objects.create(
            title='newer related collection',
            author=self.author,
            is_public=True,
        )
        CollectionItem.objects.create(
            collection=newer_collection,
            share=current,
            order=1,
        )
        Collection.objects.filter(pk=collection.pk).update(
            updated_at=timezone.now() - timedelta(days=1),
        )

        response = self.client.get(reverse('share_detail', args=[current.share_id]))

        self.assertEqual(response.status_code, 200)
        summaries = response.context['related_collections']
        self.assertEqual(
            [summary.collection for summary in summaries],
            [newer_collection, collection],
        )
        summary = next(
            summary
            for summary in summaries
            if summary.collection == collection
        )
        self.assertEqual(summary.collection, collection)
        self.assertEqual(summary.visible_item_count, 5)
        self.assertEqual(
            [item.share for item in summary.visible_items],
            visible,
        )
        self.assertNotContains(response, private.title)
        self.assertNotContains(response, rejected.title)
        self.assertNotContains(response, '查看全部')

        self.client.force_login(self.author)
        owner_response = self.client.get(
            reverse('share_detail', args=[current.share_id]),
        )
        owner_summary = next(
            summary
            for summary in owner_response.context['related_collections']
            if summary.collection == collection
        )
        self.assertEqual(owner_summary.visible_item_count, 7)
        self.assertEqual(len(owner_summary.visible_items), 5)
        self.assertContains(owner_response, private.title)
        self.assertContains(owner_response, rejected.title)
        self.assertContains(owner_response, '查看全部 7 个内容')

    def test_staff_can_inspect_private_collection_and_share_api(self):
        private_share = self.make_share('private', visibility=Share.Visibility.PRIVATE)
        private_collection = Collection.objects.create(
            title='私有合集',
            author=self.author,
            is_public=False,
        )
        CollectionItem.objects.create(collection=private_collection, share=private_share)
        self.client.force_login(self.staff)

        page = self.client.get(reverse('collection_detail', args=[private_collection.pk]))
        collection_api = self.client.get(reverse('get_collection_codes', args=[private_collection.pk]))
        share_api = self.client.get(reverse('get_share_code', args=[private_share.share_id]))

        self.assertEqual(page.status_code, 200)
        self.assertEqual(collection_api.status_code, 200)
        self.assertEqual(share_api.status_code, 200)
