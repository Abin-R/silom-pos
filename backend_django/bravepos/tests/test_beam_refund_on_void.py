"""Voiding a Beam-paid bill refunds it through Beam.

Only bills rung up after the feature shipped carry ``refund_on_void``; older
Beam bills void exactly as before, without touching the gateway.  The refund
goes first and the void lands only if Beam accepts it.
"""
from __future__ import annotations

from unittest import mock

from django.test import TestCase

from bravepos import gateways
from bravepos.models import BranchSession, Order, Staff

from .factories import make_branch, make_product, make_shop, open_shift


class BeamRefundOnVoidTests(TestCase):
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
        open_shift(self.branch)
        self.calls = []

    # ── helpers ─────────────────────────────────────────────────────────────
    def _sell(self, method='Beam QR', **gateway_ids):
        res = self.client.post(
            '/api/orders',
            {
                'items': [{
                    'product_id': str(self.product.id), 'name': self.product.name,
                    'qty': 1, 'price': '100.00', 'total': '100.00',
                }],
                'payment_method': method,
                'subtotal': '100.00', 'total': '100.00', 'paid_amount': '100.00',
                'status': 'completed',
                **gateway_ids,
            },
            content_type='application/json', **self.auth,
        )
        self.assertEqual(res.status_code, 201, res.content)
        return Order.objects.get(id=res.json()['id'])

    def _set_status(self, order, status):
        return self.client.put(
            f'/api/orders/{order.id}/status', {'status': status},
            content_type='application/json', **self.auth,
        )

    def _beam(self, responses):
        """Stub the Beam HTTP layer: record each call, answer from ``responses``."""
        def fake(method, path, *, cfg, json=None, timeout):
            self.calls.append((method, path, json))
            res = responses[(method, path)]
            if isinstance(res, Exception):
                raise res
            return res
        return mock.patch.object(gateways, '_beam_request', side_effect=fake)

    # ── tests ───────────────────────────────────────────────────────────────
    def test_voiding_a_beam_qr_bill_refunds_the_charge(self):
        order = self._sell(beam_charge_id='ch_1')
        with self._beam({('POST', '/api/v1/refunds'): {'refundId': 'rf_1'}}):
            res = self._set_status(order, 'cancel')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['beam_refund_id'], 'rf_1')
        self.assertEqual(self.calls[0][2]['chargeId'], 'ch_1')
        self.assertNotIn('amount', self.calls[0][2])  # full refund
        order.refresh_from_db()
        self.assertEqual(order.status, 'cancel')

    def test_card_bill_without_charge_id_looks_it_up_from_the_link(self):
        order = self._sell(method='Beam Card', beam_link_id='lk_1')
        with self._beam({
            ('GET', '/api/v1/payment-links/lk_1'): {'status': 'PAID', 'chargeId': 'ch_9'},
            ('POST', '/api/v1/refunds'): {'refundId': 'rf_9'},
        }):
            res = self._set_status(order, 'cancel')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.calls[-1][2]['chargeId'], 'ch_9')

    def test_a_rejected_refund_leaves_the_bill_unvoided(self):
        order = self._sell(beam_charge_id='ch_1')
        err = gateways.GatewayError('Beam API error 422: REFUND_PENDING', 502)
        with self._beam({('POST', '/api/v1/refunds'): err}):
            res = self._set_status(order, 'cancel')
        self.assertEqual(res.status_code, 502)
        self.assertIn('REFUND_PENDING', res.json()['detail'])
        order.refresh_from_db()
        self.assertEqual(order.status, 'completed')
        self.assertEqual(order.beam_refund_id, '')

    def test_revoiding_does_not_refund_twice_and_unvoid_is_refused(self):
        order = self._sell(beam_charge_id='ch_1')
        with self._beam({('POST', '/api/v1/refunds'): {'refundId': 'rf_1'}}):
            self._set_status(order, 'cancel')
            self.assertEqual(self._set_status(order, 'cancel').status_code, 200)
            res = self._set_status(order, 'completed')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(res.status_code, 409)
        order.refresh_from_db()
        self.assertEqual(order.status, 'cancel')

    def test_older_beam_bills_void_without_a_refund(self):
        order = self._sell(beam_charge_id='ch_1')
        Order.objects.filter(id=order.id).update(refund_on_void=False)  # pre-feature row
        with self._beam({}):
            res = self._set_status(order, 'cancel')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.calls, [])

    def test_cash_bills_never_touch_beam(self):
        order = self._sell(method='Cash')
        self.assertFalse(order.refund_on_void)
        with self._beam({}):
            self.assertEqual(self._set_status(order, 'cancel').status_code, 200)
        self.assertEqual(self.calls, [])
