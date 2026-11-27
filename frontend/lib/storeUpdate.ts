/**
 * Force a new store build (Google Play / App Store) onto the till.
 *
 * OTA updates (lib/otaAutoUpdate) only replace the JavaScript. Anything native
 * — an Expo SDK upgrade, a new native module — needs a new build from the
 * store. When one is live, the till goes straight to the store's own update
 * screen and cannot be used until it updates. There is no "Later".
 *
 *  - Android: Google Play's in-app update, *immediate* flow: Play's full-screen
 *    updater opens over the app, installs, and reopens the app. Its X (or
 *    Back) cannot be removed, so closing it closes Brave POS; reopening the app
 *    shows the updater again. Only works for an install that came from Play.
 *  - iOS: Apple has no in-app update and apps may not close themselves, so
 *    the till is covered by a full-screen "Update required" page
 *    (components/StoreUpdateRequired) and the App Store pop-up opens over it.
 *    Closing the pop-up leaves the page up; its Update now button opens the
 *    pop-up again. The page goes away only when the new version is installed
 *    (iOS restarts the app on it) or the backoffice switch is turned off.
 *    Nothing happens until the new version is live there.
 *
 * The update screen only opens when the till is idle (isTillIdle: PIN or sale
 * screen, no sale, payment, popup or print in progress) and never while an OTA
 * is downloading or installing. On launch that is within seconds.
 *
 * Off switch: backoffice Shop settings → "Force app updates on the tills"
 * (Settings.force_app_update, read from the public /api/app-config). Asked
 * only right before the update screen opens, so it costs nothing on a normal
 * day. If the backend can't be reached, the last answer seen stands (on, if
 * none yet).
 *
 * Kept light on purpose: a store check on launch, hourly, and on return to
 * the foreground (at most every 15 min); the idle watcher runs only while an
 * update is waiting.
 *
 * No-op in dev, on web, and in any build without the native module, so it is
 * safe to ship by OTA to a build that predates it.
 */
import { AppState, BackHandler, Linking, Platform } from "react-native";
import { requireOptionalNativeModule } from "expo";
import Constants from "expo-constants";
import * as Sentry from "@sentry/react-native";

import { apiFetch } from "./api";
import { showToast } from "./dialog";
import { t } from "./i18n";
import { getOtaStatus, isTillIdle } from "./otaAutoUpdate";

type InAppUpdates = typeof import("expo-in-app-updates");

// Give the first render and the OTA check a moment before asking the store.
const FIRST_CHECK_DELAY_MS = 2_000;
const CHECK_EVERY_MS = 60 * 60_000;
// Foregrounding a till is frequent; don't ask the store every time.
const MIN_CHECK_GAP_MS = 15 * 60_000;
const IDLE_TICK_MS = 1_000;
// A cashier tapping straight after a sale should not get the update screen
// under their finger.
const IDLE_BEFORE_UPDATE_MS = 3_000;
// The update screen could not be opened (store sheet failed to load, Play
// refused): try again after this.
const RETRY_AFTER_FAILURE_MS = 60_000;
// The update screen went away without a cancel event (Play redirected to the
// store page, an install failed): wait this long after the app is back in front
// before opening it again, so a late cancel event lands first.
const RESUME_GRACE_MS = 5_000;
const CONFIG_TIMEOUT_MS = 8_000;
// iOS: while the "Update required" page is up, look at the backoffice switch
// this often, so turning it off frees the till without a restart.
const SWITCH_RECHECK_MS = 5 * 60_000;

function loadModule(): InAppUpdates | null {
  if (__DEV__ || Platform.OS === "web") return null;
  // expo-in-app-updates throws at import when its native half is missing
  // (any build made before it was added), so look before requiring.
  if (!requireOptionalNativeModule("ExpoInAppUpdates")) return null;
  try {
    return require("expo-in-app-updates") as InAppUpdates;
  } catch {
    return null;
  }
}

function note(message: string, data?: Record<string, unknown>) {
  Sentry.addBreadcrumb({ category: "store-update", message, data, level: "info" });
}

function openStorePage() {
  const pkg = Constants.expoConfig?.android?.package;
  if (Platform.OS !== "android" || !pkg) return;
  Linking.openURL(`market://details?id=${pkg}`).catch(() =>
    Linking.openURL(`https://play.google.com/store/apps/details?id=${pkg}`).catch(() => {}),
  );
}

