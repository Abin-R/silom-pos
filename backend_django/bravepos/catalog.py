"""Copying one branch's catalogue onto another.

Products are per-branch rows: "Sexy Back Cookie" at BIO HOUSE and the one at
Silom are two records whose only connection is that somebody typed the same
name twice.  This module is the single place that knows how to make a second
branch's list match a first one's, and it backs two callers:

* the backoffice **Sync products** page, which is how a newly opened branch
  gets its menu and how a product added at one shop reaches the others;
* ``manage.py sync_products``, the same job from a terminal.

The two differ in exactly one respect, and it is the important one:

**The page adds; it does not overwrite.**  A product the target branch already
has is left completely alone — its price, its cost, its photo, its stock.  That
is what makes the page safe to press twice, and safe to press on a branch that
has been running its own prices for a month: the worst it can do is add rows
that were missing.  The command keeps its older ``update_existing`` behaviour,
because pushing a price change out of a terminal is what it was written for.

Three things are never copied either way:

* **Stock.**  On-hand quantity is a fact about one shop's shelves — BIO HOUSE
  sits at -160 Choco Gems because deliveries were never recorded, and that
  number is meaningless anywhere else.  A new copy starts at 0.
* **Orders, movements, customers.**  Only the menu travels.
* **The source branch's own rows.**  Category and unit are resolved, and
  created if missing, *under the target*.  A copy pointing at the source's
  category would mean renaming a category at one branch silently moved a
  product at another.

Matching is by name, case-insensitively, which is what makes "already there"
mean what an admin means by it: the name is what they read on the till.
"""
from __future__ import annotations

from .models import Branch, Category, Product, Unit

# Copied verbatim from source to target.  Everything absent from this list is
# either identity (id, branch), a foreign key resolved per-branch below
# (category, unit), or deliberately branch-local (stock — see the docstring).
PRODUCT_FIELDS = (
    "name_th", "price", "cost", "par_level", "sku", "barcode",
    "image_url", "image_base64", "is_favorite", "tax_type", "product_type",
    "active", "sort_order",
)

CATEGORY_FIELDS = ("name_th", "color", "order", "source", "active")


def other_branches(branch):
    """Every other active branch, in the order the backoffice lists them."""
    qs = Branch.objects.filter(active=True).order_by("name")
    return qs.exclude(pk=branch.pk) if branch is not None else qs


def source_catalogue(branch):
    """The rows a sync would read out of ``branch``.

    Only active products travel: a product taken off sale at the source is one
    the shop has decided to stop selling, and seeding a new branch with it
    would undo that decision at the new shop.
    """
    cats = list(branch.categories.all())
    prods = list(
        branch.products.filter(active=True).select_related("category", "unit")
    )
    return cats, prods


def removed_catalogue(branch):
    """Products ``branch`` has taken off sale.

    Deliberately not folded into `source_catalogue`: these travel for the
    opposite reason. An active product travels to be *created* at the target;
    a removed one travels only to retire a copy the target already has, and
    must never be created there.

    Without this, removing a product at the source could not propagate at all —
    it simply dropped out of the payload, so the target's copy was never looked
    at and stayed on sale.
    """
    return list(branch.products.filter(active=False).only("id", "name"))


# ── Matching ────────────────────────────────────────────────────────────────
def _find(qs, name):
    name = (name or "").strip()
    return qs.filter(name__iexact=name).first() if name else None


def _diff(dest, src, fields):
    return [f for f in fields if getattr(dest, f) != getattr(src, f)]


# ── Copying ─────────────────────────────────────────────────────────────────
def upsert_category(target, cat, *, update_existing):
    """Give ``target`` a category matching ``cat``.  Returns (row, action, changed)."""
    dest = _find(target.categories, cat.name)
    if dest is None:
        dest = Category(branch=target, name=cat.name)
        for field in CATEGORY_FIELDS:
            setattr(dest, field, getattr(cat, field))
        dest.save()
        return dest, "created", []

    if not update_existing:
        return dest, "skipped", []

    changed = _diff(dest, cat, CATEGORY_FIELDS)
    if changed:
        for field in changed:
            setattr(dest, field, getattr(cat, field))
        dest.save(update_fields=changed)
        return dest, "updated", changed
    return dest, "skipped", []


def unit_for(target, unit):
    """Reuse a shop-wide unit; recreate a branch-scoped one under ``target``."""
    if unit is None or unit.branch_id is None:
        return unit
    dest = _find(target.units, unit.name)
    if dest is None:
        dest = Unit.objects.create(
            branch=target, name=unit.name, order=unit.order, active=unit.active,
        )
    return dest


