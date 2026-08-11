/**
 * App-wide internationalization.
 *
 * Stack: `i18n-js` for lookup/interpolation/pluralization, `expo-localization`
 * for the device's own language. Dictionaries live in ./locales.
 *
 * Three rules that shaped this file:
 *
 *  1. **English is canonical.** Every key exists in `en`; `th` is allowed to be
 *     incomplete. `enableFallback` means a missing Thai string renders the
 *     English one rather than a `[missing …]` marker — a half-translated screen
 *     is usable, a screen full of bracket-noise is not.
 *
 *  2. **The picker in Settings wins.** Device locale only seeds the *first*
 *     launch. Once a staff member chooses a language it is stored per device
 *     and never silently overridden — a shop that sets Thai keeps Thai even if
 *     someone changes the tablet's system language.
 *
 *  3. **expo-localization must never be load-bearing.** It is a native module,
 *     so a build made before it was added has no implementation to call. Every
 *     use is wrapped: if it throws we fall back to English and the Settings
 *     picker still works, because that path is pure JS + AsyncStorage.
 */
import { useCallback, useEffect, useState } from "react";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { I18n } from "i18n-js";
import en from "./locales/en";
import th from "./locales/th";

export type LangCode = "en" | "th";

export const LANGUAGES: { code: LangCode; label: string; native: string }[] = [
  { code: "en", label: "English", native: "English" },
  { code: "th", label: "Thai", native: "ไทย" },
];

const LANG_KEY = "bravepos:lang:v1";
const DEFAULT_LANG: LangCode = "en";

export const i18n = new I18n({ en, th });
i18n.defaultLocale = DEFAULT_LANG;
i18n.enableFallback = true;
i18n.locale = DEFAULT_LANG;

// i18n-js renders `[missing "th.foo.bar" translation]` for an unknown key.
// On a POS that string would land on a cashier's screen mid-sale, so a key
// with no entry degrades to readable text instead: the last segment,
// de-slugged. A plain string that isn't a key at all (no dot) comes back
// unchanged, which keeps `t()` safe to apply to text that was never keyed.
i18n.missingTranslation.register("readable", (_i18n, scope) => {
  const key = String(scope);
  if (!key.includes(".")) return key;
  const leaf = key.slice(key.lastIndexOf(".") + 1);
  return leaf.replace(/_/g, " ");
});
i18n.missingBehavior = "readable";

// Thai has a single plural category ("other") in CLDR — there is no separate
// singular form. Without this, i18n-js applies the default English one/other
// rule, looks up `th.<key>.one` for a count of 1, finds nothing and renders
// the raw scope ("pos,items_pcs,one") on screen. Registering the real rule
// means Thai entries only ever need `other`.
i18n.pluralization.register("th", () => ["other"]);

let cachedLang: LangCode = DEFAULT_LANG;
const listeners = new Set<(lang: LangCode) => void>();

function isLang(v: unknown): v is LangCode {
  return v === "en" || v === "th";
}

/** The tablet's own language, or null when the native module is unavailable. */
function deviceLang(): LangCode | null {
  try {
    // Required lazily so a missing native module surfaces here, inside the
    // try, instead of as an import-time crash that takes the whole app down.
    const Localization = require("expo-localization");
    const tag: string | undefined =
      Localization.getLocales?.()[0]?.languageCode ??
      Localization.getLocales?.()[0]?.languageTag;
    if (typeof tag === "string" && tag.toLowerCase().startsWith("th")) return "th";
    if (typeof tag === "string" && tag.toLowerCase().startsWith("en")) return "en";
  } catch {}
  return null;
}

/**
 * Resolve the startup language: saved choice → device language → English.
 * Call once, before the first render (see app/_layout.tsx).
 */
export async function initLanguage(): Promise<LangCode> {
  let next: LangCode | null = null;
  try {
    const saved = await AsyncStorage.getItem(LANG_KEY);
    if (isLang(saved)) next = saved;
  } catch {}
  // No stored preference yet — take the tablet's language as the opening bid so
  // a Thai-locale device is in Thai before anyone visits Settings.
  if (!next) next = deviceLang();
  cachedLang = next ?? DEFAULT_LANG;
  i18n.locale = cachedLang;
  return cachedLang;
}

