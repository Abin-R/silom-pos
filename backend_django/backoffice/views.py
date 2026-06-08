"""Backoffice views — server-rendered Bootstrap dashboard backed by the
existing bravepos Django models. All views require a Django auth login
(see /backoffice/login/); the DRF POS API at /api/* uses its own token
auth and is unaffected. Filtering is via query string:
?branch=<uuid>&from=YYYY-MM-DD&to=YYYY-MM-DD."""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Sum, Q
from django.db.models.functions import Coalesce, TruncDate
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from bravepos.models import (
    Branch,
    Category,
    Order,
    OrderItem,
    Product,
    Settings,
)


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


def _common_filters(request):
    """Branch / date-range filter values that every report page uses."""
    today = timezone.localdate()
    branches = list(Branch.objects.filter(active=True).order_by("name"))
    requested_branch = request.GET.get("branch") or ""
    branch = next((b for b in branches if str(b.id) == requested_branch), None)
    if branch is None and branches:
        branch = branches[0]
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


def customer_receipt(request, order_number: str):
    """Public landing page for the receipt QR code.  No auth required —
    customers scan the QR on their thermal receipt and land here.  Two
    CTAs: request a full tax invoice (ใบกำกับภาษีเต็มรูป) and leave a
    review.  Buttons are placeholders until the real flows are wired."""
    return render(request, "backoffice/customer_receipt.html", {
        "order_number": order_number,
    })


def create_tax_invoice(request, order_number: str):
    """Tax-invoice creation form for a given order.  Visual mock only —
    no save handler yet — so the Save button is inert.  Linked from the
    customer receipt landing page's 'Issue Full Tax Invoice' button."""
    return render(request, "backoffice/create_tax_invoice.html", {
        "order_number": order_number,
    })


@login_required
def dashboard(request):
    today = timezone.localdate()
    branches = list(Branch.objects.filter(active=True).order_by("name"))
    requested_branch = request.GET.get("branch") or ""
    branch = next((b for b in branches if str(b.id) == requested_branch), None)
    if branch is None and branches:
        branch = branches[0]

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
    if settings_row and settings_row.tax_mode == "inclusive":
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

    context = {
        "active": "dashboard",
        "branches": branches,
        "branch": branch,
        "date_from": dfrom.isoformat(),
        "date_to": dto.isoformat(),
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
@login_required
def transactions(request):
    """Per-bill list with expandable detail rows.

    Maps to SilomPOS `/report/transaction`. Each row mirrors the columns
    you see there (Sub Total, Discount, Grand Total, Total incl/non-Tax,
    Sub-total ex-Tax, Tax, Add-on, Service Charge, Rounding, Shipping,
    Status). Clicking a row expands an inline panel with the line items
    and payment block."""
    branches, branch, dfrom, dto = _common_filters(request)
    start, end = _date_window(dfrom, dto)

    qs = (
        Order.objects.filter(created_at__gte=start, created_at__lte=end)
        .prefetch_related("items", "items__product")
        .order_by("-created_at")
    )
    if branch:
        qs = qs.filter(branch=branch)

    settings_row = Settings.objects.first()
    tax_percent = settings_row.tax_percent if settings_row else Decimal("7")
    tax_mode = settings_row.tax_mode if settings_row else "exclusive"
    service_charge_pct = (
        settings_row.service_charge_percent
        if settings_row and settings_row.service_charge_enabled
        else Decimal(0)
    )

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))

    rows = []
    for o in page_obj.object_list:
        sub = o.subtotal or Decimal(0)
        disc = o.discount_amount or Decimal(0)
        taxable = sub - disc
        if tax_mode == "inclusive":
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

        items_data = []
        for it in o.items.all():
            line_total = (it.price or Decimal(0)) * (it.qty or 0)
            items_data.append({
                "item": it,
                "barcode": it.product.barcode if it.product_id else "",
                "line_total": line_total,
            })

        rows.append({
            "order": o,
            "items": items_data,
            "promotion_discount": Decimal(0),   # no promo tracking yet
            "add_on_total": Decimal(0),         # no add-on tracking yet
            "service_charge": service_charge,
            "rounding_adj": Decimal(0),
            "shipping_fee": Decimal(0),
            "tax_amount": tax_amount,
            "total_incl_tax": total_incl_tax,
            "total_non_tax": total_non_tax,
            "sub_ex_tax": sub_ex_tax,
        })

    context = {
        "active": "transactions",
        "branches": branches,
        "branch": branch,
        "date_from": dfrom.isoformat(),
        "date_to": dto.isoformat(),
        "rows": rows,
        "page_obj": page_obj,
        "paginator": paginator,
        "qs": _filter_qs(request),
        "tax_percent": tax_percent,
    }
    return render(request, "backoffice/transactions.html", context)


