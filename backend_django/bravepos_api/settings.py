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


# ── Sentry (error + performance monitoring) ─────────────────────────────────
# Only initialises when SENTRY_DSN is set, so local dev stays quiet.  The
# Django integration is auto-enabled, capturing unhandled 500s and request
# transactions (performance).  Set SENTRY_DSN + SENTRY_ENV in the VM's .env.
SENTRY_DSN = os.environ.get('SENTRY_DSN')
if SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=os.environ.get('SENTRY_ENV', 'production'),
        # Performance: fraction of requests traced. 1.0 = every request while
        # we're getting started; dial down (e.g. 0.2) once traffic grows.
        traces_sample_rate=float(os.environ.get('SENTRY_TRACES_RATE', '1.0')),
        # POS payloads can carry customer/order data — keep PII out of Sentry.
        send_default_pii=False,
    )


SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'django-insecure-dev-only-CHANGE-IN-PRODUCTION',
)
DEBUG = os.environ.get('DJANGO_DEBUG', '1') == '1'
ALLOWED_HOSTS = os.environ.get('DJANGO_ALLOWED_HOSTS', '*').split(',')


INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    # Sessions + messages are required for the backoffice login flow
    # (django.contrib.auth.views.LoginView reads session and flashes
    # messages on success/failure).
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.humanize',
    'django.contrib.staticfiles',
    # Third-party
    'rest_framework',
    'corsheaders',
    # Brave POS app — table prefix `bravepos_*`
    'bravepos.apps.BraveposConfig',
    # Server-rendered backoffice (Bootstrap + Chart.js). Read-only views
    # against the bravepos models. No tables of its own.
    'backoffice.apps.BackofficeConfig',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
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
                # Required by login_required / template `user`/`messages`
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'bravepos_api.wsgi.application'


# ── Database ────────────────────────────────────────────────────────────────
# Shared Azure Postgres `instamator3`, also used by `home` and `instamator_app`.
# All Brave POS tables are prefixed `bravepos_*` via the app label, so
# migrations only ever touch our own tables — never the other servers'.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'instamator3',
        'USER': os.environ['DATABASE_USER'],
        'PASSWORD': os.environ['DATABASE_PASSWORD'],
        'HOST': os.environ['DATABASE_HOST'],
        'PORT': '5432',
        'CONN_MAX_AGE': 60,
        'OPTIONS': {'sslmode': 'require'},
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

# ── Auth (backoffice only) ──────────────────────────────────────────────────
# The DRF-based POS API at /api/* uses its own PIN/token auth; these settings
# only affect server-rendered /backoffice/* pages, which require a Django
# admin/staff login.
LOGIN_URL = '/backoffice/login/'
LOGIN_REDIRECT_URL = '/backoffice/'
LOGOUT_REDIRECT_URL = '/backoffice/login/'

# Brave POS app config
BRAVEPOS = {
    # External payment gateway
    'BEAM_PLAYGROUND_URL': 'https://playground.api.beamcheckout.com',
    'BEAM_PRODUCTION_URL': 'https://api.beamcheckout.com',
}
