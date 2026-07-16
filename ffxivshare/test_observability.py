import io
import json
import logging
import os
import re
import subprocess
import sys
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import OperationalError
from django.http import HttpResponse, StreamingHttpResponse
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import path, reverse

from .observability import (
    JsonLogFormatter,
    REQUEST_LOGGER_NAME,
    RequestObservabilityMiddleware,
    get_current_request_id,
)
from .urls import urlpatterns as application_urlpatterns


def _plain_view(request, item_id=None):
    return HttpResponse('ok')


def _query_view(request):
    User.objects.exists()
    return HttpResponse('queried', status=201)


def _authenticated_view(request):
    return HttpResponse(str(request.user.pk))


def _exception_view(request):
    raise RuntimeError('sensitive exception text')


def _streaming_view(request):
    return StreamingHttpResponse(iter((b'one', b'two')), content_type='text/plain')


class _ExplodingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        raise RuntimeError('inner-middleware-password123')


urlpatterns = [
    path('__observability__/plain/', _plain_view, name='observability_plain'),
    path(
        '__observability__/items/<str:item_id>/',
        _plain_view,
        name='observability_item',
    ),
    path('__observability__/query/', _query_view, name='observability_query'),
    path('__observability__/authenticated/', _authenticated_view, name='observability_auth'),
    path('__observability__/exception/', _exception_view, name='observability_exception'),
    path('__observability__/stream/', _streaming_view, name='observability_stream'),
] + application_urlpatterns


REQUEST_LOG_FIELDS = {
    'timestamp',
    'level',
    'logger',
    'event',
    'request_id',
    'method',
    'route',
    'view',
    'status',
    'duration_ms',
    'db_queries',
    'response_bytes',
    'user_id',
}


class JsonLogFormatterTests(SimpleTestCase):
    def setUp(self):
        self.formatter = JsonLogFormatter()

    def make_record(self, *, name=REQUEST_LOGGER_NAME, message='ignored', extra=None):
        return logging.getLogger(name).makeRecord(
            name,
            logging.INFO,
            __file__,
            1,
            message,
            (),
            None,
            extra=extra or {},
        )

    def test_request_record_contains_only_the_documented_allowlist(self):
        secret = 'must-never-reach-stdout'
        record = self.make_record(
            message=secret,
            extra={
                'event': 'http.request',
                'request_id': 'a' * 32,
                'method': 'POST',
                'route': 'share/<str:share_id>/',
                'view': 'share_detail',
                'status': 200,
                'duration_ms': 1.25,
                'db_queries': 3,
                'response_bytes': 128,
                'user_id': None,
                'path': f'/shares/{secret}/',
                'query_string': f'password={secret}',
                'body': secret,
                'cookies': secret,
                'headers': secret,
                'ip': secret,
                'sql': f'SELECT {secret}',
                'params': secret,
                'reason': secret,
                'content': secret,
                'password': secret,
            },
        )

        serialized = self.formatter.format(record)
        payload = json.loads(serialized)

        self.assertEqual(set(payload), REQUEST_LOG_FIELDS)
        self.assertNotIn(secret, serialized)
        self.assertEqual(payload['request_id'], 'a' * 32)
        self.assertIsNone(payload['user_id'])
        self.assertRegex(
            payload['timestamp'],
            r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$',
        )

    def test_third_party_message_and_arbitrary_extra_fields_are_not_serialized(self):
        secret = 'password123'
        record = self.make_record(
            name='third.party',
            message=secret,
            extra={
                'event': secret,
                'request_id': secret,
                'route': secret,
                'password': secret,
                'exception_type': 'DatabasePassword',
            },
        )

        serialized = self.formatter.format(record)
        payload = json.loads(serialized)

        self.assertEqual(
            set(payload),
            {'timestamp', 'level', 'logger', 'event'},
        )
        self.assertEqual(payload['event'], 'log')
        self.assertNotIn(secret, serialized)
        self.assertNotIn('DatabasePassword', serialized)

    def test_exception_output_keeps_only_the_exception_class(self):
        secret = 'database-password-in-error'
        try:
            raise OperationalError(secret)
        except OperationalError:
            record = logging.LogRecord(
                'third.party',
                logging.ERROR,
                __file__,
                1,
                secret,
                (),
                sys.exc_info(),
            )

        serialized = self.formatter.format(record)
        payload = json.loads(serialized)

        self.assertEqual(payload['exception_type'], 'OperationalError')
        self.assertNotIn(secret, serialized)

    def test_trusted_operational_events_keep_only_bounded_metadata(self):
        cases = (
            (
                'shares.admin',
                {
                    'event': 'admin.visibility_batch_failed',
                    'batch_number': 2,
                    'batch_size': 100,
                    'target_visibility': 'private',
                    'selected_ids': 'sensitive-id-list',
                },
                {
                    'batch_number': 2,
                    'batch_size': 100,
                    'target_visibility': 'private',
                },
            ),
            (
                'shares.rate_limits',
                {
                    'event': 'rate_limit.cache_failed',
                    'rule': 'login_account',
                    'identity': 'sensitive-account-name',
                },
                {'rule': 'login_account'},
            ),
        )
        for logger_name, extra, expected in cases:
            with self.subTest(logger=logger_name):
                record = self.make_record(
                    name=logger_name,
                    message='sensitive-log-message',
                    extra=extra,
                )
                serialized = self.formatter.format(record)
                payload = json.loads(serialized)

                self.assertEqual(payload['event'], extra['event'])
                for name, value in expected.items():
                    self.assertEqual(payload[name], value)
                self.assertNotIn('sensitive', serialized)


