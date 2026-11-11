"""Refund due: self-orders paid after their time ran out.

No SeaTalk alert any more; staff find them on the till (Orders › Refund due)
and on Backoffice › Transactions › Refund due, and settle each one by
refunding it through Beam, by recording a refund made by hand when Beam
refuses, or by making the order after all.
"""
from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from bravepos import gateways, permissions, selforder
from bravepos.models import AuditLog, BranchSession, Order, Product, SelfOrder, Staff

from .factories import StubGateway, make_branch, make_product, make_shop, open_shift


def late_draft(branch, product, qty=2):
    draft = selforder.create_draft(branch, [{'product_id': str(product.id), 'qty': qty}])
    with StubGateway():
        selforder.start_payment(draft, 'qr', redirect_url='https://x/')
    SelfOrder.objects.filter(pk=draft.pk).update(status='late_paid')
    return SelfOrder.objects.get(pk=draft.pk)


class TillRefundDueTests(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch()
        self.other = make_branch('Asok')
        open_shift(self.branch)
        self.p = make_product(self.branch, stock=5)
        self.draft = late_draft(self.branch, self.p)

    def _refund(self, draft, auth):
        return self.client.post(f'/api/self-orders/{draft.id}/refunded',
                                {'via': 'beam'}, content_type='application/json', **auth)

    def _session(self, perms):
        staff = Staff.objects.create(
            name='Nok', email=f'nok{Staff.objects.count()}@t.local', password_hash='x',
            role='cashier', permissions=perms,
        )
        staff.branches.add(self.branch)
        sess = BranchSession.objects.create(
            token=f'tok{staff.pk}'.ljust(36, 'x'), branch=self.branch, staff=staff)
        return {'HTTP_AUTHORIZATION': f'Bearer {sess.token}'}

    def test_everyone_sees_the_list(self):
        res = self.client.get('/api/self-orders/refund-due', **self._session([]))
        self.assertEqual(res.status_code, 200)
        [row] = res.json()
        self.assertEqual(row['reference'], f'SELF-{self.draft.token[:12]}')
        self.assertEqual(row['items'][0]['qty'], 2)

    def test_another_branchs_late_payment_is_not_listed(self):
        late_draft(self.other, make_product(self.other))
        res = self.client.get('/api/self-orders/refund-due', **self._session([]))
        self.assertEqual(len(res.json()), 1)

    def test_settling_needs_the_permission(self):
        auth = self._session(['manage_inventory'])
        for action in ('refunded', 'make-order'):
            res = self.client.post(f'/api/self-orders/{self.draft.id}/{action}', {'via': 'beam'},
                                   content_type='application/json', **auth)
            self.assertEqual(res.status_code, 403)
            self.assertEqual(res.json()['permission'], 'settle_late_payments')
        self.assertEqual(SelfOrder.objects.get(pk=self.draft.pk).status, 'late_paid')

    def test_refund_goes_back_through_beam(self):
        auth = self._session(['settle_late_payments'])
        with patch('bravepos.gateways.beam_refund_order', return_value='rf_1') as beam:
            res = self._refund(self.draft, auth)
        self.assertEqual(res.status_code, 200)
        [(draft,), kw] = beam.call_args
        self.assertEqual(draft.pk, self.draft.pk)
        self.assertIn('Nok', kw['reason'])
        d = SelfOrder.objects.get(pk=self.draft.pk)
        self.assertEqual((d.status, d.resolution, d.resolved_by, d.beam_refund_id),
                         ('refunded', 'refunded', 'Nok', 'rf_1'))
        self.assertEqual(res.json()['beam_refund_id'], 'rf_1')
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(Product.objects.get(pk=self.p.pk).stock, 5)
        self.assertTrue(AuditLog.objects.filter(model='SelfOrder', object_id=str(d.pk)).exists())
        self.assertEqual(self.client.get('/api/self-orders/refund-due', **auth).json(), [])

    def test_beam_refusing_records_nothing(self):
        auth = self._session(['settle_late_payments'])
        err = gateways.GatewayError('Max refundable amount is 0.', 502)
        with patch('bravepos.gateways.beam_refund_order', side_effect=err):
            res = self._refund(self.draft, auth)
        self.assertEqual(res.status_code, 502)
        self.assertTrue(res.json()['refund_failed'])
        self.assertIn('Max refundable', res.json()['detail'])
        d = SelfOrder.objects.get(pk=self.draft.pk)
        self.assertEqual((d.status, d.resolution, d.beam_refund_id), ('late_paid', '', ''))

    def test_refunded_by_hand_skips_beam(self):
        # Also what a till on an older bundle sends (no body): its button
        # promised "this only records it".
        auth = self._session(['settle_late_payments'])
        with patch('bravepos.gateways.beam_refund_order') as beam:
            res = self.client.post(f'/api/self-orders/{self.draft.id}/refunded', **auth)
        beam.assert_not_called()
        self.assertEqual(res.status_code, 200)
        d = SelfOrder.objects.get(pk=self.draft.pk)
        self.assertEqual((d.status, d.beam_refund_id), ('refunded', ''))

    def test_make_order_is_a_sale_that_prints(self):
        auth = self._session(['settle_late_payments'])
        res = self.client.post(f'/api/self-orders/{self.draft.id}/make-order', **auth)
        self.assertEqual(res.status_code, 200)
        d = SelfOrder.objects.get(pk=self.draft.pk)
        self.assertEqual((d.status, d.resolution), ('paid', 'ordered'))
        self.assertEqual(res.json()['order_number'], d.order.order_number)
        self.assertEqual(Product.objects.get(pk=self.p.pk).stock, 3)
        self.assertIsNone(d.printed_at)     # the till's print poll picks it up

    def test_it_can_only_be_settled_once(self):
        auth = self._session(['settle_late_payments'])
        with patch('bravepos.gateways.beam_refund_order', return_value='rf_1') as beam:
            self._refund(self.draft, auth)
            res = self.client.post(f'/api/self-orders/{self.draft.id}/make-order', **auth)
            again = self._refund(self.draft, auth)
        self.assertEqual(beam.call_count, 1)      # never refunded twice
        self.assertEqual(again.status_code, 409)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(Order.objects.count(), 0)

    def test_another_branchs_late_payment_cannot_be_settled_here(self):
        far = late_draft(self.other, make_product(self.other))
        auth = self._session(['settle_late_payments'])
        with patch('bravepos.gateways.beam_refund_order') as beam:
            res = self._refund(far, auth)
        self.assertEqual(res.status_code, 404)
        beam.assert_not_called()


class PermissionTests(TestCase):
    def test_admins_hold_it_by_default_and_cashiers_do_not(self):
        self.assertIn('settle_late_payments', permissions.defaults_for('admin'))
        self.assertNotIn('settle_late_payments', permissions.defaults_for('cashier'))


class BackofficeRefundDueTests(TestCase):
    password = 'correct-horse-battery'

    def setUp(self):
        make_shop()
        self.branch = make_branch()
        open_shift(self.branch)
        self.p = make_product(self.branch, stock=5)
        self.draft = late_draft(self.branch, self.p)
        user = Staff(name='Boss', username='boss', email='boss@x.invalid', role='admin',
                     active=True, backoffice_access=True)
        user.set_password(self.password)
        user.save()
        self.assertTrue(self.client.login(username='boss', password=self.password))

    def test_badge_and_list(self):
        page = self.client.get(reverse('backoffice:transactions'), {'status': 'refund_due'})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, f'SELF-{self.draft.token[:12]}')
        self.assertContains(page, 'class="alert"')
        # The ordinary bill list still renders.
        self.assertEqual(self.client.get(reverse('backoffice:transactions')).status_code, 200)

    def test_refund_button(self):
        url = reverse('backoffice:late_payment_settle', args=[self.draft.id, 'refunded'])
        with patch('bravepos.gateways.beam_refund_order', return_value='rf_9'):
            res = self.client.post(url)
        self.assertEqual(res.status_code, 302)
        d = SelfOrder.objects.get(pk=self.draft.pk)
        self.assertEqual((d.status, d.resolved_by, d.beam_refund_id), ('refunded', 'Boss', 'rf_9'))
        page = self.client.get(reverse('backoffice:transactions'), {'status': 'refund_due'})
        self.assertContains(page, 'Nothing to refund')
        self.assertContains(page, 'Refunded via Beam')
        self.assertNotContains(page, 'class="alert"')

    def test_beam_refusing_keeps_it_open(self):
        url = reverse('backoffice:late_payment_settle', args=[self.draft.id, 'refunded'])
        err = gateways.GatewayError('Max refundable amount is 0.', 502)
        with patch('bravepos.gateways.beam_refund_order', side_effect=err):
            page = self.client.post(url, follow=True)
        self.assertContains(page, 'Beam did not refund it')
        self.assertEqual(SelfOrder.objects.get(pk=self.draft.pk).status, 'late_paid')

    def test_refunded_by_hand_button(self):
        url = reverse('backoffice:late_payment_settle', args=[self.draft.id, 'refunded-by-hand'])
        with patch('bravepos.gateways.beam_refund_order') as beam:
            self.client.post(url)
        beam.assert_not_called()
        self.assertEqual(SelfOrder.objects.get(pk=self.draft.pk).status, 'refunded')
        page = self.client.get(reverse('backoffice:transactions'), {'status': 'refund_due'})
        self.assertContains(page, 'Refunded by hand')

    def test_make_order_button(self):
        url = reverse('backoffice:late_payment_settle', args=[self.draft.id, 'make-order'])
        self.client.post(url)
        d = SelfOrder.objects.get(pk=self.draft.pk)
        self.assertEqual(d.status, 'paid')
        self.assertEqual(Order.objects.count(), 1)

    def test_get_does_not_settle(self):
        url = reverse('backoffice:late_payment_settle', args=[self.draft.id, 'refunded'])
        self.client.get(url)
        self.assertEqual(SelfOrder.objects.get(pk=self.draft.pk).status, 'late_paid')
