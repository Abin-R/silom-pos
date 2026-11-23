"""``stock_pool``: what the till, self-order and Grab may each sell.

    grab_target      = max(0, stock - self_held - grab_held - counter_stock)
    self-order sells = stock - self_held - grab_held - outage_shown - counter_stock
    till sells       = stock - grab_held - outage_shown

Run:
    python manage.py test bravepos.tests.test_stock_pool \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone as djtz

from bravepos import selforder, stock_pool
from bravepos.models import GrabOrderHold, GrabSyncStatus, SelfOrder

from .factories import StubGateway, make_branch, make_product, make_shop, open_shift

MERCHANT = '3-C2M2RUXENZCZGT'


def hold_grab(branch, product, qty, status='held', order_id=None):
    return GrabOrderHold.objects.create(
        branch=branch, order_id=order_id or f'G{GrabOrderHold.objects.count()}',
        status=status,
        items=[{'product_id': str(product.id), 'barcode': product.barcode, 'qty': qty}],
    )


def hold_self(branch, product, qty):
    draft = selforder.create_draft(branch, [{'product_id': str(product.id), 'qty': qty}])
    with StubGateway():
        selforder.start_payment(draft, 'qr', redirect_url='https://x/')
    return SelfOrder.objects.get(pk=draft.pk)


class PoolTestCase(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch()
        self.branch.grab_merchant_id = MERCHANT
        self.branch.save()
        open_shift(self.branch)

    def product(self, stock=10, counter=3, name='Latte', barcode='8812'):
        p = make_product(self.branch, name=name, stock=stock, counter_stock=counter)
        p.barcode = barcode
        p.save()
        return p

    def row(self, product):
        return stock_pool.availability(self.branch)[str(product.id)]

    def outage(self, shown, **extra):
        fields = dict(push_live=True, last_ok_at=djtz.now() - timedelta(minutes=10),
                      grab_shown=shown)
        fields.update(extra)
        return GrabSyncStatus.objects.update_or_create(branch=self.branch, defaults=fields)[0]


class TargetTests(PoolTestCase):
    def test_worked_example_from_the_plan(self):
        # stock 10, reserve 3, a self-order hold of 1 and a known Grab hold of 2.
        p = self.product(stock=10, counter=3)
        hold_self(self.branch, p, 1)
        hold_grab(self.branch, p, 2)
        row = self.row(p)
        self.assertEqual((row['self_held'], row['grab_held']), (1, 2))
        self.assertEqual(row['grab_target'], 10 - 1 - 2 - 3)
        self.assertEqual(row['till_can_sell'], 10 - 2)

    def test_the_target_is_clamped_at_zero(self):
        p = self.product(stock=4, counter=3)
        hold_grab(self.branch, p, 5)
        self.assertEqual(self.row(p)['grab_target'], 0)
        q = self.product(stock=-2, counter=0, name='Neg', barcode='1')
        self.assertEqual(self.row(q)['grab_target'], 0)

    def test_a_reserve_of_zero_keeps_nothing_back(self):
        p = self.product(stock=7, counter=0)
        self.assertEqual(self.row(p)['grab_target'], 7)

    def test_only_held_rows_count(self):
        p = self.product(stock=10, counter=0)
        for n, status in enumerate(['held', 'converted', 'released', 'restored']):
            hold_grab(self.branch, p, 2, status=status, order_id=f'S{n}')
        self.assertEqual(self.row(p)['grab_held'], 2)

    def test_two_held_orders_add_up(self):
        p = self.product(stock=10, counter=0)
        hold_grab(self.branch, p, 2)
        hold_grab(self.branch, p, 3)
        self.assertEqual(self.row(p)['grab_held'], 5)

    def test_a_lapsed_self_order_hold_stops_counting(self):
        p = self.product(stock=10, counter=0)
        draft = hold_self(self.branch, p, 2)
        self.assertEqual(self.row(p)['self_held'], 2)
        SelfOrder.objects.filter(pk=draft.pk).update(hold_expires_at=djtz.now() - timedelta(seconds=1))
        self.assertEqual(self.row(p)['self_held'], 0)

    def test_a_branch_with_no_merchant_id_has_no_grab_holds(self):
        p = self.product()
        hold_grab(self.branch, p, 2)
        self.branch.grab_merchant_id = ''
        self.branch.save()
        self.assertEqual(self.row(p)['grab_held'], 0)
        self.assertEqual(self.row(p)['grab_target'], 7)


class OutageTests(PoolTestCase):
    def test_outage_shows_the_last_figure_only_when_push_is_live(self):
        p = self.product(stock=10, counter=3)
        self.outage({'8812': 4})
        row = self.row(p)
        self.assertEqual(row['outage_shown'], 4)
        self.assertEqual(row['till_can_sell'], 10 - 4)

    def test_no_outage_when_push_is_not_live(self):
        p = self.product()
        self.outage({'8812': 4}, push_live=False)
        self.assertEqual(self.row(p)['outage_shown'], 0)
        self.assertEqual(stock_pool.outage_shown(self.branch), {})

    def test_a_recent_good_cycle_is_not_an_outage(self):
        p = self.product()
        self.outage({'8812': 4}, last_ok_at=djtz.now() - timedelta(minutes=2))
        self.assertEqual(self.row(p)['outage_shown'], 0)

    def test_a_named_problem_is_an_outage_even_with_a_recent_cycle(self):
        p = self.product()
        self.outage({'8812': 4}, last_ok_at=djtz.now(), problem='login')
        self.assertEqual(self.row(p)['outage_shown'], 4)

    def test_no_status_row_means_no_outage(self):
        p = self.product()
        self.assertEqual(self.row(p)['outage_shown'], 0)

    def test_unknown_barcodes_and_zero_figures_are_ignored(self):
        self.product()
        self.outage({'9999': 3, '8812': 0, 'x': 'bad'})
        self.assertEqual(stock_pool.outage_shown(self.branch), {})


class SelfOrderIncludesGrabTests(PoolTestCase):
    def menu_item(self, p):
        return next((i for i in selforder.menu_for(self.branch) if i['id'] == str(p.id)), None)

    def test_held_quantities_is_self_plus_grab_plus_outage(self):
        p = self.product(stock=20, counter=0)
        hold_self(self.branch, p, 1)
        hold_grab(self.branch, p, 2)
        self.outage({'8812': 4})
        self.assertEqual(selforder.held_quantities(self.branch), {str(p.id): 1 + 2 + 4})

    def test_exclude_pk_leaves_the_drafts_own_hold_out_but_not_grabs(self):
        p = self.product(stock=20, counter=0)
        draft = hold_self(self.branch, p, 1)
        hold_grab(self.branch, p, 2)
        self.assertEqual(selforder.held_quantities(self.branch, exclude_pk=draft.pk),
                         {str(p.id): 2})

    def test_the_menu_stock_drops_by_the_grab_hold(self):
        p = self.product(stock=10, counter=3)
        self.assertEqual(self.menu_item(p)['stock'], 7)
        hold_grab(self.branch, p, 2)
        self.assertEqual(self.menu_item(p)['stock'], 5)

    def test_a_grab_hold_can_make_it_counter_only_or_sold_out(self):
        p = self.product(stock=6, counter=3)
        hold_grab(self.branch, p, 4)
        self.assertTrue(self.menu_item(p)['counter_only'])
        hold_grab(self.branch, p, 2)
        self.assertIsNone(self.menu_item(p))

    def test_paying_cannot_take_units_held_for_grab(self):
        p = self.product(stock=6, counter=0)
        hold_grab(self.branch, p, 5)
        with self.assertRaises(selforder.SelfOrderError):
            hold_self(self.branch, p, 2)
        self.assertEqual(hold_self(self.branch, p, 1).status, 'pending')

    def test_free_items_respect_grab_holds(self):
        p = self.product(stock=5, counter=0)
        items = [{'product_id': str(p.id), 'qty': 2, 'is_free': True}]
        self.assertEqual(selforder.free_item_stock(self.branch, items)[str(p.id)], 'ok')
        hold_grab(self.branch, p, 4)
        self.assertEqual(selforder.free_item_stock(self.branch, items)[str(p.id)], 'counter')
        hold_grab(self.branch, p, 1)
        self.assertEqual(selforder.free_item_stock(self.branch, items)[str(p.id)], 'sold_out')

    def test_an_outage_takes_what_grab_can_still_sell_off_the_menu(self):
        p = self.product(stock=10, counter=0)
        self.outage({'8812': 4})
        self.assertEqual(self.menu_item(p)['stock'], 6)


class TargetChangedAtTests(PoolTestCase):
    def test_it_reports_movements_grab_holds_and_self_order_holds(self):
        from bravepos.models import StockMovement
        a = self.product(name='A', barcode='A')
        b = self.product(name='B', barcode='B')
        c = self.product(name='C', barcode='C')
        d = self.product(name='D', barcode='D')
        StockMovement.objects.create(branch=self.branch, product=a, type='in', qty=1)
        hold_grab(self.branch, b, 1)
        hold_self(self.branch, c, 1)
        changed = stock_pool.target_changed_at(self.branch, [a, b, c, d])
        self.assertEqual(set(changed), {'A', 'B', 'C'})
