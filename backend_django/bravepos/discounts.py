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

from django.db.models import Q
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
        "BravePOS: Other discount applied at the till",
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
        return (" or " if d.combo_match == DiscountType.MATCH_ANY else " + ").join(parts)
    return "All products"



# ─── What a promotion applies to, with its history ───────────────────────────

def _rows_text(rows) -> str:
    """Combination rows as one readable line, for the history."""
    out = []
    for r in rows:
        target = r["product"] or r["category"]
        name = target.name if target else "?"
        out.append(f"{name} ×{r['min_qty']}" if r["min_qty"] > 1 else name)
    return " + ".join(out)


def apply_targets(dt, products, categories, rows):
    """Set ``dt``'s products, categories and combination rows, and write one
    history entry saying what changed.

    The list fields and the rows live outside the discount's own row, so the
    automatic audit (a diff of one row's columns) never sees them.  This is
    the one place they are changed, so it is where they are recorded — as a
    single entry on the promotion ("products: + Latte, − Mocha") rather than
    a delete and a create per combination row on every save.  Nothing changed,
    nothing written.

    ``rows``: ``[{"product": Product|None, "category": Category|None,
    "min_qty": int}]``.
    """
    from . import audit
    from .models import DiscountCondition

    changes = {}

    def diff_set(field, manager, new):
        old = {o.pk: o for o in manager.all()}
        new = {o.pk: o for o in new}
        added = sorted(n.name for k, n in new.items() if k not in old)
        removed = sorted(o.name for k, o in old.items() if k not in new)
        if added or removed:
            manager.set(list(new.values()))
            changes[field] = {"from": ", ".join(removed) or None,
                              "to": ", ".join(added) or None}

    diff_set("products (removed → added)", dt.products, products)
    diff_set("categories (removed → added)", dt.categories, categories)

    old_rows = [{"product": c.product, "category": c.category, "min_qty": c.min_qty}
                for c in dt.conditions.select_related("product", "category")]
    key = lambda rs: [(r["product"].pk if r["product"] else None,
                       r["category"].pk if r["category"] else None, r["min_qty"]) for r in rs]
    if key(old_rows) != key(rows):
        dt.conditions.all().delete()
        DiscountCondition.objects.bulk_create([
            DiscountCondition(discount_type=dt, sort_order=i, min_qty=r["min_qty"],
                              product=r["product"], category=r["category"])
            for i, r in enumerate(rows)
        ])
        changes["combination"] = {"from": _rows_text(old_rows) or None,
                                  "to": _rows_text(rows) or None}

    if changes:
        audit.record("update", instance=dt, changes=changes)
    return changes


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
    missing, plan = [], {"products": [], "categories": [], "rows": [], "free": None,
                         "free_category": None}

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
    if dt.free_category_id:
        m = _by_name(categories, dt.free_category.name)
        plan["free_category"] = m
        if m is None:
            missing.append(dt.free_category.name)
    return plan, missing


def _cheapest(target, rows, match="all"):
    if match == "any":
        prices = [_cheapest(target, [r]) for r in rows]
        return None if None in prices else min(prices, default=None)
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
                 "start_date", "end_date", "active", "code",
                 "combo_match", "min_order_amount", "max_discount")


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
    from .models import DiscountType

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
            cheapest = _cheapest(target, plan["rows"], dt.combo_match)
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
        copy.free_category = plan["free_category"]
        if created:
            copy.created_by = dt.updated_by or dt.created_by
        copy.updated_by = dt.updated_by or dt.created_by
        copy.save()
        apply_targets(copy, plan["products"], plan["categories"], plan["rows"])
        report["created" if created else "updated"].append(target.name)
    return report


