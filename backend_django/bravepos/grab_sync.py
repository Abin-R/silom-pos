"""Grab ↔ BravePOS stock sync: the BravePOS half.

Grab takes an order's units off its own count the moment the order is
accepted, and never puts them back — not even when the order is cancelled.
BravePOS does exactly the same:

Linking a branch to its Grab outlet (``Branch.grab_merchant_id``, set in
Backoffice) is the one switch: from that moment (``grab_linked_at``) the
branch is synced; unlinking stops it.

* ``outlets`` — the linked branches, so the poller knows which outlets to run.
* ``deduct`` — the poller (shopify_django_app) sends every Grab order it sees
  on Grab's live pages.  An order placed before the branch was linked is
  ignored.  The first time BravePOS sees an order, its units are
  deducted ('out' ``StockMovement`` rows).  Sending the same order again
  deducts only units that were added since (an edit); units removed are
  ignored.  The order is remembered in ``GrabOrderDeduction``, so sending it
  every cycle is safe.
* A cancellation needs no call: nothing changes.
* ``heartbeat`` — the poller's cycle finished; only the watchdog reads it.
* ``stock`` — what the poller needs to work out what Grab should show.

The deduction stays in ``StockMovement``: 'out' rows with ``document_no`` set to
the Grab order ID (27 characters; the short display ID like ``GF-921`` repeats,
so it goes in the note instead).  A Grab order ID can never equal an
``Order.order_number``, so the "inventory as of" report does not mistake these
movements for till sales — it undoes them through the ``AuditLog`` rows
``Product.save()`` writes, which is why stock here changes through
``save(update_fields=["stock"])`` and not a queryset ``update()``.

Locking: the order row first (``select_for_update``), then the products in
primary-key order, so two calls for the same order run one after the other and
two orders cannot deadlock.  Till checkout locks its products in id order too.
The ``Branch`` row is never locked here.  The stock read takes no locks.

``GRAB_SYNC_API_KEY`` (environment) is the key the poller sends as
``X-API-Key``; unset means the endpoints answer 503.  ``"dry_run": true`` in a
deduct body returns the would-be result and writes nothing (the poller's
``--dry-run``).

A branch with no ``grab_merchant_id`` is not synced: the poller cannot find it,
and nothing else in BravePOS (till, self-order) reads any Grab table.
"""
from __future__ import annotations

import hmac
import logging
import os
from datetime import timezone as dt_timezone

from django.db import IntegrityError, transaction
from django.utils import timezone as djtz
from django.utils.dateparse import parse_datetime
from rest_framework.response import Response

from . import audit, stock_pool
from .models import Branch, GrabOrderDeduction, GrabSyncStatus, Product, StockMovement

logger = logging.getLogger("bravepos")

API_KEY_ENV = "GRAB_SYNC_API_KEY"

AUDIT_SOURCE = "grab-sync"
CREATED_BY = "Grab sync"

# ``StockMovement.document_no`` is 64 characters.
ORDER_ID_MAX = 64
DISPLAY_ID_MAX = 64              # ``GrabOrderDeduction.display_id`` is 64 too
MAX_ORDERS_PER_CALL = 500

# The Grab merchant IDs a branch may be linked to, and the store name Grab shows
# for each.  Copied from instamator's ``grab_outlets`` (instamator_app/views.py);
# chidlom is left out on purpose — that store closed and is now maneeya.  The
# branch form offers exactly these, or blank.
GRAB_OUTLETS = {
    "3-C2KTWENBEXUENT": "Grab Biohouse",
    "3-C7CCAAMKETC3LX": "Grab Ladprao",
    "3-C6XTLVK2CZJJNE": "Grab Samyan",
    "3-C2M2RUXENZCZGT": "Grab Silom",
    "3-C7DUDGJTLE4GKA": "Grab Punnawithi",
    "3-C7JDEBAWAVLATE": "Grab Central World",
    "3-C7JECZBXGBADAA": "Grab Grand De Cafe",
    "3-C7KJGBUEJNDWLX": "Grab Thonburi-Klong San",
    "3-C7MCT7BAEJ2ZGX": "Grab Dusit Central Park",
    "3-C6WAWE2YTTVUGN": "Grab Maneeya Center",
    "3-C3D3JJVUEYVZT6": "Grab Little Cafe",
    "3-C7C2EVKXCPTYVN": "Grab Habito Mall",
}

class BadRequest(Exception):
    """The body is not something we can act on — a 400, never a 500."""


