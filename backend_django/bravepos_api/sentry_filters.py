"""Client-side event throttling for Sentry.

Why this exists
---------------
On 2026-08-11 a ``git pull`` landed while gunicorn was serving.  Django
imports the URLconf lazily on the first request, so a worker imported the
*new* ``bravepos.peak`` against the *old* ``bravepos.models`` and raised
``ImportError: cannot import name 'ConsolidatedReceipt'``.  Python caches
the half-built modules in ``sys.modules``, so every later request on that
worker raised the same thing — until someone restarted the service hours
later.  The POS tablets poll ``/self-orders/pending`` on a timer, so the
outage turned into **1,041 identical Sentry events** for one root cause.

A thousand copies of one traceback is strictly worse than ten: it burns
the event quota, and the issue list stops being readable at a glance.

What it does
------------
Events are counted per grouping key and sent on a **log scale** — the
1st, 2nd, 3rd, 5th, 10th, 25th, 50th, 100th, 250th … occurrence.  So a
storm that would have been 1,041 events becomes ~11, while an error that
fires once still reports immediately.

The true volume is not lost, just moved: each event carries an
``occurrence_count`` extra saying which occurrence it was, and
``occurrences_since_last_report`` saying how many it stands in for.  So
the issue list shows ~11 events and the newest one says "occurrence
1000" — which is the actual goal, since a count of 1,041 told us nothing
that "still happening, a lot" doesn't.

Counters reset after ``_RESET_AFTER`` seconds of quiet, so a fault that
recurs next week opens with a fresh unsampled event instead of being
silenced by last week's tally.

Scope: per worker process (gunicorn runs 3), so real ceilings are ~3x the
numbers above.  That is deliberate — cross-process coordination would
mean Redis, and the point here is to blunt a firehose, not to be exact.
"""
from __future__ import annotations

import re
import threading
import time
from urllib.parse import urlsplit


# Occurrence numbers that actually get sent. Past the last explicit step we
# fall back to every `_TAIL_STEP`-th event, so a truly endless storm keeps a
# faint heartbeat instead of going completely silent.
_SEND_AT = (1, 2, 3, 5, 10, 25, 50, 100, 250, 500, 1000)
_TAIL_STEP = 1000

# Quiet period after which a key's counter is forgotten.
_RESET_AFTER = 30 * 60  # 30 minutes

_lock = threading.Lock()
# key -> [count, last_seen_monotonic]
_seen: dict = {}


def _grouping_key(event, hint) -> str:
    """A stand-in for Sentry's server-side fingerprint.

    Sentry groups on the server, so the SDK cannot know the real issue id
    here.  Exception type + the innermost app frame is close enough: it is
    what makes two tracebacks "the same bug" in practice, and being
    slightly coarser than Sentry only means we throttle a little harder.
    """
    exc = (event.get("exception") or {}).get("values") or []
    if exc:
        last = exc[-1]
        etype = last.get("type") or "?"
        frames = ((last.get("stacktrace") or {}).get("frames")) or []
        # Walk from the innermost frame outwards for the first in-app frame;
        # django/ site-packages frames are identical across unrelated bugs.
        loc = ""
        for frame in reversed(frames):
            if frame.get("in_app"):
                loc = "{}:{}".format(frame.get("module") or frame.get("filename"),
                                     frame.get("function"))
                break
        if not loc and frames:
            inner = frames[-1]
            loc = "{}:{}".format(inner.get("module") or inner.get("filename"),
                                 inner.get("function"))
        return "exc|{}|{}".format(etype, loc)

    # Non-exception events (logger errors, messages) group on their text.
    msg = (event.get("logentry") or {}).get("message") or event.get("message") or ""
    return "msg|{}|{}".format(event.get("logger") or "", msg[:200])


def _bump(key: str) -> int:
    """Record one occurrence of `key`; return its running count."""
    now = time.monotonic()
    with _lock:
        entry = _seen.get(key)
        if entry is None or (now - entry[1]) > _RESET_AFTER:
            _seen[key] = [1, now]
            return 1
        entry[0] += 1
        entry[1] = now

        # Cheap bound on the dict: a process that has seen this many distinct
        # keys is either under attack or long-lived enough that stale entries
        # are worthless. Drop anything idle rather than growing forever.
        if len(_seen) > 512:
            cutoff = now - _RESET_AFTER
            for k in [k for k, v in _seen.items() if v[1] < cutoff]:
                del _seen[k]

        return entry[0]


def _should_send(count: int) -> bool:
    if count in _SEND_AT:
        return True
    return count > _SEND_AT[-1] and count % _TAIL_STEP == 0


def before_send(event, hint):
    """Sentry ``before_send`` hook — return the event, or None to drop it."""
    try:
        key = _grouping_key(event, hint)
        count = _bump(key)
        if not _should_send(count):
            return None

        # Carry the volume we suppressed, so triage can still tell a fault
        # that fired 4 times from one that fired 1,000.
        prev = 0
        for step in _SEND_AT:
            if step < count:
                prev = step
        if count > _SEND_AT[-1]:
            prev = count - _TAIL_STEP

        extra = event.setdefault("extra", {})
        extra["occurrence_count"] = count
        extra["occurrences_since_last_report"] = max(1, count - prev)
        return event
    except Exception:
        # Never let the filter itself swallow a report.
        return event


# ── Self-order routing ───────────────────────────────────────────────────────
# The customer self-order site (`/order/...`) reports to its own Sentry
# project, so a customer's phone failing to pay never sits in the same issue
# list as the tills' API errors.  The SDK takes one DSN per process, so the
# main client keeps capturing everything and its before_send hands self-order
# events to a second client (built in settings) instead of sending them.
# The till's own /api/self-orders/... endpoints stay in the backend project:
# those are staff calls, not the customer site.
SELFORDER_PREFIX = "/order/"

# /order/s/<token>/ — the token is the customer's handle on their order, so
# it is masked before an event leaves the server.
_TOKEN_RE = re.compile(r"(/order/s/)[^/?#]+")


def is_selforder_event(event) -> bool:
    if (event.get("transaction") or "").startswith(SELFORDER_PREFIX):
        return True
    url = (event.get("request") or {}).get("url") or ""
    try:
        return urlsplit(url).path.startswith(SELFORDER_PREFIX)
    except ValueError:
        return False


def _mask_token(event) -> None:
    request = event.get("request")
    if isinstance(request, dict) and isinstance(request.get("url"), str):
        request["url"] = _TOKEN_RE.sub(r"\1[token]", request["url"])
    if isinstance(event.get("transaction"), str):
        event["transaction"] = _TOKEN_RE.sub(r"\1[token]", event["transaction"])


def make_router(selforder_client):
    """before_send / before_send_transaction hooks for the main client.

    With no self-order client (DSN unset) events go to the main project
    exactly as before.  If handing an event over fails, it falls back to the
    main project rather than being lost.
    """

    def _hand_over(event, hint):
        if selforder_client is None or not is_selforder_event(event):
            return event
        try:
            _mask_token(event)
            selforder_client.capture_event(event, hint)
            return None
        except Exception:
            return event

    def route_event(event, hint):
        event = before_send(event, hint)
        if event is None:
            return None
        return _hand_over(event, hint)

    def route_transaction(event, hint):
        return _hand_over(event, hint)

    return route_event, route_transaction
