"""
Django settings for ffxivshare project.
"""

from pathlib import Path
from dotenv import load_dotenv

from .environment import (
    env_bool,
    env_int,
    env_list,
    resolve_app_environment,
    resolve_secret_key,
    validate_runtime_config,
)

# Load environment variables from .env file
load_dotenv()

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

APP_ENV = resolve_app_environment()
IS_PRODUCTION = APP_ENV == 'production'

SECRET_KEY = resolve_secret_key(APP_ENV)
DEBUG = env_bool('DEBUG', default=APP_ENV == 'development')

default_allowed_hosts = () if IS_PRODUCTION else ('127.0.0.1', 'localhost', 'testserver')
ALLOWED_HOSTS = env_list('ALLOWED_HOSTS', default=default_allowed_hosts)
CSRF_TRUSTED_ORIGINS = env_list('CSRF_TRUSTED_ORIGINS')

validate_runtime_config(APP_ENV, debug=DEBUG, allowed_hosts=ALLOWED_HOSTS)

# Security and reverse-proxy settings. The production Waitress service must only
# listen on loopback, and the reverse proxy must replace X-Forwarded-Proto.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https') if IS_PRODUCTION else None
SECURE_SSL_REDIRECT = env_bool('SECURE_SSL_REDIRECT', default=IS_PRODUCTION)
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
    'ckeditor',
    'shares',
]

CKEDITOR_CONFIGS = {
    'default': {
        'toolbar': 'Custom',
        'toolbar_Custom': [
            ['Bold', 'Italic', 'Underline'],
            ['NumberedList', 'BulletedList', '-', 'Outdent', 'Indent', '-', 'JustifyLeft', 'JustifyCenter', 'JustifyRight', 'JustifyBlock'],
            ['Link', 'Unlink'],
            ['RemoveFormat', 'Source']
        ],
        'height': 300,
        'width': '100%',
    }
}

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

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

WSGI_APPLICATION = 'ffxivshare.wsgi.application'

# Database
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
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

# Media files
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Login settings
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/'
