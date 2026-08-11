"""Peak (peakengineapi.azurewebsites.net) integration.

Ported from Shopster's Django app (``shopify_django_app``).  Two layers:

* :func:`make_peak_request` — low-level HTTP client. Signs every request
  with the rotating Client-Token + HMAC-SHA1 timestamp signature Peak
  expects, audit-logs the round-trip in :class:`PeakRequest`, and
  re-reads :class:`PeakClientToken` on a Peak ``resCode == '600'`` (token
  expired) response.

* :func:`create_peak_receipt_for_order` — high-level orchestration.
  Given a Brave POS :class:`Order` whose ``tax_invoice_data`` has been
  populated by the customer-facing form, this:

  1. Builds the line-items list, lazily creating any missing Peak
     products via ``POST /products`` and caching them in
     :class:`PeakProductMap` so future receipts skip the round-trip.
  2. Creates the customer contact via ``POST /contacts``.
  3. Enqueues the receipt via ``POST /receipts/queue``.
  4. Polls ``GET /receipts/queue?queueId=…`` until the document is
     ready, and returns the resulting ``documentLink``.

Credentials come from three env vars (.env on prod, .env or shell on
dev): ``PEAK_CONNECT_ID``, ``PEAK_USER_TOKEN``, ``PEAK_CLIENT_TOKEN``.
``PEAK_CLIENT_TOKEN`` is only used to seed :class:`PeakClientToken` on a
fresh DB — runtime always reads the token from the DB row so out-of-band
refresh flows can rotate it without restarting the app.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from typing import Any, Optional

import httpx

from django.db import transaction

from .models import (
    ConsolidatedReceipt,
    Order,
    OrderItem,
    PeakClientToken,
    PeakProductMap,
    PeakRequest,
)


PEAK_BASE_URL = "https://peakengineapi.azurewebsites.net/api/v1"


def _get_peak_token() -> str:
    """Return the current Peak Client-Token, resolved in this order:

    1. **Shopster's ``instamator_app_clientpeaktoken`` table** on the
       shared ``instamator3`` Postgres.  Shopster's existing refresh
       flow writes the rotating token here, so reading it directly
       means we stay current automatically — no cron / sync job on our
       side.  Column index ``[1]`` matches Shopster's own
       ``get_peak_token()`` implementation.
    2. **Our own** ``PeakClientToken`` singleton row.  Acts as a manual
       override — useful if Shopster's table is briefly stale or the
       cross-app DB read is down.
    3. **Env var** ``PEAK_TOKEN`` (Shopster's naming) or
       ``PEAK_CLIENT_TOKEN`` (ours).  Last-resort seed for fresh
       installs / dev.

    Any of these returning empty falls through to the next source.
    """
    # 1. Shopster's table on the shared DB.
    try:
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM instamator_app_clientpeaktoken "
                "ORDER BY id DESC LIMIT 1"
            )
            row = cursor.fetchone()
        if row and len(row) > 1 and row[1]:
            return row[1]
    except Exception:
        # Table missing, permission error, etc. — fall through.  We
        # deliberately swallow this so the override paths still work
        # on environments that aren't sharing Shopster's DB.
        pass

    # 2. Our manual override row.
    row_obj = PeakClientToken.objects.filter(pk="default").first()
    if row_obj and row_obj.token:
        return row_obj.token

    # 3. Env-var seed.  Seed the override row on first successful read.
    seed = (
        os.environ.get("PEAK_TOKEN", "")
        or os.environ.get("PEAK_CLIENT_TOKEN", "")
    )
    if seed:
        PeakClientToken.objects.update_or_create(
            pk="default", defaults={"token": seed},
        )
    return seed


def _peak_headers(method: str) -> dict[str, str]:
    """Build the Peak auth headers for one request.  Signature is HMAC-
    SHA1 of the current UTC timestamp keyed by PEAK_CONNECT_ID — matches
    what Shopster's existing integration sends."""
    connect_id = os.environ.get("PEAK_CONNECT_ID", "")
    user_token = os.environ.get("PEAK_USER_TOKEN", "")
    client_token = _get_peak_token()
    timestamp = datetime.now(dt_timezone.utc).strftime("%Y%m%d%H%M%S")
    signature = hmac.new(
        connect_id.encode("utf-8"),
        timestamp.encode("utf-8"),
        hashlib.sha1,
    ).hexdigest()
    return {
        "Client-Token": client_token,
        "User-Token": user_token,
        "Time-Stamp": timestamp,
        "Time-Signature": signature,
        "Content-Type": "application/json",
    }


def make_peak_request(path: str, method: str, body: Optional[dict] = None) -> dict:
    """Send a request to Peak, audit-log it, and return the JSON body.

    On a Peak ``resCode == '600'`` (client token expired) the call is
    retried up to 3 times, re-reading the token from the DB between
    attempts.  Returns the *final* parsed JSON response either way —
    caller is responsible for interpreting Peak's error codes.
    """
    url = PEAK_BASE_URL + path
    method = method.upper()

    def _send(headers: dict[str, str]) -> dict:
        if method == "POST":
            resp = httpx.post(url, headers=headers, json=body, timeout=30)
        else:
            resp = httpx.get(url, headers=headers, params=(body or {}), timeout=30)
        # Peak always answers with JSON; defensive in case of network 5xx.
        try:
            data = resp.json()
        except ValueError:
            data = {"_http_status": resp.status_code, "_text": resp.text}
        PeakRequest.objects.create(
            url=url, method=method, headers=headers, body=body, response=data,
        )
        return data

    headers = _peak_headers(method)
    response = _send(headers)

    # Token-expired retry loop.  We only re-read the token between
    # attempts — refreshing the token itself is out-of-band (admin task).
    attempts = 0
    while response.get("resCode") == "600" and attempts < 3:
        attempts += 1
        time.sleep(1)
        response = _send(_peak_headers(method))

    return response


# ─── Receipt orchestration ────────────────────────────────────────────────

def _v(d: dict, k: str) -> Optional[str]:
    """Tolerate Shopster's list-wrapped form values without forcing
    every caller to unwrap.  Returns ``None`` for empty strings so the
    contact builder can skip optional fields cleanly."""
    x = d.get(k)
    x = x[0] if isinstance(x, list) and x else x
    return x or None


def _build_peak_products(order: Order) -> tuple[list[dict], float]:
    """Return ``(peak_products, total_item_price)``.  Lazily creates a
    Peak product (and a :class:`PeakProductMap` row) for any of our
    Products we haven't sent to Peak before."""
    items = list(OrderItem.objects.filter(order=order).select_related("product"))

    # Look up existing Peak codes for the products on this order.
    product_ids = [it.product_id for it in items if it.product_id]
    existing = {
        m.product_id: m.peak_code
        for m in PeakProductMap.objects.filter(product_id__in=product_ids)
    }

    peak_products: list[dict] = []
    to_create: list[dict] = []
    # Pending: (description-marker, OrderItem) so we can match the Peak
    # response back to the row when caching the mapping.
    pending: list[tuple[str, OrderItem]] = []
    total_item_price = 0.0

    for it in items:
        qty = float(it.qty or 0)
        unit_price = float(it.price or 0)
        if qty <= 0:
            continue
        total_item_price += unit_price * qty

        if it.product_id and it.product_id in existing:
            peak_products.append({
                "productCode": existing[it.product_id],
                "name": it.name,
                "description": it.name,
                "quantity": qty,
                "price": unit_price,
                "discount": 0,
                "vatType": 3,
                "accountCode": "410107",
            })
        else:
            # We need Peak to create this product first.  The Shopster
            # convention is to round-trip our internal identifier through
            # the ``description`` field; Peak echoes it back so we can
            # correlate the response.
            marker = str(it.product_id) if it.product_id else f"item:{it.id}"
            to_create.append({"name": it.name, "description": marker})
            pending.append((marker, it))

    if to_create:
        resp = make_peak_request("/products", "POST", {
            "PeakProducts": {"products": to_create},
        })
        created = (resp.get("PeakProducts") or {}).get("products") or []
        new_maps: list[PeakProductMap] = []
        for pr in created:
            marker = str(pr.get("description") or "")
            match = next((p for p in pending if p[0] == marker), None)
            if not match:
                continue
            _, it = match
            qty = float(it.qty or 0)
            unit_price = float(it.price or 0)
            if it.product_id:
                new_maps.append(PeakProductMap(
                    product_id=it.product_id, peak_code=pr["code"],
                ))
            peak_products.append({
                "productCode": pr["code"],
                "name": it.name,
                "description": it.name,
                "quantity": qty,
                "price": unit_price,
                "discount": 0,
                "vatType": 3,
                "accountCode": "410107",
            })
        if new_maps:
            PeakProductMap.objects.bulk_create(new_maps, ignore_conflicts=True)

    return peak_products, total_item_price


# ─── Consolidation contact ────────────────────────────────────────────────

# A consolidated receipt bills a whole branch-day — hundreds of walk-in bills
# with no single buyer to name — so it is booked to one standing platform
# contact, exactly as the SilomPOS job books every consolidated receipt to its
# fixed PEAK_CONTACT_ID.  This is the Brave POS equivalent: "bravepos" is to
# our consolidation what "silompos" is to theirs.
CONSOLIDATION_CONTACT_NAME = "bravepos"

# Peak's id for that contact, once it exists.  The env var wins so prod and a
# test Peak account can point at different contacts; the constant is the
# fallback to paste the id into if you'd rather pin it in code the way the
# SilomPOS job does.
CONSOLIDATION_CONTACT_ID_ENV = "PEAK_BRAVEPOS_CONTACT_ID"
CONSOLIDATION_CONTACT_ID = ""


def consolidation_contact_id() -> str:
    """The Peak contact id consolidated receipts are booked to, or ``""``
    if the contact hasn't been created yet."""
    return (
        os.environ.get(CONSOLIDATION_CONTACT_ID_ENV, "")
        or CONSOLIDATION_CONTACT_ID
    ).strip()


def create_consolidation_contact(name: str = CONSOLIDATION_CONTACT_NAME) -> str:
    """Create the standing consolidation contact in Peak, return its id.

    Run ONCE per Peak account, deliberately — Peak does not upsert on contact
    name, so a second call silently creates a second "bravepos" contact and
    the branch-days issued after it point at a different customer than the
    ones before.  Callers must therefore gate this behind an explicit flag and
    persist the returned id (``PEAK_BRAVEPOS_CONTACT_ID`` in .env), never call
    it lazily per run.

    ``type: 5`` matches what the per-order tax-invoice contact sends.
    """
    resp = make_peak_request("/contacts", "POST", {
        "PeakContacts": {"contacts": [{"name": name, "type": 5}]},
    })
    try:
        return resp["PeakContacts"]["contacts"][0]["id"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(
            f"Peak /contacts did not return a contact id for {name!r}. "
            f"Response: {resp!r}"
        )


def _build_peak_contact(form_data: dict) -> dict:
    """Translate the customer-facing tax-invoice form into Peak's
    ``contacts`` payload shape.  Optional fields are omitted when blank
    so Peak doesn't reject the contact for empty strings."""
    contact: dict[str, Any] = {"name": _v(form_data, "name"), "type": 5}
    field_map = [
        ("tax_id", "taxNumber"),
        ("registered_address", "address"),
        ("registered_country", "country"),
        ("registered_province", "province"),
        ("registered_city", "subDistrict"),
        ("registered_district", "district"),
        ("registered_postal_code", "postcode"),
    ]
    for src, dst in field_map:
        val = _v(form_data, src)
        if val:
            contact[dst] = val
    return contact


def _create_peak_contact(form_data: dict) -> str:
    """Send the contact to Peak and return its ``id``, retrying up to
    five times for transient response-shape failures.  Raises with
    Peak's actual response on the wire when retries exhaust — without
    that we'd just see "list index out of range" with no clue which
    Peak error code caused it."""
    body = {"PeakContacts": {"contacts": [_build_peak_contact(form_data)]}}
    last_resp: dict = {}
    for _ in range(5):
        last_resp = make_peak_request("/contacts", "POST", body)
        try:
            return last_resp["PeakContacts"]["contacts"][0]["id"]
        except (KeyError, IndexError, TypeError):
            time.sleep(2)
    raise RuntimeError(
        "Peak /contacts did not return a contact id after 5 retries. "
        f"Last response: {last_resp!r}. Last request body: {body!r}"
    )


def _document_link_from_response(peak_response: Optional[dict]) -> Optional[str]:
    """Pull the ``documentLink`` out of a stored Peak queue payload, if any."""
    receipts = (peak_response or {}).get("PeakReceipts", {}).get("receipts") or []
    if receipts and receipts[0].get("documentLink"):
        return receipts[0]["documentLink"]
    return None


def _payment_method(account_code: str, amount: float) -> dict:
    """Per-branch Peak payment method.  A branch left on the default "BSV003"
    is sent as Peak's generic payment-method ``code``; a branch configured
    with a real chart-of-accounts code (e.g. "113105") is sent as an
    ``accountCode`` instead."""
    code = (account_code or "BSV003").strip() or "BSV003"
    if code == "BSV003":
        return {"code": "BSV003", "amount": amount}
    return {"accountCode": code, "amount": amount}


def _enqueue_peak_receipt(order: Order) -> str:
    """Build the receipt payload and POST it to Peak's queue.  Returns the
    ``queueId`` Peak hands back.  Performs no DB writes — the caller persists
    the queue id under a row lock so we never enqueue the same order twice."""
    form = order.tax_invoice_data or {}
    peak_products, total_item_price = _build_peak_products(order)
    contact_id = _create_peak_contact(form)

    amount = float(order.total or 0)
    total_discount = max(total_item_price - amount, 0)
    issued_date = order.created_at.astimezone().strftime("%Y%m%d")

    account_code = getattr(order.branch, "peak_account_code", "")
    payment_method = _payment_method(account_code, amount)

    receipt = {
        "contact": {"id": contact_id},
        "issuedDate": issued_date,
        "discountTotal": float(total_discount),
        "taxStatus": 1,
        "remark": order.order_number,
        "tags": [order.order_number],
        "isTaxInvoice": 1,
        "products": peak_products,
        "paidPayments": {
            "paymentDate": issued_date,
            "payments": [
                {
                    **payment_method,
                    "paymentMethod": payment_method,
                }
            ],
        },
    }

    enqueue_resp = make_peak_request("/receipts/queue", "POST", {
        "PeakReceipts": {"receipts": [receipt]},
    })
    queue_id = enqueue_resp.get("queueId")
    if not queue_id:
        raise RuntimeError(f"Peak did not return a queueId: {enqueue_resp!r}")
    return queue_id


def _check_peak_receipt(order: Order, queue_id: str) -> Optional[str]:
    """One ``GET /receipts/queue`` status check (no sleeping).  Persists the
    final payload on the order and returns its ``documentLink`` if Peak has
    materialised the receipt, or ``None`` if it's still processing."""
    queue_res = make_peak_request("/receipts/queue", "GET", {"queueId": queue_id})
    receipts = (queue_res.get("PeakReceipts") or {}).get("receipts") or []
    if receipts:
        order.peak_response = queue_res
        order.peak_queue_id = queue_id
        order.save(update_fields=["peak_response", "peak_queue_id"])
        return receipts[0].get("documentLink")
    return None


def create_peak_receipt_for_order(order: Order) -> Optional[str]:
    """Advance the Peak receipt for an order by ONE step and return early.

    This is called once per poll from the customer-facing progress page.  It
    deliberately does NOT block on a long polling loop — each call performs at
    most a single Peak enqueue *or* a single status check, so the HTTP request
    always finishes well under the gunicorn worker timeout.  A request that
    blocked for ~50s (the old inline poll) was being killed at the 30s worker
    timeout, returning a non-JSON 502 the progress page surfaced as the
    generic "Could not create tax invoice" error.

    Idempotent: an order maps to at most ONE Peak receipt.  Concurrent polls —
    and re-scans of the receipt QR days later — must never enqueue a second
    receipt.  The enqueue decision is serialised behind a row lock and we skip
    straight to a status check whenever a queue id already exists.

    Returns the Peak ``documentLink`` (HTTPS URL) once ready, or ``None`` while
    the receipt is still processing (caller responds 202 and the page polls
    again).
    """
    # Claim-or-reuse the enqueue under a short row lock.  A second request
    # blocks here, then finds the queue id already set and falls through to a
    # status check without re-enqueuing.
    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)

        # Already finished on a prior attempt — hand back the stored link.
        link = _document_link_from_response(locked.peak_response)
        if link:
            return link

        queue_id = locked.peak_queue_id
        if not queue_id:
            # First poll for this order: enqueue, then return "processing"
            # immediately.  Peak isn't ready in the same instant, and checking
            # now would only lengthen this request — the next poll checks.
            queue_id = _enqueue_peak_receipt(locked)
            locked.peak_queue_id = queue_id
            locked.save(update_fields=["peak_queue_id"])
            return None

    # Subsequent poll: a single quick status check, outside the lock.
    return _check_peak_receipt(order, queue_id)


# ─── Consolidated receipts (one per branch-day) ───────────────────────────
#
# Where the flow above bills ONE order to the customer who asked for a full
# tax invoice, this bills a whole branch-day to the standing "bravepos"
# contact.  Both end up at POST /receipts/queue; the difference is what goes
# in the products list and who the contact is.

# Peak's "Receipt not found" on a void.  There is no live document under this
# code — already voided, or never materialised — so nothing is left billing
# and the reissue may proceed.  Treating it as a failure would strand a
# branch-day forever: every retry would block on re-voiding a document that
# is already gone.
VOID_NOT_FOUND = "384"


class PeakVoidFailed(Exception):
    """The superseded receipt could not be voided, so no new one may issue.

    Issuing anyway leaves two live receipts for one branch-day — the day is
    double-counted in Peak, and since the new document overwrites the stored
    response, the old code is no longer recorded anywhere and nothing detects
    it.  Failing here instead leaves the branch-day billed by its (stale)
    original receipt, which the next run retries.
    """


def void_receipt(code: str) -> Optional[dict]:
    """Void a Peak receipt.  Returns ``None`` when Peak reports no such
    document, the response otherwise, and raises :class:`PeakVoidFailed` on
    anything else."""
    resp = make_peak_request("/receipts/void", "POST", {
        "PeakReceipts": {"code": code},
    })
    peak = resp.get("PeakReceipts") or {}
    res_code = str(peak.get("resCode") or resp.get("resCode") or "")
    if res_code == VOID_NOT_FOUND:
        return None
    if res_code and res_code != "200":
        raise PeakVoidFailed(
            f"void of {code} returned resCode {res_code}: "
            f"{peak.get('resDesc') or resp.get('resDesc')}"
        )
    return resp


def ensure_consolidated_product_codes(rows: list[dict]) -> dict[str, str]:
    """Map every distinct product on a branch-day to a Peak product code.

    ``rows`` are the item rows from the consolidation payload. Known products
    come from :class:`PeakProductMap`; whatever Peak has never seen is created
    in a single POST /products, the same round-trip the per-order path makes.

    Keys are ``str(product_id)``, or ``name:<name>`` for a row whose Product
    was deleted. Those orphan rows have nothing to cache against — the map
    keys on the Product FK — so they cost one Peak product per run. That only
    happens after a product delete, and the cost is a duplicate entry in
    Peak's catalogue, never a duplicated sale.
    """
    ids = {r["product_id"] for r in rows if r["product_id"]}
    codes: dict[str, str] = {
        str(m.product_id): m.peak_code
        for m in PeakProductMap.objects.filter(product_id__in=ids)
    }

    # Deduplicate first: the same product appears once per price it sold at.
    to_create: dict[str, str] = {}
    for row in rows:
        key = row["product_id"] or f"name:{row['name']}"
        if key in codes or key in to_create:
            continue
        to_create[key] = row["name"] or key

    if not to_create:
        return codes

    resp = make_peak_request("/products", "POST", {"PeakProducts": {"products": [
        # Peak caps product names at 100 chars and rejects longer ones
        # outright (resCode 400), which would leave the product uncreated and
        # the whole branch-day unissuable.  The marker round-trips our key
        # through ``description`` so the response can be correlated back.
        {"name": name[:100], "description": key}
        for key, name in to_create.items()
    ]}})

    new_maps = []
    for product in (resp.get("PeakProducts") or {}).get("products") or []:
        key = str(product.get("description") or "").strip()
        if not key:
            continue
        if not product.get("code"):
            # Rejected product. Skip it rather than crash: the caller's
            # missing-code guard then refuses just this branch-day.
            print(f"Peak product create failed for {key}: {product.get('resDesc')}")
            continue
        codes[key] = product["code"]
        if not key.startswith("name:"):
            new_maps.append(PeakProductMap(
                product_id=key, peak_code=product["code"],
            ))

    if new_maps:
        PeakProductMap.objects.bulk_create(new_maps, ignore_conflicts=True)

    return codes


def build_consolidated_receipt(
    day, branch_payload: dict, contact_id: str, codes: dict[str, str],
) -> dict:
    """Return the Peak receipt body for one branch-day.

    Shaped exactly like the SilomPOS consolidation: every product goes in at
    its gross price and one ``discountTotal`` reconciles the products total
    down to the money actually taken, so
    ``products_total - discountTotal == the amount we send``.

    Raises :class:`ValueError` when the branch-day has no items or a product
    Peak never accepted — the caller skips that branch-day and reports it.
    """
    rows = branch_payload["items"]
    if not rows:
        raise ValueError("no items")

    missing = sorted({
        (r["product_id"] or f"name:{r['name']}") for r in rows
        if (r["product_id"] or f"name:{r['name']}") not in codes
    })
    if missing:
        raise ValueError(f"unmapped products: {missing}")

    issued_date = day.strftime("%Y%m%d")
    products = [
        {
            "productCode": codes[r["product_id"] or f"name:{r['name']}"],
            "name": r["name"],
            "description": r["name"],
            "quantity": r["qty"],
            "price": r["unit_price"],
            "discount": 0,
            "vatType": 3,
            "accountCode": "410107",
        }
        for r in rows
    ]

    amount = Decimal(str(branch_payload["grand_total"]))
    products_total = Decimal(str(branch_payload["items_gross"]))
    # Derived from the already-rounded amount so that
    # products_total - discountTotal == the amount we send, exactly as the
    # SilomPOS job does it.  On a day whose card processing fees exceed the
    # discounts given, the money taken is more than the products are worth and
    # this goes negative; it is sent as-is rather than clamped, so the receipt
    # still totals to what was actually taken.  If Peak rejects it, the caller
    # reports that branch-day and the rest of the day still bills.
    discount_total = products_total - amount

    branch_name = branch_payload["branch_name"]
    branch_id = branch_payload["branch_id"]
    payment_method = _payment_method(
        branch_payload["peak_account_code"], float(amount),
    )

    return {
        "contact": {"id": contact_id},
        "issuedDate": issued_date,
        "discountTotal": float(discount_total),
        "taxStatus": 1,
        "remark": f"{issued_date}-{branch_name}({branch_id})",
        "tags": [
            f"Brave POS consolidated receipt {issued_date}",
            f"branch_name:{branch_name}",
            f"branch_id:{branch_id}",
        ],
        "isTaxInvoice": 1,
        "products": products,
        "paidPayments": {
            "paymentDate": issued_date,
            "payments": [{**payment_method, "paymentMethod": payment_method}],
        },
    }


def poll_consolidated_receipt(cr: ConsolidatedReceipt, attempts: int = 5,
                              interval: float = 2.0) -> Optional[str]:
    """Poll the queue until Peak materialises the document, then store the
    payload and return its receipt code.  ``None`` while still processing.

    The response is only written once Peak has actually produced a receipt.
    Storing the enqueue acknowledgement instead would overwrite the code of
    the document this one replaces — leaving a branch-day whose live Peak
    receipt is recorded nowhere.
    """
    if not cr.peak_queue_id:
        return None

    for attempt in range(attempts):
        if attempt:
            time.sleep(interval)
        resp = make_peak_request(
            "/receipts/queue", "GET", {"queueId": cr.peak_queue_id},
        )
        receipts = (resp.get("PeakReceipts") or {}).get("receipts") or []
        if receipts:
            cr.response = resp
            cr.save(update_fields=["response", "updated_at"])
            return receipts[0].get("code") or ""
    return None


def issue_consolidated_receipt(day, branch, branch_payload: dict,
                               contact_id: str, reissue: bool = False,
                               poll_attempts: int = 5) -> tuple[ConsolidatedReceipt, str]:
    """Issue (or resume) the one Peak receipt for ``branch`` on ``day``.

    Returns ``(row, action)`` where action is one of ``issued``, ``polled``,
    ``already-issued``.  Idempotent by design — the (branch, date) row is the
    record of what has been billed, so a second run polls or no-ops instead of
    enqueuing a second receipt for the same sales.
    """
    cr = ConsolidatedReceipt.objects.filter(branch=branch, date=day).first()

    if cr and cr.peak_code and not reissue:
        return cr, "already-issued"

    # Enqueued earlier but never confirmed: finish that attempt rather than
    # start a new one, or the branch-day ends up with two live documents.
    if cr and cr.peak_queue_id and not cr.peak_code and not reissue:
        poll_consolidated_receipt(cr, attempts=poll_attempts)
        return cr, "polled"

    # Void first, issue second.  The other order double-bills the branch-day
    # whenever the void fails.
    if cr and cr.peak_code:
        void_receipt(cr.peak_code)

    codes = ensure_consolidated_product_codes(branch_payload["items"])
    receipt = build_consolidated_receipt(day, branch_payload, contact_id, codes)

    resp = make_peak_request("/receipts/queue", "POST", {
        "PeakReceipts": {"receipts": [receipt]},
    })
    queue_id = resp.get("queueId")
    if not queue_id:
        raise RuntimeError(f"Peak did not return a queueId: {resp!r}")

    with transaction.atomic():
        cr, _ = ConsolidatedReceipt.objects.get_or_create(branch=branch, date=day)
        cr.peak_queue_id = queue_id
        cr.needs_reissue = False
        # response deliberately left alone until the poll confirms — see
        # poll_consolidated_receipt.
        cr.save(update_fields=["peak_queue_id", "needs_reissue", "updated_at"])
        # set(), not add(): the link table must hold exactly the bills this
        # receipt covers, or a reissued day shows bills its live document
        # never counted.
        cr.orders.set(Order.objects.filter(id__in=branch_payload["order_ids"]))

    poll_consolidated_receipt(cr, attempts=poll_attempts)
    return cr, "issued"
