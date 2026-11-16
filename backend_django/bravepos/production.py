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

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    Branch, Product, ProductionRequest, ProductionRequestItem,
)
from .production_policy import (
    cutoff_time, deadline_provenance, earliest_open_date, is_open, latest_locked_date, lock_settings,
    locks_at,
)

DEFAULT_CUTOFF = datetime.time(23, 30)

# Products with no category sort after every category and are listed under
# this heading.  Not stored: the snapshot keeps the empty name.
UNCATEGORISED = "Uncategorised"
_LAST = 10 ** 6


class ProductionSaveError(Exception):
    """A production save was deliberately refused without changing data."""

    def __init__(self, message, *, conflicts=None, keys=None):
        super().__init__(message)
        self.conflicts = conflicts or {}
        self.keys = list(keys or [])


class RequestLocked(ProductionSaveError):
    """The delivery date's cut-off has passed."""

    def __init__(self, delivery_date=None):
        message = "This delivery date is locked; nothing was saved."
        if delivery_date:
            message = f"{delivery_date} is locked; nothing was saved."
        super().__init__(message)


class RevisionConflict(ProductionSaveError):
    """The submitted form was based on an older saved version."""


class CatalogueConflict(ProductionSaveError):
    """A current catalogue row cannot safely be attached to saved history."""


class InvalidRows(ProductionSaveError):
    """The submitted rows are malformed, unknown, or out of scope."""


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
    saved_keys = {it.match_key for it in items}
    rows = []
    # Saved identities always win over catalogue rows.  Their own immutable
    # snapshots are the authoritative labels, even while the request is open.
    for it in items:
        rows.append({
            "key": f"item:{it.id}",
            "name": it.product_name,
            "name_th": "",
            "category_name": it.category_name,
            "qty": it.requested_qty,
            "revision": getattr(it, "revision", 0),
            "gone": not it.product_id,
        })
    for p in requestable_products(branch):
        it = by_product.get(p.id)
        key = _product_key(p)
        if it is not None:
            # Do not let a product UUID form key rebind or overwrite a saved
            # item.  The stable item key above is the only edit address.
            continue
        rows.append({
            "key": str(p.id),
            "name": p.name,
            "name_th": p.name_th,
            "category_name": p.category.name if p.category_id else "",
            "qty": 0,
            # A different retained row owns this match key.  It cannot safely
            # become another request line merely because the catalogue changed.
            "unavailable": key in saved_keys,
        })
    return rows


def _quantity_errors(values):
    """Return submitted keys with non-whole or out-of-range quantities."""
    bad = []
    for key, qty in values.items():
        if isinstance(qty, bool) or not isinstance(qty, int) or not 0 <= qty <= 99999:
            bad.append(str(key))
    return bad


def _locked_request(branch, delivery_date, staff, name):
    """Return the branch/date parent locked after creation races settle.

    Supervisor writes take locks in this order: the settings singleton, this
    request parent, then its items in primary-key order.  ``get_or_create``
    keeps the uniqueness constraint authoritative while the explicit refetch
    makes the resulting parent lock unambiguous on every supported database.
    """
    try:
        with transaction.atomic():
            req, created = ProductionRequest.objects.get_or_create(
                branch=branch,
                delivery_date=delivery_date,
                defaults={
                    "created_by": staff,
                    "created_by_name": name,
                    "updated_by_name": name,
                },
            )
    except IntegrityError:
        # Only a concurrent unique-parent creation is expected here.  Let an
        # unrelated integrity failure escape rather than pretending it saved.
        req = ProductionRequest.objects.get(branch=branch, delivery_date=delivery_date)
        created = False
    return ProductionRequest.objects.select_for_update().get(pk=req.pk), created


def _visible_request(req, items):
    return req if any(it.requested_qty or it.packed_qty is not None for it in items) else None


