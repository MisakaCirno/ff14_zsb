import os
from collections.abc import Mapping
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


APP_ENVIRONMENTS = frozenset({'development', 'test', 'production'})
DEVELOPMENT_SECRET_KEY = 'django-insecure-local-development-only-key'


def _environment(environ=None):
    return os.environ if environ is None else environ


def env_bool(name, default=False, *, environ=None):
    raw_value = _environment(environ).get(name)
    if raw_value is None or str(raw_value).strip() == '':
        return default

    value = str(raw_value).strip().lower()
    if value in {'1', 'true', 'yes', 'on'}:
        return True
    if value in {'0', 'false', 'no', 'off'}:
        return False
    raise ImproperlyConfigured(f'{name} must be a boolean value.')


def env_int(name, default=0, *, minimum=None, environ=None):
    raw_value = _environment(environ).get(name)
    if raw_value is None or str(raw_value).strip() == '':
        value = default
    else:
        try:
            value = int(str(raw_value).strip())
        except ValueError as exc:
            raise ImproperlyConfigured(f'{name} must be an integer value.') from exc

    if minimum is not None and value < minimum:
        raise ImproperlyConfigured(f'{name} must be greater than or equal to {minimum}.')
    return value


def env_list(name, default=(), *, environ=None):
    raw_value = _environment(environ).get(name)
    if raw_value is None:
        return list(default)
    return [item.strip() for item in str(raw_value).split(',') if item.strip()]


def env_choice(name, choices, default, *, environ=None, normalize=str.lower):
    raw_value = _environment(environ).get(name)
    value = default if raw_value is None or str(raw_value).strip() == '' else raw_value
    normalized = normalize(str(value).strip())
    allowed = {normalize(str(choice)): choice for choice in choices}
    if normalized not in allowed:
        values = ', '.join(str(choice) for choice in choices)
        raise ImproperlyConfigured(f'{name} must be one of: {values}.')
    return allowed[normalized]


def _required_env(name, source):
    value = str(source.get(name, '')).strip()
    if not value:
        raise ImproperlyConfigured(f'{name} is required for PostgreSQL.')
    return value


def build_database_config(base_dir, *, environ=None):
    source = _environment(environ)
    engine = env_choice(
        'DATABASE_ENGINE',
        ('sqlite', 'postgresql'),
        'sqlite',
        environ=source,
    )
    if engine == 'sqlite':
        raw_path = str(source.get('DATABASE_PATH', '')).strip() or 'db.sqlite3'
        database_path = Path(raw_path).expanduser()
        if not database_path.is_absolute():
            database_path = Path(base_dir) / database_path
        transaction_mode = env_choice(
            'SQLITE_TRANSACTION_MODE',
            ('DEFERRED', 'IMMEDIATE', 'EXCLUSIVE'),
            'IMMEDIATE',
            environ=source,
            normalize=str.upper,
        )
        journal_mode = env_choice(
            'SQLITE_JOURNAL_MODE',
            ('DELETE', 'WAL'),
            'WAL',
            environ=source,
            normalize=str.upper,
        )
        synchronous = env_choice(
            'SQLITE_SYNCHRONOUS',
            ('FULL', 'EXTRA'),
            'FULL',
            environ=source,
            normalize=str.upper,
        )
        return {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': database_path.resolve(),
            'CONN_MAX_AGE': 0,
            'OPTIONS': {
                'timeout': env_int(
                    'SQLITE_TIMEOUT',
                    default=30,
                    minimum=1,
                    environ=source,
                ),
                'transaction_mode': transaction_mode,
                'init_command': (
                    f'PRAGMA journal_mode={journal_mode}; '
                    f'PRAGMA synchronous={synchronous}; '
                    'PRAGMA wal_autocheckpoint=1000;'
                ),
            },
        }

    sslmode = env_choice(
        'DATABASE_SSLMODE',
        ('disable', 'allow', 'prefer', 'require', 'verify-ca', 'verify-full'),
        'prefer',
        environ=source,
    )
    return {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': _required_env('DATABASE_NAME', source),
        'USER': _required_env('DATABASE_USER', source),
        'PASSWORD': _required_env('DATABASE_PASSWORD', source),
        'HOST': _required_env('DATABASE_HOST', source),
        'PORT': env_int('DATABASE_PORT', default=5432, minimum=1, environ=source),
        'CONN_MAX_AGE': env_int(
            'DATABASE_CONN_MAX_AGE',
            default=60,
            minimum=0,
            environ=source,
        ),
        'CONN_HEALTH_CHECKS': True,
        'OPTIONS': {
            'connect_timeout': env_int(
                'DATABASE_CONNECT_TIMEOUT',
                default=10,
                minimum=1,
                environ=source,
            ),
            'sslmode': sslmode,
        },
    }


def resolve_app_environment(*, environ=None):
    source = _environment(environ)
    raw_value = source.get('APP_ENV')
    if raw_value is None or str(raw_value).strip() == '':
        return 'development' if env_bool('DEBUG', False, environ=source) else 'production'

    app_env = str(raw_value).strip().lower()
    if app_env not in APP_ENVIRONMENTS:
        choices = ', '.join(sorted(APP_ENVIRONMENTS))
        raise ImproperlyConfigured(f'APP_ENV must be one of: {choices}.')
    return app_env


def resolve_secret_key(app_env, *, environ=None):
    secret_key = str(_environment(environ).get('SECRET_KEY', '')).strip()
    if app_env != 'production':
        return secret_key or DEVELOPMENT_SECRET_KEY

    if not secret_key:
        raise ImproperlyConfigured('SECRET_KEY is required in production.')
    if secret_key.startswith('django-insecure-'):
        raise ImproperlyConfigured('SECRET_KEY must not use a django-insecure value in production.')
    if len(secret_key) < 50 or len(set(secret_key)) < 5:
        raise ImproperlyConfigured('SECRET_KEY is too weak for production.')
    return secret_key


def validate_runtime_config(app_env, *, debug, allowed_hosts):
    if app_env != 'production':
        return
    if debug:
        raise ImproperlyConfigured('DEBUG must be disabled in production.')
    if not allowed_hosts or '*' in allowed_hosts:
        raise ImproperlyConfigured('ALLOWED_HOSTS must be explicit in production.')
