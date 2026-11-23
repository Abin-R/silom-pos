// Session replay (LogRocket): what the cashier saw and tapped, for the bugs a
// stack trace can't explain — "I pressed Pay and nothing happened".  Sentry
// stays the place errors are reported; this is the recording beside it.
//
// Records on every branch.  What it must never carry:
//   * the PIN — the keypad is redacted on the login screen, and touches on
//     redacted views are dropped (captureRedactedViewTouches), because the
//     tap positions on a keypad *are* the PIN;
//   * customer details — the phone field (components/PhoneInput), every
//     field of the tax-invoice / new-member forms (TiField in app/admin) and
//     the card's last 4 digits (app/pos) sit inside a `redactProps` view.
//     Wrappers, not the inputs' own testIDs: on iOS LogRocket only honours
//     a testID on a view that isn't accessible, and an input always is;
//   * request and response bodies, query strings and the bearer token —
//     customers, orders, payments and login all carry personal data, and a
//     list of "sensitive routes" would miss the next one someone adds.  The
//     network log keeps method, path, status and timing, which is what a
//     "save failed" replay needs.
//
// The SDK is native.  A till on a build without it (anything before 1.5.1)
// gets init → console.warn → no-op, and web gets a no-op too, so this file is
// safe to reach through an OTA.  Loaded with require() inside a try so even an
// unexpected throw at import can't take the root layout down with it.
import { Platform } from "react-native";
import * as Updates from "expo-updates";

const APP_ID = "nhjsae/the-rolling-pinn";

type LogRocketSDK = typeof import("@logrocket/react-native").default;

let LR: LogRocketSDK | null = null;
/** Spread onto a View to black it out in the replay (and drop its touches). */
export let redactProps: object = {};

if (Platform.OS !== "web") {
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const mod = require("@logrocket/react-native");
    LR = mod.default ?? mod;
    redactProps = mod.LRRedactProps();
  } catch {
    LR = null;
  }
}

/** Path only: no host, no query string (searches carry phone numbers). */
function pathOf(url: string | null | undefined): string | null {
  if (!url) return url ?? null;
  return url.replace(/^[a-z]+:\/\/[^/]+/i, "").split("?")[0];
}

function withoutAuth(headers: { [k: string]: string | null | undefined }) {
  const out: { [k: string]: string | null | undefined } = {};
  for (const [k, v] of Object.entries(headers || {})) {
    if (k.toLowerCase() !== "authorization" && k.toLowerCase() !== "cookie") out[k] = v;
  }
  return out;
}

export function initLogRocket(): void {
  if (!LR || __DEV__) return;
  try {
    LR.init(APP_ID, {
      captureRedactedViewTouches: false,
      // Which OTA the till was running, so a replay lines up with a release.
      updateId: Updates.updateId ?? null,
      expoChannel: Updates.channel ?? null,
      network: {
        requestSanitizer: (req) => ({
          ...req,
          url: pathOf(req.url),
          headers: withoutAuth(req.headers),
          body: null,
        }),
        responseSanitizer: (res) => ({
          ...res,
          url: pathOf(res.url),
          headers: withoutAuth(res.headers),
          body: null,
        }),
      },
    });
  } catch {
    // Monitoring must never be the reason the till doesn't open.
  }
}

/** Name the session after whoever signed in, and where. */
export function identifyLogRocket(ctx: {
  staffId?: string | number;
  staffName?: string;
  role?: string;
  branchName?: string;
}): void {
  if (!LR || __DEV__) return;
  try {
    const traits: { [k: string]: string } = {
      role: ctx.role ?? "unknown",
      branch: ctx.branchName ?? "unknown",
    };
    if (ctx.staffName) traits.name = ctx.staffName;
    LR.identify(
      ctx.staffId != null ? String(ctx.staffId) : `${ctx.branchName ?? "unknown"}:${ctx.staffName ?? "staff"}`,
      traits,
    );
  } catch {
    // ignore — see initLogRocket
  }
}
