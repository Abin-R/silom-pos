"""Preset discounts at the till, and the alert for the ones that aren't presets.

With ``Branch.discount_types_enabled`` on, a cashier discounts a line by
picking a :class:`~bravepos.models.DiscountType` from a dropdown.  The last
entry, "Other", is the old free ฿/% box, and the till will not apply it
without a written reason.  When a sale carrying one goes through, the reason
is posted to a SeaTalk group so someone sees it the same day.

Alerts go out once per *order*, not per line, and only after the order is
saved: a discount on a cart that was never paid for cost nothing.
"""
from __future__ import annotations

from decimal import Decimal

from django.utils import timezone

from . import seatalk

OTHER_LABEL = "Other"


def _baht(v) -> str:
    return f"฿{Decimal(v or 0):,.2f}"


def other_discount_lines(order) -> list:
    """The order's lines that carry a hand-entered ("Other") discount."""
    return [
        it for it in order.items.all()
        if it.discount_label == OTHER_LABEL and (it.discount or 0) > 0
    ]


def build_alert(order, lines) -> str:
    branch = order.branch.name if order.branch else "—"
    when = timezone.localtime(order.created_at).strftime("%d %b %Y %H:%M")
    out = [
        "Other discount applied at the till",
        f"Branch: {branch}",
        f"Order: {order.order_number}",
        f"Staff: {order.staff or '—'}",
        f"Time: {when}",
    ]
    for it in lines:
        gross = Decimal(it.price) * it.qty
        pct = (Decimal(it.discount) / gross * 100) if gross else Decimal(0)
        out += [
            "",
            f"{it.name} × {it.qty} ({_baht(gross)})",
            f"Discount: −{_baht(it.discount)} ({pct:.0f}%)",
            f"Reason: {it.discount_reason.strip() or '—'}",
        ]
    out += ["", f"Bill total: {_baht(order.total)}"]
    return "\n".join(out)


def alert_other_discounts(order):
    """Post a SeaTalk alert if ``order`` carries any "Other" discount.

    Returns the background thread (for tests), or None when nothing was sent.
    Never raises — the order is already saved and paid for.
    """
    branch = order.branch
    if branch is None or not branch.discount_types_enabled:
        return None
    lines = other_discount_lines(order)
    if not lines:
        return None
    return seatalk.send_group_text_async(
        seatalk.discount_group_id(), build_alert(order, lines))


def describe_buys(d, limit: int = 3) -> str:
    """What the customer has to buy, in one line: "All products",
    "Choc chip, Latte +2 more", "Cookies", "Choc chip + Drinks ×2".

    Expects ``products``, ``categories`` and ``conditions`` (with their
    product/category) to be prefetched when called over many rows.
    """
    from .models import DiscountType  # local: models imports nothing from here

    def some(names):
        names = list(names)
        more = len(names) - limit
        return ", ".join(names[:limit]) + (f" +{more} more" if more > 0 else "")

    if d.applies_to == DiscountType.APPLIES_PRODUCTS:
        return some(p.name for p in d.products.all())
    if d.applies_to == DiscountType.APPLIES_CATEGORIES:
        return some(c.name for c in d.categories.all())
    if d.applies_to == DiscountType.APPLIES_COMBO:
        parts = []
        for c in d.conditions.all():
            target = c.product or c.category
            name = target.name if target else "?"
            parts.append(f"{name} ×{c.min_qty}" if c.min_qty > 1 else name)
        return " + ".join(parts)
    return "All products"

