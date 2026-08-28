"""Automatic audit logging — who changed what, when, from where.

Two halves:

* **Actor context** — a thread-local set by
  ``backoffice.middleware.AuditContextMiddleware`` at the top of every request
  and cleared at the end. Model signals fire deep inside view/serializer code
  with no access to the request, so this is how a save knows who did it.
  Nothing set (shell, management command, cron) simply records ``source=shell``.

* **Change capture** — ``pre_save`` snapshots the stored row, ``post_save``
  diffs it against the new one, and ``post_delete`` records the final state.
  Only models in ``AUDITED`` are watched; ``UPDATE_DELETE_ONLY`` members skip
  the create event because the row itself already is the record of creation
  (auditing every POS order would double write volume for no new information).

Secrets never land in the log — see ``REDACTED_FIELDS``.

Wired up in ``BraveposConfig.ready`` via ``bravepos.signals``.
"""
from __future__ import annotations

import logging
import threading
import uuid
from decimal import Decimal

from django.db import models
from django.db.models.signals import post_delete, post_save, pre_save
from django.utils import timezone

logger = logging.getLogger(__name__)

_local = threading.local()


# ── Which models are watched ────────────────────────────────────────────────
# Model names as they appear in ``Model.__name__``.
AUDITED = {
    "Staff", "Branch", "Settings",
    "Product", "Category", "Unit", "DrawerCategory", "StockOutReason",
    "StockMovement", "StockDocument", "StockDocumentItem",
    "Shift", "ShiftMovement",
    "Customer", "PeakProductMap", "AppRelease",
    "Order", "OrderItem", "SelfOrder",
}

# Creating one of these is already fully recorded by the row itself; only
# later mutations (voids, edits, refunds) and deletions are interesting.
UPDATE_DELETE_ONLY = {"Order", "OrderItem", "SelfOrder", "StockMovement"}

# Never audited: AuditLog itself (infinite recursion), BranchSession (its
# `last_seen_at` is touched on every single POS request — pure noise),
# ParkedOrder (scratch state), and the Peak request/token plumbing.
NEVER = {"AuditLog", "BranchSession", "ParkedOrder", "PeakRequest", "PeakClientToken"}

# Values replaced with "***" before storage.
REDACTED_FIELDS = {
    "password", "password_hash", "pin", "pin_hash", "token",
    "beam_api_key", "omise_secret_key", "omise_public_key",
    "beam_merchant_id", "peak_client_secret", "secret", "api_key",
}

# Churny bookkeeping columns — excluded from diffs so they don't manufacture
# an audit row every time something unrelated is touched.
IGNORED_FIELDS = {"last_seen_at", "last_login_at", "updated_at", "modified_at"}


# ── Actor context ───────────────────────────────────────────────────────────
def set_context(*, actor=None, source="", method="", path="", ip=None, user_agent=""):
    _local.ctx = {
        "actor": actor,
        "actor_resolver": None,
        "source": source,
        "method": method,
        "path": path[:300],
        "ip": ip,
        "user_agent": (user_agent or "")[:300],
    }


def set_context_actor_resolver(fn):
    """Defer actor lookup until a row is actually written.

    ``request.user`` is a SimpleLazyObject backed by a DB query; resolving it
    eagerly for every request would add a SELECT to read-only page loads that
    never write an audit row.
    """
    ctx = getattr(_local, "ctx", None)
    if ctx is not None:
        ctx["actor_resolver"] = fn


def get_context() -> dict:
    return getattr(_local, "ctx", None) or {}


def clear_context():
    _local.ctx = None


def set_actor(actor):
    """Attach an actor to an already-open context — used by the POS API, which
    only resolves the staff member after the session token is validated."""
    ctx = getattr(_local, "ctx", None)
    if ctx is None:
        set_context(actor=actor, source="api")
    else:
        ctx["actor"] = actor


