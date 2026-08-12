"""A branch-day that changes after it was billed must be corrected at Peak.

The consolidated receipt is a snapshot: it says what a branch sold on a day,
and it is filed the morning after.  Two things can invalidate it afterwards —
a bill gets its own full tax invoice (so the sale is now filed twice), or a
bill is voided (so the day's total is overstated).  Neither is rare, and Peak
does not notice either one.

So the write paths flag the branch-day and ``consolidate_daily --issue`` ends
with a sweep that voids the stale document and files a replacement built from
the orders as they stand now.  These tests hold that loop closed at both ends:
the flag is set when it should be (and not when it shouldn't), and the sweep
turns a flag into exactly one live, correct document.

Peak is stubbed at its single HTTP boundary, ``make_peak_request`` — the tests
assert on what would have gone over the wire.
"""
from __future__ import annotations

import os
from datetime import date as date_cls, datetime, time as dt_time, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone as djtz

from bravepos.models import (
    BranchSession, ConsolidatedReceipt, Order, OrderItem, Staff,
)
from bravepos.peak import create_peak_receipt_for_order, flag_branch_day_for_reissue

from .factories import make_branch, make_product, make_shop


CONTACT_ID = "contact-bravepos"

TAX_INVOICE_FORM = {
    "name": "Brave Brand Co., Ltd.",
    "tax_id": "0105563083534",
    "registered_address": "55 Biohouse Building, Sukhumvit 39, Bangkok",
    "registered_postal_code": "10110",
}


def at_local_noon(day: date_cls):
    """Noon Bangkok — far from either midnight, so a test never lands on the
    wrong side of the day boundary the consolidation groups on."""
    return djtz.make_aware(datetime.combine(day, dt_time(12, 0)))


class FakePeak:
    """Stands in for the Peak API at the ``make_peak_request`` seam.

    Records what was sent and hands back the minimum well-formed response each
    caller unpacks.  ``void_res_code`` lets a test make the void fail, which is
    the case that must never end with two live receipts for one day.
    """

    def __init__(self, void_res_code: str = "200"):
        self.void_res_code = void_res_code
        self.enqueued: list[dict] = []
        self.voided: list[str] = []
        self.queue_checks: list[str] = []
        self._seq = 0

    # Every receipt this fake materialises is derived from its queue id, so a
    # test can say "the document from the second enqueue" without bookkeeping.
    def code_for(self, n: int) -> str:
        return f"RT-q{n}"

    def __call__(self, path: str, method: str, body: dict | None = None) -> dict:
        body = body or {}

        if path == "/contacts":
            return {"PeakContacts": {"contacts": [{"id": "contact-buyer"}]}}

        if path == "/products":
            sent = (body.get("PeakProducts") or {}).get("products") or []
            return {"PeakProducts": {"products": [
                # Peak echoes ``description`` back; that is how the caller
                # correlates a created product to the row it asked for.
                {"code": f"PROD-{i}", "description": p.get("description")}
                for i, p in enumerate(sent)
            ]}}

        if path == "/receipts/queue" and method == "POST":
            self._seq += 1
            self.enqueued.append(body["PeakReceipts"]["receipts"][0])
            return {"queueId": f"q{self._seq}"}

        if path == "/receipts/queue" and method == "GET":
            queue_id = body["queueId"]
            self.queue_checks.append(queue_id)
            return {"PeakReceipts": {"receipts": [{
                "code": f"RT-{queue_id}",
                "documentLink": f"https://peak.test/{queue_id}",
            }]}}

        if path == "/receipts/void":
            self.voided.append(body["PeakReceipts"]["code"])
            return {"PeakReceipts": {
                "resCode": self.void_res_code, "resDesc": "voided",
            }}

        raise AssertionError(f"unexpected Peak call: {method} {path}")

    def patch(self):
        return mock.patch("bravepos.peak.make_peak_request", new=self)


class ConsolidationTestCase(TestCase):
    """Shared world: one branch, one product, bills placed on chosen days."""

    def setUp(self):
        make_shop()
        self.branch = make_branch(name="BIO HOUSE")
        self.product = make_product(self.branch, name="Red Velvet Cookie",
                                    price="160.00")
        self.today = djtz.localdate()

    def make_order(self, day: date_cls, number: str, total="160.00", qty=1,
                   status="completed") -> Order:
        order = Order.objects.create(
            branch=self.branch, order_number=number,
            subtotal=Decimal(total), total=Decimal(total),
            payment_method="QR", paid_amount=Decimal(total),
            status=status, staff="Cashier",
        )
        OrderItem.objects.create(
            order=order, product=self.product, name=self.product.name,
            price=self.product.price, qty=qty,
        )
        # created_at is auto_now_add, so the day has to be forced afterwards.
        Order.objects.filter(pk=order.pk).update(created_at=at_local_noon(day))
        order.refresh_from_db()
        return order

    def make_consolidated(self, day: date_cls, orders: list[Order],
                          queue_id="q0", confirmed=True) -> ConsolidatedReceipt:
        """A branch-day already billed at Peak, as the nightly run leaves it."""
        cr = ConsolidatedReceipt.objects.create(
            branch=self.branch, date=day, peak_queue_id=queue_id,
            response={"PeakReceipts": {"receipts": [{
                "code": f"RT-{queue_id}", "documentLink": "https://peak.test/old",
            }]}} if confirmed else None,
        )
        cr.orders.set(orders)
        return cr


