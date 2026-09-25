from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent

APP_NAME = os.getenv('APP_NAME', 'Pakabet')

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', 'django-insecure-replace-this-with-a-secure-key')

DEBUG = os.getenv('DJANGO_DEBUG', 'True').lower() in ['true', '1', 'yes']

ALLOWED_HOSTS = os.getenv('DJANGO_ALLOWED_HOSTS', '127.0.0.1,localhost,testserver').split(',')

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

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
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
STATICFILES_DIRS = [BASE_DIR / 'static']
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
LOGOUT_REDIRECT_URL = 'home'

ASGI_APPLICATION = 'aviator_system.asgi.application'

CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            'hosts': [('127.0.0.1', 6379)],
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
