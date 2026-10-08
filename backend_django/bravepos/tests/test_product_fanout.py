"""The product form's "Sync to all branches" toggle.

A product is a per-branch row, so "Sexy Back Cookie" at BIO HOUSE and the one
at Silom are two records whose only connection is that somebody typed the same
name twice.  Adding a cake therefore meant adding it eight more times, and a
price change meant opening eight forms — or opening the Sync page afterwards
and remembering to.  The toggle does it on the save that is already happening.

An edit carries the whole form — cost, photo, category and unit included —
except stock and par level, which describe one shop's shelves.  A rename has
to find the copies under the name they still hold.  A removed product must not
be born dead at a branch that never had it.  Removing and restoring are their
own buttons, each with its own "at all branches too" box.

Run:
    python manage.py test bravepos.tests.test_product_fanout \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from bravepos.models import Branch, Category, Product, Settings, Staff, Unit


class FanoutTestCase(TestCase):
    """A signed-in admin and three branches: one to save into, two to reach."""

    @classmethod
    def setUpTestData(cls):
        cls.password = "correct-horse-battery"
        cls.admin = Staff(
            name="Fanout Admin", username="fanout",
            email="fanout@therollingpinn.com", role="admin",
            active=True, backoffice_access=True,
        )
        cls.admin.set_password(cls.password)
        cls.admin.save()
        Settings.objects.get_or_create(id="shop")
        # Migration 0005 seeds an "EmQuartier" branch when the table is empty,
        # and this test is about *which* branches a save reaches — so retire it
        # and let the three below be the whole shop.
        Branch.objects.update(active=False)
        cls.branch = Branch.objects.create(name="Ari", code="ARI")
        cls.silom = Branch.objects.create(name="Silom", code="SIL")
        cls.bio = Branch.objects.create(name="BIO HOUSE", code="BIO")

    def setUp(self):
        self.assertTrue(
            self.client.login(username="fanout", password=self.password))

    # ── helpers ─────────────────────────────────────────────────────────────

    def form(self, **overrides):
        data = {"name": "Cherry Mousse Pop", "price": "120.00"}
        data.update(overrides)
        return data

    def create(self, *, sync=True, branch=None, follow=False, **overrides):
        """POST the create form, with the toggle on unless told otherwise."""
        branch = self.branch if branch is None else branch
        data = self.form(**overrides)
        if sync:
            data["sync_all"] = "1"
        return self.client.post(
            f"{reverse('backoffice:product_new')}?branch={branch.id}",
            data, follow=follow)

    def edit(self, product, *, sync=True, follow=False, **overrides):
        data = self.form(**overrides)
        if sync:
            data["sync_all"] = "1"
        return self.client.post(
            reverse("backoffice:product_detail", args=[product.id]),
            data, follow=follow)

    def at(self, branch, name="Cherry Mousse Pop"):
        return Product.objects.filter(branch=branch, name__iexact=name).first()


class CreatingWithTheToggleOnTests(FanoutTestCase):
    """A new product appears at every branch, not just the one on screen."""

    def test_it_reaches_the_other_branches(self):
        self.create()
        self.assertIsNotNone(self.at(self.silom))
        self.assertIsNotNone(self.at(self.bio))
        self.assertEqual(Product.objects.count(), 3)

    def test_the_copies_carry_the_price_and_shopster_id(self):
        self.create(price="145.00", barcode="39574")
        copy = self.at(self.silom)
        self.assertEqual(copy.price, Decimal("145.00"))
        self.assertEqual(copy.barcode, "39574")

    def test_the_copy_points_at_the_target_branch_own_category(self):
        cat = Category.objects.create(branch=self.branch, name="Pops")
        self.create(category=str(cat.id))
        copy = self.at(self.silom)
        self.assertEqual(copy.category.name, "Pops")
        self.assertEqual(copy.category.branch, self.silom)

    def test_a_branch_scoped_unit_is_recreated_under_the_target(self):
        unit = Unit.objects.create(branch=self.branch, name="Box")
        self.create(unit=str(unit.id))
        copy = self.at(self.silom)
        self.assertEqual(copy.unit.name, "Box")
        self.assertEqual(copy.unit.branch, self.silom)

    def test_without_the_toggle_nothing_else_is_written(self):
        self.create(sync=False)
        self.assertEqual(Product.objects.count(), 1)

    def test_an_archived_branch_is_not_written_to(self):
        Branch.objects.filter(pk=self.bio.pk).update(active=False)
        self.create()
        self.assertIsNone(self.at(self.bio))

    def test_a_branch_that_already_has_the_name_keeps_its_own_row(self):
        theirs = Product.objects.create(
            branch=self.silom, name="Cherry Mousse Pop",
            price=Decimal("99.00"), cost=Decimal("30.00"), stock=7)
        self.create(price="120.00")
        theirs.refresh_from_db()
        # Matched, so no second row — and the create form's price is the one
        # the admin just typed, so it lands.
        self.assertEqual(
            Product.objects.filter(branch=self.silom).count(), 1)
        self.assertEqual(theirs.price, Decimal("120.00"))
        self.assertEqual(theirs.stock, 7)


class EditingWithTheToggleOnTests(FanoutTestCase):
    """An edit carries the form, except each branch's stock and par level."""

    def setUp(self):
        super().setUp()
        self.create()
        self.product = self.at(self.branch)
        # Give the other branches prices, costs and stock of their own, so a
        # test that says "left alone" has something to leave alone.
        Product.objects.filter(branch__in=[self.silom, self.bio]).update(
            cost=Decimal("55.00"), stock=9, par_level=4,
            image_url="https://x/y.jpg")

    def test_the_price_reaches_the_other_branches(self):
        self.edit(self.product, price="135.00")
        self.assertEqual(self.at(self.silom).price, Decimal("135.00"))
        self.assertEqual(self.at(self.bio).price, Decimal("135.00"))

    def test_the_shopster_id_reaches_the_other_branches(self):
        self.edit(self.product, barcode="39574")
        self.assertEqual(self.at(self.silom).barcode, "39574")

    def test_a_rename_finds_the_copies_under_their_old_name(self):
        self.edit(self.product, name="Cherry Mousse Pop Large")
        self.assertEqual(
            Product.objects.filter(branch=self.silom).count(), 1)
        self.assertIsNotNone(self.at(self.silom, "Cherry Mousse Pop Large"))

    def test_stock_and_par_level_stay_branch_local(self):
        self.edit(self.product, price="135.00", stock="3", par_level="12")
        copy = self.at(self.silom)
        self.assertEqual(copy.stock, 9)
        self.assertEqual(copy.par_level, 4)

    def test_cost_description_sku_and_flags_travel(self):
        self.edit(self.product, cost="10.00", name_th="เค้ก", sku="BK-9",
                  tax_type="V", is_favorite="1")
        copy = self.at(self.silom)
        self.assertEqual(copy.cost, Decimal("10.00"))
        self.assertEqual(copy.name_th, "เค้ก")
        self.assertEqual(copy.sku, "BK-9")
        self.assertEqual(copy.tax_type, "V")
        self.assertTrue(copy.is_favorite)

    def test_shelf_life_travels(self):
        self.edit(self.product, shelf_life="3")
        self.assertEqual(self.at(self.silom).shelf_life, 3)

    def test_the_photo_travels(self):
        self.edit(self.product, image_url="https://x/new.jpg")
        self.assertEqual(self.at(self.silom).image_url, "https://x/new.jpg")

    def test_the_category_travels_as_the_target_branch_own(self):
        cat = Category.objects.create(branch=self.branch, name="Pops")
        self.edit(self.product, category=str(cat.id))
        copy = self.at(self.silom)
        self.assertEqual(copy.category.name, "Pops")
        self.assertEqual(copy.category.branch, self.silom)

    def test_the_unit_travels(self):
        unit = Unit.objects.create(branch=self.branch, name="Box")
        self.edit(self.product, unit=str(unit.id))
        copy = self.at(self.silom)
        self.assertEqual(copy.unit.name, "Box")
        self.assertEqual(copy.unit.branch, self.silom)

    def test_a_branch_missing_the_product_gets_it(self):
        self.at(self.bio).delete()
        self.edit(self.product, price="135.00")
        self.assertIsNotNone(self.at(self.bio))
        self.assertEqual(self.at(self.bio).price, Decimal("135.00"))

    def test_a_removed_product_is_not_created_where_it_never_was(self):
        self.at(self.bio).delete()
        Product.objects.filter(pk=self.product.pk).update(active=False)
        self.edit(self.product, price="135.00")
        self.assertIsNone(self.at(self.bio))

    def test_a_removed_product_still_updates_the_copies_that_exist(self):
        Product.objects.filter(pk=self.product.pk).update(active=False)
        self.edit(self.product, price="135.00")
        self.assertEqual(self.at(self.silom).price, Decimal("135.00"))

    def test_a_copy_the_branch_removed_stays_removed(self):
        Product.objects.filter(branch=self.silom).update(active=False)
        self.edit(self.product, price="135.00")
        copy = self.at(self.silom)
        self.assertFalse(copy.active)
        self.assertEqual(copy.price, Decimal("135.00"))

    def test_without_the_toggle_the_other_branches_do_not_move(self):
        self.edit(self.product, sync=False, price="135.00")
        self.assertEqual(self.at(self.silom).price, Decimal("120.00"))


