import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from django.test import SimpleTestCase, override_settings

from . import render_version
from .preview_urls import build_board_preview_url
from .render_version import MAX_META_BYTES, RenderVersionCache


def metadata_response(version='release/a', *, status=200, headers=None, body=None):
    response = BytesIO(body if body is not None else json.dumps({
        'ok': True, 'data': {'renderVersion': version},
    }).encode())
    response.status = status
    response.headers = headers if headers is not None else {
        'Cache-Control': 'public, max-age=60, must-revalidate',
    }
    return response


@override_settings(
    BOARD_RENDER_META_URL='http://renderer.test/render-meta',
    BOARD_RENDER_META_TIMEOUT_SECONDS=1,
)
class RenderVersionTests(SimpleTestCase):
    def setUp(self):
        self.cache = RenderVersionCache()
        self.clock = self.enterContext(patch.object(render_version.time, 'monotonic', return_value=100.0))
        self.urlopen = self.enterContext(patch.object(render_version.urllib.request, 'urlopen'))
        self.enterContext(patch.object(render_version, 'logger'))
        self.enterContext(patch.object(render_version, '_version_cache', self.cache))

    def test_cache_hit_and_refresh_exactly_at_expiry_discovers_changed_version(self):
        self.urlopen.side_effect = [metadata_response('first'), metadata_response('second')]
        self.assertEqual(self.cache.get(), 'first')
        self.clock.return_value = 159.999
        self.assertEqual(self.cache.get(), 'first')
        self.assertEqual(self.urlopen.call_count, 1)
        self.clock.return_value = 160
        self.assertEqual(self.cache.get(), 'second')
        self.assertEqual(self.urlopen.call_count, 2)

    def test_age_and_network_time_are_subtracted_from_sixty_seconds(self):
        def fetch(*args, **kwargs):
            self.clock.return_value = 102
            return metadata_response(headers={'Age': '45', 'Cache-Control': 'max-age=60'})
        self.urlopen.side_effect = fetch
        self.assertEqual(self.cache.get(), 'release/a')
        self.clock.return_value = 114.999
        self.assertEqual(self.cache.get(), 'release/a')
        self.assertEqual(self.urlopen.call_count, 1)
        self.clock.return_value = 115
        self.urlopen.side_effect = None
        self.urlopen.return_value = metadata_response('new')
        self.assertEqual(self.cache.get(), 'new')
        self.assertEqual(self.urlopen.call_count, 2)

    def test_cache_control_can_shorten_but_never_extend_the_limit(self):
        for max_age, expiry in [('3600', 160), ('10', 110), ('"20"', 120)]:
            with self.subTest(max_age=max_age):
                self.clock.return_value = 100
                cache = RenderVersionCache()
                self.urlopen.side_effect = [
                    metadata_response(headers={'Cache-Control': f'public, max-age={max_age}'}),
                    metadata_response('changed'),
                ]
                self.assertEqual(cache.get(), 'release/a')
                self.clock.return_value = expiry - 0.001
                self.assertEqual(cache.get(), 'release/a')
                self.clock.return_value = expiry
                self.assertEqual(cache.get(), 'changed')

    def test_stale_or_unverifiable_freshness_uses_unversioned_url_with_backoff(self):
        headers_cases = [
            {'Age': value} for value in ('60', '61', '9999', '-1', 'bad', 'NaN', '1.5', '')
        ] + [
            {'Cache-Control': value} for value in ('no-store', 'no-cache', 'max-age=0', 'max-age=bad')
        ]
        for headers in headers_cases:
            with self.subTest(headers=headers):
                cache = RenderVersionCache()
                self.urlopen.reset_mock()
                self.urlopen.return_value = metadata_response(headers=headers)
                self.assertIsNone(cache.get())
                self.assertIsNone(cache.get())
                self.assertEqual(self.urlopen.call_count, 1)

    def test_response_already_expired_during_request_is_not_used(self):
        def fetch(*args, **kwargs):
            self.clock.return_value = 102
            return metadata_response(headers={'Age': '59'})
        self.urlopen.side_effect = fetch
        self.assertIsNone(self.cache.get())

    def test_failures_drop_old_version_and_retry_after_short_backoff(self):
        self.urlopen.side_effect = [metadata_response('old'), TimeoutError(), metadata_response('new')]
        self.assertIn('?rv=old', build_board_preview_url('code'))
        self.clock.return_value = 160
        self.assertEqual(build_board_preview_url('code'), '/n/board/code')
        for _ in range(12):
            self.assertEqual(build_board_preview_url('code'), '/n/board/code')
        self.assertEqual(self.urlopen.call_count, 2)
        self.clock.return_value = 165
        self.assertIn('?rv=new', build_board_preview_url('code'))

    def test_transport_failures_do_not_break_preview_urls(self):
        for error in (
            HTTPError('http://renderer.test/render-meta', 404, 'Not deployed', {}, None),
            HTTPError('http://renderer.test/render-meta', 503, 'Unavailable', {}, None),
            URLError('offline'), TimeoutError(), OSError('connection reset'),
        ):
            with self.subTest(error=type(error).__name__):
                self.cache = RenderVersionCache()
                with patch.object(render_version, '_version_cache', self.cache):
                    self.urlopen.side_effect = error
                    self.assertEqual(build_board_preview_url('a/b'), '/n/board/a%2Fb')

    def test_invalid_status_envelope_version_and_body_are_rejected(self):
        cases = [
            {'status': status} for status in (204, 302, 404, 500)
        ] + [
            {'body': body} for body in (b'not json', b'\xff', b'[]', b'null', b'{}',
                b'{"ok":1,"data":{"renderVersion":"x"}}',
                b'{"ok":false,"data":{"renderVersion":"x"}}',
                b'{"ok":true,"data":[]}', b'{"ok":true}',
                b'{"ok":true,"data":{}}', b'x' * (MAX_META_BYTES + 1))
        ] + [
            {'version': value} for value in ('', None, 3, True, [], {}, '\ud800')
        ]
        for kwargs in cases:
            with self.subTest(kwargs=str(kwargs)[:100]):
                self.urlopen.return_value = metadata_response(**kwargs)
                self.assertIsNone(RenderVersionCache().get())

    def test_opaque_version_is_preserved_including_whitespace_and_unicode(self):
        self.urlopen.return_value = metadata_response(' next/+?&=#%中文 ')
        self.assertEqual(
            build_board_preview_url('a/b'),
            '/n/board/a%2Fb?rv=%20next%2F%2B%3F%26%3D%23%25%E4%B8%AD%E6%96%87%20',
        )

    def test_fetch_uses_configured_backend_url_and_short_timeout(self):
        self.urlopen.return_value = metadata_response()
        self.cache.get()
        args, kwargs = self.urlopen.call_args
        self.assertEqual(args[0].full_url, 'http://renderer.test/render-meta')
        self.assertEqual(args[0].get_method(), 'GET')
        self.assertEqual(args[0].get_header('Accept'), 'application/json')
        self.assertEqual(kwargs['timeout'], 1)

    def test_changing_metadata_endpoint_does_not_reuse_another_renderers_version(self):
        self.urlopen.side_effect = [metadata_response('one'), metadata_response('two')]
        self.assertEqual(self.cache.get(), 'one')
        with self.settings(BOARD_RENDER_META_URL='http://another.test/render-meta'):
            self.assertEqual(self.cache.get(), 'two')

    @override_settings(BOARD_RENDER_META_URL='')
    def test_explicitly_disabled_discovery_uses_unversioned_route_without_network(self):
        self.assertEqual(build_board_preview_url('code'), '/n/board/code')
        self.urlopen.assert_not_called()

    @contextmanager
    def simultaneous_callers(self, *, fail=False):
        entered = threading.Event()
        release = threading.Event()
        barrier = threading.Barrier(8)
        all_attempted = threading.Event()
        real_lock = threading.Lock()
        counter_lock = threading.Lock()
        attempts = 0

        class ObservedLock:
            def acquire(self, **kwargs):
                nonlocal attempts
                with counter_lock:
                    attempts += 1
                    if attempts == 8:
                        all_attempted.set()
                return real_lock.acquire(**kwargs)

            def release(self):
                real_lock.release()

        self.cache._lock = ObservedLock()

        def fetch(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError('Test failed to release metadata request')
            if fail:
                raise TimeoutError()
            return metadata_response('new')

        def get():
            barrier.wait(timeout=3)
            return self.cache.get()

        self.urlopen.side_effect = fetch
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(get) for _ in range(8)]
            try:
                self.assertTrue(entered.wait(3))
                self.assertTrue(all_attempted.wait(3))
                yield
            finally:
                release.set()
            self.assertEqual([f.result(timeout=3) for f in futures], [None if fail else 'new'] * 8)
        self.assertEqual(self.urlopen.call_count, 1)

    def test_cold_concurrent_requests_share_one_refresh(self):
        with self.simultaneous_callers():
            pass

    def test_concurrent_expiry_refresh_is_merged(self):
        self.urlopen.return_value = metadata_response('old')
        self.assertEqual(self.cache.get(), 'old')
        self.clock.return_value = 160
        self.urlopen.reset_mock()
        with self.simultaneous_callers():
            pass

    def test_concurrent_failures_are_merged_and_never_return_expired_version(self):
        self.urlopen.return_value = metadata_response('old')
        self.assertEqual(self.cache.get(), 'old')
        self.clock.return_value = 160
        self.urlopen.reset_mock()
        with self.simultaneous_callers(fail=True):
            pass

    def test_wait_for_inflight_refresh_is_bounded(self):
        lock = self.enterContext(patch.object(self.cache, '_lock'))
        lock.acquire.return_value = False
        self.assertIsNone(self.cache.get())
        lock.acquire.assert_called_once_with(timeout=1)
        lock.release.assert_not_called()
        self.urlopen.assert_not_called()
