from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from .environment import (
    DEVELOPMENT_SECRET_KEY,
    ENV_FILE_VARIABLE,
    build_database_config,
    env_bool,
    env_int,
    env_list,
    load_environment_file,
    resolve_app_environment,
    resolve_runtime_path,
    resolve_secret_key,
    validate_runtime_config,
)


class EnvironmentFileLoadingTests(SimpleTestCase):
    def write_env(self, directory, content, name='app.env'):
        env_file = Path(directory) / name
        env_file.write_text(content, encoding='utf-8')
        return env_file

    def test_explicit_file_must_be_an_existing_absolute_regular_file(self):
        with TemporaryDirectory() as temporary_directory:
            base_dir = Path(temporary_directory)
            relative_file = self.write_env(base_dir, 'VALUE=relative\n')
            missing_file = base_dir / 'missing.env'

            invalid_values = (
                '',
                '   ',
                relative_file.name,
                str(missing_file),
                str(base_dir),
            )
            for value in invalid_values:
                with self.subTest(value=value):
                    with self.assertRaisesMessage(
                        ImproperlyConfigured,
                        f'{ENV_FILE_VARIABLE} must be an absolute path to an existing file.',
                    ) as raised:
                        load_environment_file(
                            base_dir,
                            environ={ENV_FILE_VARIABLE: value},
                        )
                    self.assertNotIn(str(base_dir), str(raised.exception))

    def test_environment_read_failure_is_generic_and_does_not_log_the_error(self):
        secret = 'secret-from-filesystem-error'
        with TemporaryDirectory() as temporary_directory:
            env_file = self.write_env(temporary_directory, 'VALUE=unread\n')

            with patch(
                'ffxivshare.environment.dotenv_values',
                side_effect=OSError(secret),
            ):
                with self.assertNoLogs(level='WARNING'):
                    with self.assertRaisesMessage(
                        ImproperlyConfigured,
                        'The environment file could not be read.',
                    ) as raised:
                        load_environment_file(
                            temporary_directory,
                            environ={ENV_FILE_VARIABLE: str(env_file)},
                        )

            self.assertNotIn(secret, str(raised.exception))

    def test_explicit_file_is_loaded_without_reading_project_dotenv(self):
        with TemporaryDirectory() as base_directory, TemporaryDirectory() as external_directory:
            base_dir = Path(base_directory)
            self.write_env(base_dir, 'SOURCE=project\n', name='.env')
            external_file = self.write_env(
                external_directory,
                'SOURCE=external\nNEW_VALUE=from-file\n',
            )
            environ = {ENV_FILE_VARIABLE: str(external_file)}

            loaded_file = load_environment_file(base_dir, environ=environ)

            self.assertEqual(loaded_file, external_file.resolve())
            self.assertEqual(environ['SOURCE'], 'external')
            self.assertEqual(environ['NEW_VALUE'], 'from-file')

    def test_process_environment_has_priority_over_file_values(self):
        with TemporaryDirectory() as temporary_directory:
            env_file = self.write_env(
                temporary_directory,
                (
                    'SECRET_KEY=file-secret\n'
                    'NEW_VALUE=from-file\n'
                    'DERIVED=${SECRET_KEY}-derived\n'
                ),
            )
            environ = {
                ENV_FILE_VARIABLE: str(env_file),
                'SECRET_KEY': 'process-secret',
            }

            with self.assertNoLogs(level='WARNING'):
                load_environment_file(temporary_directory, environ=environ)

            self.assertEqual(environ['SECRET_KEY'], 'process-secret')
            self.assertEqual(environ['NEW_VALUE'], 'from-file')
            self.assertEqual(environ['DERIVED'], 'process-secret-derived')

    def test_default_loader_reads_only_base_dir_dotenv(self):
        with TemporaryDirectory() as temporary_directory:
            base_dir = Path(temporary_directory)
            env_file = self.write_env(base_dir, 'SOURCE=base-dir\n', name='.env')
            environ = {}

            with patch(
                'ffxivshare.environment.dotenv_values',
                return_value={'SOURCE': 'base-dir'},
            ) as dotenv_values_mock:
                loaded_file = load_environment_file(base_dir, environ=environ)

            self.assertEqual(loaded_file, env_file.resolve())
            dotenv_values_mock.assert_called_once()
            called_path = dotenv_values_mock.call_args.kwargs['dotenv_path']
            self.assertTrue(called_path.samefile(env_file))
            self.assertEqual(
                dotenv_values_mock.call_args.kwargs['encoding'],
                'utf-8',
            )
            self.assertFalse(dotenv_values_mock.call_args.kwargs['interpolate'])
            self.assertEqual(environ['SOURCE'], 'base-dir')

    def test_missing_default_file_is_a_noop_and_never_searches_the_cwd(self):
        with TemporaryDirectory() as temporary_directory:
            with patch('ffxivshare.environment.dotenv_values') as dotenv_values_mock:
                loaded_file = load_environment_file(temporary_directory, environ={})

            self.assertIsNone(loaded_file)
            dotenv_values_mock.assert_not_called()

    def test_selector_inside_dotenv_cannot_change_future_file_selection(self):
        with TemporaryDirectory() as temporary_directory:
            env_file = self.write_env(
                temporary_directory,
                f'{ENV_FILE_VARIABLE}=D:\\private\\other.env\nVALUE=loaded\n',
                name='.env',
            )
            environ = {}

            self.assertEqual(
                load_environment_file(temporary_directory, environ=environ),
                env_file.resolve(),
            )

            self.assertNotIn(ENV_FILE_VARIABLE, environ)
            self.assertEqual(environ['VALUE'], 'loaded')

    def test_project_dotenv_must_be_a_regular_file_when_present(self):
        with TemporaryDirectory() as temporary_directory:
            (Path(temporary_directory) / '.env').mkdir()

            with self.assertRaisesMessage(
                ImproperlyConfigured,
                'The project .env path must be a regular file.',
            ):
                load_environment_file(temporary_directory, environ={})


