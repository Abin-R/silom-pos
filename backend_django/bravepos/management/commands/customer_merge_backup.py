"""Inspect, restore from, or delete the customer merge's undo tape.

Migration 0045 folds customers sharing a phone number into one row and deletes
the others.  Before it touches anything it writes a ``CustomerMergeBackup`` —
every customer row as it stood, plus the previous owner of every bill and held
cart it moves.  This command is what that tape is for:

    manage.py customer_merge_backup             # what is stored, and when
    manage.py customer_merge_backup --show      # the groups it merged
    manage.py customer_merge_backup --restore   # put the book back
    manage.py customer_merge_backup --delete    # once you are satisfied

Restoring recreates the deleted rows, undoes the fields filled in on the
survivors, and returns every moved bill to its previous owner.  Customers
registered *since* the merge are left alone — a new customer is not a mistake
to undo.  It does not re-run the merge afterwards, so the duplicates come back
exactly as they were.

Which is what migration 0047 forbids: one phone number, one customer.  A book
with that applied refuses the duplicates a restore brings back, so
``manage.py migrate bravepos 0046`` comes first — --restore says so itself
rather than failing with an IntegrityError.

Deleting is the last step, not the first.  Once the tape is gone the merge is
permanent, so leave it until the shop has been trading on the merged book long
enough that anything wrong would have surfaced.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from bravepos.customers import restore_snapshot
from bravepos.models import Customer, CustomerMergeBackup, Order, ParkedOrder


class Command(BaseCommand):
    help = "Inspect, restore from, or delete the customer merge backup."

    def add_arguments(self, parser):
        parser.add_argument(
            '--id', type=int, default=None,
            help='Which backup to act on. Defaults to the most recent.',
        )
        parser.add_argument(
            '--show', action='store_true',
            help='List the customers the backup holds.',
        )
        parser.add_argument(
            '--restore', action='store_true',
            help='Put the customer book back as the backup found it.',
        )
        parser.add_argument(
            '--delete', action='store_true',
            help='Delete the backup. The merge becomes permanent.',
        )

    def handle(self, *args, **options):
        backups = CustomerMergeBackup.objects.all()
        if not backups.exists():
            self.stdout.write(self.style.WARNING(
                "No customer merge backup stored. Either the merge has not run "
                "yet, or the backup has already been deleted."))
            return

        for b in backups:
            payload = b.payload or {}
            self.stdout.write(
                f"#{b.id}  {b.created_at:%Y-%m-%d %H:%M}  "
                f"{len(payload.get('customers') or [])} customers, "
                f"{len(payload.get('moved_orders') or {})} bills moved, "
                f"{len(payload.get('moved_parked') or {})} held carts  "
                f"{b.note}")

        if not (options['show'] or options['restore'] or options['delete']):
            self.stdout.write(self.style.NOTICE(
                "\nNothing changed. --show to see it, --restore to use it, "
                "--delete when you are satisfied the merge was right."))
            return

        backup = (CustomerMergeBackup.objects.filter(id=options['id']).first()
                  if options['id'] else backups.first())
        if backup is None:
            raise CommandError(f"No backup #{options['id']}.")

        if options['show']:
            rows = (backup.payload or {}).get("customers") or []
            live = set(Customer.objects.values_list("id", flat=True))
            self.stdout.write(f"\nBackup #{backup.id} holds {len(rows)} customers:")
            for row in sorted(rows, key=lambda r: (r.get("name") or "")):
                gone = "" if _uuid_in(row["id"], live) else "  <- deleted by the merge"
                self.stdout.write(
                    f"  {row.get('name')!r} {row.get('phone')!r}{gone}")

        if options['restore']:
            try:
                with transaction.atomic():
                    result = restore_snapshot(
                        Customer, Order, ParkedOrder, backup)
            except IntegrityError as exc:
                if 'phone' not in str(exc):
                    raise
                # Bringing back rows that share a number is the whole point of
                # the restore, and exactly what 0047 refuses.  Nothing was
                # written — the atomic block rolled back — so say what to do
                # instead of leaving a half-restored book.
                raise CommandError(
                    "The book holds one phone number to one customer "
                    "(migration 0047), and this backup contains rows that "
                    "share a number — which is what the merge it undoes was "
                    "for.\n\n"
                    "Drop the constraint first, then restore:\n"
                    "    manage.py migrate bravepos 0046\n"
                    "    manage.py customer_merge_backup --restore\n\n"
                    "Nothing was changed. Re-applying 0047 later re-runs the "
                    "merge, so the duplicates would fold again."
                ) from exc
            self.stdout.write(self.style.SUCCESS(
                f"\nRestored from backup #{backup.id}: "
                f"{result['customers']} customer rows written "
                f"({result['recreated']} of them recreated), "
                f"{result['orders']} bills and {result['parked']} held carts "
                f"returned to their previous owner."))
            self.stdout.write(
                "The duplicates are back exactly as they were. The backup is "
                "kept — restoring does not consume it.")

        if options['delete']:
            backup_id = backup.id
            backup.delete()
            self.stdout.write(self.style.SUCCESS(
                f"Deleted backup #{backup_id}. The merge is now permanent."))


def _uuid_in(value, live) -> bool:
    """Snapshot ids are strings; live ids are UUIDs."""
    return any(str(x) == str(value) for x in live)
