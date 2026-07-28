"""Backoffice authentication — against `bravepos.Staff`, not `auth_user`.

The Azure Postgres this Django project lives on is shared with two other
Django apps (`home`, `instamator_app`). Their fixtures/seed scripts keep
rewriting rows in the shared `auth_user` table, which is why the built-in
Django auth password for our admin user kept flipping to random values.

`bravepos_staff` is app-owned — nothing else on the box touches it. This
backend uses it as the sole source of truth for backoffice login.

The API surface matches Django's `ModelBackend`:

* `authenticate(request, username=..., password=...)` — the AuthenticationForm
  passes the login field verbatim as `username`. The identifier is matched
  against **either** `Staff.username` or `Staff.email`, so the same account
  works whichever mode the login page's identifier toggle is set to.
* `get_user(user_id)` — called on every subsequent request via
  `AuthenticationMiddleware` to hydrate `request.user` from the session.

Only rows with `backoffice_access=True` may sign in here. The flag does not
touch the POS PIN pad — a cashier keeps their in-app login either way.
"""
from __future__ import annotations

from functools import lru_cache

from django.contrib.auth.hashers import check_password, make_password
from django.db.models import Q
from django.utils import timezone

from bravepos.models import Staff


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """A real hash to verify against when no account matched.

    Without it, "no such user" returns in microseconds while "wrong password"
    spends a full KDF round — a timing gap that turns the login form into a
    username oracle. Computed once, lazily, so import stays cheap.
    """
    return make_password("timing-equaliser-not-a-real-credential")


class StaffBackend:
    """Authenticate a backoffice user against the `bravepos_staff` table."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not username or not password:
            return None
        identifier = username.strip()
        staff = (
            Staff.objects
            .filter(Q(username__iexact=identifier) | Q(email__iexact=identifier))
            .order_by("-backoffice_access")
            .first()
        )
        if staff is None:
            check_password(password, _dummy_hash())
            return None
        if not staff.active or not staff.backoffice_access:
            return None
        if not staff.check_password(password):
            return None
        # Surfaced in the Users list as "Last sign-in". Written with update()
        # so it can't fire the audit diff or rotate the session auth hash.
        Staff.objects.filter(pk=staff.pk).update(last_login_at=timezone.now())
        return staff

    def get_user(self, user_id):
        try:
            staff = Staff.objects.get(pk=user_id)
        except (Staff.DoesNotExist, ValueError):
            return None
        if not staff.active or not staff.backoffice_access:
            return None
        return staff
