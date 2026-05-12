// Thin fetch wrapper that:
//   1. Prepends EXPO_PUBLIC_BACKEND_URL + /api (callers pass `/path`)
//   2. Injects the bearer token from AsyncStorage so the backend can
//      look up the BranchSession and scope reads/writes to one branch.
//   3. On 401, clears the cached auth so the next call to / re-mounts the
//      login screen.  The active screen still needs to handle the failed
//      response (return null / show retry), but at least the session is
//      gone instead of looping with a dead token.
import AsyncStorage from "@react-native-async-storage/async-storage";

const ROOT = `${process.env.EXPO_PUBLIC_BACKEND_URL}/api`;
const AUTH_KEY = "bravepos:auth:v1";

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
  const res = await fetch(url, { ...init, headers });
  if (res.status === 401) {
    // Token rejected (replaced by another login, or never valid).  Drop the
    // cached copy so the next mount of / starts fresh.
    cachedToken = null;
    try {
      await AsyncStorage.removeItem(AUTH_KEY);
    } catch {}
  }
  return res;
}

export const API_ROOT = ROOT;