class pause:
    """Context manager that suspends audit capture on this thread.

    For bulk/backfill work where a few thousand rows of "system changed
    everything" would bury the entries a human actually needs.
    """

    def __enter__(self):
        self._prev = getattr(_local, "paused", False)
        _local.paused = True
        return self

    def __exit__(self, *exc):
        _local.paused = self._prev
        return False


def _paused() -> bool:
    return getattr(_local, "paused", False)


# ── Serialisation ───────────────────────────────────────────────────────────
def _json_safe(value):
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _redact(field_name: str, value):
    lowered = field_name.lower()
    if any(secret in lowered for secret in REDACTED_FIELDS):
        return "***" if value not in (None, "") else value
    return _json_safe(value)


def _label(instance) -> str:
    try:
        return str(instance)[:250]
    except Exception:
        return f"{instance.__class__.__name__}({instance.pk})"


def _branch_id_of(instance):
    """Best-effort branch attribution so the log can be filtered per shop.

    Reads the raw ``branch_id`` column rather than the ``branch`` descriptor —
    the latter would fire a SELECT on every audited save.
    """
    branch_id = getattr(instance, "branch_id", None)
    if branch_id is not None:
        return branch_id
    if instance.__class__.__name__ == "Branch":
        return instance.pk
    return None


def _normalise(field, value):
    """Make an in-memory value comparable with the same value loaded from the
    database.

    Without this, ``product.price = 120`` diffs as ``"100.00" → 120`` — the
    stored side is a quantised ``Decimal``, the assigned side a bare int — and
    every field with a numeric default reports a phantom change on any save.
    """
    if value is None:
        return None
    if isinstance(field, models.DecimalField):
        try:
            quantum = Decimal(1).scaleb(-field.decimal_places)
            return Decimal(str(value)).quantize(quantum)
        except Exception:
            return value
    return value


def _field_values(instance) -> dict:
    """Concrete local fields as a redacted, JSON-safe dict. M2M excluded —
    those arrive separately through ``m2m_changed``."""
    out = {}
    for field in instance._meta.concrete_fields:
        name = field.name
        if name in IGNORED_FIELDS:
            continue
        try:
            value = getattr(instance, field.attname)
        except Exception:
            continue
        out[name] = _redact(name, _normalise(field, value))
    return out


# ── Writing ─────────────────────────────────────────────────────────────────
def record(action, *, instance=None, model="", object_id="", object_label="",
           changes=None, branch_id=None, note="", actor=None):
    """Write one audit row. Never raises — a logging failure must not take a
    real request down with it."""
    from .models import AuditLog

    try:
        ctx = get_context()
        actor = actor or ctx.get("actor")
        if actor is None and ctx.get("actor_resolver"):
            try:
                actor = ctx["actor_resolver"]()
            except Exception:
                actor = None
        if actor is not None and not getattr(actor, "pk", None):
            actor = None

        if instance is not None:
            model = model or instance.__class__.__name__
            object_id = object_id or str(getattr(instance, "pk", "") or "")
            object_label = object_label or _label(instance)
            branch_id = branch_id or _branch_id_of(instance)

        AuditLog.objects.create(
            at=timezone.now(),
            actor=actor,
            actor_label=(getattr(actor, "name", "") or getattr(actor, "login_id", "") or "")[:200],
            actor_role=(getattr(actor, "role", "") or "")[:32],
            action=action,
            model=model[:64],
            object_id=str(object_id)[:64],
            object_label=(object_label or "")[:250],
            branch_id=branch_id,
            changes=changes or {},
            source=(ctx.get("source") or "shell")[:16],
            method=(ctx.get("method") or "")[:8],
            path=(ctx.get("path") or "")[:300],
            ip=ctx.get("ip"),
            user_agent=(ctx.get("user_agent") or "")[:300],
            note=note[:300],
        )
    except Exception:  # pragma: no cover — defensive
        logger.exception("audit: failed to record %s on %s", action, model)


# ── Signal receivers ────────────────────────────────────────────────────────
def _watched(sender) -> bool:
    name = sender.__name__
    return name in AUDITED and name not in NEVER


