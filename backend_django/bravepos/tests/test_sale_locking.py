"""A till sale locks its products in id order, and a database deadlock rolls
the sale back whole and tries it again.

The Grab sync deducts accepted Grab orders by locking the same product rows in
the same order, and stock documents do too, so a sale and a deduction cannot
wait on each other in a circle.

Run:
    python manage.py test bravepos.tests.test_sale_locking \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from unittest import mock, skipUnless

from django.db import connection
from django.test import TestCase

from bravepos.models import BranchSession, Order, Staff, StockMovement

from .factories import make_branch, make_product, make_shop, open_shift


class SaleTestCase(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch()
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

    def sell(self, product, qty):
        return self.client.post('/api/orders', {
            'items': [{'product_id': str(product.id), 'name': product.name, 'qty': qty,
                       'price': '100.00', 'total': str(100 * qty)}],
            'payment_method': 'Cash', 'subtotal': str(100 * qty), 'total': str(100 * qty),
            'paid_amount': str(100 * qty), 'status': 'completed',
        }, content_type='application/json', **self.auth)


class DeadlockTests(SaleTestCase):
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
class ProductLockOrderTests(SaleTestCase):
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
