"""Backoffice views — server-rendered Bootstrap dashboard backed by the
existing bravepos Django models. All views require a Django auth login
(see /backoffice/login/); the DRF POS API at /api/* uses its own token
auth and is unaffected. Filtering is via query string:
?branch=<uuid>&from=YYYY-MM-DD&to=YYYY-MM-DD."""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse, HttpResponseNotModified, HttpResponseRedirect
from django.db import transaction
from django.db.models import (
    Count, DecimalField, Exists, ExpressionWrapper, F, Max, Min, OuterRef, Sum, Q,
)
from django.db.models.functions import Coalesce, Length, TruncDate
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from bravepos.models import (
    AppRelease,
    AuditLog,
    Branch,
    BranchSession,
    Category,
    Customer,
    DiscountType,
    Order,
    OrderItem,
    Product,
    Settings,
    Shift,
    Staff,
    StockDocument,
    StockDocumentItem,
    StockMovement,
    Unit,
)
from bravepos import appdist, catalog, crm, discounts, images
from bravepos.gateways import seed_branch_payment
from bravepos.staff_provisioning import DEFAULT_ADMIN_PIN, DEFAULT_CASHIER_PIN
from bravepos.views import _next_stock_doc_no


def _parse_date(s: str | None, default: date) -> date:
    if not s:
        return default
    try:
        return date.fromisoformat(s)
    except ValueError:
        return default


def _date_window(dfrom: date, dto: date):
    """Convert two dates into an inclusive aware-datetime window in local TZ."""
    tz = timezone.get_current_timezone()
    start = datetime.combine(dfrom, time.min).replace(tzinfo=tz)
    end = datetime.combine(dto, time.max).replace(tzinfo=tz)
    return start, end


def _money(value) -> float:
    """Decimals don't JSON-serialise; charts need floats."""
    if value is None:
        return 0.0
    return float(value)


def _pct_change(now, before):
    """Percentage change, or None when there is nothing to compare against.

    None and 0 must stay distinguishable: "no data last month" is not
    "flat on last month", and a template that renders +0.0% for the first
    one is lying about a figure the owner will act on.
    """
    if not before:
        return None
    return float((Decimal(now) - Decimal(before)) / Decimal(before) * 100)


def admin_required(view):
    """Restrict a view to `role == "admin"` accounts.

    Deliberately the *only* permission check in the backoffice — there's no
    role matrix yet. It exists because user management and the audit log are
    the two screens where "any signed-in account can do this" is unacceptable:
    one hands out credentials, the other is the record of who did what.
    """
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if getattr(request.user, "role", "") != "admin":
            return render(request, "backoffice/forbidden.html", {
                "active": "users",
                "hide_dates": True,
                **_branch_topbar_context(request),
            }, status=403)
        return view(request, *args, **kwargs)
    return wrapped


def viewer_forbidden(request):
    """The page `ViewerAccessMiddleware` shows a viewer outside the reports."""
    return render(request, "backoffice/forbidden.html", {
        "reports_only": True,
        "hide_dates": True,
        **_branch_topbar_context(request),
    }, status=403)


# The branch picker sits in the header of every page, but each page is its own
# GET request: leave Catalogue and the `?branch=` stays behind with it, so the
# next tab fell back to whichever branch sorts first. Which branch you are
# looking at is a property of the session, not of one URL — so it is remembered
# here. An explicit `?branch=` still wins, and is what updates the memory, so a
# shared or bookmarked link keeps meaning exactly what it said.
SESSION_BRANCH_KEY = "backoffice_branch"


def _select_branch(request, branches, remember=True):
    """The branch this request is scoped to: `?branch=` when it names an active
    branch, else the one last picked in this session, else the first.

    `remember=False` for the one page whose `?branch=` is its own filter rather
    than the header picker — reading it as a new global scope would let a
    throwaway filter follow the user onto every other page.
    """
    by_id = {str(b.id): b for b in branches}

    requested = request.GET.get("branch") or ""
    if remember and requested in by_id:
        # Only write on a change — an unconditional assignment marks the
        # session dirty and re-saves it on every page view.
        if request.session.get(SESSION_BRANCH_KEY) != requested:
            request.session[SESSION_BRANCH_KEY] = requested
        return by_id[requested]

    # A branch that has since been archived is no longer a choice, so a stale
    # id in the session falls through to the default rather than pinning the
    # whole backoffice to a branch the picker can't even show.
    remembered = by_id.get(request.session.get(SESSION_BRANCH_KEY) or "")
    if remembered is not None:
        return remembered

    return branches[0] if branches else None


def _common_filters(request):
    """Branch / date-range filter values that every report page uses."""
    today = timezone.localdate()
    branches = list(Branch.objects.filter(active=True).order_by("name"))
    branch = _select_branch(request, branches)
    dfrom = _parse_date(request.GET.get("from"), today)
    dto = _parse_date(request.GET.get("to"), today)
    if dto < dfrom:
        dfrom, dto = dto, dfrom
    return branches, branch, dfrom, dto


def _filter_qs(request, **extra):
    """Build the persistent ?branch=&from=&to=… query string for pagination
    links so the user keeps their filters when paging."""
    keep = {}
    for key in ("branch", "from", "to"):
        if request.GET.get(key):
            keep[key] = request.GET[key]
    keep.update({k: v for k, v in extra.items() if v is not None})
    from urllib.parse import urlencode
    return urlencode(keep)


def _csv_num(v) -> str:
    """Format a Decimal/number for CSV money cells: fixed 2 places, no commas."""
    return f"{(v or Decimal(0)):.2f}"


def _csv_response(filename: str):
    """A text/csv attachment response, BOM-prefixed so Excel reads UTF-8
    (Thai shop/product names) correctly, with a csv.writer over it."""
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("﻿")  # UTF-8 BOM
    return response, csv.writer(response)


def _write_export_header(writer, title, branch, dfrom, dto):
    """Write the shared report header block (title, shop, branch, date
    window) used by every backoffice CSV export. The 'To' value caps at the
    export moment for an in-progress current day, matching SilomPOS."""
    settings_row = Settings.objects.first()
    shop_name = settings_row.shop_name if settings_row else ""
    start, end = _date_window(dfrom, dto)
    now = timezone.localtime()
    to_dt = now if dto == now.date() else timezone.localtime(end)

    writer.writerow([title])
    writer.writerow(["Shop", shop_name])
    writer.writerow(["Branch", branch.name if branch else "All"])
    writer.writerow([])
    writer.writerow(["From", timezone.localtime(start).strftime("%d %B %Y %H:%M:%S")])
    writer.writerow(["To", to_dt.strftime("%d %B %Y %H:%M:%S")])
    writer.writerow([])


# Where a receipt-QR scan sends the customer.  ``oid`` lets the form tie a
# review back to the order it came from.
FEEDBACK_FORM_URL = "https://rollingpinn.formaloo.me/zg8zkq"


def customer_receipt(request, order_number: str):
    """Where the receipt QR points.  No auth required — the scanner is a
    customer with no session — and it 302s straight to the feedback form.

    This has gone back and forth.  It was a two-button menu (issue a full tax
    invoice / leave a review), then a redirect, then the menu again on the
    argument that a customer who only realises they need a tax invoice after
    leaving has no cashier to ask.  It is a redirect again by decision: the
    scan is for feedback, and the tax invoice is the counter's job.

    The tax-invoice views under ``/receipt/<n>/tax-invoice/`` stay mounted, so
    anyone holding that link — a customer sent it directly, or a cashier
    working a walked-away case — can still reach the form.  Only the automatic
    landing page is gone."""
    return HttpResponseRedirect(f"{FEEDBACK_FORM_URL}?oid={order_number}")


def _public_chrome(request) -> dict:
    """The page chrome the rail's context processor only fills in for a
    signed-in user.  A page reachable without a login has to name the shop
    itself, or a customer reads the product's name where the shop's belongs."""
    if request.user.is_authenticated:
        return {}
    settings_row = Settings.objects.first()
    return {"shop_name": settings_row.shop_name if settings_row else "Brave POS"}


def _tax_invoice_prefill(order) -> dict:
    """Values to open the Peak tax-invoice form with, in the form's own schema.

    Three sources, most-recently-stated first:

      * ``tax_invoice_data`` — this exact form, submitted before.  A bill only
        keeps it when Peak never returned a document (it errored, or the page
        was closed while it was queueing), so re-opening the form should show
        what was typed rather than an empty one.
      * ``pos_tax_invoice`` — the slip the till already issued for this bill.
        Same buyer, different schema: one ``address`` block, which is this
        form's ``registered_address``.
      * the ``Customer`` on the bill, whose tax identity is captured once and
        reused on every later invoice for that buyer.

    Only ever handed to a signed-in admin.  Bill numbers are sequential, so an
    anonymous visitor who can guess one must not be shown the buyer's tax ID
    and registered address on it.
    """
    if order is None:
        return {}

    submitted = order.tax_invoice_data or {}
    if submitted:
        return submitted

    issued = order.pos_tax_invoice or {}
    customer = order.customer
    name = issued.get("name") or ""
    if not name and customer is not None:
        name = " ".join(p for p in (customer.name, customer.last_name) if p)
    return {
        "name": name,
        "tax_id": issued.get("tax_id") or (customer.tax_id if customer else ""),
        "registered_address": issued.get("address") or (customer.address if customer else ""),
        "registered_country": "Thailand",
    }


def create_tax_invoice(request, order_number: str):
    """Tax-invoice creation form for a given order.  The Save button
    POSTs the form to :func:`save_tax_invoice` which then hands off to
    the Peak flow.  Reached from Transactions, or by anyone sent the link.

    If this order already has a Peak tax invoice (e.g. the customer scans
    the QR a second time and presses the button again), skip the form and
    redirect straight to the existing document — one order, one receipt.

    Two audiences, one form, so the shell is chosen here rather than baked in.
    An admin arrives from Transactions and stays in the backoffice: the rail,
    the bill they clicked, and whatever the bill already knows about the buyer.
    A buyer holding the link has no login, so they get the same design system
    with none of the chrome — and a blank form over an unnamed bill, because
    the particulars on a guessable bill number are not theirs to read.
    """
    order = (
        Order.objects.select_related("branch", "customer")
        .filter(order_number=order_number)
        .first()
    )
    if order is not None:
        link = _document_link_from_response(order.peak_response)
        if link:
            return HttpResponseRedirect(link)

    signed_in = request.user.is_authenticated
    context = {
        "base_template": "backoffice/base.html" if signed_in else "backoffice/base_plain.html",
        # Nothing on this page is scoped to a branch or a date window, and the
        # rail should still show where you came from.
        "active": "transactions",
        "hide_branch": True,
        "hide_dates": True,
        "order_number": order_number,
        "prefill": _tax_invoice_prefill(order) if signed_in else {},
    }
    if signed_in and order is not None:
        tax_percent, tax_mode, service_charge_pct = _tax_settings()
        context["order"] = order
        context["tax_percent"] = tax_percent
        context["row"] = _build_transaction_row(
            order, tax_percent, tax_mode, service_charge_pct
        )
    context.update(_public_chrome(request))

    return render(request, "backoffice/create_tax_invoice.html", context)


# ─── Peak full-tax-invoice flow ─────────────────────────────────────────────
# Three views move the customer from "filled out the form" to "looking
# at their tax-invoice PDF":
#
#   POST /receipt/<n>/tax-invoice/save/      → save_tax_invoice
#         persists form data → returns loading page URL
#   GET  /receipt/<n>/tax-invoice/progress/  → tax_invoice_progress
#         renders a polling page that JS-fetches the process URL
#   GET  /receipt/<n>/tax-invoice/process/   → tax_invoice_process
#         runs the Peak workflow, returns {documentLink} when ready
#
# CSRF is exempted on save/process because the customer hitting Submit
# isn't a logged-in user — this is the same trust model Shopster uses
# for its public peak_create_receipt_view endpoint.

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from bravepos.models import Order
from bravepos.peak import create_peak_receipt_for_order, _document_link_from_response


def _form_to_tax_invoice_data(post) -> dict:
    """Extract the form fields submitted from create_tax_invoice.html
    into a flat dict.  Stored verbatim on Order.tax_invoice_data so the
    Peak helper (or a human inspecting the DB later) can re-build the
    contact payload without parsing a request body twice."""
    fields = (
        "name", "tax_id", "customer_type",
        "registered_address", "registered_country",
        "registered_province", "registered_city",
        "registered_district", "registered_postal_code",
    )
    return {f: (post.get(f) or "").strip() for f in fields}


@csrf_exempt
def save_tax_invoice(request, order_number: str):
    """Persist the customer-submitted tax-invoice form on the matching
    Order row, then redirect the browser to the loading page that will
    drive the Peak API call.  Idempotent — a second submit just
    overwrites the previously-saved form data."""
    if request.method != "POST":
        return HttpResponseRedirect(
            reverse("create_tax_invoice", kwargs={"order_number": order_number})
        )

    order = get_object_or_404(Order, order_number=order_number)
    order.tax_invoice_data = _form_to_tax_invoice_data(request.POST)
    order.save(update_fields=["tax_invoice_data"])

    return HttpResponseRedirect(
        reverse("tax_invoice_progress", kwargs={"order_number": order_number})
    )


def tax_invoice_progress(request, order_number: str):
    """Loading page — JS on the page polls the process endpoint and
    redirects to the Peak document URL when one comes back.  Kept as a
    separate route so a user who refreshes the form-save target doesn't
    re-trigger the Peak flow."""
    process_url = reverse("tax_invoice_process", kwargs={"order_number": order_number})
    return render(request, "backoffice/tax_invoice_progress.html", {
        "order_number": order_number,
        "process_url": process_url,
        **_public_chrome(request),
    })


@csrf_exempt
def tax_invoice_process(request, order_number: str):
    """Run the Peak workflow for ``order_number`` and return JSON.

    Response shape:
        * ``{"status": "ready", "url": "<documentLink>"}`` — receipt done,
          progress page should redirect to ``url``.
        * ``{"status": "processing", "queueId": "..."}`` (HTTP 202) — Peak
          hasn't finished; the progress page should poll again.
        * ``{"status": "error", "error": "..."}`` (HTTP 400/500) — give up
          and surface the message.

    The actual API work happens inline.  This means the request can take
    up to ~50 seconds during the polling loop, which is fine for a
    customer-initiated single-shot request but would NOT be appropriate
    for a high-QPS endpoint."""
    order = get_object_or_404(Order, order_number=order_number)
    if not order.tax_invoice_data:
        return JsonResponse({"status": "error", "error": "Tax invoice form not submitted"}, status=400)

    # If we already have a document link from a previous attempt, short-
    # circuit and return it.  Lets the customer come back to the URL
    # later without re-creating the receipt in Peak.
    link = _document_link_from_response(order.peak_response)
    if link:
        return JsonResponse({"status": "ready", "url": link})

    try:
        document_link = create_peak_receipt_for_order(order)
    except Exception as exc:  # noqa: BLE001 — surface Peak/HTTP errors to caller
        return JsonResponse({"status": "error", "error": str(exc)}, status=500)

    if document_link:
        return JsonResponse({"status": "ready", "url": document_link})
    return JsonResponse(
        {"status": "processing", "queueId": order.peak_queue_id},
        status=202,
    )


@login_required
def dashboard(request):
    today = timezone.localdate()
    branches = list(Branch.objects.filter(active=True).order_by("name"))
    branch = _select_branch(request, branches)

    dfrom = _parse_date(request.GET.get("from"), today)
    dto = _parse_date(request.GET.get("to"), today)
    if dto < dfrom:
        dfrom, dto = dto, dfrom

    start, end = _date_window(dfrom, dto)

    # Base order queryset for this branch + window. Cancelled orders are
    # excluded from sales totals but counted separately as "Cancel" bills.
    orders_all = Order.objects.filter(created_at__gte=start, created_at__lte=end)
    if branch:
        orders_all = orders_all.filter(branch=branch)
    orders = orders_all.exclude(status="cancel")

    # ── Top tiles + tax breakdown ─────────────────────────────────────────
    sales_agg = orders.aggregate(
        sales=Sum("total"),
        subtotal=Sum("subtotal"),
        discount=Sum("discount_amount"),
        bills=Count("id"),
    )
    sales = sales_agg["sales"] or Decimal(0)
    subtotal = sales_agg["subtotal"] or Decimal(0)
    discount = sales_agg["discount"] or Decimal(0)
    bills = sales_agg["bills"] or 0
    cancel_bills = orders_all.filter(status="cancel").count()

    items = OrderItem.objects.filter(order__in=orders)
    profit = items.annotate(
        line_profit=(F("price") - Coalesce(F("product__cost"), Decimal(0))) * F("qty")
    ).aggregate(p=Sum("line_profit"))["p"] or Decimal(0)

    settings_row = Settings.objects.first()
    tax_percent = settings_row.tax_percent if settings_row else Decimal("7")
    taxable_base = subtotal - discount
    # Prefer the VAT the POS stored per order (VAT-inclusive) so the summary
    # matches the per-bill report; fall back to the settings calc for orders
    # predating ``vat_amount``.
    stored_vat = orders.aggregate(v=Sum("vat_amount"))["v"] or Decimal(0)
    if stored_vat:
        tax_amount = stored_vat
        total_incl_tax = taxable_base
        total_non_tax = taxable_base - tax_amount
    elif settings_row and settings_row.tax_mode == "inclusive":
        # Total already includes tax — back it out.
        tax_amount = taxable_base * tax_percent / (Decimal(100) + tax_percent)
        total_incl_tax = taxable_base
        total_non_tax = taxable_base - tax_amount
    else:
        tax_amount = taxable_base * tax_percent / Decimal(100)
        total_incl_tax = taxable_base + tax_amount
        total_non_tax = taxable_base

    avg_per_bill = (sales / bills) if bills else Decimal(0)

    # ── Payment donut ────────────────────────────────────────────────────
    payment_rows = list(
        orders.values("payment_method").annotate(total=Sum("total")).order_by("-total")
    )
    payment_chart = {
        "labels": [(r["payment_method"] or "Unknown").title() for r in payment_rows],
        "values": [_money(r["total"]) for r in payment_rows],
    }

    # ── Inventory tiles ──────────────────────────────────────────────────
    products = Product.objects.filter(active=True)
    if branch:
        products = products.filter(branch=branch)
    inv_agg = products.aggregate(
        qty=Sum("stock"),
        cost_value=Sum(F("cost") * F("stock")),
        inv_value=Sum(F("price") * F("stock")),
    )
    inv_qty = inv_agg["qty"] or 0
    cost_value = inv_agg["cost_value"] or Decimal(0)
    inv_value = inv_agg["inv_value"] or Decimal(0)

    # ── Sales-by-time histogram ──────────────────────────────────────────
    # Single day → 24 hourly buckets. Range → per-day buckets.
    order_tuples = list(orders.values_list("created_at", "total"))
    if dfrom == dto:
        bucket_labels = [f"{h:02d}:00" for h in range(24)]
        buckets = [0.0] * 24
        for created_at, total in order_tuples:
            hour = timezone.localtime(created_at).hour
            buckets[hour] += _money(total)
    else:
        days = (dto - dfrom).days + 1
        bucket_labels = [(dfrom + timedelta(days=i)).strftime("%d/%m") for i in range(days)]
        buckets = [0.0] * days
        for created_at, total in order_tuples:
            idx = (timezone.localtime(created_at).date() - dfrom).days
            if 0 <= idx < days:
                buckets[idx] += _money(total)
    sales_chart = {"labels": bucket_labels, "values": buckets}

    # ── Best sellers (top 5, matching SilomPOS) ──────────────────────────
    # NB: alias names must not collide with model field names — using `qty` as
    # the alias here causes Django to resolve F("qty") in the next annotation
    # against the aggregate instead of the column, raising FieldError.
    top_products = list(
        items.values("name")
        .annotate(qty_sold=Sum("qty"), sales=Sum(F("price") * F("qty")))
        .order_by("-sales")[:5]
    )
    top_products_chart = {
        "labels": [r["name"] for r in top_products],
        "values": [_money(r["sales"]) for r in top_products],
    }

    top_categories = list(
        items.exclude(category_name="")
        .values("category_name")
        .annotate(qty_sold=Sum("qty"), sales=Sum(F("price") * F("qty")))
        .order_by("-sales")[:5]
    )
    top_categories_chart = {
        "labels": [r["category_name"] for r in top_categories],
        "values": [_money(r["sales"]) for r in top_categories],
    }

    # ── Delivery Channels ────────────────────────────────────────────────
    # Group by Order.delivery_provider; empty string = walk-in / in-store.
    channel_rows = list(
        orders.values("delivery_provider")
        .annotate(qty=Count("id"), channel_sales=Sum("total"))
        .order_by("-channel_sales")
    )
    delivery_channels = [
        {
            "name": r["delivery_provider"] or "In-store",
            "qty": r["qty"] or 0,
            "sales": r["channel_sales"] or Decimal(0),
        }
        for r in channel_rows
    ]
    delivery_totals_revenue = sum((c["sales"] for c in delivery_channels), Decimal(0))
    delivery_order_total = sum(c["qty"] for c in delivery_channels)
    delivery_channels_chart = {
        "labels": [c["name"] for c in delivery_channels],
        "values": [_money(c["sales"]) for c in delivery_channels],
    }

    # ── Table Usage total ────────────────────────────────────────────────
    # The Order model doesn't track party size or table open/close timestamps
    # yet — Customer Avg, Table Usage, and Time Avg stay at 0 until those
    # columns exist. SilomPOS shows 0s here too for branches without table
    # service, so the layout matches even with zeros.
    total_items_qty = items.aggregate(q=Sum("qty"))["q"] or 0
    items_per_bill = (total_items_qty / bills) if bills else 0
    table_usage = {
        "items_avg": items_per_bill,
        "items_total": total_items_qty,
        "customer_avg": 0,
        "customer_total": 0,
        "bill_per_table_per_day": 0,
        "table_open_count": 0,
        "time_avg_hours": 0,
        "time_avg_seconds": 0,
        "time_total_seconds": 0,
    }

    # ── The same window, one window earlier ──────────────────────────────
    # Growth is the whole question a dashboard answers. Every headline figure
    # carries its change against the immediately preceding window of equal
    # length, and the trend chart draws that window behind this one in grey.
    span = (dto - dfrom).days + 1
    prev_from, prev_to = dfrom - timedelta(days=span), dfrom - timedelta(days=1)
    prev_start, prev_end = _date_window(prev_from, prev_to)
    prev_orders = Order.objects.filter(
        created_at__gte=prev_start, created_at__lte=prev_end,
    ).exclude(status="cancel")
    if branch:
        prev_orders = prev_orders.filter(branch=branch)
    prev_agg = prev_orders.aggregate(sales=Sum("total"), bills=Count("id"))
    prev_sales = prev_agg["sales"] or Decimal(0)
    prev_bills = prev_agg["bills"] or 0
    prev_avg = (prev_sales / prev_bills) if prev_bills else Decimal(0)

    # Cancelled bills are money that was rung up and then wasn't. Shown as a
    # share of sales because ฿4,120 means nothing without the denominator.
    void_value = orders_all.filter(status="cancel").aggregate(
        v=Sum("total"))["v"] or Decimal(0)

    # Both series share one bucket layout so the two lines are comparable
    # point for point; the previous window is re-indexed onto this one's days.
    prev_buckets = [0.0] * len(buckets)
    for created_at, total in prev_orders.values_list("created_at", "total"):
        if dfrom == dto:
            prev_buckets[timezone.localtime(created_at).hour] += _money(total)
        else:
            idx = (timezone.localtime(created_at).date() - prev_from).days
            if 0 <= idx < len(prev_buckets):
                prev_buckets[idx] += _money(total)
    sales_chart["previous"] = prev_buckets

    # ── Branches in this window ──────────────────────────────────────────
    # Rendered even when one branch is selected: seeing the others is how you
    # tell "quiet morning" from "quiet at this shop".
    branch_totals = {
        row["branch"]: row
        for row in Order.objects.filter(created_at__gte=start, created_at__lte=end)
        .exclude(status="cancel")
        .values("branch")
        .annotate(sales=Sum("total"), bills=Count("id"))
    }
    open_shifts = {
        s.branch_id: s for s in Shift.objects.filter(status="open").select_related("branch")
    }
    branch_rows = []
    for b in branches:
        row = branch_totals.get(b.id, {})
        branch_rows.append({
            "branch": b,
            "sales": row.get("sales") or Decimal(0),
            "bills": row.get("bills") or 0,
            "shift": open_shifts.get(b.id),
        })
    branch_rows.sort(key=lambda r: r["sales"], reverse=True)
    branch_peak = branch_rows[0]["sales"] if branch_rows else Decimal(0)
    for row in branch_rows:
        row["share"] = float(row["sales"] / branch_peak * 100) if branch_peak else 0

    # A shift open since before today is unreconciled cash sitting in a
    # drawer nobody has counted. It leads the page, above every number.
    today_start, _ = _date_window(today, today)
    stale_shifts = [s for s in open_shifts.values() if s.opened_at < today_start]
    stale_cash = sum((s.total_sales_cash for s in stale_shifts), Decimal(0))

    # ── Needs attention ──────────────────────────────────────────────────
    out_of_stock = products.filter(stock__lte=0).count()
    low_stock = products.filter(stock__gt=0, par_level__gt=0,
                                stock__lt=F("par_level")).count()

    payment_total = sum((r["total"] or Decimal(0) for r in payment_rows), Decimal(0))
    payment_mix = [
        {
            "name": (r["payment_method"] or "Unknown").title(),
            "total": r["total"] or Decimal(0),
            "pct": float((r["total"] or 0) / payment_total * 100) if payment_total else 0,
        }
        for r in payment_rows
    ]

    context = {
        "active": "dashboard",
        "page_title": "Dashboard",
        "branches": branches,
        "branch": branch,
        "date_from": dfrom.isoformat(),
        "date_to": dto.isoformat(),
        "now": timezone.localtime(),
        # Headline comparison
        "prev_sales": prev_sales,
        "prev_bills": prev_bills,
        "sales_delta": _pct_change(sales, prev_sales),
        "bills_delta": _pct_change(bills, prev_bills),
        "avg_delta": _pct_change(avg_per_bill, prev_avg),
        "span_days": span,
        # Named rather than "vs previous period" so the comparison is a fact,
        # not a promise: the reader can check it against the date picker.
        "compare_label": (
            "vs " + prev_from.strftime("%-d %b") if span == 1
            else f"vs {prev_from:%-d %b} – {prev_to:%-d %b}"
        ),
        "qs": _filter_qs(request),
        "void_value": void_value,
        "void_share": float(void_value / sales * 100) if sales else 0,
        # Panels
        "branch_rows": branch_rows,
        "payment_mix": payment_mix,
        "latest_orders": list(
            orders_all.select_related("branch").order_by("-created_at")[:6]
        ),
        "stale_shifts": stale_shifts,
        "stale_cash": stale_cash,
        "out_of_stock": out_of_stock,
        "low_stock": low_stock,
        "open_shift_count": len(open_shifts),
        # Tiles
        "sales": sales,
        "profit": profit,
        "discount": discount,
        # Bill total card
        "bills": bills,
        "avg_per_bill": avg_per_bill,
        "cancel_bills": cancel_bills,
        # Tax breakdown
        "subtotal": subtotal,
        "total_incl_tax": total_incl_tax,
        "total_non_tax": total_non_tax,
        "tax_percent": tax_percent,
        "tax_amount": tax_amount,
        "grand_total": sales,
        # Inventory
        "inv_qty": inv_qty,
        "cost_value": cost_value,
        "inv_value": inv_value,
        # Charts (JSON-encoded for safe template injection)
        "payment_chart_json": json.dumps(payment_chart),
        "sales_chart_json": json.dumps(sales_chart),
        "top_products_chart_json": json.dumps(top_products_chart),
        "top_categories_chart_json": json.dumps(top_categories_chart),
        # Tables
        "top_products": top_products,
        "top_categories": top_categories,
        # Delivery Channels
        "delivery_channels": delivery_channels,
        "delivery_totals_revenue": delivery_totals_revenue,
        "delivery_order_total": delivery_order_total,
        "delivery_channels_chart_json": json.dumps(delivery_channels_chart),
        # Table Usage
        "table_usage": table_usage,
    }
    return render(request, "backoffice/dashboard.html", context)


