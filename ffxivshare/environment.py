import os
from collections.abc import Mapping

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
