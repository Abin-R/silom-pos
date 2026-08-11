"""Signal receivers for the bravepos app. Wired in ``BraveposConfig.ready``."""
from __future__ import annotations

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from . import audit
from .gateways import seed_branch_payment
from .models import DEFAULT_STOCK_OUT_REASONS, Branch, StockOutReason
from .staff_provisioning import ensure_branch_staff

# Audit capture — hooks post_save/post_delete on the models listed in
# ``audit.AUDITED`` plus Django's auth signals. Idempotent (every receiver
# carries a dispatch_uid), so a double ``ready()`` can't double-log.
audit.connect()


@receiver(pre_save, sender=Branch)
def seed_new_branch_payment(sender, instance, raw=False, **kwargs):
    """Give a brand-new branch the shop template's payment credentials.

    ``pre_save`` rather than ``post_save`` so the credentials land in the same
    INSERT — a branch is never briefly visible without a payment config. Fires
    for every creation path (backoffice form, POS API, shell); skipped on plain
    updates and on fixture loads (``raw``), and a no-op if the caller already
    supplied credentials."""
    if instance._state.adding and not raw:
        seed_branch_payment(instance)


@receiver(post_save, sender=Branch)
def provision_branch_staff(sender, instance, created, raw=False, **kwargs):
    """When a new branch is created, give it its own Admin + Cashier.

    Fires for every creation path — backoffice form, POS API, or shell —
    so a branch never exists without a login. Skipped on plain updates and
    on fixture loads (``raw``)."""
    if created and not raw:
        ensure_branch_staff(instance)


@receiver(post_save, sender=Branch)
def seed_new_branch_stock_out_reasons(sender, instance, created, raw=False, **kwargs):
    """Give a new branch the default stock-out reason codes.

    Migration 0034 seeds the branches that existed when the feature shipped;
    without this receiver every branch opened afterwards would have an empty
    Reason dropdown, and since the app disables Save until a reason is picked,
    that branch could not record a stock-out at all.

    The API deliberately still accepts a blank reason — an older app build that
    has not taken the update yet must keep working, so the requirement lives in
    the UI rather than as a 400 from the server.

    ``get_or_create`` per row rather than ``bulk_create`` so re-saving a branch
    can never duplicate the list."""
    if not created or raw:
        return
    for i, (name, name_th) in enumerate(DEFAULT_STOCK_OUT_REASONS):
        StockOutReason.objects.get_or_create(
            branch=instance, name=name,
            defaults={"name_th": name_th, "sort_order": i},
        )
