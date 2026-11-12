"""Production requests: branches ask the central kitchen for stock.

Three people use this, each from one backoffice screen:

* a **stock supervisor** says how many of each product a branch wants on a
  delivery date (`save_request`);
* the **kitchen** reads the totals across every branch, to know what to bake
  (`kitchen_totals`) — read only;
* the **packer** records how many of each actually went into each branch's
  delivery (`packing_tables`, `save_packed`).

Two rules shape everything below.

**A request locks at the cut-off** (`Settings.production_cutoff`, the day
before delivery) and nobody — admins included — can change it after that.
The kitchen starts baking from these numbers; a request that could still move
would make the totals it baked from wrong.  Admins can change the cut-off
time, which moves the lock for dates that have not locked yet.

**Nothing here moves stock or reaches stock-in.**  The packed figure is a
record of what left the kitchen.  The branch receiving the delivery counts it
and enters its own stock-in; if the packed number were offered there it would
be copied instead of counted.  So these are their own tables, not a
StockDocument type, and no stock-in import reads them.

Products are per-branch rows (see `catalog`), so the same cookie at two shops
is two records.  What joins them is the **barcode**, which holds the Shopster
product id and is the same at every branch whatever each one calls the
product; a product with no barcode falls back to its name (`match_key`).
Requests snapshot the key with the name, category and sort position, and the
kitchen and packer screens line branches up on those snapshots.  Where
branches name or file one product differently, the screens show BIO HOUSE's
version — Shopster's source — or else the one most branches use.
"""
from __future__ import annotations

import datetime
from collections import Counter, OrderedDict

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    Branch, Product, ProductionRequest, ProductionRequestItem, Settings,
)

DEFAULT_CUTOFF = datetime.time(23, 30)

# Products with no category sort after every category and are listed under
# this heading.  Not stored: the snapshot keeps the empty name.
UNCATEGORISED = "Uncategorised"
_LAST = 10 ** 6


class RequestLocked(Exception):
    """The delivery date's cut-off has passed."""


# ── The cut-off ─────────────────────────────────────────────────────────────
def cutoff_time() -> datetime.time:
    row = Settings.objects.filter(id="shop").only("production_cutoff").first()
    return row.production_cutoff if row else DEFAULT_CUTOFF


def locks_at(delivery_date, cutoff=None) -> datetime.datetime:
    """When requests for ``delivery_date`` stop being editable: the cut-off
    time on the day before, in the shop's timezone."""
    if cutoff is None:
        cutoff = cutoff_time()
    day_before = delivery_date - datetime.timedelta(days=1)
    return timezone.make_aware(datetime.datetime.combine(day_before, cutoff))


def is_open(delivery_date, now=None, cutoff=None) -> bool:
    return (now or timezone.now()) < locks_at(delivery_date, cutoff)


def earliest_open_date(now=None, cutoff=None) -> datetime.date:
    """The first delivery date a request can still be made for."""
    now = now or timezone.now()
    if cutoff is None:
        cutoff = cutoff_time()
    day = timezone.localdate(now) + datetime.timedelta(days=1)
    while not is_open(day, now, cutoff):
        day += datetime.timedelta(days=1)
    return day


def latest_locked_date(now=None, cutoff=None) -> datetime.date:
    """The date the kitchen and packer most likely want: the soonest delivery
    whose numbers are already final.  Tomorrow once today's cut-off has
    passed, otherwise today."""
    now = now or timezone.now()
    tomorrow = timezone.localdate(now) + datetime.timedelta(days=1)
    if is_open(tomorrow, now, cutoff):
        return timezone.localdate(now)
    return tomorrow


# ── Which branches ──────────────────────────────────────────────────────────
def active_branches() -> list:
    """Every active branch.  Anyone who may open Request production may
    request for any of them — branches ticked on the Users page play no part,
    since one supervisor covers several shops and the list changes."""
    return list(Branch.objects.filter(active=True).order_by("name"))


