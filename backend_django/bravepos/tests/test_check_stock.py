"""Backoffice Check stock: a named product + quantity list that never moves
stock, stays editable, and is what the till's Stock-In → Import Documents
loads (via ``GET /api/stock-documents?type=check``)."""
from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from bravepos.models import BranchSession, Settings, Staff, StockDocument

from .factories import make_branch, make_product


class _CheckStockBase(TestCase):
    def setUp(self):
        Settings.objects.get_or_create(id="shop")
        password = "correct-horse-battery"
        admin = Staff(name="Bo", username="bo", email="bo@therollingpinn.com",
                      role="admin", active=True, backoffice_access=True)
        admin.set_password(password)
        admin.save()
        self.branch = make_branch(name="Central Ayutthaya")
        self.mousse = make_product(self.branch, name="Raspberry Mousse", stock=3)
        self.pop = make_product(self.branch, name="Choco Gems Pop", stock=74)

        staff = Staff.objects.create(name="Nok", email="nok@test.local",
                                     password_hash="x", role="admin")
        staff.branches.add(self.branch)
        self.token = BranchSession.objects.create(
            token="tk4" * 12, branch=self.branch, staff=staff).token
        self.client.post(reverse("backoffice:login"),
                         {"username": "bo", "password": password})

    def _create(self, lines, name="ตรวจนับสินค้า", branch=None):
        return self.client.post(
            reverse("backoffice:check_stock_new") + f"?branch={(branch or self.branch).id}",
            {"document_name": name,
             "product_id": [str(p.id) for p, _ in lines],
             "qty": [str(q) for _, q in lines]})


