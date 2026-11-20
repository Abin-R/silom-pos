"""Backoffice: editing a saved stock in / stock out — Type and Note only.

Run:
    python manage.py test bravepos.tests.test_stock_document_edit \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from bravepos.models import BranchSession, Staff, StockDocument, StockInReason, StockMovement
from bravepos.tests.test_user_management_and_audit import make_user, sign_in
from bravepos.views import save_stock_in_document, save_stock_out_document

from .factories import make_branch, make_product


class _Base(TestCase):
    def setUp(self):
        self.branch = make_branch(name="Central Ayutthaya")
        self.mousse = make_product(self.branch, name="Raspberry Mousse", stock=10)
        StockInReason.objects.get_or_create(branch=self.branch, name="Purchase")
        StockInReason.objects.get_or_create(branch=self.branch, name="Transfer")
        sign_in(self.client, make_user(username="bo", email="bo@example.com", role="admin"))
        self.out = save_stock_out_document(
            self.branch, [(self.mousse, Decimal(3), Decimal(40), Decimal(0))], "Expired",
            receiver="Nok", ref_no="B-12", note="bin day")
        self.inn = save_stock_in_document(
            self.branch, [(self.mousse, Decimal(5), Decimal(40), Decimal(0))],
            vendor="Makro", ref_no="INV-1", note="", reason="Purchase")

    def _edit(self, doc, **data):
        kind = "stock_in" if doc.type == "in" else "stock_out"
        return self.client.post(reverse(f"backoffice:{kind}_document_edit", args=[doc.id]), data)


class EditStockDocumentTests(_Base):
    def test_document_page_links_to_edit(self):
        page = self.client.get(reverse("backoffice:stock_out_document", args=[self.out.id]))
        self.assertContains(page, reverse("backoffice:stock_out_document_edit", args=[self.out.id]))

    def test_edit_page_has_only_type_and_note_inputs(self):
        page = self.client.get(reverse("backoffice:stock_out_document_edit", args=[self.out.id]))
        self.assertContains(page, 'name="reason"')
        self.assertContains(page, 'name="note"')
        for field in ("receiver", "ref_no", "vendor", "product_id", "qty", "price"):
            self.assertNotContains(page, f'name="{field}"')

    def test_stock_out_type_and_note_change_nothing_else(self):
        res = self._edit(self.out, reason="Damaged (In-Store)", note="dropped",
                         receiver="Someone else", ref_no="X", qty="99")
        self.assertRedirects(res, reverse("backoffice:stock_out_document", args=[self.out.id]))
        self.out.refresh_from_db()
        self.mousse.refresh_from_db()
        self.assertEqual((self.out.reason, self.out.note, self.out.receiver, self.out.ref_no),
                         ("Damaged (In-Store)", "dropped", "Nok", "B-12"))
        self.assertEqual(self.out.items.get().qty, 3)
        self.assertEqual(self.mousse.stock, 12)  # 10 - 3 + 5, untouched by the edit
        mv = StockMovement.objects.get(document_no=self.out.document_no)
        self.assertEqual(mv.note, "Damaged (In-Store)")

    def test_stock_in_type_and_note(self):
        self._edit(self.inn, reason="Transfer", note="from Silom")
        self.inn.refresh_from_db()
        self.assertEqual((self.inn.reason, self.inn.note, self.inn.vendor), ("Transfer", "from Silom", "Makro"))
        self.assertEqual(StockMovement.objects.get(document_no=self.inn.document_no).note, "Transfer")

    def test_unknown_type_refused(self):
        res = self._edit(self.out, reason="Not a type", note="x")
        self.assertContains(res, "Choose a type.")
        self.out.refresh_from_db()
        self.assertEqual((self.out.reason, self.out.note), ("Expired", "bin day"))

    def test_retired_type_can_be_kept(self):
        StockDocument.objects.filter(id=self.out.id).update(reason="Old reason")
        page = self.client.get(reverse("backoffice:stock_out_document_edit", args=[self.out.id]))
        self.assertContains(page, '<option value="Old reason" selected>')
        self._edit(self.out, reason="Old reason", note="kept")
        self.out.refresh_from_db()
        self.assertEqual((self.out.reason, self.out.note), ("Old reason", "kept"))

    def test_viewer_cannot_edit(self):
        self.client = self.client_class()
        sign_in(self.client, make_user(username="v", email="v@example.com", role="viewer"))
        page = self.client.get(reverse("backoffice:stock_out_document", args=[self.out.id]))
        self.assertNotContains(page, reverse("backoffice:stock_out_document_edit", args=[self.out.id]))
        self.assertEqual(self._edit(self.out, reason="Expired", note="x").status_code, 403)
        self.out.refresh_from_db()
        self.assertEqual(self.out.note, "bin day")


class TillEditStockDocumentTests(_Base):
    """``PATCH /api/stock-documents/<id>`` — the till's Edit, same rule."""

    def setUp(self):
        super().setUp()
        self.staff = Staff.objects.create(name="Nok", email="nok@test.local",
                                          password_hash="x", role="cashier")
        self.staff.branches.add(self.branch)
        self.token = BranchSession.objects.create(
            token="tk7" * 12, branch=self.branch, staff=self.staff).token

    def _patch(self, doc, body):
        return self.client.patch(f"/api/stock-documents/{doc.id}", body,
                                 content_type="application/json",
                                 HTTP_AUTHORIZATION=f"Bearer {self.token}")

    def test_type_and_note_only(self):
        res = self._patch(self.out, {"reason": "Damaged (In-Store)", "note": "dropped",
                                     "receiver": "X", "ref_no": "Y", "items": []})
        self.assertEqual(res.status_code, 200)
        self.assertEqual((res.json()["reason"], res.json()["note"]), ("Damaged (In-Store)", "dropped"))
        self.out.refresh_from_db()
        self.assertEqual((self.out.receiver, self.out.ref_no, self.out.items.count()), ("Nok", "B-12", 1))
        self.assertEqual(StockMovement.objects.get(document_no=self.out.document_no).note,
                         "Damaged (In-Store)")

    def test_note_alone_keeps_the_type(self):
        self.assertEqual(self._patch(self.inn, {"note": "late van"}).status_code, 200)
        self.inn.refresh_from_db()
        self.assertEqual((self.inn.reason, self.inn.note), ("Purchase", "late van"))

    def test_unknown_type_refused(self):
        res = self._patch(self.out, {"reason": "Not a type"})
        self.assertEqual(res.status_code, 400)
        self.out.refresh_from_db()
        self.assertEqual(self.out.reason, "Expired")

    def test_needs_manage_inventory(self):
        self.staff.permissions = []
        self.staff.save()
        self.assertEqual(self._patch(self.out, {"note": "x"}).status_code, 403)

    def test_other_branch_not_found(self):
        other = save_stock_out_document(
            make_branch(name="Paragon"), [], "Expired")
        self.assertEqual(self._patch(other, {"note": "x"}).status_code, 404)
