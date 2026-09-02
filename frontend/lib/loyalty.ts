/**
 * CRM loyalty at the till — points and rewards for the customer on the bill.
 *
 * The app never talks to crm.rollingpinn.com. Its API key can read and write
 * any member at any branch, and this repo (and the APK built from it) are
 * public, so the key lives on the server: the till asks `POST /api/crm/member`
 * and the backend makes the call. See backend_django/bravepos/loyalty.py.
 *
 * Two rules shape everything below:
 *
 *  1. **Loyalty is decoration.** It must never be able to stop a sale. A
 *     branch outside the rollout, a customer with no phone number, a CRM that
 *     is down — all of them end with the cart looking exactly as it did before
 *     this existed, and the cashier rings the bill up as normal.
 *
 *  1a. **A branch outside the rollout makes no request at all.** The flag is
 *     read once from the branch feed and the lookup is skipped entirely — not
 *     sent and declined. Every branch but the one being trialled must be
 *     byte-for-byte the till it was, including its network traffic. The read
 *     fails closed: a branch feed that doesn't answer means no loyalty.
 *
 *  2. **A toggle that does nothing is worse than no toggle.** A voucher the
 *     customer is still holding can only be redeemed by them, on their own
 *     phone; sending its id with an order is ignored *silently* by the CRM.
 *     So the backend marks each reward `redeemable` and only those get a
 *     control — the rest are shown as information.
 *
 * Ticking a reward changes no price. It records that this bill consumed a
 * voucher the customer had already redeemed themselves, which the CRM
 * confirms when the sale is filed. Whatever the reward is worth was handed
 * over at the counter, not discounted off the total.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch, safeJson } from "./api";

export type LoyaltyReward = {
  id: number;
  title: string;
  detail: string;
  /** False for a voucher the customer is still holding — display only. */
  redeemable: boolean;
  /** The five minutes after the customer redeemed it. A badge, not a deadline. */
  in_redemption_window: boolean;
  expires_at: string | null;
};

export type LoyaltyMember = {
  id: number;
  name: string;
  /** Decimal strings, straight from the CRM. Rendered, never calculated with. */
  points_balance: string;
  total_spent: string;
  order_count: number;
  tier: string;
};

/**
 * `off` covers every ordinary reason there is nothing to show — no customer
 * on the bill, the branch is not in the rollout, the customer has no phone
 * number — so the cart has one state to check and renders nothing for it.
 * `error` is only ever a CRM that failed to answer, which is worth telling the
 * cashier about: an empty panel would read as "no rewards" and the customer
 * would be sent away without them.
 */
export type LoyaltyState =
  | { status: "off" }
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; member: LoyaltyMember | null; rewards: LoyaltyReward[] };

type ApiReply = {
  enabled?: boolean;
  member?: LoyaltyMember | null;
  rewards?: LoyaltyReward[];
};

export type Loyalty = {
  state: LoyaltyState;
  /** Ids of the rewards the cashier ticked; sent with the order as `crm_reward_ids`. */
  selected: number[];
  toggle: (rewardId: number) => void;
  /** Re-ask the CRM after a failure. */
  retry: () => void;
};

/**
 * Look up the chosen customer's loyalty standing, and hold the cashier's
 * reward ticks until the bill is paid.
 *
 * Pass the signed-in branch and the selected customer's id (null when there is
 * none). Changing the customer — including to null, when the cart is cleared —
 * drops the previous member and every tick with them: a reward belongs to one
 * customer, and carrying a tick across to the next bill would confirm a
 * stranger's voucher.
 *
 * At a branch that is not in the rollout this does nothing whatsoever. No
 * lookup is sent, so those tills are unchanged down to the requests they make.
 */
export function useLoyalty(
  branchId: string | null | undefined,
  customerId: string | null | undefined,
): Loyalty {
  const [state, setState] = useState<LoyaltyState>({ status: "off" });
  const [selected, setSelected] = useState<number[]>([]);
  const [attempt, setAttempt] = useState(0);

  // Is THIS branch in the loyalty rollout? Read once per branch from the same
  // feed the login screen uses, exactly as self-ordering does it. Undefined
  // means "not established yet", which is treated as off — so the very first
  // customer picked after launch cannot slip a lookup out of a branch that
  // should never make one.
  const [allowed, setAllowed] = useState(false);
  useEffect(() => {
    let cancelled = false;
    if (!branchId) {
      setAllowed(false);
      return;
    }
    (async () => {
      try {
        const res = await apiFetch("/branches");
        if (!res.ok || cancelled) return;
        const list = await safeJson<any[]>(res, []);
        const b = (Array.isArray(list) ? list : []).find(
          (x: any) => String(x?.id) === String(branchId),
        );
        if (!cancelled) setAllowed(!!b?.crm_loyalty_enabled);
      } catch {
        if (!cancelled) setAllowed(false); // fail closed
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [branchId]);

  // A cashier can pick the wrong customer and correct it faster than the CRM
  // answers. Without this the first (slower) reply would land last and put
  // somebody else's rewards on the bill.
  const liveRef = useRef(0);

  useEffect(() => {
    const ticket = ++liveRef.current;
    setSelected([]);
    if (!allowed || !customerId) {
      setState({ status: "off" });
      return;
    }
    setState({ status: "loading" });
    (async () => {
      try {
        const res = await apiFetch("/crm/member", {
          method: "POST",
          body: JSON.stringify({ customer_id: customerId }),
        });
        if (ticket !== liveRef.current) return;
        if (!res.ok) {
          // A 404 is the customer row itself being gone, which is the cart's
          // problem and not something a loyalty banner can help with.
          setState(res.status === 404 ? { status: "off" } : { status: "error" });
          return;
        }
        const body = await safeJson<ApiReply>(res, {});
        if (ticket !== liveRef.current) return;
        setState(
          body.enabled
            ? {
                status: "ready",
                member: body.member ?? null,
                rewards: Array.isArray(body.rewards) ? body.rewards : [],
              }
            : { status: "off" },
        );
      } catch {
        // apiFetch already reported it to Sentry; the tablet is offline or the
        // backend is unreachable, and the cart carries on without a panel.
        if (ticket === liveRef.current) setState({ status: "error" });
      }
    })();
  }, [allowed, customerId, attempt]);

  const toggle = useCallback((rewardId: number) => {
    setSelected((ids) =>
      ids.includes(rewardId) ? ids.filter((i) => i !== rewardId) : [...ids, rewardId],
    );
  }, []);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  return { state, selected, toggle, retry };
}
