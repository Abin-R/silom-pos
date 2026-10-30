"""The till's discount arithmetic, in Python, for the self-order website.

A line-for-line port of ``frontend/lib/discountRules.ts`` (plus the till's
"which presets does this line offer" filter from ``CartItemModal`` in
``app/pos.tsx``).  At the till the tablet computes discounts and the server
records them; a customer's phone is never trusted for money, so self-order
computes them here instead.  The till itself does not use this module.

Keep the two in step: ``bravepos/tests/fixtures/discount_cases.json`` is run
through both — ``test_discount_rules.py`` here and
``frontend/__tests__/discount-rules.test.js`` against the TypeScript — so a
change to one that the other doesn't follow fails a test.

Money is JS-style float arithmetic on purpose, rounded the way ``Math.round``
rounds, so both sides land on the same satang.  Lines and presets are plain
dicts shaped like the TypeScript ``DiscountLine`` / ``DiscountPreset``; a
missing key is ``undefined`` there and ``None`` here.
"""
from __future__ import annotations

import math


def js_round(n: float) -> float:
    """``Math.round``: halves go up, toward +infinity (Python's round() is banker's)."""
    return math.floor(n + 0.5)


def round2(n: float) -> float:
    return js_round(n * 100) / 100


def _num(n) -> str:
    """How JS prints a number in a template string: 10 → "10", 12.5 → "12.5"."""
    n = float(n)
    return str(int(n)) if n == int(n) else repr(n)


def _baht(n) -> str:
    n = float(n)
    return f"฿{int(n)}" if n % 1 == 0 else f"฿{n:.2f}"


def bill_before_discounts(lines) -> float:
    s = 0.0
    for l in lines:
        if not l.get("free_of"):
            s += l["price"] * l["qty"]
    return s


def meets_minimum(p, bill: float) -> bool:
    return not p.get("min_order_amount") or bill + 1e-9 >= p["min_order_amount"]


def is_combo(p) -> bool:
    return bool(p) and p.get("applies_to") == "combo" and bool(p.get("conditions"))


def is_free(p) -> bool:
    return bool(p) and p.get("kind") == "free" and (
        bool(p.get("free_product")) or bool(p.get("free_choices")))


def picks_free(p) -> bool:
    return is_free(p) and not p.get("free_product")


def free_product_for(p, l):
    if p.get("free_product"):
        return p["free_product"]
    for c in p.get("free_choices") or []:
        if c["id"] == l.get("free_pick"):
            return c
    return None


def is_free_line(l) -> bool:
    return bool(l.get("free_of"))


def free_line_key(preset_id: str, trigger_pid: str) -> str:
    return f"free:{preset_id}:{trigger_pid}"


def _eligible(l, combo_id) -> bool:
    if is_free_line(l):
        return False
    if l.get("discount_type_id") == combo_id:
        return True
    return not l.get("discount_type_id") and not (l.get("discount") and l["discount"] > 0)


def match_combo(lines, combo):
    """Per-line baht discount for ``combo`` and how many complete sets matched."""
    per_line: dict = {}
    rows = combo.get("conditions") or []
    if not rows:
        return per_line, 0

    avail: dict = {}
    for l in lines:
        if _eligible(l, combo["id"]) and l["qty"] > 0:
            avail[l["product_id"]] = {"price": l["price"], "left": l["qty"]}

    def fill(row, take) -> bool:
        need = max(1, row.get("min_qty") or 1)
        got: dict = {}
        candidates = sorted(
            (pid for pid in row["product_ids"] if pid in avail),
            key=lambda pid: -avail[pid]["price"],
        )
        for pid in candidates:
            if need == 0:
                break
            free = avail[pid]["left"] - take.get(pid, 0) - got.get(pid, 0)
            n = min(free, need)
            if n > 0:
                got[pid] = got.get(pid, 0) + n
                need -= n
        if need > 0:
            return False
        for pid, n in got.items():
            take[pid] = take.get(pid, 0) + n
        return True

    any_one = combo.get("match") == "any"
    sets = 0
    for _guard in range(1000):
        take: dict = {}
        if any_one:
            ok = any(fill(row, take) for row in rows)
        else:
            ok = all(fill(row, take) for row in rows)
        if not ok:
            break

        sets += 1
        parts = [(pid, avail[pid]["price"] * n) for pid, n in take.items()]
        set_value = 0.0
        for _pid, v in parts:
            set_value += v
        if combo["kind"] == "percent":
            set_discount = round2((set_value * min(100, combo["value"])) / 100)
        else:
            set_discount = round2(min(set_value, combo["value"]))
        given = 0.0
        for i, (pid, value) in enumerate(parts):
            if i == len(parts) - 1:
                share = round2(set_discount - given)
            elif set_value > 0:
                share = round2((set_discount * value) / set_value)
            else:
                share = 0
            given = round2(given + share)
            per_line[pid] = round2(per_line.get(pid, 0) + share)
        for pid, n in take.items():
            avail[pid]["left"] -= n
    return per_line, sets


