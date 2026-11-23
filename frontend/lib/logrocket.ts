// Session replay (LogRocket): what the cashier saw and tapped, for the bugs a
// stack trace can't explain — "I pressed Pay and nothing happened".  Sentry
// stays the place errors are reported; this is the recording beside it.
//
// Records on every branch.  What it must never carry:
//   * the PIN — the keypad is redacted on the login screen, and touches on
//     redacted views are dropped (captureRedactedViewTouches), because the
//     tap positions on a keypad *are* the PIN;
//   * the card's last 4 digits — the input (app/pos) sits inside a
//     `redactProps` view;
//   * the session token and cookies — stripped from the headers.
//
// Customer details (phone, tax-invoice and member forms) are recorded on
// purpose, and so is the network log in full: URL with host and query
// string, request and response bodies.  Inside the bodies the same three
// secrets are scrubbed — the PIN travels in the pin-login body, the token
// comes back in its response, and the card digits ride along in the order's
// payment_method ("Credit · VISA ····1234").

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

/** Body keys whose values are secrets: pin, new_pin, token, access_token… */
const SECRET_KEY = /(^|_)(pin|token|password|secret)($|_)/i;
/** The card's last 4, as the till writes them into payment_method. */
const CARD_DIGITS = /····\s*\d{4}/g;

function scrubValue(v: unknown): unknown {
  if (typeof v === "string") return v.replace(CARD_DIGITS, "····####");
  if (Array.isArray(v)) return v.map(scrubValue);
  if (v && typeof v === "object") {
    const out: { [k: string]: unknown } = {};
    for (const [k, val] of Object.entries(v)) {
      out[k] = SECRET_KEY.test(k) ? "[redacted]" : scrubValue(val);
    }
    return out;
  }
  return v;
}

/** Keep the body, minus the PIN, the token and the card digits. */
function scrubBody(body: string | null | undefined): string | null | undefined {
  if (!body) return body;
  // Most bodies (the catalogue, order lists) hold none of it: skip the
  // parse/stringify round trip on the till's JS thread.
  if (!/pin|token|password|secret|····/i.test(body)) return body;
  try {
    return JSON.stringify(scrubValue(JSON.parse(body)));
  } catch {
    // Not JSON (an image, a form): only the card-digit pattern can be found.
    return body.replace(CARD_DIGITS, "····####");
  }
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
          headers: withoutAuth(req.headers),
          body: scrubBody(req.body),
        }),
        responseSanitizer: (res) => ({
          ...res,
          headers: withoutAuth(res.headers),
          body: scrubBody(res.body),
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
