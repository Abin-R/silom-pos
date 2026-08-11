"""The drawer figures have to move the moment a sale lands.

``Shift.total_sales_cash`` / ``expected_in_drawer`` are a cache of an aggregate
over the round's orders and movements.  They used to be written *only* by
``shift_close``, so for the entire life of an open round they read back as their
0 defaults: the Cash Drawer screen showed "Total Sales (cash) 0.00" however many
bills had been rung up, and the backoffice's unreconciled-cash tile was always
฿0.  These tests pin the totals to the sales, not to the close.
"""
from __future__ import annotations

from decimal import Decimal

from django.test import TestCase

from bravepos.models import BranchSession, Order, Shift, Staff

from .factories import make_branch, make_product, make_shop, open_shift


class ShiftCashTests(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch()
        self.product = make_product(self.branch, price='100.00')
        self.staff = Staff.objects.create(
            name='Nok', email='nok@test.local', password_hash='x', role='cashier',
        )
        self.staff.branches.add(self.branch)
        self.session = BranchSession.objects.create(
            token='tok' * 12, branch=self.branch, staff=self.staff,
        )
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {self.session.token}'}
        self.shift = open_shift(self.branch)
        self.shift.start_cash = Decimal('500.00')
        self.shift.save()

    # ── helpers ─────────────────────────────────────────────────────────────
    def _sell(self, total='100.00', method='Cash'):
        res = self.client.post(
            '/api/orders',
            {
                'items': [{
                    'product_id': str(self.product.id),
                    'name': self.product.name,
                    'qty': 1,
                    'price': total,
                    'total': total,
                }],
                'payment_method': method,
                'subtotal': total,
                'total': total,
                'paid_amount': total,
                'status': 'completed',
            },
            content_type='application/json',
            **self.auth,
        )
        self.assertEqual(res.status_code, 201, res.content)
        return res.json()

    def _current(self):
        res = self.client.get('/api/shifts/current', **self.auth)
        self.assertEqual(res.status_code, 200)
        return res.json()

    # ── the regression ──────────────────────────────────────────────────────
    def test_cash_sale_updates_current_shift_immediately(self):
        """The bug: this read 0.00 until the round was closed."""
        self._sell()
        cur = self._current()
        self.assertEqual(Decimal(str(cur['total_sales_cash'])), Decimal('100.00'))
        # start 500 + 100 cash sale
        self.assertEqual(Decimal(str(cur['expected_in_drawer'])), Decimal('600.00'))

    def test_totals_accumulate_across_sales(self):
        self._sell('100.00')
        self._sell('250.00')
        cur = self._current()
        self.assertEqual(Decimal(str(cur['total_sales_cash'])), Decimal('350.00'))
        self.assertEqual(Decimal(str(cur['expected_in_drawer'])), Decimal('850.00'))

    def test_card_sale_does_not_land_in_the_drawer(self):
        self._sell('100.00', method='Card')
        cur = self._current()
        self.assertEqual(Decimal(str(cur['total_sales_cash'])), Decimal('0'))
        self.assertEqual(Decimal(str(cur['expected_in_drawer'])), Decimal('500.00'))

    def test_paid_in_and_out_move_the_expected_figure(self):
        self._sell('100.00')
        self.client.post(
            '/api/shifts/movement',
            {'type': 'paid_in', 'amount': '40.00', 'category': 'Float'},
            content_type='application/json', **self.auth,
        )
        self.client.post(
            '/api/shifts/movement',
            {'type': 'paid_out', 'amount': '15.00', 'category': 'Milk'},
            content_type='application/json', **self.auth,
        )
        cur = self._current()
        # 500 start + 100 sale + 40 in - 15 out
        self.assertEqual(Decimal(str(cur['expected_in_drawer'])), Decimal('625.00'))

    def test_voiding_a_cash_bill_takes_it_back_out_of_the_drawer(self):
        order = self._sell('100.00')
        self._sell('60.00')
        res = self.client.put(
            f"/api/orders/{order['id']}/status",
            {'status': 'cancel'},
            content_type='application/json', **self.auth,
        )
        self.assertEqual(res.status_code, 200, res.content)
        cur = self._current()
        self.assertEqual(Decimal(str(cur['total_sales_cash'])), Decimal('60.00'))
        self.assertEqual(Decimal(str(cur['expected_in_drawer'])), Decimal('560.00'))

    def test_close_agrees_with_the_printed_slip(self):
        """expected_in_drawer and the slip's cash_sales must use one order set.

        ``shift_close`` used to count cancelled cash bills in the expected
        figure while the slip's ``cash_sales`` line excluded them, so a round
        with a void printed a slip that did not add up.
        """
        order = self._sell('100.00')
        self._sell('60.00')
        self.client.put(
            f"/api/orders/{order['id']}/status",
            {'status': 'cancel'},
            content_type='application/json', **self.auth,
        )
        res = self.client.put(
            '/api/shifts/close',
            {'actual_in_drawer': '560.00', 'closed_by': 'Nok'},
            content_type='application/json', **self.auth,
        )
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        summary = body['summary']
        self.assertEqual(Decimal(str(summary['cash_sales'])), Decimal('60.00'))
        self.assertEqual(Decimal(str(summary['expected_in_drawer'])), Decimal('560.00'))
        # start + cash_sales + in - out must reconcile to the printed expected.
        self.assertEqual(
            Decimal(str(summary['start_cash']))
            + Decimal(str(summary['cash_sales']))
            + Decimal(str(summary['paid_in']))
            - Decimal(str(summary['paid_out'])),
            Decimal(str(summary['expected_in_drawer'])),
        )
        self.assertEqual(Decimal(str(summary['difference'])), Decimal('0'))

    def test_backoffice_sees_unreconciled_cash_on_an_open_shift(self):
        """The stale-drawer tile reads the stored column straight off the row."""
        self._sell('100.00')
        self.shift.refresh_from_db()
        self.assertEqual(self.shift.total_sales_cash, Decimal('100.00'))
        self.assertEqual(self.shift.expected_in_drawer, Decimal('600.00'))

    def test_sale_is_stamped_against_the_open_shift(self):
        data = self._sell('100.00')
        self.assertEqual(
            str(Order.objects.get(id=data['id']).shift_id), str(self.shift.id),
        )

    def test_no_open_shift_does_not_break_checkout(self):
        Shift.objects.filter(id=self.shift.id).update(status='closed')
        self._sell('100.00')   # must not raise on the None shift
