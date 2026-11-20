/**
 * Offer a new store build (Google Play / App Store) from inside the app.
 *
 * OTA updates (lib/otaAutoUpdate) only replace the JavaScript. Anything native
 * — an Expo SDK upgrade, a new native module — needs a new build from the
 * store, and without this a till only got one when Play happened to
 * auto-update it. This checks the store now and then and, when a newer build
 * is out, asks the cashier once the till is free.
 *
 *  - Android: Google Play's in-app update, *immediate* flow only. Play shows
 *    its own full-screen updater and reopens the app when it is done. The
 *    library's flexible flow is deliberately not used: it restarts the app the
 *    moment the download finishes, which could land in the middle of a sale.
 *    Only works for an install that came from Play; a sideloaded APK simply
 *    never sees an update.
 *  - iOS: Apple has no in-app update, so this compares the App Store version
 *    and opens the App Store sheet. Nothing happens until the app is live there.
 *
 * The prompt appears only when the till is idle (isTillIdle — the same rule the
 * OTA restart uses) and never while an OTA is downloading or installing.
 * "Later" holds it off for a few hours.
 *
 * No-op in dev, on web, and in any build without the native module, so it is
 * safe to ship by OTA to a build that predates it.
 */
import { AppState, Linking, Platform } from "react-native";
import { requireOptionalNativeModule } from "expo";
import Constants from "expo-constants";
import * as Sentry from "@sentry/react-native";
import { showAlert, showToast } from "./dialog";
import { t } from "./i18n";
import { getOtaStatus, isTillIdle } from "./otaAutoUpdate";

type InAppUpdates = typeof import("expo-in-app-updates");

const FIRST_CHECK_DELAY_MS = 60_000;
const CHECK_EVERY_MS = 6 * 60 * 60_000;
// Foregrounding a till is frequent; don't ask the store every time.
const MIN_CHECK_GAP_MS = 60 * 60_000;
const SNOOZE_MS = 4 * 60 * 60_000;
const PROMPT_TICK_MS = 5_000;
// Same idea as the OTA restart: a cashier tapping straight after a sale
// should not get a popup under their finger.
const IDLE_BEFORE_PROMPT_MS = 3_000;

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

/** Start checking the store for a newer build. Returns a cleanup function. */
export function startStoreUpdateCheck(getPath: () => string): () => void {
  const mod = loadModule();
  if (!mod) return () => {};

  let available = false;
  let prompting = false;
  let checking = false;
  let lastCheckAt = 0;
  let snoozedUntil = 0;
  let idleSince: number | null = null;

  const check = async () => {
    if (checking || available) return;
    if (Date.now() - lastCheckAt < MIN_CHECK_GAP_MS) return;
    checking = true;
    lastCheckAt = Date.now();
    try {
      const info = await mod.checkForUpdate();
      // Android: an immediate update that was interrupted must be resumed,
      // and Play reports it as "in progress" rather than available.
      if (info.updateAvailable) {
        available = true;
        note("store update available", {
          storeVersion: info.storeVersion ?? null,
          inProgress: info.updateInProgress ?? false,
        });
      }
    } catch (e: any) {
      // Offline, or not installed from the store: normal, never alert.
      note("check failed", { error: String(e?.message ?? e) });
    } finally {
      checking = false;
    }
  };

  const update = async () => {
    try {
      if (Platform.OS === "android") {
        const started = await mod.startUpdate(true);
        // Play only allows the immediate flow for some updates; the store
        // page always works.
        if (!started) openStorePage();
      } else {
        await mod.startUpdate();
      }
      note("update flow started");
    } catch (e: any) {
      note("update flow failed", { error: String(e?.message ?? e) });
      showToast(t("store_update.failed"));
      if (Platform.OS === "android") openStorePage();
    } finally {
      // If the flow was cancelled or failed, don't ask again straight away.
      snoozedUntil = Date.now() + SNOOZE_MS;
      prompting = false;
    }
  };

  const prompt = () => {
    prompting = true;
    note("prompting");
    showAlert(
      t("store_update.title"),
      Platform.OS === "ios" ? t("store_update.message_ios") : t("store_update.message_android"),
      [
        {
          text: t("store_update.later"),
          style: "cancel",
          onPress: () => {
            snoozedUntil = Date.now() + SNOOZE_MS;
            prompting = false;
            note("snoozed");
          },
        },
        { text: t("store_update.update"), onPress: () => void update() },
      ],
    );
  };

  const tick = setInterval(() => {
    if (!available || prompting || Date.now() < snoozedUntil) return;
    // An OTA restart is already queued or running; let it go first.
    if (getOtaStatus() !== "idle" || !isTillIdle(getPath())) {
      idleSince = null;
      return;
    }
    if (idleSince === null) idleSince = Date.now();
    else if (Date.now() - idleSince >= IDLE_BEFORE_PROMPT_MS) {
      idleSince = null;
      prompt();
    }
  }, PROMPT_TICK_MS);

  const poll = setInterval(() => {
    if (AppState.currentState === "active") void check();
  }, CHECK_EVERY_MS);

  const sub = AppState.addEventListener("change", (state) => {
    if (state === "active") void check();
  });

  const first = setTimeout(() => void check(), FIRST_CHECK_DELAY_MS);

  return () => {
    clearTimeout(first);
    clearInterval(tick);
    clearInterval(poll);
    sub.remove();
  };
}
