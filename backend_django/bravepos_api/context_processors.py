"""Template context shared outside the backoffice app."""
import os

from django.conf import settings


def selforder_sentry(request):
    """Browser Sentry config for the customer self-order pages.

    Same gate as the server side: no DSN or DEBUG on means no script tag, so
    a dev machine never reports a local page's errors.  Only
    selforder/base.html reads it.
    """
    dsn = getattr(settings, 'SENTRY_SELFORDER_DSN', None)
    if not dsn or settings.DEBUG:
        return {}
    return {'selforder_sentry': {
        'dsn': dsn,
        'environment': os.environ.get('SENTRY_ENV', 'production'),
        'release': os.environ.get('SENTRY_RELEASE') or '',
    }}
