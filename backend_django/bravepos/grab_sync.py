"""Grab ↔ BravePOS stock sync: the BravePOS half.

An outside poller (shopify_django_app) watches a branch's Grab outlet and calls
three endpoints here: ``apply`` takes a Grab order's items out of stock,
``restore`` puts them back when the order is cancelled, and ``stock`` reads the
branch's stock so the poller can work out what Grab should show.

BravePOS is the source of truth, and the ledger for "was this Grab order taken
out of stock" is the ``StockMovement`` table itself: one 'out' movement per line
with ``document_no`` set to the Grab order ID (27 characters; the short display
ID like ``GF-921`` repeats, so it goes in the note instead).  No new table.  A
Grab order ID can never equal an ``Order.order_number``, so the "inventory as
of" report does not mistake these movements for till sales — it undoes them
through the ``AuditLog`` rows ``Product.save()`` writes, which is why stock here
changes through ``save(update_fields=["stock"])`` and not a queryset ``update()``.

Locking: product rows only, in primary-key order, so two calls for the same
order take the same locks one after the other and two different orders cannot
deadlock.  The ``Branch`` row is not locked — till checkout locks it, and a Grab
call has no business making a cashier wait.

Safety switches, both from the environment:

* ``GRAB_SYNC_API_KEY`` — the key the poller sends as ``X-API-Key``.  Unset
  means the endpoints are not configured and answer 503.
* ``GRAB_SYNC_WRITES`` — must be ``1`` for stock to change.  Without it, or with
  ``"dry_run": true`` in the body, every lookup is done and the would-be result
  returned, and nothing is written.

A branch with no ``grab_merchant_id`` is not synced: it cannot be found here.
"""
from __future__ import annotations

import hmac
import os

from django.db import transaction
from rest_framework.response import Response

from . import audit
from .models import Branch, Product, StockMovement

API_KEY_ENV = "GRAB_SYNC_API_KEY"
WRITES_ENV = "GRAB_SYNC_WRITES"

AUDIT_SOURCE = "grab-sync"
CREATED_BY = "Grab sync"

# ``StockMovement.document_no`` is 64 characters.
ORDER_ID_MAX = 64


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


def parse_merchant_id(value) -> str:
    # Never blank: a blank merchant ID would match every branch that is not synced.
    return _text(value, "merchant_id", max_length=32).upper()


def parse_order(data: dict) -> dict:
    """The fields apply and restore share, validated."""
    if not isinstance(data, dict):
        raise BadRequest("Body must be a JSON object.")
    return {
        "merchant_id": parse_merchant_id(data.get("merchant_id")),
        "order_id": _text(data.get("order_id"), "order_id", max_length=ORDER_ID_MAX),
        "display_id": _text(data.get("display_id"), "display_id",
                            required=False, max_length=32),
        # Why a restore happened (Grab's status); apply ignores it.
        "reason": _text(data.get("reason"), "reason", required=False, max_length=64),
        "dry_run": data.get("dry_run") is True,
    }


def parse_items(raw) -> list[dict]:
    """``[{barcode, qty, grab_item_id, name}]`` with duplicate barcodes merged
    (quantities added up).  A line with no barcode is kept, so it can be
    reported as skipped rather than vanish."""
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
        if barcode and barcode in by_barcode:
            by_barcode[barcode]["qty"] += qty
            continue
        line = {"barcode": barcode, "qty": qty, "grab_item_id": grab_item_id}
        lines.append(line)
        if barcode:
            by_barcode[barcode] = line
    return lines


def find_branch(merchant_id: str):
    """The active branch this Grab outlet belongs to, or None."""
    return Branch.objects.filter(grab_merchant_id=merchant_id, active=True).first()


# ── Services ────────────────────────────────────────────────────────────────
def _line(product, qty, stock_after):
    return {
        "barcode": product.barcode, "product_id": str(product.id),
        "qty": qty, "stock_after": stock_after,
    }


def _ledger(branch, order_id):
    """The Grab order's 'out' and 'return' movements at this branch, oldest
    first.  Must be read while holding the product locks."""
    return list(
        StockMovement.objects
        .filter(branch=branch, document_no=order_id, type__in=("out", "return"))
        .select_related("product").order_by("created_at", "id")
    )


def _movement_lines(movements):
    return [_line(m.product, m.qty, m.product.stock) for m in movements]


