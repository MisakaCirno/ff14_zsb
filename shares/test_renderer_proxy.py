from unittest.mock import MagicMock, patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from ffxivshare.urls import proxy_view


@override_settings(RENDERER_PROXY_TIMEOUT_SECONDS=5)
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
        self.assertEqual(urlopen.call_args.kwargs, {'timeout': 5})
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

    @override_settings(RENDERER_PROXY_MAX_BYTES=3)
    @patch('ffxivshare.urls.urllib.request.urlopen')
    def test_oversized_upstream_response_is_rejected(self, urlopen):
        upstream = MagicMock()
        upstream.read.return_value = b'abcd'
        upstream.status = 200
        upstream.headers.get.return_value = 'image/png'
        upstream.__enter__.return_value = upstream
        urlopen.return_value = upstream

        response = proxy_view(RequestFactory().get('/n/asset.png'), 'asset.png')

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.content,
            b'Renderer response exceeded the size limit.',
        )
        upstream.read.assert_called_once_with(4)

    @patch('ffxivshare.urls.urllib.request.urlopen')
    def test_upstream_exception_details_are_not_exposed(self, urlopen):
        urlopen.side_effect = OSError('secret host and filesystem details')

        response = proxy_view(RequestFactory().get('/n/asset.js'), 'asset.js')

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.content, b'Renderer proxy request failed.')
        self.assertNotIn(b'secret', response.content)

    @patch('ffxivshare.urls.urllib.request.urlopen')
    def test_head_is_forwarded_without_reading_the_body(self, urlopen):
        upstream = MagicMock()
        upstream.status = 200
        upstream.headers.get.return_value = 'text/css'
        upstream.__enter__.return_value = upstream
        urlopen.return_value = upstream

        response = proxy_view(RequestFactory().head('/n/asset.css'), 'asset.css')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'')
        upstream.read.assert_not_called()
        forwarded_request = urlopen.call_args.args[0]
        self.assertEqual(forwarded_request.method, 'HEAD')

    @patch('ffxivshare.urls.urllib.request.urlopen')
    def test_state_changing_methods_are_rejected(self, urlopen):
        response = proxy_view(RequestFactory().post('/n/asset.js'), 'asset.js')

        self.assertEqual(response.status_code, 405)
        urlopen.assert_not_called()
