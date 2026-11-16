"""Backoffice request/response handling for production workflows."""
from __future__ import annotations

import datetime
import json
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

from . import production_forms
from .views import _csv_response, _parse_date, _select_branch

MAX_QTY = 99999
HISTORY_DAYS = 60
NONE_PARAM = "_none"


def _parse_qty(raw):
    raw = (raw or "").strip()
    if raw == "":
        return None
    if not raw.isdigit() or int(raw) > MAX_QTY:
        raise ValueError(raw)
    return int(raw)


def _when(dt):
    return timezone.localtime(dt).strftime("%a %d %b %H:%M")


def _day(day):
    return day.strftime("%a %d %b %Y")


def _filter_param(raw):
    if not raw:
        return None
    return "" if raw == NONE_PARAM else raw


def _strict_day(raw):
    if not isinstance(raw, str) or not raw:
        raise production_forms.PayloadError("Delivery date is required for this save.")
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        raise production_forms.PayloadError("Delivery date must use YYYY-MM-DD.")


def _same_query_scope(request, key, value):
    supplied = request.GET.get(key)
    if supplied is not None and supplied != value:
        raise production_forms.PayloadError("The submitted form scope does not match this page. Reload before saving.")


def _request_scope(request, branches):
    production_forms.check_body_size(request)
    branch_id = request.POST.get("branch_id")
    day_text = request.POST.get("delivery_date")
    if not branch_id:
        raise production_forms.PayloadError("Branch is required for this save.")
    branch = next((row for row in branches if str(row.id) == branch_id), None)
    if branch is None:
        raise production_forms.PayloadError("That branch is no longer active. Nothing was saved.")
    day = _strict_day(day_text)
    _same_query_scope(request, "branch", branch_id)
    _same_query_scope(request, "date", day_text)
    return branch, day


def _request_payload(request):
    production_forms.check_body_size(request)
    if request.POST.get("production_payload"):
        return production_forms.parse_payload(request, "request")
    count = production_forms.fallback_row_count(request, "qty_")
    if count > production_forms.MAX_FALLBACK_FIELDS:
        raise production_forms.PayloadError("This form is too large to save without JavaScript.")
    try:
        baseline = json.loads(request.POST["request_baseline"])
        if not isinstance(baseline, dict) or baseline.get("kind") != "request":
            raise ValueError
        request_id = baseline.get("request_id")
        revision = baseline["request_revision"]
        if not isinstance(revision, int) or revision < 0:
            raise ValueError
        bases = {row["key"]: row["base_value"] for row in baseline["rows"]
                 if isinstance(row, dict) and isinstance(row.get("key"), str)}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise production_forms.PayloadError("This older form has no request baseline. Reload before saving.")
    rows = []
    quantity_error = None
    for field, raw in request.POST.items():
        if field.startswith("qty_"):
            key = field[4:]
            if key not in bases:
                raise production_forms.PayloadError("The request baseline is incomplete. Reload before saving.")
            try:
                value = _parse_qty(raw)
            except ValueError:
                quantity_error = "Quantities must be whole numbers from 0 to 99,999."
                value = None
            rows.append({"key": key, "value": value, "base_value": bases[key]})
    payload = production_forms.Payload("request", request_id, revision, rows)
    if quantity_error:
        raise production_forms.QuantityValidationError(quantity_error, payload)
    return payload


def _packing_scope(request, *, post=False):
    now = timezone.now()
    view = "packed" if request.GET.get("view") == "packed" else "live"
    dates = production.packed_dates() if view == "packed" else []
    default = dates[0] if dates else production.latest_locked_date(now)
    day = _parse_date(request.GET.get("date"), default)
    raw_category = request.GET.get("category") or ""
    if post:
        production_forms.check_body_size(request)
        mode = request.POST.get("mode")
        raw_day = request.POST.get("delivery_date")
        raw_category = request.POST.get("category", "")
        if mode not in {"live", "packed"}:
            raise production_forms.PayloadError("Packing mode is required for this save.")
        if mode != view:
            raise production_forms.PayloadError("The submitted packing mode does not match this page.")
        day = _strict_day(raw_day)
        _same_query_scope(request, "date", raw_day)
        query_view = request.GET.get("view")
        if query_view is not None and ((query_view == "packed") != (mode == "packed")):
            raise production_forms.PayloadError("The submitted packing mode does not match this page.")
        _same_query_scope(request, "category", raw_category)
    categories = production.packing_categories(day)
    category = _filter_param(raw_category)
    category_reset = category is not None and category not in categories
    if category_reset:
        category = None
        raw_category = ""
    return {
        "view": view, "dates": dates, "day": day, "raw_category": raw_category,
        "category": category, "categories": categories, "category_reset": category_reset,
        "deadline": production.locks_at(day), "is_open": production.is_open(day, now),
    }