# ── Auth and switches ───────────────────────────────────────────────────────
def check_api_key(request):
    """None when the caller may proceed, else the Response to send back.

    503 first: with no key configured nothing can be authorised, and saying so
    is more useful to whoever is deploying than a 401 for every call.
    """
    expected = os.environ.get(API_KEY_ENV, "")
    if not expected:
        return Response({"detail": "Grab sync is not configured."}, status=503)
    given = request.META.get("HTTP_X_API_KEY", "")
    # Bytes, so a non-ASCII header is a wrong key and not a TypeError.
    if not hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")):
        return Response({"detail": "Invalid API key."}, status=401)
    return None


def mark_audit_source(request):
    """Say in the audit log that these product changes came from the sync, not
    from a person on a till.  Keeps the request's method/path/IP the middleware
    already captured."""
    ctx = audit.get_context()
    audit.set_context(
        actor=None, source=AUDIT_SOURCE,
        method=ctx.get("method") or request.method or "",
        path=ctx.get("path") or request.path or "",
        ip=ctx.get("ip"),
        user_agent=ctx.get("user_agent") or "",
    )


# ── Input ───────────────────────────────────────────────────────────────────
def _text(value, name, *, required=True, max_length=None):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise BadRequest(f"{name} must be a string.")
    value = value.strip()
    if required and not value:
        raise BadRequest(f"{name} is required.")
    if max_length and len(value) > max_length:
        raise BadRequest(f"{name} is too long.")
    return value


def _when(value, name):
    """An optional ISO-8601 time, aware (naive is read as UTC), or None."""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise BadRequest(f"{name} must be an ISO-8601 time.")
    try:
        parsed = parse_datetime(value.strip())
    except ValueError:
        parsed = None
    if parsed is None:
        raise BadRequest(f"{name} must be an ISO-8601 time.")
    if djtz.is_naive(parsed):
        parsed = djtz.make_aware(parsed, dt_timezone.utc)
    return parsed


def parse_merchant_id(value) -> str:
    # Never blank: a blank merchant ID would match every branch that is not synced.
    return _text(value, "merchant_id", max_length=32).upper()


def parse_items(raw) -> list[dict]:
    """``[{barcode, qty, grab_item_id, name, bundle_item_id}]`` with duplicate
    barcodes merged (quantities added up).  A line with no barcode is kept, so
    it can be reported as skipped rather than vanish."""
    if not isinstance(raw, list):
        raise BadRequest("items must be a list.")
    lines: list[dict] = []
    by_barcode: dict[str, dict] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise BadRequest("Each item must be an object.")
        qty = item.get("qty")
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
            raise BadRequest("Each item needs a whole-number qty of at least 1.")
        barcode = _text(item.get("barcode"), "barcode", required=False, max_length=64)
        grab_item_id = _text(item.get("grab_item_id"), "grab_item_id",
                             required=False, max_length=64)
        name = _text(item.get("name"), "name", required=False, max_length=200)
        bundle_item_id = _text(item.get("bundle_item_id"), "bundle_item_id",
                               required=False, max_length=64)
        if barcode and barcode in by_barcode:
            line = by_barcode[barcode]
            line["qty"] += qty
            line["bundle_item_id"] = line["bundle_item_id"] or bundle_item_id
            continue
        line = {"barcode": barcode, "qty": qty, "grab_item_id": grab_item_id,
                "name": name, "bundle_item_id": bundle_item_id}
        lines.append(line)
        if barcode:
            by_barcode[barcode] = line
    return lines


def _parse_order(o) -> dict:
    """One order of the batch, validated; ``BadRequest`` if it is not usable."""
    if not isinstance(o, dict):
        raise BadRequest("Each order must be an object.")
    return {
        "order_id": _text(o.get("order_id"), "order_id", max_length=ORDER_ID_MAX),
        "display_id": _text(o.get("display_id"), "display_id",
                            required=False, max_length=DISPLAY_ID_MAX),
        "placed_at": _when(o.get("placed_at"), "placed_at"),
        "items": parse_items(o.get("items") if o.get("items") is not None else []),
    }


def parse_deduct(data: dict) -> dict:
    """``{merchant_id, orders, invalid, dry_run}`` for the batch deduct call.

    Only a problem with the call as a whole (body, merchant, ``orders``) is a
    ``BadRequest``.  An order that cannot be read goes to ``invalid`` —
    ``{index, order_id, detail}`` — and the rest of the batch still goes
    through: one bad order must not stall every other order at the outlet.
    """
    if not isinstance(data, dict):
        raise BadRequest("Body must be a JSON object.")
    merchant_id = parse_merchant_id(data.get("merchant_id"))
    raw = data.get("orders")
    if not isinstance(raw, list):
        raise BadRequest("orders must be a list.")
    if len(raw) > MAX_ORDERS_PER_CALL:
        raise BadRequest("Too many orders in one call.")
    orders, invalid, seen = [], [], set()
    for index, o in enumerate(raw):
        try:
            order = _parse_order(o)
            if order["order_id"] in seen:
                raise BadRequest(f"order_id {order['order_id']} appears twice.")
        except BadRequest as e:
            raw_id = o.get("order_id") if isinstance(o, dict) else None
            invalid.append({
                "index": index,
                "order_id": raw_id.strip()[:ORDER_ID_MAX] if isinstance(raw_id, str) else "",
                "detail": str(e),
            })
            continue
        seen.add(order["order_id"])
        orders.append(order)
    return {"merchant_id": merchant_id, "orders": orders, "invalid": invalid,
            "dry_run": data.get("dry_run") is True}


