/**
 * Combination and free-item discounts at the till.
 *
 * The POS only has per-line discounts (`CartItem.discount`, in baht), so both
 * kinds are written onto lines:
 *
 * - **Combination** ("Cake AND any drink ≥ 2"): the discount is taken off the
 *   matched items as a set and split across their lines in proportion to price,
 *   with the leftover satang on the last line so the parts add up exactly.
 *   Items beyond the set stay full price. It repeats for every complete set.
 *   Units are matched most-expensive first — the best result for the customer.
 * - **Free item**: choosing the discount on a product adds a separate line of
 *   the free product at ฿0, linked to the line that earned it.
 *
 * `reconcileDiscounts` recomputes both from scratch and is run after every
 * cart change, so a discount never outlives its reason: remove the drink and
 * the cake's share of the combination goes too; remove the cake and its free
 * item goes with it.
 *
 * Pure functions, no React — so the arithmetic can be checked on its own.
 */

export type DiscountPreset = {
  id: string;
  name: string;
  kind: "percent" | "fixed" | "free";
  value: number;
  applies_to?: "all" | "products" | "categories" | "combo";
  all_products: boolean;
  product_ids: string[];
  /** Combination rows; every row must be met. */
  conditions?: { product_ids: string[]; min_qty: number }[];
  free_product?: { id: string; name: string; price: number } | null;
  free_qty?: number;
};

export type DiscountLine = {
  product_id: string;
  name: string;
  price: number;
  qty: number;
  discount?: number;
  discount_type_id?: string;
  discount_label?: string;
  discount_reason?: string;
  /** Set on a free-item line: the real product, since `product_id` is a key. */
  real_product_id?: string;
  /** Set on a free-item line: the `product_id` of the line that earned it. */
  free_of?: string;
};

export const isCombo = (p: DiscountPreset | undefined) =>
  !!p && p.applies_to === "combo" && !!p.conditions?.length;

export const isFree = (p: DiscountPreset | undefined) =>
  !!p && p.kind === "free" && !!p.free_product;

const round2 = (n: number) => Math.round(n * 100) / 100;

export const freeLineKey = (presetId: string, triggerPid: string) =>
  `free:${presetId}:${triggerPid}`;

/** A free-item line never counts toward anything else. */
const isFreeLine = (l: DiscountLine) => !!l.free_of;

/**
 * May this line's units be used by `combo`? Lines already carrying a
 * different discount are left alone — one discount per line.
 */
function eligible(l: DiscountLine, comboId: string) {
  if (isFreeLine(l)) return false;
  if (l.discount_type_id === comboId) return true;
  return !l.discount_type_id && !(l.discount && l.discount > 0);
}

/**
 * Match `combo` against the cart. Returns the baht discount per line
 * `product_id` and how many complete sets were found.
 */
export function matchCombo(
  lines: DiscountLine[],
  combo: DiscountPreset,
): { perLine: Map<string, number>; sets: number } {
  const perLine = new Map<string, number>();
  const rows = combo.conditions || [];
  if (!rows.length) return { perLine, sets: 0 };

  const avail = new Map<string, { price: number; left: number }>();
  for (const l of lines) {
    if (eligible(l, combo.id) && l.qty > 0) avail.set(l.product_id, { price: l.price, left: l.qty });
  }

  let sets = 0;
  // A generous ceiling rather than `while (true)`: a set always consumes at
  // least one unit, so this is never the limit in practice.
  for (let guard = 0; guard < 1000; guard++) {
    const take = new Map<string, number>();
    let ok = true;
    for (const row of rows) {
      let need = Math.max(1, row.min_qty || 1);
      const candidates = row.product_ids
        .filter((pid) => avail.has(pid))
        .sort((a, b) => avail.get(b)!.price - avail.get(a)!.price);
      for (const pid of candidates) {
        if (need === 0) break;
        const free = avail.get(pid)!.left - (take.get(pid) || 0);
        const n = Math.min(free, need);
        if (n > 0) {
          take.set(pid, (take.get(pid) || 0) + n);
          need -= n;
        }
      }
      if (need > 0) {
        ok = false;
        break;
      }
    }
    if (!ok) break;

    sets++;
    const parts = [...take.entries()].map(([pid, n]) => ({
      pid,
      value: avail.get(pid)!.price * n,
    }));
    const setValue = parts.reduce((s, p) => s + p.value, 0);
    const setDiscount = round2(
      combo.kind === "percent"
        ? (setValue * Math.min(100, combo.value)) / 100
        : Math.min(setValue, combo.value),
    );
    // Pro-rata by price; the last part takes the rounding remainder.
    let given = 0;
    parts.forEach((p, i) => {
      const share =
        i === parts.length - 1
          ? round2(setDiscount - given)
          : setValue > 0
            ? round2((setDiscount * p.value) / setValue)
            : 0;
      given = round2(given + share);
      perLine.set(p.pid, round2((perLine.get(p.pid) || 0) + share));
    });
    for (const [pid, n] of take) avail.get(pid)!.left -= n;
  }
  return { perLine, sets };
}

