// Display names for payment methods.
//
// The *stored* strings are load-bearing and must never change. The backend
// decides card-ness — and therefore whether the customer covers the processing
// fee — by matching them literally:
//
//   gateways.py   CARD_METHOD_PREFIX = 'Credit Card'
//                 BEAM_CARD_METHOD   = 'Beam Card'
//   orders.py     is_card = payment_method.startswith(CARD_METHOD_PREFIX)
//
// Rename a stored value and the fee silently stops being charged, with nothing
// on screen to show it. Peak accounting and every historical order key off the
// same strings. So the wire value stays fixed and only the label moves.

const LABELS: Record<string, string> = {
  Cash: "Cash",
  // "Beam" is the provider; what the guest actually does is scan a Thai QR.
  Beam: "PromptPay QR",
  "Beam QR": "PromptPay QR",
  PromptPay: "PromptPay QR",
  // Two card rails that differ only by provider — so say which.
  "Credit Card": "Card · Omise",
  "Beam Card": "Card · Beam",
  "QR Kbank": "QR · KBank",
  "Easy Pay": "Easy Pay",
  EDC: "Card terminal",
  // Stored bare as "Custom" (plus a " · provider" suffix). Say "Custom Pay" so
  // the till, the receipt and the backoffice report column all read the same.
  Custom: "Custom Pay",
};

/**
 * Turn a stored payment_method into something a cashier can read.
 *
 * Stored values may carry a suffix the POS appended — "Custom · คนละครึ่ง",
 * "Credit · VISA ····1234" — so the head is translated and the rest kept.
 */
export function methodLabel(stored?: string | null): string {
  const raw = (stored || "").trim();
  if (!raw) return "—";
  const [head, ...rest] = raw.split(" · ");
  const base = LABELS[head] ?? head;
  return rest.length ? `${base} · ${rest.join(" · ")}` : base;
}

// Custom Pay's options are set per branch in the backoffice (Payment types).
// This is only for a till that has not reached the server yet this session —
// the same list every branch was seeded with, see DEFAULT_PAYMENT_TYPES.
export const FALLBACK_CUSTOM_TYPES = [
  "EDC Kbank", "EDC Bangkok", "Brave Brand Co.,Ltd",
  "Thai Dot Com Pay", "คนละครึ่ง", "EDC SCB", "QR",
];

/** The names in a /payment-types response; null if it isn't one. */
export function customTypeNames(body: any): string[] | null {
  if (!Array.isArray(body?.payment_types)) return null;
  return body.payment_types.map((t: any) => t?.name).filter(Boolean);
}
