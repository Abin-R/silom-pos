/**
 * A random id this tablet keeps for as long as the app is installed.
 *
 * Sent with the PIN login so the server can tell "the same tablet signing
 * back in" from "a second tablet". An account may only be on one tablet at a
 * time, but a till whose logout never reached the server (lib/appLock) must
 * still be able to sign its own staff back in. Not a secret — just a label.
 */
import AsyncStorage from "@react-native-async-storage/async-storage";

const KEY = "bravepos:device-id:v1";
let cached: string | null = null;

function randomId(): string {
  let s = "";
  for (let i = 0; i < 32; i++) s += Math.floor(Math.random() * 16).toString(16);
  return s;
}

export async function getDeviceId(): Promise<string> {
  if (cached) return cached;
  try {
    const saved = await AsyncStorage.getItem(KEY);
    if (saved) return (cached = saved);
  } catch {}
  cached = randomId();
  try { await AsyncStorage.setItem(KEY, cached); } catch {}
  return cached;
}
