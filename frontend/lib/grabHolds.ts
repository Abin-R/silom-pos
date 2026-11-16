// The till's gate on units BravePOS is holding for Grab orders.
//
// Before the payment screen opens, the cart goes to POST /grab/till-check.  If
// any product has fewer units left to sell than the cart wants, the sale is
// blocked — for every payment method, with no override — so no money is taken
// for a unit that is already promised to a Grab rider.  A check that cannot be
// answered blocks too: a sale never passes unchecked.

export type TillCheckLine = { product_id: string; qty: number };

export type TillCheckBlocked = {
  product_id: string;
  name: string;
  wanted: number;
  can_sell: number;
  held_for_grab: number;
};

export type TillCheckResult =
  | { status: "ok" }
  | { status: "blocked"; blocked: TillCheckBlocked[] }
  | { status: "unavailable" };

type CartLine = {
  product_id: string;
  qty: number;
  free_of?: string;
  real_product_id?: string;
};

/**
 * The till-check body, built from the cart the way the order save builds it:
 * paid lines under their own id, a promotion's free items under the real
 * product (the free line itself is keyed by a made-up id).
 */
export function buildTillCheckBody(cart: CartLine[]): {
  items: TillCheckLine[];
  free_items: TillCheckLine[];
} {
  return {
    items: cart
      .filter((i) => !i.free_of)
      .map((i) => ({ product_id: i.product_id, qty: i.qty })),
    free_items: cart
      .filter((i) => !!i.free_of)
      .map((i) => ({ product_id: i.real_product_id as string, qty: i.qty })),
  };
}

/**
 * Turn a till-check response into a decision.  Anything that is not a clear
 * 200 {"ok": true} blocks: an error status, an unreadable body, or a missing
 * `ok` all read as "unavailable" rather than letting the sale through.
 */
export function readTillCheck(status: number, body: any): TillCheckResult {
  if (status !== 200 || !body || typeof body.ok !== "boolean") {
    return { status: "unavailable" };
  }
  if (body.ok) return { status: "ok" };
  const blocked: TillCheckBlocked[] = Array.isArray(body.blocked) ? body.blocked : [];
  // `ok: false` with nothing to name still blocks; the caller shows the
  // "can't check" message since there is no product to point at.
  return blocked.length ? { status: "blocked", blocked } : { status: "unavailable" };
}

/**
 * Ask the backend whether the cart can be sold.  Retries once on a network
 * error or a bad answer; if the second try fails too the result is
 * "unavailable".  `send` is the request itself (apiFetch in the app).
 */
export async function checkTillStock(
  send: (body: ReturnType<typeof buildTillCheckBody>) => Promise<{
    status: number;
    json: () => Promise<any>;
  }>,
  cart: CartLine[],
): Promise<TillCheckResult> {
  const body = buildTillCheckBody(cart);
  let result: TillCheckResult = { status: "unavailable" };
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const res = await send(body);
      const json = await res.json().catch(() => null);
      result = readTillCheck(res.status, json);
    } catch {
      result = { status: "unavailable" };
    }
    if (result.status !== "unavailable") return result;
  }
  return result;
}
