/**
 * Install over-the-air updates by themselves, while the till is in use.
 *
 * expo-updates on its own checks at launch, downloads in the background and
 * only switches on the launch *after* that — so a till had to be closed twice
 * to pick up a fix, and one left open all day never did.  This module does the
 * whole cycle from JS: it checks now and then, downloads quietly, and restarts
 * the app the first moment the till is not in the middle of something.
 *
 * "In the middle of something" is decided by the screens (setBusy): a cart
 * with items, a payment or any popup open, a PIN half typed, a slip printing.
 * Admin screens always count as busy — their forms are not tracked and an
 * unsaved product edit must not vanish.
 *
 * The restart keeps whoever is signed in signed in.  app/index.tsx normally
 * ends any session it finds at launch (a shared till must not reopen as the
 * last user), so just before reloading we leave a short-lived marker that
 * tells it this launch is our own restart, not someone reopening the app.
 *
 * No-op in dev, on web, and in builds without expo-updates.
 */
import { AppState, Platform } from "react-native";
import AsyncStorage from "@react-native-async-storage/async-storage";
import * as Updates from "expo-updates";
import * as Sentry from "@sentry/react-native";
import { isPrinting } from "./printerQueue";
import { isDialogOpen } from "./dialog";

const RESUME_KEY = "bravepos:ota-resume:v1";
// The marker only vouches for the launch straight after our own reload.
const RESUME_MAX_AGE_MS = 60_000;

const CHECK_EVERY_MS = 30 * 60_000;
// Expo asks apps not to poll in a tight loop; foregrounding a till often
// must not turn into a check every time.
const MIN_CHECK_GAP_MS = 5 * 60_000;
const APPLY_TICK_MS = 2_000;
// How long the till must stay idle before we restart — long enough that a
// cashier tapping a product right after a sale is not cut off.
const IDLE_BEFORE_APPLY_MS = 3_000;

// Paths where a restart is allowed (when nothing is busy). Anything else —
// the admin screens — waits.
const SAFE_PATHS = new Set(["/", "/pos"]);

const busy = new Set<string>();
let updateReady = false;
let checking = false;
let lastCheckAt = 0;

/** Declare a screen busy (true) or idle (false) under a key of its own. */
export function setBusy(key: string, on: boolean): void {
  if (on) busy.add(key);
  else busy.delete(key);
}

function note(message: string, data?: Record<string, unknown>) {
  Sentry.addBreadcrumb({ category: "ota-auto", message, data, level: "info" });
}

async function checkAndFetch(): Promise<void> {
  if (checking || updateReady) return;
  if (Date.now() - lastCheckAt < MIN_CHECK_GAP_MS) return;
  checking = true;
  lastCheckAt = Date.now();
  try {
    const check = await Updates.checkForUpdateAsync();
    if (!check.isAvailable) return;
    const fetched = await Updates.fetchUpdateAsync();
    if (fetched.isNew) {
      updateReady = true;
      note("update downloaded, waiting for an idle moment");
    }
  } catch (e: any) {
    // Offline tills are normal; never alert, just leave a trail.
    note("check failed", { error: String(e?.message ?? e) });
  } finally {
    checking = false;
  }
}

/**
 * Was this launch our own update restart?  Reads and deletes the marker, so
 * it can only ever vouch for one launch.
 */
export async function consumeResumeMarker(): Promise<boolean> {
  try {
    const raw = await AsyncStorage.getItem(RESUME_KEY);
    if (!raw) return false;
    await AsyncStorage.removeItem(RESUME_KEY);
    const at = Number(JSON.parse(raw)?.at);
    return Number.isFinite(at) && Date.now() - at >= 0 && Date.now() - at < RESUME_MAX_AGE_MS;
  } catch {
    return false;
  }
}

/** Start checking for and installing updates. Returns a cleanup function. */
export function startOtaAutoUpdate(getPath: () => string): () => void {
  if (__DEV__ || Platform.OS === "web" || !Updates.isEnabled) return () => {};

  // expo-updates' own check at launch may download an update in the
  // background; its native state says so, and that counts as ready too.
  const markIfPending = (pending: boolean | undefined) => {
    if (pending && !updateReady) {
      updateReady = true;
      note("update downloaded at launch, waiting for an idle moment");
    }
  };
  markIfPending(Updates.latestContext?.isUpdatePending);
  const stateSub = Updates.addUpdatesStateChangeListener((e) =>
    markIfPending(e.context?.isUpdatePending),
  );

  let idleSince: number | null = null;
  let applying = false;

  const isIdle = () =>
    SAFE_PATHS.has(getPath()) && busy.size === 0 && !isDialogOpen() && !isPrinting();

  const apply = async () => {
    applying = true;
    note("restarting into the downloaded update", { path: getPath() });
    try {
      await AsyncStorage.setItem(RESUME_KEY, JSON.stringify({ at: Date.now() }));
      await Updates.reloadAsync();
    } catch (e: any) {
      // Reload refused — clear the marker so a later manual reopen is not
      // mistaken for our restart, and try again on a later tick.
      try { await AsyncStorage.removeItem(RESUME_KEY); } catch {}
      note("reload failed", { error: String(e?.message ?? e) });
      applying = false;
    }
  };

  const tick = setInterval(() => {
    if (!updateReady || applying) return;
    if (!isIdle()) {
      idleSince = null;
      return;
    }
    if (idleSince === null) idleSince = Date.now();
    else if (Date.now() - idleSince >= IDLE_BEFORE_APPLY_MS) void apply();
  }, APPLY_TICK_MS);

  const poll = setInterval(() => {
    if (AppState.currentState === "active") void checkAndFetch();
  }, CHECK_EVERY_MS);

  const sub = AppState.addEventListener("change", (state) => {
    if (state === "active") void checkAndFetch();
  });

  void checkAndFetch();

  return () => {
    clearInterval(tick);
    clearInterval(poll);
    sub.remove();
    stateSub.remove();
  };
}