# ── Matching one product across branches ────────────────────────────────────
def match_key(barcode, name) -> str:
    """What makes two branches' products the same product: the barcode (the
    Shopster id), or the name when there is no barcode."""
    barcode = (barcode or "").strip()
    if barcode:
        return "b:" + barcode
    return "n:" + (name or "").strip().lower()


def _product_key(product) -> str:
    return match_key(product.barcode, product.name)


# ── The request form ────────────────────────────────────────────────────────
def _snapshot(product) -> dict:
    cat = product.category
    parent = cat.parent_category if cat else None
    return {
        "product_name": product.name,
        "barcode": (product.barcode or "").strip(),
        "match_key": _product_key(product),
        "category_name": cat.name if cat else "",
        "parent_category_name": parent.name if parent else "",
        "category_order": cat.order if cat else _LAST,
        "product_order": product.sort_order,
    }


def requestable_products(branch) -> list:
    """Every active product of ``branch``, one per ``match_key``.

    On-sale products can't share a barcode within a branch (constraint
    ``product_barcode_unique_on_sale``), but two without one can share a
    name — and the kitchen and packer screens would add those up as one line
    anyway, so asking for it twice would double the order.
    """
    rows = (Product.objects
            .filter(branch=branch, active=True)
            .select_related("category__parent_category"))
    seen, out = set(), []
    for p in rows:
        key = _product_key(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    out.sort(key=lambda p: (
        p.category.order if p.category_id else _LAST,
        (p.category.name if p.category_id else "").lower(),
        p.sort_order, p.name.lower(),
    ))
    return out


def group_by_category(rows, name=lambda r: r["category_name"]) -> list:
    """``[{"name", "rows"}]`` in the order ``rows`` already have."""
    groups = OrderedDict()
    for r in rows:
        groups.setdefault(name(r) or UNCATEGORISED, []).append(r)
    return [{"name": k, "rows": v} for k, v in groups.items()]


def request_form_rows(branch, req) -> list:
    """The form's lines: every requestable product with the quantity already
    asked for, then any line on the request whose product has since left the
    catalogue (so it can still be seen, and zeroed).

    No packed figures: the supervisor may well be the person who receives the
    delivery and enters its stock-in, and must count it rather than copy it.
    """
    items = list(req.items.all()) if req else []
    by_product = {it.product_id: it for it in items if it.product_id}
    by_key = {it.match_key: it for it in items}
    used = set()
    rows = []
    for p in requestable_products(branch):
        it = by_product.get(p.id) or by_key.get(_product_key(p))
        if it is not None:
            used.add(it.id)
        rows.append({
            "key": str(p.id),
            "name": p.name,
            "name_th": p.name_th,
            "category_name": p.category.name if p.category_id else "",
            "qty": it.requested_qty if it else 0,
        })
    for it in items:
        if it.id not in used and it.requested_qty:
            rows.append({
                "key": f"item:{it.id}",
                "name": it.product_name,
                "name_th": "",
                "category_name": it.category_name,
                "qty": it.requested_qty,
                "gone": True,
            })
    return rows


@transaction.atomic
def save_request(branch, delivery_date, wanted, user=None, now=None):
    """Write the posted quantities onto the branch's request for the date.

    ``wanted`` maps a form row's ``key`` (a product id, or ``item:<id>`` for
    a line whose product has left the catalogue) to a whole number.  Keys not
    posted are left as they are.  A line set to 0 is removed, unless the
    packer has already entered a number for it — then it stays, at 0
    requested, so the packed figure is not lost.  A request left with no
    lines is deleted.

    Returns the request, or None when nothing is requested.  Raises
    ``RequestLocked`` once the cut-off has passed.
    """
    if not is_open(delivery_date, now):
        raise RequestLocked()
    name = getattr(user, "name", "") or getattr(user, "username", "") or ""
    staff = user if getattr(user, "pk", None) else None

    req = (ProductionRequest.objects.select_for_update()
           .filter(branch=branch, delivery_date=delivery_date).first())
    if req is None:
        req = ProductionRequest.objects.create(
            branch=branch, delivery_date=delivery_date,
            created_by=staff, created_by_name=name, updated_by_name=name,
        )
    items = list(req.items.all())
    by_id = {str(it.id): it for it in items}
    by_product = {it.product_id: it for it in items if it.product_id}
    by_key = {it.match_key: it for it in items}

    products = {str(p.id): p for p in requestable_products(branch)}
    for key, qty in wanted.items():
        product = products.get(key)
        if product is not None:
            item = by_product.get(product.id) or by_key.get(_product_key(product))
        elif key.startswith("item:"):
            item = by_id.get(key[5:])
            if item is None:
                continue
        else:
            continue

        if qty > 0:
            if item is None:
                item = ProductionRequestItem(request=req, product=product)
                by_key[_product_key(product)] = item
            if product is not None:
                item.product = product
                for field, value in _snapshot(product).items():
                    setattr(item, field, value)
            item.requested_qty = qty
            item.save()
        elif item is not None:
            if item.packed_qty is None:
                item.delete()
            elif item.requested_qty:
                item.requested_qty = 0
                item.save()

    if not req.items.exists():
        req.delete()
        return None
    req.updated_by_name = name
    req.save(update_fields=["updated_by_name", "updated_at"])
    return req


def request_status(req, now=None, cutoff=None) -> dict:
    """What the history list says about one request: ``open`` until the
    cut-off, then ``locked``, ``packing`` once the packer has entered some
    lines and ``packed`` once every line has a number."""
    deadline = locks_at(req.delivery_date, cutoff)
    lines = [it for it in req.items.all() if it.requested_qty or it.packed_qty is not None]
    packed = sum(1 for it in lines if it.packed_qty is not None)
    if (now or timezone.now()) < deadline:
        state = "open"
    elif packed:
        state = "packed" if packed == len(lines) else "packing"
    else:
        state = "locked"
    return {"state": state, "locks_at": deadline, "packed": packed, "lines": len(lines)}


# ── Lining branches up ──────────────────────────────────────────────────────
def _items_for(delivery_date):
    return (ProductionRequestItem.objects
            .filter(request__delivery_date=delivery_date)
            .select_related("request__branch"))


def _lines(items) -> list:
    """The date's lines grouped by ``match_key``, one entry per product.

    Each entry takes its name, category, group and position from one
    representative line: BIO HOUSE's — the shop Shopster feeds, so its name is
    the source — or else the name most branches use, so one branch's typo
    can't rename the product for everybody.  Sorted the way the catalogue is.
    """
    from .views import SHOPSTER_BRANCH_NAME
    central = SHOPSTER_BRANCH_NAME.lower()
    grouped = OrderedDict()
    for it in items:
        grouped.setdefault(it.match_key, []).append(it)
    out = []
    for key, its in grouped.items():
        rep = next((it for it in its if it.request.branch.name.lower() == central), None)
        if rep is None:
            common = Counter(it.product_name for it in its).most_common(1)[0][0]
            rep = next(it for it in its if it.product_name == common)
        out.append({
            "key": key,
            "name": rep.product_name,
            "category_name": rep.category_name,
            "parent_category_name": rep.parent_category_name,
            "order": (rep.category_order, rep.category_name.lower(),
                      rep.product_order, rep.product_name.lower()),
            "items": its,
        })
    out.sort(key=lambda line: line["order"])
    return out


# ── The kitchen ─────────────────────────────────────────────────────────────
def _kitchen_lines(delivery_date) -> list:
    return _lines(_items_for(delivery_date).filter(requested_qty__gt=0))


def kitchen_groups(delivery_date) -> list:
    """The product groups (parent categories) on the date's requests, for the
    filter chips.  "" stands for products with no group."""
    names = {line["parent_category_name"] for line in _kitchen_lines(delivery_date)}
    return sorted(names, key=lambda n: (n == "", n.lower()))


def kitchen_totals(delivery_date, group=None) -> dict:
    """How many of each product every branch together asked for.

    ``group`` limits it to one parent category ("" for ungrouped products);
    None is everything.
    """
    lines = _kitchen_lines(delivery_date)
    if group is not None:
        lines = [line for line in lines if line["parent_category_name"] == group]
    rows = [{
        "category_name": line["category_name"],
        "name": line["name"],
        "total": sum(it.requested_qty for it in line["items"]),
        "branches": len({it.request.branch_id for it in line["items"]}),
    } for line in lines]
    categories = group_by_category(rows)
    for c in categories:
        c["total"] = sum(r["total"] for r in c["rows"])
    return {
        "categories": categories,
        "total": sum(c["total"] for c in categories),
        "branches": len({it.request.branch_id for line in lines for it in line["items"]}),
    }


# ── The packer ──────────────────────────────────────────────────────────────
def _packing_lines(delivery_date) -> list:
    # A line zeroed after it was packed still shows, so its packed figure
    # stays visible; a line that is simply zero is never stored.
    return _lines(_items_for(delivery_date)
                  .filter(Q(requested_qty__gt=0) | Q(packed_qty__isnull=False)))


def packing_categories(delivery_date) -> list:
    """The categories on the date's requests, in catalogue order."""
    seen = OrderedDict()
    for line in _packing_lines(delivery_date):
        seen.setdefault(line["category_name"], None)
    return list(seen)


def packed_dates(limit=60) -> list:
    """Delivery dates with any packed number, newest first."""
    return list(ProductionRequestItem.objects
                .filter(packed_qty__isnull=False)
                .order_by("-request__delivery_date")
                .values_list("request__delivery_date", flat=True)
                .distinct()[:limit])


def packing_tables(delivery_date, category=None) -> list:
    """One grid per category: a row per branch, a column per product.

    Each cell is the branch's line for that product, or None where the branch
    did not ask for it — the packer packs what was requested, so there is no
    box to fill there.  ``category`` limits it to one category; None is all.
    """
    lines = _packing_lines(delivery_date)
    if category is not None:
        lines = [line for line in lines if line["category_name"] == category]

    tables = OrderedDict()
    for line in lines:
        t = tables.setdefault(line["category_name"], {"lines": [], "branches": {}})
        t["lines"].append(line)
        for it in line["items"]:
            branch = it.request.branch
            t["branches"].setdefault(branch.id, {"branch": branch, "cells": {}})
            t["branches"][branch.id]["cells"][line["key"]] = it

    out = []
    for cat, t in tables.items():
        keys = [line["key"] for line in t["lines"]]
        rows = []
        col_requested = [0] * len(keys)
        col_packed = [0] * len(keys)
        for entry in sorted(t["branches"].values(), key=lambda e: e["branch"].name.lower()):
            cells = [entry["cells"].get(k) for k in keys]
            for i, cell in enumerate(cells):
                if cell is not None:
                    col_requested[i] += cell.requested_qty
                    col_packed[i] += cell.packed_qty or 0
            rows.append({
                "branch": entry["branch"],
                "cells": cells,
                "requested": sum(c.requested_qty for c in cells if c),
                "packed": sum(c.packed_qty or 0 for c in cells if c),
                "any_packed": any(c is not None and c.packed_qty is not None for c in cells),
            })
        out.append({
            "name": cat or UNCATEGORISED,
            "category": cat,
            "columns": [line["name"] for line in t["lines"]],
            "rows": rows,
            "col_requested": col_requested,
            "col_packed": col_packed,
            "requested": sum(col_requested),
            "packed": sum(col_packed),
        })
    return out


@transaction.atomic
def save_packed(delivery_date, values, user=None) -> int:
    """Store the packer's numbers.  ``values`` maps a line id to a whole
    number, or None to clear it.  Only lines on ``delivery_date`` are touched.
    Returns how many changed."""
    name = getattr(user, "name", "") or getattr(user, "username", "") or ""
    now = timezone.now()
    changed = 0
    items = (ProductionRequestItem.objects.select_for_update()
             .filter(request__delivery_date=delivery_date, id__in=list(values)))
    for it in items:
        qty = values[str(it.id)]
        if qty == it.packed_qty:
            continue
        it.packed_qty = qty
        it.packed_by_name = name if qty is not None else ""
        it.packed_at = now if qty is not None else None
        it.save(update_fields=["packed_qty", "packed_by_name", "packed_at"])
        changed += 1
    return changed
