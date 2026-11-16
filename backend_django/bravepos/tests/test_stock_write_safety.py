"""Every stock write holds a row lock and leaves a movement.

* The stock modal, stock documents and the backoffice's stock-in / stock-out
  pages read a product, add to it and save it.  Without a lock a sale or a Grab
  deduction made in between is silently undone; they now lock the row first and
  work from what they read there.  (SQLite ignores ``SELECT ... FOR UPDATE``; the
  race tests are PostgreSQL only.)
* Product create with an opening stock, and a catalogue copy with copy_stock, set
  stock and used to leave no movement.

Run:
    python manage.py test bravepos.tests.test_stock_write_safety \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

import threading
from decimal import Decimal
from io import StringIO
from unittest import mock, skipUnless

from django.core.management import call_command
from django.db import connection, connections, transaction
from django.test import TestCase, TransactionTestCase

from bravepos import catalog
from bravepos.models import (
    BranchSession, Product, Staff, StockMovement,
)
from bravepos.serializers import ProductSerializer
from bravepos.views import save_stock_in_document, save_stock_out_document

from .factories import make_branch, make_product, make_shop, open_shift
from .test_selforder import run_concurrently


def till_auth(branch):
    staff = Staff.objects.create(
        name='Nok', email=f'nok-{branch.pk}@test.local', password_hash='x', role='admin')
    staff.branches.add(branch)
    session = BranchSession.objects.create(
        token=f'tk{str(branch.pk)[:8]}'.ljust(36, 'x'), branch=branch, staff=staff)
    return {'HTTP_AUTHORIZATION': f'Bearer {session.token}'}


class LockedWritesUseFreshStockTests(TestCase):
    """The stock a write works from is the row read *under the lock*, not a copy
    read earlier.  Simulated by changing stock behind the caller's back after the
    caller loaded its copy."""

    def setUp(self):
        make_shop()
        self.branch = make_branch()
        open_shift(self.branch)
        self.p = make_product(self.branch, stock=10)
        self.auth = till_auth(self.branch)

    def stock(self):
        self.p.refresh_from_db()
        return self.p.stock

    def test_stock_modal_adds_to_the_current_stock(self):
        stale = Product.objects.get(pk=self.p.pk)
        Product.objects.filter(pk=self.p.pk).update(stock=7)      # a sale since
        res = self.client.post('/api/stock-movements', {
            'product_id': str(stale.id), 'type': 'in', 'qty': 5, 'note': 'x'},
            content_type='application/json', **self.auth)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(self.stock(), 12)

    def test_stock_documents_work_from_the_current_stock(self):
        res = self.client.post('/api/stock-documents', {
            'type': 'out', 'reason': 'Waste',
            'items': [{'product_id': str(self.p.id), 'qty': 3}]},
            content_type='application/json', **self.auth)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(self.stock(), 7)
        self.assertEqual(StockMovement.objects.get(product=self.p).qty, 3)

    def test_the_document_savers_ignore_a_stale_copy(self):
        stale = Product.objects.get(pk=self.p.pk)
        Product.objects.filter(pk=self.p.pk).update(stock=4)
        save_stock_out_document(self.branch, [(stale, Decimal(1), Decimal(0), Decimal(0))], 'Waste')
        self.assertEqual(self.stock(), 3)
        stale = Product.objects.get(pk=self.p.pk)
        Product.objects.filter(pk=self.p.pk).update(stock=20)
        save_stock_in_document(self.branch, [(stale, Decimal(5), Decimal(0), Decimal(0))])
        self.assertEqual(self.stock(), 25)

    def test_an_unknown_product_in_the_modal_is_404(self):
        res = self.client.post('/api/stock-movements', {
            'product_id': '00000000-0000-0000-0000-000000000000', 'type': 'in', 'qty': 1},
            content_type='application/json', **self.auth)
        self.assertEqual(res.status_code, 404)

    def test_each_product_is_locked_once_in_id_order(self):
        q = make_product(self.branch, name='Cake', stock=5)
        calls = []
        real = Product.objects.select_for_update

        def spy(*a, **kw):
            calls.append(1)
            return real(*a, **kw)

        with mock.patch.object(type(Product.objects), 'select_for_update', spy):
            save_stock_in_document(self.branch, [
                (q, Decimal(1), Decimal(0), Decimal(0)),
                (self.p, Decimal(1), Decimal(0), Decimal(0))])
        self.assertEqual(len(calls), 1)             # one locked query for every line


class OpeningStockMovementTests(TestCase):
    def setUp(self):
        self.branch = make_branch()

    def test_an_opening_stock_leaves_an_in_movement(self):
        ser = ProductSerializer(data={'name': 'Mocha', 'price': '110.00', 'stock': 12})
        self.assertTrue(ser.is_valid(), ser.errors)
        product = ser.save(branch=self.branch)
        move = StockMovement.objects.get(product=product)
        self.assertEqual((move.type, move.qty, move.note, move.branch),
                         ('in', 12, 'Opening stock', self.branch))

    def test_no_opening_stock_leaves_no_movement(self):
        ser = ProductSerializer(data={'name': 'Mocha', 'price': '110.00'})
        self.assertTrue(ser.is_valid(), ser.errors)
        ser.save(branch=self.branch)
        self.assertFalse(StockMovement.objects.exists())

    def test_a_negative_opening_stock_is_an_adjust(self):
        ser = ProductSerializer(data={'name': 'Mocha', 'price': '110.00', 'stock': -3})
        self.assertTrue(ser.is_valid(), ser.errors)
        product = ser.save(branch=self.branch)
        self.assertEqual(StockMovement.objects.get(product=product).type, 'adjust')

    def test_an_edit_adds_no_movement(self):
        ser = ProductSerializer(data={'name': 'Mocha', 'price': '110.00', 'stock': 12})
        ser.is_valid()
        product = ser.save(branch=self.branch)
        ser = ProductSerializer(product, data={'name': 'Mocha 2', 'stock': 99}, partial=True)
        self.assertTrue(ser.is_valid(), ser.errors)
        ser.save()
        self.assertEqual(StockMovement.objects.filter(product=product).count(), 1)

    def test_the_shopster_endpoint_records_it_too(self):
        make_branch(name='BIO HOUSE')
        res = self.client.post('/api/shopster/create-product/', {
            'name': 'Croissant', 'price': 85, 'stock': 12, 'barcode': 'S-OPEN-1'},
            content_type='application/json')
        self.assertEqual(res.status_code, 201, res.content)
        move = StockMovement.objects.get(product_id=res.json()['id'])
        self.assertEqual((move.type, move.qty, move.note), ('in', 12, 'Opening stock'))


class CatalogueCopyMovementTests(TestCase):
    def setUp(self):
        self.source = make_branch(name='BIO HOUSE')
        self.target = make_branch(name='Silom')
        self.p = make_product(self.source, name='Brownie', stock=24)
        self.p.barcode = 'B1'
        self.p.save()

    def copy(self, **kw):
        return catalog.copy_catalogue(self.source, self.target, **kw)

    def test_copy_stock_on_create_writes_a_movement(self):
        self.copy(copy_stock=True)
        dest = Product.objects.get(branch=self.target)
        self.assertEqual(dest.stock, 24)
        move = StockMovement.objects.get(product=dest)
        self.assertEqual((move.type, move.qty, move.note, move.branch),
                         ('in', 24, 'Catalogue copy', self.target))

    def test_without_copy_stock_there_is_no_movement(self):
        self.copy()
        self.assertFalse(StockMovement.objects.exists())

    def test_a_negative_copied_stock_is_an_adjust(self):
        Product.objects.filter(pk=self.p.pk).update(stock=-24)
        self.copy(copy_stock=True)
        self.assertEqual(StockMovement.objects.get().type, 'adjust')

    def test_updating_stock_on_an_existing_product_writes_one_and_notes_the_old_figure(self):
        self.copy()
        dest = Product.objects.get(branch=self.target)
        Product.objects.filter(pk=dest.pk).update(stock=2)
        self.copy(update_existing=True, copy_stock=True)
        dest.refresh_from_db()
        self.assertEqual(dest.stock, 24)
        move = StockMovement.objects.get(product=dest)
        self.assertEqual((move.type, move.qty, move.note),
                         ('adjust', 24, 'Catalogue copy (was 2)'))

    def test_a_rerun_with_nothing_to_change_writes_nothing_more(self):
        self.copy(copy_stock=True)
        self.copy(update_existing=True, copy_stock=True)
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_the_command_path_records_it(self):
        call_command('sync_products', '--from', 'BIO HOUSE', '--to', 'Silom', '--copy-stock',
                     stdout=StringIO(), stderr=StringIO())
        self.assertTrue(StockMovement.objects.filter(note='Catalogue copy').exists())


# SQLite ignores SELECT ... FOR UPDATE, so this only means something on PostgreSQL.
@skipUnless(connection.vendor == 'postgresql',
            'row locking needs PostgreSQL (SELECT ... FOR UPDATE)')
class LockedWriteRaceTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        make_shop()
        self.branch = make_branch()
        open_shift(self.branch)
        self.p = make_product(self.branch, stock=10)
        self.auth = till_auth(self.branch)

    def test_concurrent_stock_modal_writes_all_land(self):
        def add():
            from django.test import Client
            res = Client().post('/api/stock-movements', {
                'product_id': str(self.p.id), 'type': 'in', 'qty': 1},
                content_type='application/json', **self.auth)
            assert res.status_code == 201, res.content

        errors = run_concurrently([add] * 6)
        self.assertEqual(errors, [])
        self.p.refresh_from_db()
        self.assertEqual(self.p.stock, 16)
        self.assertEqual(StockMovement.objects.filter(product=self.p).count(), 6)
        connections.close_all()

    def test_a_document_waits_for_a_concurrent_sale_and_keeps_it(self):
        locked, released = threading.Event(), threading.Event()

        def hold_lock_then_sell():
            with transaction.atomic():
                Product.objects.select_for_update().get(pk=self.p.pk)
                locked.set()
                released.wait(10)
                Product.objects.filter(pk=self.p.pk).update(stock=3)    # a sale, 10 -> 3

        def document():
            from django.test import Client
            locked.wait(10)
            threading.Timer(0.5, released.set).start()
            res = Client().post('/api/stock-documents', {
                'type': 'in', 'items': [{'product_id': str(self.p.id), 'qty': 5}]},
                content_type='application/json', **self.auth)
            assert res.status_code == 201, res.content

        errors = run_concurrently([hold_lock_then_sell, document])
        self.assertEqual(errors, [])
        self.p.refresh_from_db()
        self.assertEqual(self.p.stock, 8)          # 3 + 5: the sale was not undone
        connections.close_all()
