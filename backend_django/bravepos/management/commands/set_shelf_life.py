"""Set Product.shelf_life (days from MFG) from the shop's FEFO sheet.

Applies to every branch: products are matched by name (or by ALIASES, for
till names that differ from the sheet), ignoring case and repeated spaces. Dry run by default; pass --apply to write.

    python manage.py set_shelf_life
    python manage.py set_shelf_life --apply
"""
from __future__ import annotations

import re

from django.core.management.base import BaseCommand
from django.db import transaction

from bravepos.models import Product

# "Shelf Life (Days) MFG" column of the FEFO control sheet.
SHELF_LIFE = {
    "Choco gems Pop Original": 30,
    "Choco gems Pop Mommy": 30,
    "Choco gems Pop Family": 30,
    "Raspberry Mousses Pop": 7,
    "Pistachio Mousses Pop": 7,
    "Mango Mousses Pop": 7,
    "Strawberry Mousses Pop": 7,
    "Cherry Mousses Pop": 7,
    "Large Cookies Breakfast Confitti": 6,
    "Large Cookies Redvelved": 6,
    "Large Cookies Pink Birthday": 6,
    "Large Cookies Oreo Confetti": 6,
    "Large Cookies Biscoff": 9,
    "Large Cookies Hella Nutella": 9,
    "Small Cookies Marching Ladies": 9,
    "Small Cookies Mama OG": 9,
    "Cookies Cake Breakfast Confitti": 6,
    "Cookies Cake Pink Birthday": 6,
    "Cookies Cake Biscoff": 9,
    "Mini Dot Cake": 6,
    "Mini Lily Princess Cake": 7,
    "Mix Brownie": 9,
    "Dubai Chewy Cookies": 9,
}

# The till names products differently from the sheet: till name -> sheet name.
ALIASES = {
    "Choco Gems pop": "Choco gems Pop Original",
    "Chocogems Mommy Edition": "Choco gems Pop Mommy",
    "Family Edition - Choco gems pop": "Choco gems Pop Family",
    "Raspberry Mousse cake": "Raspberry Mousses Pop",
    "Pistachio Chocolate Mousse cake": "Pistachio Mousses Pop",
    "Mango Sticky Rice Mousse cake": "Mango Mousses Pop",
    "Strawberry Mousse cake": "Strawberry Mousses Pop",
    "Cherry Mousse Pop เชอร์รี่มูสสุดป๊อบ จาก The Rolling Pinn": "Cherry Mousses Pop",
    "Cherry Mousse cake": "Cherry Mousses Pop",
    "Breakfash Confetti Cookie": "Large Cookies Breakfast Confitti",
    "Red Velvet Cookie": "Large Cookies Redvelved",
    "Pink Birthday Cookies": "Large Cookies Pink Birthday",
    "Oreo confetti": "Large Cookies Oreo Confetti",
    "\u0e3aBiscoff Mochi": "Large Cookies Biscoff",
    "Hella Nutella Cookie": "Large Cookies Hella Nutella",
    "The Marching Ladies Cookie": "Small Cookies Marching Ladies",
    "Mama OG": "Small Cookies Mama OG",
    "Mama OG Dark Chocolate Walnut Cookie": "Small Cookies Mama OG",
    "Breakfast confetti birthday cookie cake": "Cookies Cake Breakfast Confitti",
    "Pink Birthday Cookie cake 1 lb": "Cookies Cake Pink Birthday",
    "Pink Birthday Cookie Cake (1lb)": "Cookies Cake Pink Birthday",
    "1lb Biscoff Mochi cookie cake": "Cookies Cake Biscoff",
    "Dot Birthday Cake": "Mini Dot Cake",
    "Mini Lily Princess Cake 0.4lb": "Mini Lily Princess Cake",
    "Assorted Brownies Bites": "Mix Brownie",
}


def _key(name):
    return re.sub(r"\s+", " ", name or "").strip().lower()


class Command(BaseCommand):
    help = "Set product shelf life (days) from the FEFO sheet, across all branches."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write the changes. Without it, only report.")

    def handle(self, *args, **options):
        wanted = {_key(k): v for k, v in SHELF_LIFE.items()}
        sheet_name = {_key(k): _key(k) for k in SHELF_LIFE}
        sheet_name.update({_key(a): _key(s) for a, s in ALIASES.items()})
        matched_keys = set()
        changes = []
        for p in Product.objects.select_related("branch").order_by("name"):
            k = sheet_name.get(_key(p.name))
            if k is None:
                continue
            matched_keys.add(k)
            if p.shelf_life != wanted[k]:
                changes.append((p, wanted[k]))

        for p, days in changes:
            branch = p.branch.name if p.branch else "-"
            flag = "" if p.active else "  (inactive)"
            self.stdout.write("%-40s %-20s %s -> %s%s" % (
                p.name, branch, p.shelf_life, days, flag))

        missing = [n for n in SHELF_LIFE if _key(n) not in matched_keys]
        for n in missing:
            self.stdout.write(self.style.WARNING("No product named %r" % n))

        if not options["apply"]:
            self.stdout.write("Dry run: %d product(s) would change. Re-run with --apply."
                              % len(changes))
            return

        with transaction.atomic():
            for p, days in changes:
                p.shelf_life = days
                p.save(update_fields=["shelf_life"])
        self.stdout.write(self.style.SUCCESS("Updated %d product(s)." % len(changes)))
