"""The Shopster product ID: required, unique on sale, and the first match key.

Before 2026-11-11 every sync matched a branch's copy of a product by name
alone.  On that day "0.4lb - Gems X'mas Tree Mini Cake" was renamed at BIO
HOUSE without the sync box, then the Sync products page was run: no branch
had the new name, so ten branches got a second copy beside the old one.

Now the Shopster product ID (``Product.barcode``) is matched first, it is
required wherever a product is written, and it is unique among a branch's
on-sale products — removed rows are exempt, since old duplicates live there.

Run:
    python manage.py test bravepos.tests.test_shopster_id_matching \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from bravepos import catalog, sale_windows
from bravepos.models import Branch, BranchSession, Product, ProductSaleWindow, Settings, Staff

OLD = "0.4lb - Gems X’mas Tree Mini Cake"
NEW = "0.4lb - Classic - Gems X’mas Tree Mini Cake"


class ShopsterIdTestCase(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.password = "correct-horse-battery"
        admin = Staff(
            name="ID Admin", username="shopsterid",
            email="shopsterid@therollingpinn.com", role="admin",
            active=True, backoffice_access=True,
        )
        admin.set_password(cls.password)
        admin.save()
        cls.admin = admin
        Settings.objects.get_or_create(id="shop")
        Branch.objects.update(active=False)  # the seeded EmQuartier
        cls.bio = Branch.objects.create(name="BIO HOUSE", code="BIO")
        cls.westgate = Branch.objects.create(name="Central Westgate", code="CWG")
        cls.pinklao = Branch.objects.create(name="Central Pinklao", code="CPK")

    def setUp(self):
        self.assertTrue(
            self.client.login(username="shopsterid", password=self.password))

    def make(self, branch, name=OLD, barcode="39722", price="650.00", **kw):
        return Product.objects.create(branch=branch, name=name, barcode=barcode,
                                      price=Decimal(price), **kw)

    def form(self, **overrides):
        data = {"name": OLD, "price": "650.00", "barcode": "39722"}
        data.update(overrides)
        return data

    def copies(self, branch):
        return list(Product.objects.filter(branch=branch).values_list("name", "active"))


class TheIncidentTests(ShopsterIdTestCase):
    """Rename first, sync later — the copy is renamed, not duplicated."""

    def setUp(self):
        super().setUp()
        self.here = self.make(self.bio)
        self.make(self.westgate, stock=2)
        self.make(self.pinklao)

    def test_renaming_then_running_the_sync_page_renames_the_copies(self):
        self.client.post(reverse("backoffice:product_detail", args=[self.here.id]),
                         self.form(name=NEW))
        self.client.post(reverse("backoffice:product_sync"), {
            "source": str(self.bio.id),
            "targets": [str(self.westgate.id), str(self.pinklao.id)],
        })
        self.assertEqual(self.copies(self.westgate), [(NEW, True)])
        self.assertEqual(self.copies(self.pinklao), [(NEW, True)])
        self.assertEqual(Product.objects.get(branch=self.westgate).stock, 2)

    def test_changing_the_name_and_the_id_in_one_save_finds_every_copy(self):
        self.client.post(reverse("backoffice:product_detail", args=[self.here.id]),
                         self.form(name=NEW, barcode="40001", sync_all="1"))
        for branch in (self.westgate, self.pinklao):
            row = Product.objects.get(branch=branch)
            self.assertEqual((row.name, row.barcode), (NEW, "40001"))

    def test_quick_edit_changes_both_at_once_too(self):
        self.client.post(
            reverse("backoffice:product_bulk_edit") + f"?branch={self.bio.id}",
            {"id": [str(self.here.id)], "barcode": ["40001"], "name": [NEW],
             "description": [""], "category": [""], "unit": [""],
             "price": ["650.00"], "cost": ["0"], "sync_all": "1"})
        for branch in (self.westgate, self.pinklao):
            row = Product.objects.get(branch=branch)
            self.assertEqual((row.name, row.barcode), (NEW, "40001"))


class MatchingTests(ShopsterIdTestCase):

    def test_the_id_wins_over_the_name(self):
        live = self.make(self.westgate, name="Something else entirely")
        self.make(self.westgate, name=OLD, barcode="11111")
        source = self.make(self.bio)
        self.assertEqual(
            catalog.find_product(self.westgate.products, [source.barcode], [source.name]),
            live)

    def test_the_name_is_the_fallback_for_a_copy_with_no_id(self):
        blank = self.make(self.westgate, barcode="")
        source = self.make(self.bio)
        self.assertEqual(
            catalog.find_product(self.westgate.products, [source.barcode], [source.name]),
            blank)

    def test_an_on_sale_row_is_preferred_to_a_removed_duplicate(self):
        self.make(self.westgate, active=False)
        live = self.make(self.westgate)
        self.assertEqual(
            catalog.find_product(self.westgate.products, ["39722"], []), live)

    def test_a_removed_duplicate_does_not_retire_the_live_product_elsewhere(self):
        """BIO HOUSE keeps an old removed copy under the same ID as its live
        one: that is history, not a decision to stop selling."""
        self.make(self.bio)
        self.make(self.bio, name="Old name", active=False)
        self.make(self.westgate)
        self.assertEqual(catalog.removed_catalogue(self.bio), [])

    def test_the_sync_page_preview_lists_edits_with_before_and_after(self):
        self.make(self.bio, price="650.00")
        self.make(self.westgate, price="590.00")
        result = catalog.preview(self.bio, self.westgate)
        self.assertEqual(result["updating"][0]["changes"], ["price 590.00 → 650.00"])

    def test_products_only_the_target_sells_are_left_alone(self):
        self.make(self.bio)
        mine = self.make(self.westgate, name="Westgate Special", barcode="777")
        self.client.post(reverse("backoffice:product_sync"), {
            "source": str(self.bio.id), "targets": [str(self.westgate.id)]})
        mine.refresh_from_db()
        self.assertTrue(mine.active)


class RequiredAndUniqueTests(ShopsterIdTestCase):

    def new(self, branch=None, **overrides):
        branch = branch or self.bio
        return self.client.post(
            reverse("backoffice:product_new") + f"?branch={branch.id}",
            self.form(**overrides))

    def test_the_product_form_requires_it(self):
        response = self.new(barcode="  ")
        self.assertContains(response, "Shopster product ID is required.")
        self.assertFalse(Product.objects.exists())

    def test_two_on_sale_products_in_a_branch_cannot_share_it(self):
        self.make(self.bio, name="Already here")
        response = self.new(name="Another cake")
        self.assertContains(response, "already used by")
        self.assertEqual(Product.objects.count(), 1)

    def test_another_branch_may_use_the_same_id(self):
        self.make(self.westgate)
        self.assertEqual(self.new().status_code, 302)

    def test_a_removed_product_does_not_hold_the_id(self):
        self.make(self.bio, name="Retired cake", active=False)
        self.assertEqual(self.new().status_code, 302)

    def test_the_database_refuses_it_as_a_backstop(self):
        self.make(self.bio)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make(self.bio, name="Second")

    def test_restoring_onto_a_taken_id_is_refused_with_a_reason(self):
        gone = self.make(self.bio, name="Old copy", active=False)
        self.make(self.bio, name="Live copy")
        response = self.client.post(
            reverse("backoffice:product_restore", args=[gone.id]), follow=True)
        self.assertContains(response, "was not restored")
        gone.refresh_from_db()
        self.assertFalse(gone.active)

    def test_the_schedule_does_not_restore_onto_a_taken_id(self):
        gone = self.make(self.bio, name="Seasonal", active=False,
                         removed_by_schedule=True)
        ProductSaleWindow.objects.create(product=gone, launch_date=None)
        self.make(self.bio, name="Live copy")
        self.assertIsNone(sale_windows.apply(gone))
        gone.refresh_from_db()
        self.assertFalse(gone.active)

    def test_bulk_add_refuses_a_blank_id_and_a_repeat_within_the_batch(self):
        response = self.client.post(
            reverse("backoffice:product_bulk_add") + f"?branch={self.bio.id}", {
                "name": ["One", "Two", "Three"], "barcode": ["A1", "", "A1"],
                "price": ["1", "1", "1"], "category": ["", "", ""], "unit": ["", "", ""],
            }, follow=True)
        self.assertEqual(list(Product.objects.values_list("name", flat=True)), ["One"])
        self.assertContains(response, "2 rows not added")

    def test_quick_edit_refuses_a_blank_id_and_saves_the_other_rows(self):
        a = self.make(self.bio, name="A", barcode="1")
        b = self.make(self.bio, name="B", barcode="2")
        self.client.post(
            reverse("backoffice:product_bulk_edit") + f"?branch={self.bio.id}", {
                "id": [str(a.id), str(b.id)], "barcode": ["", "2"],
                "name": ["A", "B"], "description": ["", ""], "category": ["", ""],
                "unit": ["", ""], "price": ["5", "6"], "cost": ["0", "0"],
            })
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.barcode, a.price), ("1", Decimal("650.00")))
        self.assertEqual(b.price, Decimal("6"))


class ApiTests(ShopsterIdTestCase):
    """The till and Shopster go through ``ProductSerializer``."""

    def setUp(self):
        super().setUp()
        staff = Staff.objects.create(
            name="Till Admin", email="tilladmin@id.local", password_hash="x", role="admin")
        staff.branches.add(self.bio)
        session = BranchSession.objects.create(
            token="sid0" * 9, branch=self.bio, staff=staff)
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {session.token}"}

    def till_create(self, **body):
        return self.client.post("/api/products", {"name": "Till Cake", "price": 10, **body},
                                content_type="application/json", **self.auth)

    def test_an_installed_till_without_the_field_can_still_add(self):
        self.assertEqual(self.till_create().status_code, 201)

    @override_settings(TILL_REQUIRES_PRODUCT_BARCODE=True)
    def test_once_switched_on_the_till_must_send_it(self):
        response = self.till_create()
        self.assertEqual(response.status_code, 400)
        self.assertIn("barcode", response.json())
        self.assertEqual(self.till_create(barcode="T-1").status_code, 201)

    def test_the_till_cannot_reuse_an_id_on_sale_in_its_branch(self):
        self.make(self.bio)
        self.assertEqual(self.till_create(barcode="39722").status_code, 400)

    def test_an_old_till_edit_without_the_field_still_saves(self):
        product = self.make(self.bio, barcode="")
        response = self.client.put(
            # The installed till's Edit product body: no ``barcode`` key.
            f"/api/products/{product.id}",
            {"name": "Till Cake", "price": 12, "cost": 0, "is_favorite": True,
             "tax_type": "V", "image_url": "", "image_base64": ""},
            content_type="application/json", **self.auth)
        self.assertEqual(response.status_code, 200, response.content)

    def test_an_edit_cannot_blank_it(self):
        product = self.make(self.bio)
        response = self.client.put(
            f"/api/products/{product.id}", {"barcode": ""},
            content_type="application/json", **self.auth)
        self.assertEqual(response.status_code, 400)

    def test_shopster_must_send_it_and_cannot_reuse_one(self):
        url = "/api/shopster/create-product/"
        self.assertEqual(self.client.post(
            url, {"name": "X", "price": 1}, content_type="application/json").status_code, 400)
        self.make(self.bio)
        self.assertEqual(self.client.post(
            url, {"name": "X", "price": 1, "barcode": "39722"},
            content_type="application/json").status_code, 400)
