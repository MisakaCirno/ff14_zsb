from django.test import SimpleTestCase, override_settings

from .checks import check_rate_limit_cache


class RateLimitDeploymentCheckTests(SimpleTestCase):
    @override_settings(
        RATE_LIMIT_ENABLED=True,
        CACHES={
            'default': {
                'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            },
        },
    )
    def test_enabled_process_local_rate_limits_raise_deployment_warning(self):
        warnings = check_rate_limit_cache(None)

        self.assertEqual([warning.id for warning in warnings], ['shares.W001'])
        self.assertIn('one application process', warnings[0].hint)

    @override_settings(
        RATE_LIMIT_ENABLED=True,
        CACHES={
            'default': {
                'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            },
        },
    )
    def test_shared_cache_does_not_raise_warning(self):
        self.assertEqual(check_rate_limit_cache(None), [])

    @override_settings(RATE_LIMIT_ENABLED=False)
    def test_disabled_rate_limits_do_not_raise_warning(self):
        self.assertEqual(check_rate_limit_cache(None), [])