class RefusedSaveTests(FanoutTestCase):
    """A save that was refused must not have reached the other branches."""

    def test_a_form_error_writes_nowhere(self):
        response = self.create(price="ten baht")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Product.objects.count(), 0)

    def test_the_toggle_comes_back_ticked(self):
        response = self.create(price="ten baht")
        self.assertContains(response, 'name="sync_all"')
        self.assertContains(response, 'value="1" checked')

    def test_the_duplicate_warning_holds_the_fanout_too(self):
        self.create(sync=False)
        product = self.at(self.branch)
        response = self.create(name="Cherry Mousse Pop")
        self.assertContains(response, "may already exist")
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(Product.objects.get().pk, product.pk)


class WhatTheBannerSaysTests(FanoutTestCase):
    """Ticking a box that writes elsewhere has to report where."""

    def test_creating_names_the_branches_it_reached(self):
        response = self.create(follow=True)
        self.assertContains(response, "Also added to BIO HOUSE, Silom.")

    def test_editing_names_the_branches_it_updated(self):
        self.create()
        response = self.edit(self.at(self.branch), price="135.00", follow=True)
        self.assertContains(response, "Also updated at BIO HOUSE, Silom.")

    def test_a_run_that_changed_nothing_says_so(self):
        self.create()
        response = self.edit(self.at(self.branch), follow=True)
        self.assertContains(response, "Every other branch already matched it.")

    def test_without_the_toggle_the_banner_is_unchanged(self):
        response = self.create(sync=False, follow=True)
        self.assertContains(response, "was added to the catalogue.")
        self.assertNotContains(response, "Also added to")


