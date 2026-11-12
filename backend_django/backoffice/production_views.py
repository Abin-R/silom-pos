"""Backoffice pages for production requests — the rules live in
``bravepos.production``; this module is only the request/response side.

Four screens, one per person:

* **Request production** (stock supervisor) — a branch, a delivery date and
  one number box per product, grouped by category.
* **My requests** (stock supervisor) — what has been asked for, at every
  branch, and whether it can still be changed.
* **Kitchen totals** (viewer, read only) — every branch added up, per product.
* **Packing** (packer) — a grid per category, branches down the side and
  products across: what was requested, then a box per product for what was
  packed.

Who may open which is decided by ``backoffice.middleware`` (the role
allowlists), like every other limited role, not here.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from bravepos import production
from bravepos.models import ProductionRequest

from .views import _csv_response, _parse_date, _select_branch

# Larger than any real order, small enough that a slipped finger (an extra
# three zeros) is refused rather than sent to the kitchen.
MAX_QTY = 99999

# How far back My requests reaches.
HISTORY_DAYS = 60

# The query value standing for "products with no group / category", which
# are stored as "" and so cannot be told apart from "no filter" in a URL.
NONE_PARAM = "_none"


def _parse_qty(raw):
    """A whole number from a box, None when it is blank.  Raises ValueError
    for anything else — decimals, negatives, text, or more than MAX_QTY."""
    raw = (raw or "").strip()
    if raw == "":
        return None
    if not raw.isdigit():
        raise ValueError(raw)
    value = int(raw)
    if value > MAX_QTY:
        raise ValueError(raw)
    return value


def _when(dt) -> str:
    return timezone.localtime(dt).strftime("%a %d %b %H:%M")


def _day(d) -> str:
    return d.strftime("%a %d %b %Y")


def _filter_param(raw):
    """``?group=`` / ``?category=`` → None (everything), "" (none) or a name."""
    if not raw:
        return None
    return "" if raw == NONE_PARAM else raw


# ── Request production ──────────────────────────────────────────────────────
@login_required
def production_request(request):
    branches = production.active_branches()
    branch = _select_branch(request, branches)
    now = timezone.now()
    cutoff = production.cutoff_time()
    earliest = production.earliest_open_date(now, cutoff)
    day = _parse_date(request.GET.get("date"), earliest)
    deadline = production.locks_at(day, cutoff)
    is_open = now < deadline
    here = reverse("backoffice:production_request") + "?" + urlencode(
        {"branch": str(branch.id) if branch else "", "date": day.isoformat()})

    error = None
    posted = {}
    if request.method == "POST" and branch is not None:
        wanted, bad = {}, False
        for field, raw in request.POST.items():
            if not field.startswith("qty_"):
                continue
            key = field[4:]
            posted[key] = raw
            try:
                wanted[key] = _parse_qty(raw) or 0
            except ValueError:
                bad = True
        if bad:
            error = f"Quantities must be whole numbers from 0 to {MAX_QTY:,}. Nothing was saved."
        else:
            try:
                saved = production.save_request(branch, day, wanted, request.user, now)
            except production.RequestLocked:
                error = (f"Requests for {_day(day)} closed at {_when(deadline)}. "
                         f"Nothing was saved.")
            else:
                if saved is None:
                    messages.success(request, f"{branch.name} has nothing requested for {_day(day)}.")
                else:
                    items = list(saved.items.filter(requested_qty__gt=0))
                    pieces = sum(it.requested_qty for it in items)
                    messages.success(request, (
                        f"Saved. {branch.name} asks for {pieces:,} piece{'' if pieces == 1 else 's'} "
                        f"({len(items)} product{'' if len(items) == 1 else 's'}) on {_day(day)}. "
                        f"You can change it until {_when(deadline)}."))
                return redirect(here)

    req = None
    if branch is not None:
        req = (ProductionRequest.objects.filter(branch=branch, delivery_date=day)
               .prefetch_related("items").first())
    rows = production.request_form_rows(branch, req) if branch is not None else []
    for r in rows:
        r["shown"] = posted.get(r["key"], r["qty"] or "")
    total = sum(r["qty"] for r in rows)
    return render(request, "backoffice/production_request.html", {
        "active": "production_request",
        "branches": branches,
        "branch": branch,
        "hide_dates": True,
        "day": day,
        "earliest": earliest,
        "is_open": is_open,
        "deadline": deadline,
        "req": req,
        "groups": production.group_by_category(rows),
        "total": total,
        "lines": sum(1 for r in rows if r["qty"]),
        "error": error,
        "max_qty": MAX_QTY,
    })


@login_required
def production_requests(request):
    branches = production.active_branches()
    now = timezone.now()
    cutoff = production.cutoff_time()
    since = timezone.localdate(now) - timedelta(days=HISTORY_DAYS)
    qs = (ProductionRequest.objects
          .filter(branch__in=branches, delivery_date__gte=since)
          .select_related("branch").prefetch_related("items")
          .order_by("-delivery_date", "branch__name"))
    page_obj = Paginator(qs, 50).get_page(request.GET.get("page"))
    rows = []
    for r in page_obj.object_list:
        items = [it for it in r.items.all() if it.requested_qty]
        summary = ", ".join(f"{it.product_name} ×{it.requested_qty}" for it in items[:2])
        if len(items) > 2:
            summary += f" +{len(items) - 2} more"
        rows.append({
            "req": r,
            "summary": summary,
            "pieces": sum(it.requested_qty for it in items),
            "products": len(items),
            "status": production.request_status(r, now, cutoff),
        })
    return render(request, "backoffice/production_requests.html", {
        "active": "production_requests",
        "hide_branch": True,
        "hide_dates": True,
        "rows": rows,
        "page_obj": page_obj,
        "paginator": page_obj.paginator,
        "since": since,
        "qs": "",
    })


# ── Kitchen totals ──────────────────────────────────────────────────────────
def _kitchen_scope(request):
    now = timezone.now()
    cutoff = production.cutoff_time()
    day = _parse_date(request.GET.get("date"), production.latest_locked_date(now, cutoff))
    raw_group = request.GET.get("group") or ""
    group = _filter_param(raw_group)
    deadline = production.locks_at(day, cutoff)
    return day, raw_group, group, deadline, now < deadline


@login_required
def production_kitchen(request):
    day, raw_group, group, deadline, is_open = _kitchen_scope(request)
    groups = production.kitchen_groups(day)
    data = production.kitchen_totals(day, group)
    return render(request, "backoffice/production_kitchen.html", {
        "active": "production_kitchen",
        "hide_branch": True,
        "hide_dates": True,
        "day": day,
        "group": group,
        "raw_group": raw_group,
        "groups": [{"name": g or "Ungrouped", "param": g or NONE_PARAM} for g in groups],
        "data": data,
        "deadline": deadline,
        "is_open": is_open,
        "export_qs": urlencode({"date": day.isoformat(), "group": raw_group}),
    })


@login_required
def production_kitchen_export(request):
    day, _raw, group, deadline, is_open = _kitchen_scope(request)
    data = production.kitchen_totals(day, group)
    response, w = _csv_response(f"production-totals-{day.isoformat()}.csv")
    w.writerow(["Production totals"])
    w.writerow(["Delivery date", day.isoformat()])
    w.writerow(["Group", "All" if group is None else (group or "Ungrouped")])
    w.writerow(["Status", f"Open until {_when(deadline)}" if is_open else "Final"])
    w.writerow([])
    w.writerow(["Category", "Product", "Branches", "Total"])
    for c in data["categories"]:
        for r in c["rows"]:
            w.writerow([c["name"], r["name"], r["branches"], r["total"]])
    w.writerow(["Total", "", data["branches"], data["total"]])
    return response


# ── Packing ─────────────────────────────────────────────────────────────────
def _packing_scope(request):
    now = timezone.now()
    cutoff = production.cutoff_time()
    view = "packed" if request.GET.get("view") == "packed" else "live"
    dates = production.packed_dates() if view == "packed" else []
    default = dates[0] if dates else production.latest_locked_date(now, cutoff)
    day = _parse_date(request.GET.get("date"), default)
    raw_category = request.GET.get("category") or ""
    deadline = production.locks_at(day, cutoff)
    return {
        "view": view, "dates": dates, "day": day,
        "raw_category": raw_category, "category": _filter_param(raw_category),
        "deadline": deadline, "is_open": now < deadline,
    }


@login_required
def production_packing(request):
    scope = _packing_scope(request)
    day = scope["day"]
    here = reverse("backoffice:production_packing") + "?" + urlencode(
        {k: v for k, v in (("date", day.isoformat()), ("category", scope["raw_category"]),
                           ("view", "packed" if scope["view"] == "packed" else ""))
         if v})

    error = None
    posted = {}
    if request.method == "POST":
        values, bad = {}, False
        for field, raw in request.POST.items():
            if not field.startswith("packed_"):
                continue
            item_id = field[7:]
            try:
                uuid.UUID(item_id)
            except ValueError:
                continue
            posted[item_id] = raw
            try:
                values[item_id] = _parse_qty(raw)
            except ValueError:
                bad = True
        if bad:
            error = f"Packed quantities must be whole numbers from 0 to {MAX_QTY:,}. Nothing was saved."
        else:
            changed = production.save_packed(day, values, request.user)
            if changed:
                messages.success(request, (
                    f"Saved {changed} packed quantit{'y' if changed == 1 else 'ies'} "
                    f"for {_day(day)}."))
            else:
                messages.info(request, "Nothing had changed, so nothing was saved.")
            return redirect(here)

    tables = production.packing_tables(day, scope["category"])
    if scope["view"] == "packed":
        # History is what was sent: branches and categories with nothing
        # packed are noise there, however much they asked for.
        for t in tables:
            t["rows"] = [r for r in t["rows"] if r["any_packed"]]
        tables = [t for t in tables if t["rows"]]
    for t in tables:
        for row in t["rows"]:
            for cell in row["cells"]:
                if cell is not None:
                    # `saved` is what the database holds, so after a refused
                    # save the re-shown boxes still count as changes.
                    cell.saved = "" if cell.packed_qty is None else cell.packed_qty
                    cell.shown = posted.get(str(cell.id), cell.saved)
    categories = production.packing_categories(day)
    return render(request, "backoffice/production_packing.html", {
        "active": "production_packing",
        "hide_branch": True,
        "hide_dates": True,
        **scope,
        "categories": [{"name": c or production.UNCATEGORISED, "param": c or NONE_PARAM}
                       for c in categories],
        "tables": tables,
        "error": error,
        "max_qty": MAX_QTY,
        "export_qs": urlencode({"date": day.isoformat(), "category": scope["raw_category"]}),
    })


@login_required
def production_packing_export(request):
    scope = _packing_scope(request)
    day = scope["day"]
    response, w = _csv_response(f"packing-{day.isoformat()}.csv")
    w.writerow(["Packing"])
    w.writerow(["Delivery date", day.isoformat()])
    w.writerow(["Category", "All" if scope["category"] is None
                else (scope["category"] or production.UNCATEGORISED)])
    w.writerow([])
    w.writerow(["Category", "Branch", "Product", "Requested", "Packed", "Packed by"])
    for t in production.packing_tables(day, scope["category"]):
        for row in t["rows"]:
            for name, cell in zip(t["columns"], row["cells"]):
                if cell is None:
                    continue
                w.writerow([
                    t["name"], row["branch"].name, name, cell.requested_qty,
                    "" if cell.packed_qty is None else cell.packed_qty,
                    cell.packed_by_name,
                ])
    return response
