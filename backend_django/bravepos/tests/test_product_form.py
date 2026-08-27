"""Saving a product tells you what went wrong instead of 500ing.

Nothing on ``Product`` is unique, so a resubmitted create form is
indistinguishable from a genuinely new product.  On 2026-08-26 that is exactly
what happened: ``POST /backoffice/products/new`` twice, eleven seconds apart,
from one form — two rows in the catalogue, and ten minutes afterwards spent
editing both.  The admin had seen a browser error, gone back and tried again,
with no way to tell that the first save had landed.

The other half is the fields themselves.  Every one of them went straight from
``request.POST`` to the column: ``Decimal("ten")`` raises, ``Decimal("NaN")``
does not raise but overflows the column, a 201-character description does not
fit ``varchar(200)``, and the Description box invited 5000 of them.  Each of
those is a 500 that takes every other field the admin typed with it.

Run:
    python manage.py test bravepos.tests.test_product_form \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from bravepos.models import Branch, Product, Settings, Staff


class ProductFormTestCase(TestCase):
    """A signed-in admin and two branches to save products into."""

    @classmethod
    def setUpTestData(cls):
        cls.password = "correct-horse-battery"
        cls.admin = Staff(
            name="Product Form Admin", username="productform",
            email="productform@therollingpinn.com", role="admin",
            active=True, backoffice_access=True,
        )
        cls.admin.set_password(cls.password)
        cls.admin.save()
        Settings.objects.get_or_create(id="shop")
        cls.branch = Branch.objects.create(name="Ari", code="ARI")
        cls.other = Branch.objects.create(name="Zebra Branch", code="ZEB")

    def setUp(self):
        self.assertTrue(
            self.client.login(username="productform", password=self.password))

    # ── helpers ─────────────────────────────────────────────────────────────

    def form(self, **overrides):
        """A minimally valid product form, before the field under test."""
        data = {"name": "Cherry Mousse Pop", "price": "120.00"}
        data.update(overrides)
        return data

    def post_new(self, branch=None, **overrides):
        url = reverse("backoffice:product_new")
        branch = self.branch if branch is None else branch
        return self.client.post(f"{url}?branch={branch.id}", self.form(**overrides))

    def assertNotSaved(self, response, *, count=0):
        """The form came back rather than redirecting, and nothing was written."""
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Product.objects.count(), count)


class DuplicateProductTests(ProductFormTestCase):
    """A second save of the same name warns once, then goes through."""

    def test_first_save_creates_the_product(self):
        response = self.post_new()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Product.objects.count(), 1)

    def test_resubmitting_the_same_form_does_not_create_a_second(self):
        self.post_new()
        response = self.post_new()
        self.assertNotSaved(response, count=1)
        self.assertContains(response, "may already exist")

    def test_the_warning_names_the_product_already_there(self):
        self.post_new()
        response = self.post_new()
        existing = Product.objects.get()
        self.assertContains(response, str(existing.id))
        self.assertContains(response, "Ari")

    def test_confirming_creates_the_second_one(self):
        self.post_new()
        self.post_new()
        response = self.post_new(confirm_duplicate="1")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Product.objects.count(), 2)

    def test_the_match_ignores_case_and_surrounding_space(self):
        self.post_new()
        response = self.post_new(name="  cherry mousse pop  ")
        self.assertNotSaved(response, count=1)

    def test_another_branch_may_hold_the_same_name(self):
        self.post_new()
        response = self.post_new(branch=self.other)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Product.objects.count(), 2)

    def test_a_different_name_saves_straight_away(self):
        self.post_new()
        response = self.post_new(name="Cherry Mousse Pop Large")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Product.objects.count(), 2)

    def test_a_product_is_not_a_duplicate_of_itself(self):
        self.post_new()
        product = Product.objects.get()
        response = self.client.post(
            reverse("backoffice:product_detail", args=[product.id]),
            self.form(price="130.00"))
        self.assertEqual(response.status_code, 302)
        product.refresh_from_db()
        self.assertEqual(product.price, Decimal("130.00"))

    def test_renaming_onto_a_name_already_taken_warns(self):
        self.post_new()
        self.post_new(name="Cherry Mousse Pop Large")
        large = Product.objects.get(name="Cherry Mousse Pop Large")
        response = self.client.post(
            reverse("backoffice:product_detail", args=[large.id]), self.form())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "may already exist")
        large.refresh_from_db()
        self.assertEqual(large.name, "Cherry Mousse Pop Large")


class ProductFieldLimitTests(ProductFormTestCase):
    """Anything the column cannot hold is a form error, not an error page."""

    def test_a_name_longer_than_the_column_is_refused(self):
        response = self.post_new(name="x" * 201)
        self.assertNotSaved(response)
        self.assertContains(response, "the most that fits is 200")

    def test_a_name_at_the_limit_saves(self):
        response = self.post_new(name="x" * 200)
        self.assertEqual(response.status_code, 302)

    def test_a_description_longer_than_the_column_is_refused(self):
        response = self.post_new(name_th="ก" * 201)
        self.assertNotSaved(response)
        self.assertContains(response, "Description is 201 characters")

    def test_the_description_box_no_longer_invites_5000_characters(self):
        response = self.client.get(
            f"{reverse('backoffice:product_new')}?branch={self.branch.id}")
        # The box and its counter both used to say 5000, against a column that
        # holds 200.  Asserted on the attribute and the counter rather than on
        # the bare number, which also appears in the comment explaining why.
        self.assertNotContains(response, 'maxlength="5000"')
        self.assertNotContains(response, "(0/5000)")
        self.assertContains(response, 'maxlength="200"')
        self.assertContains(response, "(0/200)")

    def test_an_over_long_sku_is_refused(self):
        response = self.post_new(sku="S" * 65)
        self.assertNotSaved(response)
        self.assertContains(response, "SKU is 65 characters")

    def test_an_over_long_barcode_is_refused(self):
        response = self.post_new(barcode="8" * 65)
        self.assertNotSaved(response)
        self.assertContains(response, "Barcode is 65 characters")

    def test_a_blank_name_is_refused(self):
        response = self.post_new(name="   ")
        self.assertNotSaved(response)
        self.assertContains(response, "Product name is required")


class ProductNumberFieldTests(ProductFormTestCase):
    """The numeric fields parse without raising, whatever is posted."""

    def test_a_price_that_is_not_a_number_is_refused(self):
        response = self.post_new(price="ten baht")
        self.assertNotSaved(response)
        self.assertContains(response, "Price must be a number")

    def test_nan_is_refused(self):
        # Decimal("NaN") does not raise — it survives to the column and dies
        # there, which is the same 500 by a longer route.
        response = self.post_new(price="NaN")
        self.assertNotSaved(response)
        self.assertContains(response, "Price must be a number")

    def test_infinity_is_refused(self):
        response = self.post_new(price="Infinity")
        self.assertNotSaved(response)

    def test_a_price_past_the_column_is_refused(self):
        response = self.post_new(price="100000000")
        self.assertNotSaved(response)
        self.assertContains(response, "Price can&#x27;t be more than")

    def test_a_price_at_the_limit_saves(self):
        response = self.post_new(price="99999999.99")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Product.objects.get().price, Decimal("99999999.99"))

    def test_a_fractional_stock_count_is_refused(self):
        response = self.post_new(stock="1.5")
        self.assertNotSaved(response)
        self.assertContains(response, "Stock on hand must be a whole number")

    def test_a_stock_count_past_the_column_is_refused(self):
        response = self.post_new(stock="99999999999")
        self.assertNotSaved(response)
        self.assertContains(response, "Stock on hand is too large")

    def test_blank_numbers_stay_at_zero(self):
        response = self.post_new(price="", cost="", stock="", par_level="")
        self.assertEqual(response.status_code, 302)
        product = Product.objects.get()
        self.assertEqual(product.price, Decimal("0"))
        self.assertEqual(product.stock, 0)


class RefusedSaveKeepsTheTypingTests(ProductFormTestCase):
    """A refused save must not cost the admin the rest of the form."""

    def test_the_other_fields_come_back_as_typed(self):
        response = self.post_new(
            name="Cherry Mousse Pop", price="ten baht", sku="BK-001",
            barcode="8850123456789", cost="45.50")
        self.assertNotSaved(response)
        for typed in ("Cherry Mousse Pop", "BK-001", "8850123456789", "45.50"):
            self.assertContains(response, typed)

    def test_a_refused_edit_leaves_the_stored_product_alone(self):
        self.post_new()
        product = Product.objects.get()
        response = self.client.post(
            reverse("backoffice:product_detail", args=[product.id]),
            self.form(name="Renamed", price="ten baht"))
        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.name, "Cherry Mousse Pop")
        self.assertEqual(product.price, Decimal("120.00"))

    def test_a_refused_edit_shows_the_attempted_values(self):
        self.post_new()
        product = Product.objects.get()
        response = self.client.post(
            reverse("backoffice:product_detail", args=[product.id]),
            self.form(name="Renamed", price="ten baht"))
        self.assertContains(response, "Renamed")
