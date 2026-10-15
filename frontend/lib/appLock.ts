/**
 * Back to the PIN pad whenever the till has been left.
 *
 * A counter tablet is shared. If whoever was signed in switches to another
 * app, turns the screen off, or the app is closed, the next person to pick it
 * up must not inherit their session — so on return the app signs out and shows
 * the login screen. (Cold start is handled in app/index.tsx, which no longer
 * restores a saved session.)
 *
 * Two kinds of "leaving" are not leaving:
 *  - The app's own trips out — photo picker, share sheet, opening the
 *    backoffice in the browser. Wrap those in `whileAway` so coming back from
 *    them doesn't throw away the edit that sent you there.
 *  - Blips under GRACE_MS. Android's USB-permission prompt for the printer is
 *    a separate system activity, so the app reports "background" while it is
 *    up; a cashier answering it shouldn't lose the cart.
 */
import { AppState } from "react-native";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { apiFetch, clearAuthToken } from "./api";

const AUTH_KEY = "bravepos:auth:v1";
const GRACE_MS = 10_000;
// openURL / Share resolve before Android reports "background", so a counter
// alone would miss them; a short window after the call catches the switch.
const EXCURSION_WINDOW_MS = 5_000;

let excursions = 0;
let excursionUntil = 0;

/** Run an action that leaves the app on purpose without tripping the lock. */
export async function whileAway<T>(fn: () => Promise<T> | T): Promise<T> {
  excursions++;
  excursionUntil = Date.now() + EXCURSION_WINDOW_MS;
  try {
    return await fn();
  } finally {
    excursions--;
  }
}

/** Drop the session on this device and tell the server it has ended. */
export async function endSession(): Promise<void> {
  try {
    await apiFetch("/auth/logout", { method: "POST", body: JSON.stringify({}) });
  } catch {}
  clearAuthToken();
  try { await AsyncStorage.removeItem(AUTH_KEY); } catch {}
}

/**
 * Call `onLock` when the app returns to the foreground after being away.
 * Whether a trip counts is decided when the app leaves, not when it returns —
 * the order of the AppState event and the excursion's promise isn't fixed.
 */
export function startAppLock(onLock: () => void): () => void {
  let leftAt: number | null = null;
  let exempt = false;
  const sub = AppState.addEventListener("change", (state) => {
    if (state === "background") {
      if (leftAt !== null) return;
      leftAt = Date.now();
      exempt = excursions > 0 || Date.now() < excursionUntil;
      return;
    }
    if (state === "active" && leftAt !== null) {
      const away = Date.now() - leftAt;
      leftAt = null;
      if (!exempt && away >= GRACE_MS) onLock();
    }
  });
  return () => sub.remove();
}
