"""Bounded, versioned production form payloads.

The production screens remain normal server-rendered forms.  JavaScript places
all grid values in one compact field so a large grid does not exhaust Django's
normal parsed-field limit; this module keeps that special case local and
strictly bounded.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

MAX_ROWS = 10_000
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_FALLBACK_FIELDS = 900
MAX_KEY_LENGTH = 80


class PayloadError(ValueError):
    """A safe, user-facing production form error."""


@dataclass(frozen=True)
class Payload:
    kind: str
    request_id: str | None
    request_revision: int | None
    rows: list[dict]


def _quantity(value):
    """Validate a raw form value while preserving blank versus zero."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise PayloadError("Production form values must be strings.")
    if len(value) > 8:
        raise PayloadError("A production quantity is too long.")
    value = value.strip()
    if not value:
        return None
    if not value.isdigit() or int(value) > 99999:
        raise PayloadError("Quantities must be whole numbers from 0 to 99,999.")
    return int(value)


def _revision(value, field):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PayloadError(f"{field} must be a non-negative integer.")
    return value


def check_body_size(request):
    length = request.META.get("CONTENT_LENGTH")
    try:
        if length is not None and int(length) > MAX_BODY_BYTES:
            raise PayloadError("This production form is over the 8 MiB submission limit.")
    except ValueError:
        raise PayloadError("The production form has an invalid content length.")


def parse_payload(request, expected_kind: str) -> Payload:
    """Parse the compact v1 envelope and reject partial/ambiguous input."""
    check_body_size(request)
    raw = request.POST.get("production_payload")
    if raw is None:
        raise PayloadError("This form needs its compact save handler. Reload it before saving.")
    if len(raw.encode("utf-8")) > MAX_JSON_BYTES:
        raise PayloadError("This production form is over the 2 MiB payload limit.")
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise PayloadError("The production form payload is invalid. Nothing was saved.")
    if not isinstance(data, dict) or data.get("version") != 1:
        raise PayloadError("This production form version is not supported. Reload before saving.")
    if data.get("kind") != expected_kind:
        raise PayloadError("This production form does not match the page being saved.")
    rows = data.get("rows")
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise PayloadError("This production form has too many rows (maximum 10,000).")
    request_id = data.get("request_id")
    request_revision = data.get("request_revision")
    if expected_kind == "request":
        if request_id is not None and (not isinstance(request_id, str) or len(request_id) > MAX_KEY_LENGTH):
            raise PayloadError("The request identity is invalid.")
        _revision(request_revision, "Request revision")
    elif request_id is not None or request_revision is not None:
        raise PayloadError("Packing payloads must not carry a request revision.")

    seen = set()
    cleaned = []
    for row in rows:
        if not isinstance(row, dict):
            raise PayloadError("A production row is invalid.")
        key = row.get("key")
        if not isinstance(key, str) or not key or len(key) > MAX_KEY_LENGTH:
            raise PayloadError("A production row key is invalid.")
        if key in seen:
            raise PayloadError("A production row was sent more than once.")
        seen.add(key)
        value = _quantity(row.get("value"))
        base_value = _quantity(row.get("base_value"))
        if expected_kind == "packing":
            revision = _revision(row.get("revision"), "Packing revision")
        elif "revision" in row:
            raise PayloadError("Request rows must not carry packing revisions.")
        cleaned_row = {"key": key, "value": value, "base_value": base_value}
        if expected_kind == "packing":
            cleaned_row["revision"] = revision
        cleaned.append(cleaned_row)
    return Payload(expected_kind, request_id, request_revision, cleaned)


def fallback_row_count(request, prefix: str) -> int:
    return sum(1 for key in request.POST if key.startswith(prefix))


def submitted_draft(request, kind: str) -> dict[str, str]:
    """Best-effort raw draft retention after a validation/parser rejection."""
    raw = request.POST.get("production_payload")
    if raw:
        try:
            rows = json.loads(raw).get("rows", [])
            return {row["key"]: "" if row.get("value") is None else str(row.get("value"))
                    for row in rows if isinstance(row, dict) and isinstance(row.get("key"), str)}
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            pass
    prefix = "qty_" if kind == "request" else "packed_"
    return {key[len(prefix):]: value for key, value in request.POST.items() if key.startswith(prefix)}


def compact_rows_from_inputs(rows, kind):
    """Build a v1 envelope on the server for the <=900 field no-JS fallback."""
    if len(rows) > MAX_FALLBACK_FIELDS:
        raise PayloadError("This grid is too large to save without the compact form handler.")
    return rows