def preset_feed(branch, version=None) -> list:
    """The promotions running today at ``branch``, as the till's dropdown reads
    them (``GET /discount-types``).  The self-order menu reads the same list
    (``version="2"``: no free item the cashier picks from a category).
    """
    from .models import DiscountType, Product

    # v2: combinations and free items.  v3 adds a free item picked from a
    # category at the till — a v2 till can't show that picker, so it isn't
    # offered those.
    v2 = version in ('2', '3')
    v3 = version == '3'
    # Only promotions running today (shop-local date): outside its start/end
    # dates a promotion simply isn't offered.  Blank dates are open-ended.
    today = timezone.localdate()
    qs = (DiscountType.objects.filter(branch=branch, active=True)
          .filter(Q(start_date__isnull=True) | Q(start_date__lte=today))
          .filter(Q(end_date__isnull=True) | Q(end_date__gte=today)))
    if not v2:
        qs = (qs.exclude(applies_to=DiscountType.APPLIES_COMBO)
                .exclude(kind=DiscountType.KIND_FREE))
    rows = list(qs.select_related('free_product', 'free_category')
                  .prefetch_related('products', 'categories', 'conditions__product',
                                    'conditions__category'))

    # One query for every category any preset or combination row names.
    cat_ids = {c.id for d in rows if d.applies_to == DiscountType.APPLIES_CATEGORIES
               for c in d.categories.all()}
    cat_ids |= {c.category_id for d in rows for c in d.conditions.all() if c.category_id}
    free_cat_ids = {d.free_category_id for d in rows if d.free_category_id} if v3 else set()
    free_by_cat: dict = {}
    if free_cat_ids:
        for p in (Product.objects.filter(branch=branch, active=True, category_id__in=free_cat_ids)
                  .order_by('sort_order', 'name')):
            free_by_cat.setdefault(p.category_id, []).append(
                {'id': str(p.id), 'name': p.name, 'price': float(p.price)})
    by_cat: dict = {}
    if cat_ids:
        for pid, cid in (Product.objects
                         .filter(branch=branch, active=True, category_id__in=cat_ids)
                         .values_list('id', 'category_id')):
            by_cat.setdefault(cid, []).append(str(pid))
    live_ids = {str(i) for i in Product.objects.filter(branch=branch, active=True)
                .filter(id__in={c.product_id for d in rows for c in d.conditions.all()
                                if c.product_id}).values_list('id', flat=True)}

    def product_ids(d):
        if d.applies_to == DiscountType.APPLIES_PRODUCTS:
            return [str(p.id) for p in d.products.all()]
        if d.applies_to == DiscountType.APPLIES_CATEGORIES:
            return [pid for c in d.categories.all() for pid in by_cat.get(c.id, [])]
        return []

    def conditions(d):
        out = []
        for c in d.conditions.all():
            if c.product_id:
                ids = [str(c.product_id)] if str(c.product_id) in live_ids else []
            else:
                ids = by_cat.get(c.category_id, [])
            out.append({'product_ids': ids, 'min_qty': c.min_qty})
        return out

    def free_product(d):
        p = d.free_product
        if p is None or not p.active or p.branch_id != branch.id:
            return None
        return {'id': str(p.id), 'name': p.name, 'price': float(p.price)}

    payload = []
    for d in rows:
        row = {
            'id': str(d.id),
            'name': d.name,
            'kind': d.kind,
            'value': float(d.value),
            'applies_to': d.applies_to,
            'all_products': d.applies_to == DiscountType.APPLIES_ALL,
            'product_ids': product_ids(d),
            'category_ids': ([str(c.id) for c in d.categories.all()]
                             if d.applies_to == DiscountType.APPLIES_CATEGORIES else []),
            # Promotion details the till shows under each dropdown entry.
            'code': d.code or '',
            'start_date': d.start_date.isoformat() if d.start_date else None,
            'end_date': d.end_date.isoformat() if d.end_date else None,
            'summary': describe_buys(d),
            # Bill-level rules the till enforces: offered only once the bill
            # (before discounts) reaches min_order_amount; everything this
            # promotion takes off one bill is capped at max_discount.
            'min_order_amount': float(d.min_order_amount) if d.min_order_amount else None,
            'max_discount': float(d.max_discount) if d.max_discount else None,
            'match': d.combo_match,
        }
        if v2:
            if d.applies_to == DiscountType.APPLIES_COMBO:
                row['conditions'] = conditions(d)
            if d.kind == DiscountType.KIND_FREE:
                row['free_product'] = free_product(d)
                row['free_qty'] = d.free_qty
                if d.free_category_id and v3:
                    # The cashier picks one of these at the till.
                    row['free_choices'] = free_by_cat.get(d.free_category_id, [])
                    row['free_category_name'] = d.free_category.name
                # A free item whose product has since been retired (or whose
                # category is now empty) has nothing left to give away.
                if row['free_product'] is None and not row.get('free_choices'):
                    continue
        payload.append(row)

    return payload
