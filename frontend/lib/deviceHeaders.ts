/**
 * Who this device is and what it runs, sent with every API call.
 *
 * The server records it per tablet (backend_django/bravepos/till_devices.py)
 * for the backoffice Devices page: which tablet, at which branch, on which app
 * version and OTA. It is the same information the nav rail and
 * Settings → About show, nothing more.
 *
 * JS only, on purpose: Platform.constants, expo-constants and expo-updates are
 * in every build, so this can reach the tills as an OTA. The native build
 * number isn't available without another native module, and version + OTA +
 * channel already tell the builds apart.
 *
 * Skipped on web: custom headers would turn every browser call into a CORS
 * preflight, and the web build isn't a till.
 */
import { Platform } from "react-native";
import Constants from "expo-constants";
import * as Updates from "expo-updates";

import { getDeviceId } from "./deviceId";

// Header values must be plain printable ASCII; anything else could make
// fetch() refuse the request, and a header must never cost a till a sale.
function ascii(v: unknown, max = 64): string {
  return String(v ?? "")
    .replace(/[^\x20-\x7E]/g, "")
    .trim()
    .slice(0, max);
}

function build(deviceId: string): Record<string, string> {
  const c = (Platform.constants ?? {}) as Record<string, unknown>;
  const android = Platform.OS === "android";
  const h: Record<string, string> = {
    "X-Device-Id": ascii(deviceId),
    "X-Device-Platform": ascii(Platform.OS),
    "X-Device-Manufacturer": ascii(android ? c.Manufacturer : "Apple"),
    "X-Device-Model": ascii(android ? c.Model : c.interfaceIdiom === "pad" ? "iPad" : "iPhone"),
    "X-Device-OS": ascii(android ? c.Release : c.osVersion, 32),
    "X-App-Version": ascii(Constants.expoConfig?.version, 32),
    "X-App-Runtime": ascii(Updates.runtimeVersion, 32),
    "X-App-Channel": ascii(Updates.channel, 32),
    "X-App-Update-Id": ascii(Updates.isEmbeddedLaunch ? "" : Updates.updateId),
    "X-App-Update-Published": Updates.isEmbeddedLaunch || !Updates.createdAt
      ? ""
      : ascii(Updates.createdAt.toISOString()),
    "X-App-Embedded": Updates.isEmbeddedLaunch ? "1" : "0",
  };
  // Drop empties rather than send blank headers.
  for (const k of Object.keys(h)) if (!h[k]) delete h[k];
  return h;
}

let cached: Promise<Record<string, string>> | null = null;

/** The headers, built once per app launch (an OTA reloads the JS, so a new
 *  update id is picked up the moment it's applied). Never rejects. */
export function getDeviceHeaders(): Promise<Record<string, string>> {
  if (Platform.OS === "web") return Promise.resolve({});
  if (!cached) {
    cached = getDeviceId()
      .then(build)
      .catch(() => {
        cached = null; // try again on the next call
        return {};
      });
  }
  return cached;
}
