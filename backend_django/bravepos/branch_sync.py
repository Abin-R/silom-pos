"""Making other branches match one branch — the backoffice Sync branches page.

Everything a branch owns is a set of per-branch rows: its products, its
categories, its promotions, its stock-out types.  Opening a new shop, or
adding something at one shop that the others should have, is the same job for
each of them — "make that branch's list look like this one".  This module
holds one *section* per list, in the order they must run, and the page drives
whichever sections the admin ticks.

Every section follows the rule products always have:

* **add** what the target is missing, **edit** what it has to match;
* rows only the target has are **left alone** — a branch may run things of
  its own;
* the preview names every row it would touch, and writes nothing.

Adding a list (Stock-In types, when they exist) is one more `Section` in
`SECTIONS`: a ``preview`` and an ``apply`` that each take
``(source, target, ctx)``.

``ctx`` is built once per run by `context`: the source's rows are read once,
not once per target, and a section can see which other sections are running
(the promotions preview needs to know products are travelling too).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import catalog, discounts
from .models import DiscountType, StockOutReason


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    blurb: str
    preview: Callable
    apply: Callable


def _norm(name):
    return (name or "").strip().lower()


# ── Categories ──────────────────────────────────────────────────────────────
def _source_parents(cats):
    seen, out = set(), []
    for c in cats:
        p = c.parent_category
        if p is not None and p.id not in seen:
            seen.add(p.id)
            out.append(p)
    return out


def preview_categories(source, target, ctx):
    cats, _ = ctx["catalogue"]
    adding, updating = [], []
    for cat in cats:
        dest = catalog._find(target.categories, cat.name)
        if dest is None:
            adding.append(cat.name)
            continue
        changed = catalog._diff(dest, cat, catalog.CATEGORY_FIELDS)
        want = cat.parent_category.name if cat.parent_category_id else ""
        have = dest.parent_category.name if dest.parent_category_id else ""
        if want and _norm(want) != _norm(have):
            changed.append("group")
        if changed:
            updating.append({"name": cat.name, "changes": [
                {"name_th": "Thai name", "parent_category": "group"}.get(f, f)
                for f in changed]})
    have_parents = {_norm(n) for n in target.parent_categories.values_list("name", flat=True)}
    return {
        "adding": adding,
        "updating": updating,
        "parents_adding": sorted(p.name for p in _source_parents(cats)
                                 if _norm(p.name) not in have_parents),
        "already": len(cats) - len(adding) - len(updating),
    }


def apply_categories(source, target, ctx):
    cats, _ = ctx["catalogue"]
    parent_map = {}
    for p in _source_parents(cats):
        parent_map[p.id], _, _ = catalog.upsert_parent(target, p, update_existing=True)
    out = {"created": 0, "updated": 0}
    for cat in cats:
        dest, action, _ = catalog.upsert_category(target, cat, update_existing=True)
        moved = catalog._file_under(dest, parent_map.get(cat.parent_category_id),
                                    overwrite=True)
        if action == "created":
            out["created"] += 1
        elif action == "updated" or moved:
            out["updated"] += 1
    return out


# ── Products ────────────────────────────────────────────────────────────────
def preview_products(source, target, ctx):
    return catalog.preview(source, target, ctx["catalogue"], removed=ctx["removed"])


def apply_products(source, target, ctx):
    return catalog.copy_catalogue(
        source, target, catalogue=ctx["catalogue"],
        removed=ctx["removed"], retire=ctx["retire"], mirror=True,
    )


# ── Stock-out types ─────────────────────────────────────────────────────────
STOCK_OUT_FIELDS = ("name_th", "active")


def add_stock_out_type(branch, name, name_th, active=True):
    """A new stock-out type at ``branch``, last in its list."""
    last = StockOutReason.objects.filter(branch=branch).order_by("-sort_order").first()
    return StockOutReason.objects.create(
        branch=branch, name=name, name_th=name_th, active=active,
        sort_order=(last.sort_order + 1) if last else 0,
    )


def _stock_out_plan(target, ctx):
    have = {_norm(r.name): r for r in StockOutReason.objects.filter(branch=target)}
    plan = []
    for r in ctx["stock_out"]:
        dest = have.get(_norm(r.name))
        if dest is None:
            # A type the source has hidden is not offered there either, so a
            # branch that never had it gains nothing by getting it hidden.
            if r.active:
                plan.append(("create", r, None, []))
            continue
        changed = [f for f in STOCK_OUT_FIELDS if getattr(dest, f) != getattr(r, f)]
        plan.append(("update" if changed else "same", r, dest, changed))
    return plan


def preview_stock_out(source, target, ctx):
    plan = _stock_out_plan(target, ctx)
    return {
        "adding": [r.name for a, r, _, _ in plan if a == "create"],
        "updating": [{"name": r.name, "changes": [
            {"name_th": "Thai name", "active": "shown/hidden"}[f] for f in ch]}
            for a, r, _, ch in plan if a == "update"],
        "already": sum(1 for a, *_ in plan if a == "same"),
    }


def apply_stock_out(source, target, ctx):
    out = {"created": 0, "updated": 0}
    for action, r, dest, changed in _stock_out_plan(target, ctx):
        if action == "create":
            add_stock_out_type(target, r.name, r.name_th, r.active)
            out["created"] += 1
        elif action == "update":
            for f in changed:
                setattr(dest, f, getattr(r, f))
            dest.save(update_fields=changed)
            out["updated"] += 1
    return out


# ── Promotions ──────────────────────────────────────────────────────────────
def _promo_plan(target, dt, assume=()):
    """``(action, reason)`` for one promotion at ``target``.

    A copy is found by ``group_id`` only.  A promotion of the target's own
    that merely shares the name is the target's, and is not taken over: the
    sync skips and says so, rather than rewriting something nobody linked.
    """
    copy = DiscountType.objects.filter(group_id=dt.group_id, branch=target).first()
    if copy is None and DiscountType.objects.filter(
            branch=target, name__iexact=dt.name).exists():
        return "skip", f"{target.name} already has its own promotion called “{dt.name}”"
    reason = discounts.promotion_blocker(dt, target, assume=assume)
    if reason:
        return "skip", f"{target.name} {reason}"
    return ("create" if copy is None else "update"), None


def preview_promotions(source, target, ctx):
    # When products/categories travel in the same run, what they will add
    # counts as present — otherwise every promotion on a new branch would
    # preview as skipped and then go through.
    assume = set()
    if "products" in ctx["sections"] or "categories" in ctx["sections"]:
        cats, prods = ctx["catalogue"]
        assume |= {_norm(c.name) for c in cats}
        if "products" in ctx["sections"]:
            assume |= {_norm(p.name) for p in prods}
    adding, updating, skipped = [], [], []
    for dt in ctx["promotions"]:
        action, reason = _promo_plan(target, dt, assume)
        label = f"{dt.code} {dt.name}" if dt.code else dt.name
        if action == "create":
            adding.append(label)
        elif action == "update":
            updating.append({"name": label, "changes": ["edited to match"]})
        else:
            skipped.append(f"{label} — {reason}")
    return {"adding": adding, "updating": updating, "skipped": skipped, "already": 0}


def apply_promotions(source, target, ctx):
    out = {"created": 0, "updated": 0, "skipped": []}
    for dt in ctx["promotions"]:
        action, reason = _promo_plan(target, dt)
        if action == "skip":
            out["skipped"].append(f"{dt.name} ({reason})")
            continue
        result, reason = discounts.copy_promotion_to(dt, target)
        if result == "skipped":
            out["skipped"].append(f"{dt.name} ({reason})")
        else:
            out[result] += 1
    return out


def source_promotions(branch):
    """Running, scheduled and paused promotions — not ones that have ended."""
    rows = (DiscountType.objects.filter(branch=branch)
            .select_related("free_product", "free_category")
            .prefetch_related("products", "categories",
                              "conditions__product", "conditions__category"))
    return [d for d in rows if d.status != DiscountType.STATUS_ENDED]


# ── Registry ────────────────────────────────────────────────────────────────
# Order matters: categories before products (a product is filed under the
# target's category), products before promotions (a promotion points at the
# target's own products, matched by name).
SECTIONS = [
    Section("categories", "Categories",
            "Every category and group, including empty ones.",
            preview_categories, apply_categories),
    Section("products", "Products",
            "Products on sale, added or edited to match. Stock is never copied.",
            preview_products, apply_products),
    Section("stock_out", "Stock-Out Dropdowns",
            "The Type list on Stock out and the till.",
            preview_stock_out, apply_stock_out),
    Section("promotions", "Promotions",
            "Running, scheduled and paused promotions — not ended ones.",
            preview_promotions, apply_promotions),
]
SECTION_KEYS = [s.key for s in SECTIONS]


def context(source, sections, *, retire=True):
    """Everything read out of ``source`` once for a run over many targets."""
    ctx = {"sections": set(sections), "retire": retire}
    if {"categories", "products", "promotions"} & ctx["sections"]:
        ctx["catalogue"] = catalog.source_catalogue(source)
    ctx["removed"] = (catalog.removed_catalogue(source)
                      if "products" in ctx["sections"] and retire else [])
    if "stock_out" in ctx["sections"]:
        ctx["stock_out"] = list(StockOutReason.objects.filter(branch=source))
    if "promotions" in ctx["sections"]:
        ctx["promotions"] = source_promotions(source)
    return ctx


def chosen(sections):
    return [s for s in SECTIONS if s.key in set(sections)]


def preview(source, target, ctx):
    return [(s, s.preview(source, target, ctx)) for s in chosen(ctx["sections"])]


def apply(source, target, ctx):
    return {s.key: s.apply(source, target, ctx) for s in chosen(ctx["sections"])}
