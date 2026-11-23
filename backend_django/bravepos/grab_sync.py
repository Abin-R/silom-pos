"""Grab ↔ BravePOS stock sync: the BravePOS half.

An outside poller (shopify_django_app) watches a branch's Grab outlet and tells
BravePOS about its orders.  BravePOS keeps every open Grab order as a **hold**
(``GrabOrderHold``): Grab lowers its own count when an order is placed, so from
that moment the units are spoken for even though they are still on the shelf.

* ``hold`` — a Grab order has been seen: hold its units (batch upsert).
* ``convert`` — the order is packed (Ready or picked up): the hold becomes a
  deduction, as 'out' ``StockMovement`` rows, in one transaction.
* ``release`` — it was cancelled before that: drop the hold, no stock moves.
* ``restore`` — it was cancelled after: stage a staff-confirmed return review.
* ``correct`` — the order was edited: apply only the difference.
* ``review`` — the poller cannot tell: open a review for staff.
* ``heartbeat`` — the poller's cycle finished; see ``stock_pool.outage_shown``.
* ``stock`` — what the poller needs to work out what Grab should show.

BravePOS is the source of truth.  What each channel may sell is worked out in
``stock_pool``; this module only keeps the holds and the deductions.

The deduction stays in ``StockMovement``: 'out' rows with ``document_no`` set to
the Grab order ID (27 characters; the short display ID like ``GF-921`` repeats,
so it goes in the note instead), 'return' rows only after staff confirms a
staged cancellation or correction return.  A Grab order ID can never equal an ``Order.order_number``,
so the "inventory as of" report does not mistake these movements for till sales
— it undoes them through the ``AuditLog`` rows ``Product.save()`` writes, which
is why stock here changes through ``save(update_fields=["stock"])`` and not a
queryset ``update()``.

Locking: the hold row first (``select_for_update``), then the products in
primary-key order, so two calls for the same order take the same locks one
after the other and two different orders cannot deadlock.  The ``Branch`` row is
never locked (``of=("self",)`` keeps a ``select_related`` branch out of it) —
till checkout locks it, and a Grab call has no business making a cashier wait.
Till checkout takes that lock as FOR NO KEY UPDATE, so the share lock a stock
movement's foreign key takes on the branch does not wait for it either; and it
locks its products in id order too, so a sale and a convert cannot wait on each
other in a circle.

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
import hashlib
import json
import os
from datetime import timedelta, timezone as dt_timezone

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone as djtz
from django.utils.dateparse import parse_datetime
from rest_framework.response import Response

from . import audit, seatalk, stock_pool
from .models import Branch, GrabOrderHold, GrabSyncStatus, Product, SelfOrder, StockMovement

logger = logging.getLogger("bravepos")

API_KEY_ENV = "GRAB_SYNC_API_KEY"
WRITES_ENV = "GRAB_SYNC_WRITES"

AUDIT_SOURCE = "grab-sync"
CREATED_BY = "Grab sync"

# ``StockMovement.document_no`` is 64 characters.
ORDER_ID_MAX = 64
NOTHING_TO_CONVERT = "nothing_to_convert"   # close_reason of an empty row closed at once
DISPLAY_ID_MAX = 64              # ``GrabOrderHold.display_id`` is 64 too
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
    "restore_required": "Cancellation after stock was deducted needs staff confirmation before return",
    "correction_return": "An order correction proposes a partial stock return that needs staff confirmation",
    "stale": "Open for hours and nothing has settled it",
    "till_took_held": "The till sold units that were held for it, so the bag may be short",
}
SELF_ORDER_TOOK_HELD_TEXT = (
    "A self-order sale took units that were held for it, so the bag may be short")


def review_text(reason: str, detail: dict | None = None) -> str:
    """The sentence that says why an order needs a look.  A ``till_took_held``
    review names the channel that took the units: the till or a self-order."""
    if reason == "till_took_held" and (detail or {}).get("source") == "self_order":
        return SELF_ORDER_TOOK_HELD_TEXT
    return REVIEW_TEXT.get(reason, reason)


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
                            required=False, max_length=DISPLAY_ID_MAX),
        # Why a release/restore happened (Grab's status); convert ignores it.
        "reason": _text(data.get("reason"), "reason", required=False, max_length=64),
        # The Grab page or state the order was last seen in.
        "grab_state": _text(data.get("grab_state"), "grab_state",
                            required=False, max_length=32),
        "ready_at": _when(data.get("ready_at"), "ready_at"),
        # The poller's GRAB_STOCK_SINCE: a Ready time before it is an order
        # packed before go-live, whose stock the count at go-live already knows.
        "go_live_at": _when(data.get("since"), "since"),
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


def _parse_hold_order(o) -> dict:
    """One order of the hold batch, validated; ``BadRequest`` if it is not usable."""
    if not isinstance(o, dict):
        raise BadRequest("Each order must be an object.")
    return {
        "order_id": _text(o.get("order_id"), "order_id", max_length=ORDER_ID_MAX),
        "display_id": _text(o.get("display_id"), "display_id",
                            required=False, max_length=DISPLAY_ID_MAX),
        "placed_at": _when(o.get("placed_at"), "placed_at"),
        "grab_state": _text(o.get("grab_state"), "grab_state",
                            required=False, max_length=32),
        "items": parse_items(o.get("items") if o.get("items") is not None else []),
    }


def parse_hold_orders(data: dict) -> dict:
    """``{merchant_id, orders, invalid, dry_run}`` for the batch hold call.

    Only a problem with the call as a whole (body, merchant, ``orders``) is a
    ``BadRequest``.  An order that cannot be read goes to ``invalid`` —
    ``{index, order_id, detail}`` — and the rest of the batch still holds: one
    bad order must not stall every other order at the outlet.
    """
    if not isinstance(data, dict):
        raise BadRequest("Body must be a JSON object.")
    merchant_id = parse_merchant_id(data.get("merchant_id"))
    raw = data.get("orders")
    if not isinstance(raw, list):
        raise BadRequest("orders must be a list.")
    if len(raw) > MAX_ORDERS_PER_HOLD_CALL:
        raise BadRequest("Too many orders in one call.")
    orders, invalid, seen = [], [], set()
    for index, o in enumerate(raw):
        try:
            order = _parse_hold_order(o)
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
    never from inside a lock — and on a background thread, so the request (a
    till checkout among them) never waits on SeaTalk.  Quietly nothing when no
    group is configured."""
    group = seatalk.grab_group_id()
    if not group:
        return
    transaction.on_commit(lambda: seatalk.send_group_text_async(group, text))


