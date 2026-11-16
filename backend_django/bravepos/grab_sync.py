"""Grab ↔ BravePOS stock sync: the BravePOS half.

An outside poller (shopify_django_app) watches a branch's Grab outlet and tells
BravePOS about its orders.  BravePOS keeps every open Grab order as a **hold**
(``GrabOrderHold``): Grab lowers its own count when an order is placed, so from
that moment the units are spoken for even though they are still on the shelf.

* ``hold`` — a Grab order has been seen: hold its units (batch upsert).
* ``convert`` — the order is packed (Ready or picked up): the hold becomes a
  deduction, as 'out' ``StockMovement`` rows, in one transaction.
* ``release`` — it was cancelled before that: drop the hold, no stock moves.
* ``restore`` — it was cancelled after: put the deduction back.
* ``correct`` — the order was edited: apply only the difference.
* ``review`` — the poller cannot tell: open a review for staff.
* ``heartbeat`` — the poller's cycle finished; see ``stock_pool.outage_shown``.
* ``stock`` — what the poller needs to work out what Grab should show.

BravePOS is the source of truth.  What each channel may sell is worked out in
``stock_pool``; this module only keeps the holds and the deductions.

The deduction stays in ``StockMovement``: 'out' rows with ``document_no`` set to
the Grab order ID (27 characters; the short display ID like ``GF-921`` repeats,
so it goes in the note instead), 'return' rows for a restore or a correction
that takes units back.  A Grab order ID can never equal an ``Order.order_number``,
so the "inventory as of" report does not mistake these movements for till sales
— it undoes them through the ``AuditLog`` rows ``Product.save()`` writes, which
is why stock here changes through ``save(update_fields=["stock"])`` and not a
queryset ``update()``.

Locking: the hold row first (``select_for_update``), then the products in
primary-key order, so two calls for the same order take the same locks one
after the other and two different orders cannot deadlock.  The ``Branch`` row is
never locked — till checkout locks it, and a Grab call has no business making a
cashier wait.

Safety switches, both from the environment:

* ``GRAB_SYNC_API_KEY`` — the key the poller sends as ``X-API-Key``.  Unset
  means the endpoints are not configured and answer 503.
* ``GRAB_SYNC_WRITES`` — must be ``1`` for anything to be written.  Without it,
  or with ``"dry_run": true`` in the body, every lookup is done and the
  would-be result returned, and nothing is written.

A branch with no ``grab_merchant_id`` is not synced: it cannot be found here,
and any old holds it has are ignored by ``stock_pool``.
"""
from __future__ import annotations

import hmac
import logging
import os
from datetime import timedelta, timezone as dt_timezone

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone as djtz
from django.utils.dateparse import parse_datetime
from rest_framework.response import Response

from . import audit, seatalk, stock_pool
from .models import Branch, GrabOrderHold, GrabSyncStatus, Product, StockMovement

logger = logging.getLogger("bravepos")

API_KEY_ENV = "GRAB_SYNC_API_KEY"
WRITES_ENV = "GRAB_SYNC_WRITES"

AUDIT_SOURCE = "grab-sync"
CREATED_BY = "Grab sync"

# ``StockMovement.document_no`` is 64 characters.
ORDER_ID_MAX = 64
MAX_ORDERS_PER_HOLD_CALL = 500

# How long a converted order stays in the stock read's ``open_orders``, so the
# poller can watch it to ``COMPLETED``.
OPEN_ORDER_HOURS = 24

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

