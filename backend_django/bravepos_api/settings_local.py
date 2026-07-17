"""Local-dev settings for hand-testing in a browser.  NOT for production.

Same guard rail as settings_test.py: the default DATABASES points at the shared
*production* Postgres, so this overrides it with a throwaway on-disk SQLite file
that lives OUTSIDE the repo.  Nothing here can reach production data.

Usage:
    python manage.py migrate  --settings=bravepos_api.settings_local
    python manage.py runserver 0.0.0.0:8000 --settings=bravepos_api.settings_local
"""
import os

os.environ.setdefault('DATABASE_USER', 'local')
os.environ.setdefault('DATABASE_PASSWORD', 'local')
os.environ.setdefault('DATABASE_HOST', 'localhost')

from .settings import *  # noqa: F401,F403,E402

# On-disk SQLite (persists across runserver reloads, unlike :memory:), kept out
# of the repo tree.  Path overridable so it can live in the scratchpad.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.environ.get('DEV_DB_PATH', str(BASE_DIR / 'local_dev.sqlite3')),  # noqa: F405
    }
}

# Public origin the customer's phone actually reaches this box at.  Feeds the
# self-order QR and the card redirect URL; must be the LAN address in dev, not
# the production host.
_base = os.environ.get('PUBLIC_BASE_URL')
if _base:
    BRAVEPOS = {**BRAVEPOS, 'PUBLIC_BASE_URL': _base.rstrip('/')}  # noqa: F405

SENTRY_DSN = None
DEBUG = True
ALLOWED_HOSTS = ['*']
