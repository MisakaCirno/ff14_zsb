"""Discover node-zsb's opaque render version, once per process/cache window."""

import json
import logging
import re
import threading
import time
import urllib.request

from django.conf import settings


logger = logging.getLogger(__name__)
MAX_AGE_SECONDS = 60
FAILURE_RETRY_SECONDS = 5
MAX_META_BYTES = 16 * 1024


def _remaining_lifetime(headers):
    # Missing Age means a direct response; malformed Age cannot establish freshness.
    raw_age = headers.get('Age', '0').strip()
    if not re.fullmatch(r'[0-9]+', raw_age):
        return 0
    age = int(raw_age)
    lifetime = MAX_AGE_SECONDS
    for directive in headers.get('Cache-Control', '').lower().split(','):
        name, _, value = directive.strip().partition('=')
        if name in {'no-cache', 'no-store'}:
            return 0
        if name == 'max-age':
            value = value.strip().strip('"')
            if not re.fullmatch(r'[0-9]+', value):
                return 0
            lifetime = min(lifetime, int(value))
    return max(0, lifetime - age)


class RenderVersionCache:
    """Thread-safe cache for the current single-process Waitress deployment.

    Refreshes are serialized and checked again after acquiring the lock. Waiters
    have a bounded wait and can use the unversioned image route in the meantime.
    Failures cache only None, never an expired version.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._key = None
        self._version = None
        self._expires_at = 0

    def get(self):
        url = settings.BOARD_RENDER_META_URL
        if not url:
            return None
        timeout = settings.BOARD_RENDER_META_TIMEOUT_SECONDS
        key = (url, timeout)
        if not self._lock.acquire(timeout=timeout):
            return None
        try:
            if self._key == key and time.monotonic() < self._expires_at:
                return self._version

            # Anchor freshness before the request, so network time is deducted too.
            started_at = time.monotonic()
            version = None
            expires_at = started_at
            try:
                request = urllib.request.Request(url, headers={'Accept': 'application/json'})
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    if response.status != 200:
                        raise ValueError('Invalid metadata status')
                    body = response.read(MAX_META_BYTES + 1)
                    if len(body) > MAX_META_BYTES:
                        raise ValueError('Metadata exceeds size limit')
                    payload = json.loads(body)
                    if not isinstance(payload, dict) or payload.get('ok') is not True:
                        raise ValueError('Invalid metadata envelope')
                    data = payload.get('data')
                    candidate = data.get('renderVersion') if isinstance(data, dict) else None
                    if not isinstance(candidate, str) or not candidate:
                        raise ValueError('Invalid render version')
                    # Reject unencodable JSON surrogates before they reach URL generation.
                    candidate.encode('utf-8')
                    expires_at = started_at + _remaining_lifetime(response.headers)
                    if time.monotonic() < expires_at:
                        version = candidate
            except Exception as error:
                version = None
                # Metadata is optional. Avoid logging URLs, response bodies or codes.
                logger.warning('Render metadata unavailable (%s).', type(error).__name__)

            if version is None:
                expires_at = time.monotonic() + FAILURE_RETRY_SECONDS
            self._key = key
            self._version = version
            self._expires_at = expires_at
            return version
        finally:
            self._lock.release()


_version_cache = RenderVersionCache()


def get_board_render_version():
    return _version_cache.get()
