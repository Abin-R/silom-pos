"""Take products on and off sale by their launch and discontinue dates.

Idempotent — run it as often as you like; it only touches products whose
dates disagree with their on-sale state today (Bangkok's date).  Run hourly
from the server's crontab, so a product is moved within the hour of midnight
and a missed run is made up by the next one:

    5 * * * *  cd /home/azureuser/silom-pos/backend_django && <venv>/python manage.py apply_sale_windows

    python manage.py apply_sale_windows             # apply
    python manage.py apply_sale_windows --dry-run   # say what would change
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from bravepos import sale_windows


class Command(BaseCommand):
    help = "Move products to/from Removed according to their sale dates."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Report the changes and roll them back.")

    def handle(self, *args, dry_run=False, **options):
        day = sale_windows.today()
        with transaction.atomic():
            changes = sale_windows.apply_all(day)
            for product, change in changes:
                where = product.branch.name if product.branch else "no branch"
                self.stdout.write(f"{day} {change}: {product.name} ({where})")
            if dry_run:
                transaction.set_rollback(True)
        self.stdout.write(
            f"{day}: {len(changes)} product(s) "
            f"{'would change' if dry_run else 'changed'}.")
