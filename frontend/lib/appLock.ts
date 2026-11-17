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
 *
 * And one thing must never be thrown away by the lock: a payment QR the
 * customer may be paying right now. Signing out closes the payment screen,
 * and that screen's poll is the only thing that turns the payment into a
 * bill (Central Surat Thani, 17 Nov: the app was left 12 s after a ฿590 QR
 * went up, the customer paid at +10 s, the lock signed out at +12 s, and no
 * bill was saved). While a QR waits the screen holds the lock (`holdLock`);
 * coming back, the QR is still there and its poll finds the payment.
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
const holds = new Set<string>();

/** Keep the till signed in on return while `on` (e.g. a QR awaiting payment). */
export function holdLock(key: string, on: boolean): void {
  if (on) holds.add(key);
  else holds.delete(key);
}

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
      // Holds are read on return, not on leaving: a QR is still waiting
      // only if nothing settled it while the app was away.
      if (!exempt && away >= GRACE_MS && holds.size === 0) onLock();
    }
  });
  return () => sub.remove();
}
