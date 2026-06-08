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
from typing import Any, Optional

import httpx

from .models import (
    Order,
    OrderItem,
    PeakClientToken,
    PeakProductMap,
    PeakRequest,
)


PEAK_BASE_URL = "https://peakengineapi.azurewebsites.net/api/v1"


def _get_peak_token() -> str:
    """Return the current Peak Client-Token.  Reads the DB row each call
    so a refreshed token (written by an admin task / cron) takes effect
    without an app restart.  Falls back to the env var ``PEAK_CLIENT_TOKEN``
    when no row exists yet — seeds the row on first use so subsequent
    calls hit the DB."""
    row = PeakClientToken.objects.filter(pk="default").first()
    if row and row.token:
        return row.token
    seed = os.environ.get("PEAK_CLIENT_TOKEN", "")
    if seed:
        PeakClientToken.objects.update_or_create(pk="default", defaults={"token": seed})
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
    five times for transient KeyError/IndexError in the response shape."""
    body = {"PeakContacts": {"contacts": [_build_peak_contact(form_data)]}}
    last_err: Optional[Exception] = None
    for attempt in range(5):
        resp = make_peak_request("/contacts", "POST", body)
        try:
            return resp["PeakContacts"]["contacts"][0]["id"]
        except (KeyError, IndexError, TypeError) as e:
            last_err = e
            time.sleep(2)
    raise RuntimeError(f"Could not extract Peak contact id after 5 retries: {last_err}")


def create_peak_receipt_for_order(order: Order) -> Optional[str]:
    """End-to-end Peak receipt creation for a Brave POS order.

    Returns the Peak ``documentLink`` (HTTPS URL) on success, or ``None``
    if the receipt is still in the queue after our polling budget.  The
    caller (usually a view) decides whether to redirect, show a 'still
    processing' page, or schedule another poll.
    """
    form = order.tax_invoice_data or {}
    peak_products, total_item_price = _build_peak_products(order)
    contact_id = _create_peak_contact(form)

    amount = float(order.total or 0)
    total_discount = max(total_item_price - amount, 0)
    issued_date = order.created_at.astimezone().strftime("%Y%m%d")

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
                    "code": "BSV003",
                    "amount": amount,
                    "paymentMethod": {"code": "BSV003", "amount": amount},
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

    order.peak_queue_id = queue_id
    order.save(update_fields=["peak_queue_id"])

    # Poll until Peak materialises the receipt.  Total budget: 10 × 5s.
    queue_res: dict = {}
    for _ in range(10):
        queue_res = make_peak_request("/receipts/queue", "GET", {"queueId": queue_id})
        receipts = (queue_res.get("PeakReceipts") or {}).get("receipts") or []
        if receipts:
            order.peak_response = queue_res
            order.save(update_fields=["peak_response"])
            return receipts[0].get("documentLink")
        time.sleep(5)

    # Still processing — caller can poll again later.
    return None
