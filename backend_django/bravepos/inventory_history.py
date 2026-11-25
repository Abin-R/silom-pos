"""Past stock: worked out for the last ``OPEN_DAYS`` days, saved before that.

The Inventory page's "As of" date works a past day's stock out from what has
changed since (``rewind_stock``).  Once a day is more than ``OPEN_DAYS`` days
old it is saved (``InventorySnapshot``, by the ``save_inventory_snapshots``
cron) and the page shows the saved figures, which nothing rewrites.

The same number decides when a bill can no longer be voided or refunded
(``order_is_closed``).  The two go together: a void changes how much was sold
on the bill's own day, so a day is only saved once every bill its figures read
can no longer change.

Everything here counts calendar days in the shop's time zone (Bangkok).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.db import connection, transaction
from django.db.models import Exists, Max, OuterRef, Q, Sum
from django.utils import timezone

from .models import (
    AuditLog, InventorySnapshot, Order, OrderItem, Product, StockMovement,
)

# Days back from today that stay open: 15 days ago is still worked out live and
# its bills can still be voided; 16 days ago is saved and its bills are closed.
OPEN_DAYS = 15


def is_closed(day: date) -> bool:
    """Is ``day`` more than ``OPEN_DAYS`` days ago?"""
    return day < timezone.localdate() - timedelta(days=OPEN_DAYS)


def order_is_closed(order) -> bool:
    """Is this bill too old to be voided, refunded or un-voided?"""
    return bool(order.created_at) and is_closed(timezone.localtime(order.created_at).date())


def day_window(dfrom: date, dto: date):
    """Two dates as an inclusive aware-datetime window in the shop's zone."""
    tz = timezone.get_current_timezone()
    return (datetime.combine(dfrom, time.min).replace(tzinfo=tz),
            datetime.combine(dto, time.max).replace(tzinfo=tz))


def first_day():
    """The earliest day there are figures for, or None.

    Edits from the product forms, the till's product save and catalogue sync
    overwrite ``stock`` without a StockMovement.  The audit log is their only
    record, so stock can't be worked back past its first Product row."""
    first = (AuditLog.objects.filter(model="Product")
             .order_by("at").values_list("at", flat=True).first())
    days = [timezone.localtime(first).date()] if first else []
    saved = (InventorySnapshot.objects.order_by("date")
             .values_list("date", flat=True).first())
    if saved:
        days.append(saved)
    return min(days) if days else None


# ── Working a past day out ──────────────────────────────────────────────────
def rewind_stock(products, as_of):
    """Set each product's ``stock``, ``cost`` and ``par_level`` to what they
    were at the end of ``as_of``, and return the ones on sale that day.

    Stock changes in two ways:

    * bills — ``create_order_from_items`` and a void write with a queryset
      ``update()`` (no audit row), so they're read from the StockMovement
      written alongside, recognised by ``document_no`` being an order number.
      A voided sale counts too: its stock left on the day and came back later,
      as a movement on the same document;
    * everything else — stock documents, the till's stock screen, Grab orders,
      product edits, catalogue sync — goes through ``Product.save()``, whose
      audit row carries ``stock`` from/to.  That covers absolute overwrites a
      StockMovement can't express.

    So the first audited change after the day says what stock was just before
    it, and only the bills between the two are undone.  A product nobody has
    edited since is worked back from today's figure.

    Cost, par level and whether it was on sale come from the audit log alone.
    A cost of 0 means nobody had entered it yet (most were only filled in on
    21-22 Oct 2026), so the first real one entered since stands in for it.
    """
    if not products:
        return products
    _, cutoff = day_window(as_of, as_of)
    by_id = {str(p.id): p for p in products}
    stock_at = {}        # product id -> (stock just before, when it changed)
    was = {}             # (product id, field) -> value at the end of the day
    born_later = set()

    for oid, action, at, stock, cost, par, active in (
        AuditLog.objects
        .filter(model="Product", branch_id__in={p.branch_id for p in products},
                at__gt=cutoff)
        .filter(Q(action="create")
                | Q(action="update", changes__has_any_keys=[
                    "stock", "cost", "par_level", "active"]))
        .order_by("at")
        # The keys, not ``changes``: an edit that changed a photo carries it.
        .values_list("object_id", "action", "at", "changes__stock",
                     "changes__cost", "changes__par_level", "changes__active")
    ):
        if oid not in by_id:
            continue
        if action == "create":
            born_later.add(oid)
            continue
        if isinstance(stock, dict):
            stock_at.setdefault(oid, (int(stock.get("from") or 0), at))
        for field, change in (("par_level", par), ("active", active)):
            if isinstance(change, dict):
                was.setdefault((oid, field), change.get("from"))
        if isinstance(cost, dict) and not was.get((oid, "cost")):
            before, after = (Decimal(str(cost.get(k) or 0)) for k in ("from", "to"))
            was[oid, "cost"] = before or after

    kept = []
    for oid, p in by_id.items():
        if oid in born_later or not was.get((oid, "active"), p.active):
            continue
        if oid in stock_at:
            p.stock = stock_at[oid][0]
        if (oid, "cost") in was:
            p.cost = was[oid, "cost"]
        if (oid, "par_level") in was:
            p.par_level = int(was[oid, "par_level"] or 0)
        kept.append(p)

    bills = (
        StockMovement.objects
        .filter(product__in=kept, type__in=("out", "return", "in"), created_at__gt=cutoff)
        .filter(Exists(Order.objects.filter(order_number=OuterRef("document_no"))))
        .values_list("product_id", "type", "qty", "created_at")
    )
    for product_id, kind, qty, at in bills:
        oid = str(product_id)
        if oid in stock_at and at > stock_at[oid][1]:
            continue  # after the change the figure is read from
        by_id[oid].stock += qty if kind == "out" else -qty
    return kept


