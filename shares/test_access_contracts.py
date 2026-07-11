from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from .models import Collection, CollectionItem, Share


class ShareAccessContractTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(username='author', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')
        self.admin = User.objects.create_user(username='admin', password='password123', is_staff=True)

    def create_share(
        self,
        *,
        title='测试分享',
        visibility=Share.Visibility.PUBLIC,
        status=Share.Status.APPROVED,
        author=None,
        code='[stgy:test-code]',
    ):
        return Share.objects.create(
            title=title,
            strategy_code=code,
            author=author if author is not None else self.author,
            visibility=visibility,
            status=status,
        )

    def test_public_approved_share_is_visible_anonymously(self):
        share = self.create_share()

        response = self.client.get(reverse('share_detail', args=[share.share_id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['share'], share)

    def test_unlisted_approved_share_is_visible_by_direct_link(self):
        share = self.create_share(visibility=Share.Visibility.UNLISTED)

        response = self.client.get(reverse('share_detail', args=[share.share_id]))

        self.assertEqual(response.status_code, 200)

    def test_private_share_redirects_anonymous_user(self):
        share = self.create_share(visibility=Share.Visibility.PRIVATE)

        response = self.client.get(reverse('share_detail', args=[share.share_id]))

        self.assertRedirects(response, reverse('index'))

    def test_rejected_public_share_redirects_anonymous_user(self):
        share = self.create_share(status=Share.Status.REJECTED)

        response = self.client.get(reverse('share_detail', args=[share.share_id]))

        self.assertRedirects(response, reverse('index'))

    def test_pending_public_share_is_visible_by_direct_link(self):
        share = self.create_share(status=Share.Status.PENDING)

        response = self.client.get(reverse('share_detail', args=[share.share_id]))

        self.assertEqual(response.status_code, 200)

    def test_private_share_is_visible_to_author_and_staff(self):
        share = self.create_share(visibility=Share.Visibility.PRIVATE)

        for user in (self.author, self.admin):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(reverse('share_detail', args=[share.share_id]))
                self.assertEqual(response.status_code, 200)
                self.client.logout()

    def test_exact_unlisted_share_id_search_redirects_to_detail(self):
        share = self.create_share(visibility=Share.Visibility.UNLISTED)

        response = self.client.get(reverse('search'), {'q': share.share_id})

        self.assertRedirects(response, reverse('share_detail', args=[share.share_id]))

    def test_text_search_only_returns_public_approved_shares(self):
        visible = self.create_share(title='共同关键词 公开')
        self.create_share(title='共同关键词 隐藏', visibility=Share.Visibility.UNLISTED)
        self.create_share(title='共同关键词 待审', status=Share.Status.PENDING)

        response = self.client.get(reverse('search'), {'q': '共同关键词'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['shares'].object_list), [visible])


class OverlayApiContractTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(username='author', password='password123')

    def create_share(
        self,
        *,
        title,
        code,
        visibility=Share.Visibility.PUBLIC,
        status=Share.Status.APPROVED,
    ):
        return Share.objects.create(
            title=title,
            strategy_code=code,
            author=self.author,
            visibility=visibility,
            status=status,
        )

    def assert_private_json_response(self, response):
        self.assertTrue(response.headers['Content-Type'].startswith('application/json'))
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertIn('private', response.headers['Cache-Control'])
        self.assertIn('Cookie', response.headers['Vary'])

    def test_public_share_code_api_keeps_overlay_response_shape(self):
        share = self.create_share(title='公开分享', code='[stgy:public]')

        response = self.client.get(reverse('get_share_code', args=[share.share_id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{
            'title': '公开分享',
            'code': '[stgy:public]',
        }])

    def test_overlay_api_paths_remain_stable(self):
        self.assertEqual(
            reverse('get_share_code', args=['abc123']),
            '/api/share/abc123/code/',
        )
        self.assertEqual(
            reverse('get_collection_codes', args=[42]),
            '/api/collection/42/codes/',
        )

    def test_overlay_api_errors_keep_json_shape(self):
        missing_share = self.client.get(reverse('get_share_code', args=['missing']))
        missing_collection = self.client.get(reverse('get_collection_codes', args=[999]))
        rejected_share = self.create_share(
            title='拒绝分享',
            code='[stgy:rejected]',
            status=Share.Status.REJECTED,
        )
        rejected_response = self.client.get(
            reverse('get_share_code', args=[rejected_share.share_id]),
        )

        self.assertEqual(missing_share.status_code, 404)
        self.assertEqual(missing_share.json(), {'error': 'Share not found'})
        self.assertEqual(missing_collection.status_code, 404)
        self.assertEqual(missing_collection.json(), {'error': 'Collection not found'})
        self.assertEqual(rejected_response.status_code, 404)
        self.assertEqual(rejected_response.json(), {'error': 'Share not found'})
        self.assert_private_json_response(missing_share)
        self.assert_private_json_response(missing_collection)
        self.assert_private_json_response(rejected_response)

    def test_overlay_api_only_allows_safe_reads_and_is_not_cacheable(self):
        share = self.create_share(title='公开分享', code='[stgy:public]')
        collection = Collection.objects.create(
            title='公开合集',
            author=self.author,
            is_public=True,
        )
        urls = (
            reverse('get_share_code', args=[share.share_id]),
            reverse('get_collection_codes', args=[collection.id]),
        )

        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assert_private_json_response(response)
                head_response = self.client.head(url)
                self.assertEqual(head_response.status_code, 200)
                self.assertEqual(head_response.content, b'')
                self.assert_private_json_response(head_response)

                csrf_client = Client(enforce_csrf_checks=True)
                for method in ('post', 'put', 'patch', 'delete', 'options', 'trace'):
                    with self.subTest(url=url, method=method):
                        method_response = getattr(csrf_client, method)(url)
                        self.assertEqual(method_response.status_code, 405)
                        self.assertEqual(method_response.json(), {
                            'error': 'Method not allowed',
                        })
                        self.assertEqual(method_response.headers['Allow'], 'GET, HEAD')
                        self.assert_private_json_response(method_response)

    def test_private_share_code_api_requires_author(self):
        share = self.create_share(
            title='私有分享',
            code='[stgy:private]',
            visibility=Share.Visibility.PRIVATE,
        )

        anonymous_response = self.client.get(reverse('get_share_code', args=[share.share_id]))
        self.assertEqual(anonymous_response.status_code, 403)

        self.client.force_login(self.author)
        author_response = self.client.get(reverse('get_share_code', args=[share.share_id]))
        self.assertEqual(author_response.status_code, 200)
        self.assertEqual(author_response.json()[0]['code'], '[stgy:private]')

    def test_public_collection_code_api_keeps_order_and_filters_private_share(self):
        public_share = self.create_share(title='公开分享', code='[stgy:public]')
        unlisted_share = self.create_share(
            title='链接分享',
            code='[stgy:unlisted]',
            visibility=Share.Visibility.UNLISTED,
        )
        private_share = self.create_share(
            title='私有分享',
            code='[stgy:private]',
            visibility=Share.Visibility.PRIVATE,
        )
        collection = Collection.objects.create(title='公开合集', author=self.author, is_public=True)
        CollectionItem.objects.create(collection=collection, share=public_share, order=1)
        CollectionItem.objects.create(collection=collection, share=unlisted_share, order=2)
        CollectionItem.objects.create(collection=collection, share=private_share, order=3)

        response = self.client.get(reverse('get_collection_codes', args=[collection.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [
            {'title': '公开分享', 'code': '[stgy:public]'},
            {'title': '链接分享', 'code': '[stgy:unlisted]'},
        ])

    def test_private_collection_code_api_requires_owner(self):
        share = self.create_share(title='合集分享', code='[stgy:collection]')
        collection = Collection.objects.create(title='私有合集', author=self.author, is_public=False)
        CollectionItem.objects.create(collection=collection, share=share, order=1)

        anonymous_response = self.client.get(reverse('get_collection_codes', args=[collection.id]))
        self.assertEqual(anonymous_response.status_code, 403)

        self.client.force_login(self.author)
        owner_response = self.client.get(reverse('get_collection_codes', args=[collection.id]))
        self.assertEqual(owner_response.status_code, 200)
        self.assertEqual(owner_response.json(), [
            {'title': '合集分享', 'code': '[stgy:collection]'},
        ])
