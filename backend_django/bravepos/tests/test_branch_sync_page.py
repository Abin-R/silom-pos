"""Sync branches — the sections beyond products.

Products have their own suite (test_product_sync_page); this covers
categories, stock-out types, promotions, choosing sections, and the old
Catalogue address.

Run:
    python manage.py test bravepos.tests.test_branch_sync_page \\
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from bravepos.models import (
    Category, DiscountType, ParentCategory, Product, Staff, StockOutReason,
)

from .factories import make_branch, make_product


class BranchSyncPageTests(TestCase):
    def setUp(self):
        self.source = make_branch(name="BIO HOUSE")
        self.target = make_branch(name="Silom")
        self.other = make_branch(name="test branch")

        boss = Staff(name="Boss", username="boss", email="boss@example.com",
                     role="admin", active=True, backoffice_access=True)
        boss.set_password("correct-horse-battery")
        boss.save()
        self.client.post(reverse("backoffice:login"), {
            "username": "boss", "password": "correct-horse-battery"})
        self.url = reverse("backoffice:branch_sync")

    def params(self, *sections, targets=None):
        return {
            "source": str(self.source.id),
            "targets": [str(b.id) for b in (targets or [self.target])],
            "sections": list(sections),
            "picked": "1",
        }

    def sync(self, *sections, **kw):
        return self.client.post(self.url, self.params(*sections, **kw), follow=True)

    # ── Page ────────────────────────────────────────────────────────────
    def test_the_sidebar_links_the_page_and_catalogue_has_no_button(self):
        res = self.client.get(reverse("backoffice:product_list"))
        self.assertContains(res, self.url)
        self.assertNotContains(res, "Sync to branch")

    def test_the_old_catalogue_address_lands_here_with_products_ticked(self):
        res = self.client.get(reverse("backoffice:product_sync"),
                              {"source": str(self.source.id)})
        self.assertEqual(res.status_code, 301)
        self.assertIn(self.url, res["Location"])
        self.assertIn("sections=products", res["Location"])

    def test_first_visit_ticks_every_section(self):
        res = self.client.get(self.url)
        for label in ("Products", "Categories", "Promotions", "Stock-Out Dropdowns"):
            self.assertContains(res, label)
        self.assertEqual(len(res.context["section_keys"]), 4)

    def test_an_unticked_section_writes_nothing(self):
        make_product(self.source, name="Brownie")
        Category.objects.create(branch=self.source, name="Empty shelf")
        self.sync("categories")
        self.assertTrue(Category.objects.filter(branch=self.target, name="Empty shelf").exists())
        self.assertFalse(Product.objects.filter(branch=self.target).exists())

    def test_nothing_ticked_writes_nothing(self):
        make_product(self.source, name="Brownie")
        self.sync()
        self.assertFalse(Product.objects.filter(branch=self.target).exists())

    # ── Categories ──────────────────────────────────────────────────────
    def test_an_empty_category_and_its_group_are_copied(self):
        group = ParentCategory.objects.create(branch=self.source, name="Cake")
        Category.objects.create(branch=self.source, name="Mini Cake",
                                parent_category=group, color="#ff0000")
        res = self.client.get(self.url, self.params("categories"))
        self.assertContains(res, "Mini Cake")
        self.assertFalse(Category.objects.filter(branch=self.target, name="Mini Cake").exists())

        self.sync("categories")
        copy = Category.objects.get(branch=self.target, name="Mini Cake")
        self.assertEqual(copy.color, "#ff0000")
        self.assertEqual(copy.parent_category.name, "Cake")
        self.assertEqual(copy.parent_category.branch, self.target)

    def test_a_category_only_the_target_has_is_left_alone(self):
        Category.objects.create(branch=self.target, name="Silom only")
        self.sync("categories")
        self.assertTrue(Category.objects.filter(branch=self.target, name="Silom only").exists())

    # ── Stock-out types ─────────────────────────────────────────────────
    def test_a_missing_stock_out_type_is_added_and_extras_survive(self):
        StockOutReason.objects.create(branch=self.source, name="Event", name_th="งาน")
        StockOutReason.objects.create(branch=self.target, name="Silom thing")
        res = self.client.get(self.url, self.params("stock_out"))
        self.assertContains(res, "Event")

        self.sync("stock_out")
        self.assertEqual(
            StockOutReason.objects.get(branch=self.target, name="Event").name_th, "งาน")
        self.assertTrue(StockOutReason.objects.filter(branch=self.target, name="Silom thing").exists())

    def test_a_hidden_stock_out_type_is_not_created_elsewhere(self):
        StockOutReason.objects.create(branch=self.source, name="Old", active=False)
        self.sync("stock_out")
        self.assertFalse(StockOutReason.objects.filter(branch=self.target, name="Old").exists())

    # ── Promotions ──────────────────────────────────────────────────────
    def promo(self, branch=None, **kw):
        kw.setdefault("name", "Staff 20%")
        kw.setdefault("value", Decimal("20"))
        return DiscountType.objects.create(branch=branch or self.source, **kw)

    def test_a_running_promotion_is_copied_and_linked(self):
        dt = self.promo()
        self.sync("promotions")
        copy = DiscountType.objects.get(branch=self.target, name="Staff 20%")
        self.assertEqual(copy.group_id, dt.group_id)
        self.assertEqual(copy.code, dt.code)
        self.assertFalse(DiscountType.objects.filter(branch=self.other).exists())

    def test_a_second_sync_edits_the_copy_rather_than_adding_one(self):
        dt = self.promo()
        self.sync("promotions")
        dt.value = Decimal("25")
        dt.save()
        self.sync("promotions")
        copies = DiscountType.objects.filter(branch=self.target)
        self.assertEqual(copies.count(), 1)
        self.assertEqual(copies.get().value, Decimal("25"))

    def test_an_ended_promotion_does_not_travel(self):
        self.promo(end_date=timezone.localdate() - timedelta(days=1))
        self.sync("promotions")
        self.assertFalse(DiscountType.objects.filter(branch=self.target).exists())

    def test_a_paused_promotion_travels_paused(self):
        self.promo(active=False)
        self.sync("promotions")
        self.assertFalse(DiscountType.objects.get(branch=self.target).active)

    def test_the_targets_own_promotions_are_untouched(self):
        mine = self.promo(branch=self.target, name="Silom happy hour", value=Decimal("10"))
        same_name = self.promo(branch=self.target, value=Decimal("5"))
        self.promo()
        res = self.client.get(self.url, self.params("promotions"))
        self.assertContains(res, "already has its own promotion")

        self.sync("promotions")
        mine.refresh_from_db()
        same_name.refresh_from_db()
        self.assertEqual(mine.value, Decimal("10"))
        self.assertEqual(same_name.value, Decimal("5"))
        self.assertEqual(DiscountType.objects.filter(branch=self.target).count(), 2)

    def test_a_promotion_naming_a_missing_product_is_skipped_and_named(self):
        latte = make_product(self.source, name="Latte")
        dt = self.promo(applies_to=DiscountType.APPLIES_PRODUCTS)
        dt.products.set([latte])
        res = self.client.get(self.url, self.params("promotions"))
        self.assertContains(res, "doesn")
        self.assertContains(res, "Latte")

        res = self.sync("promotions")
        self.assertFalse(DiscountType.objects.filter(branch=self.target).exists())
        self.assertContains(res, "Skipped 1 promotions")

    def test_products_and_promotions_together_land_in_one_run(self):
        latte = make_product(self.source, name="Latte")
        dt = self.promo(applies_to=DiscountType.APPLIES_PRODUCTS)
        dt.products.set([latte])
        res = self.client.get(self.url, self.params("products", "promotions"))
        self.assertNotContains(res, "doesn")

        self.sync("products", "promotions")
        copy = DiscountType.objects.get(branch=self.target)
        self.assertEqual([p.branch_id for p in copy.products.all()], [self.target.id])

    def test_a_get_writes_nothing(self):
        self.promo()
        StockOutReason.objects.create(branch=self.source, name="Event")
        Category.objects.create(branch=self.source, name="Empty shelf")
        make_product(self.source, name="Brownie")
        before = (DiscountType.objects.count(), StockOutReason.objects.count(),
                  Category.objects.count(), Product.objects.count())
        self.client.get(self.url, self.params("products", "categories", "stock_out", "promotions"))
        self.assertEqual(before, (DiscountType.objects.count(), StockOutReason.objects.count(),
                                  Category.objects.count(), Product.objects.count()))