class RuntimePathResolutionTests(SimpleTestCase):
    def test_missing_or_blank_value_uses_the_compatible_default(self):
        with TemporaryDirectory() as temporary_directory:
            base_dir = Path(temporary_directory)
            expected = (base_dir / 'media').resolve()

            for environ in ({}, {'MEDIA_ROOT': ''}, {'MEDIA_ROOT': '   '}):
                with self.subTest(environ=environ):
                    self.assertEqual(
                        resolve_runtime_path(
                            'MEDIA_ROOT',
                            base_dir,
                            'media',
                            environ=environ,
                        ),
                        expected,
                    )

    def test_relative_value_is_anchored_to_base_dir_not_the_cwd(self):
        with TemporaryDirectory() as temporary_directory:
            base_dir = Path(temporary_directory)

            resolved = resolve_runtime_path(
                'MEDIA_ROOT',
                base_dir,
                'media',
                environ={'MEDIA_ROOT': 'persistent/media'},
            )

            self.assertEqual(resolved, (base_dir / 'persistent' / 'media').resolve())

    def test_absolute_external_path_is_preserved_without_requiring_it_to_exist(self):
        with TemporaryDirectory() as temporary_directory:
            external_path = Path(temporary_directory) / 'future-media-directory'

            resolved = resolve_runtime_path(
                'MEDIA_ROOT',
                Path.cwd(),
                'media',
                environ={'MEDIA_ROOT': str(external_path)},
            )

            self.assertEqual(resolved, external_path.resolve())
            self.assertFalse(external_path.exists())

    def test_invalid_path_fails_without_echoing_its_value(self):
        invalid_value = 'secret-value\0media'

        with self.assertRaisesMessage(
            ImproperlyConfigured,
            'MEDIA_ROOT must be a valid filesystem path.',
        ) as raised:
            resolve_runtime_path(
                'MEDIA_ROOT',
                Path.cwd(),
                'media',
                environ={'MEDIA_ROOT': invalid_value},
            )

        self.assertNotIn('secret-value', str(raised.exception))