export async function setLanguage(lang: LangCode): Promise<void> {
  cachedLang = lang;
  i18n.locale = lang;
  try {
    await AsyncStorage.setItem(LANG_KEY, lang);
  } catch {}
  // Notify every mounted component so they re-render with the new strings
  // immediately — no need to reload the app.
  listeners.forEach((fn) => fn(lang));
}

export function getLanguage(): LangCode {
  return cachedLang;
}

export type TOptions = Record<string, any>;

/**
 * Translate `key`, interpolating any `{{placeholders}}` from `options`.
 *
 * Prefer `useT()` inside components — this plain form does not re-render on a
 * language change, so it is for callbacks, handlers and module-level helpers
 * that read the string at call time.
 */
export function t(key: string, options?: TOptions): string {
  return i18n.t(key, options);
}

/**
 * Hook form of `t()`. Components using this re-render automatically when the
 * language changes in Settings.
 */
export function useT(): {
  t: (key: string, options?: TOptions) => string;
  lang: LangCode;
  setLang: (lang: LangCode) => Promise<void>;
} {
  const [lang, setLangState] = useState<LangCode>(cachedLang);
  useEffect(() => {
    const listener = (next: LangCode) => setLangState(next);
    listeners.add(listener);
    // A language change between first render and this effect running would
    // otherwise be missed, leaving this component on the old strings.
    setLangState(cachedLang);
    return () => {
      listeners.delete(listener);
    };
  }, []);
  const tr = useCallback(
    (key: string, options?: TOptions) => {
      // `lang` is unused inside but is the dependency that rebuilds this
      // callback on a language change, which is what re-renders consumers.
      void lang;
      return i18n.t(key, options);
    },
    [lang],
  );
  return { t: tr, lang, setLang: setLanguage };
}

// ── Locale-aware formatting ────────────────────────────────────────────────
// Thailand runs on the Buddhist Era (year + 543) and the shop's printed
// paperwork already uses it, so date output is locale-dependent in a way that
// plain string translation cannot express.

export const BE_OFFSET = 543;

const pad = (n: number) => String(n).padStart(2, "0");

/** Year in the calendar the current language expects (BE for Thai, CE for English). */
export function displayYear(d: Date, lang: LangCode = cachedLang): number {
  return lang === "th" ? d.getFullYear() + BE_OFFSET : d.getFullYear();
}

/** "12/08/2569" (th) · "12/08/2026" (en) */
export function formatDate(d: Date, lang: LangCode = cachedLang): string {
  return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${displayYear(d, lang)}`;
}

/** "วันอังคารที่ 11 สิงหาคม 2569" (th) · "Tuesday 11 August" (en) */
export function formatLongDate(d: Date, lang: LangCode = cachedLang): string {
  const day = t(`date.days.${d.getDay()}`);
  const month = t(`date.months.${d.getMonth()}`);
  return lang === "th"
    ? `${day}ที่ ${d.getDate()} ${month} ${displayYear(d, lang)}`
    : `${day} ${d.getDate()} ${month}`;
}

/** "11 Aug 2569" — compact, for list rows and range headers. */
export function formatShortDate(d: Date, lang: LangCode = cachedLang): string {
  return `${d.getDate()} ${t(`date.monthsShort.${d.getMonth()}`)} ${displayYear(d, lang)}`;
}

/** 24-hour "15:18" — the shop reads 24h in both languages. */
export function formatClock(d: Date): string {
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** "15:18:47" */
export function formatTime(d: Date): string {
  return `${formatClock(d)}:${pad(d.getSeconds())}`;
}

/**
 * Money. Thai and English both use Arabic digits and the same grouping here —
 * Thai digits (๑๒๓) are not used on receipts or in POS — so this is
 * language-independent by design, and lives here only to keep every amount in
 * the app going through one function.
 */
export function formatMoney(amount: number, decimals = 2): string {
  const n = Number.isFinite(amount) ? amount : 0;
  return n.toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}
