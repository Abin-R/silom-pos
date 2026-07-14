"""Session-backed auth for backoffice Staff users.

Django's default ``AuthenticationMiddleware`` hydrates ``request.user`` via
``get_user()`` which does::

    get_user_model()._meta.pk.to_python(session[SESSION_KEY])

That path assumes the User model's PK type matches the session-stored value.
Our Staff PK is a UUID but ``get_user_model()`` returns ``auth.User`` (integer
PK), so the ``int()`` conversion blows up with ``ValidationError`` on every
authenticated request. We can't set ``AUTH_USER_MODEL = 'bravepos.Staff'``
either — Staff doesn't inherit ``AbstractBaseUser`` and shouldn't, since it
already models a per-branch PIN-login staff record.

This middleware bypasses the coercion entirely. It reads ``SESSION_KEY``,
looks the Staff up by UUID directly, and verifies the session-auth hash
(same rotation check Django does — Staff invalidation on password change).
"""
from __future__ import annotations

from django.conf import settings
from django.contrib.auth import (
    BACKEND_SESSION_KEY,
    HASH_SESSION_KEY,
    SESSION_KEY,
    load_backend,
)
from django.contrib.auth.models import AnonymousUser
from django.utils.crypto import constant_time_compare
from django.utils.functional import SimpleLazyObject


def _resolve_staff(request):
    session_key = request.session.get(SESSION_KEY)
    backend_path = request.session.get(BACKEND_SESSION_KEY)
    if not session_key or backend_path not in settings.AUTHENTICATION_BACKENDS:
        return AnonymousUser()
    backend = load_backend(backend_path)
    user = backend.get_user(session_key)
    if user is None:
        return AnonymousUser()
    # Session-auth-hash rotation check: if the Staff password changes, all
    # previously-issued sessions become invalid.
    stored_hash = request.session.get(HASH_SESSION_KEY) or ""
    fresh_hash = user.get_session_auth_hash()
    if not constant_time_compare(stored_hash, fresh_hash):
        request.session.flush()
        return AnonymousUser()
    return user


class StaffAuthMiddleware:
    """Drop-in replacement for ``django.contrib.auth.middleware.AuthenticationMiddleware``
    that hydrates ``request.user`` as a Staff instance (via the configured
    AUTHENTICATION_BACKENDS) instead of going through ``get_user_model()``."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.user = SimpleLazyObject(lambda: _resolve_staff(request))
        return self.get_response(request)
