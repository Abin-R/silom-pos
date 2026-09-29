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
 * Two bill-level rules apply to every kind of promotion:
 *
 * - **Minimum order**: offered, and kept, only while the bill before any
 *   discount is at least `min_order_amount`.
 * - **Maximum discount**: everything one promotion takes off the bill is
 *   capped at `max_discount`, shared across its lines in proportion.
 *
 * `reconcileDiscounts` recomputes all of it from scratch and is run after
 * every cart change, so a discount never outlives its reason: remove the
 * drink and the cake's share of the combination goes too; remove the cake and
 * its free item goes with it; drop below the minimum and the promotion comes
 * off.
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
  /** Combination rows: every row together (`match` "all") or any one ("any"). */
  conditions?: { product_ids: string[]; min_qty: number }[];
  match?: "all" | "any";
  /** Bill total before discounts the promotion needs; null = none. */
  min_order_amount?: number | null;
  /** Most the promotion can take off one bill; null = no cap. */
  max_discount?: number | null;
  free_product?: { id: string; name: string; price: number } | null;
  free_qty?: number;
  /** Promotion details shown under the dropdown entry. */
  code?: string;
  start_date?: string | null; // YYYY-MM-DD, inclusive
  end_date?: string | null;
  summary?: string;
};

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-09-30" → "30 Sep 2026", without relying on Intl (thin on Hermes). */
export function shortDate(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  return y && m && d ? `${d} ${MONTHS[m - 1]} ${y}` : iso;
}

/**
 * "PR-0003 · Choc chip + Drinks ×2 · 1 Sep – 30 Sep 2026": the promotion's
 * ID, what the customer buys, and when it runs. `until`/`from` are the
 * translated words for a one-sided period.
 */
export function presetDetails(
  p: DiscountPreset,
  words: { until: string; from: string; minBill: string; maxOff: string },
): string {
  let when = "";
  if (p.start_date && p.end_date) when = `${shortDate(p.start_date)} – ${shortDate(p.end_date)}`;
  else if (p.end_date) when = `${words.until} ${shortDate(p.end_date)}`;
  else if (p.start_date) when = `${words.from} ${shortDate(p.start_date)}`;
  const baht = (n: number) => `฿${n % 1 === 0 ? n : n.toFixed(2)}`;
  return [
    p.code,
    p.summary,
    p.min_order_amount ? `${words.minBill} ${baht(p.min_order_amount)}` : "",
    p.max_discount ? `${words.maxOff} ${baht(p.max_discount)}` : "",
    when,
  ]
    .filter(Boolean)
    .join(" · ");
}

/** The bill before any discount — what a minimum order is checked against. */
export function billBeforeDiscounts(lines: DiscountLine[]): number {
  return lines
    .filter((l) => !l.free_of)
    .reduce((s, l) => s + l.price * l.qty, 0);
}

