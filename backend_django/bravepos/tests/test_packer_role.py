"""The "packer" role: dashboard + Check stock in the backoffice, never a till.

Run:
    python manage.py test bravepos.tests.test_packer_role \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from bravepos.models import Branch, Product, StockDocument, Staff
from bravepos.tests.test_user_management_and_audit import make_user, sign_in


class PackerBackofficeTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="Silom")
        self.product = Product.objects.create(branch=self.branch, name="Croissant", price=50, stock=10)
        self.packer = make_user(username="packer_team", email="packer@example.com", role="packer")
        sign_in(self.client, self.packer)

    def _create(self):
        self.client.post(
            reverse("backoffice:check_stock_new") + f"?branch={self.branch.id}",
            {"document_name": "Monday", "product_id": [str(self.product.id)], "qty": ["4"]},
        )
        return StockDocument.objects.get(type="check")

    def test_dashboard_and_check_stock_open(self):
        for name in ("dashboard", "check_stock", "check_stock_new"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(f"backoffice:{name}")).status_code, 200)

    def test_create_edit_delete(self):
        doc = self._create()
        self.assertEqual(doc.items.get().reconcile_qty, 4)

        self.client.post(reverse("backoffice:check_stock_document", args=[doc.id]),
                         {"document_name": "Tuesday", "product_id": [str(self.product.id)], "qty": ["7"]})
        doc.refresh_from_db()
        self.assertEqual(doc.document_name, "Tuesday")
        self.assertEqual(doc.items.get().reconcile_qty, 7)

        response = self.client.post(reverse("backoffice:check_stock_delete", args=[doc.id]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(StockDocument.objects.filter(id=doc.id).exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)

    def test_delete_needs_post(self):
        doc = self._create()
        self.client.get(reverse("backoffice:check_stock_delete", args=[doc.id]))
        self.assertTrue(StockDocument.objects.filter(id=doc.id).exists())

    def test_everything_else_refused(self):
        for name in ("transactions", "report_daily", "inventory", "stock_in", "stock_out",
                     "product_list", "discount_list", "branch_list", "staff_list",
                     "customer_list", "shop_settings", "user_list", "audit_log",
                     "report_daily_export"):
            with self.subTest(name=name):
                response = self.client.get(reverse(f"backoffice:{name}"))
                self.assertEqual(response.status_code, 403)
                self.assertContains(response, "Check stock only", status_code=403)

    def test_writes_elsewhere_refused(self):
        self.assertEqual(self.client.post(reverse("backoffice:product_new")).status_code, 403)

    def test_rail_shows_only_dashboard_and_check_stock(self):
        # The rail only — the dashboard body links into reports, which then
        # explain the account is Check stock only.
        response = self.client.get(reverse("backoffice:check_stock"))
        self.assertContains(response, reverse("backoffice:check_stock"))
        for name in ("transactions", "report_daily", "inventory", "product_list", "shop_settings"):
            self.assertNotContains(response, reverse(f"backoffice:{name}") + '"')
        self.assertContains(response, "Packer")

    def test_can_sign_out(self):
        self.assertEqual(self.client.post(reverse("backoffice:logout")).status_code, 302)


class ManagerCanDeleteCheckStockTests(TestCase):
    def test_manager_deletes(self):
        branch = Branch.objects.create(name="Silom")
        doc = StockDocument.objects.create(branch=branch, type="check", document_no="CK1")
        sign_in(self.client, make_user(username="mgr", email="mgr@example.com", role="cashier"))
        self.client.post(reverse("backoffice:check_stock_delete", args=[doc.id]))
        self.assertFalse(StockDocument.objects.filter(id=doc.id).exists())

    def test_only_check_documents(self):
        branch = Branch.objects.create(name="Silom")
        doc = StockDocument.objects.create(branch=branch, type="in", document_no="IN1")
        sign_in(self.client, make_user(username="mgr", email="mgr@example.com", role="cashier"))
        response = self.client.post(reverse("backoffice:check_stock_delete", args=[doc.id]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(StockDocument.objects.filter(id=doc.id).exists())


class UsersPagePackerTests(TestCase):
    def test_create_packer(self):
        sign_in(self.client, make_user())
        self.client.post(reverse("backoffice:user_new"), {
            "name": "Packer Team", "username": "packer_team", "role": "packer",
            "active": "on", "password": "long-enough-pass",
        })
        self.assertEqual(Staff.objects.get(username="packer_team").role, "packer")


class PackerNeverOpensATillTests(TestCase):
    def test_pin_login_refused_and_not_on_picker(self):
        branch = Branch.objects.create(name="Silom")
        packer = make_user(username="pk", email="pk@example.com", role="packer")
        packer.set_pin("4321")
        packer.save()
        packer.branches.add(branch)
        api = APIClient()
        response = api.post("/api/auth/pin-login", {
            "branch_id": str(branch.id), "staff_id": str(packer.id), "pin": "4321",
        }, format="json")
        self.assertEqual(response.status_code, 403)
        users = api.get("/api/auth/branch-users", {"branch_id": str(branch.id)}).json()["users"]
        self.assertNotIn(str(packer.id), [u["id"] for u in users])
