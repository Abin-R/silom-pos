"""Backoffice Create stock out and Manage stock out types.

Run:
    python manage.py test bravepos.tests.test_stock_out_create \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from bravepos.models import StockDocument, StockMovement, StockOutReason
from bravepos.tests.test_user_management_and_audit import make_user, sign_in

from .factories import make_branch, make_product


class _Base(TestCase):
    def setUp(self):
        self.branch = make_branch(name="Central Ayutthaya")
        self.mousse = make_product(self.branch, name="Raspberry Mousse", stock=10)
        self.mousse.cost = Decimal("40")
        self.mousse.save()
        self.pop = make_product(self.branch, name="Choco Gems Pop", stock=5)
        sign_in(self.client, make_user(username="bo", email="bo@example.com", role="admin"))

    def _create(self, lines, reason="Expired", **extra):
        return self.client.post(
            reverse("backoffice:stock_out_new") + f"?branch={self.branch.id}",
            {"reason": reason,
             "product_id": [str(p.id) for p, _q, _pr in lines],
             "qty": [str(q) for _p, q, _pr in lines],
             "price": [str(pr) for _p, _q, pr in lines],
             **extra})


class CreateStockOutTests(_Base):
    def test_save_deducts_stock_and_writes_the_ledger(self):
        res = self._create([(self.mousse, 3, "40"), (self.pop, 2, "15")],
                           receiver="Nok", ref_no="B-12", note="bin day")
        doc = StockDocument.objects.get(type="out")
        self.assertRedirects(res, reverse("backoffice:stock_out_document", args=[doc.id]))
        self.assertEqual((doc.branch, doc.reason, doc.receiver, doc.ref_no, doc.note),
                         (self.branch, "Expired", "Nok", "B-12", "bin day"))
        self.assertTrue(doc.document_no.startswith("#"))
        self.assertEqual(doc.total, Decimal("150"))
        self.assertEqual(doc.items.get(product=self.mousse).total, Decimal("120"))
        self.mousse.refresh_from_db()
        self.pop.refresh_from_db()
        self.assertEqual((self.mousse.stock, self.pop.stock), (7, 3))
        mv = StockMovement.objects.get(product=self.mousse)
        self.assertEqual((mv.type, mv.qty, mv.note, mv.document_no), ("out", 3, "Expired", doc.document_no))

    def test_form_offers_branch_types_and_cost(self):
        page = self.client.get(reverse("backoffice:stock_out_new"), {"branch": str(self.branch.id)})
        self.assertContains(page, "Damaged (In-Store)")
        self.assertContains(page, '"cost": "40.00"')

    def test_rejected_saves_move_nothing(self):
        cases = [
            ([(self.mousse, 1, "0")], "Not a type", "Choose a type."),
            ([], "Expired", "Add at least one product."),
            ([(self.mousse, 0, "0")], "Expired", "quantity must be a whole number above 0"),
            ([(self.mousse, "1.5", "0")], "Expired", "quantity must be a whole number above 0"),
            ([(self.mousse, 1, "-1")], "Expired", "price can&#x27;t be negative"),
        ]
        for lines, reason, msg in cases:
            with self.subTest(msg=msg):
                self.assertContains(self._create(lines, reason=reason), msg)
        self.assertFalse(StockDocument.objects.exists())
        self.mousse.refresh_from_db()
        self.assertEqual(self.mousse.stock, 10)

    def test_hidden_type_is_refused(self):
        StockOutReason.objects.filter(branch=self.branch, name="Expired").update(active=False)
        self.assertContains(self._create([(self.mousse, 1, "0")]), "Choose a type.")

    def test_product_from_another_branch_is_ignored(self):
        other = make_product(make_branch(name="Paragon"), name="Foreign", stock=9)
        self.assertContains(self._create([(other, 1, "0")]), "Add at least one product.")
        other.refresh_from_db()
        self.assertEqual(other.stock, 9)


class StockOutTypesTests(_Base):
    def _url(self):
        return reverse("backoffice:stock_out_types") + f"?branch={self.branch.id}"

    def test_add_appends_to_the_branch(self):
        self.client.post(self._url(), {"name": "Borrow", "name_th": "ยืม"})
        row = StockOutReason.objects.get(branch=self.branch, name="Borrow")
        self.assertEqual(row.name_th, "ยืม")
        last = StockOutReason.objects.filter(branch=self.branch).order_by("-sort_order").first()
        self.assertEqual(last, row)
        self.assertContains(self.client.get(self._url()), "Borrow")

    def test_duplicate_and_blank_are_refused(self):
        self.assertContains(self.client.post(self._url(), {"name": "expired"}), "is already a type")
        self.assertContains(self.client.post(self._url(), {"name": "  "}), "Enter a type name.")

    def test_delete_keeps_saved_documents(self):
        self._create([(self.mousse, 1, "0")], reason="Tasting")
        row = StockOutReason.objects.get(branch=self.branch, name="Tasting")
        self.client.post(reverse("backoffice:stock_out_type_delete", args=[row.id]))
        self.assertFalse(StockOutReason.objects.filter(id=row.id).exists())
        self.assertEqual(StockDocument.objects.get(type="out").reason, "Tasting")


class StockOutRoleTests(TestCase):
    def setUp(self):
        self.branch = make_branch(name="Silom")

    def test_viewer_sees_the_list_but_cannot_create(self):
        sign_in(self.client, make_user(username="acc", email="acc@example.com", role="viewer"))
        page = self.client.get(reverse("backoffice:stock_out"))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, reverse("backoffice:stock_out_new"))
        for name in ("stock_out_new", "stock_out_types"):
            self.assertEqual(self.client.get(reverse(f"backoffice:{name}")).status_code, 403)

    def test_rail_splits_reports_from_management(self):
        # Reports → Inventory reports lists the saved documents; Inventory
        # management opens the create forms.
        sign_in(self.client, make_user(username="mgr", email="mgr@example.com", role="cashier"))
        html = self.client.get(reverse("backoffice:dashboard")).content.decode()
        reports, grp = html.index("Inventory reports"), html.index("Inventory management")
        for name in ("stock_in", "stock_out", "check_stock"):
            with self.subTest(name=name):
                self.assertTrue(reports < html.index(reverse(f"backoffice:{name}") + '"') < grp)
                self.assertLess(grp, html.index(reverse(f"backoffice:{name}_new") + '"'))

    def test_viewer_rail_has_reports_only(self):
        sign_in(self.client, make_user(username="acc", email="acc@example.com", role="viewer"))
        html = self.client.get(reverse("backoffice:dashboard")).content.decode()
        self.assertIn(reverse("backoffice:stock_in") + '"', html)
        self.assertNotIn("Inventory management", html)
