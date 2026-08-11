// Client-side event throttling for Sentry — the tablet half of the same
// problem `backend_django/bravepos_api/sentry_filters.py` solves on the server.
//
// Why: the POS polls.  `useSelfOrderPrinting` hits /self-orders/pending on a
// timer, and every screen refetches on focus.  When the backend broke on
// 2026-08-11 that turned one root cause into 279 identical "API GET
// /self-orders/pending → 500" events from three tablets — and a tablet that
// merely walks out of wifi range produced another 29 of the same DNS failure.
// Neither number tells us anything the first event didn't.
//
// What: count events per grouping key and send on a log scale — the 1st, 2nd,
// 3rd, 5th, 10th, 25th … occurrence.  A one-off error still reports instantly
// and unsampled; a storm settles into a heartbeat.  Counters reset after 30
// minutes of quiet so tomorrow's recurrence is not silenced by today's tally.

const SEND_AT = [1, 2, 3, 5, 10, 25, 50, 100, 250, 500, 1000];
const TAIL_STEP = 1000;
const RESET_AFTER_MS = 30 * 60 * 1000;
const MAX_KEYS = 256;

type Entry = { count: number; lastSeen: number };

const seen = new Map<string, Entry>();

/**
 * Collapse the parts of a message that vary per occurrence but not per bug —
 * ids, timestamps, hex blobs — so N spellings of one fault share a counter.
 * `routeOf` in api.ts already does this for API errors; this catches the rest.
 */
function normalize(text: string): string {
  return text
    .replace(/\b[0-9a-f]{8}-[0-9a-f-]{27,}\b/gi, "<uuid>")
    .replace(/\b\d{4,}\b/g, "<n>")
    .slice(0, 300);
}

function groupingKey(event: any): string {
  const values = event?.exception?.values;
  if (Array.isArray(values) && values.length) {
    const last = values[values.length - 1];
    return `exc|${last?.type ?? "?"}|${normalize(String(last?.value ?? ""))}`;
  }
  const msg = event?.message ?? event?.logentry?.message ?? "";
  return `msg|${normalize(String(msg))}`;
}

function bump(key: string, now: number): number {
  const entry = seen.get(key);
  if (!entry || now - entry.lastSeen > RESET_AFTER_MS) {
    seen.set(key, { count: 1, lastSeen: now });
    return 1;
  }
  entry.count += 1;
  entry.lastSeen = now;

  if (seen.size > MAX_KEYS) {
    const cutoff = now - RESET_AFTER_MS;
    for (const [k, v] of seen) if (v.lastSeen < cutoff) seen.delete(k);
  }
  return entry.count;
}

function shouldSend(count: number): boolean {
  if (SEND_AT.includes(count)) return true;
  return count > SEND_AT[SEND_AT.length - 1] && count % TAIL_STEP === 0;
}

/** How many suppressed occurrences this send stands in for. */
function represented(count: number): number {
  if (count > SEND_AT[SEND_AT.length - 1]) return TAIL_STEP;
  let prev = 0;
  for (const step of SEND_AT) if (step < count) prev = step;
  return Math.max(1, count - prev);
}

/**
 * Sentry `beforeSend` hook. Returns the event, or null to drop it.
 * Every capture goes through here — including the explicit
 * `captureException` calls in api.ts — so this is the single choke point.
 */
export function throttleBeforeSend(event: any): any | null {
  try {
    const count = bump(groupingKey(event), Date.now());
    if (!shouldSend(count)) return null;

    // Carry the volume we suppressed, so triage can still tell a fault that
    // fired 4 times from one that fired 1,000.
    event.extra = {
      ...(event.extra ?? {}),
      occurrence_count: count,
      occurrences_since_last_report: represented(count),
    };
    return event;
  } catch {
    // A bug in the filter must never cost us a real report.
    return event;
  }
}
