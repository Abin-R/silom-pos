"""Backoffice authentication — against `bravepos.Staff`, not `auth_user`.

The Azure Postgres this Django project lives on is shared with two other
Django apps (`home`, `instamator_app`). Their fixtures/seed scripts keep
rewriting rows in the shared `auth_user` table, which is why the built-in
Django auth password for our admin user kept flipping to random values.

`bravepos_staff` is app-owned — nothing else on the box touches it. This
backend uses it as the sole source of truth for backoffice login.

The API surface matches Django's `ModelBackend`:

* `authenticate(request, username=..., password=...)` — the AuthenticationForm
  passes the login field verbatim as `username`; we look it up as an email.
* `get_user(user_id)` — called on every subsequent request via
  `AuthenticationMiddleware` to hydrate `request.user` from the session.
"""
from __future__ import annotations

from bravepos.models import Staff


class StaffBackend:
    """Authenticate a backoffice user against the `bravepos_staff` table."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not username or not password:
            return None
        try:
            staff = Staff.objects.get(email__iexact=username.strip())
        except Staff.DoesNotExist:
            return None
        if not staff.active:
            return None
        if not staff.check_password(password):
            return None
        return staff

    def get_user(self, user_id):
        try:
            staff = Staff.objects.get(pk=user_id)
        except (Staff.DoesNotExist, ValueError):
            return None
        return staff if staff.active else None
