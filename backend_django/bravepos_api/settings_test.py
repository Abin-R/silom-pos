"""Test-only settings.  NEVER used by the running app or any deploy path.

The default ``DATABASES`` points at the shared *production* Azure Postgres, so
``manage.py test`` with the normal settings would create (and drop) a
``test_instamator3`` database ON THE PRODUCTION SERVER.  This module forces a
disposable in-memory SQLite database instead, so tests can run on any machine
with zero setup and cannot reach production.

Usage:
    python manage.py test bravepos --settings=bravepos_api.settings_test

Fidelity caveat: SQLite ignores ``SELECT ... FOR UPDATE``, so the concurrency
race tests are skipped here (they carry an ``@skipUnless(postgresql)`` guard).
Run those against a local Postgres before treating the feature as verified.
"""
import os

# The real settings module reads these at import time; provide throwaway values
# so importing it never requires (or touches) the production credentials.  They
# are unused — DATABASES is fully overridden below.
os.environ.setdefault('DATABASE_USER', 'test')
os.environ.setdefault('DATABASE_PASSWORD', 'test')
os.environ.setdefault('DATABASE_HOST', 'localhost')

from .settings import *  # noqa: F401,F403,E402

# Disposable, in-memory, local.  Cannot reach the shared Postgres.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

# Keep the test run self-contained: no error reporting, faster password hashing.
SENTRY_DSN = None
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