def on_pre_save(sender, instance, raw=False, **kwargs):
    """Snapshot the stored row onto the instance so ``on_post_save`` can diff
    it. Stored under a plain ``__dict__`` key rather than an attribute so name
    mangling doesn't vary per model class."""
    if raw or _paused() or not _watched(sender):
        return
    if instance.pk is None:
        instance.__dict__["_audit_before"] = None
        return
    try:
        stored = sender.objects.filter(pk=instance.pk).first()
    except Exception:
        stored = None
    instance.__dict__["_audit_before"] = (
        _field_values(stored) if stored is not None else None
    )


def on_post_save(sender, instance, created, raw=False, **kwargs):
    if raw or _paused() or not _watched(sender):
        return

    before = instance.__dict__.pop("_audit_before", None)

    if created:
        if sender.__name__ in UPDATE_DELETE_ONLY:
            return
        record("create", instance=instance, changes=_field_values(instance))
        return

    after = _field_values(instance)
    diff = {
        field: {"from": before.get(field), "to": value}
        for field, value in after.items()
        if before is not None and before.get(field) != value
    }
    if before is None:
        # Couldn't read the prior row (rare — concurrent delete). Record the
        # update anyway rather than silently dropping it.
        record("update", instance=instance, changes={}, note="prior state unavailable")
        return
    if not diff:
        return
    record("update", instance=instance, changes=diff)


def on_post_delete(sender, instance, **kwargs):
    if _paused() or not _watched(sender):
        return
    record("delete", instance=instance, changes=_field_values(instance))


def on_branches_changed(sender, instance, action, pk_set, reverse=False, **kwargs):
    """Staff ↔ Branch assignments — which shops an account can reach is a
    security-relevant change, and M2M edits don't show up in a row diff."""
    if _paused() or action not in ("post_add", "post_remove", "post_clear"):
        return
    if reverse or instance.__class__.__name__ != "Staff":
        return
    from .models import Branch

    names = list(
        Branch.objects.filter(pk__in=pk_set or []).values_list("name", flat=True)
    )
    verb = {"post_add": "added to", "post_remove": "removed from",
            "post_clear": "cleared from"}[action]
    record(
        "update", instance=instance,
        changes={"branches": {"from": None, "to": names}},
        note=f"branches {verb} {instance.name}"[:300],
    )


# ── Auth events ─────────────────────────────────────────────────────────────
def on_logged_in(sender, request, user, **kwargs):
    record("login", instance=user, object_label=user.get_username(),
           actor=user, note="backoffice sign-in")


def on_logged_out(sender, request, user, **kwargs):
    if user is None:
        return
    record("logout", instance=user, object_label=user.get_username(), actor=user)


def on_login_failed(sender, credentials, request=None, **kwargs):
    attempted = (credentials or {}).get("username") or ""
    record("login_failed", model="Staff", object_label=attempted[:250],
           note="bad username/email or password")


def connect():
    """Wire the receivers. Called once from ``bravepos.signals``."""
    from django.apps import apps
    from django.contrib.auth.signals import (
        user_logged_in, user_logged_out, user_login_failed,
    )
    from django.db.models.signals import m2m_changed

    for model in apps.get_app_config("bravepos").get_models():
        if model.__name__ not in AUDITED or model.__name__ in NEVER:
            continue
        uid = f"audit_{model.__name__}"
        pre_save.connect(on_pre_save, sender=model, dispatch_uid=uid)
        post_save.connect(on_post_save, sender=model, dispatch_uid=uid)
        post_delete.connect(on_post_delete, sender=model, dispatch_uid=uid)

    from .models import Staff

    m2m_changed.connect(
        on_branches_changed, sender=Staff.branches.through,
        dispatch_uid="audit_staff_branches",
    )
    user_logged_in.connect(on_logged_in, dispatch_uid="audit_login")
    user_logged_out.connect(on_logged_out, dispatch_uid="audit_logout")
    user_login_failed.connect(on_login_failed, dispatch_uid="audit_login_failed")