def week_sold_and_received(products, end_day):
    """Units sold in the seven days up to ``end_day`` and the last stock-in on
    or before it, by product id."""
    start, end = day_window(end_day - timedelta(days=6), end_day)
    sold = {
        row["product_id"]: row["qty"] or 0
        for row in OrderItem.objects.filter(
            product__in=products, order__created_at__range=(start, end),
        ).exclude(order__status="cancel").values("product_id").annotate(qty=Sum("qty"))
    }
    received = {
        row["product_id"]: row["last"]
        for row in StockMovement.objects.filter(
            product__in=products, type="in", created_at__lte=end)
        .values("product_id").annotate(last=Max("created_at"))
    }
    return sold, received


# ── Saved days ──────────────────────────────────────────────────────────────
def is_saved(day: date) -> bool:
    return InventorySnapshot.objects.filter(date=day).exists()


def saved_rows(day: date, branch_ids):
    """The saved rows of these branches, shaped like the products the Inventory
    page lists."""
    snaps = list(InventorySnapshot.objects.filter(date=day, branch_id__in=branch_ids))
    alive = set(Product.objects.filter(id__in=[s.product_id for s in snaps])
                .values_list("id", flat=True))
    return [
        SimpleNamespace(
            # No link to the page of a product since deleted.
            id=s.product_id if s.product_id in alive else None,
            branch_id=s.branch_id, branch=SimpleNamespace(name=s.branch_name),
            name=s.name, name_th=s.name_th, sku=s.sku, barcode=s.barcode,
            category=SimpleNamespace(name=s.category_name) if s.category_name else None,
            stock=s.stock, cost=s.cost, par_level=s.par_level,
            sold_7d=s.sold_7d, last_received=s.last_received,
        )
        for s in snaps
    ]


def build_day(day: date):
    """Every branch's unsaved ``InventorySnapshot`` rows for ``day``."""
    products = rewind_stock(
        list(Product.objects.filter(branch__isnull=False)
             .select_related("category", "branch")
             .defer("image_base64", "image_url")), day)
    sold, received = week_sold_and_received(products, day)
    return [
        InventorySnapshot(
            date=day, branch_id=p.branch_id, branch_name=p.branch.name,
            product_id=p.id, name=p.name, name_th=p.name_th, sku=p.sku,
            barcode=p.barcode, category_name=p.category.name if p.category else "",
            stock=p.stock, cost=p.cost, par_level=p.par_level or 0,
            sold_7d=sold.get(p.id, 0), last_received=received.get(p.id),
        )
        for p in products
    ]


def days_to_save():
    """Closed days not saved yet, oldest first: normally just the one that
    closed last night, plus any a missed run left behind."""
    day = first_day()
    if day is None:
        return []
    saved = set(InventorySnapshot.objects.values_list("date", flat=True).distinct())
    days = []
    while is_closed(day):
        if day not in saved:
            days.append(day)
        day += timedelta(days=1)
    return days


def is_late(day: date) -> bool:
    """Should ``day`` have been saved by now?  It closes at midnight and the
    job runs half an hour later; two hours past midnight, it was missed."""
    closed, _ = day_window(day + timedelta(days=OPEN_DAYS + 1),
                           day + timedelta(days=OPEN_DAYS + 1))
    return timezone.now() > closed + timedelta(hours=2)


def save_day(day: date) -> int:
    """Save ``day``'s figures and return how many rows that was.  A day already
    saved is never rewritten."""
    if not is_closed(day):
        raise ValueError(f"{day} is not closed yet; its figures can still change.")
    # One consistent read.  Product, the audit log and the bills are separate
    # queries, and a sale landing between two of them would be counted once in
    # one and not in the other: wrong by that sale, for good.
    top_level = not connection.in_atomic_block
    with transaction.atomic():
        if top_level and connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        if is_saved(day):
            return 0
        rows = build_day(day)
        InventorySnapshot.objects.bulk_create(rows, batch_size=500)
        return len(rows)
