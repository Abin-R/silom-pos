"""Backoffice Stock in / Stock out reports: a document list and a per-product
roll-up of the same documents, each with a CSV export."""
from __future__ import annotations

import csv
import io

from django.test import TestCase
from django.urls import reverse

from bravepos.models import BranchSession, Settings, Staff, Unit

from .factories import make_branch, make_product


class StockReportTests(TestCase):
    def setUp(self):
        Settings.objects.get_or_create(id="shop")
        password = "correct-horse-battery"
        admin = Staff(name="Bo", username="bo", email="bo@therollingpinn.com",
                      role="admin", active=True, backoffice_access=True)
        admin.set_password(password)
        admin.save()
        self.branch = make_branch(name="Paragon")
        self.cookie = make_product(self.branch, name="Confetti Cookie", stock=50)
        self.cookie.barcode = "38022"
        self.cookie.unit = Unit.objects.create(name="ชิ้น", branch=self.branch)
        self.cookie.save()
        self.cake = make_product(self.branch, name="Mochi Cake", stock=5)

        staff = Staff.objects.create(name="Nok", email="nok@test.local",
                                     password_hash="x", role="admin")
        staff.branches.add(self.branch)
        self.token = BranchSession.objects.create(
            token="tk3" * 12, branch=self.branch, staff=staff).token
        self.client.post(reverse("backoffice:login"),
                         {"username": "bo", "password": password})

    def _doc(self, type_, lines, **extra):
        res = self.client.post("/api/stock-documents", {
            "type": type_, **extra,
            "items": [{"product_id": str(p.id), "product_name": p.name,
                       "barcode": p.barcode, "qty": qty, "price": "10.00",
                       "discount": str(disc), "total": str(10 * qty - disc)}
                      for p, qty, disc in lines],
        }, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(res.status_code, 201, res.content)
        return res.json()

    def _get(self, name, **params):
        params.setdefault("branch", str(self.branch.id))
        res = self.client.get(reverse(f"backoffice:{name}"), params)
        self.assertEqual(res.status_code, 200)
        return res

    def _csv(self, name, **params):
        res = self._get(name, **params)
        self.assertIn("text/csv", res["Content-Type"])
        return list(csv.reader(io.StringIO(res.content.decode("utf-8-sig"))))

    def test_documents_view_lists_only_its_direction(self):
        doc_in = self._doc("in", [(self.cookie, 24, 0)], vendor="Bakery Co", ref_no="PO-7")
        doc_out = self._doc("out", [(self.cake, 2, 0)], reason="Expired")

        page = self._get("stock_in")
        self.assertContains(page, doc_in["document_no"])
        self.assertContains(page, "Bakery Co")
        self.assertNotContains(page, doc_out["document_no"])

        page = self._get("stock_out")
        self.assertContains(page, doc_out["document_no"])
        self.assertContains(page, "Expired")
        self.assertNotContains(page, doc_in["document_no"])

    def test_documents_search_by_field(self):
        self._doc("in", [(self.cookie, 1, 0)], vendor="Bakery Co")
        self._doc("in", [(self.cake, 1, 0)], vendor="Dairy Farm")
        page = self._get("stock_in", field="party", q="dairy")
        self.assertContains(page, "Dairy Farm")
        self.assertNotContains(page, "Bakery Co")

    def test_product_view_sums_lines_across_documents(self):
        self._doc("in", [(self.cookie, 24, 0), (self.cake, 3, 0)])
        self._doc("in", [(self.cookie, 6, 5)])
        rows = self._csv("stock_in_export", view="products")
        header = rows.index(["#", "Barcode", "Product Name", "Unit", "Document Qty.",
                             "Quantity", "Total Discount", "Total"])
        body = {r[2]: r for r in rows[header + 1:-1]}
        self.assertEqual(body["Confetti Cookie"][1:], ["38022", "Confetti Cookie", "ชิ้น",
                                                      "2", "30", "5.00", "295.00"])
        self.assertEqual(body["Mochi Cake"][4:6], ["1", "3"])
        self.assertEqual(rows[-1][5:], ["33", "5.00", "325.00"])

    def test_product_view_page_and_sorts_render(self):
        self._doc("out", [(self.cookie, 4, 0)], reason="Damaged")
        for sort in ("name", "newest", "price", "onhand"):
            with self.subTest(sort=sort):
                self.assertContains(self._get("stock_out", view="products", sort=sort),
                                    "Confetti Cookie")

    def test_product_view_survives_a_deleted_product(self):
        self._doc("out", [(self.cake, 2, 0)], reason="Expired")
        self.cake.delete()
        self.assertContains(self._get("stock_out", view="products"), "Mochi Cake")

    def test_documents_export(self):
        doc = self._doc("out", [(self.cake, 2, 0)], reason="Expired", receiver="Staff meal")
        rows = self._csv("stock_out_export")
        self.assertIn(["Created", "Document No.", "Ref. No.", "Receiver", "Reason",
                       "Lines", "Total", "Created by", "Note"], rows)
        self.assertEqual(rows[-1][1:6], [doc["document_no"], "", "Staff meal", "Expired", "1"])

    def test_scoped_to_the_selected_branch(self):
        doc = self._doc("in", [(self.cookie, 1, 0)])
        other = make_branch(name="Central World")
        self.assertNotContains(self._get("stock_in", branch=str(other.id)), doc["document_no"])
