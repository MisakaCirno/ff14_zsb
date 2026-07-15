from unittest.mock import MagicMock, patch

from django.test import RequestFactory, SimpleTestCase

from ffxivshare.urls import proxy_view


class RendererDevelopmentProxyTests(SimpleTestCase):
    @patch('ffxivshare.urls.urllib.request.urlopen')
    def test_decoded_renderer_path_is_reencoded_before_forwarding(self, urlopen):
        upstream = MagicMock()
        upstream.read.return_value = b'png'
        upstream.status = 200
        upstream.headers.get.return_value = 'image/png'
        upstream.__enter__.return_value = upstream
        urlopen.return_value = upstream
        request = RequestFactory().get('/n/board/example', {'size': 'large'})

        response = proxy_view(
            request,
            'board/[stgy:a/../b?c#d%e&f"g]',
        )

        self.assertEqual(response.status_code, 200)
        forwarded_request = urlopen.call_args.args[0]
        self.assertEqual(
            forwarded_request.full_url,
            (
                'https://ff14hub.com/n/board/'
                '%5Bstgy%3Aa%2F..%2Fb%3Fc%23d%25e%26f%22g%5D?size=large'
            ),
        )

    @patch('ffxivshare.urls.urllib.request.urlopen')
    def test_dot_segments_are_rejected_before_forwarding(self, urlopen):
        request = RequestFactory().get('/n/../admin')

        response = proxy_view(request, '../admin')

        self.assertEqual(response.status_code, 400)
        urlopen.assert_not_called()
