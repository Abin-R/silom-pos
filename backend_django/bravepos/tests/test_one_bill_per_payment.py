"""One payment is one bill.

PS000010938 and PS000010939 (Central Nakhon Si, 26 Oct 2026) were two bills
for one Beam charge, saved 0.2 s apart: the till's paid-yet? checks piled up
while the tablet was off wifi and all answered "paid" at once, and each saved
the sale.  ฿320 in takings and two cookies out of stock for one ฿160 sale.
A save carrying a gateway id the branch has already billed now gets that bill
back instead of a new one.
"""
from __future__ import annotations

from django.test import TestCase

from bravepos.models import BranchSession, Order, Staff, StockMovement

from .factories import make_branch, make_product, make_shop


class OneBillPerPaymentTests(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch()
        self.product = make_product(self.branch, name='Biscoff Mochi Cookie',
                                    price='160.00', stock=10)
        staff = Staff.objects.create(
            name='Cashier', email='c@test.local', password_hash='x', role='cashier',
        )
        staff.branches.add(self.branch)
        session = BranchSession.objects.create(
            token='tok' * 12, branch=self.branch, staff=staff,
        )
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {session.token}'}

    def _save(self, method='Beam QR', **extra):
        body = {
            'items': [{'product_id': str(self.product.id), 'name': self.product.name,
                       'price': 160, 'qty': 1}],
            'subtotal': 160, 'total': 160, 'payment_method': method,
            'paid_amount': 160, 'source': 'table', **extra,
        }
        return self.client.post('/api/orders', body, content_type='application/json',
                                **self.auth)

    def _bills(self):
        return Order.objects.filter(branch=self.branch).count()

    def test_second_save_for_same_beam_charge_returns_the_first_bill(self):
        first = self._save(beam_charge_id='ch_1')
        again = self._save(beam_charge_id='ch_1')

        self.assertEqual(first.status_code, 201)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()['order_number'], first.json()['order_number'])
        self.assertEqual(self._bills(), 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 9)
        self.assertEqual(StockMovement.objects.filter(product=self.product).count(), 1)

    def test_card_link_ids_are_matched_too(self):
        self._save(method='Beam Card', beam_link_id='pl_1', beam_charge_id='ch_9')
        self.assertEqual(self._save(method='Beam Card', beam_link_id='pl_1').status_code, 200)
        self._save(method='Card · Omise', omise_link_id='ln_1')
        self.assertEqual(self._save(method='Card · Omise', omise_link_id='ln_1').status_code, 200)
        self.assertEqual(self._bills(), 2)

    def test_different_charges_are_different_sales(self):
        self._save(beam_charge_id='ch_1')
        self._save(beam_charge_id='ch_2')
        self.assertEqual(self._bills(), 2)

    def test_sales_without_a_gateway_id_are_never_merged(self):
        # Cash and the manual QR carry no payment id: two identical sales are
        # two sales.  The till sends null for every gateway id on these.
        for _ in range(2):
            res = self._save(method='Cash', beam_charge_id=None, beam_link_id=None,
                             omise_link_id=None, omise_charge_id=None)
            self.assertEqual(res.status_code, 201)
        self.assertEqual(self._bills(), 2)

    def test_another_branchs_bill_is_not_returned(self):
        other = make_branch(name='Other')
        Order.objects.create(branch=other, order_number='PS000000500',
                             beam_charge_id='ch_1')
        self.assertEqual(self._save(beam_charge_id='ch_1').status_code, 201)
        self.assertEqual(self._bills(), 1)
