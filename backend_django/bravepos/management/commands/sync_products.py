"""Copy one branch's catalogue — categories and products — onto other branches.

A new branch opens with an empty product list, and retyping forty-odd cakes
into it by hand is both slow and a guaranteed source of price drift. This
copies a branch that is already set up correctly onto the ones that are not:

    python manage.py sync_products --from "BIO HOUSE" --to Silom --dry-run
    python manage.py sync_products --from "BIO HOUSE" --to Silom --to Vibhavadi

Matching is by name, case-insensitively, so it is safe to re-run: a second
pass updates the rows the first pass created instead of duplicating them.
That also makes it the way to push a price change out to every branch.

Two things are deliberately *not* copied:

* **Stock.** On-hand quantity is a fact about one shop's shelves — BIO HOUSE
  sits at -160 Choco Gems because deliveries were never recorded, and that
  number is meaningless anywhere else. New products start at 0; existing rows
  keep whatever the target branch already counted. ``--copy-stock`` overrides
  this for the case where you really are mirroring a stock take.
* **Orders, movements, customers.** Only the menu travels.

Units need no copying: they live at ``branch=None`` and are shared shop-wide.
A branch-scoped unit (there are none today, but the column allows it) is
recreated under the target so the copy never points at another branch's row.

``--dry-run`` does every write for real and then rolls the transaction back,
so what it reports is what the live run does — not a second implementation of
it that can drift.
"""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from bravepos.models import Branch, Category, Product, Unit

# Copied verbatim from source to target. Everything absent from this list is
# either identity (id, branch), a foreign key resolved per-branch below
# (category, unit), or deliberately branch-local (stock — see module docstring).
PRODUCT_FIELDS = (
    "name_th", "price", "cost", "par_level", "sku", "barcode",
    "image_url", "image_base64", "is_favorite", "tax_type", "product_type",
    "active", "sort_order",
)

CATEGORY_FIELDS = ("name_th", "color", "order", "source", "active")


def _resolve_branch(ref: str) -> Branch:
    """Look a branch up by exact name, then by id — whichever the caller gave."""
    try:
        return Branch.objects.get(name=ref)
    except Branch.DoesNotExist:
        pass
    try:
        return Branch.objects.get(id=ref)
    # ValidationError: a non-UUID string reaching a UUIDField lookup. Any of
    # these just means "that is not a branch" — say so, don't traceback.
    except (Branch.DoesNotExist, ValidationError, ValueError, TypeError):
        raise CommandError(
            f"no branch named or numbered {ref!r}. Known branches: "
            + ", ".join(repr(b.name) for b in Branch.objects.order_by("name"))
        )