class SettingsBootstrapContractTests(SimpleTestCase):
    def setUp(self):
        self.project_root = Path(__file__).resolve().parent.parent
        self.settings_source = (
            self.project_root / 'ffxivshare' / 'settings.py'
        ).read_text(encoding='utf-8')

    def test_base_dir_is_known_before_the_only_dotenv_load(self):
        base_dir_assignment = 'BASE_DIR = Path(__file__).resolve().parent.parent'
        environment_load = 'load_environment_file(BASE_DIR)'

        self.assertLess(
            self.settings_source.index(base_dir_assignment),
            self.settings_source.index(environment_load),
        )
        self.assertNotIn('load_dotenv(', self.settings_source)

    def test_media_root_is_wired_to_the_shared_runtime_path_resolver(self):
        self.assertIn(
            "MEDIA_ROOT = resolve_runtime_path('MEDIA_ROOT', BASE_DIR, 'media')",
            self.settings_source,
        )

    def test_environment_samples_keep_legacy_default_and_external_production_media(self):
        development_sample = (self.project_root / '.env.sample').read_text(encoding='utf-8')
        production_sample = (
            self.project_root / '.env.production.sample'
        ).read_text(encoding='utf-8')

        self.assertIn('MEDIA_ROOT=media', development_sample)
        self.assertIn(r'MEDIA_ROOT=D:\FFXIVShareData\media', production_sample)
        self.assertIn(
            r'DATABASE_PATH=D:\FFXIVShareData\database\ffxivshare.sqlite3',
            production_sample,
        )
        self.assertIn(
            r'# FFXIVSHARE_ENV_FILE=D:\FFXIVShareData\config\ffxivshare.env',
            production_sample,
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


class DatabaseConfigurationTests(SimpleTestCase):
    def setUp(self):
        self.base_dir = Path.cwd()

    def test_sqlite_defaults_use_wal_full_sync_and_immediate_transactions(self):
        config = build_database_config(self.base_dir, environ={})

        self.assertEqual(config['ENGINE'], 'django.db.backends.sqlite3')
        self.assertEqual(config['NAME'], (self.base_dir / 'db.sqlite3').resolve())
        self.assertEqual(config['CONN_MAX_AGE'], 0)
        self.assertEqual(config['OPTIONS']['timeout'], 30)
        self.assertEqual(config['OPTIONS']['transaction_mode'], 'IMMEDIATE')
        self.assertIn('journal_mode=WAL', config['OPTIONS']['init_command'])
        self.assertIn('synchronous=FULL', config['OPTIONS']['init_command'])

    def test_sqlite_path_and_safe_options_are_configurable(self):
        config = build_database_config(self.base_dir, environ={
            'DATABASE_ENGINE': 'sqlite',
            'DATABASE_PATH': 'data/site.sqlite3',
            'SQLITE_TIMEOUT': '45',
            'SQLITE_TRANSACTION_MODE': 'deferred',
            'SQLITE_JOURNAL_MODE': 'delete',
            'SQLITE_SYNCHRONOUS': 'extra',
        })

        self.assertEqual(
            config['NAME'],
            (self.base_dir / 'data' / 'site.sqlite3').resolve(),
        )
        self.assertEqual(config['OPTIONS']['timeout'], 45)
        self.assertEqual(config['OPTIONS']['transaction_mode'], 'DEFERRED')
        self.assertIn('journal_mode=DELETE', config['OPTIONS']['init_command'])
        self.assertIn('synchronous=EXTRA', config['OPTIONS']['init_command'])

    def test_invalid_database_options_are_rejected(self):
        for environ in (
            {'DATABASE_ENGINE': 'mysql'},
            {'SQLITE_TIMEOUT': '0'},
            {'SQLITE_TRANSACTION_MODE': 'sometimes'},
            {'SQLITE_JOURNAL_MODE': 'unsafe'},
        ):
            with self.subTest(environ=environ):
                with self.assertRaises(ImproperlyConfigured):
                    build_database_config(self.base_dir, environ=environ)

    def test_postgresql_configuration_requires_credentials(self):
        with self.assertRaisesMessage(ImproperlyConfigured, 'DATABASE_NAME is required'):
            build_database_config(
                self.base_dir,
                environ={'DATABASE_ENGINE': 'postgresql'},
            )

    def test_postgresql_configuration_is_backend_neutral(self):
        config = build_database_config(self.base_dir, environ={
            'DATABASE_ENGINE': 'postgresql',
            'DATABASE_NAME': 'ffxivshare',
            'DATABASE_USER': 'ffxivshare',
            'DATABASE_PASSWORD': 'test-password',
            'DATABASE_HOST': '127.0.0.1',
            'DATABASE_PORT': '5433',
            'DATABASE_CONN_MAX_AGE': '90',
            'DATABASE_CONNECT_TIMEOUT': '5',
            'DATABASE_SSLMODE': 'require',
        })

        self.assertEqual(config['ENGINE'], 'django.db.backends.postgresql')
        self.assertEqual(config['NAME'], 'ffxivshare')
        self.assertEqual(config['PORT'], 5433)
        self.assertEqual(config['CONN_MAX_AGE'], 90)
        self.assertTrue(config['CONN_HEALTH_CHECKS'])
        self.assertEqual(config['OPTIONS']['connect_timeout'], 5)
        self.assertEqual(config['OPTIONS']['sslmode'], 'require')
