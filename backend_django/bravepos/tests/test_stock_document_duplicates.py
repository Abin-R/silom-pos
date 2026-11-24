"""A stock in / out saved twice in a row is one document.

Khlong San's stock out was saved twice 86 ms apart on 2026-11-21 and took the
stock off twice.  The server refuses the repeat from any till build and from
the backoffice's forms.

Run:
    python manage.py test bravepos.tests.test_stock_document_duplicates \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse

from bravepos.models import BranchSession, Staff, StockDocument, StockMovement
from bravepos.tests.test_user_management_and_audit import make_user, sign_in

from .factories import make_branch, make_product


class TillDuplicateStockDocumentTests(TestCase):
    def setUp(self):
        self.branch = make_branch(name="Khlong San")
        self.cookie = make_product(self.branch, name="Red Velvet Cookie", stock=10)
        self.pop = make_product(self.branch, name="Choco Gems Pop", stock=10)
        staff = Staff.objects.create(name="Cashier", role="admin")
        staff.branches.add(self.branch)
        self.token = BranchSession.objects.create(
            branch=self.branch, staff=staff, token="till-dup").token

    def _post(self, type_="out", qty=2, **extra):
        body = {
            "type": type_, "reason": "Expired" if type_ == "out" else "",
            "items": [{"product_id": str(self.cookie.id), "qty": qty},
                      {"product_id": str(self.pop.id), "qty": 1}],
            **extra,
        }
        return self.client.post("/api/stock-documents", body, content_type="application/json",
                                HTTP_AUTHORIZATION=f"Bearer {self.token}")

    def _stock(self):
        self.cookie.refresh_from_db()
        self.pop.refresh_from_db()
        return self.cookie.stock, self.pop.stock

    def test_double_tap_saves_once(self):
        first = self._post()
        second = self._post()
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(StockDocument.objects.filter(type="out").count(), 1)
        self.assertEqual(StockMovement.objects.filter(product=self.cookie).count(), 1)
        self.assertEqual(self._stock(), (8, 9))

    def test_double_tap_on_stock_in_saves_once(self):
        self._post("in")
        self._post("in")
        self.assertEqual(StockDocument.objects.filter(type="in").count(), 1)
        self.assertEqual(self._stock(), (12, 11))

    def test_line_order_does_not_matter(self):
        self._post()
        body = {"type": "out", "reason": "Expired",
                "items": [{"product_id": str(self.pop.id), "qty": "1.00"},
                          {"product_id": str(self.cookie.id), "qty": 2}]}
        res = self.client.post("/api/stock-documents", body, content_type="application/json",
                               HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(StockDocument.objects.count(), 1)

    def test_different_paperwork_is_saved(self):
        for extra in ({"qty": 3}, {"note": "second tray"}, {"reason": "Damaged"}):
            with self.subTest(**extra):
                self.assertEqual(self._post(**extra).status_code, 201)
        self._post()
        self.assertEqual(StockDocument.objects.count(), 4)

    def test_same_paperwork_after_the_window_is_saved(self):
        self._post()
        StockDocument.objects.update(created_at=StockDocument.objects.get().created_at - timedelta(seconds=11))
        self.assertEqual(self._post().status_code, 201)
        self.assertEqual(StockDocument.objects.count(), 2)
        self.assertEqual(self._stock(), (6, 8))

    def test_other_branch_is_not_a_duplicate(self):
        other = make_branch(name="Silom")
        StockDocument.objects.create(branch=other, type="out", document_no="#x",
                                     reason="Expired", created_by="Cashier")
        self.assertEqual(self._post().status_code, 201)


class BackofficeDuplicateStockDocumentTests(TestCase):
    def setUp(self):
        self.branch = make_branch(name="Central Ayutthaya")
        self.mousse = make_product(self.branch, name="Raspberry Mousse", stock=10)
        sign_in(self.client, make_user(username="bo", email="bo@example.com", role="admin"))

    def _post(self, name, reason, qty=3):
        return self.client.post(reverse(f"backoffice:{name}") + f"?branch={self.branch.id}", {
            "reason": reason, "product_id": [str(self.mousse.id)],
            "qty": [str(qty)], "price": ["40"], "note": "bin day",
        })

    def test_double_click_on_stock_out_saves_once(self):
        first = self._post("stock_out_new", "Expired")
        second = self._post("stock_out_new", "Expired")
        doc = StockDocument.objects.get(type="out")
        for res in (first, second):
            self.assertRedirects(res, reverse("backoffice:stock_out_document", args=[doc.id]))
        self.mousse.refresh_from_db()
        self.assertEqual(self.mousse.stock, 7)

    def test_double_click_on_stock_in_saves_once(self):
        first = self._post("stock_in_new", "Shop Supply")
        second = self._post("stock_in_new", "Shop Supply")
        doc = StockDocument.objects.get(type="in")
        for res in (first, second):
            self.assertRedirects(res, reverse("backoffice:stock_in_document", args=[doc.id]))
        self.mousse.refresh_from_db()
        self.assertEqual(self.mousse.stock, 13)

    def test_different_quantity_is_saved(self):
        self._post("stock_out_new", "Expired")
        self._post("stock_out_new", "Expired", qty=2)
        self.assertEqual(StockDocument.objects.filter(type="out").count(), 2)


class TillZeroQuantityTests(TestCase):
    """A stock in / out line at 0 saved paperwork that moved nothing (Central
    Pinklao, 2026-11-23).  Older till builds don't hold Save, so the server refuses."""

    def setUp(self):
        self.branch = make_branch(name="Central Pinklao")
        self.cookie = make_product(self.branch, name="Red Velvet Cookie", stock=10)
        staff = Staff.objects.create(name="Cashier", role="admin")
        staff.branches.add(self.branch)
        self.token = BranchSession.objects.create(
            branch=self.branch, staff=staff, token="till-zero").token

    def _post(self, type_, qty):
        return self.client.post("/api/stock-documents", {
            "type": type_, "reason": "Expired" if type_ == "out" else "",
            "items": [{"product_id": str(self.cookie.id), "product_name": "Red Velvet Cookie",
                       "qty": qty}],
        }, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.token}")

    def test_zero_or_negative_or_blank_is_refused(self):
        for type_ in ("in", "out"):
            for qty in (0, "0", -1, "", None):
                with self.subTest(type=type_, qty=qty):
                    res = self._post(type_, qty)
                    self.assertEqual(res.status_code, 400)
                    self.assertIn("Red Velvet Cookie: enter a quantity above 0.", res.json()["detail"])
        self.assertFalse(StockDocument.objects.exists())
        self.cookie.refresh_from_db()
        self.assertEqual(self.cookie.stock, 10)

    def test_check_stock_may_still_count_zero(self):
        res = self.client.post("/api/stock-documents", {
            "type": "check", "document_name": "Night count",
            "items": [{"product_id": str(self.cookie.id), "qty": -10,
                       "before_qty": 10, "reconcile_qty": 0}],
        }, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(res.status_code, 201, res.content)