class Command(BaseCommand):
    help = "Copy a branch's categories and products onto one or more other branches."

    def add_arguments(self, parser):
        parser.add_argument(
            "--from", dest="source", required=True,
            help="Branch to copy from, by name or id.",
        )
        parser.add_argument(
            "--to", dest="targets", action="append", default=[],
            help="Branch to copy onto, by name or id. Repeat for several.",
        )
        parser.add_argument(
            "--all-empty", action="store_true",
            help="Also copy onto every active branch that currently has no "
                 "products. Skips branches with a catalogue of their own, so "
                 "it cannot quietly overwrite one.",
        )
        parser.add_argument(
            "--copy-stock", action="store_true",
            help="Also copy on-hand stock. Off by default: quantities belong "
                 "to one shop's shelves, not to the menu.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change, then roll it all back.",
        )

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        source = _resolve_branch(opts["source"])
        targets = self._targets(source, opts)

        cats = list(source.categories.all())
        prods = list(source.products.select_related("category", "unit").all())
        self.stdout.write(
            f"source {source.name!r}: {len(cats)} categories, {len(prods)} products"
        )

        totals = Counts()
        for target in targets:
            totals += self._sync_one(source, target, cats, prods, opts["copy_stock"], dry)

        verb, also = ("would create", "would update") if dry else ("created", "updated")
        self.stdout.write(self.style.SUCCESS(
            f"{len(targets)} branch(es): {verb} {totals.cat_new} categories and "
            f"{totals.prod_new} products; {also} {totals.cat_upd} categories and "
            f"{totals.prod_upd} products"
        ))
        if dry:
            self.stdout.write(self.style.WARNING("dry run — everything was rolled back"))

    def _targets(self, source, opts):
        targets = [_resolve_branch(t) for t in opts["targets"]]
        seen = {t.id for t in targets}
        if opts["all_empty"]:
            for branch in Branch.objects.filter(active=True).order_by("name"):
                if branch.id not in seen and not branch.products.exists():
                    targets.append(branch)
                    seen.add(branch.id)

        targets = [t for t in targets if t.id != source.id]
        if not targets:
            raise CommandError("no target branches — pass --to and/or --all-empty")
        return targets

    # ── one target ──────────────────────────────────────────────────────────
    def _sync_one(self, source, target, cats, prods, copy_stock, dry):
        counts = Counts()
        self.stdout.write(f"\n→ {target.name}")

        # atomic per target: a failure halfway through leaves the branch with
        # the catalogue it started with, not half of someone else's. It is
        # also what makes --dry-run truthful — same writes, then a rollback.
        with transaction.atomic():
            cat_map = {}
            for cat in cats:
                dest, created, changed = self._upsert_category(target, cat)
                cat_map[cat.id] = dest
                if created:
                    counts.cat_new += 1
                    self.stdout.write(f"    + category {cat.name!r}")
                elif changed:
                    counts.cat_upd += 1
                    self.stdout.write(f"    ~ category {cat.name!r} ({', '.join(changed)})")

            for prod in prods:
                created, changed = self._upsert_product(target, prod, cat_map, copy_stock)
                if created:
                    counts.prod_new += 1
                    self.stdout.write(f"    + {prod.name}")
                elif changed:
                    counts.prod_upd += 1
                    self.stdout.write(f"    ~ {prod.name} ({', '.join(changed)})")

            if dry:
                transaction.set_rollback(True)

        self.stdout.write(
            f"    {counts.cat_new} new / {counts.cat_upd} updated categories, "
            f"{counts.prod_new} new / {counts.prod_upd} updated products"
        )
        return counts

    def _upsert_category(self, target, cat):
        dest = target.categories.filter(name__iexact=cat.name).first()
        if dest is None:
            dest = Category(branch=target, name=cat.name)
            for field in CATEGORY_FIELDS:
                setattr(dest, field, getattr(cat, field))
            dest.save()
            return dest, True, []

        changed = [f for f in CATEGORY_FIELDS if getattr(dest, f) != getattr(cat, f)]
        if changed:
            for field in changed:
                setattr(dest, field, getattr(cat, field))
            dest.save(update_fields=changed)
        return dest, False, changed

    def _upsert_product(self, target, prod, cat_map, copy_stock):
        category = cat_map.get(prod.category_id) if prod.category_id else None
        unit = self._unit_for(target, prod.unit)

        dest = target.products.filter(name__iexact=prod.name).first()
        if dest is None:
            dest = Product(branch=target, name=prod.name)
            for field in PRODUCT_FIELDS:
                setattr(dest, field, getattr(prod, field))
            dest.category = category
            dest.unit = unit
            dest.stock = prod.stock if copy_stock else 0
            dest.save()
            return True, []

        fields = list(PRODUCT_FIELDS) + (["stock"] if copy_stock else [])
        changed = [f for f in fields if getattr(dest, f) != getattr(prod, f)]
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
            # update_fields so a sync can't clobber a column it wasn't asked
            # to touch — notably stock, edited from the POS as this runs.
            dest.save(update_fields=changed)
        return False, changed

    def _unit_for(self, target, unit):
        """Reuse a shop-wide unit; recreate a branch-scoped one under target."""
        if unit is None or unit.branch_id is None:
            return unit
        dest = target.units.filter(name__iexact=unit.name).first()
        if dest is None:
            dest = Unit(
                branch=target, name=unit.name, order=unit.order, active=unit.active,
            )
            dest.save()
        return dest


class Counts:
    """Per-branch tallies, summed across branches for the closing line."""

    __slots__ = ("cat_new", "cat_upd", "prod_new", "prod_upd")

    def __init__(self):
        self.cat_new = self.cat_upd = self.prod_new = self.prod_upd = 0

    def __iadd__(self, other):
        for field in self.__slots__:
            setattr(self, field, getattr(self, field) + getattr(other, field))
        return self
