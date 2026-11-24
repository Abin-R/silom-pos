"""``stock_pool``: what Grab should show.

    grab_target = max(0, stock - self_held - counter_stock)

A Grab order is deducted from stock when it is accepted, so Grab needs no
reservation; self-order's own menu maths are unchanged (``test_selforder``).

Run:
    python manage.py test bravepos.tests.test_stock_pool \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from django.test import TestCase

from bravepos import selforder, stock_pool
from bravepos.models import SelfOrder, StockMovement

from .factories import StubGateway, make_branch, make_product, make_shop, open_shift


def hold_self(branch, product, qty):
    draft = selforder.create_draft(branch, [{'product_id': str(product.id), 'qty': qty}])
    with StubGateway():
        selforder.start_payment(draft, 'qr', redirect_url='https://x/')
    return SelfOrder.objects.get(pk=draft.pk)


class PoolTestCase(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch()
        self.branch.grab_merchant_id = '3-C2M2RUXENZCZGT'
        self.branch.save()
        open_shift(self.branch)

    def product(self, stock=10, counter=3, name='Latte', barcode='8812'):
        p = make_product(self.branch, name=name, stock=stock, counter_stock=counter)
        p.barcode = barcode
        p.save()
        return p

    def row(self, product):
        return stock_pool.availability(self.branch)[str(product.id)]


class TargetTests(PoolTestCase):
    def test_grab_gets_stock_less_the_counter_reserve(self):
        p = self.product(stock=10, counter=3)
        self.assertEqual(self.row(p)['grab_target'], 7)

    def test_it_never_goes_below_zero(self):
        p = self.product(stock=2, counter=3)
        self.assertEqual(self.row(p)['grab_target'], 0)
        p.stock = -4
        p.save()
        self.assertEqual(self.row(p)['grab_target'], 0)

    def test_a_live_self_order_hold_comes_off_grabs_number(self):
        p = self.product(stock=10, counter=3)
        hold_self(self.branch, p, 2)
        row = self.row(p)
        self.assertEqual((row['self_held'], row['grab_target']), (2, 5))

    def test_only_barcoded_active_products_are_listed(self):
        p = self.product()
        make_product(self.branch, name='Loose', stock=5)
        gone = self.product(name='Gone', barcode='9')
        gone.active = False
        gone.save()
        self.assertEqual(set(stock_pool.availability(self.branch)), {str(p.id)})


class TargetChangedAtTests(PoolTestCase):
    def test_it_reports_movements_and_self_order_holds(self):
        a = self.product(name='A', barcode='A')
        c = self.product(name='C', barcode='C')
        d = self.product(name='D', barcode='D')
        StockMovement.objects.create(branch=self.branch, product=a, type='in', qty=1)
        hold_self(self.branch, c, 1)
        changed = stock_pool.target_changed_at(self.branch, [a, c, d])
        self.assertEqual(set(changed), {'A', 'C'})