/** Does the bill reach `p`'s minimum order (if it has one)? */
export const meetsMinimum = (p: DiscountPreset, bill: number) =>
  !p.min_order_amount || bill + 1e-9 >= p.min_order_amount;

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
  /** A free item given by a promotion (stored on the bill as such). */
  is_free?: boolean;
  /**
   * How the promotion applied, in words — "combination (all of these) ·
   * 2 sets · capped at ฿100". Stored on the bill line for audit.
   */
  discount_logic?: string;
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

  // Fill one row from what is left, most expensive units first, into `take`.
  // Returns false (leaving `take` untouched) when the row can't be met.
  const fill = (row: { product_ids: string[]; min_qty: number }, take: Map<string, number>) => {
    let need = Math.max(1, row.min_qty || 1);
    const got = new Map<string, number>();
    const candidates = row.product_ids
      .filter((pid) => avail.has(pid))
      .sort((a, b) => avail.get(b)!.price - avail.get(a)!.price);
    for (const pid of candidates) {
      if (need === 0) break;
      const free = avail.get(pid)!.left - (take.get(pid) || 0) - (got.get(pid) || 0);
      const n = Math.min(free, need);
      if (n > 0) {
        got.set(pid, (got.get(pid) || 0) + n);
        need -= n;
      }
    }
    if (need > 0) return false;
    for (const [pid, n] of got) take.set(pid, (take.get(pid) || 0) + n);
    return true;
  };

  const any = combo.match === "any";
  let sets = 0;
  // A generous ceiling rather than `while (true)`: a set always consumes at
  // least one unit, so this is never the limit in practice.
  for (let guard = 0; guard < 1000; guard++) {
    const take = new Map<string, number>();
    // AND: every row, into one set. OR: the first row that can still be met
    // is a set on its own.
    const ok = any ? rows.some((row) => fill(row, take)) : rows.every((row) => fill(row, take));
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

  // ── Minimum order ────────────────────────────────────────────────
  // A promotion whose minimum the bill no longer reaches comes off. Its free
  // item then goes below, with the discount that earned it.
  const bill = billBeforeDiscounts(lines);
  lines = lines.map((l) => {
    const p = l.discount_type_id ? byId.get(l.discount_type_id) : undefined;
    if (!p || isFreeLine(l) || meetsMinimum(p, bill)) return l;
    return {
      ...l,
      discount: undefined,
      discount_type_id: undefined,
      discount_label: undefined,
      discount_reason: undefined,
    };
  });

  // ── One-product presets ──────────────────────────────────────────
  // Recomputed from the preset, not kept from when it was picked: a quantity
  // change moves a percentage with it, and an amount trimmed by the cap below
  // comes back if the other lines sharing the cap go.
  lines = lines.map((l) => {
    const p = l.discount_type_id ? byId.get(l.discount_type_id) : undefined;
    if (!p || isFreeLine(l) || isCombo(p) || isFree(p)) return l;
    const gross = l.price * l.qty;
    const d = round2(
      p.kind === "percent" ? (gross * Math.min(100, p.value)) / 100 : Math.min(gross, p.value),
    );
    return { ...l, discount: d > 0 ? d : undefined, discount_label: p.name };
  });

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
      is_free: true,
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
  const setsBy = new Map<string, number>();
  for (const id of comboIds) {
    const combo = byId.get(id)!;
    const { perLine, sets } = matchCombo(lines, combo);
    setsBy.set(id, sets);
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

  // ── Maximum discount ─────────────────────────────────────────────
  // Everything one promotion takes off the bill, capped and shared across its
  // lines in proportion; the last line takes the rounding remainder.
  const capped = new Set<string>();
  for (const p of presets) {
    if (!p.max_discount || isFree(p)) continue;
    const idx = lines
      .map((l, i) => (l.discount_type_id === p.id && !isFreeLine(l) && (l.discount || 0) > 0 ? i : -1))
      .filter((i) => i >= 0);
    const total = idx.reduce((s, i) => s + (lines[i].discount || 0), 0);
    if (total <= p.max_discount + 1e-9) continue;
    capped.add(p.id);
    const cap = p.max_discount;
    let given = 0;
    idx.forEach((i, k) => {
      const share =
        k === idx.length - 1
          ? round2(cap - given)
          : round2((cap * (lines[i].discount || 0)) / total);
      given = round2(given + share);
      lines[i] = { ...lines[i], discount: share };
    });
  }

  // ── What applied, in words (stored on the bill for audit) ────────
  const baht = (n: number) => `฿${n % 1 === 0 ? n : n.toFixed(2)}`;
  const names = new Map(lines.map((l) => [l.product_id, l.name]));
  return lines.map((l) => {
    const p = l.discount_type_id ? byId.get(l.discount_type_id) : undefined;
    if (!p) {
      const handTyped = l.discount_type_id && (l.discount || 0) > 0;
      return { ...l, discount_logic: handTyped ? "hand-entered discount" : undefined };
    }
    const parts: string[] = [];
    if (isFreeLine(l)) {
      parts.push(`free item with ${names.get(l.free_of!) || "a product"}`);
    } else if (isFree(p)) {
      parts.push(`earned free ${p.free_product!.name} ×${p.free_qty || 1}`);
    } else {
      const v = p.kind === "percent" ? `${p.value}% off` : `${baht(p.value)} off`;
      if (isCombo(p)) {
        const n = setsBy.get(p.id) || 0;
        parts.push(
          `combination (${p.match === "any" ? "any one of these" : "all of these"})`,
          `${v} per set`,
          `${n} set${n === 1 ? "" : "s"}`,
        );
      } else {
        parts.push(`one product`, v);
      }
    }
    if (p.min_order_amount) parts.push(`bill ≥ ${baht(p.min_order_amount)}`);
    if (capped.has(p.id)) parts.push(`capped at ${baht(p.max_discount!)} per bill`);
    return { ...l, discount_logic: parts.join(" · ") };
  });
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
