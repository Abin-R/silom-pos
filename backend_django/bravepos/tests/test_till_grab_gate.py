"""The till gate: units held for a Grab order are not the till's to sell.

``POST /api/grab/till-check`` answers before payment; ``create_order_from_items``
checks again under the branch lock, and a sale that already has its money is
saved anyway, with a ``till_took_held`` review on the Grab order.

Run:
    python manage.py test bravepos.tests.test_till_grab_gate \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

import os
from datetime import timedelta
from unittest import mock, skipUnless

from django.db import connection
from django.test import TestCase
from django.utils import timezone as djtz

from bravepos.models import (
    BranchSession, GrabOrderHold, GrabSyncStatus, Order, Staff, StockMovement,
)

from .factories import make_branch, make_product, make_shop, open_shift

MERCHANT = '3-C2M2RUXENZCZGT'
CHECK = '/api/grab/till-check'


class GateTestCase(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch()
        self.branch.grab_merchant_id = MERCHANT
        self.branch.save()
        open_shift(self.branch)
        self.latte = make_product(self.branch, name='Latte', stock=5)
        self.latte.barcode = '8812'
        self.latte.save()
        self.cake = make_product(self.branch, name='Cake', stock=5)
        staff = Staff.objects.create(
            name='Nok', email='nok@test.local', password_hash='x', role='cashier')
        staff.branches.add(self.branch)
        session = BranchSession.objects.create(
            token='tok' * 12, branch=self.branch, staff=staff)
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {session.token}'}

    def grab_hold(self, qty, product=None, order_id='G1'):
        product = product or self.latte
        return GrabOrderHold.objects.create(
            branch=self.branch, order_id=order_id, display_id='GF-1',
            items=[{'product_id': str(product.id), 'barcode': product.barcode,
                    'qty': qty, 'name': product.name}])

    def check(self, items, free=None):
        return self.client.post(CHECK, {
            'items': [{'product_id': str(p.id), 'qty': q} for p, q in items],
            'free_items': [{'product_id': str(p.id), 'qty': q} for p, q in (free or [])],
        }, content_type='application/json', **self.auth)

    def sell(self, product, qty):
        return self.client.post('/api/orders', {
            'items': [{'product_id': str(product.id), 'name': product.name, 'qty': qty,
                       'price': '100.00', 'total': str(100 * qty)}],
            'payment_method': 'Cash', 'subtotal': str(100 * qty), 'total': str(100 * qty),
            'paid_amount': str(100 * qty), 'status': 'completed',
        }, content_type='application/json', **self.auth)


class TillCheckTests(GateTestCase):
    def test_it_needs_a_till_session(self):
        res = self.client.post(CHECK, {'items': []}, content_type='application/json')
        self.assertEqual(res.status_code, 401)

    def test_no_holds_means_ok(self):
        res = self.check([(self.latte, 5)])
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {'ok': True, 'blocked': []})

    def test_it_blocks_units_held_for_grab(self):
        self.grab_hold(2)
        body = self.check([(self.latte, 4)]).json()
        self.assertFalse(body['ok'])
        self.assertEqual(body['blocked'], [{
            'product_id': str(self.latte.id), 'name': 'Latte',
            'wanted': 4, 'can_sell': 3, 'held_for_grab': 2}])

    def test_the_till_ignores_the_reserve_and_self_order_holds(self):
        self.latte.counter_stock = 3
        self.latte.save()
        self.grab_hold(2)
        self.assertTrue(self.check([(self.latte, 3)]).json()['ok'])    # 5 - 2 = 3

    def test_selling_exactly_what_is_left_is_fine(self):
        self.grab_hold(2)
        self.assertTrue(self.check([(self.latte, 3)]).json()['ok'])
        self.assertFalse(self.check([(self.latte, 4)]).json()['ok'])

    def test_other_products_pass(self):
        self.grab_hold(5)
        res = self.check([(self.cake, 5)]).json()
        self.assertEqual(res, {'ok': True, 'blocked': []})

    def test_a_product_with_no_hold_passes_even_when_the_till_would_oversell(self):
        self.grab_hold(1)
        self.assertTrue(self.check([(self.cake, 50)]).json()['ok'])

    def test_free_items_count_with_the_paid_lines(self):
        self.grab_hold(2)
        body = self.check([(self.latte, 2)], free=[(self.latte, 2)]).json()
        self.assertFalse(body['ok'])
        self.assertEqual(body['blocked'][0]['wanted'], 4)

    def test_an_outage_subtracts_what_grab_still_shows(self):
        GrabSyncStatus.objects.create(
            branch=self.branch, push_live=True, grab_shown={'8812': 4},
            last_ok_at=djtz.now() - timedelta(minutes=9))
        body = self.check([(self.latte, 2)]).json()
        self.assertFalse(body['ok'])
        self.assertEqual((body['blocked'][0]['can_sell'], body['blocked'][0]['held_for_grab']), (1, 4))

    def test_released_or_converted_orders_do_not_block(self):
        hold = self.grab_hold(5)
        GrabOrderHold.objects.filter(pk=hold.pk).update(status='converted')
        self.assertTrue(self.check([(self.latte, 5)]).json()['ok'])

    def test_a_branch_without_a_merchant_id_is_never_blocked(self):
        self.grab_hold(5)
        self.branch.grab_merchant_id = ''
        self.branch.save()
        self.assertTrue(self.check([(self.latte, 5)]).json()['ok'])

    def test_junk_lines_are_ignored_and_a_bad_list_is_400(self):
        res = self.client.post(CHECK, {'items': [{'product_id': 'nope', 'qty': 1}, 5,
                                                 {'product_id': str(self.latte.id), 'qty': 0}]},
                               content_type='application/json', **self.auth)
        self.assertEqual(res.json(), {'ok': True, 'blocked': []})
        res = self.client.post(CHECK, {'items': 'x'}, content_type='application/json', **self.auth)
        self.assertEqual(res.status_code, 400)

    def test_another_branchs_product_is_not_checked(self):
        other = make_branch(name='Paragon')
        theirs = make_product(other, name='Latte', stock=1)
        self.assertEqual(self.check([(theirs, 9)]).json(), {'ok': True, 'blocked': []})


class SaveAfterLateHoldTests(GateTestCase):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {'SEATALK_GRAB_GROUP_ID': 'grp-1'})
        env.start()
        self.addCleanup(env.stop)
        sender = mock.patch('bravepos.seatalk.send_group_text_async', return_value=True)
        self.sent = sender.start()
        self.addCleanup(sender.stop)

    def test_a_sale_that_fits_saves_with_no_review(self):
        self.grab_hold(2)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.sell(self.latte, 3).status_code, 201)
        self.assertFalse(GrabOrderHold.objects.filter(review_status='open').exists())

    def test_a_sale_that_takes_held_units_is_saved_and_opens_a_review(self):
        # The hold landed after the till's check: the customer has paid.
        hold = self.grab_hold(2)
        with self.captureOnCommitCallbacks(execute=True):
            res = self.sell(self.latte, 4)
        self.assertEqual(res.status_code, 201)
        order = Order.objects.get()
        self.latte.refresh_from_db()
        self.assertEqual(self.latte.stock, 1)
        self.assertTrue(StockMovement.objects.filter(document_no=order.order_number).exists())
        hold.refresh_from_db()
        self.assertEqual((hold.review_status, hold.review_reason), ('open', 'till_took_held'))
        self.assertEqual(hold.status, 'held')              # the hold itself stands
        self.assertEqual(hold.review_detail['order_number'], order.order_number)
        self.assertEqual(self.sent.call_count, 1)
        self.assertIn('GF-1', self.sent.call_args.args[1])

    def test_the_review_goes_on_the_newest_order_holding_the_product(self):
        self.grab_hold(1, order_id='OLD')
        GrabOrderHold.objects.filter(order_id='OLD').update(
            first_seen_at=djtz.now() - timedelta(hours=1))
        self.grab_hold(4, order_id='NEW')
        with self.captureOnCommitCallbacks(execute=True):
            self.sell(self.latte, 5)
        self.assertEqual(GrabOrderHold.objects.get(order_id='NEW').review_reason, 'till_took_held')
        self.assertEqual(GrabOrderHold.objects.get(order_id='OLD').review_status, '')

    def test_a_free_item_that_takes_held_units_also_opens_one(self):
        hold = self.grab_hold(5)
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post('/api/orders', {
                'items': [{'product_id': str(self.cake.id), 'name': 'Cake', 'qty': 1,
                           'price': '100.00', 'total': '100.00'}],
                'free_items': [{'product_id': str(self.latte.id), 'name': 'Latte', 'qty': 1}],
                'payment_method': 'Cash', 'subtotal': '100.00', 'total': '100.00',
                'paid_amount': '100.00', 'status': 'completed',
            }, content_type='application/json', **self.auth)
        self.assertEqual(res.status_code, 201)
        hold.refresh_from_db()
        self.assertEqual(hold.review_reason, 'till_took_held')

    def test_a_failure_in_the_gate_never_loses_the_sale(self):
        self.grab_hold(2)
        with mock.patch('bravepos.stock_pool.till_shortfalls', side_effect=RuntimeError('boom')):
            res = self.sell(self.latte, 5)
        self.assertEqual(res.status_code, 201)
        self.assertEqual(Order.objects.count(), 1)

    def test_a_failure_opening_the_review_never_loses_the_sale(self):
        self.grab_hold(2)
        with mock.patch('bravepos.grab_sync.open_review', side_effect=RuntimeError('boom')), \
                self.captureOnCommitCallbacks(execute=True):
            res = self.sell(self.latte, 5)
        self.assertEqual(res.status_code, 201)
        self.assertEqual(Order.objects.count(), 1)

    def test_a_branch_without_grab_is_untouched(self):
        self.branch.grab_merchant_id = ''
        self.branch.save()
        self.grab_hold(5)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.sell(self.latte, 5).status_code, 201)
        self.assertFalse(GrabOrderHold.objects.filter(review_status='open').exists())

    def test_the_review_waits_for_the_commit_and_the_alert_is_sent_in_the_background(self):
        self.grab_hold(2)
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            self.assertEqual(self.sell(self.latte, 4).status_code, 201)
        self.assertFalse(GrabOrderHold.objects.filter(review_status='open').exists())
        self.assertEqual(self.sent.call_count, 0)
        for callback in callbacks:
            callback()
        self.assertTrue(GrabOrderHold.objects.filter(review_status='open').exists())

    def test_a_self_order_sale_is_named_as_one(self):
        from bravepos import grab_sync
        hold = self.grab_hold(2)
        with self.captureOnCommitCallbacks(execute=True):
            grab_sync.till_took_held(
                self.branch, 'PS000001',
                [{'product_id': str(self.latte.id), 'wanted': 4, 'can_sell': 3,
                  'held_for_grab': 2}], 'self_order')
        hold.refresh_from_db()
        self.assertEqual(hold.review_detail['source'], 'self_order')
        self.assertIn('self-order', self.sent.call_args.args[1].lower())
        self.assertNotIn('The till sold', self.sent.call_args.args[1])
        self.assertIn('self-order', grab_sync.review_text('till_took_held', hold.review_detail).lower())

    def test_a_counter_sale_is_still_named_as_the_till(self):
        from bravepos import grab_sync
        hold = self.grab_hold(2)
        with self.captureOnCommitCallbacks(execute=True):
            self.sell(self.latte, 4)
        hold.refresh_from_db()
        self.assertEqual(hold.review_detail['source'], 'counter')
        self.assertIn('The till sold', grab_sync.review_text('till_took_held', hold.review_detail))
        self.assertIn('The till sold', self.sent.call_args.args[1])


class DeadlockTests(GateTestCase):
    """A deadlock rolls the sale back whole.  It is retried, and if it keeps
    happening the error is raised — never a half-made sale."""

    def deadlock(self):
        from django.db import OperationalError
        from psycopg.errors import DeadlockDetected
        error = OperationalError('deadlock detected')
        # psycopg 3 exposes the PostgreSQL state as ``sqlstate``, not pgcode.
        error.__cause__ = DeadlockDetected()
        return error

    def create(self):
        from bravepos.orders import create_order_from_items
        return create_order_from_items(
            branch=self.branch, source='counter',
            items=[{'product_id': str(self.latte.id), 'name': 'Latte', 'qty': 1, 'price': '100'}],
            goods_total=100, subtotal=100, payment_method='Cash', paid_amount=100)

    def test_a_deadlock_is_retried_and_the_sale_is_made_once(self):
        real = Order.objects.create
        calls = []

        def flaky(**kw):
            calls.append(1)
            if len(calls) == 1:
                raise self.deadlock()
            return real(**kw)

        with mock.patch.object(Order.objects, 'create', side_effect=flaky):
            order = self.create()
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(order.pk, Order.objects.get().pk)
        self.latte.refresh_from_db()
        self.assertEqual(self.latte.stock, 4)              # taken out once
        self.assertEqual(StockMovement.objects.filter(document_no=order.order_number).count(), 1)

    def test_repeated_deadlocks_raise_and_leave_nothing_behind(self):
        from django.db import OperationalError
        with mock.patch.object(Order.objects, 'create', side_effect=self.deadlock()):
            with self.assertRaises(OperationalError), self.assertLogs('bravepos', 'ERROR'):
                self.create()
        self.assertEqual(Order.objects.count(), 0)
        self.latte.refresh_from_db()
        self.assertEqual(self.latte.stock, 5)
        self.assertFalse(StockMovement.objects.exists())

    def test_an_operational_error_that_is_not_a_deadlock_is_not_retried(self):
        from django.db import OperationalError
        with mock.patch.object(Order.objects, 'create',
                               side_effect=OperationalError('disk full')) as create:
            with self.assertRaises(OperationalError):
                self.create()
        self.assertEqual(create.call_count, 1)

    def test_the_psycopg2_pgcode_fallback_is_still_retried(self):
        from django.db import OperationalError
        error = OperationalError('deadlock detected')
        error.__cause__ = type('Psycopg2Deadlock', (Exception,), {'pgcode': '40P01'})()
        real = Order.objects.create
        calls = []

        def flaky(**kw):
            calls.append(1)
            if len(calls) == 1:
                raise error
            return real(**kw)

        with mock.patch.object(Order.objects, 'create', side_effect=flaky):
            self.create()
        self.assertEqual(calls, [1, 1])


@skipUnless(connection.vendor == 'postgresql', 'row locks need PostgreSQL')
class ProductLockOrderTests(GateTestCase):
    def test_the_sale_locks_its_products_in_id_order_before_changing_them(self):
        from django.db import connection as conn
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(conn) as ctx:
            self.assertEqual(self.client.post('/api/orders', {
                'items': [{'product_id': str(p.id), 'name': p.name, 'qty': 1,
                           'price': '10', 'total': '10'} for p in (self.latte, self.cake)],
                'payment_method': 'Cash', 'subtotal': '20', 'total': '20',
                'paid_amount': '20', 'status': 'completed',
            }, content_type='application/json', **self.auth).status_code, 201)
        sql = [q['sql'] for q in ctx.captured_queries]
        locks = [i for i, q in enumerate(sql)
                 if 'FROM "bravepos_product"' in q and 'FOR UPDATE' in q and 'ORDER BY' in q]
        updates = [i for i, q in enumerate(sql) if q.startswith('UPDATE "bravepos_product"')]
        self.assertTrue(locks, 'no ordered product lock query')
        self.assertTrue(updates)
        self.assertLess(locks[0], updates[0])
        self.assertIn('ORDER BY "bravepos_product"."id"', sql[locks[0]])
