"""Editing a product through the API never touches its stock.

The till's Edit Product form sent back the stock count it loaded, so saving a
name or price change put back every unit sold while the form was open, and
left no movement to show for it.  Stock is set once, on create; after that it
moves through stock movements and documents only.
"""
from __future__ import annotations

from django.test import TestCase

from bravepos.serializers import ProductSerializer

from .factories import make_branch, make_product


class ProductStockIsNotEditableTests(TestCase):
    def setUp(self):
        self.branch = make_branch()

    def test_an_edit_leaves_stock_alone(self):
        product = make_product(self.branch, stock=50)
        ser = ProductSerializer(product, data={
            'name': 'Iced Latte', 'price': '120.00', 'stock': 999,
        }, partial=True)
        self.assertTrue(ser.is_valid(), ser.errors)
        ser.save()

        product.refresh_from_db()
        self.assertEqual(product.name, 'Iced Latte')
        self.assertEqual(product.stock, 50)

    def test_create_still_takes_an_opening_stock(self):
        ser = ProductSerializer(data={
            'name': 'Mocha', 'price': '110.00', 'stock': 12,
        })
        self.assertTrue(ser.is_valid(), ser.errors)
        product = ser.save(branch=self.branch)
        self.assertEqual(product.stock, 12)
