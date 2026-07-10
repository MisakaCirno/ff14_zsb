from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from .environment import (
    DEVELOPMENT_SECRET_KEY,
    env_bool,
    env_int,
    env_list,
    resolve_app_environment,
    resolve_secret_key,
    validate_runtime_config,
)


class EnvironmentParsingTests(SimpleTestCase):
    def test_env_bool_accepts_common_true_and_false_values(self):
        for raw_value in ('1', 'true', 'TRUE', 'yes', 'on'):
            with self.subTest(raw_value=raw_value):
                self.assertTrue(env_bool('VALUE', environ={'VALUE': raw_value}))
        for raw_value in ('0', 'false', 'FALSE', 'no', 'off'):
            with self.subTest(raw_value=raw_value):
                self.assertFalse(env_bool('VALUE', environ={'VALUE': raw_value}))

    def test_env_bool_rejects_unknown_value(self):
        with self.assertRaisesMessage(ImproperlyConfigured, 'VALUE must be a boolean value.'):
            env_bool('VALUE', environ={'VALUE': 'sometimes'})

    def test_env_int_validates_type_and_minimum(self):
        self.assertEqual(env_int('VALUE', environ={'VALUE': ' 42 '}), 42)
        with self.assertRaisesMessage(ImproperlyConfigured, 'VALUE must be an integer value.'):
            env_int('VALUE', environ={'VALUE': 'four'})
        with self.assertRaisesMessage(ImproperlyConfigured, 'greater than or equal to 0'):
            env_int('VALUE', minimum=0, environ={'VALUE': '-1'})

    def test_env_list_strips_whitespace_and_empty_items(self):
        self.assertEqual(
            env_list('VALUE', environ={'VALUE': ' example.com, ,localhost,127.0.0.1 '}),
            ['example.com', 'localhost', '127.0.0.1'],
        )


class ApplicationEnvironmentTests(SimpleTestCase):
    def test_explicit_application_environment_is_normalized(self):
        self.assertEqual(resolve_app_environment(environ={'APP_ENV': ' Test '}), 'test')

    def test_missing_environment_is_only_development_when_debug_is_explicit(self):
        self.assertEqual(resolve_app_environment(environ={'DEBUG': 'True'}), 'development')
        self.assertEqual(resolve_app_environment(environ={}), 'production')

    def test_invalid_application_environment_is_rejected(self):
        with self.assertRaisesMessage(ImproperlyConfigured, 'APP_ENV must be one of'):
            resolve_app_environment(environ={'APP_ENV': 'staging'})


class ProductionConfigurationTests(SimpleTestCase):
    def test_non_production_environment_can_use_local_secret_fallback(self):
        self.assertEqual(resolve_secret_key('development', environ={}), DEVELOPMENT_SECRET_KEY)

    def test_production_requires_strong_explicit_secret(self):
        for environ in (
            {},
            {'SECRET_KEY': 'django-insecure-placeholder'},
            {'SECRET_KEY': 'too-short'},
        ):
            with self.subTest(environ=environ):
                with self.assertRaises(ImproperlyConfigured):
                    resolve_secret_key('production', environ=environ)

        strong_secret = 'prod-secret-with-more-than-fifty-characters-and-enough-variety-123456'
        self.assertEqual(
            resolve_secret_key('production', environ={'SECRET_KEY': strong_secret}),
            strong_secret,
        )

    def test_production_rejects_debug_and_non_explicit_hosts(self):
        with self.assertRaisesMessage(ImproperlyConfigured, 'DEBUG must be disabled'):
            validate_runtime_config('production', debug=True, allowed_hosts=['example.com'])
        for hosts in ([], ['*'], ['example.com', '*']):
            with self.subTest(hosts=hosts):
                with self.assertRaisesMessage(ImproperlyConfigured, 'ALLOWED_HOSTS must be explicit'):
                    validate_runtime_config('production', debug=False, allowed_hosts=hosts)

        validate_runtime_config('production', debug=False, allowed_hosts=['example.com'])