REVIEW_TEXT = {
    "unclear_pickup": "Cancelled after the driver may have taken the food, and we cannot "
                      "tell whether the bag left",
    "unmapped_item": "An item on it does not match a product at this branch",
    "bundle_mismatch": "Its bundle choices do not add up to the bundle",
    "large_order_mismatch": "Its items do not match what Grab reports for the order",
    "stale": "Open for hours and nothing has settled it",
    "till_took_held": "The till sold units that were held for it, so the bag may be short",
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


def writes_enabled() -> bool:
    return os.environ.get(WRITES_ENV, "") == "1"


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


def parse_order(data: dict) -> dict:
    """The fields the order endpoints share, validated."""
    if not isinstance(data, dict):
        raise BadRequest("Body must be a JSON object.")
    return {
        "merchant_id": parse_merchant_id(data.get("merchant_id")),
        "order_id": _text(data.get("order_id"), "order_id", max_length=ORDER_ID_MAX),
        "display_id": _text(data.get("display_id"), "display_id",
                            required=False, max_length=32),
        # Why a release/restore happened (Grab's status); convert ignores it.
        "reason": _text(data.get("reason"), "reason", required=False, max_length=64),
        # The Grab page or state the order was last seen in.
        "grab_state": _text(data.get("grab_state"), "grab_state",
                            required=False, max_length=32),
        "ready_at": _when(data.get("ready_at"), "ready_at"),
        # The poller's GRAB_STOCK_SINCE: a Ready time before it is an order
        # packed before go-live, whose stock the count at go-live already knows.
        "go_live_at": _when(data.get("go_live_at") or data.get("since"), "go_live_at"),
        "dry_run": data.get("dry_run") is True,
    }


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


def parse_hold_orders(data: dict) -> tuple[str, list[dict], bool]:
    """``(merchant_id, [order, ...], dry_run)`` for the batch hold call."""
    if not isinstance(data, dict):
        raise BadRequest("Body must be a JSON object.")
    merchant_id = parse_merchant_id(data.get("merchant_id"))
    raw = data.get("orders")
    if not isinstance(raw, list):
        raise BadRequest("orders must be a list.")
    if len(raw) > MAX_ORDERS_PER_HOLD_CALL:
        raise BadRequest("Too many orders in one call.")
    orders, seen = [], set()
    for o in raw:
        if not isinstance(o, dict):
            raise BadRequest("Each order must be an object.")
        order_id = _text(o.get("order_id"), "order_id", max_length=ORDER_ID_MAX)
        if order_id in seen:
            raise BadRequest(f"order_id {order_id} appears twice.")
        seen.add(order_id)
        orders.append({
            "order_id": order_id,
            "display_id": _text(o.get("display_id"), "display_id",
                                required=False, max_length=32),
            "placed_at": _when(o.get("placed_at"), "placed_at"),
            "grab_state": _text(o.get("grab_state"), "grab_state",
                                required=False, max_length=32),
            "items": parse_items(o.get("items") if o.get("items") is not None else []),
        })
    return merchant_id, orders, data.get("dry_run") is True


def parse_review(data: dict) -> dict:
    order = parse_order(data)
    reason = _text(data.get("reason"), "reason", max_length=32)
    if reason not in GrabOrderHold.REVIEW_REASONS:
        raise BadRequest("reason is not one of: " + ", ".join(GrabOrderHold.REVIEW_REASONS) + ".")
    detail = data.get("detail")
    if detail is None:
        detail = {}
    if not isinstance(detail, dict):
        raise BadRequest("detail must be an object.")
    order["reason"], order["detail"] = reason, detail
    return order


def parse_heartbeat(data: dict) -> dict:
    if not isinstance(data, dict):
        raise BadRequest("Body must be a JSON object.")
    shown = data.get("grab_shown")
    if shown is None:
        shown = {}
    if not isinstance(shown, dict):
        raise BadRequest("grab_shown must be an object.")
    clean = {}
    for barcode, units in shown.items():
        if isinstance(units, bool) or not isinstance(units, int) or units < 0:
            raise BadRequest("grab_shown values must be whole numbers, 0 or more.")
        clean[str(barcode)] = units
    for flag in ("cycle_ok", "push_live"):
        if not isinstance(data.get(flag), bool):
            raise BadRequest(f"{flag} must be true or false.")
    return {
        "merchant_id": parse_merchant_id(data.get("merchant_id")),
        "cycle_ok": data["cycle_ok"],
        "push_live": data["push_live"],
        "problem": _text(data.get("problem"), "problem", required=False, max_length=500),
        "grab_shown": clean,
        "dry_run": data.get("dry_run") is True,
    }


def find_branch(merchant_id: str):
    """The active branch this Grab outlet belongs to, or None."""
    return Branch.objects.filter(grab_merchant_id=merchant_id, active=True).first()


def iso_z(when) -> str:
    return when.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Alerts and reviews ──────────────────────────────────────────────────────
def alert(text: str):
    """Tell the Grab SeaTalk group, once the surrounding transaction commits —
    never from inside a lock.  Quietly nothing when no group is configured."""
    group = seatalk.grab_group_id()
    if not group:
        return
    transaction.on_commit(lambda: seatalk.send_group_text(group, text))


def _order_name(hold) -> str:
    return hold.display_id or hold.order_id


def open_review(hold, reason: str, detail: dict | None = None) -> bool:
    """Open (or refresh) a review on a locked hold row.  Returns True when a
    review was newly opened.

    Idempotent: the poller sends the same case every cycle.  A review staff have
    settled does not reopen for the same reason, and each reason alerts SeaTalk
    once per order.
    """
    detail = dict(detail or {})
    old = hold.review_detail if isinstance(hold.review_detail, dict) else {}
    alerted = list(old.get("_alerted") or [])
    if hold.review_status == "done" and hold.review_reason == reason:
        return False
    if hold.review_status == "open" and hold.review_reason == reason:
        merged = {**detail, "_alerted": alerted}
        if merged != old:
            hold.review_detail = merged
            hold.save(update_fields=["review_detail"])
        return False
    if hold.review_status == "open" and hold.review_reason:
        detail["earlier_reason"] = hold.review_reason
    first_time = reason not in alerted
    if first_time:
        alerted.append(reason)
    hold.review_status = "open"
    hold.review_reason = reason
    hold.review_detail = {**detail, "_alerted": alerted}
    hold.resolved_by, hold.resolved_at, hold.resolution = "", None, ""
    hold.save(update_fields=[
        "review_status", "review_reason", "review_detail",
        "resolved_by", "resolved_at", "resolution",
    ])
    if first_time:
        alert(
            f"Grab order {_order_name(hold)} at {hold.branch.name} needs a look. "
            f"{REVIEW_TEXT.get(reason, reason)}. "
            f"Open Backoffice > Grab review to deduct, restore or ignore it."
        )
    return True


# ── Product and hold lookups ────────────────────────────────────────────────
def _line(product, qty, stock_after, skipped_reason=None, barcode=None):
    return {
        "barcode": product.barcode if product is not None else (barcode or ""),
        "product_id": str(product.id) if product is not None else "",
        "qty": qty,
        "stock_after": stock_after,
        "skipped_reason": skipped_reason,
    }


def _lock_hold(branch, order_id):
    return (GrabOrderHold.objects.select_for_update()
            .filter(branch=branch, order_id=order_id).select_related("branch").first())


def _lock_or_create(branch, order_id, **fields):
    """``(hold, created)``, the row locked.  Two callers creating the same order
    at once: one insert wins, the other hits the unique constraint and locks
    the winner's row instead."""
    for _attempt in range(3):
        hold = _lock_hold(branch, order_id)
        if hold is not None:
            return hold, False
        try:
            with transaction.atomic():
                hold = GrabOrderHold.objects.create(branch=branch, order_id=order_id, **fields)
            hold.branch = branch
            return hold, True
        except IntegrityError:
            continue
    raise RuntimeError(f"Could not create or lock Grab hold {order_id}.")


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


def _hold_items(mapped):
    return [{
        "product_id": str(product.id), "barcode": product.barcode, "qty": item["qty"],
        "grab_item_id": item.get("grab_item_id", ""), "name": item.get("name", "") or product.name,
        "bundle_item_id": item.get("bundle_item_id", ""),
    } for product, item in mapped]


def _skipped_detail(skipped):
    return {"skipped": [{"barcode": s["barcode"], "grab_item_id": s["grab_item_id"],
                         "reason": s["reason"]} for s in skipped]}


def _write_stock(branch, product, delta, *, type_, order_id, note):
    """Move one locked product's stock by ``delta`` and write the movement.

    ``save()``, not ``update()``: this is what writes the AuditLog row
    "Inventory as of" works back from.  Negative stock is allowed, as at the
    till.  Returns the stock afterwards.
    """
    product.stock = (product.stock or 0) + delta
    product.save(update_fields=["stock"])
    StockMovement.objects.create(
        branch=branch, product=product, product_name=product.name,
        type=type_, qty=abs(delta), note=note,
        document_no=order_id, created_by=CREATED_BY,
    )
    return product.stock


def _counted_after(branch, product_ids, since):
    """Product ids with an 'adjust' movement (a stock count) after ``since``.
    Staff count shelves without the Grab bags already packed, so that count
    left those units out and deducting them again would count them twice."""
    if since is None or not product_ids:
        return set()
    return {
        str(pk) for pk in StockMovement.objects.filter(
            branch=branch, product_id__in=list(product_ids),
            type="adjust", created_at__gt=since,
        ).values_list("product_id", flat=True).distinct()
    }


# ── hold ────────────────────────────────────────────────────────────────────
def hold_orders(branch, orders, *, dry_run=False) -> dict:
    """Hold every order in the batch.  One transaction per order, so a problem
    with one never loses the rest."""
    dry_run = dry_run or not writes_enabled()
    results = []
    for order in orders:
        results.append(_hold_one(branch, order, dry_run))
    return {"results": results}


def _hold_one(branch, order, dry_run) -> dict:
    barcodes = {i["barcode"] for i in order["items"] if i["barcode"]}
    mapped, skipped = _map_items(order["items"], _products_by_barcode(branch, barcodes))
    items = _hold_items(mapped)
    result = {
        "order_id": order["order_id"], "status": GrabOrderHold.HELD, "created": False,
        "skipped": [{"barcode": s["barcode"], "reason": s["reason"]} for s in skipped],
    }
    if dry_run:
        existing = GrabOrderHold.objects.filter(branch=branch, order_id=order["order_id"]).first()
        result["created"] = existing is None
        result["status"] = existing.status if existing else GrabOrderHold.HELD
        return result

    now = djtz.now()
    with transaction.atomic():
        hold, created = _lock_or_create(
            branch, order["order_id"], display_id=order["display_id"], items=items,
            grab_state=order["grab_state"], placed_at=order["placed_at"],
            first_seen_at=now, last_seen_at=now, changed_at=now,
        )
        result["created"] = created
        result["status"] = hold.status
        if hold.status != GrabOrderHold.HELD:
            return result              # settled already; the call only reports it
        if not created:
            changed = ["last_seen_at"]
            hold.last_seen_at = now
            if hold.items != items:
                hold.items, hold.changed_at = items, now
                changed += ["items", "changed_at"]
            if order["grab_state"] and hold.grab_state != order["grab_state"]:
                hold.grab_state = order["grab_state"]
                changed.append("grab_state")
            if order["display_id"] and hold.display_id != order["display_id"]:
                hold.display_id = order["display_id"]
                changed.append("display_id")
            if order["placed_at"] and hold.placed_at is None:
                hold.placed_at = order["placed_at"]
                changed.append("placed_at")
            hold.save(update_fields=changed)
        if skipped:
            open_review(hold, "unmapped_item", _skipped_detail(skipped))
    return result


# ── convert ─────────────────────────────────────────────────────────────────
def convert_order(branch, order_id, display_id, items, *, ready_at=None, go_live_at=None,
                  grab_state="", dry_run=False) -> dict:
    """Turn a Grab order's hold into a deduction, once.

    ``items`` are the Ready-time re-read of the order (``None`` means: use what
    the hold has).  Returns ``{"status", "lines"}``; ``status`` is ``converted``,
    ``already_converted``, ``already_restored``, ``released_earlier``,
    ``nothing_to_convert`` or ``dry_run`` (reported only when a real call would
    have written).
    """
    dry_run = dry_run or not writes_enabled()
    now = djtz.now()
    with transaction.atomic():
        if dry_run:
            hold = _lock_hold(branch, order_id) or GrabOrderHold(
                branch=branch, order_id=order_id, display_id=display_id)
        else:
            hold, _created = _lock_or_create(
                branch, order_id, display_id=display_id, grab_state=grab_state,
                first_seen_at=now, last_seen_at=now, changed_at=now,
            )
        return _convert_locked(
            branch, hold, display_id or hold.display_id, items, ready_at=ready_at,
            go_live_at=go_live_at, grab_state=grab_state, dry_run=dry_run, now=now,
        )


def _convert_locked(branch, hold, display_id, items, *, ready_at=None, go_live_at=None,
                    grab_state="", dry_run=False, now=None) -> dict:
    now = now or djtz.now()
    if hold.status == GrabOrderHold.CONVERTED:
        return {"status": "already_converted", "lines": _deducted_lines(branch, hold)}
    if hold.status == GrabOrderHold.RESTORED:
        return {"status": "already_restored", "lines": []}
    if hold.status == GrabOrderHold.RELEASED:
        return {"status": "released_earlier", "lines": []}

    if items is None:
        items = [{"barcode": i.get("barcode", ""), "qty": i.get("qty", 0),
                  "grab_item_id": i.get("grab_item_id", ""), "name": i.get("name", ""),
                  "bundle_item_id": i.get("bundle_item_id", "")}
                 for i in (hold.items or []) if int(i.get("qty") or 0) > 0]
    barcodes = {i["barcode"] for i in items if i["barcode"]}

    # Packed before go-live: the count at go-live already knows these units.
    if go_live_at and ready_at and ready_at < go_live_at:
        plain = _products_by_barcode(branch, barcodes)
        mapped, skipped = _map_items(items, plain)
        lines = [_line(p, i["qty"], p.stock, "before_go_live") for p, i in mapped]
        lines += [_line(None, 0, None, s["reason"], s["barcode"]) for s in skipped]
        if dry_run:
            return {"status": "dry_run", "lines": lines}
        _finish_convert(hold, display_id, _hold_items(mapped), {}, ready_at, grab_state, now,
                        close_reason="before_go_live")
        return {"status": "converted", "lines": lines}

    # The products, locked in id order, then the ledger: only now is it safe to act on.
    products = _products_by_barcode(branch, barcodes, lock=True)
    already_out = not hold._state.adding and StockMovement.objects.filter(
        branch=branch, document_no=hold.order_id, type="out").exists()
    if already_out:
        # Second guard: stock for this order already left, whatever the row says.
        if not dry_run:
            hold.status = GrabOrderHold.CONVERTED
            hold.converted_at = hold.converted_at or now
            hold.save(update_fields=["status", "converted_at"])
        return {"status": "already_converted", "lines": []}

    mapped, skipped = _map_items(items, products)
    counted = _counted_after(branch, [p.id for p, _i in mapped], ready_at)
    to_take, counted_lines = [], []
    for product, item in mapped:
        if str(product.id) in counted:
            counted_lines.append((product, item))
        else:
            to_take.append((product, item))

    lines = []
    for product, item in counted_lines:
        lines.append(_line(product, item["qty"], product.stock, "counted_after_packing"))
    for s in skipped:
        lines.append(_line(None, 0, None, s["reason"], s["barcode"]))

    if not to_take and not counted_lines:
        if skipped and not dry_run and not hold._state.adding:
            open_review(hold, "unmapped_item", _skipped_detail(skipped))
        return {"status": "nothing_to_convert", "lines": lines}

    deducted = {}
    note = f"Grab {display_id}".strip()
    for product, qty in _sum_by_product(to_take).items():
        stock_after = (product.stock or 0) - qty
        if not dry_run:
            stock_after = _write_stock(branch, product, -qty, type_="out",
                                       order_id=hold.order_id, note=note)
        deducted[str(product.id)] = qty
        lines.append(_line(product, qty, stock_after))

    if dry_run:
        return {"status": "dry_run", "lines": lines}
    _finish_convert(hold, display_id, _hold_items(mapped), deducted, ready_at, grab_state, now)
    if skipped:
        open_review(hold, "unmapped_item", _skipped_detail(skipped))
    return {"status": "converted", "lines": lines}


def _sum_by_product(pairs):
    """``{product: qty}`` from ``[(product, item)]``, in product-id order."""
    totals = {}
    for product, item in pairs:
        totals[product] = totals.get(product, 0) + item["qty"]
    return dict(sorted(totals.items(), key=lambda kv: kv[0].id))


def _finish_convert(hold, display_id, items, deducted, ready_at, grab_state, now, close_reason=""):
    hold.status = GrabOrderHold.CONVERTED
    hold.items = items
    hold.deducted = deducted
    hold.converted_at = now
    hold.changed_at = now
    hold.last_seen_at = now
    fields = ["status", "items", "deducted", "converted_at", "changed_at", "last_seen_at"]
    if display_id and hold.display_id != display_id:
        hold.display_id = display_id
        fields.append("display_id")
    if ready_at:
        hold.ready_at = ready_at
        fields.append("ready_at")
    if grab_state:
        hold.grab_state = grab_state
        fields.append("grab_state")
    if close_reason:
        hold.close_reason, hold.closed_at = close_reason, now
        fields += ["close_reason", "closed_at"]
    hold.save(update_fields=fields)


def _deducted_lines(branch, hold):
    ids = list(hold.deducted or {})
    products = {str(p.id): p for p in Product.objects.filter(branch=branch, pk__in=ids)}
    return [_line(products[pid], qty, products[pid].stock)
            for pid, qty in (hold.deducted or {}).items() if pid in products]


# ── release ─────────────────────────────────────────────────────────────────
def release_order(branch, order_id, reason="", *, dry_run=False) -> dict:
    """A Grab order cancelled before it was packed: drop its hold.  No stock
    moves.  ``status`` is ``released``, ``already_released``, ``converted`` (too
    late — the poller must restore it) or ``unknown``."""
    dry_run = dry_run or not writes_enabled()
    with transaction.atomic():
        hold = _lock_hold(branch, order_id)
        if hold is None:
            return {"status": "unknown"}
        if hold.status == GrabOrderHold.CONVERTED:
            return {"status": "converted"}
        if hold.status in (GrabOrderHold.RELEASED, GrabOrderHold.RESTORED):
            return {"status": "already_released"}
        if not dry_run:
            _release_locked(hold, reason or "released", djtz.now())
        return {"status": "released"}


def _release_locked(hold, reason, now):
    hold.status = GrabOrderHold.RELEASED
    hold.closed_at, hold.close_reason = now, reason[:64]
    hold.changed_at = now
    hold.save(update_fields=["status", "closed_at", "close_reason", "changed_at"])


# ── restore ─────────────────────────────────────────────────────────────────
def restore_order(branch, order_id, display_id, reason="", *, grab_state="", dry_run=False) -> dict:
    """Put a cancelled Grab order's stock back, once.

    Returns ``{"status", "lines"}``; ``status`` is ``restored``,
    ``already_restored``, ``not_converted`` (never converted: nothing to put
    back — release it instead) or ``dry_run``.  Only a converted order can be
    restored, and it is never converted again.
    """
    dry_run = dry_run or not writes_enabled()
    with transaction.atomic():
        hold = _lock_hold(branch, order_id)
        return _restore_locked(branch, hold, display_id, reason, grab_state=grab_state,
                               dry_run=dry_run, now=djtz.now())


def _restore_locked(branch, hold, display_id, reason, *, grab_state="", dry_run=False, now=None):
    now = now or djtz.now()
    if hold is None or hold.status in (GrabOrderHold.HELD, GrabOrderHold.RELEASED):
        return {"status": "not_converted", "lines": []}
    if hold.status == GrabOrderHold.RESTORED:
        return {"status": "already_restored", "lines": []}
    deducted = {pid: qty for pid, qty in (hold.deducted or {}).items() if qty}
    locked = {
        str(p.id): p for p in Product.objects.select_for_update()
        .filter(branch=branch, pk__in=list(deducted)).order_by("id")
    }
    display_id = display_id or hold.display_id
    note = f"Grab {display_id} cancelled".strip()
    if reason:
        note += f" ({reason})"
    lines = []
    for pid, qty in sorted(deducted.items()):
        product = locked.get(pid)
        if product is None:
            # The deduction is on record but the row is not here (a product moved
            # off this branch).  Nothing to put back onto.
            continue
        stock_after = (product.stock or 0) + qty
        if not dry_run:
            stock_after = _write_stock(branch, product, qty, type_="return",
                                       order_id=hold.order_id, note=note)
        lines.append(_line(product, qty, stock_after))
    if dry_run:
        return {"status": "dry_run", "lines": lines}
    hold.status = GrabOrderHold.RESTORED
    hold.deducted = {}
    hold.closed_at, hold.close_reason = now, (reason or "restored")[:64]
    hold.changed_at = now
    fields = ["status", "deducted", "closed_at", "close_reason", "changed_at"]
    if grab_state:
        hold.grab_state = grab_state
        fields.append("grab_state")
    hold.save(update_fields=fields)
    return {"status": "restored", "lines": lines}


# ── correct ─────────────────────────────────────────────────────────────────
def correct_order(branch, order_id, display_id, items, *, grab_state="COMPLETED",
                  dry_run=False) -> dict:
    """An edit after packing: compare the final items with what was deducted
    and write only the difference, then record the new total.  A repeat finds no
    difference.  ``status`` is ``corrected``, ``no_change`` or ``not_converted``.

    Only mapped lines count: an order whose final items map to nothing is left
    as it is rather than read as "everything was removed".  A unit added by the
    edit is not deducted if staff counted the product after the order was packed
    (their count left the bag out).  A unit taken off always goes back.
    """
    dry_run = dry_run or not writes_enabled()
    with transaction.atomic():
        hold = _lock_hold(branch, order_id)
        if hold is None or hold.status != GrabOrderHold.CONVERTED:
            return {"status": "not_converted", "lines": []}
        now = djtz.now()
        barcodes = {i["barcode"] for i in items if i["barcode"]}
        deducted = {pid: qty for pid, qty in (hold.deducted or {}).items() if qty}
        # One lock pass over every product either side mentions.
        wanted_ids = set(deducted)
        by_barcode = {p.barcode: p for p in Product.objects.filter(
            branch=branch, active=True, barcode__in=list(barcodes))}
        wanted_ids |= {str(p.id) for p in by_barcode.values()}
        locked = {
            str(p.id): p for p in Product.objects.select_for_update()
            .filter(branch=branch, pk__in=list(wanted_ids)).order_by("id")
        }
        mapped, skipped = _map_items(
            items, {bc: locked[str(p.id)] for bc, p in by_barcode.items() if str(p.id) in locked})
        final: dict[str, int] = {}
        for product, item in mapped:
            final[str(product.id)] = final.get(str(product.id), 0) + item["qty"]

        lines = [_line(None, 0, None, s["reason"], s["barcode"]) for s in skipped]
        if hold.close_reason == "before_go_live" or not final:
            if not dry_run and grab_state and hold.grab_state != grab_state:
                hold.grab_state = grab_state
                hold.save(update_fields=["grab_state"])
            return {"status": "no_change", "lines": lines}

        counted = _counted_after(branch, [pid for pid in final], hold.ready_at)
        display_id = display_id or hold.display_id
        note = f"Grab {display_id} edit".strip()
        new_deducted = dict(deducted)
        changed = False
        for pid in sorted(set(final) | set(deducted)):
            delta = final.get(pid, 0) - deducted.get(pid, 0)
            product = locked.get(pid)
            if delta == 0 or product is None:
                continue
            if delta > 0 and pid in counted:
                lines.append(_line(product, delta, product.stock, "counted_after_packing"))
                continue
            stock_after = (product.stock or 0) - delta
            if not dry_run:
                stock_after = _write_stock(
                    branch, product, -delta, type_="out" if delta > 0 else "return",
                    order_id=hold.order_id, note=note)
            lines.append(_line(product, abs(delta), stock_after))
            new_deducted[pid] = final.get(pid, 0)
            changed = True
        if dry_run:
            return {"status": "dry_run" if changed else "no_change", "lines": lines}

        fields = []
        if changed:
            hold.deducted = {pid: q for pid, q in new_deducted.items() if q}
            hold.items = _hold_items(mapped)
            hold.changed_at = now
            fields += ["deducted", "items", "changed_at"]
        if grab_state and hold.grab_state != grab_state:
            hold.grab_state = grab_state
            fields.append("grab_state")
        if fields:
            hold.save(update_fields=fields)
        if skipped:
            open_review(hold, "unmapped_item", _skipped_detail(skipped))
        return {"status": "corrected" if changed else "no_change", "lines": lines}


# ── review ──────────────────────────────────────────────────────────────────
def review_order(branch, order, *, dry_run=False) -> dict:
    """Open a review on an order for staff to settle.  The order row is created
    when the poller has not held it yet, so the review has somewhere to live.
    Returns ``{"review_status": "open"}``, or ``"done"`` when staff already
    settled a review for this same reason."""
    dry_run = dry_run or not writes_enabled()
    now = djtz.now()
    with transaction.atomic():
        if dry_run:
            hold = _lock_hold(branch, order["order_id"])
            settled = (hold is not None and hold.review_status == "done"
                       and hold.review_reason == order["reason"])
            return {"review_status": "done" if settled else "open"}
        hold, _created = _lock_or_create(
            branch, order["order_id"], display_id=order["display_id"],
            first_seen_at=now, last_seen_at=now, changed_at=now,
        )
        open_review(hold, order["reason"], order["detail"])
        return {"review_status": hold.review_status}


def settle_review(hold_pk, action: str, by: str) -> dict:
    """What a person decides on the backoffice's Grab review page.

    ``deduct`` takes a held order's units out of stock (a converted one is left
    as it is); ``restore`` releases a held order or puts a converted one back;
    ``ignore`` closes the review with no stock change — a held order is
    released without deducting, a converted one keeps its deduction.  Not gated
    by ``GRAB_SYNC_WRITES``: a person pressed the button.  Returns
    ``{"status": ...}``; raises ``ReviewNotOpen`` if there is nothing to settle.
    """
    if action not in ("deduct", "restore", "ignore"):
        raise ValueError(action)
    now = djtz.now()
    with transaction.atomic():
        hold = (GrabOrderHold.objects.select_for_update().select_related("branch")
                .filter(pk=hold_pk).first())
        if hold is None or hold.review_status != "open":
            raise ReviewNotOpen(hold_pk)
        branch = hold.branch
        result = {"status": hold.status}
        if hold.status == GrabOrderHold.HELD:
            if action == "deduct":
                result = _convert_locked(
                    branch, hold, hold.display_id, None, grab_state=hold.grab_state,
                    now=now)
                if result["status"] == "converted":
                    hold.close_reason = "staff:deduct"
                    hold.save(update_fields=["close_reason"])
                elif result["status"] == "nothing_to_convert":
                    # Nothing on it maps to a product here, so there is nothing to
                    # hold or deduct; close it rather than leave it held forever.
                    _release_locked(hold, "staff:nothing_to_deduct", now)
            else:
                _release_locked(hold, f"staff:{action}", now)
                result = {"status": GrabOrderHold.RELEASED}
        elif hold.status == GrabOrderHold.CONVERTED and action == "restore":
            result = _restore_locked(branch, hold, hold.display_id, "staff:restore", now=now)
        hold.review_status = "done"
        hold.resolved_by, hold.resolved_at, hold.resolution = by[:120], now, action
        hold.save(update_fields=["review_status", "resolved_by", "resolved_at", "resolution"])
        return result


class ReviewNotOpen(Exception):
    """The review was already settled (or never existed)."""


# ── heartbeat ───────────────────────────────────────────────────────────────
def heartbeat(branch, beat, *, dry_run=False) -> dict:
    """The poller's cycle finished.  A good cycle (``cycle_ok``) is what ends an
    outage: it stamps ``last_ok_at``, clears the problem and records what Grab
    shows.  A bad one never touches ``last_ok_at`` or ``grab_shown`` (the poller
    may resend the last good figures; they are ignored), and only a named
    problem is recorded."""
    dry_run = dry_run or not writes_enabled()
    status = stock_pool.sync_status(branch)
    if dry_run:
        return {"outage": stock_pool.in_outage(status)}
    now = djtz.now()
    with transaction.atomic():
        status, _ = GrabSyncStatus.objects.select_for_update().get_or_create(branch=branch)
        status.push_live = beat["push_live"]
        if beat["cycle_ok"]:
            status.last_ok_at = now
            status.problem = ""
            status.grab_shown = beat["grab_shown"]
        elif beat["problem"]:
            status.problem = beat["problem"]
        # A cycle that did not finish and names no problem (one incomplete read)
        # changes nothing: ``last_ok_at`` and the last good ``grab_shown`` stand,
        # and five quiet minutes make the outage on their own.
        status.save()
    return {"outage": stock_pool.in_outage(status)}


# ── stock read ──────────────────────────────────────────────────────────────
def stock_for_branch(branch) -> dict:
    """Every active, barcoded product at the branch with the numbers the poller
    needs (``grab_target`` above all), the open Grab orders, and when each
    product's target last moved."""
    products = list(
        Product.objects.filter(branch=branch, active=True)
        .exclude(barcode="").order_by("barcode")
    )
    avail = stock_pool.availability(branch, products)
    rows = []
    for p in products:
        a = avail[str(p.id)]
        rows.append({
            "barcode": p.barcode, "product_id": str(p.id), "name": p.name,
            "stock": a["stock"], "counter_stock": a["counter_stock"],
            "self_held": a["self_held"], "grab_held": a["grab_held"],
            "grab_target": a["grab_target"],
        })

    open_orders, changed = [], {}
    if branch.grab_merchant_id:
        recent = djtz.now() - timedelta(hours=OPEN_ORDER_HOURS)
        holds = GrabOrderHold.objects.filter(branch=branch).filter(
            _open_filter(recent)).order_by("first_seen_at")
        open_orders = [{
            "order_id": h.order_id, "status": h.status, "grab_state": h.grab_state,
            "review_status": h.review_status,
        } for h in holds]
        changed = {bc: iso_z(when) for bc, when in
                   stock_pool.target_changed_at(branch, products).items()}
    return {
        "branch": branch.name,
        "outage": stock_pool.in_outage(stock_pool.sync_status(branch)),
        "products": rows,
        "open_orders": open_orders,
        "target_changed_at": changed,
    }


def _open_filter(recent):
    """Held rows, plus converted rows from the last day that Grab has not marked
    ``COMPLETED`` yet — the poller watches those to the end.  An order closed as
    ``before_go_live`` was never ours to watch."""
    return (
        Q(status=GrabOrderHold.HELD)
        | (Q(status=GrabOrderHold.CONVERTED, converted_at__gte=recent)
           & ~Q(grab_state="COMPLETED") & ~Q(close_reason="before_go_live"))
    )


# ── till gate ───────────────────────────────────────────────────────────────
def till_took_held(branch, order_number: str, shortfalls: list[dict]):
    """A sale was saved that sold units held for Grab: the hold landed between
    the till's check and its save, and the customer has already paid, so the
    sale stands.  Open a review on the newest Grab order holding each product
    (and alert), so someone checks the bag.

    Called under the branch lock from ``create_order_from_items``.  A failure
    here must never lose the sale, so it never raises.
    """
    try:
        with transaction.atomic():
            holds = list(
                GrabOrderHold.objects.select_for_update().select_related("branch")
                .filter(branch=branch, status=GrabOrderHold.HELD).order_by("-first_seen_at")
            )
            for short in shortfalls:
                target = next(
                    (h for h in holds
                     if any(str(i.get("product_id")) == short["product_id"] for i in h.items or [])),
                    None)
                if target is None:
                    logger.warning(
                        "Till sale %s took %s units of %s that Grab may still sell "
                        "(no Grab order holds them).", order_number, short["wanted"],
                        short["product_id"])
                    continue
                name = next((i.get("name") for i in target.items
                             if str(i.get("product_id")) == short["product_id"]), "") or short["product_id"]
                open_review(target, "till_took_held", {
                    "order_number": order_number, "product": name,
                    "wanted": short["wanted"], "can_sell": short["can_sell"],
                    "held_for_grab": short["held_for_grab"],
                })
    except Exception:  # noqa: BLE001 — see docstring
        logger.exception("Could not open a Grab review for till sale %s", order_number)
