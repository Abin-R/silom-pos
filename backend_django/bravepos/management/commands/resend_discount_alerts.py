"""Re-post the "Other" discount alerts for one day's sales.

For when the alert group changes, or alerts were missed: every saved order
from that day carrying an "Other" discount is posted again, oldest first.

    manage.py resend_discount_alerts                       # today, to the configured group
    manage.py resend_discount_alerts --date 2026-10-13 --group <id>
    manage.py resend_discount_alerts --dry-run             # print, send nothing
"""
import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from bravepos import discounts, seatalk
from bravepos.models import Order


class Command(BaseCommand):
    help = 'Re-post one day\'s "Other" discount alerts to SeaTalk.'

    def add_arguments(self, parser):
        parser.add_argument("--date", help="YYYY-MM-DD, local time (default: today)")
        parser.add_argument("--group", help="SeaTalk group id (default: SEATALK_DISCOUNT_GROUP_ID)")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        day = (datetime.date.fromisoformat(opts["date"]) if opts["date"]
               else timezone.localdate())
        group = opts["group"] or seatalk.discount_group_id()
        if not opts["dry_run"] and not (group and seatalk.is_configured()):
            raise CommandError("SeaTalk is not configured (app id/secret or group).")

        orders = (Order.objects.filter(created_at__date=day,
                                       branch__discount_types_enabled=True)
                  .select_related("branch").prefetch_related("items")
                  .order_by("created_at"))
        sent = 0
        for order in orders:
            lines = discounts.other_discount_lines(order)
            if not lines:
                continue
            text = discounts.build_alert(order, lines)
            if opts["dry_run"]:
                self.stdout.write(text + "\n" + "-" * 30)
            elif seatalk.send_group_text(group, text):
                sent += 1
            else:
                self.stderr.write(f"failed: {order.order_number}")
        if not opts["dry_run"]:
            self.stdout.write(f"{day}: {sent} alert(s) sent")
