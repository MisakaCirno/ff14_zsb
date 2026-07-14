from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from .models import Collection, CollectionItem, Share, UserProfile


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
        category=Share.Category.ENTERTAINMENT,
        is_spoiler=False,
        is_nsfw=False,
        views=0,
    ):
        return Share.objects.create(
            title=title,
            strategy_code=code,
            author=author if author is not None else self.author,
            visibility=visibility,
            status=status,
            category=category,
            is_spoiler=is_spoiler,
            is_nsfw=is_nsfw,
            views=views,
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

    def test_text_search_applies_shared_content_filters(self):
        visible = self.create_share(
            title='filter-keyword visible',
            category=Share.Category.COMBAT,
        )
        self.create_share(
            title='filter-keyword entertainment',
            category=Share.Category.ENTERTAINMENT,
        )
        self.create_share(
            title='filter-keyword spoiler',
            category=Share.Category.COMBAT,
            is_spoiler=True,
        )
        self.create_share(
            title='filter-keyword nsfw',
            category=Share.Category.COMBAT,
            is_nsfw=True,
        )

        response = self.client.get(reverse('search'), {
            'q': 'filter-keyword',
            'category': Share.Category.COMBAT,
            'hide_spoiler': 'on',
            'hide_nsfw': 'on',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['shares'].object_list), [visible])
        self.assertEqual(response.context['current_category'], Share.Category.COMBAT)
        self.assertTrue(response.context['hide_spoiler'])
        self.assertTrue(response.context['hide_nsfw'])

    def test_text_search_applies_shared_sorting(self):
        quiet = self.create_share(title='sort-keyword quiet', views=1)
        popular = self.create_share(title='sort-keyword popular', views=20)

        response = self.client.get(reverse('search'), {
            'q': 'sort-keyword',
            'sort': 'views',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(response.context['shares'].object_list),
            [popular, quiet],
        )
        self.assertEqual(response.context['sort_by'], 'views')

    def test_search_filter_controls_preserve_search_state(self):
        self.create_share(title='state & keyword result')

        response = self.client.get(reverse('search'), {
            'q': 'state & keyword',
            'category': Share.Category.COMBAT,
            'hide_spoiler': 'on',
            'feed': UserProfile.HomeFeedMode.PAGINATED,
        })
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            '?q=state+%26+keyword&amp;category=entertainment&amp;'
            'hide_spoiler=on&amp;feed=paginated',
            content,
        )
        self.assertIn(
            '?q=state+%26+keyword&amp;category=combat&amp;hide_spoiler=on&amp;'
            'feed=paginated&amp;sort=likes',
            content,
        )
        self.assertIn('method="get" action="/search/"', content)
        self.assertIn('name="q" value="state &amp; keyword"', content)

    def test_invalid_search_browse_options_are_normalized(self):
        self.create_share(title='normalized-keyword result')

        response = self.client.get(reverse('search'), {
            'q': 'normalized-keyword',
            'category': 'unknown',
            'sort': 'unknown',
        })

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['current_category'])
        self.assertEqual(response.context['sort_by'], 'latest')

    def test_search_pagination_preserves_encoded_query_and_feed_mode(self):
        for index in range(13):
            self.create_share(title=f'pagination & keyword {index}')

        response = self.client.get(reverse('search'), {
            'q': 'pagination & keyword',
            'feed': UserProfile.HomeFeedMode.PAGINATED,
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'aria-label="首页分享分页"')
        self.assertContains(
            response,
            '?q=pagination+%26+keyword&amp;feed=paginated&amp;page=2',
        )

    def test_public_profile_pagination_preserves_query_parameters(self):
        for index in range(13):
            self.create_share(title=f'profile pagination {index}')

        response = self.client.get(
            reverse('user_public_profile', args=[self.author.username]),
            {'tab': 'shares', 'source': 'profile & link'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'aria-label="用户公开分享分页"')
        self.assertContains(
            response,
            '?tab=shares&amp;source=profile+%26+link&amp;page=2',
        )
        self.assertContains(
            response,
            '?tab=collections&amp;source=profile+%26+link',
        )
        self.assertContains(response, 'data-share-card')
        self.assertContains(response, 'data-copy-strategy')
        self.assertNotContains(response, '?fragment=card')

    def test_public_profile_renders_selected_server_tab(self):
        share = self.create_share(title='profile selected share')
        collection = Collection.objects.create(
            title='profile selected collection',
            author=self.author,
            is_public=True,
        )
        CollectionItem.objects.create(collection=collection, share=share, order=1)
        url = reverse('user_public_profile', args=[self.author.username])

        shares_response = self.client.get(url, {'tab': 'shares'})
        self.assertEqual(shares_response.status_code, 200)
        self.assertEqual(shares_response.context['current_tab'], 'shares')
        self.assertContains(shares_response, 'aria-current="page"', count=1)
        self.assertContains(shares_response, 'data-public-profile-shares')
        self.assertContains(shares_response, share.title)
        self.assertNotContains(shares_response, 'data-public-profile-collections')
        self.assertNotContains(shares_response, collection.title)

        collections_response = self.client.get(url, {'tab': 'collections'})
        self.assertEqual(collections_response.status_code, 200)
        self.assertEqual(collections_response.context['current_tab'], 'collections')
        self.assertContains(collections_response, 'aria-current="page"', count=1)
        self.assertContains(collections_response, 'data-public-profile-collections')
        self.assertContains(collections_response, 'data-public-collection')
        self.assertContains(collections_response, collection.title)
        self.assertContains(collections_response, '1 个内容')
        self.assertNotContains(collections_response, 'data-public-profile-shares')
        self.assertNotContains(collections_response, 'data-share-card')

    def test_public_profile_invalid_tab_falls_back_to_shares(self):
        visible = self.create_share(title='profile fallback share')

        response = self.client.get(
            reverse('user_public_profile', args=[self.author.username]),
            {'tab': 'unknown'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['current_tab'], 'shares')
        self.assertContains(response, visible.title)
        self.assertContains(response, 'data-public-profile-shares')
        self.assertNotContains(response, 'data-public-profile-collections')

    def test_public_profile_only_exposes_public_data_to_every_viewer(self):
        visible = self.create_share(title='profile visible share')
        self.create_share(
            title='profile unlisted share',
            visibility=Share.Visibility.UNLISTED,
        )
        self.create_share(
            title='profile private share',
            visibility=Share.Visibility.PRIVATE,
        )
        self.create_share(
            title='profile pending share',
            status=Share.Status.PENDING,
        )
        self.create_share(
            title='profile rejected share',
            status=Share.Status.REJECTED,
        )
        public_collection = Collection.objects.create(
            title='profile public collection',
            author=self.author,
            is_public=True,
        )
        Collection.objects.create(
            title='profile private collection',
            author=self.author,
            is_public=False,
        )
        url = reverse('user_public_profile', args=[self.author.username])

        for viewer in (None, self.author, self.admin):
            with self.subTest(viewer=getattr(viewer, 'username', 'anonymous')):
                if viewer is None:
                    self.client.logout()
                else:
                    self.client.force_login(viewer)

                shares_response = self.client.get(url, {'tab': 'shares'})
                self.assertEqual(
                    list(shares_response.context['shares'].object_list),
                    [visible],
                )

                collections_response = self.client.get(url, {'tab': 'collections'})
                self.assertEqual(
                    list(collections_response.context['collections']),
                    [public_collection],
                )

    def test_my_interaction_tabs_hide_shares_that_are_no_longer_viewable(self):
        visible = self.create_share(title='interaction visible')
        unlisted = self.create_share(
            title='interaction unlisted',
            visibility=Share.Visibility.UNLISTED,
        )
        pending = self.create_share(
            title='interaction pending',
            status=Share.Status.PENDING,
        )
        private = self.create_share(
            title='interaction private',
            visibility=Share.Visibility.PRIVATE,
            code='[stgy:private-interaction]',
        )
        rejected = self.create_share(
            title='interaction rejected',
            status=Share.Status.REJECTED,
            code='[stgy:rejected-interaction]',
        )
        interacted = [visible, unlisted, pending, private, rejected]
        self.other_user.liked_shares.add(*interacted)
        self.other_user.favorited_shares.add(*interacted)
        self.client.force_login(self.other_user)

        for tab in ('likes', 'favorites'):
            with self.subTest(tab=tab):
                response = self.client.get(reverse('my_shares'), {'tab': tab})

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['current_tab'], tab)
                self.assertCountEqual(
                    [share.pk for share in response.context['shares']],
                    [visible.pk, unlisted.pk, pending.pk],
                )
                self.assertNotContains(response, private.title)
                self.assertNotContains(response, rejected.title)
                self.assertNotContains(response, private.strategy_code)
                self.assertNotContains(response, rejected.strategy_code)

        self.assertTrue(private.likes.filter(pk=self.other_user.pk).exists())
        self.assertTrue(private.favorites.filter(pk=self.other_user.pk).exists())
        self.assertTrue(rejected.likes.filter(pk=self.other_user.pk).exists())
        self.assertTrue(rejected.favorites.filter(pk=self.other_user.pk).exists())

    def test_my_content_invalid_tab_falls_back_to_owned_shares(self):
        owned = self.create_share(title='owned fallback share')
        external = self.create_share(
            title='external fallback share',
            author=self.other_user,
        )
        self.author.liked_shares.add(external)
        self.client.force_login(self.author)

        response = self.client.get(reverse('my_shares'), {'tab': 'unknown'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['current_tab'], 'my_shares')
        self.assertEqual(list(response.context['shares'].object_list), [owned])
        self.assertContains(response, owned.title)
        self.assertNotContains(response, external.title)

    def test_hx_text_search_returns_cards_only(self):
        visible = self.create_share(title='局部搜索结果')

        response = self.client.get(
            reverse('search'),
            {'q': '局部搜索'},
            HTTP_HX_REQUEST='true',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, visible.title)
        self.assertNotContains(response, '<!DOCTYPE html>')
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertIn('HX-Request', response.headers['Vary'])
        self.assertIn('Cookie', response.headers['Vary'])

    def test_hx_search_redirects_use_full_page_navigation(self):
        share = self.create_share(visibility=Share.Visibility.UNLISTED)
        cases = (
            ({}, reverse('index')),
            ({'q': share.share_id}, reverse('share_detail', args=[share.share_id])),
            ({'q': 'x' * 201}, reverse('index')),
        )

        for params, expected_url in cases:
            with self.subTest(params=params):
                response = self.client.get(
                    reverse('search'),
                    params,
                    HTTP_HX_REQUEST='true',
                )
                self.assertEqual(response.status_code, 204)
                self.assertEqual(response.headers['HX-Redirect'], expected_url)
                self.assertNotIn('Location', response.headers)
                self.assertIn('no-store', response.headers['Cache-Control'])


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
        self.assertEqual(anonymous_response.json(), {'error': 'Permission denied'})
        self.assert_private_json_response(anonymous_response)

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
        self.assertEqual(anonymous_response.json(), {'error': 'Permission denied'})
        self.assert_private_json_response(anonymous_response)

        self.client.force_login(self.author)
        owner_response = self.client.get(reverse('get_collection_codes', args=[collection.id]))
        self.assertEqual(owner_response.status_code, 200)
        self.assertEqual(owner_response.json(), [
            {'title': '合集分享', 'code': '[stgy:collection]'},
        ])