class TheToggleOnScreenTests(FanoutTestCase):
    """It says which branches and which fields, before it is pressed."""

    def test_the_new_form_offers_it_and_names_the_branches(self):
        response = self.client.get(
            f"{reverse('backoffice:product_new')}?branch={self.branch.id}")
        self.assertContains(response, "Sync to all branches")
        self.assertContains(response, "BIO HOUSE, Silom")

    def test_it_is_off_until_it_is_ticked(self):
        response = self.client.get(
            f"{reverse('backoffice:product_new')}?branch={self.branch.id}")
        self.assertNotContains(response, 'name="sync_all" value="1" checked')

    def test_the_edit_form_says_which_fields_travel(self):
        self.create(sync=False)
        response = self.client.get(
            reverse("backoffice:product_detail", args=[self.at(self.branch).id]))
        self.assertContains(response, "except stock and par level")

    def test_a_shop_with_one_branch_is_not_offered_it(self):
        Branch.objects.filter(pk__in=[self.silom.pk, self.bio.pk]).update(
            active=False)
        response = self.client.get(
            f"{reverse('backoffice:product_new')}?branch={self.branch.id}")
        self.assertNotContains(response, "Sync to all branches")


class RemovingAtEveryBranchTests(FanoutTestCase):
    """Remove and Restore carry on-sale status when their box is ticked."""

    def setUp(self):
        super().setUp()
        self.create()
        self.product = self.at(self.branch)

    def remove(self, *, sync=True, follow=False):
        return self.client.post(
            reverse("backoffice:product_archive", args=[self.product.id]),
            {"sync_all": "1"} if sync else {}, follow=follow)

    def restore(self, *, sync=True):
        return self.client.post(
            reverse("backoffice:product_restore", args=[self.product.id]),
            {"sync_all": "1"} if sync else {})

    def test_removing_reaches_every_branch(self):
        self.remove()
        self.assertFalse(self.at(self.silom).active)
        self.assertFalse(self.at(self.bio).active)

    def test_without_the_box_only_this_branch_is_removed(self):
        self.remove(sync=False)
        self.assertFalse(self.at(self.branch).active)
        self.assertTrue(self.at(self.silom).active)

    def test_restoring_reaches_every_branch(self):
        self.remove()
        self.restore()
        self.assertTrue(self.at(self.silom).active)
        self.assertTrue(self.at(self.bio).active)

    def test_restoring_creates_nothing(self):
        self.at(self.bio).delete()
        self.remove()
        self.restore()
        self.assertIsNone(self.at(self.bio))

    def test_the_banner_names_the_branches(self):
        response = self.remove(follow=True)
        self.assertContains(response, "Also removed at BIO HOUSE, Silom.")


