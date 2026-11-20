"""Effective-dated, append-only production request cut-off policies.

A request deadline cannot live only on a request row: a setting edit must not
reopen an already-closed date which has no request yet.  Policy rows therefore
cover every delivery date and are selected by their effective boundary.
"""
from __future__ import annotations

import datetime

from django.db import transaction
from django.utils import timezone

from .models import Branch, ProductionCutoffPolicy, ProductionLeadTime, Settings

DEFAULT_CUTOFF = datetime.time(23, 30)
DEFAULT_LEAD_DAYS = 1
MAX_LEAD_DAYS = 30


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


def lead_policy_for(branch, delivery_date):
    """The branch's lead-time row in force for ``delivery_date``, if any."""
    if branch is None:
        return None
    return (
        ProductionLeadTime.objects
        .filter(branch=branch, effective_from__lte=delivery_date)
        .order_by("-id")
        .first()
    )


def locks_at(delivery_date, cutoff=None, branch=None) -> datetime.datetime:
    """Return the aware deadline for a delivery date in the shop timezone:
    the cut-off time, the branch's lead time in days before delivery.

    Without a branch the lead time is the original 1 day.  Passing ``cutoff``
    deliberately preserves the old arithmetic-only helper contract for tests
    (the branch's lead time still applies, its activation clamp does not).
    Runtime callers must omit it so effective-dated policies and their
    activation times are honoured: a rule activated after a date's arithmetic
    deadline closes that date at activation, never retroactively earlier.
    """
    clamps = []
    if cutoff is None:
        policy = policy_for_date(delivery_date)
        cutoff = policy.cutoff if policy else cutoff_time()
        if policy is not None:
            clamps.append(policy.activated_at)
        lead = lead_policy_for(branch, delivery_date)
        if lead is not None:
            clamps.append(lead.activated_at)
        lead_days = lead.lead_days if lead is not None else DEFAULT_LEAD_DAYS
    else:
        lead = lead_policy_for(branch, delivery_date)
        lead_days = lead.lead_days if lead is not None else DEFAULT_LEAD_DAYS

    day = delivery_date - datetime.timedelta(days=lead_days)
    deadline = timezone.make_aware(datetime.datetime.combine(day, cutoff))
    return max([deadline] + clamps)


def is_open(delivery_date, now=None, cutoff=None, branch=None) -> bool:
    """Whether a request remains editable. Equality with the deadline is closed."""
    return (now or timezone.now()) < locks_at(delivery_date, cutoff=cutoff, branch=branch)


def earliest_open_date(now=None, cutoff=None, branch=None) -> datetime.date:
    """Return the first future delivery date which remains open."""
    now = now or timezone.now()
    day = timezone.localdate(now) + datetime.timedelta(days=1)
    while not is_open(day, now=now, cutoff=cutoff, branch=branch):
        day += datetime.timedelta(days=1)
    return day


def earliest_open_date_everywhere(now=None) -> datetime.date:
    """The first delivery date still open for every active branch, so a
    shop-wide rule change starting there cannot reopen any branch's date."""
    now = now or timezone.now()
    day = earliest_open_date(now=now)
    for branch in Branch.objects.filter(active=True):
        day = max(day, earliest_open_date(now=now, branch=branch))
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
    reopen dates which have already closed, even with no request row.  With
    per-branch lead times the boundary is the first date open for *every*
    branch: a date already closed for a long-lead branch keeps its old rule.
    """
    if not isinstance(cutoff, datetime.time):
        raise TypeError("cutoff must be a datetime.time")

    with transaction.atomic():
        settings = lock_settings()
        now = timezone.now()
        effective_from = earliest_open_date_everywhere(now=now)
        policy = ProductionCutoffPolicy.objects.create(
            effective_from=effective_from,
            cutoff=cutoff,
            activated_at=now,
            source=ProductionCutoffPolicy.Source.ADMIN_CHANGE,
        )
        settings.production_cutoff = cutoff
        settings.save(update_fields=["production_cutoff"])
        return policy


def update_lead_days(branch, lead_days: int, changed_by_name: str = "") -> ProductionLeadTime:
    """Append a lead-time row for ``branch`` and update its displayed value.

    Call while holding the settings lock (``lock_settings()``), which every
    production writer takes first.  The new lead time applies from the
    branch's first still-open delivery date under the old rules, so a shorter
    lead time cannot reopen a date that has closed, and a longer one closes
    the affected dates now rather than retroactively.
    """
    if isinstance(lead_days, bool) or not isinstance(lead_days, int):
        raise TypeError("lead_days must be an int")
    if not DEFAULT_LEAD_DAYS <= lead_days <= MAX_LEAD_DAYS:
        raise ValueError(f"lead_days must be {DEFAULT_LEAD_DAYS}-{MAX_LEAD_DAYS}")
    now = timezone.now()
    row = ProductionLeadTime.objects.create(
        branch=branch,
        lead_days=lead_days,
        effective_from=earliest_open_date(now=now, branch=branch),
        activated_at=now,
        changed_by_name=changed_by_name,
    )
    Branch.objects.filter(pk=branch.pk).update(production_lead_days=lead_days)
    branch.production_lead_days = lead_days
    return row