def parse_heartbeat(data: dict) -> dict:
    """``{merchant_id, cycle_ok, problem, dry_run}``.  Anything else the poller
    sends (older pollers send ``push_live`` and ``grab_shown``) is ignored."""
    if not isinstance(data, dict):
        raise BadRequest("Body must be a JSON object.")
    if not isinstance(data.get("cycle_ok"), bool):
        raise BadRequest("cycle_ok must be true or false.")
    return {
        "merchant_id": parse_merchant_id(data.get("merchant_id")),
        "cycle_ok": data["cycle_ok"],
        "problem": _text(data.get("problem"), "problem", required=False, max_length=500),
        "dry_run": data.get("dry_run") is True,
    }


def find_branch(merchant_id: str):
    """The active branch this Grab outlet belongs to, or None."""
    return Branch.objects.filter(grab_merchant_id=merchant_id, active=True).first()


def iso_z(when) -> str:
    return when.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Helpers ─────────────────────────────────────────────────────────────────
def _line(product, qty, stock_after, skipped_reason=None, barcode=None):
    return {
        "barcode": product.barcode if product is not None else (barcode or ""),
        "product_id": str(product.id) if product is not None else "",
        "qty": qty,
        "stock_after": stock_after,
        "skipped_reason": skipped_reason,
    }


def _lock_or_create(branch, order_id, **fields):
    """``(row, created)``, the row locked.  Two callers creating the same order
    at once: one insert wins, the other hits the unique constraint and locks
    the winner's row instead."""
    for _attempt in range(3):
        row = (GrabOrderDeduction.objects.select_for_update()
               .filter(branch=branch, order_id=order_id).first())
        if row is not None:
            return row, False
        try:
            with transaction.atomic():
                row = GrabOrderDeduction.objects.create(
                    branch=branch, order_id=order_id, **fields)
            return row, True
        except IntegrityError:
            continue
    raise RuntimeError(f"Could not create or lock Grab order {order_id}.")


def _products_by_barcode(branch, barcodes, *, lock=False):
    qs = Product.objects.filter(branch=branch, active=True, barcode__in=list(barcodes))
    if lock:
        qs = qs.select_for_update()
    return {p.barcode: p for p in qs.order_by("id")}


def _map_items(items, products):
    """Split the lines into ``[(product, item)]`` and the skipped ones."""
    mapped, skipped = [], []
    for item in items:
        product = products.get(item["barcode"]) if item["barcode"] else None
        if product is None:
            skipped.append({
                "barcode": item["barcode"], "grab_item_id": item.get("grab_item_id", ""),
                "reason": "not_at_branch" if item["barcode"] else "no_barcode",
            })
        else:
            mapped.append((product, item))
    return mapped, skipped


def _units_by_product(mapped) -> dict:
    """``{product: qty}`` from ``[(product, item)]``, in product-id order."""
    totals = {}
    for product, item in mapped:
        totals[product] = totals.get(product, 0) + item["qty"]
    return dict(sorted(totals.items(), key=lambda kv: kv[0].id))


def _write_stock(branch, product, delta, *, order_id, note):
    """Move one locked product's stock by ``delta`` (negative) and write the
    'out' movement.  ``save()``, not ``update()``: this is what writes the
    AuditLog row "Inventory as of" works back from.  Negative stock is allowed,
    as at the till.  Returns the stock afterwards."""
    product.stock = (product.stock or 0) + delta
    product.save(update_fields=["stock"])
    StockMovement.objects.create(
        branch=branch, product=product, product_name=product.name,
        type="out", qty=abs(delta), note=note,
        document_no=order_id, created_by=CREATED_BY,
    )
    return product.stock


# ── deduct ──────────────────────────────────────────────────────────────────
def deduct_orders(branch, orders, *, dry_run=False) -> dict:
    """Deduct every order in the batch.  One transaction per order, so a
    problem with one never loses the rest."""
    return {"results": [_deduct_one(branch, order, dry_run) for order in orders]}