// Last answer from /api/app-config. On until the backoffice says otherwise.
let forcedUpdatesOn = true;

async function readForcedUpdatesSwitch(): Promise<boolean> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), CONFIG_TIMEOUT_MS);
  try {
    const res = await apiFetch("/app-config", { signal: controller.signal });
    if (res.ok) {
      const body = await res.json();
      if (typeof body?.force_app_update === "boolean") forcedUpdatesOn = body.force_app_update;
    }
  } catch {
    // Offline or the backend is down: keep the last answer.
  } finally {
    clearTimeout(timer);
  }
  return forcedUpdatesOn;
}

// iOS "Update required" page state, read by components/StoreUpdateRequired.
export type StoreUpdateGate = {
  required: boolean;
  version: string | null;
  // The App Store pop-up is being opened (Update now shows a spinner).
  opening: boolean;
};
let gate: StoreUpdateGate = { required: false, version: null, opening: false };
const gateListeners = new Set<(g: StoreUpdateGate) => void>();

function setGate(patch: Partial<StoreUpdateGate>) {
  gate = { ...gate, ...patch };
  gateListeners.forEach((l) => l(gate));
}

export function getStoreUpdateGate(): StoreUpdateGate {
  return gate;
}

export function subscribeStoreUpdateGate(l: (g: StoreUpdateGate) => void): () => void {
  gateListeners.add(l);
  return () => gateListeners.delete(l);
}

// Set by the running checker: Update now on the page.
let openFromPage: (() => void) | null = null;

/** The page's Update now button: open the App Store pop-up again. */
export function openStoreUpdate() {
  openFromPage?.();
}

// The root layout can mount twice (activity recreated after exitApp); only
// one checker may run.
let stopRunning: (() => void) | null = null;

