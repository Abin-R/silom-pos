"""Downscale product images that were stored before the write-time cap existed.

``bravepos.images.normalize`` now bounds every image on the way in, but rows
written before that are still whatever the client happened to send — including
the 1917x998 PNG that made the customer self-order menu 648 KB. This is the
one-off backfill for those, and the thing to re-run if an image ever gets in
by some path we haven't thought of.

    python manage.py normalize_product_images --dry-run
    python manage.py normalize_product_images

Idempotent: an image already within the cap is left byte-for-byte alone, so
running it twice does not recompress anything (JPEG generation loss is
cumulative and there is no undo).
"""
from __future__ import annotations

from bravepos import images
from bravepos.models import Product
from django.core.management.base import BaseCommand


def _kb(n: int) -> str:
    return f'{n / 1024:.1f} KB'


class Command(BaseCommand):
    help = "Downscale oversized base64 product images already in the database."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change without writing anything.',
        )
        parser.add_argument(
            '--branch', default='',
            help='Limit to one branch id.',
        )

    def handle(self, *args, **opts):
        dry = opts['dry_run']
        qs = Product.objects.all()
        if opts['branch']:
            qs = qs.filter(branch_id=opts['branch'])

        changed = before_total = after_total = dropped = 0

        for p in qs.iterator():
            fields = {}
            for field in ('image_url', 'image_base64'):
                old = getattr(p, field) or ''
                if not images.is_data_uri(old):
                    continue
                new = images.normalize(old)
                if new != old:
                    fields[field] = (old, new)

            if not fields:
                continue

            changed += 1
            for field, (old, new) in fields.items():
                before_total += len(old)
                after_total += len(new)
                if not new:
                    dropped += 1
                    note = self.style.WARNING('undecodable → cleared')
                else:
                    note = f'{_kb(len(old))} → {_kb(len(new))}'
                self.stdout.write(f'  {p.name[:34]:<34} {field:<13} {note}')
                if not dry:
                    setattr(p, field, new)

            if not dry:
                # update_fields so a backfill can't clobber a concurrent edit to
                # price or stock made from the POS while this is running.
                p.save(update_fields=list(fields))

        verb = 'would shrink' if dry else 'shrank'
        saved = before_total - after_total
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {changed} product(s): {_kb(before_total)} → {_kb(after_total)} '
            f'(saved {_kb(saved)}); {dropped} cleared as undecodable'
        ))