def _deduct_one(branch, order, dry_run) -> dict:
    """Deduct what this order has that BravePOS has not deducted for it yet.

    ``status``: ``deducted`` (first time), ``updated`` (units were added since),
    ``unchanged``, ``before_link`` (placed before the branch was linked), or
    ``dry_run`` when a real call would have written.  Units
    removed from the order are never put back.
    """
    if (branch.grab_linked_at and order["placed_at"]
            and order["placed_at"] < branch.grab_linked_at):
        # Placed before this branch was linked to Grab: the stock count at
        # linking already reflects it.
        return {"order_id": order["order_id"], "status": "before_link", "lines": [],
                "skipped": []}
    now = djtz.now()
    barcodes = {i["barcode"] for i in order["items"] if i["barcode"]}
    with transaction.atomic():
        if dry_run:
            row = GrabOrderDeduction.objects.filter(
                branch=branch, order_id=order["order_id"]).first()
            created = row is None
        else:
            row, created = _lock_or_create(
                branch, order["order_id"], display_id=order["display_id"],
                placed_at=order["placed_at"], first_seen_at=now, changed_at=now)
        products = _products_by_barcode(branch, barcodes, lock=not dry_run)
        mapped, skipped = _map_items(order["items"], products)
        done = {pid: int(q) for pid, q in ((row.deducted if row else None) or {}).items()}
        lines = [_line(None, 0, None, s["reason"], s["barcode"]) for s in skipped]
        note = f"Grab {order['display_id'] or (row.display_id if row else '')}".strip()
        wrote = False
        for product, qty in _units_by_product(mapped).items():
            pid = str(product.id)
            delta = qty - done.get(pid, 0)
            if delta <= 0:
                continue
            stock_after = (product.stock or 0) - delta
            if not dry_run:
                stock_after = _write_stock(branch, product, -delta,
                                           order_id=order["order_id"], note=note)
            done[pid] = qty
            lines.append(_line(product, delta, stock_after))
            wrote = True
        result = {
            "order_id": order["order_id"], "lines": lines,
            "skipped": [{"barcode": s["barcode"], "reason": s["reason"]} for s in skipped],
        }
        if dry_run:
            result["status"] = "dry_run" if wrote else "unchanged"
            return result
        fields = []
        if wrote:
            row.deducted, row.changed_at = done, now
            fields += ["deducted", "changed_at"]
        if order["display_id"] and row.display_id != order["display_id"]:
            row.display_id = order["display_id"]
            fields.append("display_id")
        if fields:
            row.save(update_fields=fields)
        result["status"] = ("deducted" if created else "updated") if wrote else "unchanged"
        return result


# ── heartbeat ───────────────────────────────────────────────────────────────
def heartbeat(branch, beat, *, dry_run=False) -> dict:
    """The poller's cycle finished.  A good cycle stamps ``last_ok_at``; a named
    ``problem`` is recorded whatever ``cycle_ok`` says, and a good cycle that
    names none clears it.  Only the watchdog reads this — nothing a cashier
    does depends on it."""
    if dry_run:
        return {"ok": True}
    now = djtz.now()
    with transaction.atomic():
        status, _ = GrabSyncStatus.objects.select_for_update().get_or_create(branch=branch)
        if beat["cycle_ok"]:
            status.last_ok_at = now
        if beat["problem"]:
            status.problem = beat["problem"]
        elif beat["cycle_ok"]:
            status.problem = ""
        status.save()
    return {"ok": True}


# ── outlets ─────────────────────────────────────────────────────────────────
def linked_outlets() -> dict:
    """Every active branch linked to a Grab outlet: what the poller runs."""
    rows = (Branch.objects.filter(active=True).exclude(grab_merchant_id="")
            .order_by("name").values_list("grab_merchant_id", "name", "grab_linked_at"))
    return {"outlets": [
        {"merchant_id": mid, "branch": name, "linked_at": iso_z(linked) if linked else None}
        for mid, name, linked in rows]}


# ── stock read ──────────────────────────────────────────────────────────────
def stock_for_branch(branch) -> dict:
    """What the poller needs to work out what Grab should show.  No locks.

    Self-order holds are read before the products: a self-order paid in
    between is then counted twice (still held, and already off the stock), so
    the target comes out low for one cycle, never high.
    """
    selfh = stock_pool.self_held(branch)
    products = list(Product.objects.filter(branch=branch, active=True)
                    .exclude(barcode="").order_by("barcode"))
    avail = stock_pool.availability(branch, products, selfh=selfh)
    rows = [{
        "barcode": p.barcode, "product_id": str(p.id), "name": p.name,
        "stock": avail[str(p.id)]["stock"],
        "counter_stock": avail[str(p.id)]["counter_stock"],
        "self_held": avail[str(p.id)]["self_held"],
        "grab_target": avail[str(p.id)]["grab_target"],
    } for p in products]
    changed = {bc: iso_z(when) for bc, when in
               stock_pool.target_changed_at(branch, products).items()}
    return {"branch": branch.name, "products": rows, "target_changed_at": changed}
