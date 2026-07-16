"""Minimal liveness and database readiness probes."""

import logging

from django.db import connection
from django.http import HttpResponseNotAllowed, JsonResponse
from django.views.decorators.csrf import csrf_exempt


logger = logging.getLogger(__name__)
_safe_methods = ('GET', 'HEAD')


def _no_store(response):
    response.headers['Cache-Control'] = 'no-store'
    return response


def _method_not_allowed():
    return _no_store(HttpResponseNotAllowed(_safe_methods))


@csrf_exempt
def live(request):
    if request.method not in _safe_methods:
        return _method_not_allowed()
    return _no_store(JsonResponse({'status': 'ok'}))


@csrf_exempt
def ready(request):
    if request.method not in _safe_methods:
        return _method_not_allowed()

    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
    except Exception as exception:
        logger.warning(
            'Database readiness check failed.',
            extra={
                'event': 'health.ready.unavailable',
                'exception_type': type(exception).__name__,
            },
        )
        return _no_store(JsonResponse({'status': 'unavailable'}, status=503))

    return _no_store(JsonResponse({'status': 'ok'}))
