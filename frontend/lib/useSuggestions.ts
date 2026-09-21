/**
 * "Goes well with…" for the cashier's cart.
 *
 * Everything expensive already happened in the backend's weekly
 * `mine_suggestions` cron; this hook does one POST per *set* of cart products
 * and holds the answer.
 *
 * Two properties matter more than anything else here:
 *
 *   * **It keys on which products are in the cart, not on the cart array.**
 *     A quantity change must not refetch — the rules depend on what is in the
 *     basket, not how many of each.  Without this every `+` tap fires a
 *     request.
 *   * **It fails open, always.**  A recommendations outage must be invisible:
 *     no error, no spinner, no empty state, and above all nothing that touches
 *     the screen's `loading` or `shiftOpen` state, either of which would put a
 *     network hiccup between a cashier and a sale.
 *
 * No AsyncStorage cache on purpose.  Suggestions are cheap, disposable, and
 * wrong when stale, and this app has no read cache for products either — one
 * here would be the first, for the least valuable data in the app.
 */
import { useEffect, useMemo, useRef, useState } from "react";

import { apiFetch, safeJson } from "../lib/api";

/** Shaped to match the POS screen's `Product` type field for field, so a chip
 *  can be handed straight to the existing `addToCart`. */
export type Suggestion = {
  id: string;
  name: string;
  name_th?: string;
  price: number;
  category_id: string;
  image_url: string;
  image_base64?: string;
  is_favorite: boolean;
  /** Why this chip is here — drives the label above the strip. */
  reason: "pinned" | "often_together" | "popular";
  /** The cart product that triggered it, when a single one did. */
  because_id?: string | null;
  because_name?: string;
};

type SuggestionResponse = { suggestions: Suggestion[]; source?: string };

/** A cashier ringing up four items taps four times in about two seconds.
 *  350ms sits under the human "finished this line" pause. */
const DEBOUNCE_MS = 350;

/** The stable identity of a cart *as far as suggestions are concerned*. */
export function cartKey(productIds: string[]): string {
  return [...new Set(productIds)].sort().join("|");
}

/** Fill in everything the server left to the client.
 *
 *  Two things come from the local catalogue rather than the wire:
 *
 *  * **The image.** The server omits `image_base64` deliberately — it is an
 *    unbounded data URI and four of them is a fat response on shop wifi — but
 *    the screen already holds every product in memory with its image decoded.
 *  * **The trigger's name.** A mined rule names the cart product that produced
 *    it by id only. Resolving that server-side would cost a whole extra query
 *    for names the client already has, so the "Goes well with Latte" label is
 *    assembled here.
 *
 *  Both fall back gracefully for a product added on another tablet since this
 *  one last loaded `/products`. */
export function hydrate<T extends { id: string; name?: string }>(
  suggestions: Suggestion[],
  products: T[],
): Suggestion[] {
  const byId = new Map(products.map((p) => [p.id, p]));
  return suggestions.map((s) => {
    const local = byId.get(s.id);
    const trigger = s.because_id ? byId.get(s.because_id) : undefined;
    const named = {
      ...pickReason(s),
      because_name: s.because_name || trigger?.name || "",
    };
    return local ? { ...s, ...local, ...named } : { ...s, ...named };
  });
}

/** The fields that come from the recommender rather than the catalogue, kept
 *  out of the spread above so a local product row can't erase them. */
function pickReason(s: Suggestion) {
  return { reason: s.reason, because_id: s.because_id };
}

export function useSuggestions<T extends { id: string }>(
  productIds: string[],
  products: T[],
  enabled: boolean,
): Suggestion[] {
  // The answer is stored *with* the cart it answers, not on its own. That is
  // what lets the hook return [] the instant the cart changes — without it, a
  // freshly added product would keep the previous cart's chips on screen for
  // the length of the debounce, one of which might be the item just added.
  const [answer, setAnswer] = useState<{ key: string; items: Suggestion[] }>({
    key: "",
    items: [],
  });
  const key = useMemo(() => cartKey(productIds), [productIds]);
  // What the in-flight request was asked about. A slow response for an older
  // cart must not overwrite a newer answer.
  const pending = useRef("");

  useEffect(() => {
    // An empty cart shows nothing: the grid is already sitting on Favourites,
    // which is the shop's own "we think you want this" surface. Two of those
    // competing is worse than one.
    if (!enabled || !key) {
      pending.current = "";
      return;
    }

    let cancelled = false;
    pending.current = key;

    const timer = setTimeout(async () => {
      try {
        const res = await apiFetch(`/suggestions`, {
          method: "POST",
          body: JSON.stringify({ product_ids: key.split("|"), limit: 4 }),
        });
        const body = await safeJson<SuggestionResponse>(res, { suggestions: [] });
        if (cancelled || pending.current !== key) return;
        setAnswer({
          key,
          items: hydrate(
            Array.isArray(body?.suggestions) ? body.suggestions : [],
            products,
          ),
        });
      } catch {
        // Deliberately silent. Sentry already sees the failed request via
        // apiFetch; the cashier does not need to.
        if (!cancelled) setAnswer({ key, items: [] });
      }
    }, DEBOUNCE_MS);

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // `products` is intentionally absent: it changes identity on every reload
    // of the catalogue, and refetching suggestions because a price was edited
    // elsewhere is churn. The next cart change picks up the new rows.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, enabled]);

  return enabled && answer.key === key ? answer.items : [];
}
