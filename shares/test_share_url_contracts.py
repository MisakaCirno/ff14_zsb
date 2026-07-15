from urllib.parse import quote

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Share


@override_settings(ALLOWED_HOSTS=['testserver'])
class ShareCanonicalUrlContractTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(
            username='canonical-author',
            password='password123',
        )
        self.share = Share.objects.create(
            title='标准分享链接',
            strategy_code='[stgy:canonical-url]',
            author=self.author,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )
        self.canonical_path = f'/s/{self.share.share_id}'
        self.canonical_url = f'https://testserver{self.canonical_path}'

    def test_reverse_and_model_absolute_url_use_the_suffix_free_path(self):
        self.assertEqual(
            reverse('share_detail', args=[self.share.share_id]),
            self.canonical_path,
        )
        self.assertEqual(self.share.get_absolute_url(), self.canonical_path)

    def test_historical_suffix_routes_remain_accessible_but_emit_canonical_urls(self):
        historical_paths = (
            self.canonical_path,
            f'{self.canonical_path}/',
            f'{self.canonical_path}/legacy-title/nested',
        )

        for path in historical_paths:
            with self.subTest(path=path):
                response = self.client.get(path, {'legacy_query': '1'}, secure=True)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['canonical_share_path'], self.canonical_path)
                self.assertEqual(response.context['canonical_share_url'], self.canonical_url)
                self.assertContains(
                    response,
                    f'<link rel="canonical" href="{self.canonical_url}">',
                    html=True,
                )
                self.assertContains(
                    response,
                    f'data-share-url="{self.canonical_url}"',
                )
                self.assertContains(
                    response,
                    f'data-share-url-input value="{self.canonical_url}"',
                )

        legacy_response = self.client.get(
            f'{self.canonical_path}/legacy-title/nested',
            secure=True,
        )
        self.assertNotContains(legacy_response, 'legacy-title/nested')

    def test_page_return_links_use_the_canonical_path_from_a_historical_url(self):
        historical_path = f'{self.canonical_path}/legacy-title'
        encoded_next = quote(self.canonical_path, safe='')

        anonymous_response = self.client.get(historical_path, secure=True)
        login_target = f'{reverse("login")}?next={encoded_next}'
        self.assertContains(anonymous_response, login_target, count=2)

        self.client.force_login(self.author)
        author_response = self.client.get(historical_path, secure=True)
        collection_target = f'{reverse("create_collection")}?next={encoded_next}'
        self.assertContains(author_response, collection_target, count=1)
