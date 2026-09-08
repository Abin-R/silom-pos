"""Show — or apply — the customer merge that migration 0045 performs.

Migration 0045 folds customers sharing a phone number into one row, and it is
not reversible: the absorbed rows are deleted.  Everything it decides is
mechanical (see ``bravepos/customers.py``), but two judgements are only
answerable by someone who knows the shop:

  * a household, or staff registering under the shop's own number, is several
    real people behind one phone — the merge cannot tell them from one person
    who registered twice, and will make them one customer;
  * where both rows carry a *different* non-empty value for the same field,
    the survivor's wins and the other is gone.

So run this first, read the groups, and only then deploy:

    python manage.py merge_customers            # report, touches nothing
    python manage.py merge_customers --apply    # do it early, outside 0045

``--apply`` is not required: deploying runs 0045, which does the same work.
It exists so the merge can be done deliberately, at a chosen moment, with the
report in hand — rather than as a side effect of a deploy.  Either way an undo
tape is written first; see ``manage.py customer_merge_backup``.
"""
from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand

from bravepos.customers import (
    FILLABLE, merge_duplicates_by_phone, normalise_phone,
)
from bravepos.models import Customer, CustomerMergeBackup, Order, ParkedOrder


class Command(BaseCommand):
    help = "Report (or apply) the duplicate-customer merge from migration 0045."

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Actually merge. Without this the command only reports.',
        )

    def handle(self, *args, **options):
        groups = defaultdict(list)
        no_phone = 0
        for customer in Customer.objects.select_related('branch'):
            key = normalise_phone(customer.phone)
            if key:
                groups[key].append(customer)
            else:
                no_phone += 1

        dupes = {k: v for k, v in groups.items() if len(v) > 1}
        total = Customer.objects.count()

        self.stdout.write(f"{total} customers: {no_phone} with no phone "
                          f"(never merged), {len(groups)} distinct numbers.")
        if not dupes:
            self.stdout.write(self.style.SUCCESS(
                "No number is held by more than one customer. "
                "Migration 0045 will delete nothing."))
            return

        removed = sum(len(v) - 1 for v in dupes.values())
        self.stdout.write(self.style.WARNING(
            f"\n{len(dupes)} number(s) held by more than one customer. "
            f"{removed} row(s) would be deleted.\n"))

        counts = {c.id: c.orders.count() for v in dupes.values() for c in v}
        for key, rows in sorted(dupes.items()):
            rows = sorted(rows, key=lambda c: -counts[c.id])
            self.stdout.write(f"  {key}")
            for i, c in enumerate(rows):
                mark = "KEEP  " if i == 0 else "DELETE"
                where = c.branch.name if c.branch else "-"
                self.stdout.write(
                    f"    {mark} {c.name!r} {c.last_name!r} "
                    f"[{where}] {counts[c.id]} bill(s) phone={c.phone!r}")
            # What will not survive: where both rows carry a value, the
            # survivor's wins and the other is simply dropped.
            #
            # `name` is reported even though the merge never fills it — it is
            # never blank, so the absorbed row's name is always dropped, and
            # it is the most visible thing to lose. `phone` is skipped: the
            # two formats differing is the reason these rows are a group, not
            # a casualty of merging them. `branch` reads as a name, not a UUID.
            keep = rows[0]
            for c in rows[1:]:
                for f in ('name',) + tuple(FILLABLE):
                    if f == 'phone':
                        continue
                    mine, theirs = getattr(keep, f, None), getattr(c, f, None)
                    if mine in (None, "") or theirs in (None, "") or mine == theirs:
                        continue
                    if f == 'branch_id':
                        self.stdout.write(
                            f"      home branch: stays "
                            f"{keep.branch.name if keep.branch else '-'!s} "
                            f"(not {c.branch.name if c.branch else '-'!s})")
                    else:
                        self.stdout.write(self.style.WARNING(
                            f"      lost: {f}={theirs!r} (keeping {mine!r})"))

        self.stdout.write(
            f"\nBills are re-pointed, never deleted, and each receipt keeps the "
            f"name it printed.\nHeld carts follow the merge.")

        if not options['apply']:
            self.stdout.write(self.style.NOTICE(
                "\nReport only — nothing changed. Re-run with --apply to merge, "
                "or just deploy and let migration 0045 do it."))
            return

        result = merge_duplicates_by_phone(
            Customer, Order, ParkedOrder, CustomerMergeBackup,
            note="manage.py merge_customers --apply")
        self.stdout.write(self.style.SUCCESS(
            f"\nMerged {result['groups']} group(s), removed {result['removed']} "
            f"row(s). Migration 0045 will now find nothing left to do."))
        self.stdout.write(
            f"Undo tape saved as backup #{result['backup_id']}. Put it all back "
            f"with:\n    manage.py customer_merge_backup --restore")