/** Recompute every combination and free item in the cart. */
export function reconcileDiscounts<T extends DiscountLine>(
  cart: T[],
  presets: DiscountPreset[] | null,
): T[] {
  if (!presets) return cart;
  const byId = new Map(presets.map((p) => [p.id, p]));
  let lines = cart.map((l) => ({ ...l }));

  // ── Free items ───────────────────────────────────────────────────
  // What should exist: one free line per line carrying a free-item preset.
  const wanted = new Map<string, T>();
  for (const l of lines) {
    if (isFreeLine(l) || !l.discount_type_id) continue;
    const p = byId.get(l.discount_type_id);
    if (!isFree(p)) continue;
    const fp = p!.free_product!;
    const qty = Math.max(1, p!.free_qty || 1);
    wanted.set(freeLineKey(p!.id, l.product_id), {
      product_id: freeLineKey(p!.id, l.product_id),
      real_product_id: fp.id,
      free_of: l.product_id,
      name: fp.name,
      price: fp.price,
      qty,
      discount: round2(fp.price * qty),
      discount_type_id: p!.id,
      discount_label: p!.name,
    } as T);
  }
  // Drop free lines whose reason has gone; refresh and keep the rest in place.
  lines = lines
    .filter((l) => !isFreeLine(l) || wanted.has(l.product_id))
    .map((l) => (isFreeLine(l) ? { ...l, ...wanted.get(l.product_id)! } : l));
  for (const [key, line] of wanted) {
    if (!lines.some((l) => l.product_id === key)) lines.push(line);
  }

  // ── Combinations ─────────────────────────────────────────────────
  // An id the feed no longer knows (deleted since it was applied) keeps its
  // amount: the till can't tell a retired combination from a retired
  // one-product preset, and the modal treats the latter the same way.
  const comboIds = new Set(
    lines
      .filter((l) => !isFreeLine(l) && l.discount_type_id)
      .map((l) => l.discount_type_id!)
      .filter((id) => isCombo(byId.get(id))),
  );
  for (const id of comboIds) {
    const combo = byId.get(id)!;
    const perLine = matchCombo(lines, combo).perLine;
    lines = lines.map((l) => {
      if (!eligible(l, id)) return l;
      const d = perLine.get(l.product_id) || 0;
      if (d > 0) {
        return {
          ...l,
          discount: d,
          discount_type_id: id,
          discount_label: combo.name,
          discount_reason: undefined,
        };
      }
      if (l.discount_type_id === id) {
        return {
          ...l,
          discount: undefined,
          discount_type_id: undefined,
          discount_label: undefined,
          discount_reason: undefined,
        };
      }
      return l;
    });
  }
  return lines;
}

/** How many complete sets of `combo` the cart holds, counting `pid`'s line as free to use. */
export function comboSetsWith(
  lines: DiscountLine[],
  combo: DiscountPreset,
  pid: string,
): number {
  const probe = lines.map((l) =>
    l.product_id === pid
      ? { ...l, discount: undefined, discount_type_id: undefined }
      : l,
  );
  return matchCombo(probe, combo).sets;
}
