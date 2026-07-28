"""Signal receivers for the bravepos app. Wired in ``BraveposConfig.ready``."""
from __future__ import annotations

from django.db.models.signals import post_save
from django.dispatch import receiver

from . import audit
from .models import Branch
from .staff_provisioning import ensure_branch_staff

# Audit capture — hooks post_save/post_delete on the models listed in
# ``audit.AUDITED`` plus Django's auth signals. Idempotent (every receiver
# carries a dispatch_uid), so a double ``ready()`` can't double-log.
audit.connect()


@receiver(post_save, sender=Branch)
def provision_branch_staff(sender, instance, created, raw=False, **kwargs):
    """When a new branch is created, give it its own Admin + Cashier.

    Fires for every creation path — backoffice form, POS API, or shell —
    so a branch never exists without a login. Skipped on plain updates and
    on fixture loads (``raw``)."""
    if created and not raw:
        ensure_branch_staff(instance)
