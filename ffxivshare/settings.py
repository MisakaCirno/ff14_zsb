"""
Django settings for ffxivshare project.
"""

from pathlib import Path

from django.contrib.messages import constants as message_constants

from .environment import (
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

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent
load_environment_file(BASE_DIR)

APP_ENV = resolve_app_environment()
IS_PRODUCTION = APP_ENV == 'production'

SECRET_KEY = resolve_secret_key(APP_ENV)
DEBUG = env_bool('DEBUG', default=APP_ENV == 'development')

default_allowed_hosts = () if IS_PRODUCTION else ('127.0.0.1', 'localhost', 'testserver')
ALLOWED_HOSTS = env_list('ALLOWED_HOSTS', default=default_allowed_hosts)
CSRF_TRUSTED_ORIGINS = env_list('CSRF_TRUSTED_ORIGINS')

validate_runtime_config(APP_ENV, debug=DEBUG, allowed_hosts=ALLOWED_HOSTS)

# Security and reverse-proxy settings. The production Waitress service must only
# listen on loopback, and the reverse proxy must replace forwarded headers.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https') if IS_PRODUCTION else None
TRUST_X_FORWARDED_FOR = env_bool('TRUST_X_FORWARDED_FOR', default=IS_PRODUCTION)
RATE_LIMIT_ENABLED = env_bool('RATE_LIMIT_ENABLED', default=True)
SECURE_SSL_REDIRECT = env_bool('SECURE_SSL_REDIRECT', default=IS_PRODUCTION)
SECURE_REDIRECT_EXEMPT = [r'^health/(?:live|ready)/$']
SESSION_COOKIE_SECURE = env_bool('SESSION_COOKIE_SECURE', default=IS_PRODUCTION)
CSRF_COOKIE_SECURE = env_bool('CSRF_COOKIE_SECURE', default=IS_PRODUCTION)
SECURE_HSTS_SECONDS = env_int(
    'SECURE_HSTS_SECONDS',
    default=31536000 if IS_PRODUCTION else 0,
    minimum=0,
)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool('SECURE_HSTS_INCLUDE_SUBDOMAINS', default=False)
SECURE_HSTS_PRELOAD = env_bool('SECURE_HSTS_PRELOAD', default=False)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
SESSION_COOKIE_HTTPONLY = True

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'shares.apps.SharesConfig',
]

MIDDLEWARE = [
    'ffxivshare.observability.RequestObservabilityMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

REQUEST_LOG_ENABLED = env_bool('REQUEST_LOG_ENABLED', default=IS_PRODUCTION)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'safe_json': {
            '()': 'ffxivshare.observability.JsonLogFormatter',
        },
    },
    'handlers': {
        'json_stdout': {
            'class': 'logging.StreamHandler',
            'formatter': 'safe_json',
            'stream': 'ext://sys.stdout',
        },
        'null': {
            'class': 'logging.NullHandler',
        },
    },
    'root': {
        'handlers': ['json_stdout'],
        'level': 'INFO',
    },
    'loggers': {
        'ffxivshare.request': {
            'handlers': ['json_stdout' if REQUEST_LOG_ENABLED else 'null'],
            'level': 'INFO',
            'propagate': False,
        },
        'django': {
            'handlers': ['json_stdout'],
            'level': 'INFO',
            'propagate': False,
        },
        # The structured request record replaces Django/runserver access logs.
        'django.request': {
            'handlers': ['json_stdout' if REQUEST_LOG_ENABLED else 'null'],
            'level': 'ERROR',
            'propagate': False,
        },
        'django.server': {
            'handlers': ['null'],
            'level': 'INFO',
            'propagate': False,
        },
        'django.db.backends': {
            'handlers': ['json_stdout'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}

ROOT_URLCONF = 'ffxivshare.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'shares.context_processors.admin_counts',
            ],
        },
    },
]

MESSAGE_TAGS = {
    message_constants.DEBUG: 'secondary',
    message_constants.ERROR: 'danger',
}

WSGI_APPLICATION = 'ffxivshare.wsgi.application'

# Database
DATABASES = {
    'default': build_database_config(BASE_DIR),
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = True

# Admin site configuration
ADMIN_SITE_HEADER = '粘鼠板儿管理后台'
ADMIN_SITE_TITLE = '管理后台'
ADMIN_INDEX_TITLE = '欢迎使用管理后台'

# Static files (CSS, JavaScript, Images)
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
VITE_MANIFEST_PATH = BASE_DIR / 'static' / 'app' / 'manifest.json'
VITE_ENTRYPOINT = 'src/main.ts'

# Media files
MEDIA_URL = '/media/'
MEDIA_ROOT = resolve_runtime_path('MEDIA_ROOT', BASE_DIR, 'media')

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Login settings
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/'