@transaction.atomic
def save_request(
    branch, delivery_date, wanted, user=None, now=None, *,
    expected_request_id=None, expected_revision=None, strict=False,
):
    """Write the posted quantities onto the branch's request for the date.

    ``wanted`` maps a stable saved ``item:<uuid>`` or a new catalogue product
    UUID to a whole number.  Existing rows are never rebound through barcode
    or name matching.  Zero rows and empty parents remain as tombstones so an
    old empty form can never become current again.

    Returns the request, or None when nothing is requested.  Raises
    ``RequestLocked`` once the cut-off has passed.
    """
    if strict and expected_revision is None:
        raise InvalidRows("This request form is missing its saved revision.")
    if not isinstance(wanted, dict):
        raise InvalidRows("Request rows must be a mapping.")
    bad_quantities = _quantity_errors(wanted)
    if bad_quantities:
        raise InvalidRows("Request quantities must be whole numbers from 0 to 99,999.",
                          keys=bad_quantities)
    name = getattr(user, "name", "") or getattr(user, "username", "") or ""
    staff = user if getattr(user, "pk", None) else None

    # Keep the policy stable before finding/creating the parent.  ``now`` is
    # intentionally compatibility-only: callers cannot authorize a stale save
    # by carrying the page's entry-time clock into this critical section.
    lock_settings()
    existing = (ProductionRequest.objects.select_for_update()
                .filter(branch=branch, delivery_date=delivery_date).first())
    if existing is None and not strict:
        # Preserve the historical trusted-service no-op behavior: a call that
        # cannot address a real row and requests no positive catalogue line
        # does not manufacture an empty parent.  Strict HTTP forms deliberately
        # retain their empty parent identity instead.
        catalogue_ids = {str(product.id) for product in requestable_products(branch)}
        has_positive_catalogue_line = any(
            str(key) in catalogue_ids and qty > 0 for key, qty in wanted.items()
        )
        if not has_positive_catalogue_line:
            if not is_open(delivery_date):
                raise RequestLocked(delivery_date)
            return None
    if existing is None:
        req, created = _locked_request(branch, delivery_date, staff, name)
    else:
        req, created = existing, False
    items = list(req.items.select_for_update().order_by("pk"))
    if strict:
        current_id = str(req.id)
        if ((expected_request_id is None and not created)
                or (expected_request_id is not None and str(expected_request_id) != current_id)
                or expected_revision != getattr(req, "revision", 0)):
            raise RevisionConflict(
                "This request changed in another session; nothing was saved.",
                conflicts={"request": {
                    "id": current_id,
                    "revision": getattr(req, "revision", 0),
                }},
            )
    by_id = {str(it.id): it for it in items}
    by_product = {it.product_id: it for it in items if it.product_id}

    products = {str(p.id): p for p in requestable_products(branch)}
    unknown = []
    new_product_keys = {}
    changes = []
    for key, qty in wanted.items():
        key = str(key)
        product = None
        item = None
        if key.startswith("item:"):
            item = by_id.get(key[5:])
            if item is None:
                unknown.append(key)
                continue
        else:
            product = products.get(key)
            if product is None:
                unknown.append(key)
                continue
            # Legacy trusted service callers may still address an existing
            # row by the exact product UUID.  It is deliberately not a key
            # fallback: barcode/name reuse never finds an old item.
            item = by_product.get(product.id)
            if item is None and qty:
                product_key = _product_key(product)
                existing = next((saved for saved in items if saved.match_key == product_key), None)
                if existing is not None:
                    raise CatalogueConflict(
                        "A saved request line already uses this catalogue product key; nothing was saved.",
                        keys=[key],
                        conflicts={key: {"item": str(existing.id), "match_key": product_key}},
                    )
                prior = new_product_keys.get(product_key)
                if prior is not None and prior != key:
                    raise CatalogueConflict(
                        "Two submitted catalogue products have the same production key; nothing was saved.",
                        keys=[prior, key],
                    )
                new_product_keys[product_key] = key
        changes.append((key, qty, item, product))

    if strict and unknown:
        raise InvalidRows("Some request rows are no longer available; nothing was saved.", keys=unknown)

    # Fresh policy authorization happens after every write lock is held.
    if not is_open(delivery_date):
        raise RequestLocked(delivery_date)

    requested_changed = False
    for key, qty, item, product in changes:
        if item is None:
            if not qty:
                continue
            item = ProductionRequestItem(request=req, product=product, **_snapshot(product))
            try:
                with transaction.atomic():
                    item.save()
            except IntegrityError:
                collision = ProductionRequestItem.objects.filter(
                    request=req, match_key=_product_key(product),
                ).first()
                if collision is None:
                    raise
                raise CatalogueConflict(
                    "A saved request line already uses this catalogue product key; nothing was saved.",
                    keys=[key], conflicts={key: {"item": str(collision.id), "match_key": collision.match_key}},
                )
            items.append(item)
        if qty == item.requested_qty:
            continue
        # Recheck immediately before each write so a lock wait cannot carry a
        # once-open request beyond its deadline.
        if not is_open(delivery_date):
            raise RequestLocked(delivery_date)
        item.requested_qty = qty
        item.revision = getattr(item, "revision", 0) + 1
        item.save(update_fields=["requested_qty", "revision"])
        requested_changed = True

    if requested_changed:
        if not is_open(delivery_date):
            raise RequestLocked(delivery_date)
        req.revision = getattr(req, "revision", 0) + 1
        req.updated_by_name = name
        req.save(update_fields=["revision", "updated_by_name", "updated_at"])
    return _visible_request(req, items)


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
    # stays visible; unconfirmed zero tombstones stay out of the packing grid.
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
def save_packed(delivery_date, values, user=None, *, expected_revisions=None, strict=False) -> int:
    """Store the packer's numbers.  ``values`` maps a line id to a whole
    number, or None to clear it.  Only lines on ``delivery_date`` are touched.
    Returns how many changed."""
    if not isinstance(values, dict):
        raise InvalidRows("Packing rows must be a mapping.")
    bad_quantities = [str(key) for key, qty in values.items()
                      if qty is not None and (isinstance(qty, bool) or not isinstance(qty, int)
                                              or not 0 <= qty <= 99999)]
    if bad_quantities:
        raise InvalidRows("Packed quantities must be blank or whole numbers from 0 to 99,999.",
                          keys=bad_quantities)
    if strict and not isinstance(expected_revisions, dict):
        raise InvalidRows("This packing form is missing its saved revisions.")

    name = getattr(user, "name", "") or getattr(user, "username", "") or ""
    keys = [str(key) for key in values]
    # Resolve parents before acquiring any write locks, then lock all parents
    # in deterministic primary-key order before their selected item cells.
    try:
        candidates = list(ProductionRequestItem.objects.filter(id__in=keys).values_list("id", "request_id"))
    except (ValidationError, ValueError):
        candidates = []
    item_parent_ids = {str(item_id): parent_id for item_id, parent_id in candidates}
    parent_ids = sorted(set(item_parent_ids.values()), key=str)
    parents = list(ProductionRequest.objects.select_for_update().filter(pk__in=parent_ids).order_by("pk"))
    locked_parent_ids = {parent.id for parent in parents}
    try:
        items = list(ProductionRequestItem.objects.select_for_update()
                     .filter(id__in=keys, request_id__in=locked_parent_ids).order_by("pk"))
    except (ValidationError, ValueError):
        items = []
    by_id = {str(item.id): item for item in items}

    unknown = [key for key in keys if key not in by_id]
    wrong_date = [key for key, item in by_id.items() if item.request.delivery_date != delivery_date]
    unavailable = [key for key, item in by_id.items()
                   if item.requested_qty == 0 and item.packed_qty is None]
    missing_revisions = [key for key in keys if not strict or key not in expected_revisions]
    if strict and (unknown or wrong_date or unavailable or missing_revisions):
        raise InvalidRows("Some packing cells are no longer available; nothing was saved.",
                          keys=unknown + wrong_date + unavailable + missing_revisions)
    if not strict:
        # The trusted compatibility path retains the former harmless filtering
        # of wrong-date/unknown cells.
        items = [item for item in items if item.request.delivery_date == delivery_date]

    conflicts = {}
    if strict:
        for item in items:
            if expected_revisions[str(item.id)] != getattr(item, "revision", 0):
                conflicts[str(item.id)] = {
                    "revision": getattr(item, "revision", 0),
                    "requested_qty": item.requested_qty,
                    "packed_qty": item.packed_qty,
                }
    if conflicts:
        raise RevisionConflict("Some packing cells changed in another session; nothing was saved.",
                               conflicts=conflicts)

    # Complete validation precedes the first mutation.  The fresh timestamp is
    # assigned only after locks/version checks, preserving blank (None) versus
    # confirmed zero semantics.
    now = timezone.now()
    changed = 0
    for item in items:
        qty = values[str(item.id)]
        if qty == item.packed_qty:
            continue
        item.packed_qty = qty
        item.packed_by_name = name if qty is not None else ""
        item.packed_at = now if qty is not None else None
        item.revision = getattr(item, "revision", 0) + 1
        item.save(update_fields=["packed_qty", "packed_by_name", "packed_at", "revision"])
        changed += 1
    return changed
