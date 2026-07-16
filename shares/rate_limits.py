from dataclasses import dataclass
from hashlib import sha256
from ipaddress import ip_address
import logging
import time

from django.conf import settings
from django.core.cache import cache


logger = logging.getLogger(__name__)

DEFAULT_RATE_LIMIT_RULES = {
    'register_ip': (5, 60 * 60),
    'login_ip': (30, 10 * 60),
    'login_account': (100, 60 * 60),
    'password_change_ip': (30, 60 * 60),
    'password_change_user': (10, 60 * 60),
    'anonymous_create_ip': (10, 60 * 60),
    'authenticated_create_user': (60, 60 * 60),
    'report_user': (10, 60 * 60),
    'view_counter_ip': (120, 60 * 60),
    'copy_counter_ip': (60, 60 * 60),
}


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    count: int
    limit: int
    retry_after: int


def get_client_ip(request):
    """Return a canonical client IP, trusting forwarding only when configured."""
    candidates = []
    if getattr(settings, 'TRUST_X_FORWARDED_FOR', False):
        forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
        if forwarded:
            candidates.append(forwarded.split(',', 1)[0].strip())
    candidates.append(request.META.get('REMOTE_ADDR', ''))

    for candidate in candidates:
        try:
            return ip_address(candidate).compressed
        except ValueError:
            continue
    return 'unknown'


def request_identity(request):
    if request.user.is_authenticated:
        return f'user:{request.user.pk}'
    return f'ip:{get_client_ip(request)}'


def consume_rate_limit(rule_name, identity, *, now=None):
    rules = DEFAULT_RATE_LIMIT_RULES | getattr(settings, 'RATE_LIMIT_RULES', {})
    limit, window_seconds = rules[rule_name]
    timestamp = int(time.time() if now is None else now)
    retry_after = window_seconds - (timestamp % window_seconds)

    if not getattr(settings, 'RATE_LIMIT_ENABLED', True):
        return RateLimitResult(True, 0, limit, retry_after)

    bucket = timestamp // window_seconds
    digest = sha256(f'{rule_name}:{identity}:{bucket}'.encode()).hexdigest()
    cache_key = f'ffxivshare:rate-limit:{digest}'

    try:
        if cache.add(cache_key, 1, timeout=window_seconds + 1):
            count = 1
        else:
            try:
                count = cache.incr(cache_key)
            except ValueError:
                cache.set(cache_key, 1, timeout=window_seconds + 1)
                count = 1
    except Exception:
        logger.exception(
            'Rate-limit cache failed; allowing request.',
            extra={
                'event': 'rate_limit.cache_failed',
                'rule': rule_name,
            },
        )
        return RateLimitResult(True, 0, limit, retry_after)

    return RateLimitResult(count <= limit, count, limit, retry_after)
