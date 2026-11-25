"""Save each day's stock for good once it is more than 15 days old.

Backoffice › Inventory works the last 15 days out and shows saved figures for
any day before that; this writes them (see ``bravepos.inventory_history``).
Every closed day not saved yet is saved, oldest first, so the first run fills
in the past and a missed night is caught up by the next one.  A saved day is
never rewritten.

Install it in the server's crontab (the server is on UTC, so this is 00:30 and
08:30 in Bangkok; paths as in ``deploy.sh``).  The second run normally finds
nothing to do: it is there to catch a night the first one missed.

    30 17,1 * * * cd /home/azureuser/silom-pos/backend_django && /home/azureuser/.local/share/virtualenvs/backend_django-43C1HBFU/bin/python manage.py save_inventory_snapshots >> /home/azureuser/silom-pos/backend_django/logs/inventory_snapshots.log 2>&1

A missed night that was caught up, or a day that could not be saved, is posted
to ``seatalk.INVENTORY_GROUP_ID``.  A day that fails does not stop the others;
the error is raised at the end, so it reaches Sentry too.  Until a day is saved
the page works it out as it does the last 15.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from bravepos import inventory_history, seatalk
from bravepos.models import InventorySnapshot


def describe(days) -> str:
    names = [d.strftime("%d %b %Y") for d in days[:5]]
    more = f" and {len(days) - 5} more" if len(days) > 5 else ""
    return ", ".join(names) + more


class Command(BaseCommand):
    help = "Save the stock of every day more than 15 days old that is not saved yet."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Show what would be saved. Writes and sends nothing.")

    def handle(self, *args, **options):
        days = inventory_history.days_to_save()
        if not days:
            self.stdout.write("Nothing to save.")
            return
        if options["dry_run"]:
            for day in days:
                rows = inventory_history.build_day(day)
                self.stdout.write(
                    f"{day}: would save {len(rows)} rows, "
                    f"{len({r.branch_id for r in rows})} branches, "
                    f"{sum(r.stock for r in rows)} units")
            return

        # The very first run fills in the past; none of that was missed.
        first_run = not InventorySnapshot.objects.exists()
        caught_up, failed, error = [], [], None
        for day in days:
            late = not first_run and inventory_history.is_late(day)
            try:
                rows = inventory_history.save_day(day)
            except Exception as e:  # noqa: BLE001 — one bad day must not stop the rest
                self.stderr.write(f"{day}: failed: {e}")
                failed.append(day)
                error = error or e
                continue
            self.stdout.write(f"{day}: saved {rows} rows")
            if late:
                caught_up.append(day)

        lines = []
        if caught_up:
            lines.append(f"Saved late (the nightly run missed it): {describe(caught_up)}.")
        if failed:
            lines.append(
                f"NOT saved: {describe(failed)}. {type(error).__name__}: {str(error)[:300]}\n"
                "It is tried again on the next run. Until then the Inventory page works "
                "that day out instead.")
        if lines:
            # Synchronous: this process exits right after, and a background
            # thread would die with it before the message went out.
            seatalk.send_group_text(
                seatalk.inventory_group_id(),
                "\n".join(["Inventory history (Brave POS)"] + lines))
        if error:
            raise error
