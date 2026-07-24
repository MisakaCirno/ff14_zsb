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

    def test_safe_rate_limit_configurations_do_not_raise_warning(self):
        cases = (
            {
                'RATE_LIMIT_ENABLED': True,
                'CACHES': {
                    'default': {
                        'BACKEND': (
                            'django.core.cache.backends.redis.RedisCache'
                        ),
                    },
                },
            },
            {'RATE_LIMIT_ENABLED': False},
        )
        for settings_override in cases:
            with (
                self.subTest(settings=settings_override),
                self.settings(**settings_override),
            ):
                self.assertEqual(check_rate_limit_cache(None), [])