def _packing_presentation(scope):
    tables = production.packing_tables(scope["day"], scope["category"])
    if scope["view"] == "packed":
        tables = [{**table, "rows": [row for row in table["rows"] if row["any_packed"]]}
                  for table in tables]
        tables = [table for table in tables if table["rows"]]
    return tables


def _packing_payload(request):
    production_forms.check_body_size(request)
    if request.POST.get("production_payload"):
        return production_forms.parse_payload(request, "packing")
    count = production_forms.fallback_row_count(request, "packed_")
    if count > production_forms.MAX_FALLBACK_FIELDS:
        raise production_forms.PayloadError("This grid is too large to save without JavaScript.")
    # The small fallback carries one JSON revision/baseline map, avoiding one
    # hidden field per cell while remaining well below Django's field limit.
    try:
        import json
        baseline = json.loads(request.POST["packing_baseline"])
    except (KeyError, TypeError, ValueError):
        raise production_forms.PayloadError("This older form has no packing baseline. Reload before saving.")
    if not isinstance(baseline, dict):
        raise production_forms.PayloadError("The packing baseline is invalid.")
    if isinstance(baseline.get("rows"), list):
        baseline = {row.get("key"): {"value": row.get("base_value"), "revision": row.get("revision")}
                    for row in baseline["rows"] if isinstance(row, dict)}
    rows = []
    quantity_error = None
    for field, raw in request.POST.items():
        if field.startswith("packed_"):
            key = field[7:]
            base = baseline.get(key)
            if not isinstance(base, dict):
                raise production_forms.PayloadError("The packing baseline is incomplete. Reload before saving.")
            try:
                value = _parse_qty(raw)
            except ValueError:
                quantity_error = "Quantities must be whole numbers from 0 to 99,999."
                value = None
            try:
                base_value = _parse_qty(base.get("value"))
            except (AttributeError, ValueError):
                raise production_forms.PayloadError("The packing baseline is invalid.")
            revision = base.get("revision")
            if not isinstance(revision, int) or revision < 0:
                raise production_forms.PayloadError("The packing baseline is invalid.")
            rows.append({"key": key, "value": value,
                         "base_value": base_value, "revision": revision})
    payload = production_forms.Payload("packing", None, None, rows)
    if quantity_error:
        raise production_forms.QuantityValidationError(quantity_error, payload)
    return payload


def _request_rows_context(branch, day, posted, conflicts, baseline_rows=None):
    req = (ProductionRequest.objects.filter(branch=branch, delivery_date=day)
           .prefetch_related("items").first())
    rows = production.request_form_rows(branch, req)
    known = {row["key"] for row in rows}
    baseline_rows = baseline_rows or {}
    for row in rows:
        product_key = str(row["product_id"]) if row.get("product_id") else None
        if product_key:
            known.add(product_key)
        baseline = baseline_rows.get(row["key"], baseline_rows.get(product_key, {}))
        row["saved"] = row["qty"]
        row["shown"] = posted.get(row["key"], posted.get(product_key, row["qty"] or ""))
        base_value = baseline["base_value"] if "base_value" in baseline else row["qty"]
        row["base"] = "" if base_value is None else str(base_value)
        row["conflict"] = conflicts.get(row["key"], conflicts.get(product_key))
    unknown_rows = [{"key": key, "value": value} for key, value in posted.items() if key not in known]
    return req, rows, unknown_rows


