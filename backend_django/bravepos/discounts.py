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



# ─── Running one promotion at several branches ──────────────────────────────

def _by_name(qs, name):
    name = (name or "").strip()
    return qs.filter(name__iexact=name).first() if name else None


def branch_copies(dt):
    """The other branches' copies of ``dt`` (same ``group_id``)."""
    from .models import DiscountType
    if not dt.group_id:
        return DiscountType.objects.none()
    return (DiscountType.objects.filter(group_id=dt.group_id)
            .exclude(pk=dt.pk).select_related("branch"))


def _translate(dt, target):
    """``dt``'s products, categories, combination rows and free product,
    matched by name at ``target``.  Returns ``(plan, missing)`` — ``missing``
    names everything ``target`` doesn't have; ``plan`` is only usable when
    it is empty."""
    products = target.products.filter(active=True)
    categories = target.categories.all()
    missing, plan = [], {"products": [], "categories": [], "rows": [], "free": None}

    for p in dt.products.all():
        m = _by_name(products, p.name)
        plan["products"].append(m) if m else missing.append(p.name)
    for c in dt.categories.all():
        m = _by_name(categories, c.name)
        plan["categories"].append(m) if m else missing.append(c.name)
    for row in dt.conditions.all():
        if row.product_id:
            m = _by_name(products, row.product.name)
            if m is None:
                missing.append(row.product.name)
            plan["rows"].append({"product": m, "category": None, "min_qty": row.min_qty})
        else:
            m = _by_name(categories, row.category.name)
            if m is None:
                missing.append(row.category.name)
            plan["rows"].append({"product": None, "category": m, "min_qty": row.min_qty})
    if dt.free_product_id:
        m = _by_name(products, dt.free_product.name)
        plan["free"] = m
        if m is None:
            missing.append(dt.free_product.name)
    return plan, missing


def _cheapest(target, rows):
    total = Decimal(0)
    for r in rows:
        qs = target.products.filter(active=True)
        qs = qs.filter(pk=r["product"].pk) if r["product"] else qs.filter(category=r["category"])
        price = qs.order_by("price").values_list("price", flat=True).first()
        if price is None:
            return None
        total += price * r["min_qty"]
    return total


COPIED_FIELDS = ("name", "kind", "value", "applies_to", "free_qty",
                 "start_date", "end_date", "active", "code")


def sync_promotion(dt, targets):
    """Make ``targets`` (other branches) run ``dt``, and only them.

    Each target gets — or has updated — its own copy, pointing at *its own*
    products and categories, matched by name the way product sync matches.
    A target that lacks something the promotion names is skipped rather than
    given a promotion that would mean something different there (a
    combination missing a row, a product list quietly shorter).  So is one
    where a fixed combination discount would take its cheapest combination
    below ฿0 — prices differ between branches.

    Copies at branches no longer in ``targets`` are removed: unticking a
    branch is how a promotion is taken off it.  Past sales are unaffected —
    each order line keeps the discount's name as a snapshot.

    Returns ``{"created": [...], "updated": [...], "removed": [...],
    "skipped": [(branch name, reason), ...]}``.  The caller runs this in the
    same transaction as the source's own save.
    """
    from .models import DiscountCondition, DiscountType

    report = {"created": [], "updated": [], "removed": [], "skipped": []}
    target_ids = {t.pk for t in targets if t.pk != dt.branch_id}

    for gone in branch_copies(dt).exclude(branch_id__in=target_ids):
        report["removed"].append(gone.branch.name if gone.branch else "?")
        gone.delete()

    for target in targets:
        if target.pk == dt.branch_id:
            continue
        plan, missing = _translate(dt, target)
        if missing:
            report["skipped"].append((target.name, "doesn't have " + ", ".join(sorted(set(missing)))))
            continue
        if dt.kind == DiscountType.KIND_FIXED and plan["rows"]:
            cheapest = _cheapest(target, plan["rows"])
            if cheapest is None or dt.value >= cheapest:
                report["skipped"].append((
                    target.name,
                    "its cheapest combination is ฿%s, not more than the ฿%s discount"
                    % (f"{cheapest:,.2f}" if cheapest is not None else "0.00", f"{dt.value:,.2f}")))
                continue

        copy = DiscountType.objects.filter(group_id=dt.group_id, branch=target).first()
        created = copy is None
        if created:
            copy = DiscountType(branch=target, group_id=dt.group_id)
        for f in COPIED_FIELDS:
            setattr(copy, f, getattr(dt, f))
        copy.free_product = plan["free"]
        copy.save()
        copy.products.set(plan["products"])
        copy.categories.set(plan["categories"])
        copy.conditions.all().delete()
        DiscountCondition.objects.bulk_create([
            DiscountCondition(discount_type=copy, sort_order=i, min_qty=r["min_qty"],
                              product=r["product"], category=r["category"])
            for i, r in enumerate(plan["rows"])
        ])
        report["created" if created else "updated"].append(target.name)
    return report
