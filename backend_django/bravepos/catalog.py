"""Copying one branch's catalogue onto another.

Products are per-branch rows: "Sexy Back Cookie" at BIO HOUSE and the one at
Silom are two records whose only connection is that somebody typed the same
name twice.  This module is the single place that knows how to make a second
branch's list match a first one's, and it backs two callers:

* the backoffice **Sync products** page, which is how a newly opened branch
  gets its menu and how a product added at one shop reaches the others;
* ``manage.py sync_products``, the same job from a terminal.

Both add what is missing, edit what is already there to match, and take off
sale what the source has removed.  Products the target sells that the source
has never had are left alone — a branch may carry items of its own.

Three things are never copied either way:

* **Stock.**  On-hand quantity is a fact about one shop's shelves — BIO HOUSE
  sits at -160 Choco Gems because deliveries were never recorded, and that
  number is meaningless anywhere else.  A new copy starts at 0.
* **Orders, movements, customers.**  Only the menu travels.
* **The source branch's own rows.**  Category and unit are resolved, and
  created if missing, *under the target*.  A copy pointing at the source's
  category would mean renaming a category at one branch silently moved a
  product at another.

Matching is by **Shopster product ID** (``barcode``) first, then by name,
case-insensitively.  The ID is unique among a branch's on-sale products, so
it survives a rename; the name is the fallback for rows that have no ID yet.
An edit passes the ID and name the product had *before* the save as well, so
changing both at once still finds every copy.
"""
from __future__ import annotations

from . import sale_windows
from .models import Branch, Category, ParentCategory, Product, Unit

# Copied verbatim from source to target.  Everything absent from this list is
# either identity (id, branch), a foreign key resolved per-branch below
# (category, unit), or deliberately branch-local (stock — see the docstring).
PRODUCT_FIELDS = (
    "name_th", "price", "cost", "par_level", "counter_stock", "shelf_life",
    "sku", "barcode",
    "image_url", "image_base64", "is_favorite", "tax_type",
    "active", "sort_order",
)

CATEGORY_FIELDS = ("name_th", "color", "order", "source", "active")

