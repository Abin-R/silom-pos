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


# ── Sentry (error + performance monitoring) ─────────────────────────────────
# Needs a SENTRY_DSN *and* DEBUG off.  The DEBUG gate is the important half:
# a dev machine that happens to have the DSN in its .env (to test reporting,
# or just left over) would otherwise ship every local traceback and drown the
# real issues.  The server runs DJANGO_DEBUG=0, so production still reports.
# To check reporting from your machine, run once with DJANGO_DEBUG=0 and set
# SENTRY_ENV to something other than `production` so it stays filterable.
SENTRY_DSN = os.environ.get('SENTRY_DSN')
if SENTRY_DSN and not DEBUG:
    import sentry_sdk
    from django.core.exceptions import DisallowedHost

    from .sentry_filters import before_send as _sentry_before_send

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=os.environ.get('SENTRY_ENV', 'production'),
        # Performance: fraction of requests traced. 1.0 = every request while
        # we're getting started; dial down (e.g. 0.2) once traffic grows.
        traces_sample_rate=float(os.environ.get('SENTRY_TRACES_RATE', '1.0')),
        # POS payloads can carry customer/order data — keep PII out of Sentry.
        send_default_pii=False,
        # Internet background noise: bots hit the server's raw IP, Django
        # rejects the Host header, Sentry pages us about it.  Nothing we can
        # fix in code — the request never belonged to us.
        ignore_errors=[DisallowedHost],
        # One broken deploy used to mean 1,041 copies of the same traceback,
        # because the tablets poll and every poll re-raised it.  This collapses
        # a storm onto a log scale (~11 events for that same outage) without
        # muting a fault that only fires once.  See sentry_filters.
        before_send=_sentry_before_send,
        # Tie every event to the commit actually running, so "is this fixed
        # yet?" is answerable from the issue page.  The deploy script exports
        # it; falling back to unset is fine (Sentry just omits the field).
        release=os.environ.get('SENTRY_RELEASE') or None,
    )


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
    # Custom Staff-aware auth middleware. Django's default
    # AuthenticationMiddleware hydrates request.user by int-coercing the
    # session's PK, which blows up on our UUID-keyed Staff. See
    # backoffice/middleware.py for the reasoning.
    'backoffice.middleware.StaffAuthMiddleware',
    # Must sit after StaffAuthMiddleware — it reads request.user to attribute
    # audit rows to the signed-in staff member.
    'backoffice.middleware.AuditContextMiddleware',
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
                # Shop name + the counts the backoffice rail carries.
                'backoffice.context_processors.nav',
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
        # Persistent connections get killed out from under us — Postgres
        # restarts, and the network path between here and the DB drops idle
        # sockets.  Without this, the next request to reuse a dead connection
        # raises `SSL SYSCALL error: EOF detected` instead of reconnecting.
        # Django 4.1+ pings the connection first and transparently reopens it.
        'CONN_HEALTH_CHECKS': True,
        'OPTIONS': {
            'sslmode': 'require',
            # Default is no timeout: if the DB host stops answering, gunicorn
            # workers block until the 120s request timeout and the whole POS
            # stalls.  Fail in 10s so the tablet gets an error it can retry.
            'connect_timeout': 10,
        },
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
# Custom response headers are invisible to browser JS unless listed here, and
# the web build is cross-origin (8081 -> 8000). Without this the Orders pager
# cannot read the total and would show no page count.
CORS_EXPOSE_HEADERS = ['X-Total-Count']


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

# Product images are stored as base64 data URLs in a normal form field (the
# backoffice product form has no file-upload pipeline). The form now downscales
# images client-side to ~tens of KB, but editing a product saved before that
# change re-posts its original (possibly multi-MB) data URL. Lift the request
# body cap from Django's 2.5 MB default so those edits don't 400.
DATA_UPLOAD_MAX_MEMORY_SIZE = 15 * 1024 * 1024  # 15 MB

# ── Auth (backoffice only) ──────────────────────────────────────────────────
# The DRF-based POS API at /api/* uses its own PIN/token auth; these settings
# only affect server-rendered /backoffice/* pages, which require a login.
#
# Auth backend: `bravepos.Staff` via `backoffice.auth_backend.StaffBackend`,
# NOT the shared `auth_user` table. The Postgres box is shared with `home`
# and `instamator_app`; their seed scripts kept rewriting our admin row in
# `auth_user`, breaking login. `bravepos_staff` is app-owned and stable.
AUTHENTICATION_BACKENDS = [
    'backoffice.auth_backend.StaffBackend',
]
LOGIN_URL = '/backoffice/login/'
LOGIN_REDIRECT_URL = '/backoffice/'
LOGOUT_REDIRECT_URL = '/backoffice/login/'

# Brave POS app config
BRAVEPOS = {
    # External payment gateway
    'BEAM_PLAYGROUND_URL': 'https://playground.api.beamcheckout.com',
    'BEAM_PRODUCTION_URL': 'https://api.beamcheckout.com',

    # Public origin this server is reachable at, used to build the absolute
    # URLs we hand to third parties: the self-order QR a customer scans, and
    # the redirectUrl Beam bounces their browser back to after a card payment.
    # Must be overridable — in local dev the customer's phone has to reach this
    # box, not the production host.
    'PUBLIC_BASE_URL': os.environ.get(
        'PUBLIC_BASE_URL', 'https://pos.rollingpinn.com',
    ).rstrip('/'),
}
