"""The receipt must print exactly once, on exactly one tablet.

printerQueue's in-flight Set is module state — it dedupes within one JS runtime
and knows nothing about the tablet next to it. So the claim has to be
server-side, which is what these tests pin down.
"""
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone as djtz

from bravepos import selforder
from bravepos.models import BranchSession, Order, SelfOrder, Staff
from bravepos.views import PRINT_CLAIM_LEASE_S

from .factories import StubGateway, make_branch, make_product, make_shop, open_shift


class PrintClaimTests(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch()
        open_shift(self.branch)
        self.p = make_product(self.branch, price='100.00')

        # Two tablets at the same branch, each with its own session — the exact
        # setup that used to produce two physical receipts for one order.
        self.tablets = []
        for i in range(2):
            staff = Staff.objects.create(
                name=f'Cashier {i}',
                email=f'cashier{i}@test.local',   # unique=True on the model
                password_hash='x',
                role='cashier',
            )
            staff.branches.add(self.branch)
            sess = BranchSession.objects.create(
                token=f'tok{i}' * 8, branch=self.branch, staff=staff,
            )
            self.tablets.append(sess)

    def _paid_selforder(self) -> SelfOrder:
        with StubGateway() as gw:
            d = selforder.create_draft(self.branch, [{'product_id': str(self.p.id), 'qty': 1}])
            selforder.start_payment(d, 'qr', redirect_url='https://x/')
            gw.paid = True
            selforder.promote(SelfOrder.objects.get(pk=d.pk))
        return SelfOrder.objects.get(pk=d.pk)

    def _poll(self, tablet) -> list:
        res = self.client.get(
            '/api/self-orders/pending',
            HTTP_AUTHORIZATION=f'Bearer {tablet.token}',
        )
        self.assertEqual(res.status_code, 200)
        return res.json()

    def test_only_one_tablet_is_handed_the_receipt(self):
        self._paid_selforder()

        first = self._poll(self.tablets[0])
        second = self._poll(self.tablets[1])

        self.assertEqual(len(first), 1, 'the first tablet should get the job')
        self.assertEqual(second, [], 'the second must NOT get the same job')

    def test_the_same_tablet_does_not_get_it_twice(self):
        self._paid_selforder()
        self.assertEqual(len(self._poll(self.tablets[0])), 1)
        self.assertEqual(self._poll(self.tablets[0]), [], 'no re-serve on the next poll')

    def test_a_dead_tablets_claim_is_stealable_after_the_lease(self):
        """A tablet that fetched the job then died (battery, crash, Wi-Fi) must
        not take the receipt to the grave with it."""
        so = self._paid_selforder()
        self.assertEqual(len(self._poll(self.tablets[0])), 1)

        # Tablet 0 never printed. Age its claim past the lease.
        stale = djtz.now() - timedelta(seconds=PRINT_CLAIM_LEASE_S + 10)
        SelfOrder.objects.filter(pk=so.pk).update(
            print_claimed_at=stale, printed_at=None,
        )

        recovered = self._poll(self.tablets[1])
        self.assertEqual(len(recovered), 1, 'tablet 2 should take over the job')

    def test_payload_carries_what_the_receipt_needs(self):
        self._paid_selforder()
        [job] = self._poll(self.tablets[0])

        self.assertEqual(job['source'], 'kiosk')
        self.assertIsNotNone(job['queue_number'])
        self.assertTrue(job['order_number'].startswith('PS'))
        self.assertEqual(len(job['items']), 1)

    def test_unpaid_selforders_are_never_offered(self):
        with StubGateway():
            d = selforder.create_draft(self.branch, [{'product_id': str(self.p.id), 'qty': 1}])
            selforder.start_payment(d, 'qr', redirect_url='https://x/')

        self.assertEqual(self._poll(self.tablets[0]), [])
        self.assertEqual(Order.objects.count(), 0)

    def test_a_branchs_tablet_never_sees_another_branchs_orders(self):
        other = make_branch(name='Thonglor')
        open_shift(other)
        other_p = make_product(other, price='50.00')
        with StubGateway() as gw:
            d = selforder.create_draft(other, [{'product_id': str(other_p.id), 'qty': 1}])
            selforder.start_payment(d, 'qr', redirect_url='https://x/')
            gw.paid = True
            selforder.promote(SelfOrder.objects.get(pk=d.pk))

        self.assertEqual(self._poll(self.tablets[0]), [], 'branch scoping must hold')