def _cleared(l) -> dict:
    return {**l, "discount": None, "discount_type_id": None,
            "discount_label": None, "discount_reason": None}


def reconcile(cart, presets):
    """``reconcileDiscounts``: recompute every discount in the cart from scratch."""
    if presets is None:
        return cart
    by_id = {p["id"]: p for p in presets}
    lines = [dict(l) for l in cart]

    # Minimum order.
    bill = bill_before_discounts(lines)
    out = []
    for l in lines:
        p = by_id.get(l.get("discount_type_id")) if l.get("discount_type_id") else None
        if not p or is_free_line(l) or meets_minimum(p, bill):
            out.append(l)
        else:
            out.append(_cleared(l))
    lines = out

    # One-product presets.
    out = []
    for l in lines:
        p = by_id.get(l.get("discount_type_id")) if l.get("discount_type_id") else None
        if not p or is_free_line(l) or is_combo(p) or is_free(p):
            out.append(l)
            continue
        gross = l["price"] * l["qty"]
        if p["kind"] == "percent":
            d = round2((gross * min(100, p["value"])) / 100)
        else:
            d = round2(min(gross, p["value"]))
        out.append({**l, "discount": d if d > 0 else None, "discount_label": p["name"]})
    lines = out

    # Free items.
    wanted: dict = {}
    for l in lines:
        if is_free_line(l) or not l.get("discount_type_id"):
            continue
        p = by_id.get(l["discount_type_id"])
        if not is_free(p):
            continue
        fp = free_product_for(p, l)
        if not fp:
            continue
        qty = max(1, p.get("free_qty") or 1)
        key = free_line_key(p["id"], l["product_id"])
        wanted[key] = {
            "product_id": key,
            "real_product_id": fp["id"],
            "free_of": l["product_id"],
            "name": fp["name"],
            "price": fp["price"],
            "qty": qty,
            "discount": round2(fp["price"] * qty),
            "discount_type_id": p["id"],
            "discount_label": p["name"],
            "is_free": True,
        }
    lines = [
        ({**l, **wanted[l["product_id"]]} if is_free_line(l) else l)
        for l in lines
        if not is_free_line(l) or l["product_id"] in wanted
    ]
    for key, line in wanted.items():
        if not any(l["product_id"] == key for l in lines):
            lines.append(line)

    # Combinations.
    combo_ids: list = []
    for l in lines:
        tid = l.get("discount_type_id")
        if not is_free_line(l) and tid and is_combo(by_id.get(tid)) and tid not in combo_ids:
            combo_ids.append(tid)
    sets_by: dict = {}
    for cid in combo_ids:
        combo = by_id[cid]
        per_line, sets = match_combo(lines, combo)
        sets_by[cid] = sets
        out = []
        for l in lines:
            if not _eligible(l, cid):
                out.append(l)
                continue
            d = per_line.get(l["product_id"], 0)
            if d > 0:
                out.append({**l, "discount": d, "discount_type_id": cid,
                            "discount_label": combo["name"], "discount_reason": None})
            elif l.get("discount_type_id") == cid:
                out.append(_cleared(l))
            else:
                out.append(l)
        lines = out

    # Maximum discount.
    capped: set = set()
    for p in presets:
        if not p.get("max_discount") or is_free(p):
            continue
        idx = [i for i, l in enumerate(lines)
               if l.get("discount_type_id") == p["id"] and not is_free_line(l)
               and (l.get("discount") or 0) > 0]
        total = 0.0
        for i in idx:
            total += lines[i].get("discount") or 0
        if total <= p["max_discount"] + 1e-9:
            continue
        capped.add(p["id"])
        cap = p["max_discount"]
        given = 0.0
        for k, i in enumerate(idx):
            if k == len(idx) - 1:
                share = round2(cap - given)
            else:
                share = round2((cap * (lines[i].get("discount") or 0)) / total)
            given = round2(given + share)
            lines[i] = {**lines[i], "discount": share}

    # What applied, in words (stored on the bill for audit).
    names = {l["product_id"]: l["name"] for l in lines}
    out = []
    for l in lines:
        p = by_id.get(l.get("discount_type_id")) if l.get("discount_type_id") else None
        if not p:
            hand_typed = l.get("discount_type_id") and (l.get("discount") or 0) > 0
            out.append({**l, "discount_logic": "hand-entered discount" if hand_typed else None})
            continue
        parts = []
        if is_free_line(l):
            parts.append(f"free item with {names.get(l['free_of']) or 'a product'}")
        elif is_free(p):
            fp = free_product_for(p, l)
            parts.append(f"earned free {(fp or {}).get('name') or 'item'} ×{p.get('free_qty') or 1}")
        else:
            v = f"{_num(p['value'])}% off" if p["kind"] == "percent" else f"{_baht(p['value'])} off"
            if is_combo(p):
                n = sets_by.get(p["id"], 0)
                parts += [
                    f"combination ({'any one of these' if p.get('match') == 'any' else 'all of these'})",
                    f"{v} per set",
                    f"{n} set{'' if n == 1 else 's'}",
                ]
            else:
                parts += ["one product", v]
        if p.get("min_order_amount"):
            parts.append(f"bill ≥ {_baht(p['min_order_amount'])}")
        if p["id"] in capped:
            parts.append(f"capped at {_baht(p['max_discount'])} per bill")
        out.append({**l, "discount_logic": " · ".join(parts)})
    return out


