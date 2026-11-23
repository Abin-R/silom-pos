"""Record which tablet runs which app build, from the headers on its API calls.

The till app (``frontend/lib/deviceHeaders.ts``) adds a handful of
``X-App-*`` / ``X-Device-*`` headers to every request.  ``views.get_session``
— the one place every authenticated POS call passes through — hands them here,
and they land in ``models.TillDevice``, one row per tablet.  The backoffice
Devices page reads that table.

Cost is kept to almost nothing: the decision to write is made from a
per-process dict, with no query, and a tablet's row is written at most once a
``THROTTLE_SECONDS`` per gunicorn worker unless something about it changed (a
new OTA, a different branch or staff), in which case it is written at once.

This must never be the reason a till's request fails.  Every error is caught
and logged, and the write runs in its own savepoint so a database error cannot
poison the transaction the view is about to use.

Python 3.8 on the server — keep the syntax to it.
"""
from __future__ import annotations

import logging
import time
from datetime import timezone as dt_timezone
from typing import Dict, Optional, Tuple

from django.db import transaction
from django.utils import timezone as djtz
from django.utils.dateparse import parse_datetime

log = logging.getLogger(__name__)

THROTTLE_SECONDS = 60
# A bound, not a tuning knob: the dict only ever holds one entry per tablet.
_CACHE_LIMIT = 5000

# device_id -> (fingerprint of what was last written, monotonic time of it)
_last_write: Dict[str, Tuple[tuple, float]] = {}

# header -> TillDevice field, for the plain text ones.
TEXT_HEADERS = (
    ("X-Device-Platform", "platform"),
    ("X-Device-Manufacturer", "manufacturer"),
    ("X-Device-Model", "model"),
    ("X-Device-OS", "os_version"),
    ("X-App-Version", "app_version"),
    ("X-App-Runtime", "runtime_version"),
    ("X-App-Channel", "channel"),
    ("X-App-Update-Id", "update_id"),
)


def _max_len(field: str) -> int:
    from .models import TillDevice
    return TillDevice._meta.get_field(field).max_length


def _clean(value: Optional[str], field: str) -> str:
    return (value or "").strip()[: _max_len(field)]


def _parse_bool(value: str) -> Optional[bool]:
    v = (value or "").strip().lower()
    if v in ("1", "true", "yes"):
        return True
    if v in ("0", "false", "no"):
        return False
    return None


def reported_fields(headers) -> dict:
    """The version/device fields a request reports, or ``{}`` if it reports none.

    ``{}`` rather than a dict of blanks so a till running an older bundle (no
    headers) never wipes what a newer bundle on the same tablet reported.
    """
    if not headers.get("X-App-Version") and not headers.get("X-App-Runtime"):
        return {}
    out = {field: _clean(headers.get(header), field) for header, field in TEXT_HEADERS}
    published = parse_datetime((headers.get("X-App-Update-Published") or "").strip())
    if published is not None and djtz.is_naive(published):
        published = djtz.make_aware(published, dt_timezone.utc)
    out["update_published_at"] = published
    out["is_embedded"] = _parse_bool(headers.get("X-App-Embedded") or "")
    return out


def touch(request, sess) -> None:
    """Record this request's tablet.  Never raises."""
    try:
        _touch(request, sess)
    except Exception:  # noqa: BLE001 — telemetry must not break a sale
        log.warning("till device record failed", exc_info=True)


def _touch(request, sess) -> None:
    from .models import TillDevice

    headers = request.headers
    device_id = _clean(headers.get("X-Device-Id") or sess.device_id, "device_id")
    if not device_id:
        return  # a login from a bundle older than device ids: nothing to key on

    values = {"branch_id": sess.branch_id, "staff_id": sess.staff_id}
    values.update(reported_fields(headers))
    fingerprint = tuple(sorted((k, str(v)) for k, v in values.items()))

    now = time.monotonic()
    previous = _last_write.get(device_id)
    if previous and previous[0] == fingerprint and now - previous[1] < THROTTLE_SECONDS:
        return

    values["last_seen_at"] = djtz.now()
    with transaction.atomic():
        TillDevice.objects.update_or_create(device_id=device_id, defaults=values)

    if len(_last_write) >= _CACHE_LIMIT:
        _last_write.clear()
    _last_write[device_id] = (fingerprint, now)
