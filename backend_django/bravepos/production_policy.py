"""Effective-dated, append-only production request cut-off policies.

A request deadline cannot live only on a request row: a setting edit must not
reopen an already-closed date which has no request yet.  Policy rows therefore
cover every delivery date and are selected by their effective boundary.
"""
from __future__ import annotations

import datetime

from django.db import transaction
from django.utils import timezone

from .models import ProductionCutoffPolicy, Settings

DEFAULT_CUTOFF = datetime.time(23, 30)


def cutoff_time() -> datetime.time:
    """Return the currently displayed shop setting.

    This is retained for callers which need the configured value.  Deadline
    authorization must use :func:`locks_at` without an override so historic
    delivery dates resolve through their policy row instead.
    """
    row = Settings.objects.filter(id="shop").only("production_cutoff").first()
    return row.production_cutoff if row else DEFAULT_CUTOFF


def policy_for_date(delivery_date):
    """Return the latest policy in force for ``delivery_date``, if any."""
    return (
        ProductionCutoffPolicy.objects
        .filter(effective_from__lte=delivery_date)
        .order_by("-id")
        .first()
    )


def deadline_provenance(delivery_date) -> str:
    """Return the source for a delivery date's deadline.

    ``legacy_baseline`` means the migration recorded the adoption rule, not a
    recovered historic deadline.  UI callers can use this to avoid labelling a
    legacy date with an invented original closure time.
    """
    policy = policy_for_date(delivery_date)
    return policy.source if policy else ProductionCutoffPolicy.Source.LEGACY_BASELINE


def is_legacy_deadline(delivery_date) -> bool:
    """Whether the displayed legacy deadline was clamped at policy adoption.

    The migration's baseline policy is valid for future delivery dates, whose
    cutoff time remains known.  It cannot recover a deadline that was already
    in the past when the policy was adopted, so only that clamped case should
    be labelled as unavailable in the UI.
    """
    policy = policy_for_date(delivery_date)
    if policy is None or policy.source != ProductionCutoffPolicy.Source.LEGACY_BASELINE:
        return False
    arithmetic_deadline = timezone.make_aware(datetime.datetime.combine(
        delivery_date - datetime.timedelta(days=1), policy.cutoff,
    ))
    return arithmetic_deadline < policy.activated_at


def locks_at(delivery_date, cutoff=None) -> datetime.datetime:
    """Return the aware deadline for a delivery date in the shop timezone.

    Passing ``cutoff`` deliberately preserves the old arithmetic-only helper
    contract for tests.  Runtime callers must omit it so an effective-dated
    policy and its activation time are honoured.
    """
    policy = None
    if cutoff is None:
        policy = policy_for_date(delivery_date)
        cutoff = policy.cutoff if policy else cutoff_time()

    day_before = delivery_date - datetime.timedelta(days=1)
    deadline = timezone.make_aware(datetime.datetime.combine(day_before, cutoff))
    if policy is not None and policy.activated_at > deadline:
        return policy.activated_at
    return deadline


def is_open(delivery_date, now=None, cutoff=None) -> bool:
    """Whether a request remains editable. Equality with the deadline is closed."""
    return (now or timezone.now()) < locks_at(delivery_date, cutoff=cutoff)


def earliest_open_date(now=None, cutoff=None) -> datetime.date:
    """Return the first future delivery date which remains open."""
    now = now or timezone.now()
    day = timezone.localdate(now) + datetime.timedelta(days=1)
    while not is_open(day, now=now, cutoff=cutoff):
        day += datetime.timedelta(days=1)
    return day


def latest_locked_date(now=None, cutoff=None) -> datetime.date:
    """Return the kitchen's nearest delivery date with final request numbers."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    tomorrow = today + datetime.timedelta(days=1)
    return today if is_open(tomorrow, now=now, cutoff=cutoff) else tomorrow


def lock_settings() -> Settings:
    """Fetch the singleton settings row under a row lock.

    Call inside a short ``transaction.atomic()`` block.  The migration creates
    the singleton, but the get-or-create fallback keeps empty test databases
    and newly provisioned deployments safe before the first settings edit.
    """
    Settings.objects.get_or_create(id="shop")
    return Settings.objects.select_for_update().get(id="shop")


def update_cutoff(cutoff: datetime.time) -> ProductionCutoffPolicy:
    """Append a new cutoff policy and update the displayed settings value.

    The boundary is found using the old schedule while the settings singleton
    is locked.  This means an admin can alter still-open dates but cannot
    reopen dates which have already closed, even with no request row.
    """
    if not isinstance(cutoff, datetime.time):
        raise TypeError("cutoff must be a datetime.time")

    with transaction.atomic():
        settings = lock_settings()
        now = timezone.now()
        effective_from = earliest_open_date(now=now)
        policy = ProductionCutoffPolicy.objects.create(
            effective_from=effective_from,
            cutoff=cutoff,
            activated_at=now,
            source=ProductionCutoffPolicy.Source.ADMIN_CHANGE,
        )
        settings.production_cutoff = cutoff
        settings.save(update_fields=["production_cutoff"])
        return policy
