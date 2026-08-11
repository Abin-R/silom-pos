// Thin fetch wrapper that:
//   1. Prepends EXPO_PUBLIC_BACKEND_URL + /api (callers pass `/path`)
//   2. Injects the bearer token from AsyncStorage so the backend can
//      look up the BranchSession and scope reads/writes to one branch.
//   3. On 401, clears the cached auth so the next call to / re-mounts the
//      login screen.  The active screen still needs to handle the failed
//      response (return null / show retry), but at least the session is
//      gone instead of looping with a dead token.
//   4. Reports every failed call to Sentry.  Callers each handle failure
//      their own way (empty list, retry banner, silent catch), so this is
//      the only place that sees ALL of them — without it a 500 or a dropped
//      wifi connection leaves no trace anywhere off the tablet.
import AsyncStorage from "@react-native-async-storage/async-storage";
import * as Sentry from "@sentry/react-native";

const ROOT = `${process.env.EXPO_PUBLIC_BACKEND_URL}/api`;
const AUTH_KEY = "bravepos:auth:v1";

/**
 * Path with the API root, query string and record ids stripped, e.g.
 * `https://…/api/orders/42/status?x=1` → `/orders/:id/status`.  Sentry groups
 * issues by message, so without collapsing the ids every order id would open
 * its own issue and bury the pattern.
 */
function routeOf(path: string): string {
  const bare = (path.startsWith("http") ? path.replace(ROOT, "") : path).split("?")[0];
  return bare
    .replace(/\/[0-9a-f]{8}-[0-9a-f-]{27,}/gi, "/:uuid")
    .replace(/\/\d+/g, "/:id");
}

/**
 * Was this fetch rejection the tablet losing its connection, rather than
 * anything wrong with our code?  Matches the platform-specific wordings we
 * actually see in Sentry (Android throws `UnknownHostException`, iOS and the
 * web build say "Network request failed").
 */
function isOffline(e: unknown): boolean {
  const msg = String((e as Error)?.message ?? e ?? "");
  return (
    msg.includes("Network request failed") ||
    msg.includes("UnknownHostException") ||
    msg.includes("Unable to resolve host") ||
    msg.includes("ECONNREFUSED") ||
    msg.includes("Connection reset") ||
    msg.includes("timed out")
  );
}

/**
 * `res.json()` when the response might not be JSON.
 *
 * A Django 500 (or an nginx 502, or a captive-portal splash page) is HTML, and
 * `res.json()` on it throws `SyntaxError: JSON Parse error: Unexpected
 * character: <` — which reports as a frontend bug and buries the actual
 * backend fault that caused it.  Prefer this wherever the caller can cope with
 * a missing body; it returns `fallback` instead of throwing.
 */
export async function safeJson<T>(res: Response, fallback: T): Promise<T> {
  try {
    const text = await res.text();
    if (!text) return fallback;
    return JSON.parse(text) as T;
  } catch {
    return fallback;
  }
}

let cachedToken: string | null | undefined = undefined;

async function getToken(): Promise<string | null> {
  if (cachedToken !== undefined) return cachedToken;
  try {
    const raw = await AsyncStorage.getItem(AUTH_KEY);
    cachedToken = raw ? (JSON.parse(raw)?.token ?? null) : null;
  } catch {
    cachedToken = null;
  }
  return cachedToken ?? null;
}

export function setAuthToken(token: string | null): void {
  cachedToken = token;
}

export function clearAuthToken(): void {
  cachedToken = null;
}

export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const token = await getToken();
  const headers: Record<string, string> = {
    ...(init.headers as Record<string, string> | undefined),
  };
  if (token) headers.Authorization = `Bearer ${token}`;
  // Default Content-Type for JSON bodies — callers can override.
  if (init.body && !headers["Content-Type"] && !headers["content-type"]) {
    headers["Content-Type"] = "application/json";
  }
  const url = path.startsWith("http") ? path : `${ROOT}${path.startsWith("/") ? path : `/${path}`}`;
  const method = (init.method ?? "GET").toUpperCase();
  const route = routeOf(path);

  let res: Response;
  try {
    res = await fetch(url, { ...init, headers });
  } catch (e) {
    // fetch() only rejects on a network-level failure — wifi dropped, DNS
    // dead, server unreachable.  An HTTP 500 resolves normally and is handled
    // below.  This branch is the only signal that a tablet went offline.
    //
    // A shop tablet leaving wifi range is normal operations, not a defect, so
    // it reports at `warning` — enough to spot a site that is permanently
    // unreachable, without an offline till outranking real bugs in triage.
    Sentry.captureException(e, {
      level: isOffline(e) ? "warning" : "error",
      tags: { api_route: route, api_method: method, api_status: "network" },
    });
    throw e;
  }

  if (res.status === 401) {
    // Token rejected (only happens with a stale/forged token — the "block on
    // second login" flow means valid sessions don't get yanked out from under
    // an active user).  Drop the cached copy so the next mount of / starts
    // fresh; screens that handle a 401 response will fall back to empty data.
    // Expected control flow, not an error — deliberately not reported.
    cachedToken = null;
    try {
      await AsyncStorage.removeItem(AUTH_KEY);
    } catch {}
    return res;
  }

  if (!res.ok) {
    // Read from a clone so the caller's res.json()/res.text() still works.
    const body = await res.clone().text().catch(() => "");
    Sentry.captureException(new Error(`API ${method} ${route} → ${res.status}`), {
      level: res.status >= 500 ? "error" : "warning",
      tags: { api_route: route, api_method: method, api_status: String(res.status) },
      extra: { url, body: body.slice(0, 500) },
    });
  }
  return res;
}

/**
 * Stamp reports with who/where, so an alert says "Cashier Ploy at Silom, order
 * save failed" instead of just "an error happened somewhere".  Called on login;
 * Sentry keeps this on the scope for every later event from this tablet.
 */
export function setSentryContext(ctx: {
  staffId?: string | number;
  staffName?: string;
  role?: string;
  branchName?: string;
}): void {
  Sentry.setUser(
    ctx.staffId != null || ctx.staffName
      ? { id: ctx.staffId != null ? String(ctx.staffId) : undefined, username: ctx.staffName }
      : null,
  );
  Sentry.setTag("branch", ctx.branchName ?? "unknown");
  Sentry.setTag("role", ctx.role ?? "unknown");
}

export const API_ROOT = ROOT;
