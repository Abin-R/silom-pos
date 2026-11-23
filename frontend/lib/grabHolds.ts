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

/** How long one try may take before it is given up on and counts as failed. */
export const TILL_CHECK_TIMEOUT_MS = 5000;

type TillCheckSend = (
  body: ReturnType<typeof buildTillCheckBody>,
  signal?: AbortSignal,
) => Promise<{
  status: number;
  json: () => Promise<any>;
}>;

/**
 * One try, given `timeoutMs`.  The request is aborted when the time runs out
 * (so it does not keep a connection busy), and the wait itself is raced against
 * the clock too, so a `send` that ignores the signal still cannot hang the till.
 */
async function tryOnce(
  send: TillCheckSend,
  body: ReturnType<typeof buildTillCheckBody>,
  timeoutMs: number,
): Promise<TillCheckResult> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const clock = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new Error("till-check timed out"));
    }, timeoutMs);
  });
  try {
    const answer = (async () => {
      const res = await send(body, controller.signal);
      const json = await res.json().catch(() => null);
      return readTillCheck(res.status, json);
    })();
    answer.catch(() => {}); // a late failure after the clock won is not unhandled
    return await Promise.race([answer, clock]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

/**
 * Ask the backend whether the cart can be sold.  Each try is cut off after
 * `timeoutMs`; the check retries once on a network error, a timeout or a bad
 * answer, and if the second try fails too the result is "unavailable" — which
 * blocks the sale (fail closed).  `send` is the request itself (apiFetch in the
 * app); it is handed an AbortSignal to pass on.
 */
export async function checkTillStock(
  send: TillCheckSend,
  cart: CartLine[],
  timeoutMs: number = TILL_CHECK_TIMEOUT_MS,
): Promise<TillCheckResult> {
  const body = buildTillCheckBody(cart);
  let result: TillCheckResult = { status: "unavailable" };
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      result = await tryOnce(send, body, timeoutMs);
    } catch {
      result = { status: "unavailable" };
    }
    if (result.status !== "unavailable") return result;
  }
  return result;
}
