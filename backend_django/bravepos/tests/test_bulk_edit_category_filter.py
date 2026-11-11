"""Quick edit products can be narrowed to one category.

Run:
    python manage.py test bravepos.tests.test_bulk_edit_category_filter \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from bravepos.models import Branch, Category, Product, Settings, Staff


class BulkEditCategoryFilterTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.password = "correct-horse-battery"
        admin = Staff(
            name="Bulk Filter Admin", username="bulkfilter",
            email="bulkfilter@therollingpinn.com", role="admin",
            active=True, backoffice_access=True,
        )
        admin.set_password(cls.password)
        admin.save()
        Settings.objects.get_or_create(id="shop")
        cls.branch = Branch.objects.create(name="Ari", code="ARI")
        cls.other = Branch.objects.create(name="Zebra", code="ZEB")
        cls.cake = Category.objects.create(branch=cls.branch, name="Mini Cake")
        cls.cookie = Category.objects.create(branch=cls.branch, name="Cookie")
        cls.foreign = Category.objects.create(branch=cls.other, name="Elsewhere")
        Product.objects.create(branch=cls.branch, name="Lily Cake", price=1,
                               category=cls.cake)
        Product.objects.create(branch=cls.branch, name="Biscoff Cookie", price=1,
                               category=cls.cookie)
        Product.objects.create(branch=cls.branch, name="Loose Candle", price=1)

    def setUp(self):
        self.assertTrue(
            self.client.login(username="bulkfilter", password=self.password))

    def names(self, **params):
        response = self.client.get(
            reverse("backoffice:product_bulk_edit"),
            {"branch": str(self.branch.id), **params},
        )
        self.assertEqual(response.status_code, 200)
        return [p.name for p in response.context["products"]]

    def test_no_filter_lists_everything(self):
        self.assertEqual(self.names(),
                         ["Biscoff Cookie", "Lily Cake", "Loose Candle"])

    def test_a_category_lists_only_its_products(self):
        self.assertEqual(self.names(cat=str(self.cake.id)), ["Lily Cake"])

    def test_the_dropdown_shows_the_chosen_category(self):
        response = self.client.get(
            reverse("backoffice:product_bulk_edit"),
            {"branch": str(self.branch.id), "cat": str(self.cake.id)},
        )
        self.assertContains(
            response, f'<option value="{self.cake.id}" selected>', html=False)

    def test_uncategorised(self):
        self.assertEqual(self.names(cat="none"), ["Loose Candle"])

    def test_an_unknown_category_shows_everything_not_an_error(self):
        for bad in (str(self.foreign.id), "not-an-id"):
            self.assertEqual(len(self.names(cat=bad)), 3)

    def test_pages_and_the_save_redirect_keep_the_category(self):
        response = self.client.get(
            reverse("backoffice:product_bulk_edit"),
            {"branch": str(self.branch.id), "cat": str(self.cake.id)},
        )
        self.assertIn(f"cat={self.cake.id}", response.context["qs"])
        product = Product.objects.get(name="Lily Cake")
        url = (reverse("backoffice:product_bulk_edit")
               + f"?branch={self.branch.id}&cat={self.cake.id}")
        response = self.client.post(url, {
            "id": [str(product.id)], "barcode": [""], "name": ["Lily Cake"],
            "description": [""], "price": ["2"], "cost": ["0"],
            "category": [str(self.cake.id)], "unit": [""],
        })
        self.assertIn(f"cat={self.cake.id}", response["Location"])
        product.refresh_from_db()
        self.assertEqual(product.price, 2)
