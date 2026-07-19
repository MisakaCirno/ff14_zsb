from django.conf import settings
from django.core.checks import Tags, Warning, register


@register(Tags.security, deploy=True)
def check_rate_limit_cache(app_configs, **kwargs):
    """Warn when enabled rate limits are isolated to a single process."""
    if not getattr(settings, 'RATE_LIMIT_ENABLED', True):
        return []

    default_cache = settings.CACHES.get('default', {})
    backend = default_cache.get(
        'BACKEND',
        'django.core.cache.backends.locmem.LocMemCache',
    )
    if not backend.endswith('.LocMemCache'):
        return []

    return [
        Warning(
            'Rate-limit counters use process-local memory.',
            hint=(
                'Keep one application process, or configure a shared cache '
                'before running multiple processes or instances.'
            ),
            id='shares.W001',
        ),
    ]
