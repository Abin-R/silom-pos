"""The Custom Pay list: what the till offers, and the backoffice's edits to it.

"Sync to all branches" here means what it means on the product form.  Copies
are matched by name, ignoring case, because each branch owns its own rows; the
source is whatever an admin just changed, so for that one type it overwrites.
Only active branches are written to (`catalog.other_branches`).

Callers wrap these in one transaction with the change they fan out: nothing on
screen could say where a half-finished run stopped.
"""
from __future__ import annotations

from .catalog import other_branches
from .models import PaymentType


def feed(branch):
    """What this branch's till shows."""
    rows = PaymentType.objects.filter(branch=branch, active=True)
    return [{"id": str(row.id), "name": row.name} for row in rows]


def find(branch, name):
    return PaymentType.objects.filter(branch=branch, name__iexact=name).first()


def create(branch, name, *, active=True):
    """Add a type at the end of the branch's list."""
    last = PaymentType.objects.filter(branch=branch).order_by("-sort_order").first()
    return PaymentType.objects.create(
        branch=branch, name=name, active=active,
        sort_order=(last.sort_order + 1) if last else 0,
    )


def _apply_order(rows):
    """Number ``rows`` from 0, writing only the ones that moved."""
    changed = False
    for position, row in enumerate(rows):
        if row.sort_order != position:
            row.sort_order = position
            row.save(update_fields=["sort_order"])
            changed = True
    return changed


def move(row, step):
    """Swap ``row`` with its neighbour.  False at either end of the list."""
    rows = list(PaymentType.objects.filter(branch=row.branch))
    here = next(i for i, other in enumerate(rows) if other.pk == row.pk)
    there = here + step
    if not 0 <= there < len(rows):
        return False
    rows[here], rows[there] = rows[there], rows[here]
    _apply_order(rows)
    return True


def fanout_save(row, *, previous_name=None):
    """Make every other branch's copy of ``row`` match it.

    ``previous_name`` is the name before this save; without it a rename cannot
    find its own copies and would add a second row beside them.  A branch that
    lacks the type gets it, unless it is hidden here: a type is never born
    hidden somewhere it was never offered.
    """
    report = {"created": [], "updated": [], "unchanged": []}
    for target in other_branches(row.branch):
        copy = find(target, row.name) or (
            find(target, previous_name) if previous_name else None
        )
        if copy is None:
            if row.active:
                create(target, row.name)
                report["created"].append(target.name)
            else:
                report["unchanged"].append(target.name)
        elif copy.name != row.name or copy.active != row.active:
            copy.name, copy.active = row.name, row.active
            copy.save(update_fields=["name", "active"])
            report["updated"].append(target.name)
        else:
            report["unchanged"].append(target.name)
    return report


def fanout_delete(branch, name):
    """Delete the other branches' copies.  Returns the branches that had one."""
    deleted = []
    for target in other_branches(branch):
        copy = find(target, name)
        if copy is not None:
            copy.delete()
            deleted.append(target.name)
    return deleted


def fanout_order(branch):
    """Put the other branches' lists in this branch's order.  A type only they
    have keeps its relative place after the shared ones.  Returns the branches
    whose order changed."""
    order = {
        name.lower(): i for i, name in enumerate(
            PaymentType.objects.filter(branch=branch).values_list("name", flat=True))
    }
    updated = []
    for target in other_branches(branch):
        rows = list(PaymentType.objects.filter(branch=target))
        rows.sort(key=lambda row: order.get(row.name.lower(), len(order)))
        if _apply_order(rows):
            updated.append(target.name)
    return updated