class LoggingConfigurationTests(SimpleTestCase):
    def test_request_middleware_is_outermost_and_access_logs_do_not_duplicate_it(self):
        from django.conf import settings

        self.assertEqual(
            settings.MIDDLEWARE[0],
            'ffxivshare.observability.RequestObservabilityMiddleware',
        )
        self.assertEqual(
            settings.LOGGING['handlers']['json_stdout']['stream'],
            'ext://sys.stdout',
        )
        self.assertEqual(
            settings.LOGGING['loggers']['django.request']['level'],
            'ERROR',
        )
        self.assertEqual(
            settings.LOGGING['loggers']['django.request']['handlers'],
            ['json_stdout' if settings.REQUEST_LOG_ENABLED else 'null'],
        )
        self.assertEqual(
            settings.LOGGING['loggers']['django.server']['handlers'],
            ['null'],
        )

    def test_production_default_and_explicit_disable_select_safe_handlers(self):
        from django.conf import settings

        script = (
            'import json; '
            'from ffxivshare import settings; '
            'print(json.dumps({'
            '"enabled": settings.REQUEST_LOG_ENABLED, '
            '"request": settings.LOGGING["loggers"]["ffxivshare.request"]["handlers"], '
            '"errors": settings.LOGGING["loggers"]["django.request"]["handlers"]'
            '}))'
        )
        base_environment = os.environ.copy()
        base_environment.update({
            'APP_ENV': 'production',
            'DEBUG': 'False',
            'SECRET_KEY': 'production-test-secret-with-more-than-fifty-characters-123456789',
            'ALLOWED_HOSTS': 'example.com,127.0.0.1',
            'DATABASE_ENGINE': 'sqlite',
            'DATABASE_PATH': 'db.sqlite3',
        })
        base_environment.pop('REQUEST_LOG_ENABLED', None)

        def load_config(environment):
            completed = subprocess.run(
                [sys.executable, '-c', script],
                cwd=settings.BASE_DIR,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return json.loads(completed.stdout)

        enabled_config = load_config(base_environment)
        disabled_environment = base_environment | {'REQUEST_LOG_ENABLED': 'False'}
        disabled_config = load_config(disabled_environment)

        self.assertEqual(enabled_config, {
            'enabled': True,
            'request': ['json_stdout'],
            'errors': ['json_stdout'],
        })
        self.assertEqual(disabled_config, {
            'enabled': False,
            'request': ['null'],
            'errors': ['null'],
        })


class HealthProbeTests(TestCase):
    def request_records(self, level=logging.INFO):
        return self.assertLogs(REQUEST_LOGGER_NAME, level=level)

    def test_live_get_and_head_are_no_store_and_do_not_query_the_database(self):
        for method in ('get', 'head'):
            with self.subTest(method=method):
                with self.request_records() as captured:
                    with self.assertNumQueries(0):
                        response = getattr(self.client, method)(reverse('health_live'))

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertEqual(len(captured.records), 1)
                self.assertEqual(captured.records[0].db_queries, 0)
                self.assertIsNone(captured.records[0].user_id)
                if method == 'head':
                    self.assertEqual(response.content, b'')
                    self.assertEqual(captured.records[0].response_bytes, 0)
                else:
                    self.assertEqual(response.json(), {'status': 'ok'})

    def test_ready_get_and_head_execute_exactly_one_probe_query(self):
        for method in ('get', 'head'):
            with self.subTest(method=method):
                with self.request_records() as captured:
                    with self.assertNumQueries(1):
                        response = getattr(self.client, method)(reverse('health_ready'))

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertEqual(len(captured.records), 1)
                self.assertEqual(captured.records[0].db_queries, 1)
                if method == 'head':
                    self.assertEqual(response.content, b'')
                    self.assertEqual(captured.records[0].response_bytes, 0)
                else:
                    self.assertEqual(response.json(), {'status': 'ok'})

    def test_health_probes_reject_other_methods_without_querying(self):
        csrf_client = Client(enforce_csrf_checks=True)
        for route_name in ('health_live', 'health_ready'):
            for method in ('post', 'put', 'delete', 'options'):
                with self.subTest(route=route_name, method=method):
                    with self.request_records(logging.WARNING) as captured:
                        with self.assertNumQueries(0):
                            response = getattr(csrf_client, method)(reverse(route_name))

                    self.assertEqual(response.status_code, 405)
                    self.assertEqual(response.headers['Allow'], 'GET, HEAD')
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
                    self.assertEqual(len(captured.records), 1)

    @override_settings(
        SECURE_SSL_REDIRECT=True,
        ALLOWED_HOSTS=['example.com', '127.0.0.1'],
    )
    def test_only_exact_health_paths_bypass_application_https_redirect(self):
        with self.request_records() as live_logs:
            with self.assertNumQueries(0):
                live_response = self.client.get(
                    reverse('health_live'),
                    HTTP_HOST='127.0.0.1:8000',
                )
        with self.request_records() as ready_logs:
            with self.assertNumQueries(1):
                ready_response = self.client.get(
                    reverse('health_ready'),
                    HTTP_HOST='127.0.0.1:8000',
                )

        self.assertEqual(live_response.status_code, 200)
        self.assertEqual(ready_response.status_code, 200)
        self.assertEqual(live_logs.records[0].status, 200)
        self.assertEqual(ready_logs.records[0].status, 200)

        for path_value in ('/health/live', '/health/live/extra/', '/about/'):
            with self.subTest(path=path_value):
                with self.request_records() as captured:
                    with self.assertNumQueries(0):
                        response = self.client.get(
                            path_value,
                            HTTP_HOST='127.0.0.1:8000',
                        )
                self.assertEqual(response.status_code, 301)
                self.assertEqual(captured.records[0].status, 301)

    def test_ready_failure_is_generic_and_never_logs_the_database_error_text(self):
        secret = 'postgres-password-must-stay-private'
        with patch(
            'ffxivshare.health.connection.cursor',
            side_effect=OperationalError(secret),
        ):
            with self.assertLogs('ffxivshare.health', level='WARNING') as health_logs:
                with self.request_records(logging.ERROR) as request_logs:
                    response = self.client.get(reverse('health_ready'))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.json(), {'status': 'unavailable'})
        self.assertNotIn(secret, response.content.decode())
        self.assertNotIn(secret, '\n'.join(health_logs.output))
        self.assertNotIn(secret, JsonLogFormatter().format(health_logs.records[0]))
        self.assertEqual(health_logs.records[0].exception_type, 'OperationalError')
        self.assertEqual(len(request_logs.records), 1)
        self.assertEqual(request_logs.records[0].status, 503)