# ─── Sales report by Date ───────────────────────────────────────────────
def _profit_expr():
    """Per-line profit: (price - product.cost) * qty. Wrapped because the
    multiplication output type can't be inferred when one side is nullable."""
    return ExpressionWrapper(
        (F("price") - Coalesce(F("product__cost"), Decimal(0))) * F("qty"),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )


@login_required
def report_daily(request):
    """Daily totals — one row per calendar day in the filter range. Click a
    row to drill down into per-bill detail (`report_daily_detail`)."""
    branches, branch, dfrom, dto = _common_filters(request)
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
        rows.append({
            "date": d,
            "subtotal": sub,
            "discount": disc,
            "tax_amount": tax_amount,
            "service_charge": Decimal(0),
            "profit": profit_map.get(d, Decimal(0)),
            "grand_total": entry["total"] or Decimal(0),
            "bill_count": entry["bill_count"] or 0,
        })

    context = {
        "active": "report_daily",
        "branches": branches,
        "branch": branch,
        "date_from": dfrom.isoformat(),
        "date_to": dto.isoformat(),
        "rows": rows,
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/report_daily.html", context)


@login_required
def report_daily_detail(request, date_str):
    """Per-bill detail for a single day — drill-down from `report_daily`."""
    branches, branch, _, _ = _common_filters(request)
    try:
        day = date.fromisoformat(date_str)
    except ValueError:
        return redirect("backoffice:report_daily")
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

    context = {
        "active": "report_daily",
        "branches": branches,
        "branch": branch,
        "day": day,
        "rows": rows,
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/report_daily_detail.html", context)


# ─── Sales report by Bill Detail ────────────────────────────────────────
@login_required
def report_sell(request):
    """One row per line item across all bills in the range."""
    branches, branch, dfrom, dto = _common_filters(request)
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

    paginator = Paginator(items, 50)
    page_obj = paginator.get_page(request.GET.get("page"))

    rows = []
    for it in page_obj.object_list:
        line_sub = (it.price or Decimal(0)) * (it.qty or 0)
        rows.append({
            "item": it,
            "barcode": it.product.barcode if it.product_id else "",
            "add_on_total": Decimal(0),
            "discount": Decimal(0),
            "sub_total": line_sub,
            "total": line_sub,
        })

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


# ─── Sales report by Product (SKU) ──────────────────────────────────────
@login_required
def report_sku(request):
    """Aggregated per product over the date range. Joined to current Product
    row to get barcode, category and current stock balance."""
    branches, branch, dfrom, dto = _common_filters(request)
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

    paginator = Paginator(rows, 50)
    page_obj = paginator.get_page(request.GET.get("page"))

    context = {
        "active": "report_sku",
        "branches": branches,
        "branch": branch,
        "date_from": dfrom.isoformat(),
        "date_to": dto.isoformat(),
        "rows": page_obj.object_list,
        "page_obj": page_obj,
        "paginator": paginator,
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/report_sku.html", context)


# ─── Inventory Summary ──────────────────────────────────────────────────
@login_required
def inventory_summary(request):
    """Current on-hand balance per product. Sortable by Name / Barcode /
    Category / OnhandQty (matching the SilomPOS sort options)."""
    branches, branch, _, _ = _common_filters(request)

    qs = Product.objects.filter(active=True).select_related("category")
    if branch:
        qs = qs.filter(branch=branch)

    sort = request.GET.get("sort", "name")
    sort_map = {
        "name": "name",
        "barcode": "barcode",
        "category": "category__name",
        "stock": "-stock",
    }
    qs = qs.order_by(sort_map.get(sort, "name"))

    paginator = Paginator(qs, 50)
    page_obj = paginator.get_page(request.GET.get("page"))

    context = {
        "active": "inventory",
        "branches": branches,
        "branch": branch,
        "products": page_obj.object_list,
        "page_obj": page_obj,
        "paginator": paginator,
        "sort": sort,
        "qs": _filter_qs(request, sort=sort if sort != "name" else None),
        "today": timezone.localdate(),
    }
    return render(request, "backoffice/inventory.html", context)


# ─── Products ───────────────────────────────────────────────────────────
@login_required
def product_list(request):
    """Product catalog grid/list. Filterable by category, sortable, paginated."""
    branches, branch, _, _ = _common_filters(request)

    qs = Product.objects.filter(active=True).select_related("category")
    if branch:
        qs = qs.filter(branch=branch)

    cat_id = request.GET.get("category") or ""
    if cat_id:
        qs = qs.filter(category_id=cat_id)

    sort = request.GET.get("sort", "name")
    sort_map = {
        "name": "name",
        "newest": "-id",
        "category": "category__name",
        "price": "-price",
    }
    qs = qs.order_by(sort_map.get(sort, "name"))

    categories = Category.objects.filter(active=True)
    if branch:
        categories = categories.filter(Q(branch=branch) | Q(branch__isnull=True))
    categories = list(categories.order_by("name"))

    paginator = Paginator(qs, 50)
    page_obj = paginator.get_page(request.GET.get("page"))

    view = request.GET.get("view", "grid")  # grid | list

    context = {
        "active": "products",
        "branches": branches,
        "branch": branch,
        "categories": categories,
        "selected_category": cat_id,
        "products": page_obj.object_list,
        "page_obj": page_obj,
        "paginator": paginator,
        "sort": sort,
        "view": view,
        "qs": _filter_qs(request, sort=sort, category=cat_id or None, view=view),
    }
    return render(request, "backoffice/product_list.html", context)


def _product_form_categories(branch):
    qs = Category.objects.filter(active=True)
    if branch:
        qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True))
    return list(qs.order_by("name"))


def _apply_product_form(product, post, branch):
    """Pull fields out of a POSTed product form onto a Product instance.
    Used by both `product_new` and `product_detail` to save."""
    product.branch = branch
    product.name = (post.get("name") or "").strip()
    product.name_th = (post.get("name_th") or "").strip()
    product.barcode = (post.get("barcode") or "").strip()
    product.sku = (post.get("sku") or "").strip()
    product.price = Decimal(post.get("price") or "0")
    product.cost = Decimal(post.get("cost") or "0")
    product.stock = int(post.get("stock") or 0)
    cat_id = post.get("category") or ""
    product.category_id = cat_id if cat_id else None
    product.tax_type = post.get("tax_type") or "V"
    product.product_type = post.get("product_type") or "P"
    product.image_url = (post.get("image_url") or "").strip()
    return product


@login_required
def product_detail(request, product_id):
    """View + edit a single product. POST saves and stays on the page."""
    branches, branch, _, _ = _common_filters(request)
    product = get_object_or_404(Product, id=product_id)

    if request.method == "POST":
        _apply_product_form(product, request.POST, product.branch or branch)
        product.save()
        return redirect("backoffice:product_detail", product_id=product.id)

    context = {
        "active": "products",
        "branches": branches,
        "branch": branch,
        "product": product,
        "categories": _product_form_categories(product.branch or branch),
        "mode": "edit",
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/product_form.html", context)


@login_required
def product_new(request):
    """Add a single product. POST creates and redirects to its detail page."""
    branches, branch, _, _ = _common_filters(request)

    if request.method == "POST":
        product = Product()
        _apply_product_form(product, request.POST, branch)
        product.save()
        return redirect("backoffice:product_detail", product_id=product.id)

    context = {
        "active": "products",
        "branches": branches,
        "branch": branch,
        "product": Product(branch=branch),
        "categories": _product_form_categories(branch),
        "mode": "new",
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/product_form.html", context)


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
            ptype = request.POST.getlist("product_type")[idx] or "P"
            p.product_type = ptype
            p.save()
            saved += 1
        if saved:
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
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/product_bulk_add.html", context)


@login_required
def product_bulk_edit(request):
    """Inline-editable grid for existing products. POST saves all rows."""
    branches, branch, _, _ = _common_filters(request)

    qs = Product.objects.filter(active=True).select_related("category")
    if branch:
        qs = qs.filter(branch=branch)
    qs = qs.order_by("name")

    if request.method == "POST":
        ids = request.POST.getlist("id")
        for idx, pid in enumerate(ids):
            try:
                p = Product.objects.get(id=pid)
            except Product.DoesNotExist:
                continue
            p.barcode = (request.POST.getlist("barcode")[idx] or "").strip()
            p.name = (request.POST.getlist("name")[idx] or p.name).strip()
            p.price = Decimal(request.POST.getlist("price")[idx] or "0")
            p.cost = Decimal(request.POST.getlist("cost")[idx] or "0")
            cat = request.POST.getlist("category")[idx] or ""
            p.category_id = cat or None
            p.save()
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
        "qs": _filter_qs(request),
    }
    return render(request, "backoffice/product_bulk_edit.html", context)


# ─── Shops & Branches ───────────────────────────────────────────────────
def _branch_topbar_context():
    """Shared topbar context (branch dropdown + date inputs) for pages that
    don't actually filter by branch/date. Keeps the topbar consistent."""
    today = timezone.localdate().isoformat()
    return {
        "branches": list(Branch.objects.filter(active=True).order_by("name")),
        "branch": None,
        "date_from": today,
        "date_to": today,
    }


def _apply_branch_form(b: Branch, post) -> Branch:
    b.name = (post.get("name") or "").strip()
    b.code = (post.get("code") or "").strip()
    b.tax_id = (post.get("tax_id") or "").strip()
    b.pos_id = (post.get("pos_id") or "").strip()
    b.address = (post.get("address") or "").strip()
    b.phone = (post.get("phone") or "").strip()
    b.logo_url = (post.get("logo_url") or "").strip()
    b.active = post.get("active") == "on"
    return b


@login_required
def branch_list(request):
    """Card-per-branch view. Shop-level info (logo, business type, hours)
    is sourced from the single Settings row; branch-level overrides
    (tax_id, address, phone) come from the Branch itself."""
    rows = list(Branch.objects.all().order_by("name"))
    context = {
        "active": "branches",
        "branches_all": rows,
        "settings": Settings.objects.first(),
        **_branch_topbar_context(),
    }
    return render(request, "backoffice/branch_list.html", context)


@login_required
def branch_detail(request, branch_id):
    b = get_object_or_404(Branch, id=branch_id)
    if request.method == "POST":
        _apply_branch_form(b, request.POST)
        b.save()
        return redirect("backoffice:branch_list")
    context = {
        "active": "branches",
        "branch_obj": b,
        "mode": "edit",
        **_branch_topbar_context(),
    }
    return render(request, "backoffice/branch_form.html", context)


@login_required
def branch_new(request):
    if request.method == "POST":
        b = Branch()
        _apply_branch_form(b, request.POST)
        b.save()
        return redirect("backoffice:branch_list")
    context = {
        "active": "branches",
        "branch_obj": Branch(active=True),
        "mode": "new",
        **_branch_topbar_context(),
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
    requested = request.GET.get("branch") or ""
    branch = next((b for b in branches if str(b.id) == requested), None) or (branches[0] if branches else None)

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
    }
    return render(request, "backoffice/shop_settings.html", context)
