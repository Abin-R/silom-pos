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


def _client_ip(request):
    """Real client IP behind nginx. `REMOTE_ADDR` is the proxy on this box, so
    the left-most `X-Forwarded-For` entry is the caller."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        candidate = forwarded.split(",")[0].strip()
        if candidate:
            return candidate
    return request.META.get("REMOTE_ADDR") or None


class AuditContextMiddleware:
    """Opens an audit context for the duration of each request.

    The model signals in ``bravepos.audit`` fire far from the request — inside
    serializers, ``save()`` calls, management helpers — so this thread-local is
    how a write learns who made it and from where. Cleared in a ``finally`` so
    a thread returning to the pool can never inherit the previous request's
    actor.

    Must come **after** ``StaffAuthMiddleware`` so ``request.user`` exists.
    ``request.user`` is only touched lazily, at the moment an audit row is
    actually written, so read-only requests still cost no extra query.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from bravepos import audit

        path = request.path or ""
        source = "backoffice" if path.startswith("/backoffice") else "api"
        audit.set_context(
            actor=None,
            source=source,
            method=request.method or "",
            path=path,
            ip=_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
        # Backoffice requests are session-authenticated, so the actor is known
        # up front. POS API requests resolve theirs later, in
        # `bravepos.views.get_session`, once the bearer token is validated.
        if source == "backoffice":
            audit.set_context_actor_resolver(lambda: _authenticated_or_none(request))
        try:
            return self.get_response(request)
        finally:
            audit.clear_context()


def _authenticated_or_none(request):
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return user


# What a "viewer" account may open: the Overview and Reports sections of the
# rail, their exports, and the pages those reports link into. An allowlist, not
# a blocklist — a page added to the backoffice later stays closed to viewers
# until someone decides it is a report.
VIEWER_URL_NAMES = frozenset({
    "login", "logout", "app_css", "favicon", "apple_touch_icon", "product_image",
    "home", "dashboard",
    "transactions", "transactions_export", "receipt_print",
    "report_daily", "report_daily_export",
    "report_daily_detail", "report_daily_detail_export",
    "report_sell", "report_sell_export",
    "report_sku", "report_sku_export",
    "report_tax", "report_tax_export",
    "inventory", "inventory_export",
    "stock_in", "stock_in_export", "stock_in_document",
    "stock_out", "stock_out_export", "stock_out_document",
})


class ViewerAccessMiddleware:
    """Keeps a ``role == "viewer"`` account to read-only reports.

    Enforced here rather than per view because the backoffice has ~70 views
    that only say ``@login_required``; one gate in front of all of them can't
    be forgotten on the next one. Any non-GET is refused too (bar signing
    out), so even an allowlisted page can't be used to change anything.

    Must come after ``StaffAuthMiddleware``. Only backoffice URLs are touched —
    the till API has its own token auth, and viewers are refused there at login.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        match = getattr(request, "resolver_match", None)
        if match is None or match.namespace != "backoffice":
            return None
        if getattr(request.user, "role", "") != "viewer":
            return None
        read = request.method in ("GET", "HEAD")
        if match.url_name in VIEWER_URL_NAMES and (read or match.url_name == "logout"):
            return None
        from .views import viewer_forbidden
        return viewer_forbidden(request)