class ShelfLifeFormTests(FanoutTestCase):
    """Blank is "not set"; 0 is a real answer — sell the same day."""

    def test_a_number_is_saved(self):
        self.create(sync=False, shelf_life="5")
        self.assertEqual(self.at(self.branch).shelf_life, 5)

    def test_zero_is_kept_as_zero(self):
        self.create(sync=False, shelf_life="0")
        self.assertEqual(self.at(self.branch).shelf_life, 0)

    def test_blank_is_not_set(self):
        self.create(sync=False, shelf_life="")
        self.assertIsNone(self.at(self.branch).shelf_life)

    def test_a_negative_is_refused(self):
        response = self.create(sync=False, shelf_life="-2")
        self.assertContains(response, "Shelf life can")
        self.assertEqual(Product.objects.count(), 0)

    def test_the_form_shows_it(self):
        self.create(sync=False, shelf_life="4")
        response = self.client.get(
            reverse("backoffice:product_detail", args=[self.at(self.branch).id]))
        self.assertContains(response, 'name="shelf_life"')
        self.assertContains(response, 'value="4"')


class BulkEditTests(FanoutTestCase):
    """Quick edit: a shelf life column, and the same sync toggle."""

    def setUp(self):
        super().setUp()
        self.create()
        self.product = self.at(self.branch)

    def bulk(self, *, sync=False, follow=False, **overrides):
        data = {
            "id": [str(self.product.id)], "barcode": [""],
            "name": ["Cherry Mousse Pop"], "description": [""],
            "category": [""], "unit": [""],
            "price": ["120.00"], "cost": ["0"], "shelf_life": [""],
        }
        data.update({k: [v] for k, v in overrides.items()})
        if sync:
            data["sync_all"] = "1"
        return self.client.post(
            f"{reverse('backoffice:product_bulk_edit')}?branch={self.branch.id}",
            data, follow=follow)

    def test_shelf_life_is_saved(self):
        self.bulk(shelf_life="2")
        self.product.refresh_from_db()
        self.assertEqual(self.product.shelf_life, 2)

    def test_blank_clears_it(self):
        Product.objects.filter(pk=self.product.pk).update(shelf_life=4)
        self.bulk(shelf_life="")
        self.product.refresh_from_db()
        self.assertIsNone(self.product.shelf_life)

    def test_without_the_toggle_other_branches_do_not_move(self):
        self.bulk(price="150.00", shelf_life="2")
        self.assertEqual(self.at(self.silom).price, Decimal("120.00"))
        self.assertIsNone(self.at(self.silom).shelf_life)

    def test_with_the_toggle_the_row_reaches_every_branch(self):
        response = self.bulk(sync=True, follow=True,
                             price="150.00", shelf_life="2")
        for branch in (self.silom, self.bio):
            copy = self.at(branch)
            self.assertEqual(copy.price, Decimal("150.00"))
            self.assertEqual(copy.shelf_life, 2)
        self.assertContains(response, "Also synced to BIO HOUSE, Silom.")

    def test_the_grid_shows_the_column_and_the_toggle(self):
        response = self.client.get(
            f"{reverse('backoffice:product_bulk_edit')}?branch={self.branch.id}")
        self.assertContains(response, 'name="shelf_life"')
        self.assertContains(response, 'name="sync_all"')