def combo_sets_with(lines, combo, pid) -> int:
    probe = [({**l, "discount": None, "discount_type_id": None}
              if l["product_id"] == pid else l) for l in lines]
    return match_combo(probe, combo)[1]


def offered(lines, presets, pid) -> list:
    """The presets the till's dropdown offers for the line ``pid`` in ``lines``."""
    bill = bill_before_discounts(lines)
    out = []
    for p in presets or []:
        if not meets_minimum(p, bill):
            continue
        if is_combo(p):
            if (any(pid in r["product_ids"] for r in p["conditions"])
                    and combo_sets_with(lines, p, pid) > 0):
                out.append(p)
            continue
        if p.get("all_products") or pid in (p.get("product_ids") or []):
            out.append(p)
    return out


def preset_details(p) -> str:
    """``presetDetails`` with the English words: the line under a dropdown entry."""
    def short(iso):
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        y, m, d = (int(x) for x in iso.split("-"))
        return f"{d} {months[m - 1]} {y}"

    when = ""
    if p.get("start_date") and p.get("end_date"):
        when = f"{short(p['start_date'])} – {short(p['end_date'])}"
    elif p.get("end_date"):
        when = f"Until {short(p['end_date'])}"
    elif p.get("start_date"):
        when = f"From {short(p['start_date'])}"
    parts = [
        p.get("code"),
        p.get("summary"),
        f"Bill ≥ {_baht(p['min_order_amount'])}" if p.get("min_order_amount") else "",
        f"Max off {_baht(p['max_discount'])}" if p.get("max_discount") else "",
        when,
    ]
    return " · ".join(x for x in parts if x)