# ─── Transactions ───────────────────────────────────────────────────────
def _transactions_qs(request):
    """Filtered, prefetched order queryset shared by the page and the
    CSV export so both honour the same filters — branch, date window, status,
    payment method and free-text search.

    The export reading the *same* function is the point: a filtered screen
    that exports something else is how a reconciliation goes wrong quietly.
    """
    branches, branch, dfrom, dto = _common_filters(request)
    start, end = _date_window(dfrom, dto)

    qs = (
        Order.objects.filter(created_at__gte=start, created_at__lte=end)
        .select_related("branch", "customer")
        .prefetch_related("items", "items__product")
        .order_by("-created_at")
    )
    if branch:
        qs = qs.filter(branch=branch)

    status = request.GET.get("status") or "all"
    if status == "paid":
        qs = qs.exclude(status="cancel")
    elif status == "voided":
        qs = qs.filter(status="cancel")

    payment = (request.GET.get("payment") or "").strip()
    if payment:
        qs = qs.filter(payment_method__icontains=payment)

    query = (request.GET.get("q") or "").strip()
    if query:
        # Bill number, who it was for, or the exact amount — the three things
        # someone holding a paper receipt or a phone can actually type.
        match = (Q(order_number__icontains=query)
                 | Q(customer_name__icontains=query)
                 | Q(customer__phone__icontains=query)
                 | Q(customer__name__icontains=query)
                 | Q(staff__icontains=query))
        try:
            match |= Q(total=Decimal(query))
        except (InvalidOperation, ValueError):
            pass
        qs = qs.filter(match)

    return branches, branch, dfrom, dto, qs


def _tax_settings():
    """Tax / service-charge settings used to derive each row's tax split."""
    settings_row = Settings.objects.first()
    tax_percent = settings_row.tax_percent if settings_row else Decimal("7")
    tax_mode = settings_row.tax_mode if settings_row else "exclusive"
    service_charge_pct = (
        settings_row.service_charge_percent
        if settings_row and settings_row.service_charge_enabled
        else Decimal(0)
    )
    return tax_percent, tax_mode, service_charge_pct


def _build_transaction_row(o, tax_percent, tax_mode, service_charge_pct):
    """Derive the displayed/exported columns for a single order."""
    sub = o.subtotal or Decimal(0)
    disc = o.discount_amount or Decimal(0)
    taxable = sub - disc
    # Prefer the VAT amount the POS computed and stored at sale time (always
    # VAT-inclusive: goods × p/(100+p)) so the report reconciles exactly with
    # the receipt.  Older orders predating ``vat_amount`` fall back to the
    # settings-driven calculation.
    stored_vat = o.vat_amount or Decimal(0)
    if stored_vat:
        tax_amount = stored_vat
        total_incl_tax = taxable
        total_non_tax = taxable - tax_amount
        sub_ex_tax = taxable - tax_amount
    elif tax_mode == "inclusive":
        tax_amount = taxable * tax_percent / (Decimal(100) + tax_percent)
        total_incl_tax = taxable
        total_non_tax = taxable - tax_amount
        sub_ex_tax = taxable - tax_amount
    else:
        tax_amount = taxable * tax_percent / Decimal(100)
        total_incl_tax = taxable + tax_amount
        total_non_tax = taxable
        sub_ex_tax = taxable
    service_charge = taxable * service_charge_pct / Decimal(100)
    # Omise card surcharge (0 for other methods) — passed through so the
    # detail panel / export can show it if needed.
    processing_fee = (o.processing_fee or Decimal(0)) + (o.processing_fee_vat or Decimal(0))

    items_data = []
    for it in o.items.all():
        line_total = (it.price or Decimal(0)) * (it.qty or 0)
        items_data.append({
            "item": it,
            "barcode": it.product.barcode if it.product_id else "",
            "line_total": line_total,
        })

    return {
        "order": o,
        "items": items_data,
        "promotion_discount": Decimal(0),   # no promo tracking yet
        "add_on_total": Decimal(0),         # no add-on tracking yet
        "service_charge": service_charge,
        "rounding_adj": Decimal(0),
        "shipping_fee": Decimal(0),
        "processing_fee": processing_fee,
        "tax_amount": tax_amount,
        "total_incl_tax": total_incl_tax,
        "total_non_tax": total_non_tax,
        "sub_ex_tax": sub_ex_tax,
    }


@login_required
def transactions(request):
    """Per-bill list with expandable detail rows.

    Maps to SilomPOS `/report/transaction`. Each row mirrors the columns
    you see there (Sub Total, Discount, Grand Total, Total incl/non-Tax,
    Sub-total ex-Tax, Tax, Add-on, Service Charge, Rounding, Shipping,
    Status). Clicking a row expands an inline panel with the line items
    and payment block."""
    branches, branch, dfrom, dto, qs = _transactions_qs(request)
    tax_percent, tax_mode, service_charge_pct = _tax_settings()

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))

    rows = [
        _build_transaction_row(o, tax_percent, tax_mode, service_charge_pct)
        for o in page_obj.object_list
    ]

    # The bill shown in the detail pane. Defaults to the first row so the pane
    # is never an empty box waiting to be clicked.
    selected_id = request.GET.get("o") or ""
    selected = next((r for r in rows if str(r["order"].id) == selected_id), None)
    if selected is None and rows:
        selected = rows[0]

    # Whatever is currently filtered, totalled. A page of results whose footer
    # totals something else is worse than no footer.
    window_totals = qs.exclude(status="cancel").aggregate(
        bills=Count("id"), total=Sum("total"),
        discount=Sum("discount_amount"), vat=Sum("vat_amount"),
    )

    context = {
        "active": "transactions",
        "page_title": "Transactions",
        "branches": branches,
        "branch": branch,
        "date_from": dfrom.isoformat(),
        "date_to": dto.isoformat(),
        "rows": rows,
        "selected": selected,
        "page_obj": page_obj,
        "paginator": paginator,
        "qs": _filter_qs(request),
        "tax_percent": tax_percent,
        "status": request.GET.get("status") or "all",
        "statuses": [("all", "All"), ("paid", "Paid"), ("voided", "Voided")],
        "payment": request.GET.get("payment") or "",
        "payments": ["Cash", "PromptPay", "Card"],
        "query": (request.GET.get("q") or "").strip(),
        "window_bills": window_totals["bills"] or 0,
        "window_total": window_totals["total"] or Decimal(0),
        "window_discount": window_totals["discount"] or Decimal(0),
        "window_vat": window_totals["vat"] or Decimal(0),
    }
    return render(request, "backoffice/transactions.html", context)


# English column headers — same column order and semantics as the
# SilomPOS "Sales by Bill" export so the file drops straight into the
# workflows the shop already has built around that spreadsheet.
_TRX_EXPORT_HEADERS = [
    "No.",
    "Date",                                      # date
    "Paid At",                                   # paid-at datetime
    "Bill No.",                                  # bill number
    "Total Before Discount",                     # sub total (before discount)
    "Item Discount",                             # item/line discount
    "Bill Discount",                             # end-of-bill discount
    "Net Total (after discount)",                # net total after discount
    "Taxable Amount (after discount)",           # taxable value after discount
    "Tax-Exempt Amount (after discount)",        # tax-exempt value after discount
    "Service Charge",                            # service charge
    "Shipping Fee",                              # shipping fee
    "Total Before Tax",                          # value before tax
    "Tax Amount",                                # tax value
    "Rounding",                                  # rounding adjustment
    "Grand Total",                               # exempt + before-tax + tax + rounding
    "Customer Name",                             # customer name
    "Table Name",                                # table name
    "Coupon Code",                               # coupon code
    "Coupon Type",                               # coupon type
    "Document Status",                           # document status
    "POS Number",                                # POS machine number
    "Sales Channel",                             # sales channel
    "Note",                                      # note
]

# Columns that carry money — summed into the Summary / Void Summary rows.
# Indexes are 0-based into a data row built by ``_trx_export_row``.
_TRX_MONEY_COLS = [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]


def _trx_export_row(no, o, row):
    """One SilomPOS-style data row for order ``o`` (``row`` is the dict from
    :func:`_build_transaction_row`). Money columns are Decimals so the
    summary rows can sum them; everything else is already a string."""
    net = (o.subtotal or Decimal(0)) - (o.discount_amount or Decimal(0))
    exempt = Decimal(0)
    before_tax = row["sub_ex_tax"]
    tax = row["tax_amount"]
    rounding = row["rounding_adj"]
    grand = exempt + before_tax + tax + rounding
    created = timezone.localtime(o.created_at)
    return [
        no,
        created.strftime("%d %b %Y"),
        created.strftime("%d %b %Y %H:%M:%S"),
        o.order_number,
        o.subtotal or Decimal(0),       # รวมก่อนลด
        o.discount_amount or Decimal(0),  # ส่วนลดรายการ (POS has no bill-level discount)
        Decimal(0),                     # ส่วนลดท้ายบิล
        net,                            # รวมสุทธิ
        net,                            # taxable (no tax-exempt products yet)
        exempt,                         # tax-exempt
        row["service_charge"],          # ค่าบริการ
        Decimal(0),                     # ค่าขนส่ง
        before_tax,                     # รวมมูลค่าก่อนภาษี
        tax,                            # มูลค่าภาษี
        rounding,                       # ปัดเศษ
        grand,                          # grand total
        o.customer_name or "",          # ชื่อลูกค้า
        "-",                            # ชื่อโต๊ะ
        "",                             # รหัสคูปอง
        "",                             # ประเภทคูปอง
        "V" if o.status == "cancel" else "A",  # document status
        "",                             # POS number (filled below)
        "Storefront",                   # sales channel
        "-",                            # note
    ]


@login_required
def transactions_export(request):
    """CSV download of the transactions list for the current filters.

    Mirrors the SilomPOS "Sales by Bill" spreadsheet: a metadata header
    block (shop, branch, date range, timezone), the column headers, one
    row per bill, then Summary / Void Summary footer rows. Covers every
    order in the date/branch window, not just the visible page."""
    _branches, branch, dfrom, dto, qs = _transactions_qs(request)
    tax_percent, tax_mode, service_charge_pct = _tax_settings()

    settings_row = Settings.objects.first()
    shop_name = settings_row.shop_name if settings_row else "Brave POS"
    pos_number = (settings_row.pos_number if settings_row else "") or "001"
    branch_name = branch.name if branch else "All branches"
    tz_name = str(timezone.get_current_timezone())

    fname_branch = branch.name.replace(" ", "_") if branch else "all"
    filename = f"transactions_{fname_branch}_{dfrom.isoformat()}_{dto.isoformat()}.csv"

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("﻿")  # BOM so Excel reads UTF-8 (Thai names) correctly

    writer = csv.writer(response)

    # ── Metadata header block ───────────────────────────────────────────
    writer.writerow(["Sales by Bill Report"])
    writer.writerow(["Shop Name", shop_name])
    writer.writerow(["Branch", branch_name])
    writer.writerow([])
    writer.writerow(["From", dfrom.strftime("%d %B %Y"), "Timezone", tz_name])
    writer.writerow(["To", dto.strftime("%d %B %Y"), "Timezone", tz_name])
    writer.writerow([])
    writer.writerow(_TRX_EXPORT_HEADERS)

    def num(v):
        return f"{(v or Decimal(0)):.2f}"

    def fmt(cell):
        return num(cell) if isinstance(cell, Decimal) else cell

    valid_totals = [Decimal(0)] * len(_TRX_MONEY_COLS)
    void_totals = [Decimal(0)] * len(_TRX_MONEY_COLS)
    valid_count = void_count = 0

    no = 0
    # chunk_size is mandatory once the queryset has prefetch_related() — Django
    # deprecated the bare call in 4.1 and made it a hard ValueError in 5.0, so
    # this export 500s without it. `_transactions_qs` prefetches items and
    # items__product; 500 orders per chunk keeps the prefetch query small
    # enough while still streaming a big date range.
    for o in qs.iterator(chunk_size=500):
        no += 1
        row = _build_transaction_row(o, tax_percent, tax_mode, service_charge_pct)
        data = _trx_export_row(no, o, row)
        data[21] = pos_number  # ขายเลขเครื่อง POS
        writer.writerow([fmt(c) for c in data])

        bucket = void_totals if o.status == "cancel" else valid_totals
        for i, col in enumerate(_TRX_MONEY_COLS):
            bucket[i] += data[col]
        if o.status == "cancel":
            void_count += 1
        else:
            valid_count += 1

    def summary_row(label, count, totals):
        cells = [""] * len(_TRX_EXPORT_HEADERS)
        cells[2] = label
        cells[3] = count
        for i, col in enumerate(_TRX_MONEY_COLS):
            cells[col] = num(totals[i])
        return cells

    writer.writerow([])
    writer.writerow(summary_row("Summary", valid_count, valid_totals))
    writer.writerow(summary_row("Void Summary", void_count, void_totals))

    return response


# Cash-like methods print the Thai "เงินสด" label and show a change line;
# everything else prints its stored method string with no change.
_RECEIPT_CASH_METHODS = {"cash", "เงินสด"}


@login_required
def receipt_print(request, order_number):
    """Print-friendly 'Simplified Tax Invoice' slip for a single bill,
    mirroring the in-app thermal receipt (ReceiptImage). Opened in a new tab
    from the Transactions page; the page auto-opens the browser print dialog
    so the user can Save as PDF or print to a thermal printer.

    VAT is shown inclusive (the displayed prices already include tax), exactly
    like the printed app receipt: value-before-VAT = total / (1 + rate)."""
    order = get_object_or_404(
        Order.objects.select_related("branch", "customer")
        .prefetch_related("items", "items__product"),
        order_number=order_number,
    )
    settings_row = Settings.objects.first()
    tax_percent = settings_row.tax_percent if settings_row else Decimal("7")

    total = order.total or Decimal(0)
    rate = tax_percent / Decimal(100)
    sub_before_vat = (total / (Decimal(1) + rate)) if rate else total
    vat = total - sub_before_vat

    items = []
    item_count = 0
    for it in order.items.all():
        items.append({
            "name": it.name,
            "barcode": (it.product.barcode if it.product_id else "") or "",
            "qty": it.qty or 0,
            "price": it.price or Decimal(0),
            "line_total": (it.price or Decimal(0)) * (it.qty or 0),
        })
        item_count += it.qty or 0
    gross_subtotal = sum((i["line_total"] for i in items), Decimal(0))

    # Queue number - mirror the app: last two digits of the invoice number,
    # leading zeros stripped (PS000000076 -> "76").
    queue = (order.order_number or "")[-2:].lstrip("0") or "1"

    created = timezone.localtime(order.created_at)
    # Thai Buddhist calendar year (Gregorian + 543), e.g. 2026 -> 2569.
    thai_date = f"{created.strftime('%d/%m/')}{created.year + 543} {created.strftime('%H:%M')}"
    short_code = f"#{created.strftime('%y%m%d')}-{order.id.hex[:8].upper()}"

    method = order.payment_method or ""
    is_cash = method.strip().lower() in _RECEIPT_CASH_METHODS

    context = {
        "shop": settings_row,
        "order": order,
        "branch_name": order.branch.name if order.branch_id else (
            settings_row.branch if settings_row else ""),
        "queue": queue,
        "thai_date": thai_date,
        "short_code": short_code,
        "pos_number": (settings_row.pos_number if settings_row else "") or "001",
        # Per branch, never shop-wide — printing another branch's RD machine
        # number on this receipt would misstate which till issued it.  Blank
        # means this branch has no RD number yet and the line is omitted.
        "pos_id": order.branch.pos_id if order.branch_id else "",
        "items": items,
        "item_count": item_count,
        "gross_subtotal": gross_subtotal,
        "discount_amount": order.discount_amount or Decimal(0),
        "taxable_total": total,
        "nontax_total": Decimal(0),
        "sub_before_vat": sub_before_vat,
        "vat": vat,
        "tax_percent": tax_percent,
        "total": total,
        "payment_label": "เงินสด" if is_cash else (method or "เงินสด"),
        "paid_amount": order.paid_amount or total,
        "change": order.change or Decimal(0),
        "is_cash": is_cash,
    }
    return render(request, "backoffice/receipt_print.html", context)



# ─── Sales report by Date ───────────────────────────────────────────────
def _profit_expr():
    """Per-line profit: (price - product.cost) * qty. Wrapped because the
    multiplication output type can't be inferred when one side is nullable."""
    return ExpressionWrapper(
        (F("price") - Coalesce(F("product__cost"), Decimal(0))) * F("qty"),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )


def _report_daily_rows(branch, dfrom, dto):
    """One aggregated row per calendar day in the range — shared by the page
    and its CSV export so both stay in sync."""
    start, end = _date_window(dfrom, dto)

    orders = Order.objects.filter(
        created_at__gte=start, created_at__lte=end
    ).exclude(status="cancel")
    if branch:
        orders = orders.filter(branch=branch)

    settings_row = Settings.objects.first()
    tax_percent = settings_row.tax_percent if settings_row else Decimal("7")
    tax_mode = settings_row.tax_mode if settings_row else "exclusive"

    daily = (
        orders.annotate(date_only=TruncDate("created_at"))
        .values("date_only")
        .annotate(
            subtotal=Sum("subtotal"),
            discount=Sum("discount_amount"),
            total=Sum("total"),
            bill_count=Count("id"),
        )
        .order_by("-date_only")
    )

    profit_by_day = (
        OrderItem.objects.filter(order__in=orders)
        .annotate(date_only=TruncDate("order__created_at"))
        .values("date_only")
        .annotate(profit=Sum(_profit_expr()))
    )
    profit_map = {p["date_only"]: (p["profit"] or Decimal(0)) for p in profit_by_day}

    # Per-day payment method breakdown — one entry per (day, method).
    pay_rows = (
        orders.annotate(date_only=TruncDate("created_at"))
        .values("date_only", "payment_method")
        .annotate(amount=Sum("total"), n=Count("id"))
        .order_by("-amount")
    )
    payments_map: dict = {}
    for entry in pay_rows:
        method = (entry["payment_method"] or "").strip() or "Unknown"
        payments_map.setdefault(entry["date_only"], []).append({
            "method": method.title(),
            "amount": entry["amount"] or Decimal(0),
            "n": entry["n"] or 0,
        })

    # Voided bills, per day. Net alone hides why a day was soft, so the row
    # carries gross, discount and refunds beside it — the arithmetic that
    # produced the figure, not just the figure.
    voided = Order.objects.filter(
        created_at__gte=start, created_at__lte=end, status="cancel",
    )
    if branch:
        voided = voided.filter(branch=branch)
    refund_map = {
        entry["date_only"]: (entry["amount"] or Decimal(0))
        for entry in voided.annotate(date_only=TruncDate("created_at"))
        .values("date_only").annotate(amount=Sum("total"))
    }

    rows = []
    for entry in daily:
        d = entry["date_only"]
        sub = entry["subtotal"] or Decimal(0)
        disc = entry["discount"] or Decimal(0)
        taxable = sub - disc
        if tax_mode == "inclusive":
            tax_amount = taxable * tax_percent / (Decimal(100) + tax_percent)
        else:
            tax_amount = taxable * tax_percent / Decimal(100)
        bills = entry["bill_count"] or 0
        total = entry["total"] or Decimal(0)
        rows.append({
            "date": d,
            "subtotal": sub,
            "discount": disc,
            "refunds": refund_map.get(d, Decimal(0)),
            "tax_amount": tax_amount,
            "service_charge": Decimal(0),
            "profit": profit_map.get(d, Decimal(0)),
            "grand_total": total,
            "bill_count": bills,
            "avg_bill": (total / bills) if bills else Decimal(0),
            # A bakery's week has a shape, and it should never take arithmetic
            # to spot: weekends are coloured in the chart and the day label.
            "weekend": d.weekday() >= 5,
            "payments": payments_map.get(d, []),
        })

    # Each day against the same weekday a week earlier — the only comparison
    # that isn't confounded by Saturday being three times Tuesday.
    by_date = {r["date"]: r["grand_total"] for r in rows}
    for row in rows:
        row["vs_prev_week"] = _pct_change(
            row["grand_total"], by_date.get(row["date"] - timedelta(days=7)),
        )
    return rows


