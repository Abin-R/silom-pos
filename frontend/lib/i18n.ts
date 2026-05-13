// Minimal i18n: AsyncStorage-backed language preference + a `t()` lookup that
// falls back to English when a Thai translation hasn't been added yet.  No
// external dep — translations.ts is just two flat objects.
import { useEffect, useState, useCallback } from "react";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { translations, type LangCode } from "./translations";

const LANG_KEY = "bravepos:lang:v1";
const DEFAULT_LANG: LangCode = "en";

let cachedLang: LangCode = DEFAULT_LANG;
const listeners = new Set<(lang: LangCode) => void>();

export async function initLanguage(): Promise<LangCode> {
  try {
    const saved = await AsyncStorage.getItem(LANG_KEY);
    if (saved === "en" || saved === "th") {
      cachedLang = saved;
    }
  } catch {}
  return cachedLang;
}

export async function setLanguage(lang: LangCode): Promise<void> {
  cachedLang = lang;
  try { await AsyncStorage.setItem(LANG_KEY, lang); } catch {}
  // Notify every mounted component so they re-render with the new strings
  // immediately — no need to reload the app.
  listeners.forEach((fn) => fn(lang));
}

export function getLanguage(): LangCode {
  return cachedLang;
}

/** Returns the translation for `key` in the current language.  When a key is
 *  missing from `th`, falls back to `en`; when missing from both, returns the
 *  key itself so the gap is visible during development. */
export function t(key: string): string {
  const dict = translations[cachedLang] || translations.en;
  return dict[key] ?? translations.en[key] ?? key;
}

/** Hook variant of `t()` — components that use this re-render automatically
 *  when the user changes language from Settings. */
export function useT(): { t: (key: string) => string; lang: LangCode } {
  const [lang, setLang] = useState<LangCode>(cachedLang);
  useEffect(() => {
    const listener = (next: LangCode) => setLang(next);
    listeners.add(listener);
    return () => { listeners.delete(listener); };
  }, []);
  const tr = useCallback((key: string) => {
    const dict = translations[lang] || translations.en;
    return dict[key] ?? translations.en[key] ?? key;
  }, [lang]);
  return { t: tr, lang };
}
