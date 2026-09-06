from unittest.mock import patch
from urllib.parse import quote

from django.contrib.auth.models import User
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .middleware import PreviewPageCacheMiddleware
from .models import Share
from .render_version import RenderVersionCache
from .test_render_version import metadata_response


class PreviewPageCacheMiddlewareTests(SimpleTestCase):
    def test_existing_no_store_contract_is_preserved(self):
        response = HttpResponse(status=405, headers={'Cache-Control': 'no-store'})
        middleware = PreviewPageCacheMiddleware(lambda request: response)
        self.assertEqual(middleware(RequestFactory().post('/health/live/'))['Cache-Control'],
                         'no-store')

    def test_html_cannot_preserve_an_existing_long_cache_lifetime(self):
        response = HttpResponse('<img>', headers={'Cache-Control': 'public, max-age=3600'})
        middleware = PreviewPageCacheMiddleware(lambda request: response)
        result = middleware(RequestFactory().get('/'))
        for directive in ('max-age=0', 'no-cache', 'no-store', 'must-revalidate', 'private'):
            self.assertIn(directive, result['Cache-Control'])
        self.assertNotIn('public', result['Cache-Control'])

    def test_image_cache_policy_is_untouched(self):
        response = HttpResponse(b'image', content_type='image/webp', headers={
            'Cache-Control': 'public, max-age=31536000, immutable',
        })
        middleware = PreviewPageCacheMiddleware(lambda request: response)
        self.assertEqual(middleware(RequestFactory().get('/n/board/code'))['Cache-Control'],
                         'public, max-age=31536000, immutable')


@override_settings(BOARD_RENDER_META_URL='http://renderer.test/render-meta')
class PreviewPageCacheTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(username='preview-cache-author')
        cls.share = Share.objects.create(
            title='自动预览', strategy_code='[stgy:a/b?&+中]', author=cls.author,
            status=Share.Status.APPROVED,
        )
        Share.objects.create(
            title='另一张卡片', strategy_code='[stgy:another]', author=cls.author,
            status=Share.Status.APPROVED,
        )

    def setUp(self):
        self.enterContext(patch('shares.render_version._version_cache', RenderVersionCache()))
        self.clock = self.enterContext(patch('shares.render_version.time.monotonic', return_value=100))
        self.urlopen = self.enterContext(patch('shares.render_version.urllib.request.urlopen'))
        self.enterContext(patch('shares.render_version.logger'))

    def responses(self):
        detail = reverse('share_detail', args=[self.share.share_id])
        return [
            self.client.get(reverse('index')),
            self.client.get(reverse('index'), HTTP_HX_REQUEST='true'),
            self.client.get(reverse('index'), {'partial': 'shares'}),
            self.client.get(reverse('search'), {'q': '自动'}),
            self.client.get(reverse('user_public_profile', args=[self.author.username])),
            self.client.get(detail),
            self.client.get(detail, {'presentation': 'overlay'}, HTTP_HX_REQUEST='true'),
        ]

    def assert_preview_responses(self, responses, *, version):
        url = '/n/board/' + quote(self.share.strategy_code, safe='')
        if version is not None:
            url += '?rv=' + quote(version, safe='')
        for response in responses:
            self.assertEqual(response.status_code, 200)
            self.assertIn('no-store', response['Cache-Control'])
            self.assertIn('private', response['Cache-Control'])
            markup = (response.json()['html'] if response['Content-Type'].startswith('application/json')
                      else response.content.decode())
            self.assertIn(f'src="{url}"', markup)
        self.assertContains(responses[0], 'hx-history="false"')
        self.assertContains(responses[0], '"historyRestoreAsHxRequest":false')
        self.assertContains(responses[-1], 'data-share-detail-overlay')

    def test_all_surfaces_share_one_lookup_and_next_pages_use_changed_version(self):
        self.urlopen.side_effect = [metadata_response('first'), metadata_response('next/+?&中文')]
        self.assert_preview_responses(self.responses(), version='first')
        self.assertEqual(self.urlopen.call_count, 1)
        self.clock.return_value = 160
        self.assert_preview_responses(self.responses(), version='next/+?&中文')
        self.assertEqual(self.urlopen.call_count, 2)

    def test_expired_version_is_removed_on_every_surface_when_metadata_fails(self):
        self.urlopen.side_effect = [metadata_response('first'), TimeoutError()]
        self.assert_preview_responses(self.responses(), version='first')
        self.clock.return_value = 160
        self.assert_preview_responses(self.responses(), version=None)
        self.assertEqual(self.urlopen.call_count, 2)

    def test_history_cache_miss_gets_a_fresh_full_page(self):
        self.urlopen.return_value = metadata_response('history-new')
        # The base template disables HX-Request on HTMX history cache misses.
        response = self.client.get(reverse('index'), HTTP_HX_HISTORY_RESTORE_REQUEST='true')
        self.assertContains(response, '<!DOCTYPE html>')
        self.assertContains(response, '?rv=history-new')
        self.assertIn('no-store', response['Cache-Control'])
