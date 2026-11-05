"""The till's two ways to cancel a bill.

Void: stock comes back, no money moves.  Refund: stock comes back and the
customer gets their money.  Both are ``status='cancel'``, so reports, the
drawer and the day's tax receipt treat them alike.
"""
from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from backoffice.views import _rewind_stock
from bravepos import gateways
from bravepos.models import (
    BranchSession, Order, Product, Staff, StockDocument, StockMovement,
)

from .factories import make_branch, make_product, make_shop, open_shift


class VoidAndRefundTests(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch()
        self.product = make_product(self.branch, price='100.00', stock=50)
        self.gift = make_product(self.branch, name='Cookie', price='30.00', stock=20)
        self.staff = Staff.objects.create(
            name='Nok', email='nok@test.local', password_hash='x', role='cashier',
        )
        self.staff.branches.add(self.branch)
        session = BranchSession.objects.create(
            token='tok' * 12, branch=self.branch, staff=self.staff,
        )
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {session.token}'}
        open_shift(self.branch)
        self.refund_calls = []

    def _sell(self, method='Cash', qty=2, **extra):
        res = self.client.post(
            '/api/orders',
            {
                'items': [{
                    'product_id': str(self.product.id), 'name': self.product.name,
                    'qty': qty, 'price': '100.00', 'total': '200.00',
                }],
                'payment_method': method,
                'subtotal': '200.00', 'total': '200.00', 'paid_amount': '200.00',
                'status': 'completed',
                **extra,
            },
            content_type='application/json', **self.auth,
        )
        self.assertEqual(res.status_code, 201, res.content)
        return Order.objects.get(id=res.json()['id'])

    def _cancel(self, order, cancel_type=None):
        body = {'status': 'cancel'}
        if cancel_type:
            body['cancel_type'] = cancel_type
        return self.client.put(
            f'/api/orders/{order.id}/status', body,
            content_type='application/json', **self.auth,
        )

    def _beam(self):
        def fake(method, path, *, cfg, json=None, timeout):
            if path == '/api/v1/refunds':
                self.refund_calls.append(json)
                return {'refundId': 'rf_1'}
            return {'status': 'SUCCEEDED', 'amount': 20000}
        return mock.patch.object(gateways, '_beam_request', side_effect=fake)

    def _stock(self, product=None):
        return Product.objects.get(pk=(product or self.product).pk).stock

    # ── the two buttons ─────────────────────────────────────────────────────
    def test_void_returns_stock_and_refunds_nothing(self):
        order = self._sell(method='Beam QR', beam_charge_id='ch_1')
        self.assertEqual(self._stock(), 48)
        with self._beam():
            res = self._cancel(order, 'void')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['status'], 'cancel')
        self.assertEqual(res.json()['cancel_type'], 'void')
        self.assertEqual(res.json()['beam_refund_id'], '')
        self.assertEqual(self.refund_calls, [])
        self.assertEqual(self._stock(), 50)

    def test_refund_returns_stock_and_refunds_through_beam(self):
        order = self._sell(method='Beam QR', beam_charge_id='ch_1')
        with self._beam():
            res = self._cancel(order, 'refund')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['cancel_type'], 'refund')
        self.assertEqual(res.json()['beam_refund_id'], 'rf_1')
        self.assertEqual(len(self.refund_calls), 1)
        self.assertEqual(self._stock(), 50)

    def test_refunding_a_cash_bill_touches_no_gateway(self):
        order = self._sell()
        with self._beam():
            res = self._cancel(order, 'refund')
        self.assertEqual(res.json()['cancel_type'], 'refund')
        self.assertEqual(self.refund_calls, [])
        self.assertEqual(self._stock(), 50)

    def test_a_refused_refund_still_returns_stock(self):
        order = self._sell(method='Beam QR', beam_charge_id='ch_1')
        err = gateways.GatewayError('max refundable amount (0)', 502)
        with mock.patch.object(gateways, 'beam_refund_order', side_effect=err):
            res = self._cancel(order, 'refund')
        self.assertEqual(res.json()['status'], 'cancel')
        self.assertIn('max refundable', res.json()['beam_refund_error'])
        self.assertEqual(self._stock(), 50)

    def test_a_refund_on_a_beam_bill_older_than_the_auto_refund_still_tries_beam(self):
        order = self._sell(method='Beam QR', beam_charge_id='ch_1')
        Order.objects.filter(id=order.id).update(refund_on_void=False)
        with self._beam():
            res = self._cancel(order, 'refund')
        self.assertEqual(res.json()['beam_refund_id'], 'rf_1')

    def test_an_unknown_cancel_type_is_refused_and_changes_nothing(self):
        order = self._sell()
        self.assertEqual(self._cancel(order, 'banana').status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.status, 'completed')
        self.assertEqual(self._stock(), 48)

    # ── the ledger ──────────────────────────────────────────────────────────
    def test_the_return_is_on_the_stock_ledger_and_is_not_a_stock_in(self):
        order = self._sell()
        self._cancel(order, 'void')
        move = StockMovement.objects.get(type='return')
        self.assertEqual(move.product, self.product)
        self.assertEqual(move.qty, 2)
        self.assertEqual(move.document_no, order.order_number)
        self.assertEqual(move.created_by, 'Nok')
        self.assertFalse(StockMovement.objects.filter(type='in').exists())

    def test_free_items_go_back_too(self):
        order = self._sell(free_items=[{'product_id': str(self.gift.id), 'qty': 3}])
        self.assertEqual(self._stock(self.gift), 17)
        self._cancel(order, 'void')
        self.assertEqual(self._stock(self.gift), 20)
        doc = StockDocument.objects.get(ref_no=order.order_number)
        self.assertTrue(StockMovement.objects.filter(
            type='return', product=self.gift, document_no=doc.document_no,
        ).exists())

    def test_rewinding_stock_sees_the_return(self):
        order = self._sell()
        self._cancel(order, 'void')
        yesterday = timezone.localdate() - timedelta(days=1)
        product = Product.objects.get(pk=self.product.pk)
        _rewind_stock([product], yesterday)
        self.assertEqual(product.stock, 50)

    # ── repeats and reversals ───────────────────────────────────────────────
    def test_a_double_tap_returns_stock_once(self):
        order = self._sell()
        self._cancel(order, 'void')
        self.assertEqual(self._cancel(order, 'void').status_code, 200)
        self.assertEqual(self._stock(), 50)
        self.assertEqual(StockMovement.objects.filter(type='return').count(), 1)

    def test_a_repeat_cannot_turn_a_void_into_a_refund(self):
        order = self._sell(method='Beam QR', beam_charge_id='ch_1')
        with self._beam():
            self._cancel(order, 'void')
            res = self._cancel(order, 'refund')
            self._cancel(order)  # and nor can an older till's repeat
        self.assertEqual(res.json()['cancel_type'], 'void')
        self.assertEqual(self.refund_calls, [])

    def test_unvoiding_takes_the_stock_out_again(self):
        order = self._sell(free_items=[{'product_id': str(self.gift.id), 'qty': 3}])
        self._cancel(order, 'void')
        res = self.client.put(
            f'/api/orders/{order.id}/status', {'status': 'completed'},
            content_type='application/json', **self.auth,
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['cancel_type'], '')
        self.assertEqual(self._stock(), 48)
        self.assertEqual(self._stock(self.gift), 17)
        self._cancel(order, 'refund')  # a second cancel returns it again
        self.assertEqual(self._stock(), 50)
        self.assertEqual(self._stock(self.gift), 20)

    def test_unvoiding_a_bill_that_never_returned_stock_takes_nothing(self):
        order = self._sell()
        Order.objects.filter(id=order.id).update(status='cancel', cancel_type='')
        self.client.put(
            f'/api/orders/{order.id}/status', {'status': 'completed'},
            content_type='application/json', **self.auth,
        )
        self.assertEqual(self._stock(), 48)

    def test_a_refunded_beam_bill_refused_an_unvoid_keeps_its_stock_back(self):
        order = self._sell(method='Beam QR', beam_charge_id='ch_1')
        with self._beam():
            self._cancel(order, 'refund')
        res = self.client.put(
            f'/api/orders/{order.id}/status', {'status': 'completed'},
            content_type='application/json', **self.auth,
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self._stock(), 50)

    # ── a till still on the old bundle ──────────────────────────────────────
    def test_an_older_till_refunds_a_beam_bill_and_voids_cash_as_before(self):
        beam = self._sell(method='Beam QR', beam_charge_id='ch_1')
        cash = self._sell()
        with self._beam():
            beam_res = self._cancel(beam)
            cash_res = self._cancel(cash)
        self.assertEqual(beam_res.json()['cancel_type'], 'refund')
        self.assertEqual(beam_res.json()['beam_refund_id'], 'rf_1')
        self.assertEqual(cash_res.json()['cancel_type'], 'void')
