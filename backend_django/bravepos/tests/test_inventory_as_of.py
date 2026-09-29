"""Inventory "as of" a past day: stock rewound from today's figure."""
from __future__ import annotations

import csv
import io
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from bravepos.models import AuditLog, Settings, Staff, StockMovement
from bravepos.orders import create_order_from_items

from .factories import make_branch, make_product


class InventoryAsOfTests(TestCase):
    def setUp(self):
        Settings.objects.get_or_create(id="shop")
        admin = Staff(name="Bo", username="bo", email="bo@therollingpinn.com",
                      role="admin", active=True, backoffice_access=True)
        admin.set_password("correct-horse-battery")
        admin.save()
        self.client.post(reverse("backoffice:login"),
                         {"username": "bo", "password": "correct-horse-battery"})
        self.silom = make_branch(name="Silom")
        self.paragon = make_branch(name="Paragon")
        self.cake = make_product(self.silom, name="Mooncake", stock=10)
        self.today = timezone.localdate()
        self.yesterday = self.today - timedelta(days=1)
        # Everything so far happened ten days ago.
        self._backdate(AuditLog.objects.all(), days=10)

    def _backdate(self, qs, days):
        field = "at" if qs.model is AuditLog else "created_at"
        qs.update(**{field: timezone.now() - timedelta(days=days)})

    def _sell(self, product, qty):
        create_order_from_items(
            branch=product.branch,
            items=[{"product_id": str(product.id), "name": product.name,
                    "price": "100.00", "qty": qty}],
            payment_method="Cash", goods_total=Decimal("100.00"),
            paid_amount=Decimal("100.00"),
        )

    def _page(self, **params):
        params.setdefault("level", "all")
        params.setdefault("branch", str(self.silom.id))
        res = self.client.get(reverse("backoffice:inventory"), params)
        self.assertEqual(res.status_code, 200)
        return res

    def _stock(self, res, name="Mooncake"):
        rows = [p for p in res.context["products"] if p.name == name]
        return rows[0].stock if rows else None

    def test_without_a_date_it_is_todays_stock(self):
        self._sell(self.cake, 3)
        res = self._page()
        self.assertIsNone(res.context["as_of"])
        self.assertEqual(self._stock(res), 7)

    def test_sales_since_the_day_are_given_back(self):
        self._sell(self.cake, 3)
        self.assertEqual(self._stock(self._page(as_of=self.yesterday.isoformat())), 10)

    def test_an_absolute_overwrite_since_the_day_is_undone(self):
        self._sell(self.cake, 3)            # 7
        self.cake.refresh_from_db()
        self.cake.stock = 50                # typed over in the product form
        self.cake.save()
        self.assertEqual(self._stock(self._page(as_of=self.yesterday.isoformat())), 10)

    def test_changes_before_the_day_stay(self):
        self._sell(self.cake, 3)
        self._backdate(StockMovement.objects.filter(type="out"), days=3)
        as_of = (self.today - timedelta(days=2)).isoformat()
        self.assertEqual(self._stock(self._page(as_of=as_of)), 7)

    def test_a_product_added_later_is_not_listed(self):
        make_product(self.silom, name="Box 4 mooncake", stock=5)
        res = self._page(as_of=self.yesterday.isoformat())
        self.assertIsNone(self._stock(res, "Box 4 mooncake"))
        self.assertEqual(self._stock(self._page(), "Box 4 mooncake"), 5)

    def test_level_tabs_and_cards_use_the_rewound_stock(self):
        self._sell(self.cake, 10)           # out today, 10 yesterday
        today = self._page(level="out")
        self.assertEqual(today.context["out_of_stock"], 1)
        past = self._page(level="out", as_of=self.yesterday.isoformat())
        self.assertEqual(past.context["out_of_stock"], 0)
        self.assertIsNone(self._stock(past))
        self.assertContains(past, "Stock at the end of")

    def test_merged_branches_sum_the_rewound_stock(self):
        other = make_product(self.paragon, name="Mooncake", stock=4)
        AuditLog.objects.filter(object_id=str(other.id)).update(
            at=timezone.now() - timedelta(days=10))
        self._sell(self.cake, 3)
        self._sell(other, 1)
        res = self._page(branches=[str(self.silom.id), str(self.paragon.id)],
                         as_of=self.yesterday.isoformat())
        self.assertEqual(self._stock(res), 14)

    def test_a_date_before_the_audit_log_is_clamped(self):
        res = self._page(as_of=(self.today - timedelta(days=400)).isoformat())
        self.assertEqual(res.context["as_of"], self.today - timedelta(days=10))

    def test_today_or_later_means_now(self):
        self._sell(self.cake, 3)
        res = self._page(as_of=(self.today + timedelta(days=3)).isoformat())
        self.assertIsNone(res.context["as_of"])
        self.assertEqual(self._stock(res), 7)

    def test_export_follows_the_date(self):
        self._sell(self.cake, 3)
        res = self.client.get(reverse("backoffice:inventory_export"),
                              {"as_of": self.yesterday.isoformat(),
                               "branch": str(self.silom.id)})
        rows = list(csv.reader(io.StringIO(res.content.decode("utf-8-sig"))))
        self.assertEqual(rows[3], ["Date", self.yesterday.strftime("%d %B %Y")])
        body = {r[2]: r[5] for r in rows[6:]}
        self.assertEqual(body["Mooncake"], "10")

    def test_links_on_the_page_keep_the_date(self):
        res = self._page(as_of=self.yesterday.isoformat())
        self.assertIn(f"as_of={self.yesterday.isoformat()}", res.context["qs"])
