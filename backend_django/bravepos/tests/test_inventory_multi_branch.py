"""Inventory page: several branches at once, and a sort on the on-hand table."""
from __future__ import annotations

import csv
import io

from django.test import TestCase
from django.urls import reverse

from bravepos.models import Settings, Staff

from .factories import make_branch, make_product


class InventoryMultiBranchTests(TestCase):
    def setUp(self):
        Settings.objects.get_or_create(id="shop")
        admin = Staff(name="Bo", username="bo", email="bo@therollingpinn.com",
                      role="admin", active=True, backoffice_access=True)
        admin.set_password("correct-horse-battery")
        admin.save()
        self.client.post(reverse("backoffice:login"),
                         {"username": "bo", "password": "correct-horse-battery"})
        self.paragon = make_branch(name="Paragon")
        self.central = make_branch(name="Central World")
        self.silom = make_branch(name="Silom Complex")
        make_product(self.paragon, name="Paragon Cookie", stock=3)
        make_product(self.central, name="Central Cake", stock=9)
        make_product(self.silom, name="Silom Pie", stock=1)

    def _page(self, **params):
        params.setdefault("level", "all")
        res = self.client.get(reverse("backoffice:inventory"), params)
        self.assertEqual(res.status_code, 200)
        return res

    def test_ticked_branches_are_shown_together(self):
        page = self._page(branches=[str(self.paragon.id), str(self.central.id)])
        self.assertContains(page, "Paragon Cookie")
        self.assertContains(page, "Central Cake")
        self.assertNotContains(page, "Silom Pie")
        self.assertContains(page, "2 branches")

    def test_without_ticks_it_shows_the_header_branch(self):
        page = self._page(branch=str(self.silom.id))
        self.assertContains(page, "Silom Pie")
        self.assertNotContains(page, "Paragon Cookie")

    def test_unknown_branch_ids_are_ignored(self):
        page = self._page(branches=["not-a-branch", str(self.central.id)])
        self.assertContains(page, "Central Cake")
        self.assertNotContains(page, "Paragon Cookie")

    def test_sort_by_on_hand(self):
        ids = [str(b.id) for b in (self.paragon, self.central, self.silom)]
        body = self._page(branches=ids, sort="stock_min").content.decode()
        self.assertLess(body.index("Silom Pie"), body.index("Paragon Cookie"))
        self.assertLess(body.index("Paragon Cookie"), body.index("Central Cake"))
        body = self._page(branches=ids, sort="stock_max").content.decode()
        self.assertLess(body.index("Central Cake"), body.index("Silom Pie"))

    def test_pagination_keeps_the_ticked_branches(self):
        page = self._page(branches=[str(self.paragon.id), str(self.central.id)])
        self.assertIn(f"branches={self.paragon.id}", page.context["qs"])
        self.assertIn(f"branches={self.central.id}", page.context["qs"])

    def test_same_name_is_merged_into_one_row_with_stock_summed(self):
        make_product(self.central, name="paragon cookie ", stock=4)   # same product, sloppy name
        page = self._page(branches=[str(self.paragon.id), str(self.central.id)])
        rows = page.context["products"]
        merged = [r for r in rows if r.name.strip().lower() == "paragon cookie"]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].stock, 7)
        self.assertEqual(sorted(merged[0].by_branch), [("Central World", 4), ("Paragon", 3)])
        self.assertIsNone(merged[0].id, "a merged row has no single product page")
        self.assertContains(page, "Central World <b")

    def test_different_costs_show_as_varies(self):
        p = make_product(self.central, name="Paragon Cookie", stock=4)
        p.cost = p.cost + 5
        p.save()
        page = self._page(branches=[str(self.paragon.id), str(self.central.id)])
        self.assertContains(page, "varies")

    def test_level_filter_uses_the_combined_stock(self):
        make_product(self.central, name="Paragon Cookie", stock=-1)   # out here, 3 at Paragon: 2 in all
        page = self._page(branches=[str(self.paragon.id), str(self.central.id)], level="out")
        self.assertNotContains(page, "Paragon Cookie")

    def test_one_branch_is_unchanged(self):
        page = self._page(branches=[str(self.paragon.id)])
        self.assertContains(page, reverse("backoffice:product_detail",
                                          args=[page.context["products"][0].id]))

    def test_export_merges_by_name_with_a_per_branch_split(self):
        make_product(self.central, name="Paragon Cookie", stock=4)
        res = self.client.get(reverse("backoffice:inventory_export"),
                              {"branches": [str(self.paragon.id), str(self.central.id)]})
        rows = list(csv.reader(io.StringIO(res.content.decode("utf-8-sig"))))
        header = rows.index(["No.", "Barcode", "Product Name", "Unit", "Category",
                             "Balance", "By branch"])
        by_name = {r[2]: (r[5], r[6]) for r in rows[header + 1:]}
        self.assertEqual(by_name["Paragon Cookie"], ("7", "Central World 4; Paragon 3"))
        self.assertEqual(by_name["Central Cake"], ("9", "Central World 9"))