@login_required
def production_request(request):
    branches = production.active_branches()
    branch = _select_branch(request, branches)
    now = timezone.now()
    earliest = production.earliest_open_date(now)
    day = _parse_date(request.GET.get("date"), earliest)
    error = None
    posted, conflicts, unknown_rows = {}, {}, []
    status = 200
    retained_readonly = False
    reload_confirmation = False
    resave_blocked = False
    retained_payload = None

    if request.method == "POST" and request.POST.get("confirm_reload") == "1":
        try:
            branch, day = _request_scope(request, branches)
        except production_forms.PayloadError as exc:
            error, status = str(exc), 400
        else:
            return redirect(reverse("backoffice:production_request") + "?" + urlencode({
                "branch": str(branch.id), "date": day.isoformat()}))
    if request.method == "POST" and request.POST.get("reload_saved") == "1":
        try:
            branch, day = _request_scope(request, branches)
            posted = {field[4:]: raw for field, raw in request.POST.items() if field.startswith("qty_")}
            reload_confirmation = True
        except production_forms.PayloadError as exc:
            error, status = str(exc), 400
    elif request.method == "POST":
        try:
            branch, day = _request_scope(request, branches)
            payload = _request_payload(request)
            posted = {row["key"]: "" if row["value"] is None else str(row["value"])
                      for row in payload.rows}
            wanted = {row["key"]: row["value"] or 0 for row in payload.rows}
            saved = production.save_request(
                branch, day, wanted, request.user,
                expected_request_id=payload.request_id,
                expected_revision=payload.request_revision,
                strict=True,
            )
        except production_forms.PayloadError as exc:
            posted = production_forms.submitted_draft(request, "request")
            error = f"{exc} Nothing was saved."
            status = 400
            retained_payload = getattr(exc, "payload", None)
            resave_blocked = not isinstance(exc, production_forms.QuantityValidationError)
        except production.RequestLocked:
            error = f"Requests for {_day(day)} are closed. Nothing was saved."
            retained_readonly = True
        except getattr(production, "RevisionConflict", ()) as exc:
            error = f"Nothing was saved: this request changed in another session. Review the saved quantities before reloading."
            conflicts = getattr(exc, "conflicts", {}) or {}
            status = 409
        except getattr(production, "CatalogueConflict", ()) as exc:
            error = f"{exc} Nothing was saved."
            status = 409
            resave_blocked = True
        except getattr(production, "InvalidRows", ()) as exc:
            error = f"{exc} Nothing was saved."
            status = 409
            resave_blocked = True
        else:
            pieces = sum(item.requested_qty for item in saved.items.all()) if saved else 0
            messages.success(request, f"Saved {branch.name}'s request for {_day(day)} ({pieces:,} pieces).")
            return redirect(reverse("backoffice:production_request") + "?" + urlencode({
                "branch": str(branch.id), "date": day.isoformat()}))

    deadline = production.locks_at(day)
    is_open = production.is_open(day)
    retained_rows = {row["key"]: row for row in getattr(retained_payload, "rows", [])}
    req, rows, unknown_rows = _request_rows_context(branch, day, posted, conflicts, retained_rows) if branch else (None, [], [])
    total = sum(int(row["shown"] or 0) for row in rows if str(row["shown"] or "").isdigit())
    payload_baseline = {
        "version": 1, "kind": "request", "request_id": retained_payload.request_id if retained_payload else (str(req.id) if req else None),
        "request_revision": (retained_payload.request_revision if retained_payload else getattr(req, "revision", 0)),
        "rows": [{"key": row["key"], "value": str(row["shown"] or ""),
                  "base_value": row["base"]} for row in rows],
    }
    response = render(request, "backoffice/production_request.html", {
        "active": "production_request", "branches": branches, "branch": branch,
        "hide_dates": True, "day": day, "earliest": earliest, "is_open": is_open,
        "deadline": deadline, "req": req, "groups": production.group_by_category(rows),
        "total": total, "lines": sum(1 for row in rows if row["qty"]), "error": error,
        "max_qty": MAX_QTY,
        "request_revision": retained_payload.request_revision if retained_payload else getattr(req, "revision", 0),
        "submitted_request_id": retained_payload.request_id if retained_payload else (str(req.id) if req else None),
        "payload_baseline": payload_baseline,
        "payload_baseline_json": json.dumps(payload_baseline, separators=(",", ":")),
        "retained_readonly": retained_readonly,
        "conflict": status == 409, "reload_confirmation": reload_confirmation,
        "resave_blocked": resave_blocked,
        "compact_required": sum(not row.get("unavailable", False) for row in rows) > production_forms.MAX_FALLBACK_FIELDS,
        "deadline_legacy": getattr(production, "is_legacy_deadline", lambda value: False)(day),
        "unknown_rows": unknown_rows, "has_retained_draft": bool(posted),
    }, status=status)
    return response