@login_required
def report_daily(request):
    """Daily totals — one row per calendar day in the filter range. Click a
    row to drill down into per-bill detail (`report_daily_detail`)."""
    branches, branch, dfrom, dto = _common_filters(request)
    rows = _report_daily_rows(branch, dfrom, dto)

    gross = sum((r["subtotal"] for r in rows), Decimal(0))
    discount = sum((r["discount"] for r in rows), Decimal(0))
    refunds = sum((r["refunds"] for r in rows), Decimal(0))
    net = sum((r["grand_total"] for r in rows), Decimal(0))
    bills = sum(r["bill_count"] for r in rows)

    # The same window, one window back — so "9.8% up" names what it is up on.
    span = (dto - dfrom).days + 1
    prev_rows = _report_daily_rows(
        branch, dfrom - timedelta(days=span), dfrom - timedelta(days=1),
    )
    prev_net = sum((r["grand_total"] for r in prev_rows), Decimal(0))
    prev_gross = sum((r["subtotal"] for r in prev_rows), Decimal(0))

    # Bars are days, oldest on the left — the table reads newest first, but a
    # chart that ran backwards in time would be unreadable.
    chart_rows = sorted(rows, key=lambda r: r["date"])
    peak = max((r["grand_total"] for r in chart_rows), default=Decimal(0)) or Decimal(1)

    context = {
        "active": "report_daily",
        "page_title": "Sales",
        "branches": branches,
        "branch": branch,
        "date_from": dfrom.isoformat(),
        "date_to": dto.isoformat(),
        "rows": rows,
        "chart_rows": [
            {
                "date": r["date"],
                "weekend": r["weekend"],
                "height": float(r["grand_total"] / peak * 100),
                "total": r["grand_total"],
            }
            for r in chart_rows
        ],
        "gross": gross,
        "discount": discount,
        "discount_share": float(discount / gross * 100) if gross else 0,
        "refunds": refunds,
        "refund_bills": sum(1 for r in rows if r["refunds"]),
        "net": net,
        "bills": bills,
        "vat": sum((r["tax_amount"] for r in rows), Decimal(0)),
        "avg_bill": (net / bills) if bills else Decimal(0),
        "net_delta": _pct_change(net, prev_net),
        "gross_delta": _pct_change(gross, prev_gross),
        "compare_label": f"vs {dfrom - timedelta(days=span):%-d %b} – {dfrom - timedelta(days=1):%-d %b}",
        "span_days": span,
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/report_daily.html", context)


@login_required
def report_daily_export(request):
    """CSV of the daily totals (one row per day) for the current filters."""
    _branches, branch, dfrom, dto = _common_filters(request)
    rows = _report_daily_rows(branch, dfrom, dto)

    fname_branch = branch.name.replace(" ", "_") if branch else "all"
    filename = f"sales_by_date_summary_{fname_branch}_{dfrom.isoformat()}_{dto.isoformat()}.csv"
    response, writer = _csv_response(filename)

    _write_export_header(writer, "Sales report by Date", branch, dfrom, dto)
    writer.writerow([
        "Date", "Bills", "Sub Total", "Discount", "Tax Amount",
        "Service Charge", "Profit", "Grand Total",
    ])

    totals = {k: Decimal(0) for k in
              ("subtotal", "discount", "tax", "service", "profit", "grand")}
    bill_total = 0
    for r in rows:
        writer.writerow([
            r["date"].strftime("%d/%m/%Y"),
            r["bill_count"],
            _csv_num(r["subtotal"]), _csv_num(r["discount"]),
            _csv_num(r["tax_amount"]), _csv_num(r["service_charge"]),
            _csv_num(r["profit"]), _csv_num(r["grand_total"]),
        ])
        totals["subtotal"] += r["subtotal"]
        totals["discount"] += r["discount"]
        totals["tax"] += r["tax_amount"]
        totals["service"] += r["service_charge"]
        totals["profit"] += r["profit"]
        totals["grand"] += r["grand_total"]
        bill_total += r["bill_count"]

    writer.writerow([
        "Total", bill_total,
        _csv_num(totals["subtotal"]), _csv_num(totals["discount"]),
        _csv_num(totals["tax"]), _csv_num(totals["service"]),
        _csv_num(totals["profit"]), _csv_num(totals["grand"]),
    ])
    return response


def _report_daily_detail_rows(branch, day):
    """Per-bill rows for a single day, shared by the detail page and its
    CSV export so both stay in sync."""
    start, end = _date_window(day, day)

    orders = (
        Order.objects.filter(created_at__gte=start, created_at__lte=end)
        .exclude(status="cancel")
        .order_by("created_at")
    )
    if branch:
        orders = orders.filter(branch=branch)

    settings_row = Settings.objects.first()
    tax_percent = settings_row.tax_percent if settings_row else Decimal("7")
    tax_mode = settings_row.tax_mode if settings_row else "exclusive"

    rows = []
    for o in orders:
        sub = o.subtotal or Decimal(0)
        disc = o.discount_amount or Decimal(0)
        taxable = sub - disc
        if tax_mode == "inclusive":
            tax_amount = taxable * tax_percent / (Decimal(100) + tax_percent)
        else:
            tax_amount = taxable * tax_percent / Decimal(100)
        rows.append({
            "order": o,
            "tax_amount": tax_amount,
            "service_charge": Decimal(0),
            "rounding_adj": Decimal(0),
        })
    return rows


@login_required
def report_daily_detail(request, date_str):
    """Per-bill detail for a single day — drill-down from `report_daily`."""
    branches, branch, _, _ = _common_filters(request)
    try:
        day = date.fromisoformat(date_str)
    except ValueError:
        return redirect("backoffice:report_daily")

    rows = _report_daily_detail_rows(branch, day)

    context = {
        "active": "report_daily",
        "branches": branches,
        "branch": branch,
        "day": day,
        "rows": rows,
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/report_daily_detail.html", context)


# Payment-method columns for the daily export. The POS records one method
# per bill as a free-form string (see frontend PAYMENT_METHODS); the first
# five match the SilomPOS report layout, the rest are appended so nothing is
# lumped together. Each tuple is (bucket key, column header).
_PAYMENT_COLUMNS = [
    ("cash", "Cash"),
    ("credit", "Credit"),
    ("promptpay", "Prompt Pay"),
    ("custom", "Custom Pay"),
    ("kbank", "KBank QR Code"),
    ("beam", "Beam"),
    ("easypay", "Easy Pay"),
    ("edc", "EDC"),
]

_PAYMENT_BUCKETS = {
    "cash": "cash",
    # Card rails.  Both providers land in Credit: the column is the payment
    # *instrument*, and a shop reconciling card settlements wants one figure.
    "credit": "credit",
    "credit card": "credit",     # gateways.CARD_METHOD_PREFIX — Omise card link
    "beam card": "credit",       # gateways.BEAM_CARD_METHOD
    # QR rails.
    "promptpay": "promptpay",
    "beam": "beam",
    "beam qr": "beam",           # gateways.BEAM_QR_METHOD — till *and* self-order
    "qr kbank": "kbank",
    "easy pay": "easypay",
    "edc": "edc",
    "custom": "custom",
}


def _payment_bucket(payment_method: str) -> str:
    """Map a stored payment_method string to one of `_PAYMENT_COLUMNS`.

    Methods carry an optional ` · detail` suffix (e.g. 'Credit · VISA',
    'Custom · EDC Kbank') — only the part before the dot decides the column.
    Anything unrecognised falls into Custom Pay.

    The keys must track the strings the POS actually writes, not the tidier
    names in `frontend/lib/payments.ts`.  They drifted once already: the till
    stores 'Beam QR' / 'Beam Card' / 'Credit Card', none of which matched, so
    every QR and card sale — i.e. every non-cash method the payment modal
    exposes — was reported under Custom Pay.
    """
    base = (payment_method or "").split("·")[0].strip().lower()
    return _PAYMENT_BUCKETS.get(base, "custom")


@login_required
def report_daily_detail_export(request, date_str):
    """CSV download of the per-bill detail for a single day, matching the
    SilomPOS 'Sales report by date' layout: a header block (shop, branch,
    date window), one row per bill with the payment-method split plus
    cost/profit, and a totals row. Covers every bill in the date/branch
    window, not just the on-screen page."""
    _branches, branch, _, _ = _common_filters(request)
    try:
        day = date.fromisoformat(date_str)
    except ValueError:
        return redirect("backoffice:report_daily")

    start, _end = _date_window(day, day)
    orders = (
        Order.objects.filter(created_at__gte=start, created_at__lte=_end)
        .exclude(status="cancel")
        .select_related("branch", "customer")
        .prefetch_related("items", "items__product")
        .order_by("created_at")
    )
    if branch:
        orders = orders.filter(branch=branch)

    settings_row = Settings.objects.first()
    tax_percent = settings_row.tax_percent if settings_row else Decimal("7")
    tax_mode = settings_row.tax_mode if settings_row else "exclusive"
    pos_number = settings_row.pos_number if settings_row else ""

    num = _csv_num
    fname_branch = branch.name.replace(" ", "_") if branch else "all"
    response, writer = _csv_response(f"sales_by_date_{fname_branch}_{day.isoformat()}.csv")

    # Sales report by date (Thai title, matching SilomPOS)
    _write_export_header(writer, "รายงานยอดขายสินค้าตามวัน", branch, day, day)

    # ── Column headers ──
    pay_headers = [label for _key, label in _PAYMENT_COLUMNS]
    writer.writerow(
        ["No.", "Date", "Time", "Invoice No", "Net Amount", "Total Discount",
         "Tax", "Rounding Adj.", "Grand Total"]
        + pay_headers
        + ["Cost", "Profit", "Customer", "Staff", "Status", "POS Number"]
    )

    # ── Bill rows ──
    totals = {k: Decimal(0) for k in
              ("net", "discount", "tax", "rounding", "grand", "cost", "profit")}
    totals_pay = {key: Decimal(0) for key, _label in _PAYMENT_COLUMNS}

    for i, o in enumerate(orders, start=1):
        sub = o.subtotal or Decimal(0)
        disc = o.discount_amount or Decimal(0)
        taxable = sub - disc
        if tax_mode == "inclusive":
            tax_amount = taxable * tax_percent / (Decimal(100) + tax_percent)
        else:
            tax_amount = taxable * tax_percent / Decimal(100)
        grand = o.total or Decimal(0)
        rounding_adj = Decimal(0)

        cost = sum(
            ((it.product.cost or Decimal(0)) * (it.qty or 0))
            for it in o.items.all() if it.product_id
        ) or Decimal(0)
        profit = sub - cost

        bucket = _payment_bucket(o.payment_method)
        pay_cells = {key: (grand if key == bucket else Decimal(0))
                     for key, _label in _PAYMENT_COLUMNS}

        customer = o.customer_name or (o.customer.name if o.customer else "")
        created = timezone.localtime(o.created_at)

        writer.writerow(
            [i, created.strftime("%d/%m/%Y"),
             o.created_time or created.strftime("%H:%M"),
             o.order_number, num(sub), num(disc), num(tax_amount),
             num(rounding_adj), num(grand)]
            + [num(pay_cells[key]) for key, _label in _PAYMENT_COLUMNS]
            + [num(cost), num(profit), customer, o.staff,
               "A" if o.status != "cancel" else "Void", pos_number]
        )

        totals["net"] += sub
        totals["discount"] += disc
        totals["tax"] += tax_amount
        totals["rounding"] += rounding_adj
        totals["grand"] += grand
        totals["cost"] += cost
        totals["profit"] += profit
        for key in totals_pay:
            totals_pay[key] += pay_cells[key]

    # ── Totals row ──
    writer.writerow(
        ["", "", "", "", num(totals["net"]), num(totals["discount"]),
         num(totals["tax"]), num(totals["rounding"]), num(totals["grand"])]
        + [num(totals_pay[key]) for key, _label in _PAYMENT_COLUMNS]
        + [num(totals["cost"]), num(totals["profit"]), "", "", "", ""]
    )

    return response


# ─── Output tax report (รายงานภาษีขาย) ──────────────────────────────────
def _report_tax_rows(branch, dfrom, dto):
    """One row per calendar day in the Revenue Department's output-tax-report
    layout: date, abbreviated-tax-invoice number range, sales value ex-VAT and
    the VAT amount.

    Amounts derive from ``total`` — the VAT-inclusive figure the customer
    actually paid and the one the receipt's own VAT line is computed from — so
    the report always reconciles with the issued ใบกำกับภาษีอย่างย่อ
    (including card processing fees, which ``subtotal`` misses).  Voided bills
    are excluded from the money columns but their invoice numbers are listed
    in the remarks column: the PS sequence is continuous, so a cancelled
    invoice must stay visibly accounted for.
    """
    start, end = _date_window(dfrom, dto)

    base = Order.objects.filter(created_at__gte=start, created_at__lte=end)
    if branch:
        base = base.filter(branch=branch)

    settings_row = Settings.objects.first()
    tax_percent = settings_row.tax_percent if settings_row else Decimal("7")

    # min()/max() over order_number is safe because the PS numbers are fixed
    # width ("PS" + zero-padded 9 digits) — string order == numeric order.
    daily = (
        base.exclude(status="cancel")
        .annotate(date_only=TruncDate("created_at"))
        .values("date_only")
        .annotate(
            total=Sum("total"),
            bill_count=Count("id"),
            inv_from=Min("order_number"),
            inv_to=Max("order_number"),
        )
        .order_by("date_only")
    )

    voided_map: dict = {}
    for d, number in (
        base.filter(status="cancel")
        .annotate(date_only=TruncDate("created_at"))
        .values_list("date_only", "order_number")
        .order_by("order_number")
    ):
        voided_map.setdefault(d, []).append(number)

    rows = []
    for entry in daily:
        d = entry["date_only"]
        total = entry["total"] or Decimal(0)
        # ``total`` is VAT-inclusive regardless of tax_mode (an
        # exclusive-mode bill has the VAT added into total at payment time),
        # so the ex-VAT base is always total × 100 / (100 + rate).
        vat = total * tax_percent / (Decimal(100) + tax_percent)
        rows.append({
            "date": d,
            "inv_from": entry["inv_from"],
            "inv_to": entry["inv_to"],
            "bill_count": entry["bill_count"] or 0,
            "value": total - vat,
            "vat": vat,
            "total": total,
            "voided": voided_map.pop(d, []),
        })

    # A day where *every* bill was voided still needs a row, or its invoice
    # numbers would silently vanish from the sequence.
    for d, numbers in voided_map.items():
        rows.append({
            "date": d,
            "inv_from": numbers[0],
            "inv_to": numbers[-1],
            "bill_count": 0,
            "value": Decimal(0),
            "vat": Decimal(0),
            "total": Decimal(0),
            "voided": numbers,
        })

    rows.sort(key=lambda r: r["date"])
    return rows


def _report_tax_header(branch):
    """Taxpayer identity block the RD format requires above the table.
    Branch-level tax_id overrides the shop-wide Settings value; pos_id is
    branch-only and has no shop-wide fallback, because falling back would put
    one branch's RD machine number on another branch's report."""
    settings_row = Settings.objects.first()
    return {
        "company_name": (
            (settings_row.company_name or settings_row.shop_name)
            if settings_row else ""
        ),
        "tax_id": (
            (branch.tax_id if branch and branch.tax_id else None)
            or (settings_row.tax_id if settings_row else "")
        ),
        "pos_id": (branch.pos_id if branch else "") or "",
        "tax_percent": settings_row.tax_percent if settings_row else Decimal("7"),
    }


def _report_tax_totals(rows):
    totals = {"value": Decimal(0), "vat": Decimal(0), "total": Decimal(0), "bills": 0}
    for r in rows:
        totals["value"] += r["value"]
        totals["vat"] += r["vat"]
        totals["total"] += r["total"]
        totals["bills"] += r["bill_count"]
    return totals


@login_required
def report_tax(request):
    """รายงานภาษีขาย — the output tax report in the Director-General's
    prescribed layout, one row per day of abbreviated tax invoices."""
    branches, branch, dfrom, dto = _common_filters(request)
    rows = _report_tax_rows(branch, dfrom, dto)
    totals = _report_tax_totals(rows)

    # The receipt sequence audit is the quiet centrepiece: "1,392 issued,
    # 1,392 accounted for" is what a revenue officer asks first, and it should
    # be answerable before anyone asks. Voided bills keep their numbers, so a
    # cancelled invoice stays visibly accounted for rather than leaving a gap.
    voided = sum(len(r["voided"]) for r in rows)
    issued = totals["bills"] + voided

    # This window split by branch, so a filing pack can be reconciled shop by
    # shop without changing the picker and losing the total.
    start, end = _date_window(dfrom, dto)
    tax_percent = _report_tax_header(branch)["tax_percent"]
    by_branch = []
    for entry in (Order.objects.filter(created_at__range=(start, end))
                  .exclude(status="cancel")
                  .values("branch__id", "branch__name")
                  .annotate(receipts=Count("id"), total=Sum("total"))
                  .order_by("-total")):
        total = entry["total"] or Decimal(0)
        by_branch.append({
            "id": entry["branch__id"],
            "name": entry["branch__name"] or "Unassigned",
            "receipts": entry["receipts"] or 0,
            "total": total,
            "vat": total * tax_percent / (Decimal(100) + tax_percent),
        })

    context = {
        "active": "report_tax",
        "page_title": "Tax & VAT",
        "branches": branches,
        "branch": branch,
        "date_from": dfrom.isoformat(),
        "date_to": dto.isoformat(),
        "rows": rows,
        "totals": totals,
        "header": _report_tax_header(branch),
        "voided_count": voided,
        "issued_count": issued,
        "by_branch": by_branch,
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/report_tax.html", context)


@login_required
def report_tax_export(request):
    """CSV of the output tax report for the current filters."""
    _branches, branch, dfrom, dto = _common_filters(request)
    rows = _report_tax_rows(branch, dfrom, dto)
    header = _report_tax_header(branch)

    fname_branch = branch.name.replace(" ", "_") if branch else "all"
    filename = f"tax_report_{fname_branch}_{dfrom.isoformat()}_{dto.isoformat()}.csv"
    response, writer = _csv_response(filename)

    _write_export_header(writer, "รายงานภาษีขาย (Output Tax Report)", branch, dfrom, dto)
    writer.writerow(["ชื่อผู้ประกอบการ", header["company_name"]])
    writer.writerow(["เลขประจำตัวผู้เสียภาษี", header["tax_id"]])
    writer.writerow(["เลขรหัสประจำเครื่อง (POS ID)", header["pos_id"]])
    writer.writerow([])
    writer.writerow([
        "วัน เดือน ปี", "เลขที่ใบกำกับภาษี (จาก)", "เลขที่ใบกำกับภาษี (ถึง)",
        "จำนวนฉบับ", "มูลค่าสินค้า/บริการ", "จำนวนเงินภาษีมูลค่าเพิ่ม",
        "รวม", "หมายเหตุ",
    ])

    for r in rows:
        remark = (
            "ยกเลิก: " + ", ".join(r["voided"]) if r["voided"] else ""
        )
        writer.writerow([
            r["date"].strftime("%d/%m/%Y"),
            r["inv_from"], r["inv_to"], r["bill_count"],
            _csv_num(r["value"]), _csv_num(r["vat"]), _csv_num(r["total"]),
            remark,
        ])

    totals = _report_tax_totals(rows)
    writer.writerow([
        "รวมทั้งสิ้น", "", "", totals["bills"],
        _csv_num(totals["value"]), _csv_num(totals["vat"]),
        _csv_num(totals["total"]), "",
    ])
    return response


# ─── Sales report by Bill Detail ────────────────────────────────────────
def _report_sell_items(branch, dfrom, dto):
    """Line items across all bills in the range — shared by the page (which
    paginates it) and the CSV export (which streams every row)."""
    start, end = _date_window(dfrom, dto)
    items = (
        OrderItem.objects.filter(
            order__created_at__gte=start,
            order__created_at__lte=end,
        )
        .exclude(order__status="cancel")
        .select_related("order", "product")
        .order_by("-order__created_at")
    )
    if branch:
        items = items.filter(order__branch=branch)
    return items


def _sell_row(it):
    """Derived columns for a single line item, shared by page and export."""
    line_sub = (it.price or Decimal(0)) * (it.qty or 0)
    # The POS only has per-line discounts (a bill's discount_amount is their
    # sum), and it clamps each one to its line total — mirror that here so a
    # stale/oversized value can't push the line negative.
    disc = min(it.discount or Decimal(0), line_sub)
    return {
        "item": it,
        "barcode": it.product.barcode if it.product_id else "",
        "add_on_total": Decimal(0),
        "discount": disc,
        "sub_total": line_sub,
        "total": line_sub - disc,
    }


@login_required
def report_sell(request):
    """One row per line item across all bills in the range."""
    branches, branch, dfrom, dto = _common_filters(request)
    items = _report_sell_items(branch, dfrom, dto)

    paginator = Paginator(items, 50)
    page_obj = paginator.get_page(request.GET.get("page"))

    rows = [_sell_row(it) for it in page_obj.object_list]

    context = {
        "active": "report_sell",
        "branches": branches,
        "branch": branch,
        "date_from": dfrom.isoformat(),
        "date_to": dto.isoformat(),
        "rows": rows,
        "page_obj": page_obj,
        "paginator": paginator,
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/report_sell.html", context)


@login_required
def report_sell_export(request):
    """CSV of every bill line item in the range (not just the visible page)."""
    _branches, branch, dfrom, dto = _common_filters(request)
    items = _report_sell_items(branch, dfrom, dto)

    fname_branch = branch.name.replace(" ", "_") if branch else "all"
    filename = f"sales_by_bill_detail_{fname_branch}_{dfrom.isoformat()}_{dto.isoformat()}.csv"
    response, writer = _csv_response(filename)

    _write_export_header(writer, "Sales report by Bill Detail", branch, dfrom, dto)
    writer.writerow([
        "Date", "Receipt No.", "Barcode", "Product Name", "Quantity",
        "Price / Unit", "Add-on Total", "Sub Total", "Discount", "Total",
        # Promotion audit columns, after the SilomPOS ones so those stay put.
        "SKU", "Item Type", "Promotion ID", "Promotion", "Promotion Logic",
        "Discount Reason",
    ])

    qty_total = 0
    money_totals = {k: Decimal(0) for k in ("addon", "sub", "discount", "total")}
    for it in items.iterator():
        row = _sell_row(it)
        writer.writerow([
            timezone.localtime(it.order.created_at).strftime("%d/%m/%Y %H:%M:%S"),
            it.order.order_number,
            row["barcode"] or "-",
            it.name,
            it.qty or 0,
            _csv_num(it.price),
            _csv_num(row["add_on_total"]),
            _csv_num(row["sub_total"]),
            _csv_num(row["discount"]),
            _csv_num(row["total"]),
            it.sku or "",
            "Free" if it.is_free else "Purchased",
            it.discount_code or "",
            it.discount_label or "",
            it.discount_logic or "",
            it.discount_reason or "",
        ])
        qty_total += it.qty or 0
        money_totals["addon"] += row["add_on_total"]
        money_totals["sub"] += row["sub_total"]
        money_totals["discount"] += row["discount"]
        money_totals["total"] += row["total"]

    writer.writerow([
        "Total", "", "", "", qty_total, "",
        _csv_num(money_totals["addon"]), _csv_num(money_totals["sub"]),
        _csv_num(money_totals["discount"]), _csv_num(money_totals["total"]),
    ])
    return response


# ─── Sales report by Product (SKU) ──────────────────────────────────────
def _report_sku_rows(branch, dfrom, dto):
    """Per-product aggregation over the range — shared by the page (which
    paginates) and the CSV export (which writes every product)."""
    start, end = _date_window(dfrom, dto)

    items = OrderItem.objects.filter(
        order__created_at__gte=start,
        order__created_at__lte=end,
    ).exclude(order__status="cancel")
    if branch:
        items = items.filter(order__branch=branch)

    # Group by product_id so renamed/identical names still merge correctly.
    agg = (
        items.values("product_id")
        .annotate(
            quantity=Sum("qty"),
            sales=Sum(F("price") * F("qty")),
            profit=Sum(_profit_expr()),
        )
        .order_by("-sales")
    )

    product_ids = [r["product_id"] for r in agg if r["product_id"]]
    products = {p.id: p for p in Product.objects.filter(id__in=product_ids).select_related("category")}

    rows = []
    for r in agg:
        p = products.get(r["product_id"])
        rows.append({
            "barcode": (p.barcode if p else "") or "-",
            "name": (p.name if p else "(deleted product)"),
            "category": (p.category.name if p and p.category else ""),
            "quantity": r["quantity"] or 0,
            "balance": (p.stock if p else 0),
            "sales": r["sales"] or Decimal(0),
            "cost": ((p.cost if p else Decimal(0)) * (r["quantity"] or 0)),
            "profit": r["profit"] or Decimal(0),
        })
    return rows


@login_required
def report_sku(request):
    """Aggregated per product over the date range. Joined to current Product
    row to get barcode, category and current stock balance."""
    branches, branch, dfrom, dto = _common_filters(request)
    rows = _report_sku_rows(branch, dfrom, dto)

    query = (request.GET.get("q") or "").strip()
    if query:
        needle = query.lower()
        rows = [r for r in rows
                if needle in r["name"].lower() or needle in (r["barcode"] or "").lower()]

    units = sum(r["quantity"] for r in rows)
    revenue = sum((r["sales"] for r in rows), Decimal(0))
    cost = sum((r["cost"] for r in rows), Decimal(0))
    profit = sum((r["profit"] for r in rows), Decimal(0))
    peak = max((r["sales"] for r in rows), default=Decimal(0)) or Decimal(1)

    for index, row in enumerate(rows, start=1):
        row["rank"] = index
        row["share"] = float(row["sales"] / revenue * 100) if revenue else 0
        row["bar"] = float(row["sales"] / peak * 100)
        row["margin"] = float(row["profit"] / row["sales"] * 100) if row["sales"] else 0

    # Revenue by category, for the mix panel. Grouped here rather than in a
    # second query because the per-product rows already carry the category.
    by_category: dict = {}
    for row in rows:
        name = row["category"] or "Uncategorised"
        by_category[name] = by_category.get(name, Decimal(0)) + row["sales"]
    categories = sorted(
        (
            {"name": name, "sales": total,
             "pct": float(total / revenue * 100) if revenue else 0}
            for name, total in by_category.items()
        ),
        key=lambda c: c["sales"], reverse=True,
    )[:6]

    # Products with a catalogue row but no sale in the window. A zero is not
    # in the aggregation at all, so it has to be asked for separately — and
    # it's the fact this page is least able to show without asking.
    sold_names = {r["name"] for r in rows}
    unsold = Product.objects.filter(active=True)
    if branch:
        unsold = unsold.filter(branch=branch)
    unsold_count = unsold.exclude(name__in=sold_names).count()

    paginator = Paginator(rows, 50)
    page_obj = paginator.get_page(request.GET.get("page"))

    best = rows[0] if rows else None

    context = {
        "active": "report_sku",
        "page_title": "Product performance",
        "branches": branches,
        "branch": branch,
        "date_from": dfrom.isoformat(),
        "date_to": dto.isoformat(),
        "rows": page_obj.object_list,
        "page_obj": page_obj,
        "paginator": paginator,
        "query": query,
        "units": units,
        "revenue": revenue,
        "cost": cost,
        "profit": profit,
        "margin": float(profit / revenue * 100) if revenue else 0,
        "categories": categories,
        "best": best,
        "unsold_count": unsold_count,
        "product_count": len(rows),
        "qs": _filter_qs(request, q=query or None),
    }
    return render(request, "backoffice/report_sku.html", context)


@login_required
def report_sku_export(request):
    """CSV of the per-product sales aggregation for the current filters."""
    _branches, branch, dfrom, dto = _common_filters(request)
    rows = _report_sku_rows(branch, dfrom, dto)

    fname_branch = branch.name.replace(" ", "_") if branch else "all"
    filename = f"sales_by_product_{fname_branch}_{dfrom.isoformat()}_{dto.isoformat()}.csv"
    response, writer = _csv_response(filename)

    _write_export_header(writer, "Sales report by Product", branch, dfrom, dto)
    writer.writerow([
        "#", "Barcode", "Product Name", "Category", "Quantity",
        "Balance", "Sales", "Cost", "Profit",
    ])

    qty_total = 0
    totals = {k: Decimal(0) for k in ("sales", "cost", "profit")}
    for i, r in enumerate(rows, start=1):
        writer.writerow([
            i, r["barcode"], r["name"], r["category"],
            r["quantity"], r["balance"],
            _csv_num(r["sales"]), _csv_num(r["cost"]), _csv_num(r["profit"]),
        ])
        qty_total += r["quantity"]
        totals["sales"] += r["sales"]
        totals["cost"] += r["cost"]
        totals["profit"] += r["profit"]

    writer.writerow([
        "Total", "", "", "", qty_total, "",
        _csv_num(totals["sales"]), _csv_num(totals["cost"]), _csv_num(totals["profit"]),
    ])
    return response


# ─── Inventory Summary ──────────────────────────────────────────────────
def _inventory_qs(request):
    """Filtered/sorted product queryset shared by the page and the CSV
    export so both honour the same branch / search / sort selection.

    Search mirrors SilomPOS: a ``field`` selector chooses which column the
    free-text ``q`` matches against (All Product searches name + barcode +
    category at once)."""
    branches, branch, _, _ = _common_filters(request)

    # Several branches can be ticked at once (``?branches=<id>&branches=<id>``).
    # With none ticked the page shows the header's branch, as every other
    # page does. Ticking doesn't move the remembered branch: the other pages
    # can only show one, so they keep the one you last picked there.
    by_id = {str(b.id): b for b in branches}
    selected = [by_id[i] for i in dict.fromkeys(request.GET.getlist("branches")) if i in by_id]
    if not selected and branch:
        selected = [branch]

    qs = Product.objects.filter(active=True).select_related("category", "branch")
    if selected:
        qs = qs.filter(branch__in=selected)

    field = request.GET.get("field", "all")
    q = (request.GET.get("q") or "").strip()
    if q:
        if field == "name":
            qs = qs.filter(name__icontains=q)
        elif field == "barcode":
            qs = qs.filter(barcode__icontains=q)
        elif field == "category":
            qs = qs.filter(category__name__icontains=q)
        else:  # all
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(barcode__icontains=q)
                | Q(category__name__icontains=q)
            )

    sort = request.GET.get("sort", "name")
    sort_map = {
        "name": "name",
        "barcode": "barcode",
        "category": "category__name",
        "stock_min": "stock",   # OnhandQty → lowest on-hand first
        "stock_max": "-stock",  # OnhandQty → highest on-hand first
    }
    qs = qs.order_by(sort_map.get(sort, "name"), "branch__name")
    return branches, selected, field, q, sort, qs


def _set_stock_level(p):
    """Level bar, colour and status tag for one row of the on-hand table."""
    if p.stock <= 0:
        p.level_label, p.level_class, p.level_colour, p.fill = "Out", "t-red", "var(--red)", 0
    elif p.par_level and p.stock < p.par_level:
        p.level_label, p.level_class, p.level_colour = "Low", "t-low", "var(--amber)"
        p.fill = round(p.stock * 100 / p.par_level)
    elif p.par_level:
        p.level_label, p.level_class, p.level_colour = "Good", "t-ok", "var(--green)"
        p.fill = min(100, round(p.stock * 100 / p.par_level))
    else:
        # No par level set, so there is no "enough" to compare against.
        # Saying so beats inventing a threshold and flagging on it.
        p.level_label, p.level_class, p.level_colour, p.fill = "Untracked", "t-out", "var(--mut2)", 0


def _week_sold_and_received(products, as_of=None):
    """Units sold in the seven days up to ``as_of`` (default today) and the
    last stock-in on or before it, by product id."""
    end_day = as_of or timezone.localdate()
    week_start, week_end = _date_window(end_day - timedelta(days=6), end_day)
    sold = {
        row["product_id"]: row["qty"] or 0
        for row in OrderItem.objects.filter(
            product__in=products,
            order__created_at__range=(week_start, week_end),
        ).exclude(order__status="cancel").values("product_id").annotate(qty=Sum("qty"))
    }
    received = {
        row["product_id"]: row["last"]
        for row in StockMovement.objects.filter(
            product__in=products, type="in", created_at__lte=week_end)
        .values("product_id").annotate(last=Max("created_at"))
    }
    return sold, received


def _inventory_as_of(request):
    """The ``?as_of=`` date, or None for "now" (blank, unparseable, today or
    later). Clamped to ``_as_of_floor`` so the page never shows a rewind it
    can't do."""
    today = timezone.localdate()
    as_of = _parse_date(request.GET.get("as_of"), None)
    if as_of is None or as_of >= today:
        return None
    floor = _as_of_floor()
    if floor is not None and as_of < floor:
        as_of = floor
    return as_of if as_of < today else None


def _as_of_floor():
    """The earliest day stock can be rewound to: the day of the first audited
    Product change. Edits from the product forms, the till's product save and
    catalogue sync overwrite ``stock`` without a StockMovement; the audit log
    is their only record, and it didn't exist before this."""
    first = (AuditLog.objects.filter(model="Product")
             .order_by("at").values_list("at", flat=True).first())
    return timezone.localtime(first).date() if first else None


def _rewind_stock(products, as_of):
    """Set each product's ``stock`` to what it was at the end of ``as_of``,
    working back from today's figure, and drop products that didn't exist yet.

    Everything that changed stock since then is undone:

    * sales — ``create_order_from_items`` decrements with a queryset
      ``update()`` (no audit row), so they're read from the StockMovement it
      writes alongside, recognised by ``document_no`` being an order number.
      Voids don't give stock back, so voided sales count too;
    * every other change — stock documents, the app's stock modal, product
      edits from the till or the backoffice, catalogue sync — goes through
      ``Product.save()``, whose audit row carries ``stock`` from/to. That
      covers absolute overwrites a StockMovement can't express.
    """
    if not products:
        return products
    _, cutoff = _date_window(as_of, as_of)
    branch_ids = {p.branch_id for p in products}
    by_id = {str(p.id): p for p in products}
    change = {}          # product id -> net stock change after the cutoff
    born_later = set()

    for oid, action, changes in (
        AuditLog.objects.filter(model="Product", branch_id__in=branch_ids,
                                at__gt=cutoff, action__in=("create", "update"))
        .values_list("object_id", "action", "changes")
    ):
        if oid not in by_id:
            continue
        if action == "create":
            born_later.add(oid)
            continue
        stock = (changes or {}).get("stock")
        if isinstance(stock, dict):
            change[oid] = (change.get(oid, 0)
                           + int(stock.get("to") or 0) - int(stock.get("from") or 0))

    sales = (
        StockMovement.objects
        .filter(product__in=products, type="out", created_at__gt=cutoff)
        .filter(Exists(Order.objects.filter(order_number=OuterRef("document_no"))))
        .values("product_id").annotate(qty=Sum("qty"))
    )
    for row in sales:
        oid = str(row["product_id"])
        change[oid] = change.get(oid, 0) - (row["qty"] or 0)

    kept = []
    for p in products:
        oid = str(p.id)
        if oid in born_later:
            continue
        p.stock = p.stock - change.get(oid, 0)
        kept.append(p)
    return kept


def _inventory_grouped(qs, sort, as_of=None):
    """Several branches' products merged by name into one row each.

    Each branch has its own Product row, so "Hella Nutella" at three branches
    is three rows. Matched on the name, trimmed and ignoring case, which is
    how catalogue sync decides two branches carry the same product. On hand,
    par level, sales and stock value add up; ``by_branch`` keeps the split.
    """
    products = list(qs)
    if as_of:
        # Before grouping, so the merged row sums the rewound figures.
        products = _rewind_stock(products, as_of)
    sold, received = _week_sold_and_received(products, as_of)

    groups: dict = {}
    for p in products:
        key = (p.name or "").strip().lower()
        g = groups.get(key)
        if g is None:
            g = groups[key] = SimpleNamespace(
                id=None, name=p.name, name_th=p.name_th, category=p.category,
                sku=p.sku, barcode=p.barcode, members=[], costs=set(),
                stock=0, par_level=0, sold_7d=0, value=Decimal(0),
                last_received=None, by_branch=[],
            )
        g.members.append(p)
        g.costs.add(p.cost)
        g.sku = g.sku or p.sku
        g.barcode = g.barcode or p.barcode
        g.name_th = g.name_th or p.name_th
        g.category = g.category or p.category
        g.stock += p.stock
        g.par_level += p.par_level or 0
        g.sold_7d += sold.get(p.id, 0)
        g.value += p.cost * p.stock
        last = received.get(p.id)
        if last and (g.last_received is None or last > g.last_received):
            g.last_received = last
        g.by_branch.append((p.branch.name if p.branch_id else "", p.stock))

    rows = list(groups.values())
    for g in rows:
        # One product row can still be opened; a merged one has no single page.
        g.id = g.members[0].id if len(g.members) == 1 else None
        # Branches can price the same product differently. A made-up average
        # would read as a real cost, so a mixed row says so instead.
        g.cost = next(iter(g.costs)) if len(g.costs) == 1 else None
        daily = g.sold_7d / 7 if g.sold_7d else 0
        g.days_cover = (g.stock / daily) if daily else None
        _set_stock_level(g)

    _sort_rows(rows, sort)
    return rows


def _sort_rows(rows, sort):
    """The page's sort options, for rows built in Python rather than SQL."""
    keys = {
        "barcode": lambda g: (g.barcode or "").lower(),
        "category": lambda g: (g.category.name if g.category else "").lower(),
        "stock_min": lambda g: g.stock,
        "stock_max": lambda g: -g.stock,
    }
    rows.sort(key=lambda g: (g.name or "").lower())
    if sort in keys:
        rows.sort(key=keys[sort])


def _inventory_as_of_rows(qs, sort, as_of):
    """One branch's products with stock rewound to ``as_of``. The stock
    filters, totals and sort can't run in SQL on a figure that isn't in the
    table, so the rows are built here like the merged ones."""
    rows = _rewind_stock(list(qs), as_of)
    sold, received = _week_sold_and_received(rows, as_of)
    for p in rows:
        p.sold_7d = sold.get(p.id, 0)
        daily = p.sold_7d / 7 if p.sold_7d else 0
        p.days_cover = (p.stock / daily) if daily else None
        p.value = p.cost * p.stock
        p.last_received = received.get(p.id)
        _set_stock_level(p)
    _sort_rows(rows, sort)
    return rows


@login_required
def inventory_summary(request):
    """On-hand balance per product, now or (``?as_of=``) at the end of a past
    day. Searchable by Name / Barcode / Category and sortable by Name /
    Barcode / Category / OnhandQty (matching the SilomPOS options)."""
    branches, selected, field, q, sort, qs = _inventory_qs(request)
    multi_branch = len(selected) > 1
    as_of = _inventory_as_of(request)

    # "Needs attention" first, because that is what the page is opened for.
    # `all` is a click away, and the tab says which one you're looking at.
    level = request.GET.get("level") or "attention"

    def wanted(p):
        if level == "out":
            return p.stock <= 0
        if level == "low":
            return p.stock > 0 and p.par_level > 0 and p.stock < p.par_level
        if level == "attention":
            return p.stock <= 0 or (p.par_level > 0 and p.stock < p.par_level)
        return True

    if multi_branch or as_of:
        # Merged or rewound rows are built in Python, so the level filter, the
        # cards and the paging all work on those rows rather than the queryset.
        all_rows = (_inventory_grouped(qs, sort, as_of) if multi_branch
                    else _inventory_as_of_rows(qs, sort, as_of))
        paginator = Paginator([g for g in all_rows if wanted(g)], 50)
        page_obj = paginator.get_page(request.GET.get("page"))
        products = list(page_obj.object_list)
        totals = {"skus": len(all_rows), "value": sum((g.value for g in all_rows), Decimal(0))}
        out_of_stock = sum(1 for g in all_rows if g.stock <= 0)
        below_par = sum(1 for g in all_rows if 0 < g.stock < g.par_level)
        untracked = sum(1 for g in all_rows if g.par_level <= 0)
    else:
        all_products = qs
        if level == "out":
            qs = qs.filter(stock__lte=0)
        elif level == "low":
            qs = qs.filter(stock__gt=0, par_level__gt=0, stock__lt=F("par_level"))
        elif level == "attention":
            qs = qs.filter(Q(stock__lte=0)
                           | Q(par_level__gt=0, stock__lt=F("par_level")))

        # Whole-branch figures, not the filtered page: "4 out of stock" must not
        # become "4 of 4" because you're standing on the out-of-stock tab.
        totals = all_products.aggregate(
            skus=Count("id"), value=Sum(F("cost") * F("stock")),
        )
        out_of_stock = all_products.filter(stock__lte=0).count()
        below_par = all_products.filter(
            stock__gt=0, par_level__gt=0, stock__lt=F("par_level")).count()
        untracked = all_products.filter(par_level__lte=0).count()

        paginator = Paginator(qs, 50)
        page_obj = paginator.get_page(request.GET.get("page"))
        products = list(page_obj.object_list)

        # Days cover is the column that decides anything: "4 on hand" is
        # meaningless without the sell-through rate beside it. Measured over the
        # last seven days, so a weekend spike doesn't dominate a single day.
        sold, received = _week_sold_and_received(products)
        for p in products:
            p.sold_7d = sold.get(p.id, 0)
            daily = p.sold_7d / 7 if p.sold_7d else 0
            p.days_cover = (p.stock / daily) if daily else None
            p.value = p.cost * p.stock
            p.last_received = received.get(p.id)
            _set_stock_level(p)

    context = {
        "active": "inventory",
        "page_title": "Inventory",
        "branches": branches,
        # The stock-movement export is one branch's file, so it takes the
        # first of the ticked ones.
        "branch": selected[0] if selected else None,
        "selected": selected,
        "selected_ids": {str(b.id) for b in selected},
        "multi_branch": multi_branch,
        "as_of": as_of,
        "as_of_min": _as_of_floor(),
        "hide_branch": True,
        "sorts": [("name", "Name"), ("barcode", "Barcode"), ("category", "Category"),
                  ("stock_min", "On hand: low → high"), ("stock_max", "On hand: high → low")],
        "products": products,
        "page_obj": page_obj,
        "paginator": paginator,
        "field": field,
        "q": q,
        "sort": sort,
        "level": level,
        "levels": [("attention", "Needs attention"), ("out", "Out of stock"),
                   ("low", "Below par"), ("all", "All")],
        "sku_count": totals["skus"] or 0,
        "stock_value": totals["value"] or Decimal(0),
        "out_of_stock": out_of_stock,
        "below_par": below_par,
        "untracked": untracked,
        "qs": "&".join(filter(None, [
            urlencode([("branches", b.id) for b in selected]),
            _filter_qs(
                request,
                sort=sort if sort != "name" else None,
                field=field if field != "all" else None,
                q=q or None,
                level=level if level != "attention" else None,
                as_of=as_of.isoformat() if as_of else None,
            ),
        ])),
        "hide_dates": True,
        "today": timezone.localdate(),
    }
    return render(request, "backoffice/inventory.html", context)


@login_required
def inventory_export(request):
    """CSV download of the inventory summary for the current branch / search /
    sort selection. Covers every matching product, not just the visible page."""
    _branches, selected, _field, _q, _sort, qs = _inventory_qs(request)
    multi = len(selected) > 1
    as_of = _inventory_as_of(request)

    settings_row = Settings.objects.first()
    shop_name = settings_row.shop_name if settings_row else "Brave POS"
    branch_name = ", ".join(b.name for b in selected) if selected else "All branches"
    today = as_of or timezone.localdate()

    if len(selected) == 1:
        fname_branch = selected[0].name.replace(" ", "_")
    else:
        fname_branch = f"{len(selected)}_branches" if selected else "all"
    filename = f"inventory_{fname_branch}_{today.isoformat()}.csv"

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("﻿")  # BOM so Excel reads UTF-8 (Thai names) correctly

    writer = csv.writer(response)
    writer.writerow(["Inventory Report"])
    writer.writerow(["Shop Name", shop_name])
    writer.writerow(["Branch", branch_name])
    writer.writerow(["Date", today.strftime("%d %B %Y")])
    writer.writerow([])
    if multi:
        # Same merge as the page: one row per product name, stock summed,
        # and the per-branch split spelled out beside it.
        writer.writerow(["No.", "Barcode", "Product Name", "Unit", "Category",
                         "Balance", "By branch"])
        for no, g in enumerate(_inventory_grouped(qs, _sort, as_of), start=1):
            writer.writerow([
                no, g.barcode or "", g.name, "ชิ้น",
                g.category.name if g.category else "", g.stock,
                "; ".join(f"{name} {stock}" for name, stock in g.by_branch),
            ])
        return response

    writer.writerow(["No.", "Barcode", "Product Name", "Unit", "Category", "Balance"])
    rows = _inventory_as_of_rows(qs, _sort, as_of) if as_of else qs.iterator()
    for no, p in enumerate(rows, start=1):
        balance = "non-stock" if p.product_type == "S" else p.stock
        writer.writerow([
            no,
            p.barcode or "",
            p.name,
            "ชิ้น",
            p.category.name if p.category_id else "",
            balance,
        ])

    return response


def _stock_qty(v) -> str:
    """Quantities print as integers when whole (3, not 3.00) to match the
    SilomPOS files; fractional units still show their decimals."""
    d = Decimal(v or 0)
    return str(d.quantize(Decimal(1))) if d == d.to_integral_value() else f"{d:.2f}"


# ─── Stock-in / stock-out reports ───────────────────────────────────────
# One page per direction, each with two views of the same documents:
#   documents — one row per saved document, the SilomPOS "Stock-In Documents"
#               list;
#   products  — one row per product, summed across those documents, the
#               SilomPOS "Stock in by Product" report.
_STOCK_REPORT_KINDS = {
    "in": {"title": "Stock in", "party": "Supplier", "active": "stock_in"},
    "out": {"title": "Stock out", "party": "Receiver", "active": "stock_out"},
}

_STOCK_DOC_FIELDS = [
    ("all", "All fields"), ("document_no", "Document no."),
    ("ref_no", "Ref. no."), ("party", "Supplier / receiver"),
    ("created_by", "Created by"),
]
_STOCK_PRODUCT_SORTS = [
    ("name", "Name"), ("newest", "Newest"),
    ("price", "Product price"), ("onhand", "On hand"),
]


def _stock_report_filters(request):
    branches, branch, dfrom, dto = _common_filters(request)
    view = request.GET.get("view") if request.GET.get("view") in ("documents", "products") else "documents"
    field = request.GET.get("field") or "all"
    if field not in dict(_STOCK_DOC_FIELDS):
        field = "all"
    sort = request.GET.get("sort") or "name"
    if sort not in dict(_STOCK_PRODUCT_SORTS):
        sort = "name"
    q = (request.GET.get("q") or "").strip()
    return branches, branch, dfrom, dto, view, field, sort, q


def _stock_report_docs(branch, kind, dfrom, dto, field, q):
    """Documents of one direction in the window, newest first."""
    start, end = _date_window(dfrom, dto)
    qs = (StockDocument.objects
          .filter(type=kind, created_at__gte=start, created_at__lte=end)
          .annotate(line_count=Count("items"))
          .order_by("-created_at"))
    if branch:
        qs = qs.filter(branch=branch)
    if q:
        party = Q(vendor__icontains=q) if kind == "in" else Q(receiver__icontains=q)
        match = {
            "document_no": Q(document_no__icontains=q),
            "ref_no": Q(ref_no__icontains=q),
            "party": party,
            "created_by": Q(created_by__icontains=q),
        }
        qs = qs.filter(match.get(field) or (
            Q(document_no__icontains=q) | Q(ref_no__icontains=q)
            | party | Q(created_by__icontains=q) | Q(note__icontains=q)
            | Q(reason__icontains=q)
        ))
    return qs


def _stock_report_products(branch, kind, dfrom, dto, sort, q):
    """Document lines summed per product.

    Grouped in Python, like ``stock_movement_export``: a line whose product
    was deleted still has its name and barcode snapshotted, and must keep
    being reported rather than vanish from the totals.
    """
    items = (StockDocumentItem.objects
             .filter(document__in=_stock_report_docs(branch, kind, dfrom, dto, "all", ""))
             .select_related("document", "product__unit", "product__category"))

    rows: dict[object, dict] = {}
    for it in items:
        p = it.product
        key = it.product_id or (it.barcode, it.product_name)
        row = rows.get(key)
        if row is None:
            row = rows[key] = {
                "barcode": it.barcode or (p.barcode if p else ""),
                "name": it.product_name or (p.name if p else ""),
                "unit": p.unit.name if p and p.unit_id else "",
                "category": p.category.name if p and p.category_id else "",
                "price": p.price if p else None,
                "onhand": p.stock if p else None,
                "docs": set(), "qty": Decimal(0),
                "discount": Decimal(0), "total": Decimal(0),
                "last": it.document.created_at,
            }
        row["docs"].add(it.document_id)
        row["qty"] += it.qty or 0
        row["discount"] += it.discount or 0
        row["total"] += it.total or 0
        row["last"] = max(row["last"], it.document.created_at)

    out = list(rows.values())
    for row in out:
        row["doc_count"] = len(row.pop("docs"))

    if q:
        needle = q.lower()
        out = [r for r in out if needle in r["name"].lower()
               or needle in r["barcode"].lower() or needle in r["category"].lower()]

    out.sort(key=lambda r: r["name"].lower())
    if sort == "newest":
        out.sort(key=lambda r: r["last"], reverse=True)
    elif sort == "price":
        out.sort(key=lambda r: r["price"] if r["price"] is not None else Decimal(-1), reverse=True)
    elif sort == "onhand":
        # Lowest first — the reason to sort by on-hand is to find what's short.
        out.sort(key=lambda r: r["onhand"] if r["onhand"] is not None else 10 ** 9)
    return out


def _stock_report(request, kind):
    conf = _STOCK_REPORT_KINDS[kind]
    branches, branch, dfrom, dto, view, field, sort, q = _stock_report_filters(request)

    docs = _stock_report_docs(branch, kind, dfrom, dto, field if view == "documents" else "all",
                              q if view == "documents" else "")
    totals = docs.aggregate(count=Count("id"), value=Sum("total"))
    context = {
        "active": conf["active"],
        "kind": kind,
        "title": conf["title"],
        "party_label": conf["party"],
        "branches": branches,
        "branch": branch,
        "date_from": dfrom.isoformat(),
        "date_to": dto.isoformat(),
        "view": view,
        "field": field,
        "fields": _STOCK_DOC_FIELDS,
        "sort": sort,
        "sorts": _STOCK_PRODUCT_SORTS,
        "q": q,
        "doc_count": totals["count"] or 0,
        "doc_value": totals["value"] or Decimal(0),
        "export_url": reverse(f"backoffice:{conf['active']}_export"),
        "qs": _filter_qs(
            request, view=view, q=q or None,
            field=field if view == "documents" and field != "all" else None,
            sort=sort if view == "products" and sort != "name" else None,
        ),
    }

    if view == "documents":
        paginator = Paginator(docs, 50)
    else:
        rows = _stock_report_products(branch, kind, dfrom, dto, sort, q)
        context.update(
            product_count=len(rows),
            qty_total=sum((r["qty"] for r in rows), Decimal(0)),
            discount_total=sum((r["discount"] for r in rows), Decimal(0)),
            value_total=sum((r["total"] for r in rows), Decimal(0)),
        )
        paginator = Paginator(rows, 50)
    page_obj = paginator.get_page(request.GET.get("page"))
    context.update(rows=page_obj.object_list, page_obj=page_obj, paginator=paginator)
    return render(request, "backoffice/stock_report.html", context)


def _stock_report_export(request, kind):
    """CSV of whichever view is on screen, every row rather than one page."""
    conf = _STOCK_REPORT_KINDS[kind]
    _branches, branch, dfrom, dto, view, field, sort, q = _stock_report_filters(request)

    fname_branch = branch.name.replace(" ", "_") if branch else "all"
    response, writer = _csv_response(
        f"stock_{kind}_{view}_{fname_branch}_{dfrom.isoformat()}_{dto.isoformat()}.csv"
    )

    if view == "documents":
        _write_export_header(writer, f"{conf['title']} documents", branch, dfrom, dto)
        header = ["Created", "Document No.", "Ref. No.", conf["party"]]
        if kind == "out":
            header.append("Reason")
        writer.writerow(header + ["Lines", "Total", "Created by", "Note"])
        for d in _stock_report_docs(branch, kind, dfrom, dto, field, q):
            row = [
                timezone.localtime(d.created_at).strftime("%d/%m/%Y %H:%M:%S"),
                d.document_no, d.ref_no, d.vendor if kind == "in" else d.receiver,
            ]
            if kind == "out":
                row.append(d.reason)
            writer.writerow(row + [d.line_count, _csv_num(d.total), d.created_by, d.note])
        return response

    _write_export_header(writer, f"{conf['title']} by Product", branch, dfrom, dto)
    writer.writerow(["#", "Barcode", "Product Name", "Unit", "Document Qty.",
                     "Quantity", "Total Discount", "Total"])
    qty = disc = value = Decimal(0)
    for i, r in enumerate(_stock_report_products(branch, kind, dfrom, dto, sort, q), start=1):
        writer.writerow([i, r["barcode"], r["name"], r["unit"], r["doc_count"],
                         _stock_qty(r["qty"]), _csv_num(r["discount"]), _csv_num(r["total"])])
        qty += r["qty"]
        disc += r["discount"]
        value += r["total"]
    writer.writerow(["Total", "", "", "", "", _stock_qty(qty), _csv_num(disc), _csv_num(value)])
    return response


def _stock_document(request, kind, doc_id):
    """One stock document, read-only: its header and every line as saved."""
    conf = _STOCK_REPORT_KINDS[kind]
    doc = get_object_or_404(StockDocument.objects.select_related("branch"), id=doc_id, type=kind)
    items = list(
        doc.items.select_related("product__unit")
        .annotate(image_len=Length("product__image_url") + Length("product__image_base64"))
        .order_by("id")
    )
    back = request.GET.urlencode()
    return render(request, "backoffice/stock_document.html", {
        "active": conf["active"],
        "kind": kind,
        "title": conf["title"],
        "party_label": conf["party"],
        "doc": doc,
        "items": items,
        "qty_total": sum((it.qty or 0 for it in items), Decimal(0)),
        "back_url": reverse(f"backoffice:{conf['active']}") + (f"?{back}" if back else ""),
        "hide_branch": True,
        "hide_dates": True,
    })


@login_required
def stock_in_report(request):
    return _stock_report(request, "in")


@login_required
def stock_in_export(request):
    return _stock_report_export(request, "in")


@login_required
def stock_out_report(request):
    return _stock_report(request, "out")


@login_required
def stock_out_export(request):
    return _stock_report_export(request, "out")


@login_required
def stock_in_document(request, doc_id):
    return _stock_document(request, "in", doc_id)


@login_required
def stock_out_document(request, doc_id):
    return _stock_document(request, "out", doc_id)


# ─── Check stock ────────────────────────────────────────────────────────
# A check-stock document is a named list of products and quantities — the
# SilomPOS "ตรวจนับสินค้า" sheet.  It never moves stock, which is why, unlike
# stock in/out, it stays editable after saving.  Its point is downstream: the
# till's Stock-In → Import Documents loads one, quantities included, so a
# delivery is received against the list instead of re-keyed line by line.
#
# The quantity lives in ``reconcile_qty`` — the same slot the till's own Check
# Stock form writes its counted figure to — so documents from either side
# import the same way.  ``before_qty`` is on-hand when the line was added and
# ``qty`` the difference, again matching the till.

def _check_stock_pickable(branch):
    """The branch's sellable products as the Add Product dialog needs them."""
    if branch is None:
        return []
    rows = (Product.objects
            .filter(branch=branch, active=True)
            .exclude(product_type="BOM")
            .select_related("category")
            .annotate(image_len=Length("image_url") + Length("image_base64"))
            .order_by("name"))
    return [{
        "id": str(p.id),
        "name": p.name,
        "barcode": p.barcode or "",
        "category": p.category.name if p.category_id else "",
        "stock": p.stock or 0,
        "img": reverse("backoffice:product_image", args=[p.id]) if p.image_len else "",
    } for p in rows]


def _check_stock_post(request, branch, doc=None):
    """Save the posted name + lines onto ``doc`` (new when None).

    Returns ``(doc, error)``.  Lines arrive as parallel ``product_id`` /
    ``qty`` lists, one pair per table row.
    """
    name = (request.POST.get("document_name") or "").strip()[:200]
    ids = request.POST.getlist("product_id")
    qtys = request.POST.getlist("qty")

    wanted: dict[str, Decimal] = {}
    for pid, raw in zip(ids, qtys):
        try:
            qty = Decimal(str(raw).strip() or "0")
        except InvalidOperation:
            return doc, "Quantities must be numbers."
        if qty < 0:
            return doc, "Quantities can't be negative."
        wanted[pid] = qty

    products = {str(p.id): p for p in Product.objects.filter(branch=branch, id__in=list(wanted))}
    # A line whose product row is gone can't be posted back by id; keep it
    # as saved rather than silently dropping it on the next edit.
    orphans = doc.items.filter(product__isnull=True).count() if doc else 0
    if not products and not orphans:
        return doc, "Add at least one product."

    with transaction.atomic():
        if doc is None:
            doc = StockDocument.objects.create(
                branch=branch, type="check",
                document_no=_next_stock_doc_no(branch, "check"),
                document_name=name,
                created_by=getattr(request.user, "name", "") or getattr(request.user, "username", ""),
            )
            before = {}
        else:
            doc.document_name = name
            doc.save(update_fields=["document_name"])
            before = {str(it.product_id): it.before_qty
                      for it in doc.items.filter(product__isnull=False)}
            doc.items.filter(product__isnull=False).delete()
        for pid, qty in wanted.items():
            p = products.get(pid)
            if p is None:
                continue
            on_hand = before.get(pid, Decimal(p.stock or 0))
            StockDocumentItem.objects.create(
                document=doc, product=p,
                barcode=p.barcode or "", product_name=p.name,
                before_qty=on_hand, reconcile_qty=qty, qty=qty - on_hand,
            )
    return doc, None


@login_required
def check_stock_list(request):
    branches, branch, dfrom, dto = _common_filters(request)
    start, end = _date_window(dfrom, dto)
    docs = (StockDocument.objects
            .filter(type="check", branch=branch, created_at__gte=start, created_at__lte=end)
            .annotate(line_count=Count("items"))
            .order_by("-created_at"))
    page_obj = Paginator(docs, 50).get_page(request.GET.get("page"))
    return render(request, "backoffice/check_stock_list.html", {
        "active": "check_stock",
        "branches": branches,
        "branch": branch,
        "date_from": dfrom.isoformat(),
        "date_to": dto.isoformat(),
        "rows": page_obj.object_list,
        "page_obj": page_obj,
        "paginator": page_obj.paginator,
        "qs": _filter_qs(request),
    })


@login_required
def check_stock_new(request):
    branches, branch, _dfrom, _dto = _common_filters(request)
    error = None
    if request.method == "POST" and branch is not None:
        doc, error = _check_stock_post(request, branch)
        if error is None:
            messages.success(request, f"{doc.document_no} was saved.")
            return redirect("backoffice:check_stock_document", doc_id=doc.id)
    return render(request, "backoffice/check_stock_form.html", {
        "active": "check_stock",
        "branches": branches,
        "branch": branch,
        "doc": None,
        "lines": [],
        "error": error,
        "pickable": _check_stock_pickable(branch),
        "back_url": reverse("backoffice:check_stock"),
        "hide_dates": True,
    })


@login_required
def check_stock_document(request, doc_id):
    doc = get_object_or_404(StockDocument.objects.select_related("branch"), id=doc_id, type="check")
    error = None
    if request.method == "POST":
        _doc, error = _check_stock_post(request, doc.branch, doc)
        if error is None:
            messages.success(request, f"{doc.document_no} was saved.")
            return redirect("backoffice:check_stock_document", doc_id=doc.id)
    lines = list(
        doc.items.select_related("product__unit")
        .annotate(image_len=Length("product__image_url") + Length("product__image_base64"))
        .order_by("product_name")
    )
    return render(request, "backoffice/check_stock_form.html", {
        "active": "check_stock",
        "doc": doc,
        "branch": doc.branch,
        "lines": lines,
        "error": error,
        "pickable": _check_stock_pickable(doc.branch),
        "back_url": reverse("backoffice:check_stock"),
        "hide_branch": True,
        "hide_dates": True,
    })


# ─── Products ───────────────────────────────────────────────────────────
@login_required
def product_list(request):
    """Product catalog grid/list. Searchable, sortable, paginated.

    Search mirrors SilomPOS: a ``field`` selector chooses which column the
    free-text ``q`` matches against (All Product searches name + barcode +
    category at once)."""
    branches, branch, _, _ = _common_filters(request)

    # Product photos are stored as base64 data: URIs in `image_url`. Selecting
    # them for a 50-row list shipped megabytes out of Postgres and pasted them
    # into the markup — this page was 574 KB against ~20 KB for its siblings.
    # `defer` keeps them out of the query; the annotation is all the template
    # needs to know, and `backoffice:product_image` serves the bytes.
    # "Removed" is a view of this same page, not a separate screen: the search,
    # the category rail and the sort all have to work there too.
    archived = request.GET.get("archived") == "1"

    qs = (
        Product.objects.filter(active=not archived)
        .select_related("category")
        .defer("image_url", "image_base64")
        .annotate(image_len=Length("image_url") + Length("image_base64"))
    )
    if branch:
        qs = qs.filter(branch=branch)

    field = request.GET.get("field", "all")
    q = (request.GET.get("q") or "").strip()
    if q:
        if field == "name":
            qs = qs.filter(name__icontains=q)
        elif field == "barcode":
            qs = qs.filter(barcode__icontains=q)
        elif field == "category":
            qs = qs.filter(category__name__icontains=q)
        elif field == "type":
            qs = qs.filter(product_type__icontains=q)
        else:  # all
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(barcode__icontains=q)
                | Q(category__name__icontains=q)
            )

    sort = request.GET.get("sort", "name")
    sort_map = {
        "name": "name",
        "newest": "-id",
        "category": "category__name",
        "price_min": "price",   # Product price → lowest first
        "price_max": "-price",  # Product price → highest first
    }
    qs = qs.order_by(sort_map.get(sort, "name"))

    # The left column of the catalogue: every category with how much is in it,
    # counted before the category filter is applied so the numbers don't all
    # collapse to the one you clicked.
    category_qs = Category.objects.all()
    if branch:
        category_qs = category_qs.filter(Q(branch=branch) | Q(branch__isnull=True))
    counts = {
        row["category"]: row["n"]
        for row in (Product.objects.filter(active=not archived, branch=branch)
                    if branch else Product.objects.filter(active=not archived))
        .values("category").annotate(n=Count("id"))
    }
    categories = [
        {"obj": c, "count": counts.get(c.id, 0)}
        for c in category_qs.order_by("order", "name")
    ]
    total_products = sum(counts.values())

    selected_category = request.GET.get("cat") or ""
    if selected_category:
        qs = qs.filter(category_id=selected_category)

    paginator = Paginator(qs, 50)
    page_obj = paginator.get_page(request.GET.get("page"))

    # Sold in the last 30 days, so a row shows whether it earns its listing.
    products = list(page_obj.object_list)
    month_start, month_end = _date_window(
        timezone.localdate() - timedelta(days=29), timezone.localdate())
    sold = {
        row["product_id"]: row["qty"] or 0
        for row in OrderItem.objects.filter(
            product__in=products, order__created_at__range=(month_start, month_end),
        ).exclude(order__status="cancel").values("product_id").annotate(qty=Sum("qty"))
    }
    for p in products:
        p.sold_30d = sold.get(p.id, 0)
        p.margin = float((p.price - p.cost) / p.price * 100) if p.price else 0

    view = request.GET.get("view", "grid")  # grid | list

    context = {
        "active": "products",
        "page_title": "Catalogue",
        "branches": branches,
        "branch": branch,
        "field": field,
        "q": q,
        "products": products,
        "categories": categories,
        "selected_category": selected_category,
        "total_products": total_products,
        "page_obj": page_obj,
        "paginator": paginator,
        "sort": sort,
        "view": view,
        "archived": archived,
        # The count keeps removed products findable — an unlabelled tab nobody
        # has a reason to click is where a soft delete goes to be forgotten.
        "archived_count": (
            Product.objects.filter(active=False, branch=branch).count()
            if branch else Product.objects.filter(active=False).count()
        ),
        "hide_dates": True,
        "qs": _filter_qs(
            request,
            sort=sort if sort != "name" else None,
            field=field if field != "all" else None,
            q=q or None,
            view=view,
            cat=selected_category or None,
            archived="1" if archived else None,
        ),
    }
    return render(request, "backoffice/product_list.html", context)


def _product_form_categories(branch):
    qs = Category.objects.filter(active=True)
    if branch:
        qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True))
    return list(qs.order_by("name"))