@override_settings(ROOT_URLCONF=__name__)
class RequestObservabilityIntegrationTests(TestCase):
    def test_server_id_replaces_forged_id_and_sensitive_request_values_are_absent(self):
        secret = 'sensitive-value-9f34'
        forged_request_id = 'f' * 32

        with self.assertLogs(REQUEST_LOGGER_NAME, level='INFO') as captured:
            with self.assertNumQueries(0):
                response = self.client.post(
                    f'/__observability__/items/{secret}/?reason={secret}',
                    {
                        'password': secret,
                        'content': secret,
                        'reason': secret,
                    },
                    HTTP_X_REQUEST_ID=forged_request_id,
                    HTTP_AUTHORIZATION=f'Bearer {secret}',
                    HTTP_COOKIE=f'custom={secret}',
                    HTTP_X_FORWARDED_FOR='203.0.113.10',
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        response_request_id = response.headers['X-Request-ID']
        self.assertRegex(response_request_id, r'^[0-9a-f]{32}$')
        self.assertNotEqual(response_request_id, forged_request_id)
        self.assertEqual(record.request_id, response_request_id)
        self.assertEqual(record.route, '__observability__/items/<str:item_id>/')
        self.assertEqual(record.view, 'observability_item')
        self.assertEqual(record.method, 'POST')
        self.assertEqual(record.status, 200)
        self.assertEqual(record.db_queries, 0)
        self.assertEqual(record.response_bytes, 2)
        self.assertIsNone(record.user_id)
        self.assertGreaterEqual(record.duration_ms, 0)

        serialized = JsonLogFormatter().format(record)
        self.assertEqual(set(json.loads(serialized)), REQUEST_LOG_FIELDS)
        self.assertNotIn(secret, serialized)
        self.assertNotIn(forged_request_id, serialized)
        self.assertIsNone(get_current_request_id())

    def test_database_queries_are_counted_without_recording_query_content(self):
        with self.assertLogs(REQUEST_LOGGER_NAME, level='INFO') as captured:
            with self.assertNumQueries(1):
                response = self.client.get('/__observability__/query/')

        self.assertEqual(response.status_code, 201)
        self.assertEqual(captured.records[0].db_queries, 1)
        serialized = JsonLogFormatter().format(captured.records[0])
        self.assertNotIn('SELECT', serialized.upper())

    def test_authenticated_user_is_recorded_only_after_the_request_resolves_it(self):
        user = User.objects.create_user(
            username='observability-user',
            password='password123',
        )
        self.client.force_login(user)

        with self.assertLogs(REQUEST_LOGGER_NAME, level='INFO') as captured:
            response = self.client.get('/__observability__/authenticated/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured.records[0].user_id, user.pk)
        self.assertIsNone(get_current_request_id())

    @override_settings(DEBUG=False)
    def test_404_response_is_logged_once_without_the_raw_path(self):
        secret = 'missing-sensitive-path'
        with self.assertLogs(REQUEST_LOGGER_NAME, level='WARNING') as captured:
            response = self.client.get(f'/__observability__/{secret}/?token={secret}')

        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertIsNone(record.route)
        self.assertIsNone(record.view)
        self.assertEqual(record.request_id, response.headers['X-Request-ID'])
        self.assertNotIn(secret, JsonLogFormatter().format(record))
        self.assertIsNone(get_current_request_id())

    @override_settings(DEBUG=False)
    def test_unhandled_exception_is_logged_once_and_context_is_cleared(self):
        client = Client(raise_request_exception=False)

        with self.assertLogs(REQUEST_LOGGER_NAME, level='ERROR') as captured:
            response = client.get('/__observability__/exception/')

        self.assertEqual(response.status_code, 500)
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertEqual(record.exception_type, 'RuntimeError')
        self.assertEqual(record.request_id, response.headers['X-Request-ID'])
        serialized = JsonLogFormatter().format(record)
        self.assertEqual(
            set(json.loads(serialized)),
            REQUEST_LOG_FIELDS | {'exception_type'},
        )
        self.assertNotIn('sensitive exception text', serialized)
        self.assertIsNone(get_current_request_id())

    @override_settings(
        DEBUG=False,
        MIDDLEWARE=[
            'ffxivshare.observability.RequestObservabilityMiddleware',
            'ffxivshare.test_observability._ExplodingMiddleware',
        ],
    )
    def test_inner_middleware_exception_has_a_safe_correlated_error_event(self):
        error_output = io.StringIO()
        error_handler = logging.StreamHandler(error_output)
        error_handler.setFormatter(JsonLogFormatter())
        framework_logger = logging.getLogger('django.request')
        framework_logger.addHandler(error_handler)
        self.addCleanup(framework_logger.removeHandler, error_handler)

        client = Client(raise_request_exception=False)
        with self.assertLogs(REQUEST_LOGGER_NAME, level='ERROR') as request_logs:
            response = client.get('/inner-middleware-secret-path/')

        self.assertEqual(response.status_code, 500)
        self.assertEqual(len(request_logs.records), 1)
        request_record = request_logs.records[0]
        self.assertFalse(hasattr(request_record, 'exception_type'))

        error_lines = error_output.getvalue().splitlines()
        self.assertEqual(len(error_lines), 1)
        error_payload = json.loads(error_lines[0])
        self.assertEqual(error_payload['event'], 'django.request.error')
        self.assertEqual(error_payload['exception_type'], 'RuntimeError')
        self.assertEqual(
            error_payload['request_id'],
            response.headers['X-Request-ID'],
        )
        self.assertEqual(error_payload['request_id'], request_record.request_id)
        self.assertNotIn('password123', error_lines[0])
        self.assertNotIn('secret-path', error_lines[0])
        self.assertIsNone(get_current_request_id())

    def test_streaming_response_is_logged_once_without_consuming_its_body(self):
        with self.assertLogs(REQUEST_LOGGER_NAME, level='INFO') as captured:
            response = self.client.get('/__observability__/stream/')

        self.assertTrue(response.streaming)
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(
            captured.records[0].request_id,
            response.headers['X-Request-ID'],
        )
        self.assertIsNone(captured.records[0].response_bytes)
        self.assertIsNone(get_current_request_id())
        self.assertEqual(b''.join(response.streaming_content), b'onetwo')
        self.assertEqual(len(captured.records), 1)
        self.assertIsNone(get_current_request_id())

    def test_direct_downstream_exception_also_clears_the_context(self):
        request = RequestFactory().get('/not-logged?password=secret')

        def raise_exception(_request):
            raise ValueError('sensitive direct error')

        middleware = RequestObservabilityMiddleware(raise_exception)
        with self.assertLogs(REQUEST_LOGGER_NAME, level='ERROR') as captured:
            with self.assertRaises(ValueError):
                middleware(request)

        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].exception_type, 'ValueError')
        self.assertIsNone(get_current_request_id())

    def test_sequential_requests_never_reuse_request_context(self):
        request_ids = []
        for _ in range(2):
            with self.assertLogs(REQUEST_LOGGER_NAME, level='INFO') as captured:
                response = self.client.get('/__observability__/plain/')
            request_ids.append(response.headers['X-Request-ID'])
            self.assertEqual(captured.records[0].request_id, request_ids[-1])
            self.assertIsNone(get_current_request_id())

        self.assertEqual(len(set(request_ids)), 2)
        self.assertTrue(all(re.fullmatch(r'[0-9a-f]{32}', value) for value in request_ids))
