import { router, Stack, usePathname } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { useFonts } from "expo-font";
import { useEffect, useRef, useState } from "react";
import { Text, TextInput } from "react-native";
import * as Sentry from "@sentry/react-native";
import { C } from "../lib/theme";
import { setUnauthorizedHandler } from "../lib/api";
import { DialogHost, showAlert, ToastHost } from "../lib/dialog";
import { initLanguage, t as tr } from "../lib/i18n";
import { throttleBeforeSend } from "../lib/sentryThrottle";
import { initLogRocket } from "../lib/logrocket";
import { endSession, startAppLock } from "../lib/appLock";
import { PrinterHost } from "../lib/printerHost";
import { startOtaAutoUpdate } from "../lib/otaAutoUpdate";
import { startStoreUpdateCheck } from "../lib/storeUpdate";
import { OtaStatus } from "../components/OtaStatus";

// Error + performance monitoring. Needs EXPO_PUBLIC_SENTRY_DSN (set in
// .env.production, so it only lands in release bundles) *and* a non-dev build.
// The __DEV__ gate is the one that matters: Metro-served bundles throw
// constantly while you're editing, and none of that belongs in Sentry — so a
// DSN sitting in a local .env can't turn the firehose on by accident.
// tracesSampleRate drives the performance side; dial down once we have real
// traffic.
Sentry.init({
  dsn: process.env.EXPO_PUBLIC_SENTRY_DSN,
  enabled: !__DEV__,
  // EAS sets this per build profile (preview → "preview"). Falling back to
  // "development" rather than "production" means an untagged build can never
  // masquerade as the real thing in the issue list.
  environment: process.env.EXPO_PUBLIC_SENTRY_ENV ?? (__DEV__ ? "development" : "production"),
  tracesSampleRate: 1.0,
  // Auto-instrument navigation + touch events for performance traces.
  integrations: [Sentry.reactNativeTracingIntegration()],
  // The POS polls, so one backend outage used to arrive as hundreds of copies
  // of the same event (279 for the 2026-08-11 one).  Collapse repeats onto a
  // log scale — see lib/sentryThrottle.
  beforeSend: throttleBeforeSend,
});

// Session replay beside Sentry's error reports. See lib/logrocket.
initLogRocket();

// Set Sarabun as the default font for all Text and TextInput components
// so Thai characters render correctly on web and native.
(Text as any).defaultProps = (Text as any).defaultProps || {};
(Text as any).defaultProps.style = { fontFamily: "Sarabun" };
(TextInput as any).defaultProps = (TextInput as any).defaultProps || {};
(TextInput as any).defaultProps.style = { fontFamily: "Sarabun" };

// Android can rebuild the activity while the till is away, so the AppState
// "active" event may land before the navigator has rendered again — and
// expo-router throws rather than queue the navigation.  Uncaught inside the
// AppState listener, that was a fatal crash on resume (REACT-NATIVE-C, test
// branch, 1.5.1).  Retry until the navigator is back.  Both ways back to the
// PIN pad below — the 401 handler and the app lock — fire from outside render.
function toPinPad(tries = 0): void {
  try {
    router.replace("/");
  } catch {
    if (tries < 20) setTimeout(() => toPinPad(tries + 1), 250);
  }
}

function RootLayout() {
  // Resolve the saved (or device) language before the first paint. It is one
  // AsyncStorage read; rendering ahead of it would flash English at a Thai
  // shop on every cold start, and the flash is worse than a few idle ms.
  const [langReady, setLangReady] = useState(false);
  useEffect(() => {
    initLanguage().finally(() => setLangReady(true));
  }, []);

  // Resetting a till PIN in the backoffice ends that staff member's session,
  // so a cashier stranded by a dead tablet can sign in on a working one.  The
  // tablet finds out when its next call comes back 401 — api.ts has dropped
  // the token by then, so what is left is to take whoever is holding it back
  // to the PIN pad, and say why.  Without this they carry on at a screen
  // where nothing saves and discover it with a customer at the counter.
  //
  // Registered here because the root layout outlives every screen: a handler
  // owned by /pos would go with it the moment it unmounted.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      toPinPad();
      showAlert(tr("login.signed_out_title"), tr("login.signed_out_body"));
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  // Leaving the app — another app, screen off — signs the till out, so the
  // next person to pick it up starts at the PIN pad. See lib/appLock.
  // Navigate first: the logout call can hang on wifi that is still waking up,
  // and the screen must not sit on the sale while it does.
  const pathname = usePathname();
  const pathRef = useRef(pathname);
  useEffect(() => { pathRef.current = pathname; }, [pathname]);
  useEffect(
    () =>
      startAppLock(() => {
        if (pathRef.current === "/") return;
        toPinPad();
        endSession();
      }),
    [],
  );

  // Over-the-air updates install themselves the first moment the till is
  // idle, and keep whoever is signed in signed in. See lib/otaAutoUpdate.
  useEffect(() => startOtaAutoUpdate(() => pathRef.current), []);

  // A newer store build (native changes OTA can't carry): open the store's
  // update screen once the till is free; no "Later". See lib/storeUpdate.
  useEffect(() => startStoreUpdateCheck(() => pathRef.current), []);

  const [fontsLoaded] = useFonts({
    Sarabun: require("../assets/fonts/Sarabun-Regular.ttf"),
    "Sarabun-Bold": require("../assets/fonts/Sarabun-Bold.ttf"),
    // Figures only — see MONO in lib/theme. Money set in Sarabun re-flows as
    // digits change, which makes a column of prices visibly ragged.
    SpaceMono: require("../assets/fonts/SpaceMono-Regular.ttf"),
  });

  // Render layout even if fonts haven't loaded yet — Text falls back to system font
  if (!langReady) return null;

  return (
    <SafeAreaProvider>
      <StatusBar style="dark" />
      {/* The receipt printer, with its hidden slip view beside the stack so
          it stays on the display whichever screen is open.  See printerHost. */}
      <PrinterHost>
        <Stack
          screenOptions={{
            headerShown: false,
            animation: "fade",
            contentStyle: { backgroundColor: C.bg },
          }}
        />
        {/* Confirmations and error messages. Mounted once, above the stack, so
            a dialog raised from any screen renders over it. */}
        <DialogHost />
        <ToastHost />
        {/* "Downloading update" / "Updating…" — see lib/otaAutoUpdate. */}
        <OtaStatus />
      </PrinterHost>
    </SafeAreaProvider>
  );
}

// Sentry.wrap enables automatic error-boundary + touch/navigation tracking.
export default Sentry.wrap(RootLayout);