@login_required
def production_requests(request):
    branches = production.active_branches()
    now = timezone.now()
    since = timezone.localdate(now) - timedelta(days=HISTORY_DAYS)
    qs = (ProductionRequest.objects.filter(branch__in=branches, delivery_date__gte=since)
          .select_related("branch").prefetch_related("items").order_by("-delivery_date", "branch__name"))
    visible = []
    for request_row in qs:
        request_items = [item for item in request_row.items.all() if item.requested_qty]
        # A retained item with a packed value remains meaningful history, even
        # after its requested quantity is cleared. Its supervisor summary must
        # still use only requested values and never reveal packing details.
        if request_items or any(item.packed_qty is not None for item in request_row.items.all()):
            visible.append((request_row, request_items))
    page_obj = Paginator(visible, 50).get_page(request.GET.get("page"))
    rows = []
    for row, items in page_obj.object_list:
        if items:
            summary = ", ".join(f"{item.product_name} ×{item.requested_qty}" for item in items[:2])
            if len(items) > 2:
                summary += f" +{len(items) - 2} more"
        else:
            summary = "No current requested quantities"
        rows.append({"req": row, "summary": summary, "pieces": sum(item.requested_qty for item in items),
                     "products": len(items), "status": production.request_status(row, now)})
    return render(request, "backoffice/production_requests.html", {
        "active": "production_requests", "hide_branch": True, "hide_dates": True, "rows": rows,
        "page_obj": page_obj, "paginator": page_obj.paginator, "since": since, "qs": "",
    })


def _kitchen_scope(request):
    now = timezone.now()
    day = _parse_date(request.GET.get("date"), production.latest_locked_date(now))
    raw_group = request.GET.get("group") or ""
    deadline = production.locks_at(day)
    return day, raw_group, _filter_param(raw_group), deadline, production.is_open(day, now), production.is_legacy_deadline(day)


@login_required
def production_kitchen(request):
    day, raw_group, group, deadline, is_open, deadline_legacy = _kitchen_scope(request)
    groups = production.kitchen_groups(day)
    return render(request, "backoffice/production_kitchen.html", {
        "active": "production_kitchen", "hide_branch": True, "hide_dates": True, "day": day,
        "group": group, "raw_group": raw_group,
        "groups": [{"name": group or "Ungrouped", "param": group or NONE_PARAM} for group in groups],
        "data": production.kitchen_totals(day, group), "deadline": deadline, "is_open": is_open,
        "deadline_legacy": deadline_legacy,
        "export_qs": urlencode({"date": day.isoformat(), "group": raw_group}),
    })


@login_required
def production_kitchen_export(request):
    day, _raw, group, deadline, is_open, _deadline_legacy = _kitchen_scope(request)
    data = production.kitchen_totals(day, group)
    response, writer = _csv_response(f"production-totals-{day.isoformat()}.csv")
    writer.writerow(["Production totals"])
    writer.writerow(["Delivery date", day.isoformat()])
    writer.writerow(["Group", "All" if group is None else (group or "Ungrouped")])
    writer.writerow(["Status", f"Open until {_when(deadline)}" if is_open else "Final"])
    writer.writerow([]); writer.writerow(["Category", "Product", "Branches", "Total"])
    for category in data["categories"]:
        for row in category["rows"]:
            writer.writerow([category["name"], row["name"], row["branches"], row["total"]])
    writer.writerow(["Total", "", data["branches"], data["total"]])
    return response


