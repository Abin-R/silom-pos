"""Refund due: self-orders paid after their time ran out.

No SeaTalk alert any more; staff find them on the till (Orders › Refund due)
and on Backoffice › Transactions › Refund due, and settle each one as
refunded (recorded only, no Beam call) or by making the order after all.
"""
from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from bravepos import permissions, selforder
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
            res = self.client.post(f'/api/self-orders/{self.draft.id}/{action}', **auth)
            self.assertEqual(res.status_code, 403)
            self.assertEqual(res.json()['permission'], 'settle_late_payments')
        self.assertEqual(SelfOrder.objects.get(pk=self.draft.pk).status, 'late_paid')

    def test_refunded_only_records_it(self):
        auth = self._session(['settle_late_payments'])
        with patch('bravepos.gateways.beam_get_charge') as beam:
            res = self.client.post(f'/api/self-orders/{self.draft.id}/refunded', **auth)
        beam.assert_not_called()
        self.assertEqual(res.status_code, 200)
        d = SelfOrder.objects.get(pk=self.draft.pk)
        self.assertEqual((d.status, d.resolution, d.resolved_by), ('refunded', 'refunded', 'Nok'))
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(Product.objects.get(pk=self.p.pk).stock, 5)
        self.assertTrue(AuditLog.objects.filter(model='SelfOrder', object_id=str(d.pk)).exists())
        self.assertEqual(self.client.get('/api/self-orders/refund-due', **auth).json(), [])

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
        self.client.post(f'/api/self-orders/{self.draft.id}/refunded', **auth)
        res = self.client.post(f'/api/self-orders/{self.draft.id}/make-order', **auth)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(Order.objects.count(), 0)

    def test_another_branchs_late_payment_cannot_be_settled_here(self):
        far = late_draft(self.other, make_product(self.other))
        auth = self._session(['settle_late_payments'])
        res = self.client.post(f'/api/self-orders/{far.id}/refunded', **auth)
        self.assertEqual(res.status_code, 404)


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

    def test_refunded_button(self):
        url = reverse('backoffice:late_payment_settle', args=[self.draft.id, 'refunded'])
        res = self.client.post(url)
        self.assertEqual(res.status_code, 302)
        d = SelfOrder.objects.get(pk=self.draft.pk)
        self.assertEqual((d.status, d.resolved_by), ('refunded', 'Boss'))
        page = self.client.get(reverse('backoffice:transactions'), {'status': 'refund_due'})
        self.assertContains(page, 'Nothing to refund')
        self.assertNotContains(page, 'class="alert"')

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