def _product_form_units(branch):
    qs = Unit.objects.filter(active=True)
    if branch:
        qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True))
    return list(qs.order_by("order", "name"))


# Column widths, read off the model rather than written out again, so a
# migration that widens a column widens the form check with it.
_PRODUCT_MAX_LEN = {
    name: Product._meta.get_field(name).max_length
    for name in ("name", "name_th", "sku", "barcode")
}

# `numeric(10, 2)` and `integer` respectively.  Postgres raises past either,
# which is a 500 with every other field the admin typed lost along with it.
_PRODUCT_PRICE_MAX = Decimal("99999999.99")
_PRODUCT_INT_MAX = 2147483647


def _product_text(post, field, label, errors) -> str:
    """A text field off the form, with over-length reported not truncated.

    The value is handed back whole even when it is too long, so the form comes
    back with the typing intact and the admin trims the one field that is
    wrong — truncating silently would save something they did not write.
    """
    value = (post.get(field) or "").strip()
    limit = _PRODUCT_MAX_LEN[field]
    if len(value) > limit:
        errors.append(
            f"{label} is {len(value)} characters — the most that fits is {limit}."
        )
    return value


def _product_decimal(post, field, label, errors) -> Decimal:
    """A money field off the form, as a Decimal, never raising.

    ``Decimal`` on raw POST text raises ``InvalidOperation`` on anything that
    is not a number, and — worse, because it looks like it worked — accepts
    two values the column will not: "NaN" and "Infinity".  Every one of those
    is a 500 on save, so all three become form errors instead.
    """
    raw = (post.get(field) or "").strip()
    if not raw:
        return Decimal("0")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        errors.append(f"{label} must be a number — “{raw}” isn't one.")
        return Decimal("0")
    if not value.is_finite():
        errors.append(f"{label} must be a number.")
        return Decimal("0")
    if value > _PRODUCT_PRICE_MAX:
        errors.append(f"{label} can't be more than {_PRODUCT_PRICE_MAX:,}.")
    return value


