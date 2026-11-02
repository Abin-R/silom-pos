"""Alert on a till Beam QR that was paid but never became a bill.

The till saves a Beam QR sale only when its own poll sees "paid", and there
are no webhooks in this project.  If the cashier cancels the QR, closes the
payment screen or loses wifi just before the money lands, the customer has
paid, Beam has the money, and nothing points at it (PS000011903, 30 Oct 2026:
paid 0.8 s after the last poll, then rung up again as Custom · QR).

The till's basket never reaches the server, so this cannot make the sale
itself.  It tells a person instead: one SeaTalk message per charge, with the
bills at that branch that could be the same sale rung up another way.

Run it on a timer, every couple of minutes:

    */2 * * * * cd ~/silom-pos/backend_django && <venv>/python manage.py check_till_beam_charges

Idempotent: a charge is finished (``done``) once it failed, matched a bill or
was alerted, and is never looked at again.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone as djtz

from bravepos import gateways, seatalk
from bravepos.gateways import GatewayConfigError, GatewayError
from bravepos.models import Order, TillBeamCharge

# Leave a fresh QR to the till's own poll.
MIN_AGE = timedelta(minutes=2)
# A PromptPay QR is long dead by then; stop asking Beam about it.
MAX_AGE = timedelta(hours=24)
# How long a paid charge may sit without a bill before it is reported.  The
# till saves the bill within one 3 s poll of "paid"; this only covers a sweep
# that happens to land between Beam's answer and the till's save.
PAID_GRACE = timedelta(minutes=2)
# Window for "bills that could be this sale, rung up some other way".
NEARBY = timedelta(minutes=15)


def _baht(v) -> str:
    return f"฿{Decimal(v or 0):,.2f}"


def build_alert(charge, candidates) -> str:
    when = djtz.localtime(charge.created_at).strftime("%d %b %Y %H:%M")
    out = [
        "BravePOS: Beam QR paid, but no bill was saved",
        f"Branch: {charge.branch.name}",
        f"Amount: {_baht(charge.amount)}",
        f"QR shown: {when} by {charge.staff or '—'}",
        f"Beam charge: {charge.charge_id}",
        "",
        "The customer paid this Beam QR, but the till did not save a sale "
        "for it (the QR was cancelled or closed before the payment arrived).",
    ]
    if candidates:
        out += ["", "Bills at this branch with the same amount and no Beam payment:"]
        out += [
            f"• {o.order_number} — {o.payment_method}, "
            f"{djtz.localtime(o.created_at).strftime('%H:%M')}"
            for o in candidates
        ]
    else:
        out += ["", "No bill with this amount was found nearby — the sale may be missing."]
    return "\n".join(out)


def candidate_bills(charge) -> list:
    """Bills that may be this payment rung up another way: same branch and
    amount, close in time, carrying no gateway id of their own."""
    return list(
        Order.objects.filter(
            branch=charge.branch,
            total=charge.amount,
            created_at__gte=charge.created_at - NEARBY,
            created_at__lte=charge.created_at + NEARBY,
            beam_charge_id="", beam_link_id="", omise_link_id="", omise_charge_id="",
        ).order_by("created_at")
    )


class Command(BaseCommand):
    help = "Alert on paid till Beam QR charges that no bill carries."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would happen without saving or alerting.',
        )

    def handle(self, *args, **opts):
        dry = opts['dry_run']
        now = djtz.now()
        rows = list(
            TillBeamCharge.objects
            .filter(done=False, created_at__gte=now - MAX_AGE, created_at__lte=now - MIN_AGE)
            .select_related('branch')
        )

        matched = failed = alerted = waiting = errored = 0
        for row in rows:
            if row.status not in gateways.BEAM_CHARGE_SUCCESS:
                try:
                    row.status = gateways.beam_get_charge(row.charge_id, branch=row.branch)['status']
                except (GatewayError, GatewayConfigError) as e:
                    # A gateway hiccup must not kill the sweep; the next run retries.
                    errored += 1
                    self.stderr.write(f'  {row.charge_id} gateway error: {e}')
                    continue
                if row.status in gateways.BEAM_CHARGE_FAILED:
                    failed += 1
                    row.done = True
                elif row.status in gateways.BEAM_CHARGE_SUCCESS:
                    row.paid_seen_at = now
                else:
                    waiting += 1

            if row.status in gateways.BEAM_CHARGE_SUCCESS:
                if Order.objects.filter(beam_charge_id=row.charge_id).exists():
                    matched += 1
                    row.done = True
                elif now - row.paid_seen_at >= PAID_GRACE:
                    candidates = candidate_bills(row)
                    self.stdout.write(
                        f'  UNBILLED {row.charge_id} {row.branch.name} {row.amount} THB; '
                        f'candidates: {[o.order_number for o in candidates]}'
                    )
                    # Sandbox branches take test payments; their alerts would
                    # only be noise in the live group.
                    sent = dry or row.branch.beam_sandbox or seatalk.send_group_text(
                        seatalk.beam_group_id(), build_alert(row, candidates))
                    if sent:
                        alerted += 1
                        row.alerted_at = now
                        row.done = True
                    else:
                        # Not delivered: keep the row open so the next run retries.
                        errored += 1
                else:
                    waiting += 1

            if not dry:
                row.save(update_fields=['status', 'paid_seen_at', 'alerted_at', 'done'])

        self.stdout.write(
            f'checked={len(rows)} matched={matched} failed={failed} '
            f'alerted={alerted} waiting={waiting} errors={errored}'
        )
