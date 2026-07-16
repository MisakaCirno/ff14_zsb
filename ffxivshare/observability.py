"""Safe, structured request observability for the Django application."""

from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import re
import time
from uuid import uuid4

from django.db import connection
from django.utils.functional import empty


REQUEST_LOGGER_NAME = 'ffxivshare.request'
REQUEST_EVENT = 'http.request'

_current_request_id = ContextVar('ffxivshare_request_id', default=None)
_exception_type_pattern = re.compile(r'^[A-Za-z_][A-Za-z0-9_.]{0,127}$')
_request_id_pattern = re.compile(r'^[0-9a-f]{32}$')
_trusted_events = {
    REQUEST_LOGGER_NAME: frozenset({REQUEST_EVENT}),
    'ffxivshare.health': frozenset({'health.ready.unavailable'}),
    'shares.admin': frozenset({
        'admin.visibility_batch_failed',
        'admin.visibility_batch_selection_failed',
    }),
    'shares.rate_limits': frozenset({'rate_limit.cache_failed'}),
}
_rate_limit_rule_pattern = re.compile(r'^[a-z][a-z0-9_]{0,63}$')
_request_fields = (
    'request_id',
    'method',
    'route',
    'view',
    'status',
    'duration_ms',
    'db_queries',
    'response_bytes',
    'user_id',
)


def get_current_request_id():
    """Return the server-generated ID for the current request context."""
    return _current_request_id.get()


def _json_scalar(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _safe_event(record):
    if record.name == 'django.request' and record.levelno >= logging.ERROR:
        return 'django.request.error'
    candidate = str(getattr(record, 'event', '') or '').lower()
    return (
        candidate
        if candidate in _trusted_events.get(record.name, ())
        else 'log'
    )


def _safe_request_id(value):
    candidate = str(value or '').lower()
    return candidate if _request_id_pattern.fullmatch(candidate) else None


def _safe_exception_type(record):
    if record.exc_info and record.exc_info[0] is not None:
        candidate = getattr(record.exc_info[0], '__name__', '')
    elif record.name.startswith(('ffxivshare.', 'shares.')):
        candidate = getattr(record, 'exception_type', '')
    else:
        candidate = ''
    candidate = str(candidate or '')
    return (
        candidate
        if _exception_type_pattern.fullmatch(candidate)
        else None
    )


def _safe_operational_fields(event, record):
    if event.startswith('admin.visibility_batch_'):
        fields = {}
        for name in ('batch_number', 'batch_size'):
            value = getattr(record, name, None)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                fields[name] = value
        visibility = getattr(record, 'target_visibility', None)
        if visibility in {'public', 'private'}:
            fields['target_visibility'] = visibility
        return fields
    if event == 'rate_limit.cache_failed':
        rule = str(getattr(record, 'rule', '') or '').lower()
        if _rate_limit_rule_pattern.fullmatch(rule):
            return {'rule': rule}
    return {}


class JsonLogFormatter(logging.Formatter):
    """Serialize only an explicit safe field allowlist as one JSON object."""

    def format(self, record):
        timestamp = datetime.fromtimestamp(record.created, timezone.utc)
        event = _safe_event(record)
        payload = {
            'timestamp': timestamp.isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
            'level': record.levelname,
            'logger': record.name,
            'event': event,
        }

        context_request_id = _safe_request_id(get_current_request_id())
        if record.name == REQUEST_LOGGER_NAME:
            for field in _request_fields:
                payload[field] = _json_scalar(getattr(record, field, None))
            payload['request_id'] = _safe_request_id(payload['request_id'])
        elif context_request_id is not None:
            payload['request_id'] = context_request_id

        exception_type = _safe_exception_type(record)
        if exception_type:
            payload['exception_type'] = exception_type
        payload.update(_safe_operational_fields(event, record))

        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(',', ':'),
        )


class _QueryCounter:
    def __init__(self):
        self.count = 0

    def __call__(self, execute, sql, params, many, context):
        self.count += 1
        return execute(sql, params, many, context)


def _resolved_user_id(request):
    """Read an already-resolved user without triggering authentication queries."""
    user = request.__dict__.get('user')
    if user is None:
        return None

    resolved_user = getattr(user, '_wrapped', user)
    if resolved_user is empty:
        return None
    if not getattr(resolved_user, 'is_authenticated', False):
        return None
    return getattr(resolved_user, 'pk', None)


def _response_size(request, response):
    if request.method == 'HEAD':
        return 0
    if getattr(response, 'streaming', False):
        content_length = response.headers.get('Content-Length')
        try:
            return int(content_length) if content_length is not None else None
        except (TypeError, ValueError):
            return None

    try:
        return len(response.content)
    except (AttributeError, TypeError, ValueError):
        return None


def _request_route(request):
    resolver_match = getattr(request, 'resolver_match', None)
    if resolver_match is None:
        return None, None
    return resolver_match.route, resolver_match.view_name


def _request_log_level(status):
    if status >= 500:
        return logging.ERROR
    if status >= 400:
        return logging.WARNING
    return logging.INFO


class RequestObservabilityMiddleware:
    """Emit one bounded request record without retaining request data or SQL."""

    exception_attribute = '_observability_exception_type'

    def __init__(self, get_response):
        self.get_response = get_response
        self.logger = logging.getLogger(REQUEST_LOGGER_NAME)

    def __call__(self, request):
        request_id = uuid4().hex
        request.request_id = request_id
        context_token = _current_request_id.set(request_id)
        started_at = time.perf_counter()
        query_counter = _QueryCounter()
        response = None
        user_id = None

        try:
            with connection.execute_wrapper(query_counter):
                try:
                    response = self.get_response(request)
                except Exception as exception:
                    user_id = _resolved_user_id(request)
                    self._emit_request_log(
                        request=request,
                        request_id=request_id,
                        status=500,
                        started_at=started_at,
                        query_count=query_counter.count,
                        response_bytes=None,
                        user_id=user_id,
                        exception_type=type(exception).__name__,
                    )
                    raise
                user_id = _resolved_user_id(request)

            response.headers['X-Request-ID'] = request_id
            self._emit_request_log(
                request=request,
                request_id=request_id,
                status=response.status_code,
                started_at=started_at,
                query_count=query_counter.count,
                response_bytes=_response_size(request, response),
                user_id=user_id,
                exception_type=getattr(request, self.exception_attribute, None),
            )
            return response
        finally:
            _current_request_id.reset(context_token)

    def process_exception(self, request, exception):
        """Preserve only the exception class for the eventual request record."""
        setattr(request, self.exception_attribute, type(exception).__name__)
        return None

    def _emit_request_log(
        self,
        *,
        request,
        request_id,
        status,
        started_at,
        query_count,
        response_bytes,
        user_id,
        exception_type=None,
    ):
        route, view = _request_route(request)
        extra = {
            'event': REQUEST_EVENT,
            'request_id': request_id,
            'method': str(request.method).upper()[:32],
            'route': route,
            'view': view,
            'status': status,
            'duration_ms': round((time.perf_counter() - started_at) * 1000, 3),
            'db_queries': query_count,
            'response_bytes': response_bytes,
            'user_id': user_id,
        }
        if exception_type:
            extra['exception_type'] = exception_type
        self.logger.log(
            _request_log_level(status),
            REQUEST_EVENT,
            extra=extra,
        )