def _order_name(hold) -> str:
    return hold.display_id or hold.order_id


def _pending_signature(detail: dict) -> str:
    pending = detail.get("pending_return") if isinstance(detail, dict) else None
    return pending.get("signature", "") if isinstance(pending, dict) else ""


def _return_signature(kind, deducted_before, return_quantities, final_items, evidence) -> str:
    """Stable, non-sensitive identity for one staff-return proposal."""
    payload = {
        "kind": kind,
        "deducted_before": dict(sorted(deducted_before.items())),
        "return_quantities": dict(sorted(return_quantities.items())),
        "final_items": final_items,
        "evidence": evidence,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _pending_return(kind, deducted_before, return_quantities, final_items, *, source, evidence):
    deducted_before = {str(k): int(v) for k, v in deducted_before.items() if int(v) > 0}
    return_quantities = {str(k): int(v) for k, v in return_quantities.items() if int(v) > 0}
    final_items = sorted(
        [{"product_id": str(i.get("product_id", "")), "qty": int(i.get("qty") or 0)}
         for i in final_items if int(i.get("qty") or 0) > 0],
        key=lambda line: line["product_id"],
    )
    evidence = {str(k): v for k, v in sorted((evidence or {}).items())}
    return {
        "kind": kind, "deducted_before": deducted_before,
        "return_quantities": return_quantities, "final_items": final_items,
        "source": source, "evidence": evidence,
        "signature": _return_signature(
            kind, deducted_before, return_quantities, final_items, evidence),
    }


def _valid_pending_return(pending) -> bool:
    """Never accept client-shaped or stale JSON as authority to move stock."""
    if not isinstance(pending, dict):
        return False
    required = ("kind", "deducted_before", "return_quantities", "final_items", "evidence", "signature")
    if any(key not in pending for key in required):
        return False
    try:
        expected = _return_signature(pending["kind"], pending["deducted_before"],
                                     pending["return_quantities"], pending["final_items"],
                                     pending["evidence"])
    except (AttributeError, TypeError, ValueError):
        return False
    return hmac.compare_digest(str(pending["signature"]), expected)


def _clear_pending_return(hold):
    """Invalidate an older correction before evaluating any newer evidence."""
    detail = dict(hold.review_detail or {})
    if "pending_return" not in detail:
        return
    detail.pop("pending_return", None)
    detail["proposal_version"] = int(detail.get("proposal_version") or 0) + 1
    hold.review_detail = detail
    hold.save(update_fields=["review_detail"])


def open_review(hold, reason: str, detail: dict | None = None, *, trusted_pending=False) -> bool:
    """Open/refresh review evidence without discarding a signed return proposal."""
    incoming = dict(detail or {})
    old = dict(hold.review_detail or {})
    old_pending = old.get("pending_return")
    supplied_pending = incoming.pop("pending_return", None)
    invalidates = bool(incoming.pop("invalidate_pending_return", False))
    if trusted_pending and supplied_pending is not None and _valid_pending_return(supplied_pending):
        pending = supplied_pending
    elif invalidates:
        pending = None
    else:
        pending = old_pending if _valid_pending_return(old_pending) else None
    proposal_changed = _pending_signature({"pending_return": pending}) != _pending_signature(old)
    version = int(old.get("proposal_version") or 0) + (1 if proposal_changed else 0)
    detail = {**old, **incoming, "_alerted": list(old.get("_alerted") or [])}
    if pending is None:
        detail.pop("pending_return", None)
    else:
        detail["pending_return"] = pending
    detail["proposal_version"] = version
    proposed_signature = _pending_signature(detail)
    settled_signature = old.get("settled_signature", "")
    if hold.review_status == "done" and hold.review_reason == reason:
        if not proposed_signature or hmac.compare_digest(proposed_signature, settled_signature):
            if detail != old:
                hold.review_detail = detail
                hold.save(update_fields=["review_detail"])
            return False
    if hold.review_status == "open" and hold.review_reason and hold.review_reason != reason:
        detail["earlier_reason"] = hold.review_reason
    alerted = detail["_alerted"]
    first_time = reason not in alerted
    if first_time:
        alerted.append(reason)
    hold.review_status, hold.review_reason, hold.review_detail = "open", reason, detail
    hold.resolved_by, hold.resolved_at, hold.resolution = "", None, ""
    hold.save(update_fields=["review_status", "review_reason", "review_detail",
                            "resolved_by", "resolved_at", "resolution"])
    if first_time:
        alert(f"Grab order {_order_name(hold)} at {hold.branch.name} needs a look. "
              f"{review_text(reason, detail)}. Open Backoffice > Grab review to deduct, restore or ignore it.")
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
    # ``of=("self",)``: lock the hold row only.  A bare ``select_for_update()`` on a
    # ``select_related`` query would lock the Branch row too, which the till's
    # checkout holds for the whole sale.
    return (GrabOrderHold.objects.select_for_update(of=("self",))
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


def _write_stock(branch, product, delta, *, type_, order_id, note, created_by=CREATED_BY):
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
        document_no=order_id, created_by=created_by,
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
    return {"results": [_hold_one(branch, order, dry_run) for order in orders]}


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
    if hold.status == GrabOrderHold.RELEASED and hold.close_reason != NOTHING_TO_CONVERT:
        return {"status": "released_earlier", "lines": []}
    # (A row closed as ``nothing_to_convert`` was never a real hold: once its
    # products exist, the same order can still be converted.)

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
    to_take = [(p, i) for p, i in mapped if str(p.id) not in counted]
    lines = [_line(p, i["qty"], p.stock, "counted_after_packing")
             for p, i in mapped if str(p.id) in counted]
    lines += [_line(None, 0, None, s["reason"], s["barcode"]) for s in skipped]

    if not mapped:
        if not dry_run and not hold._state.adding:
            if skipped:
                open_review(hold, "unmapped_item", _skipped_detail(skipped))
            if hold.status == GrabOrderHold.HELD and not hold.items:
                # Nothing here holds any unit (made by this very call, or empty from
                # the start): do not leave an empty ``held`` row for the poller to
                # watch forever.  A review, if one opened, stays on the closed row.
                _release_locked(hold, NOTHING_TO_CONVERT, now)
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
    elif hold.close_reason == NOTHING_TO_CONVERT:
        hold.close_reason, hold.closed_at = "", None
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
    """Machine cancellation evidence can only stage a staff-confirmed return."""
    dry_run = dry_run or not writes_enabled()
    with transaction.atomic():
        hold = _lock_hold(branch, order_id)
        if hold is None or hold.status in (GrabOrderHold.HELD, GrabOrderHold.RELEASED):
            return {"status": "not_converted", "lines": []}
        if hold.status == GrabOrderHold.RESTORED:
            return {"status": "already_restored", "lines": []}
        deducted = {pid: int(qty) for pid, qty in (hold.deducted or {}).items() if int(qty) > 0}
        pending = _pending_return(
            "cancellation", deducted, deducted, [], source="api",
            evidence={"state": grab_state or "CANCELLED", "reason": reason or ""},
        )
        if dry_run:
            return {"status": "review_required", "review_status": "open", "lines": []}
        if grab_state and hold.grab_state != grab_state:
            hold.grab_state = grab_state
            hold.save(update_fields=["grab_state"])
        open_review(hold, "restore_required", {"pending_return": pending}, trusted_pending=True)
        return {"status": "review_required", "review_status": "open", "lines": []}


class StaleReturnProposal(Exception):
    """The deduction changed after staff saw the proposed return."""


def _restore_locked(branch, hold, display_id, reason, *, return_quantities=None,
                    deducted_before=None, actor="", now=None):
    """Private staff settlement. The machine endpoint never calls this."""
    now = now or djtz.now()
    if hold is None or hold.status in (GrabOrderHold.HELD, GrabOrderHold.RELEASED):
        return {"status": "not_converted", "lines": []}
    if hold.status == GrabOrderHold.RESTORED:
        return {"status": "already_restored", "lines": []}
    current = {pid: int(qty) for pid, qty in (hold.deducted or {}).items() if int(qty) > 0}
    if deducted_before is not None and current != deducted_before:
        raise StaleReturnProposal(hold.pk)
    quantities = current if return_quantities is None else {
        pid: int(qty) for pid, qty in return_quantities.items() if int(qty) > 0}
    if any(qty > current.get(pid, 0) for pid, qty in quantities.items()):
        raise StaleReturnProposal(hold.pk)
    locked = {str(p.id): p for p in Product.objects.select_for_update()
              .filter(branch=branch, pk__in=list(quantities)).order_by("id")}
    display_id = display_id or hold.display_id
    note = f"Grab {display_id} cancelled".strip()
    if reason:
        note += f" ({reason})"
    lines = []
    remaining = dict(current)
    for pid, qty in sorted(quantities.items()):
        product = locked.get(pid)
        if product is None:
            raise StaleReturnProposal(hold.pk)
        stock_after = _write_stock(
            branch, product, qty, type_="return", order_id=hold.order_id,
            note=note, created_by=actor[:120] or CREATED_BY)
        lines.append(_line(product, qty, stock_after))
        remaining[pid] -= qty
        if not remaining[pid]:
            del remaining[pid]
    hold.deducted = remaining
    hold.changed_at = now
    fields = ["deducted", "changed_at"]
    if not remaining:
        hold.status = GrabOrderHold.RESTORED
        hold.closed_at, hold.close_reason = now, (reason or "restored")[:64]
        fields += ["status", "closed_at", "close_reason"]
    hold.save(update_fields=fields)
    return {"status": hold.status, "lines": lines}


# ── correct ─────────────────────────────────────────────────────────────────
def correct_order(branch, order_id, display_id, items, *, grab_state="COMPLETED",
                  dry_run=False) -> dict:
    """Apply only additional confirmed deductions; stage every possible return."""
    dry_run = dry_run or not writes_enabled()
    with transaction.atomic():
        hold = _lock_hold(branch, order_id)
        if hold is None or hold.status != GrabOrderHold.CONVERTED:
            return {"status": "not_converted", "lines": []}
        now = djtz.now()
        if not dry_run:
            _clear_pending_return(hold)
        deducted = {pid: int(qty) for pid, qty in (hold.deducted or {}).items() if int(qty) > 0}
        barcodes = {item["barcode"] for item in items if item["barcode"]}
        by_barcode = _products_by_barcode(branch, barcodes)
        wanted_ids = set(deducted) | {str(product.id) for product in by_barcode.values()}
        locked = {str(product.id): product for product in Product.objects.select_for_update()
                  .filter(branch=branch, pk__in=list(wanted_ids)).order_by("id")}
        mapped, skipped = _map_items(
            items, {barcode: locked[str(product.id)] for barcode, product in by_barcode.items()
                    if str(product.id) in locked})
        new_items = _hold_items(mapped)
        final = stock_pool.line_quantities(new_items)
        lines = [_line(None, 0, None, skipped_line["reason"], skipped_line["barcode"])
                 for skipped_line in skipped]
        if hold.close_reason == "before_go_live":
            return {"status": "no_change", "lines": lines}
        # An empty, missing, excluded, or unmapped detail is never evidence that
        # every packed unit was removed. Preserve the deduction and ask staff.
        if not final:
            if not dry_run:
                open_review(hold, "unmapped_item", {
                    **_skipped_detail(skipped), "source": "api", "incomplete_items": True,
                })
            return {"status": "review_required" if not dry_run else "dry_run", "lines": lines}

        counted = _counted_after(branch, final, hold.ready_at)
        display_id = display_id or hold.display_id
        note = f"Grab {display_id} edit".strip()
        updated_deducted = dict(deducted)
        changed = False
        for pid in sorted(final):
            delta = final[pid] - deducted.get(pid, 0)
            product = locked.get(pid)
            if delta <= 0 or product is None:
                continue
            if pid in counted:
                lines.append(_line(product, delta, product.stock, "counted_after_packing"))
                continue
            stock_after = (product.stock or 0) - delta
            if not dry_run:
                stock_after = _write_stock(
                    branch, product, -delta, type_="out", order_id=hold.order_id, note=note)
            lines.append(_line(product, delta, stock_after))
            updated_deducted[pid] = updated_deducted.get(pid, 0) + delta
            changed = True

        return_quantities = {
            pid: deducted[pid] - final.get(pid, 0)
            for pid in deducted if deducted[pid] > final.get(pid, 0)
        }
        # A partial return is safe to propose only from complete, mapped detail.
        # Any skipped line could conceal the identity or quantity of a removal.
        pending = None if skipped else _pending_return(
            "correction", updated_deducted, return_quantities, new_items, source="api",
            evidence={"state": grab_state or "COMPLETED"},
        ) if return_quantities else None
        if dry_run:
            return {
                "status": "review_required" if pending else ("dry_run" if changed else "no_change"),
                "lines": lines,
                **({"pending_return": pending["return_quantities"]} if pending else {}),
            }

        fields = []
        if changed:
            hold.deducted = updated_deducted
            hold.items = new_items
            hold.changed_at = now
            fields += ["deducted", "items", "changed_at"]
        if grab_state and hold.grab_state != grab_state:
            hold.grab_state = grab_state
            fields.append("grab_state")
        if fields:
            hold.save(update_fields=fields)
        if pending:
            open_review(hold, "correction_return", {"pending_return": pending}, trusted_pending=True)
            return {"status": "review_required", "lines": lines,
                    "pending_return": pending["return_quantities"]}
        if skipped:
            open_review(hold, "unmapped_item", {**_skipped_detail(skipped), "source": "api"})
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
        # A machine can describe evidence, never proposal authority. Strip all
        # fields that could otherwise impersonate a staff-bound server proposal.
        reserved = {"pending_return", "proposal_version", "settled_signature", "signature",
                    "return_quantities", "deducted_before", "final_items", "invalidate_pending_return"}
        detail = {key: value for key, value in dict(order["detail"]).items() if key not in reserved}
        if order["reason"] == "restore_required" and hold.status == GrabOrderHold.CONVERTED:
            deducted = {pid: int(qty) for pid, qty in (hold.deducted or {}).items() if int(qty) > 0}
            detail["pending_return"] = _pending_return(
                "cancellation", deducted, deducted, [], source="review",
                evidence={key: detail[key] for key in (
                    "proposal_type", "authoritative_cancel", "cancelled_at", "overall_state",
                    "delivery_task_status", "pickup_status", "order_lines") if key in detail},
            )
        open_review(hold, order["reason"], detail, trusted_pending=True)
        return {"review_status": hold.review_status}


def settle_review(hold_pk, action: str, by: str, *, expected_signature="", expected_version="") -> dict:
    """Apply the authenticated staff decision, with an atomic stale check."""
    if action not in ("deduct", "restore", "ignore"):
        raise ValueError(action)
    now = djtz.now()
    with transaction.atomic():
        hold = (GrabOrderHold.objects.select_for_update(of=("self",)).select_related("branch")
                .filter(pk=hold_pk).first())
        if hold is None or hold.review_status != "open":
            raise ReviewNotOpen(hold_pk)
        branch = hold.branch
        result = {"status": hold.status}
        pending = (hold.review_detail or {}).get("pending_return")
        if hold.status == GrabOrderHold.HELD:
            if action == "deduct":
                result = _convert_locked(branch, hold, hold.display_id, None,
                                         grab_state=hold.grab_state, now=now)
                if result["status"] == "converted":
                    hold.close_reason = "staff:deduct"
                    hold.save(update_fields=["close_reason"])
                elif result["status"] == "nothing_to_convert":
                    _release_locked(hold, "staff:nothing_to_deduct", now)
            else:
                _release_locked(hold, f"staff:{action}", now)
                result = {"status": GrabOrderHold.RELEASED}
        elif hold.status == GrabOrderHold.CONVERTED and action == "restore":
            if not (_valid_pending_return(pending) and expected_signature and
                    str(expected_version) == str((hold.review_detail or {}).get("proposal_version", "")) and
                    hmac.compare_digest(expected_signature, _pending_signature({"pending_return": pending}))):
                raise StaleReturnProposal(hold.pk)
            result = _restore_locked(
                branch, hold, hold.display_id, "staff:restore",
                return_quantities=pending["return_quantities"],
                deducted_before=pending["deducted_before"], actor=by, now=now)
        detail = dict(hold.review_detail or {})
        signature = _pending_signature(detail)
        if signature:
            detail["settled_signature"] = signature
            hold.review_detail = detail
        hold.review_status = "done"
        hold.resolved_by, hold.resolved_at, hold.resolution = by[:120], now, action
        fields = ["review_status", "resolved_by", "resolved_at", "resolution"]
        if signature:
            fields.append("review_detail")
        hold.save(update_fields=fields)
        return result


class ReviewNotOpen(Exception):
    """The review was already settled (or never existed)."""


# ── heartbeat ───────────────────────────────────────────────────────────────
def heartbeat(branch, beat, *, dry_run=False) -> dict:
    """The poller's cycle finished.  A good cycle (``cycle_ok``) stamps
    ``last_ok_at`` and records what Grab shows; a bad one never touches either
    (the poller may resend the last good figures; they are ignored).  A named
    ``problem`` is recorded whatever ``cycle_ok`` says — a cycle can read fine
    and still fail to push — and puts the branch in outage; a good cycle that
    names none clears it."""
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
            status.grab_shown = beat["grab_shown"]
        # A named problem is recorded whatever ``cycle_ok`` says — a cycle can read
        # fine and still fail to push — and a good, unnamed cycle clears it.
        if beat["problem"]:
            status.problem = beat["problem"]
        elif beat["cycle_ok"]:
            status.problem = ""
        # A cycle that did not finish and names no problem (one incomplete read)
        # changes nothing: ``last_ok_at`` and the last good ``grab_shown`` stand,
        # and five quiet minutes make the outage on their own.
        status.save()
    return {"outage": stock_pool.in_outage(status)}


# ── stock read ──────────────────────────────────────────────────────────────
def stock_for_branch(branch) -> dict:
    """Return one lock-consistent availability snapshot for the poller.

    A conversion locks its hold then products.  This read takes those same
    locks before materialising either stocks or reductions: a conversion is
    wholly before this snapshot or waits until it has been assembled.  That is
    deliberately stronger than READ COMMITTED's per-query snapshots and avoids
    advertising a transient stock rise to the poller.
    """
    now = djtz.now()
    with transaction.atomic():
        # Same order as conversion: Grab hold rows, then product rows. Locking
        # live self-order rows also prevents a paid/released QR hold from being
        # paired with its pre/post stock value.
        list(GrabOrderHold.objects.select_for_update().filter(branch=branch).order_by("id").only("id"))
        list(SelfOrder.objects.select_for_update().filter(
            branch=branch, status="pending", hold_expires_at__gt=now).order_by("id").only("id"))
        status = GrabSyncStatus.objects.select_for_update().filter(branch=branch).first()
        products = list(Product.objects.select_for_update().filter(
            branch=branch, active=True).exclude(barcode="").order_by("id"))
        products.sort(key=lambda product: product.barcode)
        avail = stock_pool.availability(branch, products)
        rows = [{
            "barcode": p.barcode, "product_id": str(p.id), "name": p.name,
            "stock": avail[str(p.id)]["stock"],
            "counter_stock": avail[str(p.id)]["counter_stock"],
            "self_held": avail[str(p.id)]["self_held"],
            "grab_held": avail[str(p.id)]["grab_held"],
            "grab_target": avail[str(p.id)]["grab_target"],
        } for p in products]
        open_orders, changed = [], {}
        if branch.grab_merchant_id:
            recent = now - timedelta(hours=OPEN_ORDER_HOURS)
            holds = GrabOrderHold.objects.filter(branch=branch).filter(
                _open_filter(recent)).order_by("first_seen_at")
            open_orders = [{
                "order_id": h.order_id, "status": h.status, "grab_state": h.grab_state,
                "review_status": h.review_status,
                "review_reason": (h.review_reason or None) if h.review_status == "open" else None,
            } for h in holds]
            changed = {bc: iso_z(when) for bc, when in
                       stock_pool.target_changed_at(branch, products).items()}
        return {
            "branch": branch.name, "outage": stock_pool.in_outage(status, now),
            "products": rows, "open_orders": open_orders, "target_changed_at": changed,
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
def till_took_held(branch, order_number: str, shortfalls: list[dict], source: str = "counter"):
    """A sale was saved that sold units held for Grab: the hold landed between
    the till's check and its save, and the customer has already paid, so the
    sale stands.  Open a review on the newest Grab order holding each product
    (and alert), so someone checks the bag.

    Called from ``create_order_from_items`` once the sale has committed, so it
    holds no product lock (the Grab paths take the hold row, then products; this
    one takes only hold rows) and a slow alert cannot delay the sale.  A failure
    here must never lose the sale, so it never raises.  ``source`` is the channel
    that sold the units: ``counter`` (the till) or ``self_order``.  A shortfall
    from a real sale carries its ``_hold_id`` from the locked-sale check: review
    that row even if it converted before this callback got its hold lock.
    """
    try:
        with transaction.atomic():
            captured_ids = {short.get('_hold_id') for short in shortfalls if short.get('_hold_id')}
            captured = {
                str(hold.pk): hold for hold in
                GrabOrderHold.objects.select_for_update(of=("self",))
                .select_related("branch")
                .filter(branch=branch, pk__in=captured_ids).order_by("id")
            }
            # Direct callers without a captured id retain the old, newest-held
            # behaviour.  Sales always use their captured identity.
            held = list(
                GrabOrderHold.objects.select_for_update(of=("self",))
                .select_related("branch")
                .filter(branch=branch, status=GrabOrderHold.HELD).order_by("id")
            ) if any(not short.get('_hold_id') for short in shortfalls) else []
            for short in shortfalls:
                target = captured.get(str(short.get('_hold_id')))
                if target is None and not short.get('_hold_id'):
                    target = next(
                        (h for h in held
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
                    "order_number": order_number, "product": name, "source": source,
                    "wanted": short["wanted"], "can_sell": short["can_sell"],
                    "held_for_grab": short["held_for_grab"],
                })
    except Exception:  # noqa: BLE001 — see docstring
        logger.exception("Could not open a Grab review for till sale %s", order_number)