def upsert_product(target, prod, category, *, update_existing, copy_stock=False):
    """Give ``target`` a product matching ``prod``.  Returns (row, action, changed)."""
    dest = _find(target.products, prod.name)
    if dest is None:
        unit = unit_for(target, prod.unit)
        dest = Product(branch=target, name=prod.name)
        for field in PRODUCT_FIELDS:
            setattr(dest, field, getattr(prod, field))
        dest.category = category
        dest.unit = unit
        dest.stock = prod.stock if copy_stock else 0
        dest.save()
        return dest, "created", []

    if not update_existing:
        # The whole point of the page: a product this branch already has keeps
        # its own price, cost, photo and stock.  Nothing is touched.
        return dest, "skipped", []

    unit = unit_for(target, prod.unit)
    fields = list(PRODUCT_FIELDS) + (["stock"] if copy_stock else [])
    changed = _diff(dest, prod, fields)
    if dest.category_id != (category.pk if category else None):
        changed.append("category")
    if dest.unit_id != (unit.pk if unit else None):
        changed.append("unit")

    if changed:
        for field in changed:
            if field == "category":
                dest.category = category
            elif field == "unit":
                dest.unit = unit
            else:
                setattr(dest, field, getattr(prod, field))
        # update_fields so a sync cannot clobber a column it was not asked to
        # touch — notably stock, edited from the POS while this runs.
        dest.save(update_fields=changed)
        return dest, "updated", changed
    return dest, "skipped", []


def retire_removed(target, removed):
    """Take off sale at ``target`` the products the source has removed.

    Matched by name, exactly the way `upsert_product` matches, so this retires
    the same row an earlier sync would have created — no more.

    Only rows already on sale are touched, and nothing is ever created: a
    product the target never had is not this sync's business. Reversible from
    the target branch's own Removed view, like any other removal.
    """
    retired = []
    for prod in removed:
        dest = _find(target.products.filter(active=True), prod.name)
        if dest is None:
            continue
        dest.active = False
        dest.save(update_fields=["active"])
        retired.append(dest.name)
    return retired


def copy_catalogue(source, target, *, update_existing=False, copy_stock=False,
                   catalogue=None, removed=None, retire=False):
    """Copy ``source``'s catalogue onto ``target``.  Returns a per-item report.

    ``catalogue`` is the ``(cats, prods)`` pair from `source_catalogue`, passed
    in when several targets are being done in one go so the source is read once
    rather than once per branch.

    The caller wraps this in a transaction: a half-copied catalogue is worse
    than none, because nothing on screen tells you where it stopped.
    """
    cats, prods = catalogue if catalogue is not None else source_catalogue(source)
    report = {"branch": target, "categories": [], "products": []}

    cat_map = {}
    for cat in cats:
        dest, action, changed = upsert_category(
            target, cat, update_existing=update_existing,
        )
        cat_map[cat.id] = dest
        report["categories"].append(
            {"name": cat.name, "action": action, "changed": changed},
        )

    for prod in prods:
        dest, action, changed = upsert_product(
            target, prod, cat_map.get(prod.category_id) if prod.category_id else None,
            update_existing=update_existing, copy_stock=copy_stock,
        )
        report["products"].append(
            {"name": prod.name, "action": action, "changed": changed},
        )

    report["retired"] = retire_removed(target, removed or []) if retire else []

    return report


# ── Preview ─────────────────────────────────────────────────────────────────
def preview(source, target, catalogue=None, removed=None):
    """What a sync would add to ``target``, writing nothing.

    Names the products rather than only counting them: "18 products will be
    added" is not something an admin can check, and this page's one dangerous
    property is that it writes to a branch nobody is looking at.
    """
    cats, prods = catalogue if catalogue is not None else source_catalogue(source)
    existing = {
        n.strip().lower()
        for n in target.products.values_list("name", flat=True) if n
    }
    have_cats = {
        n.strip().lower()
        for n in target.categories.values_list("name", flat=True) if n
    }

    adding = [p.name for p in prods if (p.name or "").strip().lower() not in existing]

    # Only rows the target is currently selling can be retired, so the preview
    # counts those and not every name that happens to match.
    on_sale = {
        n.strip().lower()
        for n in target.products.filter(active=True).values_list("name", flat=True) if n
    }
    retiring = [
        p.name for p in (removed or [])
        if (p.name or "").strip().lower() in on_sale
    ]

    return {
        "branch": target,
        "adding": adding,
        "retiring": retiring,
        "already": len(prods) - len(adding),
        "categories_adding": [
            c.name for c in cats if (c.name or "").strip().lower() not in have_cats
        ],
        "product_total": target.products.count(),
    }
