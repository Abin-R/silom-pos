// Offline-resilient print queue.
//
// When the printer is unreachable (power off, Wi-Fi dropped, OTG cable
// unplugged), receipts get queued here instead of being dropped on the
// floor.  useStarPrinter drains the queue every 30s in the background;
// successful prints are removed from the queue, failures bump the
// attempts counter so we can stop retrying after a sane upper bound.
//
// Why AsyncStorage and not just in-memory: an unattended printer
// outage could last minutes-to-hours.  The cashier may close the app,
// the tablet may sleep, the device may even reboot during the outage.
// The queue has to survive all of those — that means persistent disk.

import AsyncStorage from "@react-native-async-storage/async-storage";
import type { PrinterConfig, ReceiptOrder, ReceiptShop } from "./starPrinter";

const QUEUE_KEY = "bravepos:printer:queue:v1";

// Receipts older than this are auto-removed on the next drain.  Two
// hours is a reasonable "give up" threshold — a power outage longer
// than that probably means we have bigger problems than reprints, and
// the customer is long gone anyway.
const MAX_AGE_MS = 2 * 60 * 60 * 1000;

// After this many failed attempts we stop retrying.  Saves us from
// spamming an offline printer indefinitely if e.g. the IP changed.
const MAX_ATTEMPTS = 60;        // 60 × 30s = 30 min of retries

export type PrintJob = {
  id: string;
  config: PrinterConfig;
  order: ReceiptOrder;
  shop: ReceiptShop;
  attempts: number;
  lastAttemptAt: number;        // ms epoch, 0 = never
  createdAt: number;            // ms epoch
  lastError?: string;
};

// In-flight claim set.  When two useStarPrinter instances are mounted at
// the same time (e.g. pos.tsx + admin.tsx live in the navigation stack),
// their independent drainers can both read the same job out of
// AsyncStorage and both call printImage — producing duplicate physical
// receipts.  Single-instance ticks can race the same way between the
// listJobs() read and the removeJob() write.
//
// To prevent that, nextDueJob atomically claims the job by adding its id
// to this Set before returning it.  Any other drainer skips claimed
// jobs.  release(id) MUST be called by the caller after the print
// attempt completes (success OR failure) — otherwise the job sticks in
// the in-flight set forever and never gets retried.
//
// Module state is shared across the entire JS runtime, so a single Set
// is enough to coordinate every useStarPrinter instance in the app.
const inFlight = new Set<string>();

export function release(id: string): void {
  inFlight.delete(id);
}

export async function listJobs(): Promise<PrintJob[]> {
  try {
    const raw = await AsyncStorage.getItem(QUEUE_KEY);
    return raw ? (JSON.parse(raw) as PrintJob[]) : [];
  } catch {
    return [];
  }
}

async function save(jobs: PrintJob[]): Promise<void> {
  await AsyncStorage.setItem(QUEUE_KEY, JSON.stringify(jobs));
}

export async function enqueue(
  config: PrinterConfig,
  order: ReceiptOrder,
  shop: ReceiptShop,
  initialError?: string,
): Promise<PrintJob> {
  const jobs = await listJobs();
  // Don't double-enqueue the same order — if the cashier hits a retry
  // button on a row that's already queued, we'd otherwise stack copies.
  const existing = jobs.find((j) => j.order.order_number === order.order_number);
  if (existing) return existing;

  const job: PrintJob = {
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    config,
    order,
    shop,
    attempts: 0,
    lastAttemptAt: 0,
    createdAt: Date.now(),
    lastError: initialError,
  };
  await save([...jobs, job]);
  return job;
}

export async function removeJob(id: string): Promise<void> {
  const jobs = await listJobs();
  await save(jobs.filter((j) => j.id !== id));
  // Job no longer exists in the queue, so any in-flight claim is moot —
  // clear it so we don't leak a permanent entry in the Set if the same
  // id ever gets re-used.
  inFlight.delete(id);
}

export async function recordAttempt(id: string, error?: string): Promise<void> {
  const jobs = await listJobs();
  const next = jobs.map((j) =>
    j.id === id
      ? { ...j, attempts: j.attempts + 1, lastAttemptAt: Date.now(), lastError: error }
      : j,
  );
  await save(next);
}

/**
 * Returns the next job that's a candidate for retry (haven't tried
 * recently AND under the attempt cap AND not too old).  Also cleans
 * out stale jobs as a side effect.
 */
export async function nextDueJob(retryIntervalMs: number): Promise<PrintJob | null> {
  const now = Date.now();
  const jobs = await listJobs();
  const fresh = jobs.filter((j) => now - j.createdAt < MAX_AGE_MS);
  if (fresh.length < jobs.length) await save(fresh);

  for (const j of fresh) {
    if (inFlight.has(j.id)) continue;                  // claimed by another drainer
    if (j.attempts >= MAX_ATTEMPTS) continue;
    if (now - j.lastAttemptAt < retryIntervalMs) continue;
    // Atomic claim: from here until release(j.id) no other drainer can
    // pick this job.  The Set.add + return below is synchronous so
    // there's no await between checking and claiming.
    inFlight.add(j.id);
    return j;
  }
  return null;
}
