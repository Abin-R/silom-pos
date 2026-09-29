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

    def test_export_covers_every_ticked_branch_with_a_branch_column(self):
        res = self.client.get(reverse("backoffice:inventory_export"),
                              {"branches": [str(self.paragon.id), str(self.central.id)]})
        rows = list(csv.reader(io.StringIO(res.content.decode("utf-8-sig"))))
        header = rows.index(["No.", "Barcode", "Product Name", "Unit", "Category",
                             "Balance", "Branch"])
        by_name = {r[2]: r[6] for r in rows[header + 1:]}
        self.assertEqual(by_name, {"Paragon Cookie": "Paragon", "Central Cake": "Central World"})
