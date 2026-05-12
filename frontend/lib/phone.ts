/**
 * Phone country list + per-country validation.
 *
 * Kept simple on purpose — no libphonenumber-js because the bundle is
 * heavy and our use case is "POS in Thailand with a handful of tourist
 * countries".  Each country defines the digit count it accepts and an
 * optional first-digit rule.  Numbers are stored in E.164 format
 * (+<country><digits>, no spaces).
 */

export type Country = {
  /** ISO-3166 alpha-2 — used as the picker key */
  code: string;
  /** Display name (English) */
  name: string;
  /** Emoji flag (works on Android + iOS + web without an icon font) */
  flag: string;
  /** International dial code WITHOUT the leading "+" */
  dial: string;
  /** Min digits expected AFTER the dial code */
  min: number;
  /** Max digits expected AFTER the dial code */
  max: number;
  /**
   * Regex applied to the digits-after-dial-code part.  Use it to catch
   * mobile-only rules like "Thai mobiles start with 6, 8 or 9".  Pass
   * /^$/ off if you don't want any additional rule.
   */
  pattern: RegExp;
};

/** Countries the picker offers.  Thailand first, then most-common tourist sources. */
export const COUNTRIES: Country[] = [
  { code: 'TH', name: 'Thailand',       flag: '🇹🇭', dial: '66', min: 9,  max: 9,  pattern: /^[689]\d{8}$/ },
  { code: 'US', name: 'United States',  flag: '🇺🇸', dial: '1',  min: 10, max: 10, pattern: /^\d{10}$/ },
  { code: 'GB', name: 'United Kingdom', flag: '🇬🇧', dial: '44', min: 10, max: 10, pattern: /^7\d{9}$/ },
  { code: 'IN', name: 'India',          flag: '🇮🇳', dial: '91', min: 10, max: 10, pattern: /^[6-9]\d{9}$/ },
  { code: 'SG', name: 'Singapore',      flag: '🇸🇬', dial: '65', min: 8,  max: 8,  pattern: /^[3689]\d{7}$/ },
  { code: 'MY', name: 'Malaysia',       flag: '🇲🇾', dial: '60', min: 9,  max: 10, pattern: /^1\d{8,9}$/ },
  { code: 'JP', name: 'Japan',          flag: '🇯🇵', dial: '81', min: 10, max: 10, pattern: /^[789]0\d{8}$/ },
  { code: 'KR', name: 'South Korea',    flag: '🇰🇷', dial: '82', min: 9,  max: 10, pattern: /^10\d{7,8}$/ },
  { code: 'CN', name: 'China',          flag: '🇨🇳', dial: '86', min: 11, max: 11, pattern: /^1[3-9]\d{9}$/ },
  { code: 'AU', name: 'Australia',      flag: '🇦🇺', dial: '61', min: 9,  max: 9,  pattern: /^4\d{8}$/ },
  { code: 'DE', name: 'Germany',        flag: '🇩🇪', dial: '49', min: 10, max: 11, pattern: /^1[5-7]\d{8,9}$/ },
  { code: 'FR', name: 'France',         flag: '🇫🇷', dial: '33', min: 9,  max: 9,  pattern: /^[67]\d{8}$/ },
];

export const COUNTRY_BY_CODE: Record<string, Country> = Object.fromEntries(
  COUNTRIES.map((c) => [c.code, c]),
);

export const DEFAULT_COUNTRY = COUNTRIES[0]; // Thailand

/** Strip everything that's not a digit. */
export function digitsOnly(s: string): string {
  return (s || '').replace(/\D+/g, '');
}

/**
 * Drop a leading country-code or trunk-prefix "0" so the user can paste
 * a Thai number like "0991234567" and have it accepted as "991234567".
 */
export function normalizeLocal(country: Country, raw: string): string {
  let d = digitsOnly(raw);
  // Leading "+" then country code → strip both
  if (d.startsWith(country.dial)) d = d.slice(country.dial.length);
  // Local trunk prefix "0" (Thailand, UK, etc.) → strip
  if (d.startsWith('0')) d = d.slice(1);
  return d;
}

export type Validation =
  | { valid: true;  e164: string;  display: string }
  | { valid: false; reason: string };

/** Validate + format. Empty input is treated as "no phone" — valid. */
export function validatePhone(country: Country, raw: string): Validation {
  const d = normalizeLocal(country, raw);
  if (!d) return { valid: true, e164: '', display: '' };
  if (d.length < country.min) {
    return { valid: false, reason: `Too short (need ${country.min}${country.max !== country.min ? `–${country.max}` : ''} digits)` };
  }
  if (d.length > country.max) {
    return { valid: false, reason: `Too long (max ${country.max} digits)` };
  }
  if (!country.pattern.test(d)) {
    return { valid: false, reason: `Invalid ${country.name} mobile number` };
  }
  return {
    valid: true,
    e164: `+${country.dial}${d}`,
    // Pretty-format with one space after the dial code and break the rest
    // in 3-character groups: "+66 991 234 567"
    display: `+${country.dial} ${d.replace(/(\d{3})(?=\d)/g, '$1 ').trim()}`,
  };
}

/**
 * Given a stored E.164 number, recover the country it belongs to (best
 * effort — longest matching dial-code prefix wins) and the local-digit
 * remainder.  Falls back to Thailand if no match.
 */
export function parseE164(stored: string): { country: Country; local: string } {
  const d = digitsOnly(stored);
  let best: Country | null = null;
  for (const c of COUNTRIES) {
    if (d.startsWith(c.dial) && (!best || c.dial.length > best.dial.length)) {
      best = c;
    }
  }
  if (!best) return { country: DEFAULT_COUNTRY, local: d };
  return { country: best, local: d.slice(best.dial.length) };
}
