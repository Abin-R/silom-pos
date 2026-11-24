"""Tell SeaTalk when the Grab stock sync goes quiet, and when it is back.

The poller (shopify_django_app) sends a heartbeat after every cycle.  No good
heartbeat for ``QUIET_AFTER`` — or a heartbeat that names a problem — means
Grab orders are not being deducted and Grab's stock is not being kept up to
date.  Shop sales are never affected, but someone should know.  The poller
cannot report that it is down, so this runs here, on its own timer, and works
even if the shopify server is gone.  Install it in the
server's crontab (paths as in ``deploy.sh``):

    * * * * * cd /home/azureuser/silom-pos/backend_django && /home/azureuser/.local/share/virtualenvs/backend_django-43C1HBFU/bin/python manage.py grab_sync_watchdog

Only branches with a Grab merchant ID whose poller has sent at least one
heartbeat are watched.  One alert when the quiet spell starts and one when it
ends, however often this runs; ``GrabSyncStatus.quiet_alerted_at`` remembers.
Alerts go to ``SEATALK_GRAB_GROUP_ID``; when SeaTalk is not configured or a
send fails, nothing is remembered as sent, so the next run tries again.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone as djtz

from bravepos import seatalk
from bravepos.models import GrabSyncStatus

QUIET_AFTER = timedelta(minutes=15)   # more than 3 order cycles (the poller runs one every 4 minutes)


def is_quiet(status, now) -> bool:
    if status.problem or status.last_ok_at is None:
        return True
    return now - status.last_ok_at > QUIET_AFTER


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

    def _send(self, text) -> bool:
        # Synchronous on purpose: this process exits right after, and a
        # background thread would die with it before the message went out.
        group = seatalk.grab_group_id()
        return bool(group) and seatalk.send_group_text(group, text)

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        now = djtz.now()
        rows = (
            GrabSyncStatus.objects.select_related("branch")
            .filter(branch__active=True).exclude(branch__grab_merchant_id="")
        )
        for status in rows:
            name = status.branch.name
            quiet = is_quiet(status, now)
            if quiet and status.quiet_alerted_at is None:
                text = (
                    f"Grab stock sync is down at {name}: {describe(status, now)}. "
                    "Grab orders are not being deducted and Grab's stock is not being updated. "
                    "Shop sales are not affected."
                )
                self.stdout.write(f"{name}: quiet — {text}")
                if not dry and self._send(text):
                    GrabSyncStatus.objects.filter(pk=status.pk).update(quiet_alerted_at=now)
            elif not quiet and status.quiet_alerted_at is not None:
                text = f"Grab stock sync is back at {name}."
                self.stdout.write(f"{name}: recovered")
                if not dry and self._send(text):
                    GrabSyncStatus.objects.filter(pk=status.pk).update(quiet_alerted_at=None)