class CheckStockTests(_CheckStockBase):
    def test_create_saves_quantities_and_leaves_stock_alone(self):
        res = self._create([(self.mousse, 8), (self.pop, 2)])
        doc = StockDocument.objects.get(type="check")
        self.assertRedirects(res, reverse("backoffice:check_stock_document", args=[doc.id]))
        self.assertEqual(doc.branch, self.branch)
        self.assertEqual(doc.document_name, "ตรวจนับสินค้า")
        self.assertEqual(doc.created_by, "Bo")
        self.assertTrue(doc.document_no.startswith("CK"))
        line = doc.items.get(product=self.mousse)
        self.assertEqual((line.reconcile_qty, line.before_qty, line.qty), (8, 3, 5))
        self.mousse.refresh_from_db()
        self.assertEqual(self.mousse.stock, 3)

    def test_list_shows_the_branch_documents(self):
        self._create([(self.mousse, 1)], name="Monday delivery")
        page = self.client.get(reverse("backoffice:check_stock"), {"branch": str(self.branch.id)})
        self.assertContains(page, "Monday delivery")
        other = make_branch(name="Paragon")
        page = self.client.get(reverse("backoffice:check_stock"), {"branch": str(other.id)})
        self.assertNotContains(page, "Monday delivery")

    def test_edit_changes_name_and_lines(self):
        self._create([(self.mousse, 8), (self.pop, 2)])
        doc = StockDocument.objects.get(type="check")
        self.mousse.stock = 50
        self.mousse.save()
        res = self.client.post(reverse("backoffice:check_stock_document", args=[doc.id]), {
            "document_name": "Renamed",
            "product_id": [str(self.mousse.id)], "qty": ["6"]})
        self.assertEqual(res.status_code, 302)
        doc.refresh_from_db()
        self.assertEqual(doc.document_name, "Renamed")
        line = doc.items.get()
        # On-hand is kept from when the line was first added, not re-read.
        self.assertEqual((line.product_id, line.reconcile_qty, line.before_qty), (self.mousse.id, 6, 3))

    def test_rejects_empty_negative_and_foreign_products(self):
        self.assertContains(self._create([]), "Add at least one product.")
        self.assertContains(self._create([(self.mousse, -1)]), "can&#x27;t be negative")
        foreign = make_product(make_branch(name="Elsewhere"), name="Not ours")
        self.assertContains(self._create([(foreign, 4)]), "Add at least one product.")
        self.assertFalse(StockDocument.objects.exists())

    def test_till_import_sees_backoffice_document(self):
        self._create([(self.pop, 12)], name="9/10/2026")
        res = self.client.get("/api/stock-documents", {"type": "check"},
                              HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(res.status_code, 200)
        [doc] = res.json()
        self.assertEqual(doc["document_name"], "9/10/2026")
        self.assertEqual(doc["items"][0]["product_id"], str(self.pop.id))
        self.assertEqual(doc["items"][0]["reconcile_qty"], 12)

    def test_till_check_document_opens_in_backoffice(self):
        res = self.client.post("/api/stock-documents", {
            "type": "check", "document_name": "Counted on till",
            "items": [{"product_id": str(self.mousse.id), "qty": 1,
                       "before_qty": 3, "reconcile_qty": 4}],
        }, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(res.status_code, 201)
        page = self.client.get(reverse("backoffice:check_stock_document", args=[res.json()["id"]]))
        self.assertContains(page, "Counted on till")
        self.assertContains(page, 'value="4"')

    def test_viewer_cannot_open_it(self):
        viewer = Staff(name="Vi", username="vi", email="vi@test.local",
                       role="viewer", active=True, backoffice_access=True)
        viewer.set_password("correct-horse-battery")
        viewer.save()
        self.client = self.client_class()
        self.client.post(reverse("backoffice:login"),
                         {"username": "vi", "password": "correct-horse-battery"})
        self.assertEqual(self.client.get(reverse("backoffice:check_stock")).status_code, 403)
        self.assertEqual(self._create([(self.mousse, 1)]).status_code, 403)


class CheckStockApiEditTests(_CheckStockBase):
    """The till edits a saved check document the way the backoffice does."""

    def _put(self, doc_id, body, token=None):
        return self.client.put(f"/api/stock-documents/{doc_id}", body,
                               content_type="application/json",
                               HTTP_AUTHORIZATION=f"Bearer {token or self.token}")

    def test_till_edits_name_and_lines(self):
        self._create([(self.mousse, 8), (self.pop, 2)])
        doc = StockDocument.objects.get(type="check")
        res = self._put(doc.id, {"document_name": "Edited on till", "items": [
            {"product_id": str(self.pop.id), "reconcile_qty": 9}]})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["document_name"], "Edited on till")
        [line] = res.json()["items"]
        self.assertEqual((line["product_id"], line["reconcile_qty"], line["before_qty"]),
                         (str(self.pop.id), 9, 74))
        self.pop.refresh_from_db()
        self.assertEqual(self.pop.stock, 74)

    def test_rejects_bad_edits(self):
        self._create([(self.mousse, 1)])
        doc = StockDocument.objects.get(type="check")
        self.assertEqual(self._put(doc.id, {"items": []}).status_code, 400)
        self.assertEqual(self._put(doc.id, {"items": [
            {"product_id": str(self.mousse.id), "reconcile_qty": -2}]}).status_code, 400)
        self.assertEqual(doc.items.get().reconcile_qty, 1)

    def test_stock_in_documents_are_not_editable(self):
        res = self.client.post("/api/stock-documents", {
            "type": "in", "items": [{"product_id": str(self.mousse.id), "qty": 5}],
        }, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.token}")
        res = self._put(res.json()["id"], {"items": [
            {"product_id": str(self.mousse.id), "reconcile_qty": 1}]})
        self.assertEqual(res.status_code, 400)

    def test_other_branch_cannot_edit(self):
        self._create([(self.mousse, 1)])
        doc = StockDocument.objects.get(type="check")
        other = make_branch(name="Paragon")
        staff = Staff.objects.create(name="Oth", email="oth@test.local",
                                     password_hash="x", role="admin")
        staff.branches.add(other)
        token = BranchSession.objects.create(token="tk5" * 12, branch=other, staff=staff).token
        self.assertEqual(self._put(doc.id, {"items": [
            {"product_id": str(self.mousse.id), "reconcile_qty": 3}]}, token=token).status_code, 404)
