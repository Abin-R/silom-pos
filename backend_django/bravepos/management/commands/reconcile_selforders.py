"""Confirm paid self-orders that nothing else noticed.

This is the *primary* safety net for the whole self-ordering flow, not a
tidy-up job. There are no webhooks anywhere in this project, so a draft only
becomes a sale when something polls the gateway — and every other poller is
best-effort:

  * the customer's browser stops polling the moment they close the tab, which
    is exactly what people do after paying;
  * the POS tablet may be asleep, on another screen, offline, or logged out
    (its poll needs a session, and the token is cleared on logout).

Without this command, a customer can pay and have no order, no receipt, and no
record — while the money sits with Beam. Run it on a timer (systemd/cron, every
minute or two):

    */2 * * * * cd /srv/bravepos && .venv/bin/python manage.py reconcile_selforders

Idempotent and safe to run concurrently with the pollers: promotion takes a row
lock and re-checks state, so at most one Order is ever created per draft.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone as djtz

from bravepos import selforder
from bravepos.gateways import GatewayConfigError, GatewayError
from bravepos.models import SelfOrder


class Command(BaseCommand):
    help = "Promote paid self-orders the pollers missed; expire dead drafts."

    def add_arguments(self, parser):
        parser.add_argument(
            '--max-age-minutes', type=int, default=selforder.DRAFT_TTL_MINUTES,
            help='Ignore (and expire) drafts older than this.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would happen without touching anything.',
        )

    def handle(self, *args, **opts):
        max_age = opts['max_age_minutes']
        dry = opts['dry_run']
        cutoff = djtz.now() - timedelta(minutes=max_age)

        # Only drafts that actually reached a gateway can have been paid; one
        # with no charge/link is just an abandoned cart.
        stale = (
            SelfOrder.objects
            .filter(status='pending', created_at__gte=cutoff)
            .exclude(beam_charge_id='', beam_link_id='')
            .select_related('branch')
        )

        promoted = failed = errored = 0
        for draft in stale:
            try:
                if dry:
                    status = selforder.gateway_status(draft)
                    self.stdout.write(f'  {draft.token[:8]}… → {status}')
                    continue
                order = selforder.promote(draft)
            except (GatewayError, GatewayConfigError) as e:
                # A gateway hiccup must not kill the sweep — the next run
                # retries this draft. Only give up when it ages out.
                errored += 1
                self.stderr.write(f'  {draft.token[:8]}… gateway error: {e}')
                continue

            if order is not None:
                promoted += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f'  promoted {draft.token[:8]}… → {order.order_number} '
                        f'({order.total} THB)'
                    )
                )
            else:
                draft.refresh_from_db()
                if draft.status == 'failed':
                    failed += 1

        expired = 0 if dry else selforder.expire_stale()

        self.stdout.write(
            f'checked={len(stale)} promoted={promoted} failed={failed} '
            f'expired={expired} errors={errored}'
        )
