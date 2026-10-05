"""Set Product.shelf_life (days from MFG) from the shop's FEFO sheet.

Applies to every branch: products are matched by name, ignoring case and
repeated spaces. Dry run by default; pass --apply to write.

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


def _key(name):
    return re.sub(r"\s+", " ", name or "").strip().lower()


class Command(BaseCommand):
    help = "Set product shelf life (days) from the FEFO sheet, across all branches."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write the changes. Without it, only report.")

    def handle(self, *args, **options):
        wanted = {_key(k): v for k, v in SHELF_LIFE.items()}
        matched_keys = set()
        changes = []
        for p in Product.objects.select_related("branch").order_by("name"):
            k = _key(p.name)
            if k not in wanted:
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
