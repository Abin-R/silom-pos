"""Tell SeaTalk when the Grab stock sync goes quiet, and when it is back.

The poller (shopify_django_app) sends a heartbeat after every cycle.  With live
pushes on, no good heartbeat for five minutes — or a heartbeat that reports a
problem — is an outage: Grab's count can no longer be kept right, so the till
and self-order hold back whatever Grab can still sell (``stock_pool``).  Nobody
is told by that alone, and the poller cannot report that it is down, so this
runs here, on its own timer, and works even if the shopify server is gone:

    * * * * * cd /srv/bravepos && .venv/bin/python manage.py grab_sync_watchdog

One alert when an outage starts and one when it ends, however often it runs;
``GrabSyncStatus.outage_alerted_at`` remembers.  Alerts go to
``SEATALK_GRAB_GROUP_ID``; unset, nothing is sent (and nothing is remembered as
sent, so setting it later still announces an outage that is still going on).
Safe to run concurrently: the "alerted" mark is won with one conditional UPDATE.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone as djtz

from bravepos import grab_sync, seatalk, stock_pool
from bravepos.models import GrabSyncStatus


def describe(status, now) -> str:
    if status.problem:
        return f"the poller reports a problem: {status.problem}"
    if status.last_ok_at is None:
        return "the poller has never finished a good cycle"
    minutes = int((now - status.last_ok_at).total_seconds() // 60)
    return f"no good cycle for {minutes} minutes"


class Command(BaseCommand):
    help = "Alert SeaTalk when the Grab stock sync goes quiet, and when it recovers."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be alerted without sending or remembering anything.",
        )

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        now = djtz.now()
        configured = bool(seatalk.grab_group_id())
        rows = (
            GrabSyncStatus.objects.select_related("branch")
            .filter(branch__active=True).exclude(branch__grab_merchant_id="")
        )
        for status in rows:
            name = status.branch.name
            outage = stock_pool.in_outage(status, now)
            if outage and status.outage_alerted_at is None:
                text = (
                    f"Grab stock sync is down at {name}: {describe(status, now)}. "
                    "Grab's stock is not being kept up to date, so the till and the QR "
                    "menu are holding back the units Grab can still sell."
                )
                self.stdout.write(f"{name}: outage — {text}")
                if not dry and configured and (
                    GrabSyncStatus.objects
                    .filter(pk=status.pk, outage_alerted_at__isnull=True)
                    .update(outage_alerted_at=now)
                ):
                    grab_sync.alert(text)
            elif not outage and status.outage_alerted_at is not None:
                text = f"Grab stock sync is back at {name}. The till and QR menu are back to normal."
                self.stdout.write(f"{name}: recovered")
                if not dry and configured and (
                    GrabSyncStatus.objects
                    .filter(pk=status.pk, outage_alerted_at__isnull=False)
                    .update(outage_alerted_at=None)
                ):
                    grab_sync.alert(text)
