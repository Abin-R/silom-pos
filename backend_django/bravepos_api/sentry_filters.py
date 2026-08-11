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

import threading
import time


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