@login_required
def production_packing(request):
    error = None
    posted, conflicts = {}, {}
    status = 200
    conflict = False
    reload_confirmation = False
    resave_blocked = False
    scope = None
    retained_payload = None
    try:
        scope = _packing_scope(request, post=request.method == "POST")
        if request.method == "POST" and request.POST.get("confirm_reload") == "1":
            return redirect(reverse("backoffice:production_packing") + "?" + urlencode({
                key: value for key, value in (("date", scope["day"].isoformat()),
                                               ("category", scope["raw_category"]),
                                               ("view", "packed" if scope["view"] == "packed" else "")) if value}))
        if request.method == "POST" and request.POST.get("reload_saved") == "1":
            posted = production_forms.submitted_draft(request, "packing")
            conflict = reload_confirmation = True
        elif request.method == "POST":
            if scope["view"] != "live":
                raise production_forms.PayloadError("Packed history is read-only. Nothing was saved.")
            payload = _packing_payload(request)
            posted = {row["key"]: "" if row["value"] is None else str(row["value"])
                      for row in payload.rows}
            changed_rows = [row for row in payload.rows if row["value"] != row["base_value"]]
            values = {row["key"]: row["value"] for row in changed_rows}
            expected = {row["key"]: row["revision"] for row in changed_rows}
            changed = production.save_packed(scope["day"], values, request.user,
                                             expected_revisions=expected, strict=True)
            messages.success(request, f"Saved {changed} packed quantit{'y' if changed == 1 else 'ies'} for {_day(scope['day'])}.")
            return redirect(reverse("backoffice:production_packing") + "?" + urlencode({
                key: value for key, value in (("date", scope["day"].isoformat()),
                                               ("category", scope["raw_category"])) if value}))
    except production_forms.PayloadError as exc:
        posted = production_forms.submitted_draft(request, "packing")
        error = str(exc)
        status = 400
        retained_payload = getattr(exc, "payload", None)
        resave_blocked = not isinstance(exc, production_forms.QuantityValidationError)
        if scope is None:
            scope = _packing_scope(request)
    except getattr(production, "RevisionConflict", ()) as exc:
        error = "Nothing was saved: one or more packed cells changed in another session. Reload saved quantities to discard this draft."
        conflicts = getattr(exc, "conflicts", {}) or {}
        status, conflict = 409, True
        resave_blocked = True
        scope = _packing_scope(request, post=True)
    except (getattr(production, "InvalidRows", ()), getattr(production, "CatalogueConflict", ())) as exc:
        error = f"{exc} Nothing was saved."
        status, resave_blocked = 409, True
        scope = _packing_scope(request, post=True)

    tables = _packing_presentation(scope)
    retained_rows = {row["key"]: row for row in getattr(retained_payload, "rows", [])}
    for table in tables:
        for row in table["rows"]:
            for cell in row["cells"]:
                if cell is not None:
                    cell.saved = "" if cell.packed_qty is None else cell.packed_qty
                    baseline = retained_rows.get(str(cell.id), {})
                    base_value = baseline["base_value"] if "base_value" in baseline else cell.saved
                    cell.base = "" if base_value is None else str(base_value)
                    cell.baseline_revision = baseline.get("revision", getattr(cell, "revision", 0))
                    cell.shown = posted.get(str(cell.id), cell.base if retained_payload else cell.saved)
                    cell.conflict = conflicts.get(str(cell.id))
    payload_baseline = {
        "version": 1, "kind": "packing", "rows": [
            {"key": str(cell.id), "value": str(cell.shown),
             "base_value": cell.base, "revision": cell.baseline_revision}
            for table in tables for row in table["rows"] for cell in row["cells"] if cell is not None
        ],
    }
    known_keys = {row["key"] for row in payload_baseline["rows"]}
    unknown_rows = [
        {"key": key, "value": value}
        for key, value in posted.items() if key not in known_keys
    ]
    canonical_packing_url = "?" + urlencode({
        key: value for key, value in (
            ("date", scope["day"].isoformat()),
            ("view", "packed" if scope["view"] == "packed" else ""),
            ("category", scope["raw_category"]),
        ) if value
    })
    response = render(request, "backoffice/production_packing.html", {
        "active": "production_packing", "hide_branch": True, "hide_dates": True, **scope,
        "categories": [{"name": category or production.UNCATEGORISED, "param": category or NONE_PARAM}
                       for category in scope["categories"]], "tables": tables, "error": error,
        "max_qty": MAX_QTY, "conflict": conflict, "reload_confirmation": reload_confirmation,
        "resave_blocked": resave_blocked,
        "compact_required": len(payload_baseline["rows"]) > production_forms.MAX_FALLBACK_FIELDS,
        "unknown_rows": unknown_rows,
        "canonical_packing_url": canonical_packing_url,
        "payload_baseline": payload_baseline, "payload_baseline_json": json.dumps(payload_baseline, separators=(",", ":")),
        "export_qs": urlencode({key: value for key, value in (
            ("date", scope["day"].isoformat()), ("category", scope["raw_category"]),
            ("view", "packed" if scope["view"] == "packed" else "")) if value}),
    }, status=status)
    return response


@login_required
def production_packing_export(request):
    scope = _packing_scope(request)
    response, writer = _csv_response(f"packing-{scope['day'].isoformat()}.csv")
    writer.writerow(["Packing"])
    writer.writerow(["Delivery date", scope["day"].isoformat()])
    writer.writerow(["Mode", scope["view"]])
    writer.writerow(["Category", "All" if scope["category"] is None else (scope["category"] or production.UNCATEGORISED)])
    writer.writerow([])
    if scope["view"] == "packed":
        writer.writerow(["Category", "Branch", "Product", "Packed", "Packed by"])
    else:
        writer.writerow(["Category", "Branch", "Product", "Requested", "Packed", "Packed by"])
    for table in _packing_presentation(scope):
        for row in table["rows"]:
            for name, cell in zip(table["columns"], row["cells"]):
                if cell is None or (scope["view"] == "packed" and cell.packed_qty is None):
                    continue
                packed = "" if cell.packed_qty is None else cell.packed_qty
                values = [table["name"], row["branch"].name, name]
                if scope["view"] != "packed":
                    values.append(cell.requested_qty)
                values.extend([packed, cell.packed_by_name])
                writer.writerow(values)
    return response
