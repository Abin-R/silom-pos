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
// A sign-in always checks (checkOnLogin), but two staff signing in back to
// back need not ask twice.
const MIN_LOGIN_CHECK_GAP_MS = 30_000;
const APPLY_TICK_MS = 2_000;
// How long the till must stay idle before we restart — long enough that a
// cashier tapping a product right after a sale is not cut off.
const IDLE_BEFORE_APPLY_MS = 3_000;

// Paths where a restart is allowed (when nothing is busy). Anything else —
// the admin screens — waits.
const SAFE_PATHS = new Set(["/", "/pos"]);

const busy = new Set<string>();
let updateReady = false;
// How long the "Updating…" screen stays up before the reload, so staff see
// why the till is about to blank for a moment.
const INSTALL_NOTICE_MS = 1_500;

/**
 * What the till shows about updates (components/OtaStatus):
 *  - downloading: a small note while the new version comes down
 *  - ready: a small note until the till is free to install it
 *  - installing: a full-screen "Updating…" just before the restart
 */
export type OtaStatus = "idle" | "downloading" | "ready" | "installing";
let status: OtaStatus = "idle";
const statusListeners = new Set<(s: OtaStatus) => void>();

function setStatus(next: OtaStatus) {
  if (status === next) return;
  status = next;
  statusListeners.forEach((l) => l(next));
}

/** Current update status, and a subscription to changes. */
export function getOtaStatus(): OtaStatus {
  return status;
}
export function subscribeOtaStatus(l: (s: OtaStatus) => void): () => void {
  statusListeners.add(l);
  return () => { statusListeners.delete(l); };
}
let checking = false;
let lastCheckAt = 0;

/** Declare a screen busy (true) or idle (false) under a key of its own. */
export function setBusy(key: string, on: boolean): void {
  if (on) busy.add(key);
  else busy.delete(key);
}

/**
 * Is the till free to be interrupted — on the PIN or sales screen, with no
 * cart, payment, popup or print in progress?  Also used by lib/storeUpdate
 * to decide when the store-update prompt may appear.
 */
export function isTillIdle(path: string): boolean {
  return SAFE_PATHS.has(path) && busy.size === 0 && !isDialogOpen() && !isPrinting();
}

function note(message: string, data?: Record<string, unknown>) {
  Sentry.addBreadcrumb({ category: "ota-auto", message, data, level: "info" });
}

let started = false;

async function checkAndFetch(minGapMs = MIN_CHECK_GAP_MS): Promise<void> {
  if (!started || checking || updateReady) return;
  if (Date.now() - lastCheckAt < minGapMs) return;
  checking = true;
  lastCheckAt = Date.now();
  try {
    const check = await Updates.checkForUpdateAsync();
    if (!check.isAvailable) return;
    setStatus("downloading");
    const fetched = await Updates.fetchUpdateAsync();
    if (fetched.isNew) {
      updateReady = true;
      note("update downloaded, waiting for an idle moment");
    }
  } catch (e: any) {
    // Offline tills are normal; never alert, just leave a trail.
    note("check failed", { error: String(e?.message ?? e) });
  } finally {
    setStatus(updateReady ? "ready" : "idle");
    checking = false;
  }
}

/**
 * Every sign-in checks for an update, however recently the timer did.  If
 * there is one it downloads now and installs as soon as the sales screen is
 * idle — usually seconds after the PIN, before the first item is rung up.
 */
export function checkOnLogin(): void {
  void checkAndFetch(MIN_LOGIN_CHECK_GAP_MS);
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
  started = true;

  // expo-updates' own check at launch may download an update in the
  // background; its native state says so, and that counts as ready too.
  const markIfPending = (pending: boolean | undefined) => {
    if (pending && !updateReady) {
      updateReady = true;
      setStatus("ready");
      note("update downloaded at launch, waiting for an idle moment");
    }
  };
  markIfPending(Updates.latestContext?.isUpdatePending);
  const stateSub = Updates.addUpdatesStateChangeListener((e) =>
    markIfPending(e.context?.isUpdatePending),
  );

  let idleSince: number | null = null;
  let applying = false;

  const isIdle = () => isTillIdle(getPath());

  const apply = async () => {
    applying = true;
    note("restarting into the downloaded update", { path: getPath() });
    setStatus("installing");
    try {
      await AsyncStorage.setItem(RESUME_KEY, JSON.stringify({ at: Date.now() }));
      await new Promise((r) => setTimeout(r, INSTALL_NOTICE_MS));
      // Something may have started while the notice was up (a self-order
      // slip arriving); if so, stand down and wait for the next idle moment.
      if (!isIdle()) {
        await AsyncStorage.removeItem(RESUME_KEY);
        setStatus("ready");
        idleSince = null;
        applying = false;
        return;
      }
      await Updates.reloadAsync();
    } catch (e: any) {
      // Reload refused — clear the marker so a later manual reopen is not
      // mistaken for our restart, and try again on a later tick.
      try { await AsyncStorage.removeItem(RESUME_KEY); } catch {}
      note("reload failed", { error: String(e?.message ?? e) });
      setStatus("ready");
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
    started = false;
  };
}
