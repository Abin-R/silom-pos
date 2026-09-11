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
 *
 * The panel is a summary and stays one. "How many points do I have?" is
 * answered by `openViewer`, which asks the backend for a short-lived link and
 * hands the customer the CRM's own page in the device's browser — rather than
 * by growing a second copy of that page inside the cart.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import * as WebBrowser from "expo-web-browser";
import { apiFetch, safeJson } from "./api";
import { C } from "./theme";

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
  /**
   * Ask the CRM again for the same customer.
   *
   * The reason this exists is a real counter situation: the cashier picks the
   * customer, and only *then* does the customer open their phone and redeem
   * something. That voucher did not exist when we looked, so nothing on the
   * screen will ever show it — the panel is not a live feed. This is how the
   * cashier says "look again" without dropping the customer and re-picking
   * them, which would lose every tick already made.
   */
  refresh: () => void;
  /** True while `refresh` is in flight, so the list can stay put and the
   *  control can spin rather than the whole panel collapsing to a spinner. */
  refreshing: boolean;
  /**
   * Show this customer their own CRM page — points, tier, history, wallet —
   * in the device's in-app browser.
   *
   * Resolves false when there was nothing to open: no customer, a branch
   * outside the rollout, a CRM that would not mint a link, or a tablet with
   * no browser to hand it to. The caller says so; this does not, because the
   * wording belongs on the screen that raised it.
   */
  openViewer: () => Promise<boolean>;
  /** True while the link is being minted, so the control can spin. */
  viewerOpening: boolean;
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
  const [refreshing, setRefreshing] = useState(false);

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

  // Who is on the bill *now*, for a viewer link to check itself against when
  // it lands. Same hazard as above and a worse outcome: a mis-tap corrected
  // while the link was being minted would otherwise open a stranger's page,
  // with their name, spend and order history on it. Deliberately not `liveRef`
  // — that also moves on a manual refresh, which is no reason to refuse.
  const customerRef = useRef(customerId);
  useEffect(() => {
    customerRef.current = customerId;
  }, [customerId]);

  // Ticks belong to one customer, so a change of customer drops them. A
  // *refresh* must not: the cashier may already have ticked two rewards before
  // the third was redeemed, and clearing those would be a worse bug than the
  // stale list this button exists to fix.
  useEffect(() => {
    setSelected([]);
  }, [customerId]);

  // Whether the next run is a manual refresh rather than a first look. A ref,
  // not state: it is set by the button and read by the effect it triggers, and
  // a second render in between would be pointless.
  const manualRef = useRef(false);

  useEffect(() => {
    const ticket = ++liveRef.current;
    const manual = manualRef.current;
    manualRef.current = false;
    if (!allowed || !customerId) {
      setState({ status: "off" });
      return;
    }
    // A first look at a customer replaces the panel with a spinner — there is
    // nothing on screen worth keeping. A refresh keeps the list exactly where
    // it is and spins the button instead, because the cashier is mid-sale and
    // the rewards jumping about under their finger is how the wrong one gets
    // ticked.
    if (manual) setRefreshing(true);
    else setState({ status: "loading" });
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
        const rewards = Array.isArray(body.rewards) ? body.rewards : [];
        setState(
          body.enabled
            ? { status: "ready", member: body.member ?? null, rewards }
            : { status: "off" },
        );
        // Drop ticks for anything the CRM no longer offers. Between the first
        // look and this one a voucher may have been confirmed on another till,
        // or expired; sending its id would be a silent no-op, and the cashier
        // would have handed something over believing it was recorded.
        setSelected((ids) =>
          ids.filter((id) => rewards.some((r) => r.id === id && r.redeemable)),
        );
      } catch {
        // apiFetch already reported it to Sentry; the tablet is offline or the
        // backend is unreachable, and the cart carries on without a panel.
        if (ticket === liveRef.current) setState({ status: "error" });
      } finally {
        // Whatever happened, the button stops spinning. Ticks are deliberately
        // left alone on failure: the list comes back on a successful retry and
        // the cashier finds their work where they left it.
        if (ticket === liveRef.current) setRefreshing(false);
      }
    })();
  }, [allowed, customerId, attempt]);

  const toggle = useCallback((rewardId: number) => {
    setSelected((ids) =>
      ids.includes(rewardId) ? ids.filter((i) => i !== rewardId) : [...ids, rewardId],
    );
  }, []);

  const refresh = useCallback(() => {
    manualRef.current = true;
    setAttempt((n) => n + 1);
  }, []);

  const [viewerOpening, setViewerOpening] = useState(false);

  const openViewer = useCallback(async (): Promise<boolean> => {
    if (!customerId) return false;

    let url = "";
    setViewerOpening(true);
    try {
      const res = await apiFetch("/crm/viewer-link", {
        method: "POST",
        body: JSON.stringify({ customer_id: customerId }),
      });
      if (!res.ok) return false;
      const body = await safeJson<{ enabled?: boolean; url?: string }>(res, {});
      // `enabled: false` is the ordinary answer for a branch outside the
      // rollout or a customer with no phone — there is genuinely nothing to
      // open, and the button should not have been on screen for it.
      if (!body.enabled) return false;
      // The cashier corrected the customer while this was in flight. Opening
      // now would put the wrong person's points, spend and history on a
      // screen the customer at the counter is looking at.
      if (customerRef.current !== customerId) return false;
      url = (body.url || "").trim();
      // The backend already refuses anything that is not https, so this is the
      // second lock on the same door — but this is the one line in the app
      // that hands a CRM value straight to a browser, and it carries what
      // amounts to a login for that member.
      if (!url.toLowerCase().startsWith("https://")) return false;
    } catch {
      // apiFetch has already reported it; the tablet is offline or the backend
      // is unreachable, and the cart carries on exactly as it was.
      return false;
    } finally {
      // The spinner covers the round trip and stops there. It deliberately
      // does not stay up for the browser: `openBrowserAsync` only resolves
      // when the cashier closes the tab again, which can be minutes.
      setViewerOpening(false);
    }

    try {
      // An in-app browser, not a WebView embedded in the cart: the page is the
      // CRM's, it sets its own cookies, and a customer being shown their own
      // account should be able to see whose address bar it is.
      await WebBrowser.openBrowserAsync(url, {
        showTitle: true,
        toolbarColor: C.surface,
        controlsColor: C.brand,
        enableBarCollapsing: false,
        dismissButtonStyle: "close",
      });
      return true;
    } catch {
      // No browser on the tablet to hand it to. Rare, and nothing the cashier
      // can do about it at the counter, but silence would read as a dead
      // button they keep pressing.
      return false;
    }
  }, [customerId]);

  return {
    state,
    selected,
    toggle,
    refresh,
    refreshing,
    openViewer,
    viewerOpening,
  };
}
