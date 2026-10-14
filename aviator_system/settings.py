from pathlib import Path
import os

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')

APP_NAME = os.getenv('APP_NAME', 'Pakabet')

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', '')
if not SECRET_KEY:
    raise ImproperlyConfigured('DJANGO_SECRET_KEY must be set in the environment or .env file.')

DEBUG = os.getenv('DJANGO_DEBUG', 'True').lower() in ['true', '1', 'yes']

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv('DJANGO_ALLOWED_HOSTS', '127.0.0.1,localhost,testserver').split(',')
    if host.strip()
]
ADMINS = [
    (email.strip(), email.strip())
    for email in os.getenv('DJANGO_ADMINS', '').split(',')
    if email.strip()
]
SECURITY_LOGIN_FAILURE_THRESHOLD = int(os.getenv('SECURITY_LOGIN_FAILURE_THRESHOLD', '5'))
SECURITY_LOGIN_FAILURE_WINDOW_SECONDS = int(os.getenv('SECURITY_LOGIN_FAILURE_WINDOW_SECONDS', '600'))
SECURITY_ALERT_COOLDOWN_SECONDS = int(os.getenv('SECURITY_ALERT_COOLDOWN_SECONDS', '900'))
SECURITY_LOGIN_ACCOUNT_LIMIT = int(os.getenv('SECURITY_LOGIN_ACCOUNT_LIMIT', '10'))
SECURITY_LOGIN_IP_LIMIT = int(os.getenv('SECURITY_LOGIN_IP_LIMIT', '50'))
SECURITY_LOGIN_RATE_WINDOW_SECONDS = int(os.getenv('SECURITY_LOGIN_RATE_WINDOW_SECONDS', '900'))
SECURITY_BET_ACCOUNT_LIMIT = int(os.getenv('SECURITY_BET_ACCOUNT_LIMIT', '30'))
SECURITY_BET_IP_LIMIT = int(os.getenv('SECURITY_BET_IP_LIMIT', '90'))
SECURITY_CASHOUT_ACCOUNT_LIMIT = int(os.getenv('SECURITY_CASHOUT_ACCOUNT_LIMIT', '30'))
SECURITY_CASHOUT_IP_LIMIT = int(os.getenv('SECURITY_CASHOUT_IP_LIMIT', '120'))
SECURITY_GAME_RATE_WINDOW_SECONDS = int(os.getenv('SECURITY_GAME_RATE_WINDOW_SECONDS', '60'))
SECURITY_REGISTER_IP_LIMIT = int(os.getenv('SECURITY_REGISTER_IP_LIMIT', '5'))
SECURITY_REGISTER_WINDOW_SECONDS = int(os.getenv('SECURITY_REGISTER_WINDOW_SECONDS', '3600'))
SECURITY_PAYMENT_ACCOUNT_LIMIT = int(os.getenv('SECURITY_PAYMENT_ACCOUNT_LIMIT', '8'))
SECURITY_PAYMENT_IP_LIMIT = int(os.getenv('SECURITY_PAYMENT_IP_LIMIT', '20'))
SECURITY_PAYMENT_WINDOW_SECONDS = int(os.getenv('SECURITY_PAYMENT_WINDOW_SECONDS', '3600'))
SECURITY_ROOM_CREATE_ACCOUNT_LIMIT = int(os.getenv('SECURITY_ROOM_CREATE_ACCOUNT_LIMIT', '5'))
SECURITY_ROOM_CREATE_IP_LIMIT = int(os.getenv('SECURITY_ROOM_CREATE_IP_LIMIT', '20'))
SECURITY_ROOM_CREATE_WINDOW_SECONDS = int(os.getenv('SECURITY_ROOM_CREATE_WINDOW_SECONDS', '3600'))

INSTALLED_APPS = [
    'daphne',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'channels',
    'core',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'core.security.SecurityEventMiddleware',
]

ROOT_URLCONF = 'aviator_system.urls'

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
            ],
        },
    },
]

WSGI_APPLICATION = 'aviator_system.wsgi.application'

DATABASE_URL = os.getenv('DATABASE_URL', '').strip()
if DATABASE_URL:
    DATABASES = {
        'default': dj_database_url.parse(
            DATABASE_URL,
            conn_max_age=int(os.getenv('DATABASE_CONN_MAX_AGE', '600')),
            ssl_require=not DEBUG,
        ),
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': os.getenv('SQLITE_DATABASE_PATH') or BASE_DIR / 'db.sqlite3',
            'OPTIONS': {
                'timeout': 20,
            },
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {'min_length': 8},
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static', BASE_DIR / 'Assets']
STATIC_ROOT = BASE_DIR / 'staticfiles'

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_SECURE = not DEBUG
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG
SECURE_SSL_REDIRECT = os.getenv('DJANGO_SECURE_SSL_REDIRECT', 'False').lower() in ['true', '1', 'yes']
X_FRAME_OPTIONS = 'DENY'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_REDIRECT_URL = 'play'
LOGIN_URL = 'login'
LOGOUT_REDIRECT_URL = 'home'
PASSWORD_RESET_TIMEOUT = 3600

EMAIL_BACKEND = os.getenv(
    'EMAIL_BACKEND',
    'django.core.mail.backends.console.EmailBackend' if DEBUG else 'django.core.mail.backends.smtp.EmailBackend',
) or ('django.core.mail.backends.console.EmailBackend' if DEBUG else 'django.core.mail.backends.smtp.EmailBackend')
EMAIL_HOST = os.getenv('EMAIL_HOST') or 'localhost'
EMAIL_PORT = int(os.getenv('EMAIL_PORT', '25'))
EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'False').lower() in ['true', '1', 'yes']
EMAIL_USE_SSL = os.getenv('EMAIL_USE_SSL', 'False').lower() in ['true', '1', 'yes']
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'Pakabet <no-reply@localhost>')

ASGI_APPLICATION = 'aviator_system.asgi.application'

CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            'hosts': [os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/0')],
        },
    } if os.getenv('DJANGO_USE_REDIS', 'False').lower() in ['true', '1', 'yes'] else {
        'BACKEND': 'channels.layers.InMemoryChannelLayer'
    }
}

IOTEC_PAY_MODE = os.getenv('IOTEC_PAY_MODE', 'sandbox')
IOTEC_PAY_API_BASE_URL = os.getenv('IOTEC_PAY_API_BASE_URL', 'https://pay.iotec.io')
IOTEC_PAY_TOKEN_URL = os.getenv('IOTEC_PAY_TOKEN_URL', 'https://id.iotec.io/connect/token')
IOTEC_PAY_CLIENT_ID = os.getenv('IOTEC_PAY_CLIENT_ID', '')
IOTEC_PAY_CLIENT_SECRET = os.getenv('IOTEC_PAY_CLIENT_SECRET', '')
IOTEC_PAY_WALLET_ID = os.getenv('IOTEC_PAY_WALLET_ID', '')
IOTEC_PAY_CURRENCY = os.getenv('IOTEC_PAY_CURRENCY', 'UGX')
IOTEC_PAY_CHANNEL = os.getenv('IOTEC_PAY_CHANNEL', 'Api')
IOTEC_PAY_CHARGE_CATEGORY = os.getenv('IOTEC_PAY_CHARGE_CATEGORY', 'ChargeCustomer')
IOTEC_PAY_TIMEOUT_SECONDS = int(os.getenv('IOTEC_PAY_TIMEOUT_SECONDS', '20'))
IOTEC_PAY_CALLBACK_TOKEN = os.getenv('IOTEC_PAY_CALLBACK_TOKEN', '')
