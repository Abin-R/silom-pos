import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { useFonts } from "expo-font";
import { useEffect, useState } from "react";
import { Text, TextInput } from "react-native";
import * as Sentry from "@sentry/react-native";
import { C } from "../lib/theme";
import { DialogHost } from "../lib/dialog";
import { initLanguage } from "../lib/i18n";
import { throttleBeforeSend } from "../lib/sentryThrottle";

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

// Set Sarabun as the default font for all Text and TextInput components
// so Thai characters render correctly on web and native.
(Text as any).defaultProps = (Text as any).defaultProps || {};
(Text as any).defaultProps.style = { fontFamily: "Sarabun" };
(TextInput as any).defaultProps = (TextInput as any).defaultProps || {};
(TextInput as any).defaultProps.style = { fontFamily: "Sarabun" };

function RootLayout() {
  // Resolve the saved (or device) language before the first paint. It is one
  // AsyncStorage read; rendering ahead of it would flash English at a Thai
  // shop on every cold start, and the flash is worse than a few idle ms.
  const [langReady, setLangReady] = useState(false);
  useEffect(() => {
    initLanguage().finally(() => setLangReady(true));
  }, []);

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
    </SafeAreaProvider>
  );
}

// Sentry.wrap enables automatic error-boundary + touch/navigation tracking.
export default Sentry.wrap(RootLayout);
