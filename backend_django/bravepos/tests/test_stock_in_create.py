"""Backoffice Create stock in.

Run:
    python manage.py test bravepos.tests.test_stock_in_create \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from decimal import Decimal

from django.test import Client, TestCase
from django.urls import reverse

from bravepos.models import StockDocument, StockMovement
from bravepos.tests.test_user_management_and_audit import make_user, sign_in
from bravepos.views import save_check_document

from .factories import make_branch, make_product


class CreateStockInTests(TestCase):
    def setUp(self):
        self.branch = make_branch(name="Central Ayutthaya")
        self.mousse = make_product(self.branch, name="Raspberry Mousse", stock=10)
        self.mousse.cost = Decimal("40")
        self.mousse.save()
        self.pop = make_product(self.branch, name="Choco Gems Pop", stock=5)
        sign_in(self.client, make_user(username="bo", email="bo@example.com", role="admin"))

    def _url(self):
        return reverse("backoffice:stock_in_new") + f"?branch={self.branch.id}"

    def _create(self, lines, **extra):
        return self.client.post(self._url(), {
            "product_id": [str(p.id) for p, _q, _pr in lines],
            "qty": [str(q) for _p, q, _pr in lines],
            "price": [str(pr) for _p, _q, pr in lines],
            **extra})

    def test_save_adds_stock_and_writes_the_ledger(self):
        res = self._create([(self.mousse, 3, "40"), (self.pop, 2, "15")],
                           vendor="Central Kitchen", ref_no="INV-7", note="morning drop")
        doc = StockDocument.objects.get(type="in")
        self.assertRedirects(res, reverse("backoffice:stock_in_document", args=[doc.id]))
        self.assertEqual((doc.branch, doc.vendor, doc.ref_no, doc.note),
                         (self.branch, "Central Kitchen", "INV-7", "morning drop"))
        self.assertEqual(doc.total, Decimal("150"))
        self.mousse.refresh_from_db()
        self.pop.refresh_from_db()
        self.assertEqual((self.mousse.stock, self.pop.stock), (13, 7))
        mv = StockMovement.objects.get(product=self.mousse)
        self.assertEqual((mv.type, mv.qty, mv.document_no), ("in", 3, doc.document_no))

    def test_rejected_save_moves_nothing(self):
        self.assertContains(self._create([(self.mousse, 0, "0")]), "quantity must be a whole number above 0")
        self.assertContains(self._create([]), "Add at least one product.")
        self.assertFalse(StockDocument.objects.exists())
        self.mousse.refresh_from_db()
        self.assertEqual(self.mousse.stock, 10)

    def test_form_offers_check_documents_to_import_and_past_suppliers(self):
        save_check_document(self.branch, "Close count", {str(self.mousse.id): Decimal("6")})
        self._create([(self.pop, 1, "0")], vendor="Central Kitchen")
        page = self.client.get(self._url())
        self.assertContains(page, "Close count")
        self.assertContains(page, '"qty": "6.00"')
        self.assertContains(page, '<option value="Central Kitchen">')

    def test_viewer_and_packer_cannot_create(self):
        for role in ("viewer", "packer"):
            with self.subTest(role=role):
                client = Client()
                sign_in(client, make_user(username=role, email=f"{role}@example.com", role=role))
                self.assertEqual(client.get(self._url()).status_code, 403)
