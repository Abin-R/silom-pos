"""Copying one branch's catalogue onto another, from the backoffice.

The page exists for two moments: a branch that has just opened has an empty
product list, and a product added at one shop has to reach the others.  Both
are "make that branch's list look like this one".

The property the whole screen is built around is that it **only adds**.  A
product the target already has keeps its own price, cost, photo and stock —
that is what makes it safe to press on a branch that has been trading for a
month with prices of its own, and safe to press twice.  Most of what is pinned
here is that promise, plus the two ways a copy could quietly corrupt a branch:
by inventing a stock figure, or by pointing the copy at the source branch's own
category row.

Run:
    python manage.py test bravepos.tests.test_product_sync_page \\
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from decimal import Decimal

from django.test import Client, TestCase
from django.urls import reverse

from bravepos import catalog
from bravepos.models import Category, Product, Staff, Unit

from .factories import make_branch, make_product


class CopyCatalogueTests(TestCase):
    """The engine, driven directly — no page in the way."""

    def setUp(self):
        self.source = make_branch(name="BIO HOUSE")
        self.target = make_branch(name="Silom")
        self.unit = Unit.objects.create(name="ชิ้น", order=16)  # shop-wide
        self.cat = Category.objects.create(
            branch=self.source, name="Cookies", color="#00b14f", order=3,
        )
        self.product = Product.objects.create(
            branch=self.source, category=self.cat, unit=self.unit,
            name="Sexy Back Cookie", price=Decimal("95.00"),
            # Negative because deliveries were never recorded — the exact
            # situation that makes copying stock the wrong thing to do.
            stock=-24, is_favorite=True, sort_order=2,
        )

    def test_it_copies_a_missing_product(self):
        catalog.copy_catalogue(self.source, self.target)

        copy = Product.objects.get(branch=self.target, name="Sexy Back Cookie")
        self.assertEqual(copy.price, Decimal("95.00"))
        self.assertTrue(copy.is_favorite)
        self.assertEqual(copy.sort_order, 2)

    def test_stock_never_travels(self):
        catalog.copy_catalogue(self.source, self.target)
        self.assertEqual(Product.objects.get(branch=self.target).stock, 0)

    def test_the_copy_uses_the_targets_own_category(self):
        catalog.copy_catalogue(self.source, self.target)
        copy = Product.objects.get(branch=self.target)

        self.assertEqual(copy.category.branch, self.target)
        self.assertEqual(copy.category.name, "Cookies")
        self.assertNotEqual(copy.category_id, self.cat.id)

    def test_a_shop_wide_unit_is_shared_not_duplicated(self):
        catalog.copy_catalogue(self.source, self.target)
        self.assertEqual(Product.objects.get(branch=self.target).unit_id, self.unit.id)
        self.assertEqual(Unit.objects.count(), 1)

    def test_an_existing_product_is_left_completely_alone(self):
        """The promise the page is built on.  A branch trading at its own price
        must come out of a sync with that price intact."""
        theirs = make_product(self.target, name="Sexy Back Cookie", price="70.00")
        theirs.cost = Decimal("30.00")
        theirs.stock = 12
        theirs.save()

        catalog.copy_catalogue(self.source, self.target)

        theirs.refresh_from_db()
        self.assertEqual(theirs.price, Decimal("70.00"))
        self.assertEqual(theirs.cost, Decimal("30.00"))
        self.assertEqual(theirs.stock, 12)
        self.assertEqual(Product.objects.filter(branch=self.target).count(), 1)

    def test_only_the_missing_ones_are_added(self):
        Product.objects.create(
            branch=self.source, category=self.cat, name="Brownie",
            price=Decimal("75.00"),
        )
        make_product(self.target, name="Sexy Back Cookie", price="70.00")

        report = catalog.copy_catalogue(self.source, self.target)
        actions = {p["name"]: p["action"] for p in report["products"]}

        self.assertEqual(actions["Sexy Back Cookie"], "skipped")
        self.assertEqual(actions["Brownie"], "created")

    def test_running_it_twice_changes_nothing_the_second_time(self):
        catalog.copy_catalogue(self.source, self.target)
        second = catalog.copy_catalogue(self.source, self.target)

        self.assertEqual(Product.objects.filter(branch=self.target).count(), 1)
        self.assertTrue(all(p["action"] == "skipped" for p in second["products"]))

    def test_a_removed_product_does_not_travel(self):
        """A product taken off sale is a decision; seeding a new branch with it
        would quietly undo that decision at the new shop."""
        Product.objects.create(
            branch=self.source, category=self.cat, name="Discontinued Tart",
            price=Decimal("55.00"), active=False,
        )
        catalog.copy_catalogue(self.source, self.target)

        self.assertFalse(
            Product.objects.filter(branch=self.target, name="Discontinued Tart").exists()
        )

    def test_an_uncategorised_product_stays_uncategorised(self):
        Product.objects.create(
            branch=self.source, unit=self.unit, name="Ballerina",
            price=Decimal("650.00"), category=None,
        )
        catalog.copy_catalogue(self.source, self.target)

        copy = Product.objects.get(branch=self.target, name="Ballerina")
        self.assertIsNone(copy.category)

    def test_preview_names_the_products_and_writes_nothing(self):
        make_product(self.target, name="Sexy Back Cookie", price="70.00")
        Product.objects.create(
            branch=self.source, category=self.cat, name="Brownie",
            price=Decimal("75.00"),
        )

        result = catalog.preview(self.source, self.target)

        self.assertEqual(result["adding"], ["Brownie"])
        self.assertEqual(result["already"], 1)
        self.assertEqual(Product.objects.filter(branch=self.target).count(), 1)


class ProductSyncPageTests(TestCase):
    def setUp(self):
        self.source = make_branch(name="BIO HOUSE")
        self.silom = make_branch(name="Silom")
        self.test_branch = make_branch(name="test branch")
        make_product(self.source, name="Brownie", price="75.00")
        make_product(self.source, name="Choco Gems", price="65.00")

        # Backoffice login goes through bravepos.Staff, not auth_user — see
        # backoffice.auth_backend for why.
        boss = Staff(
            name="Boss", username="boss", email="boss@therollingpinn.com",
            role="admin", active=True, backoffice_access=True,
        )
        boss.set_password("correct-horse-battery")
        boss.save()
        signed_in = self.client.post(reverse("backoffice:login"), {
            "username": "boss", "password": "correct-horse-battery",
        })
        self.assertEqual(signed_in.status_code, 302, "admin could not sign in")

        self.url = reverse("backoffice:product_sync")

    def _params(self, targets=None):
        return {
            "source": str(self.source.id),
            "targets": [str(b.id) for b in (targets or [self.silom])],
        }

    # ── Removals travel too ─────────────────────────────────────────────
    def _remove_at_source(self, name):
        """Take a product off sale at the source, the way the catalogue page
        does it — `active=False`, not a row delete."""
        product = Product.objects.get(branch=self.source, name=name)
        product.active = False
        product.save(update_fields=["active"])
        return product

    def test_removing_at_the_source_takes_the_copy_off_sale(self):
        """The bug this fixes.

        `source_catalogue` reads active rows only, so a removed product simply
        dropped out of the payload — the copy at the target was never looked at
        and stayed on sale, and re-syncing could not fix it because there was
        nothing left to carry the removal.
        """
        self.client.post(self.url, self._params(), follow=True)
        copy = Product.objects.get(branch=self.silom, name="Brownie")
        self.assertTrue(copy.active, "precondition: the copy is on sale")

        self._remove_at_source("Brownie")
        self.client.post(self.url, self._params(), follow=True)

        copy.refresh_from_db()
        self.assertFalse(copy.active)
        # The other product is untouched.
        self.assertTrue(
            Product.objects.get(branch=self.silom, name="Choco Gems").active)

    def test_the_copy_is_retired_not_deleted(self):
        """Same bargain as the catalogue page: the row survives, so the
        target's own sales history and profit figures do not move."""
        self.client.post(self.url, self._params(), follow=True)
        self._remove_at_source("Brownie")
        self.client.post(self.url, self._params(), follow=True)

        self.assertTrue(
            Product.objects.filter(branch=self.silom, name="Brownie").exists())

    def test_it_never_creates_a_removed_product_at_the_target(self):
        """A branch that never had the product must not gain a dead row: the
        retire pass matches existing rows only."""
        self._remove_at_source("Brownie")
        self.client.post(self.url, self._params(), follow=True)

        self.assertFalse(
            Product.objects.filter(branch=self.silom, name="Brownie").exists())
        self.assertTrue(
            Product.objects.filter(branch=self.silom, name="Choco Gems").exists())

    def test_the_preview_names_what_it_would_take_off_sale(self):
        """This page writes to branches nobody is looking at, so a removal has
        to be visible before it happens, not only after."""
        self.client.post(self.url, self._params(), follow=True)
        self._remove_at_source("Brownie")

        res = self.client.get(self.url, self._params())
        self.assertContains(res, "Off sale: Brownie")

    def test_the_message_reports_the_removals(self):
        self.client.post(self.url, self._params(), follow=True)
        self._remove_at_source("Brownie")
        res = self.client.post(self.url, self._params(), follow=True)

        self.assertContains(res, "1 product taken off sale")
        self.assertContains(res, "BIO HOUSE has removed it")

    def test_unticking_the_box_leaves_the_copy_on_sale(self):
        """The escape hatch: a branch that still sells something the source has
        dropped can be synced without losing it."""
        self.client.post(self.url, self._params(), follow=True)
        self._remove_at_source("Brownie")

        params = self._params()
        params["retire"] = "off"
        self.client.post(self.url, params, follow=True)

        self.assertTrue(
            Product.objects.get(branch=self.silom, name="Brownie").active)

    def test_a_branch_that_already_retired_it_is_not_reported_again(self):
        """Idempotent: pressing sync twice reports the removal once, because
        the second pass finds nothing still on sale to retire."""
        self.client.post(self.url, self._params(), follow=True)
        self._remove_at_source("Brownie")
        self.client.post(self.url, self._params(), follow=True)

        res = self.client.post(self.url, self._params(), follow=True)
        self.assertContains(res, "Nothing was changed")

    def test_the_page_lists_every_branch(self):
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 200)
        for name in ("BIO HOUSE", "Silom", "test branch"):
            self.assertContains(res, name)

    def test_a_get_only_previews_and_writes_nothing(self):
        res = self.client.get(self.url, self._params())

        self.assertContains(res, "Brownie")
        self.assertContains(res, "Choco Gems")
        self.assertEqual(Product.objects.filter(branch=self.silom).count(), 0)

    def test_the_preview_flags_a_branch_with_no_products(self):
        """The new-branch case is the main reason this page exists, so it is
        called out rather than left to be inferred from a zero."""
        res = self.client.get(self.url, self._params())
        self.assertContains(res, "Empty branch")

    def test_posting_copies_to_the_chosen_branch_only(self):
        res = self.client.post(self.url, self._params(), follow=True)
        self.assertEqual(res.status_code, 200)

        self.assertEqual(Product.objects.filter(branch=self.silom).count(), 2)
        self.assertEqual(Product.objects.filter(branch=self.test_branch).count(), 0)

    def test_several_branches_in_one_press(self):
        self.client.post(
            self.url, self._params([self.silom, self.test_branch]), follow=True,
        )
        self.assertEqual(Product.objects.filter(branch=self.silom).count(), 2)
        self.assertEqual(Product.objects.filter(branch=self.test_branch).count(), 2)

    def test_the_message_says_what_was_left_alone(self):
        res = self.client.post(self.url, self._params(), follow=True)
        self.assertContains(res, "2 products copied")
        self.assertContains(res, "left exactly as they were")

    def test_a_second_press_reports_that_there_was_nothing_to_do(self):
        self.client.post(self.url, self._params(), follow=True)
        res = self.client.post(self.url, self._params(), follow=True)

        self.assertContains(res, "Nothing was changed")
        self.assertEqual(Product.objects.filter(branch=self.silom).count(), 2)

    def test_a_new_product_added_later_is_the_only_thing_copied(self):
        """The second job this page does: one product added at BIO HOUSE, and
        the branch's own prices must survive the sync that carries it."""
        self.client.post(self.url, self._params(), follow=True)
        theirs = Product.objects.get(branch=self.silom, name="Brownie")
        theirs.price = Decimal("80.00")
        theirs.save()

        make_product(self.source, name="Lemon Tart", price="85.00")
        self.client.post(self.url, self._params(), follow=True)

        self.assertEqual(Product.objects.filter(branch=self.silom).count(), 3)
        theirs.refresh_from_db()
        self.assertEqual(theirs.price, Decimal("80.00"))

    def test_a_branch_cannot_be_told_to_sync_onto_itself(self):
        """The ids come from a form anyone can edit, and a branch reading and
        writing the same rows is not a state worth reasoning about."""
        res = self.client.post(self.url, {
            "source": str(self.source.id),
            "targets": [str(self.source.id), str(self.silom.id)],
        }, follow=True)
        self.assertEqual(res.status_code, 200)

        self.assertEqual(Product.objects.filter(branch=self.source).count(), 2)
        self.assertEqual(Product.objects.filter(branch=self.silom).count(), 2)

    def test_an_unknown_branch_id_is_ignored(self):
        res = self.client.post(self.url, {
            "source": str(self.source.id),
            "targets": ["8ad0b4c2-0000-4000-8000-000000000000"],
        }, follow=True)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(Product.objects.exclude(branch=self.source).count(), 0)

    def test_it_needs_a_login(self):
        # A fresh client rather than `logout()`: the test client's logout
        # hydrates request.user through `get_user_model()`, which is auth.User
        # with an integer PK, while our Staff PK is a UUID.
        res = Client().post(self.url, self._params())
        self.assertEqual(res.status_code, 302)
        self.assertIn(reverse("backoffice:login"), res["Location"])
        self.assertEqual(Product.objects.filter(branch=self.silom).count(), 0)
