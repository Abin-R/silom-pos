"""Copying a branch's catalogue onto branches that don't have one yet.

The command exists because a newly opened branch starts with zero products,
and the real BIO HOUSE catalogue is 44 items across 8 categories. What is
asserted here is mostly about what must *not* travel: stock is a fact about
one shop's shelves, and a copied product must never end up pointing at the
source branch's category row (which would make an edit at one branch silently
move a product at another).
"""
from __future__ import annotations

from decimal import Decimal
from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from bravepos.models import Category, Product, Unit

from .factories import make_branch


def sync(*args, **kwargs):
    out = StringIO()
    call_command('sync_products', *args, stdout=out, stderr=out, **kwargs)
    return out.getvalue()


class SyncProductsTests(TestCase):
    def setUp(self):
        self.source = make_branch(name='BIO HOUSE')
        self.target = make_branch(name='Silom')
        # branch=None: units are shared shop-wide, exactly as in production.
        self.unit = Unit.objects.create(name='ชิ้น', order=16)
        self.cat = Category.objects.create(
            branch=self.source, name='Cookies', color='#00b14f', order=3,
        )
        self.product = Product.objects.create(
            branch=self.source, category=self.cat, unit=self.unit,
            name='Sexy Back Cookie', price=Decimal('95.00'),
            # Negative because deliveries were never recorded — the exact
            # situation that makes copying stock the wrong default.
            stock=-24, is_favorite=True, sort_order=2,
        )

    def test_copies_catalogue_to_empty_branch(self):
        sync('--from', 'BIO HOUSE', '--to', 'Silom')

        copied = Product.objects.get(branch=self.target, name='Sexy Back Cookie')
        self.assertEqual(copied.price, Decimal('95.00'))
        self.assertTrue(copied.is_favorite)
        self.assertEqual(copied.sort_order, 2)
        # The category is the target's own row, not the source's.
        self.assertEqual(copied.category.branch, self.target)
        self.assertEqual(copied.category.name, 'Cookies')
        self.assertEqual(copied.category.color, '#00b14f')
        self.assertNotEqual(copied.category_id, self.cat.id)
        # A shop-wide unit is shared, not duplicated.
        self.assertEqual(copied.unit_id, self.unit.id)
        self.assertEqual(Unit.objects.count(), 1)

    def test_stock_starts_at_zero(self):
        sync('--from', 'BIO HOUSE', '--to', 'Silom')
        self.assertEqual(
            Product.objects.get(branch=self.target).stock, 0,
        )

    def test_copy_stock_flag_carries_quantities(self):
        sync('--from', 'BIO HOUSE', '--to', 'Silom', '--copy-stock')
        self.assertEqual(
            Product.objects.get(branch=self.target).stock, -24,
        )

    def test_rerun_updates_instead_of_duplicating(self):
        sync('--from', 'BIO HOUSE', '--to', 'Silom')
        self.product.price = Decimal('105.00')
        self.product.save()

        # Stock counted at the target since the first run must survive a
        # re-sync — the POS is the authority on what is on its own shelves.
        target_product = Product.objects.get(branch=self.target)
        target_product.stock = 12
        target_product.save()

        sync('--from', 'BIO HOUSE', '--to', 'Silom')

        self.assertEqual(Product.objects.filter(branch=self.target).count(), 1)
        self.assertEqual(Category.objects.filter(branch=self.target).count(), 1)
        target_product.refresh_from_db()
        self.assertEqual(target_product.price, Decimal('105.00'))
        self.assertEqual(target_product.stock, 12)

    def test_dry_run_writes_nothing(self):
        out = sync('--from', 'BIO HOUSE', '--to', 'Silom', '--dry-run')

        self.assertIn('would create', out)
        self.assertIn('rolled back', out)
        self.assertEqual(Product.objects.filter(branch=self.target).count(), 0)
        self.assertEqual(Category.objects.filter(branch=self.target).count(), 0)

    def test_all_empty_skips_branches_that_have_a_catalogue(self):
        empty = make_branch(name='Vibhavadi')
        stocked = make_branch(name='test branch')
        Product.objects.create(
            branch=stocked, name='Classic Brownie Bite', price=Decimal('45.00'),
        )

        out = sync('--from', 'BIO HOUSE', '--all-empty')

        self.assertEqual(Product.objects.filter(branch=self.target).count(), 1)
        self.assertEqual(Product.objects.filter(branch=empty).count(), 1)
        # The branch with its own products is left completely alone.
        self.assertEqual(Product.objects.filter(branch=stocked).count(), 1)
        self.assertNotIn('test branch', out)

    def test_uncategorised_product_stays_uncategorised(self):
        Product.objects.create(
            branch=self.source, unit=self.unit, name='Ballerina',
            price=Decimal('650.00'), category=None,
        )
        sync('--from', 'BIO HOUSE', '--to', 'Silom')

        copied = Product.objects.get(branch=self.target, name='Ballerina')
        self.assertIsNone(copied.category)

    def test_unknown_branch_is_an_error_not_a_traceback(self):
        with self.assertRaises(CommandError) as caught:
            sync('--from', 'BIO HOUSE', '--to', 'Nonesuch')
        self.assertIn('Nonesuch', str(caught.exception))
        self.assertIn('BIO HOUSE', str(caught.exception))

    def test_source_is_never_its_own_target(self):
        with self.assertRaises(CommandError):
            sync('--from', 'BIO HOUSE', '--to', 'BIO HOUSE')
