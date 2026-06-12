"""Per-branch staff provisioning.

Every branch gets its own Admin + Cashier (scoped to that branch via the
Staff.branches M2M), created automatically when the branch is created
(see :mod:`bravepos.signals`). For now all branches share the same default
PINs / passwords — change these once real per-branch credentials are needed.

The app login picker (``/api/auth/branch-users``) lists these branch-scoped
staff; the PIN pad logs them in. PIN is the only credential used at login —
the password is kept only so a Django-admin/script flow can still seed.
"""
from __future__ import annotations

# Shared defaults — identical across every branch on purpose (see docstring).
DEFAULT_ADMIN_PIN = "1234"
DEFAULT_CASHIER_PIN = "0000"
DEFAULT_ADMIN_PASSWORD = "admin1234"
DEFAULT_CASHIER_PASSWORD = "cashier1234"


def ensure_branch_staff(branch):
    """Idempotently provision a per-branch Admin + Cashier for ``branch``.

    Safe to re-run: staff are matched by their per-branch email, so a second
    call just re-asserts the branch link instead of creating duplicates.
    """
    from .models import Staff

    # Stable, unique slug for the per-branch emails. Prefer the branch code;
    # fall back to a short slice of the UUID so it's always unique.
    slug = (branch.code or "").strip() or str(branch.id)[:8]
    specs = [
        ("admin", f"admin.{slug}@rollingpinn.com", "Admin",
         DEFAULT_ADMIN_PIN, DEFAULT_ADMIN_PASSWORD),
        ("cashier", f"cashier.{slug}@rollingpinn.com", "Cashier",
         DEFAULT_CASHIER_PIN, DEFAULT_CASHIER_PASSWORD),
    ]
    for role, email, name, pin, password in specs:
        staff, created = Staff.objects.get_or_create(
            email=email,
            defaults={"name": name, "role": role, "active": True},
        )
        if created:
            staff.set_pin(pin)
            staff.set_password(password)
            staff.save()
        staff.branches.add(branch)