class ReissueFlagTests(ConsolidationTestCase):
    """The flag goes up exactly when the filed document stops being true."""

    def test_late_tax_invoice_flags_the_branch_day(self):
        day = self.today - timedelta(days=3)
        order = self.make_order(day, "PS000000247")
        cr = self.make_consolidated(day, [order])

        order.tax_invoice_data = TAX_INVOICE_FORM
        order.save(update_fields=["tax_invoice_data"])
        with FakePeak().patch():
            create_peak_receipt_for_order(order)

        cr.refresh_from_db()
        self.assertTrue(cr.needs_reissue)

    def test_tax_invoice_on_a_day_never_billed_flags_nothing(self):
        """The ordinary case — the consolidation simply excludes the bill when
        it runs, so there is nothing to correct and nothing to flag."""
        day = self.today - timedelta(days=3)
        order = self.make_order(day, "PS000000248")

        order.tax_invoice_data = TAX_INVOICE_FORM
        order.save(update_fields=["tax_invoice_data"])
        with FakePeak().patch():
            create_peak_receipt_for_order(order)

        self.assertFalse(ConsolidatedReceipt.objects.filter(
            needs_reissue=True).exists())

    def test_a_row_that_never_reached_peak_is_not_flagged(self):
        """No queue id means no document was ever filed, so there is nothing
        stale to replace — reissuing would just void thin air."""
        day = self.today - timedelta(days=3)
        order = self.make_order(day, "PS000000249")
        cr = ConsolidatedReceipt.objects.create(branch=self.branch, date=day)

        self.assertFalse(flag_branch_day_for_reissue(order))
        cr.refresh_from_db()
        self.assertFalse(cr.needs_reissue)

    def test_voiding_a_bill_flags_its_branch_day(self):
        day = self.today - timedelta(days=3)
        order = self.make_order(day, "PS000000250")
        cr = self.make_consolidated(day, [order])

        self._set_status(order, "cancel")

        cr.refresh_from_db()
        self.assertTrue(cr.needs_reissue)

    def test_un_voiding_a_bill_flags_its_branch_day(self):
        """Both directions matter: money coming back into the day makes the
        filed total just as wrong as money leaving it."""
        day = self.today - timedelta(days=3)
        order = self.make_order(day, "PS000000251", status="cancel")
        cr = self.make_consolidated(day, [])

        self._set_status(order, "completed")

        cr.refresh_from_db()
        self.assertTrue(cr.needs_reissue)

    def test_kitchen_status_moves_do_not_flag_anything(self):
        """Only cancelled-ness decides whether a bill's money is in the day.
        Flagging on every status write would keep replacing a correct document
        with an identical one under a new number."""
        day = self.today - timedelta(days=3)
        order = self.make_order(day, "PS000000252", status="preparing")
        cr = self.make_consolidated(day, [order])

        self._set_status(order, "completed")

        cr.refresh_from_db()
        self.assertFalse(cr.needs_reissue)

    def test_re_cancelling_an_already_cancelled_bill_does_not_flag(self):
        day = self.today - timedelta(days=3)
        order = self.make_order(day, "PS000000253", status="cancel")
        cr = self.make_consolidated(day, [])

        self._set_status(order, "cancel")

        cr.refresh_from_db()
        self.assertFalse(cr.needs_reissue)

    # — helpers —
    def _set_status(self, order: Order, status: str):
        staff = Staff.objects.create(
            name="Bonus", email="bonus@test.local", password_hash="x",
            role="cashier",
        )
        staff.branches.add(self.branch)
        session = BranchSession.objects.create(
            token="tok" * 12, branch=self.branch, staff=staff,
        )
        resp = self.client.put(
            f"/api/orders/{order.id}/status",
            data={"status": status},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {session.token}",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp


class ReissueSweepTests(ConsolidationTestCase):
    """A flagged day ends with exactly one live document, and it is correct."""

    def run_sweep(self, fake: FakePeak, *extra):
        """The nightly cron's own invocation.  Every stale day in these tests
        is older than yesterday, so anything corrected was corrected by the
        sweep and not by the ordinary yesterday pass."""
        self.out, self.err = StringIO(), StringIO()
        with fake.patch(), mock.patch.dict(
            os.environ, {"PEAK_BRAVEPOS_CONTACT_ID": CONTACT_ID}
        ):
            call_command(
                "consolidate_daily", "--issue", *extra,
                stdout=self.out, stderr=self.err,
            )

    def test_sweep_voids_the_stale_receipt_and_files_a_replacement(self):
        day = self.today - timedelta(days=3)
        invoiced = self.make_order(day, "PS000000247")
        kept = self.make_order(day, "PS000000248")
        cr = self.make_consolidated(day, [invoiced, kept], queue_id="q0")

        # The bill took itself out of the day by getting its own tax invoice.
        Order.objects.filter(pk=invoiced.pk).update(peak_queue_id="own-queue")
        cr.needs_reissue = True
        cr.save(update_fields=["needs_reissue"])

        fake = FakePeak()
        self.run_sweep(fake)

        self.assertEqual(fake.voided, ["RT-q0"])
        self.assertEqual(len(fake.enqueued), 1)

        receipt = fake.enqueued[0]
        self.assertEqual(receipt["contact"], {"id": CONTACT_ID})
        self.assertEqual(receipt["issuedDate"], day.strftime("%Y%m%d"))
        # 160.00, not 320.00: the replacement bills only the bill that is still
        # the consolidation's to bill.
        paid = receipt["paidPayments"]["payments"][0]["amount"]
        self.assertEqual(Decimal(str(paid)), Decimal("160.00"))

        cr.refresh_from_db()
        self.assertFalse(cr.needs_reissue)
        self.assertEqual(cr.peak_code, fake.code_for(1))
        # The link table must describe the live document, not the dead one.
        self.assertEqual([o.id for o in cr.orders.all()], [kept.id])

    def test_sweep_corrects_days_older_than_yesterday(self):
        """--issue refuses to file new documents for old dates. A replacement
        for a document already known to be wrong is not that, and a customer
        can ask for a tax invoice long after the day was billed."""
        day = self.today - timedelta(days=40)
        order = self.make_order(day, "PS000000260")
        cr = self.make_consolidated(day, [order], queue_id="q0")
        cr.needs_reissue = True
        cr.save(update_fields=["needs_reissue"])

        fake = FakePeak()
        self.run_sweep(fake)

        self.assertEqual(fake.voided, ["RT-q0"])
        self.assertEqual(len(fake.enqueued), 1)
        cr.refresh_from_db()
        self.assertFalse(cr.needs_reissue)

    def test_sweep_retires_a_day_with_nothing_left_to_bill(self):
        """Every bill gone — Peak will not take a receipt with no products, so
        the only honest correction is to take the old one down."""
        day = self.today - timedelta(days=3)
        order = self.make_order(day, "PS000000261", status="cancel")
        cr = self.make_consolidated(day, [order], queue_id="q0")
        cr.needs_reissue = True
        cr.save(update_fields=["needs_reissue"])

        fake = FakePeak()
        self.run_sweep(fake)

        self.assertEqual(fake.voided, ["RT-q0"])
        self.assertEqual(fake.enqueued, [])
        cr.refresh_from_db()
        self.assertFalse(cr.needs_reissue)
        self.assertEqual(cr.peak_code, "")
        self.assertEqual(cr.peak_queue_id, "")
        self.assertEqual(list(cr.orders.all()), [])

    def test_a_refused_void_leaves_the_day_flagged_and_unbilled_twice(self):
        """The one outcome that must never happen is two live receipts for one
        day. Stale beats double-counted, and the flag survives for a retry."""
        day = self.today - timedelta(days=3)
        order = self.make_order(day, "PS000000262")
        cr = self.make_consolidated(day, [order], queue_id="q0")
        cr.needs_reissue = True
        cr.save(update_fields=["needs_reissue"])

        fake = FakePeak(void_res_code="500")
        self.run_sweep(fake)

        self.assertEqual(fake.enqueued, [])
        cr.refresh_from_db()
        self.assertTrue(cr.needs_reissue)
        self.assertEqual(cr.peak_code, "RT-q0")

    def test_an_unconfirmed_document_is_polled_before_it_is_replaced(self):
        """A day whose first attempt never confirmed still has something to
        void: Peak may have materialised it after the poll gave up. Enqueuing
        over it without looking is how a day ends up billed twice."""
        day = self.today - timedelta(days=3)
        order = self.make_order(day, "PS000000263")
        cr = self.make_consolidated(day, [order], queue_id="q7", confirmed=False)
        cr.needs_reissue = True
        cr.save(update_fields=["needs_reissue"])

        fake = FakePeak()
        self.run_sweep(fake)

        self.assertIn("q7", fake.queue_checks)
        self.assertEqual(fake.voided, ["RT-q7"])
        self.assertEqual(len(fake.enqueued), 1)

    def test_skip_reissue_sweep_leaves_the_flag_alone(self):
        day = self.today - timedelta(days=3)
        order = self.make_order(day, "PS000000264")
        cr = self.make_consolidated(day, [order], queue_id="q0")
        cr.needs_reissue = True
        cr.save(update_fields=["needs_reissue"])

        fake = FakePeak()
        self.run_sweep(fake, "--skip-reissue-sweep")

        self.assertEqual(fake.voided, [])
        self.assertEqual(fake.enqueued, [])
        cr.refresh_from_db()
        self.assertTrue(cr.needs_reissue)