# A parent category's own fields.  Its name is the match key, so it is not here.
PARENT_FIELDS = ("name_th", "order")


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
    cats = list(branch.categories.select_related("parent_category"))
    prods = list(
        branch.products.filter(active=True)
        .select_related("category", "unit").prefetch_related("sale_windows")
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
    selling = branch.products.filter(active=True)
    ids = {b for b in selling.values_list("barcode", flat=True) if b}
    names = {(n or "").strip().lower() for n in selling.values_list("name", flat=True)}
    # A removed row that shares an ID or a name with one still on sale is an
    # old duplicate (a rename left behind, a double save), not a decision to
    # stop selling — retiring by it would take the live product off sale at
    # every other branch.
    return [
        p for p in branch.products.filter(active=False).only("id", "name", "barcode")
        if not (p.barcode and p.barcode in ids)
        and (p.name or "").strip().lower() not in names
    ]


# ── Matching ────────────────────────────────────────────────────────────────
def _find(qs, name):
    name = (name or "").strip()
    return qs.filter(name__iexact=name).first() if name else None


def find_product(qs, barcodes=(), names=()):
    """The row in ``qs`` that is this product: by Shopster ID, then by name.

    Each list is tried in order, so callers pass the value from before an
    edit first.  An on-sale row is preferred over a removed one with the same
    key — removed duplicates are common, and the live row is the one meant.
    """
    for code in barcodes:
        code = (code or "").strip()
        if code:
            row = qs.filter(barcode=code).order_by("-active").first()
            if row is not None:
                return row
    for name in names:
        name = (name or "").strip()
        if name:
            row = qs.filter(name__iexact=name).order_by("-active").first()
            if row is not None:
                return row
    return None


def barcode_taken(branch, barcode, exclude_pk=None):
    """The on-sale product at ``branch`` already using ``barcode``, or None.

    The Shopster product ID is unique among a branch's on-sale products
    (``product_barcode_unique_on_sale``); this is the check every writer makes
    first, so the database constraint is a backstop and not an error page.
    """
    barcode = (barcode or "").strip()
    if not barcode or branch is None:
        return None
    qs = Product.objects.filter(branch=branch, barcode=barcode, active=True)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.first()


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


def upsert_parent(target, parent, *, update_existing):
    """Give ``target`` a parent category matching ``parent``, by name.
    Returns (row, action, changed) like `upsert_category`."""
    dest = _find(target.parent_categories, parent.name)
    if dest is None:
        dest = ParentCategory(branch=target, name=parent.name)
        for field in PARENT_FIELDS:
            setattr(dest, field, getattr(parent, field))
        dest.save()
        return dest, "created", []

    if not update_existing:
        return dest, "skipped", []

    changed = _diff(dest, parent, PARENT_FIELDS)
    if changed:
        for field in changed:
            setattr(dest, field, getattr(parent, field))
        dest.save(update_fields=changed)
        return dest, "updated", changed
    return dest, "skipped", []


def _file_under(dest_cat, dest_parent, *, overwrite):
    """File ``dest_cat`` under ``dest_parent``.  Without ``overwrite`` only an
    ungrouped category is filed — one the target branch has already put in a
    group of its own stays there.  Returns True if it moved."""
    if dest_parent is None or dest_cat.parent_category_id == dest_parent.pk:
        return False
    if dest_cat.parent_category_id is not None and not overwrite:
        return False
    dest_cat.parent_category = dest_parent
    dest_cat.save(update_fields=["parent_category"])
    return True


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
    dest = find_product(target.products, [prod.barcode], [prod.name])
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

    Matched by Shopster ID then name, the way `upsert_product` matches, so this retires
    the same row an earlier sync would have created — no more.

    Only rows already on sale are touched, and nothing is ever created: a
    product the target never had is not this sync's business. Reversible from
    the target branch's own Removed view, like any other removal.
    """
    retired = []
    for prod in removed:
        dest = find_product(target.products.filter(active=True),
                            [prod.barcode], [prod.name])
        if dest is None:
            continue
        dest.active = False
        dest.save(update_fields=["active"])
        retired.append(dest.name)
    return retired


def copy_catalogue(source, target, *, update_existing=False, copy_stock=False,
                   catalogue=None, removed=None, retire=False, mirror=False):
    """Copy ``source``'s catalogue onto ``target``.  Returns a per-item report.

    ``mirror`` is the Sync products page: every source product is added or
    edited to match through `push_product` (Shopster ID, then name), exactly
    as "Sync to all branches" does for one product.  Without it this is the
    older ``manage.py sync_products`` behaviour, kept for that command.

    ``catalogue`` is the ``(cats, prods)`` pair from `source_catalogue`, passed
    in when several targets are being done in one go so the source is read once
    rather than once per branch.

    The caller wraps this in a transaction: a half-copied catalogue is worse
    than none, because nothing on screen tells you where it stopped.
    """
    cats, prods = catalogue if catalogue is not None else source_catalogue(source)
    report = {"branch": target, "categories": [], "products": []}

    # Parents first, so each category can be filed under the target's copy of
    # its parent.  Only the parents some travelling category sits under go —
    # an empty group is not part of the menu.
    parent_map = {}
    for cat in cats:
        parent = cat.parent_category
        if parent is not None and parent.id not in parent_map:
            parent_map[parent.id], _, _ = upsert_parent(
                target, parent, update_existing=update_existing,
            )

    cat_map = {}
    for cat in cats:
        dest, action, changed = upsert_category(
            target, cat, update_existing=update_existing,
        )
        if _file_under(dest, parent_map.get(cat.parent_category_id),
                       overwrite=update_existing or action == "created"):
            changed = [*changed, "parent_category"]
            if action == "skipped":
                action = "updated"
        cat_map[cat.id] = dest
        report["categories"].append(
            {"name": cat.name, "action": action, "changed": changed},
        )

    for prod in prods:
        if mirror:
            action, changes = push_product(target, prod)
            report["products"].append({
                "name": prod.name,
                "action": {"create": "created", "update": "updated",
                           "same": "skipped"}.get(action, action),
                "changed": [f for f, _, _ in changes],
            })
            continue
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
    """What a sync would do to ``target``, writing nothing.

    Names the products and, for an edit, what changes on each — "18 products
    will be updated" is not something an admin can check, and this page writes
    to branches nobody is looking at.  Built from `plan_product`, the same
    function the sync acts on.
    """
    cats, prods = catalogue if catalogue is not None else source_catalogue(source)
    have_cats = {
        n.strip().lower()
        for n in target.categories.values_list("name", flat=True) if n
    }
    have_parents = {
        n.strip().lower()
        for n in target.parent_categories.values_list("name", flat=True) if n
    }
    parents = {c.parent_category.name for c in cats if c.parent_category_id}

    adding, updating, conflicts, same = [], [], [], 0
    for prod in prods:
        dest, action, changes = plan_product(target, prod)
        if action == "create":
            adding.append(prod.name)
        elif action == "update":
            updating.append({"name": prod.name, "changes": describe_changes(changes)})
        elif action == "conflict":
            conflicts.append(prod.name)
        else:
            same += 1

    on_sale = target.products.filter(active=True)
    retiring = [
        p.name for p in (removed or [])
        if find_product(on_sale, [p.barcode], [p.name]) is not None
    ]

    return {
        "branch": target,
        "adding": adding,
        "updating": updating,
        "conflicts": conflicts,
        "retiring": retiring,
        "already": same,
        "categories_adding": [
            c.name for c in cats if (c.name or "").strip().lower() not in have_cats
        ],
        "parents_adding": sorted(
            n for n in parents if (n or "").strip().lower() not in have_parents
        ),
        "product_total": target.products.count(),
    }


# ── One product, every branch ───────────────────────────────────────────────
# What an *edit* carries to the other branches' copies: everything on the
# product form except the two numbers that describe one shop's shelves.  Stock
# is what a cashier is counting right now, and a par level is tuned to how fast
# one shop sells — neither means anything at another branch.  Category and unit
# travel too, but as foreign keys they are resolved under each target rather
# than copied, so they are handled beside this list, not in it.  On-sale status
# is not here either: removing a product is its own button, not a save, and
# `fanout_active` is what carries it.
FANOUT_FIELDS = (
    "name", "barcode", "price", "cost", "name_th", "sku", "tax_type",
    "is_favorite", "image_url", "image_base64", "shelf_life",
)

# How a change reads in the Sync page's preview.  Photos are compared but not
# printed: a base64 image is not something to show in a table cell.
FIELD_LABELS = {
    "name": "name", "barcode": "Shopster ID", "price": "price", "cost": "cost",
    "name_th": "description", "sku": "SKU", "tax_type": "tax",
    "is_favorite": "favourite", "image_url": "photo", "image_base64": "photo",
    "shelf_life": "shelf life", "category": "category", "unit": "unit",
    "sale_windows": "sale dates",
}
_QUIET_FIELDS = {"image_url", "image_base64", "sale_windows"}


def _windows(product):
    return sorted(
        ((w.launch_date, w.discontinue_date) for w in product.sale_windows.all()),
        key=lambda pair: (str(pair[0]), str(pair[1])),
    )


def _label(row, attr):
    obj = getattr(row, attr) if getattr(row, f"{attr}_id") else None
    return obj.name if obj is not None else ""


def plan_product(target, product, *, previous_barcode=None, previous_name=None):
    """What pushing ``product`` to ``target`` would do — writing nothing.

    Returns ``(dest, action, changes)``.  ``action`` is ``"create"``,
    ``"update"``, ``"same"``, ``"skip"`` (an off-sale product the target never
    had: it is never born dead somewhere else) or ``"conflict"`` (the target
    already sells a *different* product under this Shopster ID).  ``changes``
    is a list of ``(field, before, after)``.

    `push_product` acts on exactly this, so the Sync page's preview and the
    sync itself cannot disagree.
    """
    dest = find_product(
        target.products,
        [previous_barcode, product.barcode], [previous_name, product.name],
    )
    if dest is None:
        if not product.active:
            return None, "skip", []
        return None, "create", []

    changes = [(f, getattr(dest, f), getattr(product, f))
               for f in FANOUT_FIELDS if getattr(dest, f) != getattr(product, f)]
    for attr in ("category", "unit"):
        before, after = _label(dest, attr), _label(product, attr)
        if before.strip().lower() != after.strip().lower():
            changes.append((attr, before, after))
    if _windows(dest) != _windows(product):
        changes.append(("sale_windows", "", ""))

    if any(f == "barcode" for f, _, _ in changes) and dest.active and barcode_taken(
            target, product.barcode, exclude_pk=dest.pk):
        return dest, "conflict", changes
    return dest, ("update" if changes else "same"), changes


def describe_changes(changes):
    """``[(field, before, after)]`` as short phrases for the preview."""
    out, seen = [], set()
    for field, before, after in changes:
        label = FIELD_LABELS.get(field, field)
        if label in seen:
            continue
        seen.add(label)
        if field in _QUIET_FIELDS:
            out.append(f"{label} changed")
        else:
            out.append(f"{label} {before if before not in (None, '') else '—'} → "
                       f"{after if after not in (None, '') else '—'}")
    return out


def push_product(target, product, *, previous_barcode=None, previous_name=None):
    """Make ``target``'s copy of ``product`` match it.  Returns ``(action, changes)``.

    Creates the copy if the target has none, otherwise overwrites
    `FANOUT_FIELDS`, category, unit and sale dates — never stock, par level or
    the counter minimum, which are each shop's own.  A ``"conflict"`` writes
    nothing.  The caller owns the transaction.
    """
    dest, action, changes = plan_product(
        target, product,
        previous_barcode=previous_barcode, previous_name=previous_name,
    )
    if action in ("skip", "conflict"):
        return action, changes

    windows = _windows(product)
    category = None
    if product.category_id:
        category, _, _ = upsert_category(
            target, product.category, update_existing=False,
        )

    if action == "create":
        dest, _, _ = upsert_product(target, product, category, update_existing=False)
        sale_windows.replace(dest, windows)
        sale_windows.apply(dest)
        return action, changes

    if action == "same":
        return action, changes

    unit = unit_for(target, product.unit)
    fields = [f for f, _, _ in changes if f in FANOUT_FIELDS]
    for field in fields:
        setattr(dest, field, getattr(product, field))
    if dest.category_id != (category.pk if category else None):
        dest.category = category
        fields.append("category")
    if dest.unit_id != (unit.pk if unit else None):
        dest.unit = unit
        fields.append("unit")
    if fields:
        # update_fields, so this cannot clobber a column it was not asked to
        # touch — stock above all, which a cashier may be moving now.
        dest.save(update_fields=fields)
    sale_windows.replace(dest, windows)
    sale_windows.apply(dest)
    return action, changes


def fanout_product(product, *, previous_name=None, previous_barcode=None,
                   branches=None):
    """Push one product out to every other active branch.  Returns a report.

    The product form's and Quick edit's "Sync to all branches".  Each branch's
    copy is found by Shopster ID, then by name — the values from *before* this
    save first, so a rename, a new ID, or both at once land on the copies that
    already exist instead of adding a second row beside them.  See
    `push_product` for what is written.

    The caller wraps this and the product's own save in one transaction,
    because a fan-out that half ran is worse than none.
    """
    report = {"created": [], "updated": [], "unchanged": [], "conflicts": []}
    targets = other_branches(product.branch) if branches is None else branches
    for target in targets:
        action, _ = push_product(
            target, product,
            previous_barcode=previous_barcode, previous_name=previous_name,
        )
        if action == "create":
            report["created"].append(target.name)
        elif action == "update":
            report["updated"].append(target.name)
        elif action == "conflict":
            report["conflicts"].append(target.name)
        elif action == "same":
            report["unchanged"].append(target.name)
    return report


def fanout_parent(parent, *, previous_name=None, branches=None):
    """The parent category form's "Sync to all branches".  Returns a report.

    At every other active branch: the parent with this name is created, or
    renamed and updated to match (``previous_name`` finds it through a rename,
    as in `fanout_product`).  Then each category filed under ``parent`` here is
    looked up by name at that branch and filed under its copy.  A category the
    branch does not have is not created — that is the Sync products page's job
    — and categories the branch has under this parent but this branch does not
    are left where they are: this only adds to a group, never empties one.

    The caller wraps this and the parent's own save in one transaction.
    """
    report = {"created": [], "updated": [], "unchanged": [], "filed": 0}
    targets = other_branches(parent.branch) if branches is None else branches
    children = list(parent.categories.values_list("name", flat=True))

    for target in targets:
        dest = None
        for name in (previous_name, parent.name):
            dest = _find(target.parent_categories, name)
            if dest is not None:
                break

        if dest is None:
            dest, _, _ = upsert_parent(target, parent, update_existing=False)
            created, changed = True, []
        else:
            created = False
            changed = _diff(dest, parent, ("name", *PARENT_FIELDS))
            for field in changed:
                setattr(dest, field, getattr(parent, field))
            if changed:
                dest.save(update_fields=changed)

        filed = 0
        for name in children:
            cat = _find(target.categories, name)
            if cat is not None and _file_under(cat, dest, overwrite=True):
                filed += 1
        report["filed"] += filed

        if created:
            report["created"].append(target.name)
        elif changed or filed:
            report["updated"].append(target.name)
        else:
            report["unchanged"].append(target.name)

    return report


def fanout_category(category, *, previous_name=None, branches=None):
    """The category form's "Sync to all branches".  Returns a report.

    At every other active branch the category with this name is created, or
    renamed and updated to match — ``previous_name`` finds it through a
    rename, as in `fanout_product`.  Its parent follows too: filed under that
    branch's parent of the same name (created there if missing, as
    `fanout_parent` would), or ungrouped if it is ungrouped here.  Products are
    not touched; they keep pointing at the same category row, which is now
    renamed or recoloured in place.

    The caller wraps this and the category's own save in one transaction.
    """
    report = {"created": [], "updated": [], "unchanged": []}
    targets = other_branches(category.branch) if branches is None else branches
    parent = category.parent_category

    for target in targets:
        dest = None
        for name in (previous_name, category.name):
            dest = _find(target.categories, name)
            if dest is not None:
                break

        if dest is None:
            dest, _, _ = upsert_category(target, category, update_existing=False)
            created, changed = True, []
        else:
            created = False
            changed = _diff(dest, category, ("name", *CATEGORY_FIELDS))
            for field in changed:
                setattr(dest, field, getattr(category, field))
            if changed:
                dest.save(update_fields=changed)

        dest_parent = None
        if parent is not None:
            dest_parent, _, _ = upsert_parent(target, parent, update_existing=False)
        if dest.parent_category_id != (dest_parent.pk if dest_parent else None):
            dest.parent_category = dest_parent
            dest.save(update_fields=["parent_category"])
            changed.append("parent_category")

        if created:
            report["created"].append(target.name)
        elif changed:
            report["updated"].append(target.name)
        else:
            report["unchanged"].append(target.name)

    return report


def fanout_active(product, *, branches=None):
    """Carry ``product``'s on-sale status to the other branches' copies.

    The Remove and Restore buttons' "at all branches too" box.  Only copies
    that exist are touched: removing never needs to create anything, and a
    restore that created rows would be the Sync page's job done by the wrong
    button.  Matched by Shopster ID then name, the way every other sync here
    matches.  A restore is skipped (``conflicts``) at a branch already selling
    a different product under the same ID.
    """
    report = {"updated": [], "unchanged": [], "conflicts": []}
    targets = other_branches(product.branch) if branches is None else branches
    for target in targets:
        dest = find_product(target.products, [product.barcode], [product.name])
        if dest is None:
            continue
        if dest.active == product.active:
            report["unchanged"].append(target.name)
            continue
        if product.active and barcode_taken(target, dest.barcode, exclude_pk=dest.pk):
            report["conflicts"].append(target.name)
            continue
        if product.active:
            # Restored the way the original was, so the copy's own dates do
            # not take it straight back off sale.
            sale_windows.restore(dest)
        else:
            dest.active = False
            dest.removed_by_schedule = False
        dest.save(update_fields=["active", "removed_by_schedule"])
        report["updated"].append(target.name)
    return report