/** Start checking the store for a newer build. Returns a cleanup function. */
export function startStoreUpdateCheck(getPath: () => string): () => void {
  stopRunning?.();
  const mod = loadModule();
  if (!mod) return () => {};

  let available = false;
  let storeVersion: string | null = null;
  let checking = false;
  let lastCheckAt = 0;
  // An update screen is open (or being opened) right now.
  let showing = false;
  // Don't open the update screen before this time.
  let holdUntil = 0;
  // Android: the app was sent away because the updater was closed.
  let closedByCancel = false;
  let idleSince: number | null = null;
  let idleTimer: ReturnType<typeof setInterval> | null = null;

  const stopIdleWatch = () => {
    if (idleTimer) clearInterval(idleTimer);
    idleTimer = null;
    idleSince = null;
  };

  const check = async (force = false) => {
    if (checking || available) return;
    if (!force && Date.now() - lastCheckAt < MIN_CHECK_GAP_MS) return;
    checking = true;
    lastCheckAt = Date.now();
    try {
      const info = await mod.checkForUpdate();
      // Android: an immediate update that was interrupted reports as "in
      // progress"; startUpdate resumes it.
      if (info.updateAvailable) {
        available = true;
        storeVersion = info.storeVersion ?? null;
        note("store update available", {
          storeVersion: info.storeVersion ?? null,
          inProgress: info.updateInProgress ?? false,
        });
        startIdleWatch();
      }
    } catch (e: any) {
      // Offline, or not installed from the store: normal, never alert.
      note("check failed", { error: String(e?.message ?? e) });
    } finally {
      checking = false;
    }
  };

  // iOS: the backoffice switch was turned off while the page was up.
  const unblock = () => {
    note("forced updates switched off, page closed");
    available = false;
    showing = false;
    setGate({ required: false, opening: false });
  };

  // iOS: open the App Store pop-up over the "Update required" page. A failure
  // (the pop-up can't load, or one is already up after the iPad slept) leaves
  // the page with its Update now button to try again.
  const presentIosSheet = async (checkSwitch: boolean) => {
    if (gate.opening) return;
    setGate({ opening: true });
    try {
      if (checkSwitch && !(await readForcedUpdatesSwitch())) {
        unblock();
        return;
      }
      showing = true;
      note("opening update screen");
      await mod.startUpdate();
    } catch (e: any) {
      showing = false;
      note("update screen failed", { error: String(e?.message ?? e) });
    } finally {
      setGate({ opening: false });
    }
  };

  const openUpdateScreen = async () => {
    showing = true;
    stopIdleWatch();

    if (!(await readForcedUpdatesSwitch())) {
      // Switched off in the backoffice: carry on as normal and look again at
      // the next hourly check.
      note("forced updates switched off");
      available = false;
      showing = false;
      return;
    }
    // The answer can take a few seconds; a sale may have started meanwhile.
    if (getOtaStatus() !== "idle" || !isTillIdle(getPath())) {
      showing = false;
      startIdleWatch();
      return;
    }

    if (Platform.OS === "ios") {
      // The page goes up first and stays until the update is installed.
      setGate({ required: true, version: storeVersion });
      await presentIosSheet(false);
      return;
    }

    try {
      note("opening update screen");
      if (Platform.OS === "android") {
        const started = await mod.startUpdate(true);
        // Play allows the immediate flow for most updates; when it doesn't,
        // the store page always works.
        if (!started) {
          note("immediate flow refused, opening store page");
          openStorePage();
        }
      } else {
        await mod.startUpdate();
      }
      // Stay `showing` until the screen is closed (cancel event) or the app
      // comes back to the front.
    } catch (e: any) {
      note("update screen failed", { error: String(e?.message ?? e) });
      if (Platform.OS === "android") {
        showToast(t("store_update.failed"));
        openStorePage();
      }
      // iOS: also what happens when the iPad locked and woke with the App
      // Store sheet still open (the sheet can't be shown twice), so no toast
      // over it; just try again later.
      showing = false;
      holdUntil = Date.now() + RETRY_AFTER_FAILURE_MS;
      startIdleWatch();
    }
  };

  function startIdleWatch() {
    if (idleTimer) return;
    idleTimer = setInterval(() => {
      if (!available || showing || Date.now() < holdUntil) return;
      if (AppState.currentState !== "active") return;
      // An OTA restart is queued or running; let it go first.
      if (getOtaStatus() !== "idle" || !isTillIdle(getPath())) {
        idleSince = null;
        return;
      }
      if (idleSince === null) idleSince = Date.now();
      else if (Date.now() - idleSince >= IDLE_BEFORE_UPDATE_MS) void openUpdateScreen();
    }, IDLE_TICK_MS);
  }

  const stopCancelled = mod.addUpdateListener("updateCancelled", (e) => {
    note("update screen closed without updating", { reason: e?.reason ?? null });
    if (Platform.OS === "android") {
      // No "Later": closing Play's updater closes the app. Android moves it
      // to the background rather than ending it, so opening it again either
      // starts afresh or resumes here; both bring the updater straight back.
      closedByCancel = true;
      BackHandler.exitApp();
      return;
    }
    // iOS can't close itself. The "Update required" page stays up; its
    // Update now button opens the pop-up again.
    showing = false;
    if (!gate.required) startIdleWatch();
  });

  openFromPage = Platform.OS === "ios" ? () => void presentIosSheet(true) : null;
  const switchPoll =
    Platform.OS === "ios"
      ? setInterval(() => {
          if (!gate.required) return;
          void readForcedUpdatesSwitch().then((on) => {
            if (!on) unblock();
          });
        }, SWITCH_RECHECK_MS)
      : null;

  const poll = setInterval(() => {
    if (AppState.currentState === "active") void check();
  }, CHECK_EVERY_MS);

  const sub = AppState.addEventListener("change", (state) => {
    if (state !== "active") return;
    if (Platform.OS === "ios" && gate.required) {
      // The page is up and has its own button; only see whether the
      // backoffice has turned forced updates off in the meantime.
      void readForcedUpdatesSwitch().then((on) => {
        if (!on) unblock();
      });
      return;
    }
    if (showing) {
      // Reopened after closing the updater: straight back to it. Back from the
      // store page or a failed install with no cancel event: shortly.
      showing = false;
      holdUntil = closedByCancel ? 0 : Date.now() + RESUME_GRACE_MS;
      closedByCancel = false;
      if (available) startIdleWatch();
      return;
    }
    void check();
  });

  const first = setTimeout(() => void check(true), FIRST_CHECK_DELAY_MS);

  const stop = () => {
    clearTimeout(first);
    clearInterval(poll);
    if (switchPoll) clearInterval(switchPoll);
    openFromPage = null;
    stopIdleWatch();
    sub.remove();
    stopCancelled();
    if (stopRunning === stop) stopRunning = null;
  };
  stopRunning = stop;
  return stop;
}