def apply_order(branch, order_id, display_id, items, *, dry_run=False) -> dict:
    """Take a Grab order's items out of the branch's stock, once.

    Returns ``{"status", "branch", "lines", "skipped"}``; ``status`` is
    ``applied``, ``already_applied``, ``already_restored``, ``nothing_to_apply``
    or ``dry_run``.  ``dry_run`` is reported only when a real call would have
    written; an order the ledger already holds reports what it holds, so the
    caller can tell "would deduct" from "already deducted".
    """
    dry_run = dry_run or not writes_enabled()
    wanted = {i["barcode"] for i in items if i["barcode"]}
    result = {"status": "", "branch": branch.name, "lines": [], "skipped": []}

    with transaction.atomic():
        # Fixed lock order — see the module docstring.
        products = {
            p.barcode: p
            for p in Product.objects.select_for_update()
            .filter(branch=branch, active=True, barcode__in=wanted)
            .order_by("id")
        }
        ledger = _ledger(branch, order_id)
        if any(m.type == "return" for m in ledger):
            result["status"] = "already_restored"
            result["lines"] = _movement_lines([m for m in ledger if m.type == "out"])
            return result
        if any(m.type == "out" for m in ledger):
            result["status"] = "already_applied"
            result["lines"] = _movement_lines(ledger)
            return result

        to_take = []
        for item in items:
            product = products.get(item["barcode"]) if item["barcode"] else None
            if product is None:
                result["skipped"].append({
                    "barcode": item["barcode"], "grab_item_id": item["grab_item_id"],
                    "reason": "not_at_branch" if item["barcode"] else "no_barcode",
                })
            else:
                to_take.append((product, item["qty"]))

        if not to_take:
            result["status"] = "nothing_to_apply"
            return result

        note = f"Grab {display_id}".strip()
        for product, qty in to_take:
            stock_after = product.stock - qty      # negative is allowed, as at the till
            if not dry_run:
                product.stock = stock_after
                # save(), not update(): this is what writes the AuditLog row
                # "Inventory as of" works back from.
                product.save(update_fields=["stock"])
                StockMovement.objects.create(
                    branch=branch, product=product, product_name=product.name,
                    type="out", qty=qty, note=note,
                    document_no=order_id, created_by=CREATED_BY,
                )
            result["lines"].append(_line(product, qty, stock_after))
        result["status"] = "dry_run" if dry_run else "applied"
    return result


def restore_order(branch, order_id, display_id, reason="", *, dry_run=False) -> dict:
    """Put a cancelled Grab order's stock back, once.

    Returns ``{"status", "branch", "lines"}``; ``status`` is ``restored``,
    ``already_restored``, ``not_applied`` or ``dry_run``.  One way: an order
    that was restored is never taken out again by ``apply_order``.
    """
    dry_run = dry_run or not writes_enabled()
    result = {"status": "", "branch": branch.name, "lines": []}

    # The 'out' rows say which products to lock.  Read them unlocked, lock, then
    # read again: only the second read is safe to act on.
    first_look = [m for m in _ledger(branch, order_id) if m.type == "out"]
    with transaction.atomic():
        locked = {
            p.id: p
            for p in Product.objects.select_for_update()
            .filter(branch=branch, pk__in={m.product_id for m in first_look})
            .order_by("id")
        }
        ledger = _ledger(branch, order_id)
        outs = [m for m in ledger if m.type == "out"]
        if any(m.type == "return" for m in ledger):
            result["status"] = "already_restored"
            result["lines"] = _movement_lines(outs)
            return result
        if not outs:
            result["status"] = "not_applied"
            return result

        note = f"Grab {display_id} cancelled".strip()
        if reason:
            note += f" ({reason})"
        for movement in outs:
            product = locked.get(movement.product_id)
            if product is None:
                # The movement is there but the row is not (a product moved
                # off this branch).  Nothing to put back onto.
                continue
            stock_after = product.stock + movement.qty
            if not dry_run:
                product.stock = stock_after
                product.save(update_fields=["stock"])
                StockMovement.objects.create(
                    branch=branch, product=product, product_name=product.name,
                    type="return", qty=movement.qty, note=note,
                    document_no=order_id, created_by=CREATED_BY,
                )
            result["lines"].append(_line(product, movement.qty, stock_after))
        result["status"] = "dry_run" if dry_run else "restored"
    return result


def stock_for_branch(branch) -> dict:
    """Every active, barcoded product at the branch — all the poller needs to
    work out what Grab should show."""
    rows = (
        Product.objects.filter(branch=branch, active=True)
        .exclude(barcode="").order_by("barcode")
        .values("barcode", "stock", "name")
    )
    return {"branch": branch.name, "products": list(rows)}
