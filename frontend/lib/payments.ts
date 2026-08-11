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