def _product_int(post, field, label, errors) -> int:
    """A whole-number field off the form, never raising.

    Same shape as ``_product_decimal``: ``int`` raises on "1.5" or "ten", and
    a number past 2^31 is accepted here only to overflow the column on save.
    """
    raw = (post.get(field) or "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"{label} must be a whole number — “{raw}” isn't one.")
        return 0
    if abs(value) > _PRODUCT_INT_MAX:
        errors.append(f"{label} is too large.")
        return 0
    return value


def _apply_product_form(product, post, branch, errors):
    """Pull fields out of a POSTed product form onto a Product instance.

    Used by both `product_new` and `product_detail` to save.  Anything the
    column cannot hold is appended to ``errors`` rather than raised: the value
    still lands on the instance, so the page can come back with the form as it
    was typed and one field flagged, instead of an error page that loses all
    of it.  Every field here was previously passed straight to the column,
    where the only available answer was a 500.
    """
    product.branch = branch
    product.name = _product_text(post, "name", "Product name", errors)
    product.name_th = _product_text(post, "name_th", "Description", errors)
    product.barcode = _product_text(post, "barcode", "Barcode", errors)
    product.sku = _product_text(post, "sku", "SKU", errors)
    product.price = _product_decimal(post, "price", "Price", errors)
    product.cost = _product_decimal(post, "cost", "Cost", errors)
    # Stock is not on this form: it moves through Inventory (counts, receipts,
    # waste) and sales, so a POSTed "stock" is ignored rather than trusted.
    # 0 means "not tracked" — Inventory says so rather than flagging the
    # product against a threshold nobody set.
    product.par_level = _product_int(post, "par_level", "Par level", errors)
    # Blank stays blank rather than becoming 0: 0 days means "same day".
    if (post.get("shelf_life") or "").strip():
        product.shelf_life = _product_int(
            post, "shelf_life", "Shelf life", errors)
        if product.shelf_life < 0:
            errors.append("Shelf life can't be negative.")
            product.shelf_life = None
    else:
        product.shelf_life = None
    cat_id = post.get("category") or ""
    product.category_id = cat_id if cat_id else None
    product.tax_type = post.get("tax_type") or "V"
    product.product_type = post.get("product_type") or "P"
    # The form's JS downscales before POSTing, but that runs in the browser and
    # can be bypassed (JS off) or fall through its own error path. Normalising
    # server-side is what actually bounds the column. See bravepos.images.
    product.image_url = images.normalize(post.get("image_url") or "")
    product.is_favorite = bool(post.get("is_favorite"))
    unit_id = post.get("unit") or ""
    product.unit_id = unit_id if unit_id else None
    return product


def product_errors(product) -> list[str]:
    """Blocking problems with the assembled product.

    Per-field limits are collected by `_apply_product_form` as it parses; what
    is left is the one rule that needs the whole instance.  A product with no
    name is a blank row on the Sale screen that a cashier cannot identify and
    the catalogue sorts to the top — the input is `required`, but a hand-made
    POST is not.
    """
    errors = []
    if not product.name:
        errors.append("Product name is required.")
    return errors


def product_duplicate(product):
    """Another product in the same branch already answering to this name.

    Not an error — a shop may deliberately keep two rows with one name while
    renaming a variant, and nothing in the schema forbids it.  It is a warning
    because of how the duplicates actually get made: nothing on Product is
    unique, so a resubmitted create form is indistinguishable from a new
    product, and an admin who could not tell whether the first save went
    through gets two.  That is what happened on 26 August — one form POSTed
    twice eleven seconds apart, two rows in the catalogue, and ten minutes
    spent afterwards editing both.  Saying so before the second save is the
    whole fix; `confirm_duplicate` is what lets it through anyway.

    Compared case-insensitively, and scoped to the branch, because that is the
    pair a cashier sees side by side on one till.
    """
    name = (product.name or "").strip()
    if not name:
        return None
    return (Product.objects
            .filter(branch=product.branch, name__iexact=name)
            .exclude(pk=product.pk)
            .first())


def _product_save_blocked(request, form_errors, duplicate) -> bool:
    """Whether this POST should stop short of saving.

    A form error always stops it.  A duplicate stops it exactly once: the
    re-rendered form carries `confirm_duplicate`, so pressing Save a second
    time goes through.  The warning is there for the admin who cannot tell
    whether their last save landed — not to refuse a second row outright,
    which is sometimes what they want.
    """
    if form_errors:
        return True
    return bool(duplicate) and not request.POST.get("confirm_duplicate")


def _fanout_branch_names(branch):
    """The branches "Sync to all branches" would write to, for the form."""
    return [b.name for b in catalog.other_branches(branch)]


def _fanout_message(report) -> str:
    """The trailing sentence about what the other branches got.

    Names them rather than counting them, for the reason the sync page's
    preview lists products rather than totalling them: this writes to branches
    nobody is looking at, and "3 branches updated" is not something an admin
    can check.  A run that changed nothing still says so — after ticking a box
    labelled *Sync to all branches*, silence is indistinguishable from a
    toggle that quietly does nothing.
    """
    if report is None:
        return ""
    parts = []
    if report["created"]:
        parts.append("added to " + ", ".join(report["created"]))
    if report["updated"]:
        parts.append("updated at " + ", ".join(report["updated"]))
    if not parts:
        return " Every other branch already matched it."
    return " Also " + "; ".join(parts) + "."


def _active_fanout_message(report, verb) -> str:
    """`_fanout_message` for the Remove and Restore buttons."""
    if report is None:
        return ""
    if report["updated"]:
        return f" Also {verb} at " + ", ".join(report["updated"]) + "."
    return " No other branch needed changing."


@login_required
def product_detail(request, product_id):
    """View + edit a single product. POST saves and stays on the page.

    Saving redirects back to this same URL, so without the message below a
    successful save and a save that never happened look identical: the same
    form, the same values, no banner.  That ambiguity is what produced the
    duplicate rows `product_duplicate` warns about — see its docstring.

    "Sync to all branches" carries the whole form to the other branches'
    copies except stock and par level, which are each shop's own
    (`catalog.FANOUT_FIELDS` has the list).  ``previous_name`` is
    captured before the form is applied, so a rename reaches the copies that
    still answer to the old name instead of adding a second row beside them.
    """
    branches, branch, _, _ = _common_filters(request)
    product = get_object_or_404(Product, id=product_id)
    form_errors, duplicate = [], None
    sync_all = bool(request.POST.get("sync_all"))
    # Read before the form overwrites it: a rename can only find the other
    # branches' copies under the name they still hold.
    previous_name = product.name

    if request.method == "POST":
        _apply_product_form(product, request.POST, product.branch or branch,
                            form_errors)
        form_errors += product_errors(product)
        duplicate = product_duplicate(product)
        if not _product_save_blocked(request, form_errors, duplicate):
            with transaction.atomic():
                product.save()
                fanout = (
                    catalog.fanout_product(product, previous_name=previous_name)
                    if sync_all else None
                )
            messages.success(
                request,
                f"{product.name} was saved." + _fanout_message(fanout))
            return redirect("backoffice:product_detail", product_id=product.id)
        # Nothing was saved; `product` still carries what was typed, so the
        # form below comes back filled in rather than reverting to the row in
        # the database.

    context = {
        "active": "products",
        "branches": branches,
        "branch": branch,
        "product": product,
        "categories": _product_form_categories(product.branch or branch),
        "units": _product_form_units(product.branch or branch),
        "mode": "edit",
        "form_errors": form_errors,
        "duplicate": duplicate,
        "sync_all": sync_all,
        "sync_branches": _fanout_branch_names(product.branch or branch),
        "hide_dates": True,
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/product_form.html", context)


@login_required
def product_archive(request, product_id):
    """Take a product off sale without destroying what it sold.

    ``active=False`` rather than a row delete, because that is already how the
    rest of this system retires a catalogue row: the self-order menu and the
    suggestion engine filter ``active=True``, self-order checkout refuses an
    inactive product with "deactivated or deleted" as one case, and this
    catalogue has always listed active rows only. Since the API narrowed its
    product listings to active rows, the till honours it too.

    Keeping the row is not sentiment. ``OrderItem`` never snapshotted cost, so
    `_profit_expr` reads ``product__cost`` live across the FK — destroy the row
    and every past bill's profit silently jumps to equal its revenue, while
    Product Performance relabels the line "(deleted product)". Archiving leaves
    both untouched. `ProductViewSet.perform_destroy` already reasons this way
    for *sibling* branches; this is the same argument applied to the original.

    Reversible, and the catalogue's "Removed" view is where it is reversed.
    """
    product = get_object_or_404(Product, id=product_id)
    if request.method != "POST":
        return redirect("backoffice:product_detail", product_id=product.id)

    product.active = False
    with transaction.atomic():
        product.save(update_fields=["active"])
        fanout = (catalog.fanout_active(product)
                  if request.POST.get("sync_all") else None)
    messages.success(
        request,
        f"\u201c{product.name}\u201d was removed from the catalogue. "
        f"Reports keep it, and Removed products can restore it."
        + _active_fanout_message(fanout, "removed"),
    )
    return redirect(reverse("backoffice:product_list") + f"?{_filter_qs(request)}")


@login_required
def product_restore(request, product_id):
    """Put a removed product back on sale."""
    product = get_object_or_404(Product, id=product_id)
    if request.method != "POST":
        return redirect("backoffice:product_detail", product_id=product.id)

    product.active = True
    with transaction.atomic():
        product.save(update_fields=["active"])
        fanout = (catalog.fanout_active(product)
                  if request.POST.get("sync_all") else None)
    messages.success(
        request,
        f"\u201c{product.name}\u201d is back in the catalogue."
        + _active_fanout_message(fanout, "restored"))
    return redirect(reverse("backoffice:product_list") + f"?{_filter_qs(request)}")


@login_required
def product_new(request):
    """Add a single product. POST creates and redirects to its detail page.

    "Sync to all branches" creates the same product at every other active
    branch in the same transaction — the common case, since a new cake is a new
    cake everywhere and the alternative is retyping it eight times.  It is off
    by default: this form is the only place in the backoffice that can write to
    a branch the admin is not looking at, so it is asked for per save rather
    than remembered.  `catalog.fanout_product` is what it does.
    """
    branches, branch, _, _ = _common_filters(request)
    product = Product(branch=branch)
    form_errors, duplicate = [], None
    sync_all = bool(request.POST.get("sync_all"))

    if request.method == "POST":
        _apply_product_form(product, request.POST, branch, form_errors)
        form_errors += product_errors(product)
        duplicate = product_duplicate(product)
        if not _product_save_blocked(request, form_errors, duplicate):
            with transaction.atomic():
                product.save()
                fanout = catalog.fanout_product(product) if sync_all else None
            messages.success(
                request,
                f"{product.name} was added to the catalogue."
                + _fanout_message(fanout))
            return redirect("backoffice:product_detail", product_id=product.id)

    context = {
        "active": "products",
        "branches": branches,
        "branch": branch,
        "product": product,
        "categories": _product_form_categories(branch),
        "units": _product_form_units(branch),
        "mode": "new",
        "form_errors": form_errors,
        "duplicate": duplicate,
        "sync_all": sync_all,
        "sync_branches": _fanout_branch_names(branch),
        "hide_dates": True,
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/product_form.html", context)


def _sync_message(source, targets, results):
    """What the sync did, in a sentence.

    Says what was left alone as well as what was added: an admin pressing this
    on a branch that has been trading for a month needs to hear that its own
    prices survived, and hearing it only in the docs is too late.
    """
    added = sum(1 for r in results for p in r["products"] if p["action"] == "created")
    retired = sum(len(r.get("retired") or []) for r in results)
    where = ", ".join(t.name for t in targets)

    if not added and not retired:
        return (f"{where} already matched {source.name}'s catalogue. "
                f"Nothing was changed.")

    parts = []
    if added:
        parts.append(f"{added} product{'' if added == 1 else 's'} copied from "
                     f"{source.name} to {where}")
    if retired:
        parts.append(f"{retired} product{'' if retired == 1 else 's'} taken off "
                     f"sale there because {source.name} has removed "
                     f"{'it' if retired == 1 else 'them'}")
    return ("; ".join(parts) + ". Products those branches already had and still "
            "sell were left exactly as they were.")


@login_required
def product_sync(request):
    """Copy one branch's catalogue onto others — the new-branch and new-product path.

    Two jobs, one screen.  A branch that has just opened has an empty product
    list and nobody is going to retype forty-four cakes into it; and a product
    added at BIO HOUSE has to reach the other shops without an admin editing
    each one by hand.  Both are "make that branch's list look like this one".

    **It only adds.**  A product the target already has is left exactly as it
    is — its price, its cost, its photo, its stock, untouched.  That is what
    makes this safe to press on a branch that has been trading for a month with
    prices of its own, and safe to press twice.  Overwriting is deliberately
    not on this page; ``manage.py sync_products`` is where that lives, because
    it needs someone who knows they are doing it.

    ``source`` and ``targets``, not ``from`` and ``to``: the latter are the
    date-range filter every other page uses, and `_filter_qs` carries them.

    GET previews — naming the products, because "18 will be added" is not
    something an admin can check, and this page writes to branches nobody is
    looking at.  POST does it.
    """
    branches, branch, _, _ = _common_filters(request)
    all_branches = list(Branch.objects.filter(active=True).order_by("name"))

    by_id = {str(b.id): b for b in all_branches}
    source = by_id.get(request.POST.get("source") or request.GET.get("source") or "")
    if source is None:
        source = branch if branch in all_branches else (all_branches[0] if all_branches else None)

    posted = request.POST.getlist("targets") or request.GET.getlist("targets")
    # Filtered against the branches actually offered rather than trusted from
    # the request: these ids arrive from a form anyone can edit, and a branch
    # syncing onto itself would read and write the same rows.
    targets = [by_id[t] for t in posted if t in by_id and (not source or t != str(source.id))]

    # Removals travel too, unless the box is unticked. Without this a product
    # taken off sale at the source stayed on sale at every branch it had been
    # copied to, and no amount of re-syncing fixed it — the removed row simply
    # dropped out of the payload.
    retire = (request.POST.get("retire", "on") if request.method == "POST"
              else request.GET.get("retire", "on")) == "on"

    previews, results = [], []
    if source and targets:
        catalogue = catalog.source_catalogue(source)
        removed = catalog.removed_catalogue(source) if retire else []

        if request.method == "POST":
            with transaction.atomic():
                results = [
                    catalog.copy_catalogue(
                        source, t, catalogue=catalogue,
                        removed=removed, retire=retire,
                    )
                    for t in targets
                ]
            messages.success(request, _sync_message(source, targets, results))
            return redirect(
                reverse("backoffice:product_sync") + "?" + urlencode(
                    [("source", str(source.id))]
                    + [("targets", str(t.id)) for t in targets]
                    + ([] if retire else [("retire", "off")]),
                )
            )

        previews = [
            catalog.preview(source, t, catalogue, removed=removed) for t in targets
        ]

    context = {
        "active": "products",
        "branches": branches,
        "branch": branch,
        "all_branches": all_branches,
        "source": source,
        "targets": targets,
        "target_ids": {str(t.id) for t in targets},
        "previews": previews,
        "retire": retire,
        "source_count": source.products.filter(active=True).count() if source else 0,
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/product_sync.html", context)


@login_required
def product_bulk_add(request):
    """Add up to 10 products in one POST (matches SilomPOS Quick Add)."""
    branches, branch, _, _ = _common_filters(request)
    saved = 0

    if request.method == "POST":
        names = request.POST.getlist("name")
        for idx, name in enumerate(names):
            name = (name or "").strip()
            if not name:
                continue
            p = Product(branch=branch, name=name)
            p.barcode = (request.POST.getlist("barcode")[idx] or "").strip()
            p.price = Decimal(request.POST.getlist("price")[idx] or "0")
            cat = request.POST.getlist("category")[idx] or ""
            p.category_id = cat or None
            unit = request.POST.getlist("unit")[idx] or ""
            p.unit_id = unit or None
            ptype = request.POST.getlist("product_type")[idx] or "P"
            p.product_type = ptype
            p.save()
            saved += 1
        if saved:
            messages.success(
                request,
                f"{saved} product{'' if saved == 1 else 's'} added to the catalogue.")
            return redirect(reverse("backoffice:product_list") + f"?{_filter_qs(request)}")

    # GET: render N blank rows. Default 5; bumpable up to 10.
    try:
        rows_count = max(1, min(10, int(request.GET.get("rows", "5"))))
    except ValueError:
        rows_count = 5

    context = {
        "active": "products",
        "branches": branches,
        "branch": branch,
        "rows_count": rows_count,
        "rows_range": range(rows_count),
        "categories": _product_form_categories(branch),
        "units": _product_form_units(branch),
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/product_bulk_add.html", context)


@login_required
def product_bulk_edit(request):
    """Inline-editable grid for existing products. POST saves all rows.

    "Sync to all branches" runs `catalog.fanout_product` on every row saved,
    the same as the single product form — so it flattens every row on the
    page to this branch's values, not only the rows that were changed.
    """
    branches, branch, _, _ = _common_filters(request)

    # Image columns deferred for the same reason as the catalogue — see
    # `product_image`. The POST path below re-fetches each row in full, so
    # saving is unaffected.
    qs = (
        Product.objects.filter(active=True)
        .select_related("category")
        .defer("image_url", "image_base64")
        .annotate(image_len=Length("image_url") + Length("image_base64"))
    )
    if branch:
        qs = qs.filter(branch=branch)
    qs = qs.order_by("name")

    if request.method == "POST":
        ids = request.POST.getlist("id")
        shelf_lives = request.POST.getlist("shelf_life")
        sync_all = bool(request.POST.get("sync_all"))
        saved = 0
        synced = set()
        for idx, pid in enumerate(ids):
            try:
                p = Product.objects.get(id=pid)
            except Product.DoesNotExist:
                continue
            previous_name = p.name
            p.barcode = (request.POST.getlist("barcode")[idx] or "").strip()
            p.name = (request.POST.getlist("name")[idx] or p.name).strip()
            p.name_th = (request.POST.getlist("description")[idx] or "").strip()
            p.price = Decimal(request.POST.getlist("price")[idx] or "0")
            p.cost = Decimal(request.POST.getlist("cost")[idx] or "0")
            cat = request.POST.getlist("category")[idx] or ""
            p.category_id = cat or None
            unit = request.POST.getlist("unit")[idx] or ""
            p.unit_id = unit or None
            # A POST without the column leaves the saved value alone; blank
            # clears it, as on the product form. A bad number is skipped
            # rather than failing the rest of the page.
            if idx < len(shelf_lives):
                raw = shelf_lives[idx].strip()
                if not raw:
                    p.shelf_life = None
                elif raw.isdigit():
                    p.shelf_life = int(raw)
            with transaction.atomic():
                p.save()
                if sync_all:
                    report = catalog.fanout_product(
                        p, previous_name=previous_name)
                    synced.update(report["created"] + report["updated"])
            saved += 1
        if saved:
            msg = f"{saved} product{'' if saved == 1 else 's'} saved."
            if sync_all:
                msg += (" Also synced to " + ", ".join(sorted(synced)) + "."
                        if synced else " Every other branch already matched.")
            messages.success(request, msg)
        return redirect(reverse("backoffice:product_bulk_edit") + f"?{_filter_qs(request)}")

    paginator = Paginator(qs, 10)  # SilomPOS shows 10/page on this view
    page_obj = paginator.get_page(request.GET.get("page"))

    context = {
        "active": "products",
        "branches": branches,
        "branch": branch,
        "products": page_obj.object_list,
        "page_obj": page_obj,
        "paginator": paginator,
        "categories": _product_form_categories(branch),
        "units": _product_form_units(branch),
        "sync_branches": _fanout_branch_names(branch) if branch else [],
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/product_bulk_edit.html", context)


# ─── Categories ─────────────────────────────────────────────────────────
def _apply_category_form(category, post, branch):
    """Pull fields out of a POSTed category form onto a Category instance.
    Shared by `category_new` and `category_detail`."""
    category.branch = branch
    category.name = (post.get("name") or "").strip()
    category.name_th = (post.get("name_th") or "").strip()
    category.color = (post.get("color") or "#00B14F").strip() or "#00B14F"
    try:
        category.order = int(post.get("order") or 0)
    except ValueError:
        category.order = 0
    category.active = post.get("active") == "on"
    return category


@login_required
def category_list(request):
    """Category management grid for the selected branch — mirrors the
    SilomPOS Category page (order #, name, colour, active) minus the
    Grab/icon/cooking-priority columns."""
    branches, branch, _, _ = _common_filters(request)

    qs = Category.objects.all()
    if branch:
        qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True))
    qs = qs.order_by("order", "name")

    context = {
        "active": "categories",
        "branches": branches,
        "branch": branch,
        "categories": qs,
        "hide_dates": True,
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/category_list.html", context)


@login_required
def category_detail(request, category_id):
    """View + edit a single category. POST saves and returns to the list."""
    branches, branch, _, _ = _common_filters(request)
    category = get_object_or_404(Category, id=category_id)

    if request.method == "POST":
        _apply_category_form(category, request.POST, category.branch or branch)
        category.save()
        return redirect(reverse("backoffice:category_list") + f"?{_filter_qs(request)}")

    context = {
        "active": "categories",
        "branches": branches,
        "branch": branch,
        "category": category,
        "mode": "edit",
        "hide_dates": True,
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/category_form.html", context)


@login_required
def category_new(request):
    """Add a single category. POST creates and returns to the list."""
    branches, branch, _, _ = _common_filters(request)

    if request.method == "POST":
        category = Category()
        _apply_category_form(category, request.POST, branch)
        category.save()
        return redirect(reverse("backoffice:category_list") + f"?{_filter_qs(request)}")

    context = {
        "active": "categories",
        "branches": branches,
        "branch": branch,
        "category": Category(branch=branch, active=True),
        "mode": "new",
        "hide_dates": True,
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/category_form.html", context)


@login_required
def category_delete(request, category_id):
    """Delete a category. Products keep working — the FK is SET_NULL, so any
    products in this category just become uncategorised."""
    category = get_object_or_404(Category, id=category_id)
    if request.method == "POST":
        category.delete()
    return redirect(reverse("backoffice:category_list") + f"?{_filter_qs(request)}")


# ─── Discount types ─────────────────────────────────────────────────────
# The presets a cashier picks from the till's discount dropdown.  Branch-scoped
# like categories, because each branch owns its own product rows.  The branch's
# ``discount_types_enabled`` switch lives on this page rather than the branch
# form: it is meaningless without the presets, and this is where someone
# setting them up will look for it.
def _discount_products(branch):
    """Active products at ``branch``, in the till's category order, for the
    "applies to" picker."""
    if branch is None:
        return Product.objects.none()
    return (Product.objects.filter(branch=branch, active=True)
            .select_related("category")
            .order_by("category__order", "category__name", "sort_order", "name"))


def _discount_categories(branch):
    """Active categories at ``branch``, each with how many active products it
    holds — the count is what tells an admin a category preset will reach
    something on the till today."""
    if branch is None:
        return Category.objects.none()
    return (Category.objects
            .filter(Q(branch=branch) | Q(branch__isnull=True), active=True)
            .annotate(product_count=Count(
                "products", filter=Q(products__branch=branch, products__active=True)))
            .order_by("order", "name"))


class _DiscountPicks:
    """What a discount-type form chose besides the row's own fields: the
    product/category lists and the combination rows.  M2Ms and rows are saved
    by the caller after the discount itself, since a new row has no id yet."""

    def __init__(self, products=(), categories=(), rows=()):
        self.products = list(products)
        self.categories = list(categories)
        # [{"target": "p:<uuid>" | "c:<uuid>", "min_qty": int}]
        self.rows = list(rows)

    @classmethod
    def of(cls, dt):
        if dt.pk is None:
            return cls()
        return cls(
            dt.products.values_list("id", flat=True),
            dt.categories.values_list("id", flat=True),
            [{"target": f"p:{c.product_id}" if c.product_id else f"c:{c.category_id}",
              "min_qty": c.min_qty} for c in dt.conditions.all()],
        )

    def save(self, dt):
        """Apply to ``dt`` through ``discounts.apply_targets``, which writes
        the promotion's history entry for anything that changed."""
        prods = {p.pk: p for p in Product.objects.filter(pk__in=self.products)}
        cats = {c.pk: c for c in Category.objects.filter(pk__in=self.categories)}
        row_prods = {str(p.pk): p for p in Product.objects.filter(
            pk__in=[r["target"][2:] for r in self.rows if r["target"].startswith("p:")])}
        row_cats = {str(c.pk): c for c in Category.objects.filter(
            pk__in=[r["target"][2:] for r in self.rows if r["target"].startswith("c:")])}
        rows = [{"product": row_prods.get(r["target"][2:]) if r["target"].startswith("p:") else None,
                 "category": row_cats.get(r["target"][2:]) if r["target"].startswith("c:") else None,
                 "min_qty": r["min_qty"]} for r in self.rows]
        discounts.apply_targets(dt, list(prods.values()), list(cats.values()), rows)


def _cheapest_combo(branch, rows, match=DiscountType.MATCH_ALL):
    """The lowest price a set could come to, or None when a row can't be met at
    all (a category with no active products).  For "all" a set is every row;
    for "any" it is one row, so the cheapest row.  A fixed discount must stay
    under this, or some set would ring up below ฿0."""
    if match == DiscountType.MATCH_ANY:
        prices = [_cheapest_combo(branch, [r]) for r in rows]
        return None if None in prices else min(prices, default=None)
    total = Decimal(0)
    for r in rows:
        kind, ident = r["target"][:1], r["target"][2:]
        qs = Product.objects.filter(branch=branch, active=True)
        qs = qs.filter(id=ident) if kind == "p" else qs.filter(category_id=ident)
        cheapest = qs.order_by("price").values_list("price", flat=True).first()
        if cheapest is None:
            return None
        total += cheapest * r["min_qty"]
    return total


def _apply_discount_form(dt, post, branch):
    """Read a POSTed discount-type form onto ``dt``.

    Returns ``(errors, picks)`` — see :class:`_DiscountPicks`.
    """
    errors = []
    dt.branch = branch
    dt.name = (post.get("name") or "").strip()[:120]
    if not dt.name:
        errors.append("Give the discount a name — it is what the cashier sees in the dropdown.")
    elif dt.name.lower() == "other":
        # The till adds its own "Other" entry — the hand-entered, reason-required
        # one that raises an alert.  A preset with the same name would be
        # indistinguishable from it on the till and in the order history.
        errors.append("“Other” is reserved for the till's own hand-entered discount — pick another name.")

    dt.active = post.get("active") == "on"

    # Validity period. Blank means open-ended on that side.
    for field, label in (("start_date", "start"), ("end_date", "end")):
        raw = (post.get(field) or "").strip()
        try:
            setattr(dt, field, date.fromisoformat(raw) if raw else None)
        except ValueError:
            setattr(dt, field, None)
            errors.append(f"The {label} date isn't a valid date.")
    if dt.start_date and dt.end_date and dt.end_date < dt.start_date:
        errors.append("The end date is before the start date.")

    applies = post.get("applies_to")
    dt.applies_to = applies if applies in dict(DiscountType.APPLIES_CHOICES) \
        else DiscountType.APPLIES_ALL
    dt.kind = post.get("kind") if post.get("kind") in dict(DiscountType.KIND_CHOICES) \
        else DiscountType.KIND_PERCENT

    # ── What the customer buys ─────────────────────────────────────────
    picks = _DiscountPicks()
    if dt.applies_to == DiscountType.APPLIES_PRODUCTS:
        wanted = [w for w in post.getlist("products") if w]
        picks.products = list(
            _discount_products(branch).filter(id__in=wanted).values_list("id", flat=True)
        ) if wanted else []
        if not picks.products:
            errors.append("Pick at least one product, or make the discount apply to all products.")
    elif dt.applies_to == DiscountType.APPLIES_CATEGORIES:
        wanted = [w for w in post.getlist("categories") if w]
        picks.categories = list(
            _discount_categories(branch).filter(id__in=wanted).values_list("id", flat=True)
        ) if wanted else []
        if not picks.categories:
            errors.append("Pick at least one category, or make the discount apply to all products.")
    elif dt.applies_to == DiscountType.APPLIES_COMBO:
        valid = ({f"p:{i}" for i in _discount_products(branch).values_list("id", flat=True)}
                 | {f"c:{i}" for i in _discount_categories(branch).values_list("id", flat=True)})
        for target, qty in zip(post.getlist("cond_target"), post.getlist("cond_qty")):
            if not target:
                continue
            try:
                n = int(qty or 1)
            except ValueError:
                n = 0
            if target not in valid:
                errors.append("A combination row names a product or category that is not at this branch.")
                continue
            if n < 1:
                errors.append("Each combination row needs a quantity of at least 1.")
                n = 1
            picks.rows.append({"target": target, "min_qty": n})
        if not picks.rows:
            errors.append("Add at least one product or category the customer has to buy.")

    dt.combo_match = (post.get("combo_match")
                      if post.get("combo_match") in dict(DiscountType.MATCH_CHOICES)
                      else DiscountType.MATCH_ALL)

    def money(field, label):
        raw = (post.get(field) or "").strip().replace(",", "")
        if not raw:
            return None
        try:
            v = Decimal(raw)
        except InvalidOperation:
            errors.append(f"Enter the {label} as a number.")
            return None
        if v <= 0:
            errors.append(f"The {label} must be more than zero, or left blank.")
            return None
        return v

    dt.min_order_amount = money("min_order_amount", "minimum order amount")

    # ── What the customer gets ─────────────────────────────────────────
    dt.free_product = None
    dt.free_category = None
    if dt.kind == DiscountType.KIND_FREE:
        dt.value = Decimal(0)
        if dt.applies_to == DiscountType.APPLIES_COMBO:
            errors.append("A free item can only be given with a one-product discount for now — "
                          "pick a fixed or percentage discount for a combination.")
        # One picker for both: a product id, or "cat:<id>" for "any product
        # in this category — the cashier picks which one at the till".
        wanted = (post.get("free_product") or "").strip()
        if wanted.startswith("cat:"):
            dt.free_category = next(
                (c for c in _discount_categories(branch) if str(c.id) == wanted[4:]), None)
            if dt.free_category is None:
                errors.append("Pick the product or category that is given free.")
            elif not dt.free_category.product_count:
                errors.append(f"“{dt.free_category.name}” has no active products at this branch "
                              "to give away.")
        else:
            dt.free_product = next(
                (p for p in _discount_products(branch) if str(p.id) == wanted), None)
            if dt.free_product is None:
                errors.append("Pick the product or category that is given free.")
        try:
            dt.free_qty = int(post.get("free_qty") or 1)
        except ValueError:
            dt.free_qty = 0
        if dt.free_qty < 1:
            errors.append("Give away at least 1 piece.")
            dt.free_qty = 1
        # A free item has no amount to cap.
        dt.max_discount = None
    else:
        dt.max_discount = money("max_discount", "maximum discount")
        try:
            dt.value = Decimal((post.get("value") or "").strip())
        except InvalidOperation:
            dt.value = Decimal(0)
            errors.append("Enter the discount as a number.")
        else:
            if dt.value <= 0:
                errors.append("The discount must be more than zero.")
            elif dt.kind == DiscountType.KIND_PERCENT and dt.value > 100:
                errors.append("A percentage discount cannot be more than 100%.")
            elif dt.kind == DiscountType.KIND_FIXED and picks.rows:
                cheapest = _cheapest_combo(branch, picks.rows, dt.combo_match)
                if cheapest is None:
                    errors.append("A category in the combination has no active products, "
                                  "so the combination can never be met.")
                elif dt.value >= cheapest:
                    errors.append(
                        f"The discount (฿{dt.value:,.2f}) must be less than the cheapest "
                        f"this combination can ring up at (฿{cheapest:,.2f}).")
    return errors, picks


def _staff_or_none(user):
    """The signed-in backoffice user as a Staff row (they are Staff rows),
    or None for anything else."""
    return user if isinstance(user, Staff) else None


def _discount_history(dt, limit=100):
    """The promotion's audit entries, newest first, flattened for display the
    way the Audit log page flattens them."""
    if dt.pk is None:
        return []
    entries = list(AuditLog.objects.filter(model="DiscountType", object_id=str(dt.pk))
                   .order_by("-at")[:limit])
    # The free product is stored by id; show it by name.
    ids = set()
    for e in entries:
        for field in ("free_product", "free_category"):
            ch = (e.changes or {}).get(field)
            vals = [ch.get("from"), ch.get("to")] if isinstance(ch, dict) else [ch]
            ids.update(str(v) for v in vals if v)
    names = {str(k): v for k, v in Product.objects.filter(pk__in=ids).values_list("pk", "name")} \
        if ids else {}
    cat_names = {str(k): v for k, v in Category.objects.filter(pk__in=ids).values_list("pk", "name")} \
        if ids else {}

    def show(field, value):
        if field == "free_product" and value:
            return names.get(str(value), "a removed product")
        if field == "free_category" and value:
            return cat_names.get(str(value), "a removed category")
        return value

    for e in entries:
        diffs = []
        for field, change in (e.changes or {}).items():
            # Plumbing, or already on the page (the branch is in the title).
            if field in ("id", "branch", "updated_at", "created_at", "group_id",
                         "updated_by", "created_by"):
                continue
            if isinstance(change, dict) and ("from" in change or "to" in change):
                change = {"from": show(field, change.get("from")), "to": show(field, change.get("to"))}
            else:
                change = show(field, change)
            if isinstance(change, dict) and ("from" in change or "to" in change):
                diffs.append({"field": field.replace("_", " "), "old": _audit_value(change.get("from")),
                              "new": _audit_value(change.get("to")), "is_diff": True})
            else:
                diffs.append({"field": field.replace("_", " "), "old": "",
                              "new": _audit_value(change), "is_diff": False})
        e.diffs = diffs
    return entries


def _discount_sync_choice(post, dt):
    """Which other branches the form asked this promotion to run at.

    Returns ``(scope, branches)``: scope is "this", "all" or "selected".
    """
    others = list(catalog.other_branches(dt.branch))
    scope = post.get("sync_scope")
    if scope == "all":
        return scope, others
    if scope == "selected":
        wanted = set(post.getlist("sync_branches"))
        return scope, [b for b in others if str(b.id) in wanted]
    return "this", []


def _discount_sync_message(report) -> str:
    parts = []
    if report["created"]:
        parts.append("added at " + ", ".join(report["created"]))
    if report["updated"]:
        parts.append("updated at " + ", ".join(report["updated"]))
    if report["removed"]:
        parts.append("removed from " + ", ".join(report["removed"]))
    return (" It was " + "; ".join(parts) + ".") if parts else ""


def _discount_form_context(request, branches, branch, dt, mode, picks, sync=None):
    b = dt.branch or branch
    # Where the promotion runs besides here: the other branches, each marked
    # with whether it already has a copy and whether its till uses discounts.
    held = {c.branch_id for c in discounts.branch_copies(dt)} if dt.pk else set()
    others = list(catalog.other_branches(b)) if b else []
    if sync is None:
        chosen = held
        scope = ("all" if others and held >= {o.pk for o in others}
                 else "selected" if held else "this")
    else:
        scope, picked = sync
        chosen = {o.pk for o in picked}
    sync_rows = [{"branch": o, "checked": o.pk in chosen, "held": o.pk in held}
                 for o in others]
    products = list(_discount_products(b))
    categories = list(_discount_categories(b))
    # The cheapest active product per category, for the form's live
    # "cheapest combo" hint.  The server checks it again on save.
    cat_min = dict(Product.objects.filter(branch=b, active=True, category__isnull=False)
                   .values("category_id").annotate(m=Min("price"))
                   .values_list("category_id", "m")) if b else {}
    for c in categories:
        c.min_price = cat_min.get(c.id)
        c.key = f"c:{c.id}"
    for p in products:
        p.key = f"p:{p.id}"
    return {
        "active": "discounts",
        "branches": branches,
        "branch": branch,
        "dt": dt,
        "mode": mode,
        "kinds": DiscountType.KIND_CHOICES,
        "products": products,
        "categories": categories,
        "selected_ids": {str(i) for i in picks.products},
        "selected_cats": {str(i) for i in picks.categories},
        "cond_rows": picks.rows or [{"target": "", "min_qty": 1}],
        "today": timezone.localdate(),
        "sync_scope": scope,
        "sync_rows": sync_rows,
        "history": _discount_history(dt),
        "hide_dates": True,
        "qs": _filter_qs(request),
    }


@login_required
def discount_list(request):
    """Discount presets for the selected branch, and its till on/off switch."""
    branches, branch, _, _ = _common_filters(request)

    if request.method == "POST" and branch is not None:
        # The branch switch.  Admin-only: turning it on changes what every
        # cashier at that branch sees on the till.
        if getattr(request.user, "role", "") != "admin":
            messages.error(request, "Only an admin can switch discount types on or off.")
        else:
            branch.discount_types_enabled = request.POST.get("enabled") == "on"
            branch.save(update_fields=["discount_types_enabled"])
            messages.success(
                request,
                f"Discount types are now {'on' if branch.discount_types_enabled else 'off'} "
                f"at {branch.name}.")
        return redirect(reverse("backoffice:discount_list") + f"?{_filter_qs(request)}")

    qs = DiscountType.objects.filter(branch=branch) if branch else DiscountType.objects.none()
    qs = (qs.annotate(product_count=Count("products", distinct=True),
                      category_count=Count("categories", distinct=True))
          .select_related("free_product", "free_category")
          .prefetch_related("categories", "conditions__product", "conditions__category")
          .order_by("name"))

    # Status is derived, never stored: today's date against each promotion's
    # start/end dates (plus the manual pause).
    today = timezone.localdate()
    rows = list(qs)
    # Which other branches run each promotion, for the "also at" line.
    groups = {d.group_id for d in rows if d.group_id}
    elsewhere = {}
    for gid, name in (DiscountType.objects.filter(group_id__in=groups)
                      .exclude(branch=branch).order_by("branch__name")
                      .values_list("group_id", "branch__name")):
        elsewhere.setdefault(gid, []).append(name)
    for d in rows:
        d.current_status = d.status_on(today)
        d.also_at = elsewhere.get(d.group_id, [])

    context = {
        "active": "discounts",
        "branches": branches,
        "branch": branch,
        "discount_types": rows,
        "hide_dates": True,
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/discount_list.html", context)


@login_required
def discount_new(request):
    branches, branch, _, _ = _common_filters(request)
    dt = DiscountType(branch=branch, active=True, applies_to=DiscountType.APPLIES_ALL,
                      kind=DiscountType.KIND_PERCENT)
    picks = _DiscountPicks()
    sync = None
    if request.method == "POST":
        errors, picks = _apply_discount_form(dt, request.POST, branch)
        sync = _discount_sync_choice(request.POST, dt)
        if not errors:
            with transaction.atomic():
                dt.created_by = dt.updated_by = _staff_or_none(request.user)
                dt.save()
                picks.save(dt)
                report = discounts.sync_promotion(dt, sync[1])
            messages.success(request, f"“{dt.name}” added." + _discount_sync_message(report))
            for name, why in report["skipped"]:
                messages.warning(request, f"Not added at {name}: {why}.")
            return redirect(reverse("backoffice:discount_list") + f"?{_filter_qs(request)}")
        for e in errors:
            messages.error(request, e)
    return render(request, "backoffice/discount_form.html",
                  _discount_form_context(request, branches, branch, dt, "new", picks, sync))


@login_required
def discount_detail(request, discount_id):
    branches, branch, _, _ = _common_filters(request)
    dt = get_object_or_404(DiscountType, id=discount_id)
    picks = _DiscountPicks.of(dt)
    sync = None
    if request.method == "POST":
        errors, picks = _apply_discount_form(dt, request.POST, dt.branch or branch)
        sync = _discount_sync_choice(request.POST, dt)
        if not errors:
            with transaction.atomic():
                dt.updated_by = _staff_or_none(request.user)
                dt.save()
                # Only what the chosen mode uses is kept; switching a discount
                # from categories to products must not leave the old
                # categories (or combination rows) quietly attached.
                picks.save(dt)
                report = discounts.sync_promotion(dt, sync[1])
            messages.success(request, f"“{dt.name}” saved." + _discount_sync_message(report))
            for name, why in report["skipped"]:
                messages.warning(request, f"Not added at {name}: {why}.")
            return redirect(reverse("backoffice:discount_list") + f"?{_filter_qs(request)}")
        for e in errors:
            messages.error(request, e)
    return render(request, "backoffice/discount_form.html",
                  _discount_form_context(request, branches, branch, dt, "edit", picks, sync))


@login_required
def discount_delete(request, discount_id):
    """Delete this branch's copy of a promotion.  Copies at other branches
    keep running (untick them on the form to take it off there too).  Past
    sales keep its name — OrderItem snapshots it."""
    dt = get_object_or_404(DiscountType, id=discount_id)
    if request.method == "POST":
        still = [c.branch.name for c in discounts.branch_copies(dt) if c.branch]
        where = dt.branch.name if dt.branch else "this branch"
        dt.delete()
        messages.success(
            request, f"“{dt.name}” deleted at {where}."
            + (f" It is still running at {', '.join(sorted(still))}." if still else ""))
    return redirect(reverse("backoffice:discount_list") + f"?{_filter_qs(request)}")


# ─── Units ──────────────────────────────────────────────────────────────
def _apply_unit_form(unit, post, branch):
    """Pull fields out of a POSTed unit form onto a Unit instance.
    Shared by `unit_new` and `unit_detail`."""
    unit.branch = branch
    unit.name = (post.get("name") or "").strip()
    try:
        unit.order = int(post.get("order") or 0)
    except ValueError:
        unit.order = 0
    unit.active = post.get("active") == "on"
    return unit


@login_required
def unit_list(request):
    """Unit-of-measure management for the selected branch — mirrors the
    SilomPOS Unit page (order #, name, last update, active)."""
    branches, branch, _, _ = _common_filters(request)

    qs = Unit.objects.all()
    if branch:
        qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True))
    qs = qs.order_by("order", "name")

    context = {
        "active": "units",
        "branches": branches,
        "branch": branch,
        "units": qs,
        "hide_dates": True,
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/unit_list.html", context)


@login_required
def unit_detail(request, unit_id):
    """View + edit a single unit. POST saves and returns to the list."""
    branches, branch, _, _ = _common_filters(request)
    unit = get_object_or_404(Unit, id=unit_id)

    if request.method == "POST":
        _apply_unit_form(unit, request.POST, unit.branch or branch)
        unit.save()
        return redirect(reverse("backoffice:unit_list") + f"?{_filter_qs(request)}")

    context = {
        "active": "units",
        "branches": branches,
        "branch": branch,
        "unit": unit,
        "mode": "edit",
        "hide_dates": True,
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/unit_form.html", context)


@login_required
def unit_new(request):
    """Add a single unit. POST creates and returns to the list."""
    branches, branch, _, _ = _common_filters(request)

    if request.method == "POST":
        unit = Unit()
        _apply_unit_form(unit, request.POST, branch)
        unit.save()
        return redirect(reverse("backoffice:unit_list") + f"?{_filter_qs(request)}")

    context = {
        "active": "units",
        "branches": branches,
        "branch": branch,
        "unit": Unit(branch=branch, active=True),
        "mode": "new",
        "hide_dates": True,
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/unit_form.html", context)


@login_required
def unit_delete(request, unit_id):
    """Delete a unit."""
    unit = get_object_or_404(Unit, id=unit_id)
    if request.method == "POST":
        unit.delete()
    return redirect(reverse("backoffice:unit_list") + f"?{_filter_qs(request)}")


@login_required
def unit_create_ajax(request):
    """Create a unit on the fly from the product forms' inline 'add unit'
    control. Returns JSON {id, name} so the dropdowns can append + select it
    without a full page reload. Reuses an existing same-name unit if present
    so repeated adds don't pile up duplicates."""
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    name = (request.POST.get("name") or "").strip()
    if not name:
        return JsonResponse({"error": "Unit name is required"}, status=400)

    _branches, branch, _, _ = _common_filters(request)
    unit = (
        Unit.objects.filter(name__iexact=name)
        .filter(Q(branch=branch) | Q(branch__isnull=True))
        .first()
    )
    if unit is None:
        unit = Unit.objects.create(name=name, branch=branch, active=True)
    return JsonResponse({"id": str(unit.id), "name": unit.name})


# ─── Staff (POS PIN logins) ─────────────────────────────────────────────
# These manage the app-side `Staff` PIN logins, NOT the Django backoffice
# admins (those are separate `auth_user` accounts via createsuperuser). Each
# branch auto-gets one Admin + one Cashier on creation; this page lets you
# rename them, reset PINs, toggle active, or add more.
import uuid as _uuid


def _default_pin_for(role: str) -> str:
    return DEFAULT_ADMIN_PIN if role == "admin" else DEFAULT_CASHIER_PIN


# The till's PIN pad is exactly four digits (frontend/app/index.tsx:
# PIN_LENGTH = 4 — it refuses a fifth digit and auto-submits at four). A PIN
# saved here with any other length can never be typed on the till, so the
# staff member is silently locked out. Enforce the till's shape at the source
# rather than trusting the input's maxlength.
PIN_DIGITS = 4


def _clean_pin(raw: str):
    """Return (pin, error). ``pin`` is the cleaned 4-digit string, or "" when
    the field was left blank (caller decides what blank means). ``error`` is a
    message when what was typed is not a 4-digit PIN the till can accept."""
    pin = (raw or "").strip()
    if not pin:
        return "", None
    if not (pin.isdigit() and len(pin) == PIN_DIGITS):
        return pin, (
            f"The PIN must be exactly {PIN_DIGITS} digits — the till's PIN pad "
            f"accepts nothing else, so a longer or shorter PIN locks the "
            f"staff member out."
        )
    return pin, None


def _end_till_sessions(member, *, reason: str, actor=None) -> list[str]:
    """Sign `member` out of every till holding them. Returns where from.

    Deleting the row *is* the logout — it is the only thing the POS API
    checks (`bravepos.views.get_session`). Nothing can be pushed to a tablet,
    so the tablet finds out on its next call, which comes back 401.

    The branch names come back so the caller can say where it happened.
    "Signed out" means nothing to someone who cannot see which till was
    holding the account; "signed out of the till at Siam Paragon" does.

    Audited by hand: `BranchSession` is in `audit.NEVER`, because its
    `last_seen_at` is touched on every POS request and would bury the log in
    noise. That exclusion is about the automatic signal, not about this — an
    admin ending somebody's session is exactly the kind of thing the log is
    for, so it is recorded explicitly.
    """
    sessions = list(
        BranchSession.objects.select_related("branch").filter(staff=member)
    )
    if not sessions:
        return []

    BranchSession.objects.filter(id__in=[s.id for s in sessions]).delete()

    from bravepos import audit as _audit
    for session in sessions:
        _audit.record(
            "logout",
            model="BranchSession",
            object_id=str(session.id),
            object_label=f"{member.name} at {session.branch.name}",
            branch_id=session.branch_id,
            note=reason,
            actor=actor,
        )
    return [session.branch.name for session in sessions]


def _unique_staff_email(role: str, branch) -> str:
    """Generate a unique, non-colliding email for a new staff row. The app
    never uses it (PIN-only login) but the column is required + unique."""
    slug = (branch.code or "").strip() if branch else ""
    slug = slug or (str(branch.id)[:8] if branch else "shop")
    base = f"{role}.{slug}"
    email = f"{base}@rollingpinn.com"
    while Staff.objects.filter(email=email).exists():
        email = f"{base}.{_uuid.uuid4().hex[:6]}@rollingpinn.com"
    return email


@login_required
def staff_list(request):
    """POS staff (PIN logins) and the shifts they ran.

    Expected, counted, variance — in that order, every shift. A single short
    till is a bad night; the same person short twice in a week is a pattern,
    and only a list sorted this way makes the difference visible.
    """
    branches, branch, _, _ = _common_filters(request)

    staff = Staff.objects.prefetch_related("branches")
    if branch:
        staff = staff.filter(branches=branch)
    staff = staff.order_by("role", "name")

    days = int(request.GET.get("days") or 7)
    since = timezone.now() - timedelta(days=days)
    shifts = Shift.objects.filter(opened_at__gte=since).select_related("branch")
    if branch:
        shifts = shifts.filter(branch=branch)
    shifts = list(shifts.order_by("-opened_at")[:80])

    bills = {
        entry["shift"]: entry["n"]
        for entry in Order.objects.filter(shift__in=shifts).exclude(status="cancel")
        .values("shift").annotate(n=Count("id"))
    }
    sales = {
        entry["shift"]: entry["total"] or Decimal(0)
        for entry in Order.objects.filter(shift__in=shifts).exclude(status="cancel")
        .values("shift").annotate(total=Sum("total"))
    }

    for s in shifts:
        s.bills = bills.get(s.id, 0)
        s.sales = sales.get(s.id, Decimal(0))
        if s.status == "open":
            s.variance = None
            s.state_label, s.state_class = "Open", "t-low"
        elif s.actual_in_drawer is None:
            s.variance = None
            s.state_label, s.state_class = "Not counted", "t-out"
        else:
            s.variance = s.actual_in_drawer - s.expected_in_drawer
            if s.variance < 0:
                s.state_label, s.state_class = "Short", "t-red"
            elif s.variance > 0:
                s.state_label, s.state_class = "Over", "t-info"
            else:
                s.state_label, s.state_class = "Closed", "t-ok"

    # A shift open since before today holds cash nobody has counted.
    today_start, _ = _date_window(timezone.localdate(), timezone.localdate())
    stale = [s for s in shifts if s.status == "open" and s.opened_at < today_start]

    context = {
        "active": "staff",
        "page_title": "Staff & shifts",
        "branches": branches,
        "branch": branch,
        "staff_members": staff,
        "staff_count": staff.count(),
        "shifts": shifts,
        "stale_shifts": stale,
        "stale_cash": sum((s.total_sales_cash for s in stale), Decimal(0)),
        "open_count": sum(1 for s in shifts if s.status == "open"),
        "days": days,
        "day_options": [7, 14, 30],
        "hide_dates": True,
        "qs": _filter_qs(request, days=days if days != 7 else None),
    }
    return render(request, "backoffice/staff_list.html", context)


@login_required
def staff_detail(request, staff_id):
    """View + edit a single staff member. A blank PIN field keeps the
    current PIN; entering 4 digits resets it."""
    branches, branch, _, _ = _common_filters(request)
    member = get_object_or_404(Staff, id=staff_id)

    form_errors = []
    if request.method == "POST":
        member.name = (request.POST.get("name") or "").strip() or member.name
        member.role = request.POST.get("role") or member.role
        member.active = request.POST.get("active") == "on"
        pin, pin_error = _clean_pin(request.POST.get("pin"))
        if pin_error:
            form_errors.append(pin_error)
        elif pin:
            member.set_pin(pin)
        if not form_errors:
            member.save()
            # A PIN reset is nearly always someone who cannot get in — they
            # forgot it, or the one tablet allowed to hold their session is
            # flat, broken or somewhere else and the PIN pad keeps answering
            # "already signed in at X". Handing them a new PIN while that row
            # survives fixes nothing: the new PIN is refused on every other
            # device for the same reason the old one was. So the reset ends
            # the session too.
            #
            # Only this account's. A colleague signed in on another till at
            # the same branch is mid-sale and has nothing to do with whose
            # PIN was just changed.
            signed_out = _end_till_sessions(
                member, reason="till PIN reset", actor=request.user,
            ) if pin else []
            where = (
                f" They were signed out of the till at {', '.join(signed_out)}, "
                f"so the new PIN works on any device."
                if signed_out else ""
            )
            messages.success(request, f"{member.name}'s till login was saved.{where}")
            return redirect(reverse("backoffice:staff_list") + f"?{_filter_qs(request)}")

    context = {
        "active": "staff",
        "branches": branches,
        "branch": branch,
        "member": member,
        "mode": "edit",
        "form_errors": form_errors,
        # The delete panel explains *why* it is disabled rather than just
        # greying out, so it needs the same answer the view will give.
        "is_last_admin": _last_admin(member),
        "hide_dates": True,
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/staff_form.html", context)


@login_required
def staff_new(request):
    """Add a staff member to the selected branch. PIN defaults to the shared
    role default (admin 1234 / cashier 0000) when left blank."""
    branches, branch, _, _ = _common_filters(request)

    form_errors = []
    member = Staff(role="cashier", active=True)
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        role = request.POST.get("role") or "cashier"
        typed_pin, pin_error = _clean_pin(request.POST.get("pin"))
        pin = typed_pin or _default_pin_for(role)
        # Keep what was typed on screen if we have to re-render with an error.
        member = Staff(
            name=name or ("Admin" if role == "admin" else "Cashier"),
            role=role,
            active=request.POST.get("active") == "on",
        )
        if pin_error:
            form_errors.append(pin_error)
        else:
            member.email = _unique_staff_email(role, branch)
            member.set_pin(pin)
            member.set_password(_uuid.uuid4().hex)  # unused; PIN is the login
            member.save()
            if branch:
                member.branches.add(branch)
            messages.success(request, f"{member.name}'s till login was created.")
            return redirect(reverse("backoffice:staff_list") + f"?{_filter_qs(request)}")

    context = {
        "active": "staff",
        "branches": branches,
        "branch": branch,
        "member": member,
        "mode": "new",
        "form_errors": form_errors,
        "hide_dates": True,
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/staff_form.html", context)


def _last_admin(member) -> bool:
    """Is this the only admin account left that can still sign in?

    Losing it locks everyone out of Users, the audit log and payment — with
    no way back in through the product itself.
    """
    return not (
        Staff.objects.filter(role="admin", active=True)
        .exclude(id=member.id)
        .exists()
    )


@login_required
def staff_delete(request, staff_id):
    """Delete a till login for good.

    Open to any signed-in backoffice account, matching the rest of this page:
    the same form already lets a cashier rename a colleague, change their role
    and reset their PIN, so gating only Delete bought nothing but a panel that
    was invisible to the people doing the tidying up. `user_delete` stays
    admin-only — revoking someone's *web* access is a different question.

    The two refusals are what actually protects anything here, because both
    are unrecoverable from inside the product — you cannot delete the account
    you are signed in as, and you cannot delete the last admin.

    History survives. Bills and shifts store the cashier's name as text, not
    a foreign key, and audit rows keep `actor_label`; what is lost is the
    link from those rows back to a live account. The Status switch on the
    form is the reversible option and the form says so.
    """
    member = get_object_or_404(Staff, id=staff_id)
    if request.method != "POST":
        return redirect(reverse("backoffice:staff_list") + f"?{_filter_qs(request)}")

    if str(member.id) == str(request.user.id):
        messages.error(request, "You cannot delete the account you are signed in as.")
    elif member.role == "admin" and _last_admin(member):
        messages.error(
            request,
            f"{member.name} is the last admin. Make someone else an admin first, "
            f"or nobody will be able to reach Users, the audit log or payment.",
        )
    else:
        name = member.name
        member.delete()
        messages.success(request, f"{name} was deleted. Their past bills and shifts keep their name.")
    return redirect(reverse("backoffice:staff_list") + f"?{_filter_qs(request)}")


# ─── Stylesheet ─────────────────────────────────────────────────────────
# Served by Django rather than by `staticfiles`, deliberately.
#
# Production runs DJANGO_DEBUG=0, where `django.contrib.staticfiles` stops
# serving anything, and this deployment has no STATIC_ROOT and no nginx
# `location /static/` — the backoffice previously needed neither, because its
# only assets were CDN Bootstrap and an inline <style> block. Reaching for
# WhiteNoise or an nginx change to ship one stylesheet would put the whole
# backoffice's appearance behind a server-config step that a plain `git pull`
# and restart does not perform.
#
# So: read it once, hold it in memory, and hang a content hash off the URL so
# a changed file busts the cache the moment it deploys.
_CSS_PATH = Path(__file__).resolve().parent / "static" / "backoffice" / "app.css"
_css_cache: dict = {}


def _css_payload() -> tuple[bytes, str]:
    """(bytes, version) for the stylesheet, read from disk at most once.

    In DEBUG the file is re-read on every request so an edit shows up on
    reload; in production it is read once per process, which is what makes
    this cheaper than a static file served through the proxy anyway.
    """
    if not settings.DEBUG and "body" in _css_cache:
        return _css_cache["body"], _css_cache["version"]
    body = _CSS_PATH.read_bytes()
    version = hashlib.sha256(body).hexdigest()[:12]
    _css_cache.update(body=body, version=version)
    return body, version


def backoffice_css(request):
    body, version = _css_payload()
    if request.headers.get("If-None-Match") == f'"{version}"':
        return HttpResponseNotModified()

    response = HttpResponse(body, content_type="text/css")
    response["ETag"] = f'"{version}"'
    # Only promise immutability when the caller asked for the version we
    # actually have. `pos.rollingpinn.com` sits behind Cloudflare, and an
    # unconditional year-long `immutable` pinned the bare URL in its edge
    # cache against a file that changes every deploy — harmless only because
    # nothing links the bare URL. A stale-able URL has to stay revalidatable.
    if request.GET.get("v") == version:
        response["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        response["Cache-Control"] = "public, max-age=300"
    return response


_ICON_DIR = _CSS_PATH.parent
_ICONS = {"favicon": "favicon.png", "apple_touch_icon": "apple-touch-icon.png"}


def backoffice_icon(request, name):
    """The Brave POS app icon as the tab favicon / home-screen icon.

    Served like ``backoffice_css`` and for the same reason: no staticfiles
    pipeline in production. Unauthenticated so the login page gets it too.
    """
    body = (_ICON_DIR / _ICONS[name]).read_bytes()
    response = HttpResponse(body, content_type="image/png")
    response["Cache-Control"] = "public, max-age=86400"
    return response

@login_required
def product_image(request, product_id):
    """Serve one product's photo as its own cacheable resource.

    Same lesson as ``public_views.product_image``, relearned on the Catalogue:
    images are stored as base64 ``data:`` URIs, and pasting them into the list
    markup made that one page 574 KB against ~20 KB for its siblings. Because
    they are markup, nothing renders until all of it arrives and every visit
    re-downloads the lot.

    Served from here the browser fetches them lazily, in parallel, and keeps
    them. The ``?v=`` the template appends is a content hash, so a changed
    photo gets a new URL and this one caches immutably.
    """
    product = get_object_or_404(Product, id=product_id)
    raw = product.image_url or product.image_base64 or ""
    if not raw:
        raise Http404
    decoded = images.decode(raw)
    if decoded is None:
        # A hosted URL rather than a stored data: URI. Redirect instead of
        # 404ing: the catalogue defers the image columns (they are what made
        # the page enormous), so it cannot tell the two apart and always
        # points here.
        return HttpResponseRedirect(raw)

    data, mime = decoded
    etag = f'"{images.digest(raw)}"'
    if request.headers.get("If-None-Match") == etag:
        return HttpResponseNotModified()

    response = HttpResponse(data, content_type=mime)
    response["ETag"] = etag
    response["Content-Length"] = str(len(data))
    if request.GET.get("v") == images.digest(raw):
        response["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        response["Cache-Control"] = "public, max-age=300"
    return response


# ─── Customers ──────────────────────────────────────────────────────────
# Who comes back, and who used to. The till can look a customer up by phone;
# only the backoffice can see the whole history behind that name, which is
# what makes "lapsed" and "repeat rate" answerable at all.

# A customer with no bill in this many days counts as lapsed. Long enough
# that a fortnight's holiday doesn't flag someone, short enough that a
# monthly regular going quiet still surfaces while it's worth acting on.
LAPSED_DAYS = 30
# Someone is a regular once they've come back this often. Two visits is a
# coincidence; five is a habit.
REGULAR_VISITS = 5


def _customer_rows(tier: str, query: str):
    """Every customer with their order history rolled up.

    Shop-wide, and deliberately not filtered by the branch picker: a customer
    is one person across the whole business, so their spend is the sum of what
    they spent everywhere. Slicing this page by branch answered "what did this
    customer spend here", which read as their lifetime value and was not.
    Where they first registered is still shown, as the Home branch column.

    Aggregated in one query rather than per row — a shop with a thousand
    customers would otherwise issue a thousand COUNT/SUM pairs to paint one
    table. Cancelled bills are excluded from spend but the customer still
    counts as registered.
    """
    paid = Q(orders__status__in=["completed", "new", "preparing"])

    qs = Customer.objects.all()
    if query:
        qs = qs.filter(Q(name__icontains=query) | Q(phone__icontains=query)
                       | Q(last_name__icontains=query))

    qs = qs.annotate(
        visits=Count("orders", filter=paid, distinct=True),
        spend=Coalesce(Sum("orders__total", filter=paid),
                       Decimal(0), output_field=DecimalField()),
        first_seen=Min("orders__created_at", filter=paid),
        last_seen=Max("orders__created_at", filter=paid),
    ).select_related("branch")

    cutoff = timezone.now() - timedelta(days=LAPSED_DAYS)
    if tier == "members":
        qs = qs.filter(visits__gte=REGULAR_VISITS, last_seen__gte=cutoff)
    elif tier == "regulars":
        qs = qs.filter(visits__gte=2, last_seen__gte=cutoff)
    elif tier == "lapsed":
        qs = qs.filter(last_seen__lt=cutoff)
    elif tier == "new":
        qs = qs.filter(first_seen__gte=timezone.now() - timedelta(days=30))

    return qs.order_by("-spend", "name")


def _customer_tier(row, cutoff) -> tuple[str, str]:
    """(label, chip class) for a customer's standing. Lapsed wins over
    everything: a former regular who stopped coming is the fact worth
    surfacing, not that they were once a regular."""
    if row.last_seen is None:
        return "Never bought", "t-out"
    if row.last_seen < cutoff:
        return "Lapsed", "t-low"
    if row.visits >= REGULAR_VISITS:
        return "Member", "t-ok"
    if row.visits >= 2:
        return "Regular", "t-info"
    return "New", "t-purple"


@login_required
def customer_list(request):
    branches, branch, _, _ = _common_filters(request)
    tier = request.GET.get("tier") or "all"
    query = (request.GET.get("q") or "").strip()
    selected_id = request.GET.get("c") or ""

    rows = list(_customer_rows(tier, query)[:400])
    cutoff = timezone.now() - timedelta(days=LAPSED_DAYS)
    month_ago = timezone.now() - timedelta(days=30)

    for row in rows:
        row.tier_label, row.tier_class = _customer_tier(row, cutoff)
        row.avg_bill = (row.spend / row.visits) if row.visits else Decimal(0)

    # Headline figures come from the whole customer base, not the filtered
    # page — "1,284 registered" must not change because you typed a search.
    everyone = list(_customer_rows("all", ""))
    total = len(everyone)
    repeat = sum(1 for c in everyone if c.visits >= 2)
    lapsed = [c for c in everyone if c.last_seen is not None and c.last_seen < cutoff]
    new_this_month = sum(1 for c in everyone
                         if c.first_seen is not None and c.first_seen >= month_ago)
    matched_spend = sum((c.spend for c in everyone), Decimal(0))
    matched_visits = sum(c.visits for c in everyone)

    # How much of the shop's trade is attached to a name at all. A low share
    # is the reason to distrust every other number on this page, so it leads.
    start, end = _date_window(timezone.localdate() - timedelta(days=29),
                              timezone.localdate())
    recent = Order.objects.filter(created_at__range=(start, end)).exclude(status="cancel")
    recent_total = recent.count()
    recent_matched = recent.filter(customer__isnull=False).count()

    selected = None
    if rows:
        selected = next((r for r in rows if str(r.id) == selected_id), rows[0])
    if selected is not None:
        selected.recent_orders = list(
            selected.orders.exclude(status="cancel")
            .order_by("-created_at")[:6]
        )
        selected.week_bars = _customer_week_bars(selected)

    context = {
        "active": "customers",
        "page_title": "Customers",
        "branches": branches,
        "branch": branch,
        # The book is shop-wide, so a branch picker on this page would be a
        # control that silently does nothing.
        "hide_branch": True,
        "customers": rows,
        "selected": selected,
        "tier": tier,
        "tiers": [
            ("all", "All"), ("members", "Members"), ("regulars", "Regulars"),
            ("lapsed", "Lapsed"), ("new", "New this month"),
        ],
        "lapsed_days": LAPSED_DAYS,
        "query": query,
        "total_customers": total,
        "repeat_rate": round(repeat * 100 / total) if total else 0,
        "new_this_month": new_this_month,
        "lapsed_count": len(lapsed),
        "lapsed_spend": sum((c.spend for c in lapsed), Decimal(0)),
        "member_avg": (matched_spend / matched_visits) if matched_visits else Decimal(0),
        "matched_share": round(recent_matched * 100 / recent_total) if recent_total else 0,
        "hide_dates": True,
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/customer_list.html", context)


def _customer_week_bars(customer):
    """Visits per week for the last 12 weeks, as bar heights in percent.

    Lifetime spend tells you who mattered; twelve weeks of frequency tells
    you whether they still do. The last four weeks are marked `recent` so the
    template can colour them and a fading regular reads as a fade.
    """
    weeks = 12
    start = timezone.localdate() - timedelta(weeks=weeks - 1)
    start_dt, _ = _date_window(start, timezone.localdate())
    counts = [0] * weeks
    for created in (customer.orders.exclude(status="cancel")
                    .filter(created_at__gte=start_dt)
                    .values_list("created_at", flat=True)):
        index = (timezone.localtime(created).date() - start).days // 7
        if 0 <= index < weeks:
            counts[index] += 1
    peak = max(counts) or 1
    return [
        {"height": round(count * 100 / peak), "recent": i >= weeks - 4}
        for i, count in enumerate(counts)
    ]


@login_required
def customer_detail(request, customer_id):
    """Edit the name/phone the till matches on. Everything else about a
    customer is derived from their orders and is not editable here."""
    branches, branch, _, _ = _common_filters(request)
    customer = get_object_or_404(Customer, id=customer_id)

    if request.method == "POST":
        customer.name = (request.POST.get("name") or "").strip() or customer.name
        customer.last_name = (request.POST.get("last_name") or "").strip()
        # An empty box means no number on file, which the column stores as
        # NULL.  Customer.save() would fold "" anyway; spelled out here so the
        # form reads the way the column works.
        customer.phone = (request.POST.get("phone") or "").strip() or None
        customer.email = (request.POST.get("email") or "").strip()
        customer.save()
        return redirect(reverse("backoffice:customer_list")
                        + f"?{_filter_qs(request, c=str(customer.id))}")

    context = {
        "active": "customers",
        "page_title": customer.name,
        "branches": branches,
        "branch": branch,
        "hide_branch": True,
        "customer": customer,
        "hide_dates": True,
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/customer_form.html", context)


# ─── Shops & Branches ───────────────────────────────────────────────────
def _branch_topbar_context(request, remember=True):
    """Shared topbar context (branch dropdown + date inputs) for pages that
    don't actually filter by branch/date. Keeps the topbar consistent.

    These pages don't read the branch, but they still show the picker, and a
    picker that displays something other than the branch every other page is
    scoped to is a lie — so it shows, and can set, the same remembered choice.
    """
    today = timezone.localdate().isoformat()
    branches = list(Branch.objects.filter(active=True).order_by("name"))
    return {
        "branches": branches,
        "branch": _select_branch(request, branches, remember=remember),
        "date_from": today,
        "date_to": today,
    }


# ─── Payment credentials (backoffice-only) ──────────────────────────────────
# The POS app has no payment screen: a tablet on a shop counter has no business
# learning the merchant account or changing where money lands.  Both the
# per-branch config and the shop template are edited here and nowhere else.
MASK_PREFIX = "••••"
PAYMENT_SECRET_FIELDS = ("beam_api_key", "omise_secret_key")
PAYMENT_FEE_FIELDS = ("beam_card_fee_percent", "omise_fee_percent")


def mask_secret(value: str) -> str:
    """``••••1234`` for display — never the key itself.

    Short values are masked whole rather than leaking most of a short key.
    """
    v = (value or "").strip()
    if not v:
        return ""
    return MASK_PREFIX + v[-4:] if len(v) > 4 else MASK_PREFIX


def _apply_payment_form(obj, post) -> None:
    """Copy submitted payment fields onto a Branch or the Settings template.

    Secrets are write-only: the form renders a placeholder, never the stored
    key, so a blank submission means "leave it alone" rather than "wipe it".
    Clearing has to be asked for explicitly via the matching ``_clear`` box, so
    a user who tabs past the field can't silently un-configure a live branch.
    """
    obj.beam_merchant_id = (post.get("beam_merchant_id") or "").strip()
    obj.omise_public_key = (post.get("omise_public_key") or "").strip()
    obj.beam_sandbox = post.get("payment_mode") == "test"

    for field in PAYMENT_SECRET_FIELDS:
        if post.get(f"{field}_clear") == "on":
            setattr(obj, field, "")
            continue
        submitted = (post.get(field) or "").strip()
        if submitted and not submitted.startswith(MASK_PREFIX):
            setattr(obj, field, submitted)

    for field in PAYMENT_FEE_FIELDS:
        raw = (post.get(field) or "").strip()
        if not raw:
            continue
        try:
            setattr(obj, field, Decimal(raw))
        except (InvalidOperation, TypeError, ValueError):
            pass


def omise_key_kind(value: str) -> str | None:
    """``'test'`` / ``'live'`` / ``None`` for blank or unrecognised.

    Omise has no test/live switch of its own — the key prefix is the only thing
    that decides which environment a charge lands in.  That makes it the one
    credential we can check against the branch's declared lane.
    """
    v = (value or "").strip()
    if not v:
        return None
    if v.startswith(("pkey_test_", "skey_test_")):
        return "test"
    if v.startswith(("pkey_", "skey_")):
        return "live"
    return None


def payment_errors(obj) -> list[str]:
    """Lane/key mismatches serious enough to refuse the save.

    This exists because both halves of it happened for real on the live system:
    a branch marked Test was holding live Omise keys (so a "practice" terminal
    would have charged real cards), and the shop template was marked Live while
    holding test keys (so every branch created from it would have collected
    nothing).  Neither is visible by eye — the keys are masked on screen — so
    the check has to be at save time.

    Beam is deliberately not checked: its keys carry no test/live prefix, so
    there is nothing to compare the lane against.  Only Omise self-describes.
    """
    lane = "test" if obj.beam_sandbox else "live"
    errors = []

    for label, value in (("public", obj.omise_public_key),
                         ("secret", obj.omise_secret_key)):
        kind = omise_key_kind(value)
        if kind is None or kind == lane:
            continue
        if lane == "test":
            errors.append(
                f"This is a Test row, but the Omise {label} key is a LIVE key "
                f"(starts with pkey_/skey_). Omise ignores the Test setting — "
                f"the key prefix is what decides — so card payments here would "
                f"charge real cards. Use a {label} key starting with "
                f"pkey_test_/skey_test_, or clear it to switch Omise off here."
            )
        else:
            errors.append(
                f"This is a Live row, but the Omise {label} key is a TEST key "
                f"(starts with pkey_test_/skey_test_). Card payments would look "
                f"like they worked and collect no money. Use a live key."
            )

    pub, sec = omise_key_kind(obj.omise_public_key), omise_key_kind(obj.omise_secret_key)
    if pub and sec and pub != sec:
        errors.append(
            f"The Omise public key is a {pub.upper()} key but the secret key is "
            f"a {sec.upper()} key. They must be from the same account."
        )

    return errors


def can_edit_payment(user) -> bool:
    """Only admins may see or change where the shop's money lands.

    Cashiers keep the rest of the backoffice (see the Users role matrix), but
    payment is the one block where read and write are equally sensitive: the
    merchant id, the key tail and the Live/Test lane together tell you which
    account is collecting, and flipping a live branch to Test silently stops
    real money arriving while every screen still says "paid".
    """
    return getattr(user, "role", "") == "admin"


def _payment_context(obj, user=None) -> dict:
    """What the payment form needs to render without ever emitting a secret.

    ``pay_obj`` is whichever row is being edited (a Branch or the Settings
    template) so ``_payment_fields.html`` can serve both.

    For a non-admin this returns the flag alone — no object, no masks, no
    lane. The template hides the block, and there is nothing in the context
    for a view-source to find either.
    """
    if not can_edit_payment(user):
        return {"can_edit_payment": False}
    return {
        "can_edit_payment": True,
        "pay_obj": obj,
        "pay_beam_key_mask": mask_secret(obj.beam_api_key),
        "pay_omise_key_mask": mask_secret(obj.omise_secret_key),
        "pay_is_test": obj.beam_sandbox,
    }


def branch_errors(b: Branch) -> list[str]:
    """Blocking problems with the branch itself (payment is checked separately).

    Two of a branch's fields belong to one branch only, and the database says
    so for both — but a constraint can only 500.  That is what production did
    on 2026-08-18: "Khlong San" saved, the form was submitted a second time,
    the repeat hit ``bravepos_branch_name_key``, and the admin got an error
    page instead of being told the branch already existed.  Checking here is
    what lets the page name the branch already holding the value, with the
    rest of the form still filled in.

    The name is how a branch is chosen — on the POS login screen, in the
    branch picker, on the receipt — so two branches cannot answer to one.  The
    check is case-insensitive where the constraint is not: Postgres would take
    "khlong san" alongside "Khlong San", which is the same mistake with a
    worse ending, because afterwards nobody can tell the two tills apart.

    The POS ID is the machine number the Revenue Department issues to one till
    and it is printed on that till's tax invoices, so two branches cannot share
    one: whichever branch pasted it second would be filing its sales under the
    other's registration (see the ``branch_pos_id_unique_when_set``
    constraint).
    """
    errors = []
    name = (b.name or "").strip()
    if not name:
        # The input is `required`, so this is a hand-made POST — still a form
        # error rather than a 500, and blank would otherwise save once and
        # clash on the next one.
        errors.append("Branch name is required.")
    else:
        clash = Branch.objects.filter(name__iexact=name).exclude(pk=b.pk).first()
        if clash:
            errors.append(
                f'A branch called "{clash.name}" already exists. '
                "Branch names are how staff pick a till at login, so no two can "
                "share one — rename this branch, or edit the existing one instead."
            )
    if b.pos_id:
        clash = Branch.objects.filter(pos_id=b.pos_id).exclude(pk=b.pk).first()
        if clash:
            errors.append(
                f'POS ID "{b.pos_id}" already belongs to {clash.name}. '
                "Every branch needs its own Revenue Department machine number — "
                "check the number for this branch, or clear it on the other one first."
            )
    return errors


def _taken_pos_ids(b: Branch) -> str:
    """``{"<pos id>": "<branch name>"}`` for every *other* branch, as JSON.

    Feeds the POS ID field's as-you-type warning, so pasting a number that is
    already in use is caught while the cursor is still in the box rather than
    after a round trip.  ``branch_errors`` is still what refuses the save — this
    is only the fast half of the same check, and the values are branch numbers
    an admin can already read on the branch pages.
    """
    return json.dumps({
        row.pos_id: row.name
        for row in Branch.objects.exclude(pos_id="").exclude(pk=b.pk).only("pos_id", "name")
    })


def _taken_branch_names(b: Branch) -> str:
    """``{"<name lowercased>": "<name>"}`` for every *other* branch, as JSON.

    The name half of the warning ``_taken_pos_ids`` feeds.  Keys are folded to
    lower case because ``branch_errors`` compares that way, and a warning that
    disagreed with the check that refuses the save would be worse than none.
    """
    return json.dumps({
        row.name.strip().lower(): row.name
        for row in Branch.objects.exclude(pk=b.pk).only("name")
    })


# The dropdown entry that stands for "none of the above" — the shop being added
# has no row in the CRM yet, so make one.  A sentinel rather than a blank value
# because blank already means something else here: not linked at all.
CRM_CREATE_CHOICE = "__create__"


def _crm_context(b: Branch, post=None) -> dict:
    """State for the branch form's CRM dropdown, for one render.

    ``crm_enabled`` False keeps the whole panel out of the page, which is what
    a deployment with no ``CRM_API_KEY`` gets — the form is then exactly the
    form it was before any of this existed.

    A CRM that *is* configured but unreachable still renders the panel, with a
    warning where the list would be.  The branch stays editable and savable and
    its existing link survives the save untouched (``_apply_crm_choice``), so
    the CRM being down can never quietly unlink a branch.
    """
    # Two gates, and both are absences rather than settings: no key configured,
    # or a branch outside `CRM_BRANCHES`. Either way the whole panel — dropdown
    # and loyalty switch alike — is absent from the page, and the branch form is
    # exactly the form it was before any of this existed.
    if not (crm.is_configured() and crm.branch_allowed(b)):
        return {"crm_enabled": False}

    # What to show selected: the choice just submitted, when this render is a
    # refused save being handed back, so the admin doesn't have to find their
    # pick again.  Otherwise whatever the branch is actually linked to.
    if post is not None and "crm_branch" in post:
        selected = (post.get("crm_branch") or "").strip()
        neighborhood = (post.get("crm_neighborhood") or "").strip()
    else:
        selected = str(b.crm_branch_id) if b.crm_branch_id else ""
        neighborhood = ""

    try:
        rows, crm_error = crm.list_branches(), ""
    except crm.CrmError as exc:
        rows, crm_error = [], str(exc)

    # A CRM branch another POS branch already claims is shown but not
    # selectable, named so it is obvious why.  `_apply_crm_choice` refuses it
    # too — this is only the half that says so before the round trip.
    taken = {
        row.crm_branch_id: row.name
        for row in Branch.objects.filter(crm_branch_id__isnull=False)
        .exclude(pk=b.pk).only("crm_branch_id", "name")
    }

    choices = []
    for row in rows:
        try:
            crm_id = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        choices.append({
            "id": crm_id,
            "name": (row.get("name") or f"Branch {crm_id}").strip(),
            "neighborhood": (row.get("neighborhood") or "").strip(),
            "hours": (row.get("hours_display") or "").strip(),
            "inactive": row.get("active") is False,
            "taken_by": taken.get(crm_id, ""),
            "selected": str(crm_id) == selected,
        })

    return {
        "crm_enabled": True,
        "crm_branches": choices,
        "crm_error": crm_error,
        "crm_selected": selected,
        "crm_create_choice": CRM_CREATE_CHOICE,
        "crm_neighborhood": neighborhood,
        "crm_linked_id": b.crm_branch_id,
        # Whether this branch's till does loyalty.  Rendered from the POST on a
        # bounced save so a rejected form comes back with the box as the admin
        # left it, not as the database still has it.
        "crm_loyalty": (post.get("crm_loyalty") == "on" if post is not None
                        and "crm_panel" in post else b.crm_loyalty_enabled),
        # A branch linked to an id the CRM's list doesn't contain: either the
        # list didn't load, or that CRM branch is gone.  Saying so beats a
        # dropdown that silently sits on "Not linked" over a branch that is.
        "crm_linked_missing": bool(
            b.crm_branch_id and not any(c["id"] == b.crm_branch_id for c in choices)),
    }


def _apply_crm_choice(b: Branch, post) -> list[str]:
    """Apply the CRM panel to ``b``.  Returns blocking errors, like
    ``branch_errors``.

    Call this *last*, after the rest of the form has already validated.  It is
    the one part of a branch save that reaches outside our database: creating
    the CRM branch for a POS save that is then refused would leave a stray row
    over there, and every retry would leave another.
    """
    # The loyalty switch, read first because it is the panel's one field that
    # is still rendered when the branch list fails to load.  An unticked box
    # posts nothing at all, so `crm_panel` — a hidden marker the panel always
    # carries — is what tells "the admin unticked it" from "this form never
    # showed the panel", the second of which must leave the setting alone.
    # The same two gates `_crm_context` renders behind, applied again on the
    # way in: hiding the panel stops honest mistakes, but a hand-made POST
    # would otherwise still link — or start doing loyalty at — a branch the
    # rollout deliberately excludes.
    panel_live = crm.is_configured() and crm.branch_allowed(b)

    if panel_live and "crm_panel" in post:
        b.crm_loyalty_enabled = post.get("crm_loyalty") == "on"

    if not panel_live or "crm_branch" not in post:
        # No dropdown was rendered, so this POST says nothing about the link —
        # leave whatever the branch already has rather than clearing it.
        return []

    choice = (post.get("crm_branch") or "").strip()
    if not choice:
        b.crm_branch_id = None
        return []

    if choice == CRM_CREATE_CHOICE:
        try:
            b.crm_branch_id = crm.create_branch(
                b.name,
                neighborhood=(post.get("crm_neighborhood") or "").strip(),
                hours_display=f"{b.open_time}-{b.close_time}",
                active=b.active,
            )
        except crm.CrmError as exc:
            return [
                f"The CRM branch couldn't be created, so nothing was saved on "
                f"either side. {exc} Try again, or save this branch as not "
                f"linked and link it once the CRM is back."
            ]
        return []

    try:
        crm_id = int(choice)
    except ValueError:
        return ["Pick a CRM branch from the list, or choose to create a new one."]

    clash = Branch.objects.filter(crm_branch_id=crm_id).exclude(pk=b.pk).first()
    if clash:
        return [
            f'That CRM branch is already linked to "{clash.name}". One CRM shop '
            f"is one POS branch — unlink it there first, or create a new CRM "
            f"branch for this one."
        ]
    b.crm_branch_id = crm_id
    return []


def _apply_branch_form(b: Branch, post, user=None) -> Branch:
    b.name = (post.get("name") or "").strip()
    b.code = (post.get("code") or "").strip()
    b.tax_id = (post.get("tax_id") or "").strip()
    b.pos_id = (post.get("pos_id") or "").strip()
    b.address = (post.get("address") or "").strip()
    b.phone = (post.get("phone") or "").strip()
    b.logo_url = (post.get("logo_url") or "").strip()
    b.open_time = (post.get("open_time") or "09:00").strip() or "09:00"
    b.close_time = (post.get("close_time") or "22:00").strip() or "22:00"
    b.peak_account_code = (post.get("peak_account_code") or "BSV003").strip() or "BSV003"
    b.active = post.get("active") == "on"
    # Hiding the inputs only stops honest mistakes — a crafted POST would
    # otherwise still rewrite the keys, so the guard belongs here too.
    if can_edit_payment(user):
        _apply_payment_form(b, post)
    return b


@login_required
def branch_list(request):
    """Card-per-branch view. Shop-level info (logo, business type, hours)
    is sourced from the single Settings row; branch-level overrides
    (tax_id, address, phone) come from the Branch itself."""
    rows = list(Branch.objects.all().order_by("name"))
    # Surface each branch's payment lane and key ending on the list, so "which
    # branches are live, and which still need a key" is one glance rather than
    # opening every branch in turn. Admins only — the lane and the key tail
    # are the same sensitive pair the branch form gates, so a cashier gets
    # neither the column nor the values behind it.
    show_payment = can_edit_payment(request.user)
    for row in rows:
        row.beam_key_mask = mask_secret(row.beam_api_key) if show_payment else ""
        row.omise_key_mask = mask_secret(row.omise_secret_key) if show_payment else ""

    # Comparison is the entire reason this page exists, so the cards carry
    # trade for the selected window and the one before it, not just settings.
    today = timezone.localdate()
    dfrom = _parse_date(request.GET.get("from"), today.replace(day=1))
    dto = _parse_date(request.GET.get("to"), today)
    if dto < dfrom:
        dfrom, dto = dto, dfrom
    span = (dto - dfrom).days + 1
    start, end = _date_window(dfrom, dto)
    prev_start, prev_end = _date_window(
        dfrom - timedelta(days=span), dfrom - timedelta(days=1))

    def _totals(window_start, window_end):
        return {
            entry["branch"]: entry
            for entry in Order.objects
            .filter(created_at__range=(window_start, window_end))
            .exclude(status="cancel")
            .values("branch")
            .annotate(sales=Sum("total"), bills=Count("id"))
        }

    now_totals, was_totals = _totals(start, end), _totals(prev_start, prev_end)
    open_shifts = {s.branch_id: s for s in Shift.objects.filter(status="open")}
    staff_counts = {
        entry["branches"]: entry["n"]
        for entry in Staff.objects.filter(active=True).values("branches").annotate(n=Count("id"))
    }
    # Cash variance: counted minus expected, summed over closed shifts. Sitting
    # next to margin and waste is how you tell a struggling branch from a
    # leaking one, so it is a first-class column rather than a shift detail.
    variance = {
        entry["branch"]: (entry["counted"] or Decimal(0)) - (entry["expected"] or Decimal(0))
        for entry in Shift.objects
        .filter(status="closed", closed_at__range=(start, end),
                actual_in_drawer__isnull=False)
        .values("branch")
        .annotate(counted=Sum("actual_in_drawer"), expected=Sum("expected_in_drawer"))
    }

    peak = max((v["sales"] or Decimal(0) for v in now_totals.values()),
               default=Decimal(0)) or Decimal(1)
    for row in rows:
        current = now_totals.get(row.id, {})
        row.sales = current.get("sales") or Decimal(0)
        row.bills = current.get("bills") or 0
        row.avg_bill = (row.sales / row.bills) if row.bills else Decimal(0)
        row.share = float(row.sales / peak * 100)
        row.delta = _pct_change(row.sales, (was_totals.get(row.id) or {}).get("sales"))
        row.shift = open_shifts.get(row.id)
        row.staff_count = staff_counts.get(row.id, 0)
        row.variance = variance.get(row.id, Decimal(0))
    rows.sort(key=lambda r: r.sales, reverse=True)

    short = [r for r in rows if r.variance < 0]

    context = {
        "active": "branches",
        "page_title": "Branches",
        "branches_all": rows,
        "settings": Settings.objects.first(),
        "total_sales": sum((r.sales for r in rows), Decimal(0)),
        "short_branches": short,
        "can_edit_payment": show_payment,
        "span_days": span,
        **_branch_topbar_context(request),
        # The window actually used, so the header reflects the picker.
        "date_from": dfrom.isoformat(),
        "date_to": dto.isoformat(),
        "hide_branch": True,
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/branch_list.html", context)


@login_required
def branch_detail(request, branch_id):
    b = get_object_or_404(Branch, id=branch_id)
    errors, form_errors = [], []
    if request.method == "POST":
        _apply_branch_form(b, request.POST, request.user)
        errors = payment_errors(b)
        form_errors = branch_errors(b)
        # Only once everything local is good: creating a CRM branch is not
        # something we can take back if the save is then refused.
        if not errors and not form_errors:
            form_errors = _apply_crm_choice(b, request.POST)
        if not errors and not form_errors:
            b.save()
            return redirect("backoffice:branch_list")
        # Fall through and re-render with the submitted values still in place,
        # so the fix is one edit rather than retyping the whole form.
    context = {
        "active": "branches",
        "branch_obj": b,
        "mode": "edit",
        "payment_errors": errors,
        "form_errors": form_errors,
        "taken_pos_ids": _taken_pos_ids(b),
        "taken_names": _taken_branch_names(b),
        **_payment_context(b, request.user),
        **_crm_context(b, request.POST if request.method == "POST" else None),
        **_branch_topbar_context(request),
    }
    return render(request, "backoffice/branch_form.html", context)


@login_required
def branch_new(request):
    errors, form_errors = [], []
    if request.method == "POST":
        b = Branch()
        _apply_branch_form(b, request.POST, request.user)
        # Validate against the config the branch will actually end up with —
        # seeding fills the write-only key fields the form leaves blank, and it
        # is those seeded keys that have to match the lane.
        seed_branch_payment(b)
        errors = payment_errors(b)
        form_errors = branch_errors(b)
        # Last, and only if the branch is otherwise good — see
        # `_apply_crm_choice`. A CRM branch minted for a save that then bounces
        # off a duplicate name is a row nobody can see from here to clean up.
        if not errors and not form_errors:
            form_errors = _apply_crm_choice(b, request.POST)
        if not errors and not form_errors:
            b.save()
            return redirect("backoffice:branch_list")
        context = {
            "active": "branches",
            "branch_obj": b,
            "mode": "new",
            "payment_errors": errors,
            "form_errors": form_errors,
            "taken_pos_ids": _taken_pos_ids(b),
            "taken_names": _taken_branch_names(b),
            **_payment_context(b, request.user),
            **_crm_context(b, request.POST),
            **_branch_topbar_context(request),
        }
        return render(request, "backoffice/branch_form.html", context)
    # Show the payment config this branch is about to inherit rather than an
    # empty form — the same seeding the pre_save signal will do on save, run
    # early on a throwaway instance purely so the page tells the truth.
    blank = Branch(active=True)
    seed_branch_payment(blank)
    context = {
        "active": "branches",
        "branch_obj": blank,
        "mode": "new",
        "payment_errors": errors,
        "form_errors": form_errors,
        "taken_pos_ids": _taken_pos_ids(blank),
        "taken_names": _taken_branch_names(blank),
        **_payment_context(blank, request.user),
        **_crm_context(blank),
        **_branch_topbar_context(request),
    }
    return render(request, "backoffice/branch_form.html", context)


# ─── Shop settings (singleton) ──────────────────────────────────────────
def _get_or_create_settings() -> Settings:
    obj, _ = Settings.objects.get_or_create(id="shop")
    return obj


@login_required
def shop_settings(request):
    """SilomPOS-style Shop page. Per-branch fields (logo, tax_id, phone,
    address) live on the selected Branch (driven by the topbar selector);
    shop-wide fields (business type, currency, tax %, hours) live on the
    singleton Settings row that the POS app's `/api/settings` endpoint
    returns. Both update in one form."""
    branches = list(Branch.objects.filter(active=True).order_by("name"))
    branch = _select_branch(request, branches)

    s = _get_or_create_settings()

    if request.method == "POST":
        # ── Shop-wide (singleton Settings) ───────────────────────────
        s.shop_name = (request.POST.get("shop_name") or "").strip() or s.shop_name
        s.business_type = (request.POST.get("business_type") or "").strip() or s.business_type
        s.company_name = (request.POST.get("company_name") or "").strip()
        s.currency = (request.POST.get("currency") or "THB").strip() or "THB"
        s.open_time = (request.POST.get("open_time") or s.open_time).strip()
        s.close_time = (request.POST.get("close_time") or s.close_time).strip()

        tax_mode = request.POST.get("tax_mode") or "exclusive"
        s.tax_mode = "inclusive" if tax_mode == "inclusive" else "exclusive"
        try:
            s.tax_percent = Decimal(request.POST.get("tax_percent") or "0")
        except Exception:
            pass
        try:
            s.service_charge_percent = Decimal(request.POST.get("service_charge") or "0")
        except Exception:
            pass
        s.service_charge_enabled = s.service_charge_percent > 0
        # Payment credentials on the Settings row are a *template*: they seed
        # branches created from here on and are never read at charge time, so
        # editing them cannot disturb a branch that is already trading.
        # Admins only — see `can_edit_payment`. A cashier POSTing this form
        # saves the shop fields and leaves the payment template untouched.
        if can_edit_payment(request.user):
            _apply_payment_form(s, request.POST)
        errors = payment_errors(s) if can_edit_payment(request.user) else []
        if errors:
            # Refuse the whole page rather than saving the non-payment half —
            # a template that says Live while holding test keys silently mints
            # branches that collect no money.
            context = {
                "active": "shop_settings", "settings": s, "branch_obj": branch,
                "branches": branches, "branch": branch, "hide_dates": True,
                "payment_errors": errors,
                **_payment_context(s, request.user),
            }
            return render(request, "backoffice/shop_settings.html", context)
        s.save()

        # ── Per-branch (selected Branch) ─────────────────────────────
        if branch:
            branch.name = (request.POST.get("branch_name") or branch.name).strip() or branch.name
            branch.tax_id = (request.POST.get("tax_id") or "").strip()
            branch.phone = (request.POST.get("phone") or "").strip()
            branch.address_line_1 = (request.POST.get("address_line_1") or "").strip()
            branch.address_line_2 = (request.POST.get("address_line_2") or "").strip()
            branch.address = "\n".join(
                line for line in [branch.address_line_1, branch.address_line_2] if line
            )
            branch.logo_url = (request.POST.get("logo_url") or "").strip()
            branch.save()

        qs = f"?branch={branch.id}" if branch else ""
        return redirect(f"{reverse('backoffice:shop_settings')}{qs}")

    context = {
        "active": "shop_settings",
        "settings": s,
        "branch_obj": branch,
        # Topbar branch selector — no date scoping on this page.
        "branches": branches,
        "branch": branch,
        "hide_dates": True,
        **_payment_context(s, request.user),
    }
    return render(request, "backoffice/shop_settings.html", context)


# ─── Backoffice users ───────────────────────────────────────────────────
# These are the *web* logins (username OR email + password), as opposed to
# the Staff page above which manages in-app PIN logins. Both live in the same
# `bravepos_staff` table — `backoffice_access` is what separates them.
import secrets as _secrets

# Ambiguous glyphs (0/O, 1/l/I) removed so a generated password survives being
# read aloud or copied off a screen.
_PASSWORD_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_password(length: int = 14) -> str:
    return "".join(_secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


def _user_form_errors(post, instance=None) -> list[str]:
    """Validate a submitted user form. Returns human-readable problems; an
    empty list means the form is good to save."""
    errors = []
    name = (post.get("name") or "").strip()
    username = (post.get("username") or "").strip()
    email = (post.get("email") or "").strip()
    password = post.get("password") or ""

    if not name:
        errors.append("Name is required.")
    if not username and not email:
        errors.append("Give the account a username, an email, or both — it needs at least one to sign in with.")

    clash = Staff.objects.all()
    if instance is not None and instance.pk:
        clash = clash.exclude(pk=instance.pk)
    if username and clash.filter(username__iexact=username).exists():
        errors.append(f"Username “{username}” is already taken.")
    if email and clash.filter(email__iexact=email).exists():
        errors.append(f"Email “{email}” is already in use.")
    # An identifier that matches the *other* column on a different row would
    # make sign-in ambiguous, so block that too.
    if username and clash.filter(email__iexact=username).exists():
        errors.append(f"“{username}” is already another account's email address.")
    if email and clash.filter(username__iexact=email).exists():
        errors.append(f"“{email}” is already another account's username.")

    if password and len(password) < 10:
        errors.append("Password must be at least 10 characters.")
    return errors


def _user_role(value) -> str:
    """A submitted Users-page role, defaulting to Manager (stored "cashier")."""
    return value if value in ("admin", "viewer") else "cashier"


def _apply_user_form(member: Staff, post, *, is_new: bool) -> tuple[Staff, str]:
    """Copy a submitted user form onto a Staff instance.

    Returns the instance plus the plaintext password when one was set or
    generated — the caller shows it once and never stores it.

    ``is_new`` is passed explicitly rather than inferred from ``member.pk``:
    Staff's primary key is a UUID with a `default`, so a brand-new unsaved
    instance already has one and `pk` can't tell the two apart.
    """
    member.name = (post.get("name") or "").strip()
    member.username = (post.get("username") or "").strip() or None
    member.role = _user_role(post.get("role"))
    member.active = post.get("active") == "on"
    member.backoffice_access = True

    email = (post.get("email") or "").strip()
    if email:
        member.email = email
    elif not member.email:
        # Email is a required unique column but the account may sign in by
        # username alone; synthesise a non-routable placeholder.
        member.email = f"{member.username or _uuid.uuid4().hex[:8]}@users.noreply.rollingpinn.com"

    plaintext = post.get("password") or ""
    if not plaintext and is_new:
        plaintext = generate_password()
    if plaintext:
        member.set_password(plaintext)
    return member, plaintext


def _user_context(request, member, mode, errors=()):
    return {
        "active": "users",
        "member": member,
        "mode": mode,
        "errors": list(errors),
        "hide_dates": True,
        **_branch_topbar_context(request),
    }


@admin_required
def user_list(request):
    """Backoffice web logins — the accounts that can sign in here."""
    users = (
        Staff.objects.filter(backoffice_access=True)
        .prefetch_related("branches")
        .order_by("-role", "name")
    )
    # Credentials handed off exactly once, immediately after create/reset.
    # Held in the session rather than a URL so they never hit a proxy log or
    # the browser's history.
    issued = request.session.pop("issued_credentials", None)
    context = {
        "active": "users",
        "users": users,
        "issued": issued,
        "hide_dates": True,
        **_branch_topbar_context(request),
    }
    return render(request, "backoffice/user_list.html", context)


@admin_required
def user_new(request):
    if request.method == "POST":
        errors = _user_form_errors(request.POST)
        if errors:
            draft = Staff(
                name=(request.POST.get("name") or "").strip(),
                username=(request.POST.get("username") or "").strip() or None,
                email=(request.POST.get("email") or "").strip(),
                role=_user_role(request.POST.get("role")),
                active=request.POST.get("active") == "on",
            )
            return render(request, "backoffice/user_form.html",
                          _user_context(request, draft, "new", errors))

        member, plaintext = _apply_user_form(Staff(), request.POST, is_new=True)
        member.save()
        request.session["issued_credentials"] = {
            "name": member.name,
            "username": member.username or "",
            "email": member.email,
            "password": plaintext,
            "reason": "created",
        }
        return redirect("backoffice:user_list")

    draft = Staff(role="cashier", active=True)
    return render(request, "backoffice/user_form.html",
                  _user_context(request, draft, "new"))


@admin_required
def user_detail(request, staff_id):
    member = get_object_or_404(Staff, id=staff_id)

    if request.method == "POST":
        errors = _user_form_errors(request.POST, instance=member)
        # Don't let an admin strip their own admin role or deactivate
        # themselves — that's the one edit with no way back through the UI.
        if str(member.id) == str(request.user.id):
            if request.POST.get("role") != "admin":
                errors.append("You can't remove your own Admin role — ask another admin to do it.")
            if request.POST.get("active") != "on":
                errors.append("You can't deactivate your own account.")
        if errors:
            return render(request, "backoffice/user_form.html",
                          _user_context(request, member, "edit", errors))

        member, plaintext = _apply_user_form(member, request.POST, is_new=False)
        member.save()
        if plaintext:
            request.session["issued_credentials"] = {
                "name": member.name,
                "username": member.username or "",
                "email": member.email,
                "password": plaintext,
                "reason": "password reset",
            }
        return redirect("backoffice:user_list")

    return render(request, "backoffice/user_form.html",
                  _user_context(request, member, "edit"))


@admin_required
def user_reset_password(request, staff_id):
    """Generate a fresh password and show it once on the list page."""
    member = get_object_or_404(Staff, id=staff_id)
    if request.method == "POST":
        plaintext = generate_password()
        member.set_password(plaintext)
        member.save(update_fields=["password_hash"])
        request.session["issued_credentials"] = {
            "name": member.name,
            "username": member.username or "",
            "email": member.email,
            "password": plaintext,
            "reason": "password reset",
        }
    return redirect("backoffice:user_list")


@admin_required
def user_delete(request, staff_id):
    """Revoke backoffice access.

    The Staff row survives — deleting it would orphan every audit entry,
    shift and order that points at it. Clearing `backoffice_access` and the
    password is what actually locks the account out of this site; any in-app
    PIN login it also has keeps working.
    """
    member = get_object_or_404(Staff, id=staff_id)
    if request.method != "POST":
        return redirect("backoffice:user_list")

    # The page footer promises both of these refusals; until now only the
    # first was enforced, so revoking the last admin would have locked
    # everyone out of Users and the audit log with the UI still claiming it
    # could not happen.
    if str(member.id) == str(request.user.id):
        messages.error(request, "You cannot revoke the account you are signed in as.")
    elif member.role == "admin" and _last_admin(member):
        messages.error(
            request,
            f"{member.name} is the last admin. Promote someone else first, or "
            f"nobody will be able to reach Users or the audit log.",
        )
    else:
        member.backoffice_access = False
        member.username = None
        member.set_password(_uuid.uuid4().hex)
        member.save(update_fields=["backoffice_access", "username", "password_hash"])
        messages.success(
            request,
            f"{member.name} can no longer sign in here. Any till PIN they have still works.",
        )
    return redirect("backoffice:user_list")


# ─── Audit log ──────────────────────────────────────────────────────────
AUDIT_MODEL_CHOICES = [
    "Staff", "Branch", "Settings", "Product", "Category", "Unit",
    "DrawerCategory", "StockOutReason", "Order", "OrderItem", "SelfOrder",
    "StockDocument", "StockDocumentItem", "StockMovement", "Shift",
    "ShiftMovement", "Customer", "DiscountType",
]


def _audit_value(value) -> str:
    """One side of an audit diff, as text a human reads.

    A field set to null renders as "empty", not "None": the diff is meant to
    be read at a glance, and a stray Python repr in the middle of a money
    trail is exactly the kind of thing that makes a log look untrustworthy.
    Empty string and null are distinct events, so they are worded distinctly.
    """
    if value is None:
        return "empty"
    if value == "":
        return "blank"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _audit_qs(request):
    """Filtered audit rows for the current query string."""
    qs = AuditLog.objects.select_related("actor", "branch")

    dfrom = request.GET.get("from") or ""
    dto = request.GET.get("to") or ""
    if dfrom:
        try:
            start = datetime.combine(date.fromisoformat(dfrom), time.min)
            qs = qs.filter(at__gte=timezone.make_aware(start))
        except ValueError:
            pass
    if dto:
        try:
            end = datetime.combine(date.fromisoformat(dto), time.max)
            qs = qs.filter(at__lte=timezone.make_aware(end))
        except ValueError:
            pass

    action = request.GET.get("action") or ""
    if action:
        qs = qs.filter(action=action)

    model = request.GET.get("model") or ""
    if model:
        qs = qs.filter(model=model)

    actor = request.GET.get("actor") or ""
    if actor:
        qs = qs.filter(actor_id=actor)

    branch = request.GET.get("branch") or ""
    if branch:
        qs = qs.filter(branch_id=branch)

    search = (request.GET.get("q") or "").strip()
    if search:
        qs = qs.filter(
            Q(object_label__icontains=search)
            | Q(actor_label__icontains=search)
            | Q(note__icontains=search)
            | Q(path__icontains=search)
        )
    return qs.order_by("-at")


def _audit_filter_qs(request) -> str:
    """Re-encode the active filters so pagination links keep them."""
    keys = ("from", "to", "action", "model", "actor", "branch", "q")
    parts = [f"{k}={request.GET.get(k)}" for k in keys if request.GET.get(k)]
    return "&".join(parts)


@admin_required
def audit_log(request):
    """Every recorded change, newest first."""
    rows = _audit_qs(request)
    paginator = Paginator(rows, 100)
    page = paginator.get_page(request.GET.get("page"))

    # `changes` holds two shapes: {"field": {"from": …, "to": …}} for an
    # update, and a flat field map for a create or delete. Flattening them
    # here means the template renders one shape instead of guessing, and a
    # failed lookup in a template is silent — exactly the wrong place for it.
    for entry in page.object_list:
        diffs = []
        for field, change in (entry.changes or {}).items():
            if isinstance(change, dict) and ("from" in change or "to" in change):
                diffs.append({"field": field, "old": _audit_value(change.get("from")),
                              "new": _audit_value(change.get("to")), "is_diff": True})
            else:
                diffs.append({"field": field, "old": "",
                              "new": _audit_value(change), "is_diff": False})
        entry.visible_changes = diffs[:3]
        entry.hidden_changes = max(0, len(diffs) - 3)

    context = {
        "active": "audit",
        "page_title": "Audit log",
        "page_obj": page,
        "paginator": paginator,
        "entries": page.object_list,
        "total": paginator.count,
        "action_choices": AuditLog.ACTION_CHOICES,
        "model_choices": AUDIT_MODEL_CHOICES,
        "actors": Staff.objects.filter(audit_entries__isnull=False).distinct().order_by("name"),
        "selected": {
            "from": request.GET.get("from") or "",
            "to": request.GET.get("to") or "",
            "action": request.GET.get("action") or "",
            "model": request.GET.get("model") or "",
            "actor": request.GET.get("actor") or "",
            "branch": request.GET.get("branch") or "",
            "q": request.GET.get("q") or "",
        },
        "audit_qs": _audit_filter_qs(request),
        # This page filters on its own terms — action, record type, actor —
        # and its branch picker lives in that same form. Leaving the header's
        # picker on would give two controls for one field, where using the
        # header one would silently drop every other filter.
        "hide_dates": True,
        "hide_branch": True,
        **_branch_topbar_context(request, remember=False),
    }
    return render(request, "backoffice/audit_log.html", context)


def _audit_change_summary(entry) -> str:
    """Flatten a changes dict into one readable cell / CSV column."""
    changes = entry.changes or {}
    if not changes:
        return entry.note or ""
    parts = []
    for field, value in list(changes.items())[:12]:
        if isinstance(value, dict) and "from" in value:
            parts.append(f"{field}: {value.get('from')} → {value.get('to')}")
        else:
            parts.append(f"{field}={value}")
    if len(changes) > 12:
        parts.append(f"… +{len(changes) - 12} more")
    return "; ".join(parts)


@admin_required
def audit_log_export(request):
    """CSV of the currently filtered rows. The export itself is audited."""
    rows = _audit_qs(request)
    response, writer = _csv_response("audit-log.csv")
    writer.writerow([
        "When", "Actor", "Role", "Action", "Model", "Object", "Changes",
        "Branch", "Source", "Method", "Path", "IP",
    ])
    for entry in rows.iterator(chunk_size=500):
        writer.writerow([
            timezone.localtime(entry.at).strftime("%Y-%m-%d %H:%M:%S"),
            entry.actor_label or "—",
            entry.actor_role or "",
            entry.get_action_display(),
            entry.model,
            entry.object_label,
            _audit_change_summary(entry),
            entry.branch.name if entry.branch else "",
            entry.source,
            entry.method,
            entry.path,
            entry.ip or "",
        ])

    from bravepos import audit as _audit
    _audit.record(
        "export", model="AuditLog", object_label="audit log CSV",
        note=f"filters: {_audit_filter_qs(request) or 'none'}",
        actor=request.user if request.user.is_authenticated else None,
    )
    return response


# ─── App releases (the APK staff install on a till) ─────────────────────────
# Listing is visible to any signed-in account — knowing which build is live is
# ordinary operational information. Publishing one is `admin_required`: the
# page it feeds is public, and what lands there is what a shop installs.
def _app_release_form_errors(post) -> dict:
    errors = {}

    if not appdist.parse_drive_file_id(post.get("drive_link", "")):
        errors["drive_link"] = (
            "Paste the Google Drive share link for the APK (or just its file ID)."
        )

    if not (post.get("version") or "").strip():
        errors["version"] = "Version is required — the number in app.json, e.g. 1.4.1."

    code = (post.get("version_code") or "").strip()
    if code and not code.isdigit():
        errors["version_code"] = "Build number must be a whole number (Android's versionCode)."

    size = (post.get("size_mb") or "").strip()
    if size:
        try:
            if float(size) < 0:
                raise ValueError
        except ValueError:
            errors["size_mb"] = "Size must be a number of megabytes, e.g. 162.8."

    return errors


def _app_release_form_values(release=None, post=None) -> dict:
    """What the form fields should contain.

    Two sources: a saved row (opening the form) or the raw POST (re-rendering
    a rejected one). The rejected case has to come back as typed — the values
    that failed are exactly the ones that will not coerce, so there is nothing
    to read them back off a model instance.
    """
    if post is not None:
        return {
            "drive_link": (post.get("drive_link") or "").strip(),
            "version": (post.get("version") or "").strip(),
            "version_code": (post.get("version_code") or "").strip(),
            "build_id": (post.get("build_id") or "").strip(),
            "size_mb": (post.get("size_mb") or "").strip(),
            "notes": (post.get("notes") or "").strip(),
            "published": post.get("published") == "on",
            "published_on": (post.get("published_on") or "").strip(),
        }

    if release is None or release.pk is None:
        return {
            "drive_link": "", "version": "", "version_code": "", "build_id": "",
            "size_mb": "", "notes": "", "published": True,
            "published_on": timezone.localdate().isoformat(),
        }

    return {
        "drive_link": appdist.preview_url(release),
        "version": release.version,
        "version_code": release.version_code or "",
        "build_id": release.build_id,
        "size_mb": release.size_mb or "",
        "notes": release.notes,
        "published": release.published,
        "published_on": timezone.localtime(release.published_at).date().isoformat(),
    }


def _apply_app_release_form(release, post):
    release.drive_file_id = appdist.parse_drive_file_id(post.get("drive_link", ""))
    release.version = (post.get("version") or "").strip()
    release.version_code = int((post.get("version_code") or "0").strip() or 0)
    release.build_id = appdist.parse_build_id(post.get("build_id", ""))
    release.notes = (post.get("notes") or "").strip()
    release.published = post.get("published") == "on"

    size = (post.get("size_mb") or "").strip()
    release.size_bytes = int(round(float(size) * 1024 * 1024)) if size else 0

    published_on = (post.get("published_on") or "").strip()
    if published_on:
        try:
            day = date.fromisoformat(published_on)
        except ValueError:
            pass
        else:
            # Keep the time already on the row (or now, for a new one) so two
            # builds dated the same day still sort in the order they landed.
            existing = release.published_at or timezone.now()
            release.published_at = timezone.make_aware(
                datetime.combine(day, timezone.localtime(existing).time()),
                timezone.get_current_timezone(),
            )
    return release


def _app_release_context(request, release, mode, form, errors=None):
    return {
        "active": "app_releases",
        "release": release,
        "form": form,
        "mode": mode,
        "errors": errors or {},
        "hide_dates": True,
        "install_url": appdist.install_page_url(),
        **_branch_topbar_context(request),
    }


@login_required
def app_release_list(request):
    """Every build ever listed, newest first. The topmost published one is what
    /app/ offers — see ``AppRelease.current()``."""
    releases = list(AppRelease.objects.all())
    current = AppRelease.current()
    context = {
        "active": "app_releases",
        "releases": releases,
        "current_id": current.id if current else None,
        "install_url": appdist.install_page_url(),
        "hide_dates": True,
        **_branch_topbar_context(request),
    }
    return render(request, "backoffice/app_release_list.html", context)


@admin_required
def app_release_new(request):
    if request.method == "POST":
        errors = _app_release_form_errors(request.POST)
        if errors:
            return render(request, "backoffice/app_release_form.html",
                          _app_release_context(
                              request, None, "new",
                              _app_release_form_values(post=request.POST), errors))

        release = _apply_app_release_form(AppRelease(), request.POST)
        release.save()
        messages.success(request, f"Version {release.version} is on the install page.")
        return redirect(reverse("backoffice:app_release_list"))

    return render(request, "backoffice/app_release_form.html",
                  _app_release_context(request, None, "new",
                                       _app_release_form_values()))


@admin_required
def app_release_detail(request, release_id):
    release = get_object_or_404(AppRelease, id=release_id)

    if request.method == "POST":
        errors = _app_release_form_errors(request.POST)
        if errors:
            return render(request, "backoffice/app_release_form.html",
                          _app_release_context(
                              request, release, "edit",
                              _app_release_form_values(post=request.POST), errors))

        _apply_app_release_form(release, request.POST)
        release.save()
        messages.success(request, f"Version {release.version} updated.")
        return redirect(reverse("backoffice:app_release_list"))

    return render(request, "backoffice/app_release_form.html",
                  _app_release_context(request, release, "edit",
                                       _app_release_form_values(release)))


@admin_required
def app_release_delete(request, release_id):
    """Remove a build from the list entirely.

    Un-ticking *Listed* is the usual way to withdraw one — it keeps the row, so
    the audit log still explains what was live and when. Deleting is for a row
    that should never have existed, e.g. one pointed at the wrong Drive file.
    """
    release = get_object_or_404(AppRelease, id=release_id)
    if request.method == "POST":
        version = release.version
        release.delete()
        messages.success(request, f"Version {version} removed from the list.")
    return redirect(reverse("backoffice:app_release_list"))
