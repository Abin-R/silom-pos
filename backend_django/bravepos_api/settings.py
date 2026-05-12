"""Django settings for bravepos_api.

When this lands on the shared Azure Postgres later, only the DATABASES dict
changes; everything else stays the same.  All Brave POS tables are namespaced
under the `bravepos` app label, so they will not collide with the other Django
apps that share the database.
"""
from pathlib import Path
import os

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')


SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'django-insecure-dev-only-CHANGE-IN-PRODUCTION',
)
DEBUG = os.environ.get('DJANGO_DEBUG', '1') == '1'
ALLOWED_HOSTS = os.environ.get('DJANGO_ALLOWED_HOSTS', '*').split(',')


INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'django.contrib.staticfiles',
    # Third-party
    'rest_framework',
    'corsheaders',
    # Brave POS app — table prefix `bravepos_*`
    'bravepos.apps.BraveposConfig',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'bravepos_api.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
            ],
        },
    },
]

WSGI_APPLICATION = 'bravepos_api.wsgi.application'


# ── Database ────────────────────────────────────────────────────────────────
# Local dev: SQLite (zero setup).
# Production: set DATABASE_URL or the individual POSTGRES_* env vars and switch
# the ENGINE to django.db.backends.postgresql.  Migrations only ever create/
# alter tables prefixed with `bravepos_` because of the app label.
if os.environ.get('POSTGRES_HOST'):
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.environ['POSTGRES_DB'],
            'USER': os.environ['POSTGRES_USER'],
            'PASSWORD': os.environ['POSTGRES_PASSWORD'],
            'HOST': os.environ['POSTGRES_HOST'],
            'PORT': os.environ.get('POSTGRES_PORT', '5432'),
            'CONN_MAX_AGE': 60,
            'OPTIONS': {'sslmode': os.environ.get('POSTGRES_SSLMODE', 'prefer')},
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }


# ── DRF ─────────────────────────────────────────────────────────────────────
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [],   # PIN-based auth lives in the views
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.AllowAny',
    ],
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    'DEFAULT_PARSER_CLASSES': [
        'rest_framework.parsers.JSONParser',
    ],
    # Return Decimal fields as JSON numbers instead of strings.  The frontend
    # uses .toFixed(2) and arithmetic on price/stock/totals — getting strings
    # back crashes the React Native app (Drawer screen most visibly).  Numbers
    # are also what the legacy FastAPI backend returned.
    'COERCE_DECIMAL_TO_STRING': False,
}


# ── CORS ────────────────────────────────────────────────────────────────────
# Matches the old FastAPI behaviour (`allow_origins=["*"]`).  In production
# we'll restrict to the actual web origin once that's stable.
CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = False


# ── Misc ────────────────────────────────────────────────────────────────────
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Bangkok'
USE_I18N = True
USE_TZ = True
STATIC_URL = 'static/'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Frontend calls /api/categories (no trailing slash); the router below is
# configured to match.  Disabling APPEND_SLASH avoids 301-redirecting POSTs,
# which silently drops the request body.
APPEND_SLASH = False

# Brave POS app config
BRAVEPOS = {
    # External payment gateway
    'BEAM_PLAYGROUND_URL': 'https://playground.api.beamcheckout.com',
    'BEAM_PRODUCTION_URL': 'https://api.beamcheckout.com',
}
