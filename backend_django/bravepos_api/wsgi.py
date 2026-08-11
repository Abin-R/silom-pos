"""
WSGI config for bravepos_api project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.1/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'bravepos_api.settings')

application = get_wsgi_application()

# Force the URLconf to import *now*, at boot, instead of lazily on the first
# request.
#
# Django resolves `ROOT_URLCONF` through a `cached_property`, so by default the
# whole view layer is imported by whichever worker happens to serve request #1.
# On 2026-08-11 that worker was mid-`git pull`: it imported the new
# `bravepos.peak` against the old `bravepos.models` and raised ImportError.
# Python keeps half-built modules in `sys.modules`, so that worker then failed
# *every* request until the service was restarted hours later — 1,041 Sentry
# events for one bad moment.
#
# Importing here means a tree that cannot import fails while gunicorn is still
# booting (with `--preload`, before a single worker forks).  systemd retries,
# and the deploy either comes up clean or stays visibly down — never the silent
# middle state where a live worker serves 500s from a stale module cache.
from django.urls import get_resolver  # noqa: E402

get_resolver().url_patterns
