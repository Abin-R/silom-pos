"""The "viewer" role: read-only reports in the backoffice, never a till.

Run:
    python manage.py test bravepos.tests.test_viewer_role \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from bravepos.models import Branch, Staff
from bravepos.tests.test_user_management_and_audit import make_user, sign_in


class ViewerBackofficeTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="Silom")
        self.viewer = make_user(username="accounts", email="acc@example.com", role="viewer")
        sign_in(self.client, self.viewer)

    def test_reports_open(self):
        for name in ("dashboard", "transactions", "report_daily", "report_sku",
                     "report_sell", "report_tax", "inventory", "stock_in", "stock_out"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(f"backoffice:{name}")).status_code, 200)

    def test_exports_open(self):
        response = self.client.get(reverse("backoffice:report_daily_export"))
        self.assertEqual(response.status_code, 200)

    def test_manage_pages_refused(self):
        for name in ("product_list", "product_new", "discount_list", "branch_list",
                     "staff_list", "customer_list", "app_release_list",
                     "shop_settings", "user_list", "audit_log"):
            with self.subTest(name=name):
                response = self.client.get(reverse(f"backoffice:{name}"))
                self.assertEqual(response.status_code, 403)
                self.assertContains(response, "view reports only", status_code=403)

    def test_writes_refused_even_on_a_report_url(self):
        self.assertEqual(self.client.post(reverse("backoffice:inventory")).status_code, 403)

    def test_rail_hides_manage_section(self):
        response = self.client.get(reverse("backoffice:dashboard"))
        self.assertNotContains(response, reverse("backoffice:product_list"))
        self.assertNotContains(response, reverse("backoffice:shop_settings"))
        self.assertContains(response, "Viewer")

    def test_can_sign_out(self):
        response = self.client.post(reverse("backoffice:logout"))
        self.assertEqual(response.status_code, 302)


class ManagerUnaffectedTests(TestCase):
    def test_manager_still_reaches_manage_pages(self):
        manager = make_user(username="mgr", email="mgr@example.com", role="cashier")
        sign_in(self.client, manager)
        self.assertEqual(self.client.get(reverse("backoffice:product_list")).status_code, 200)
        self.assertContains(self.client.get(reverse("backoffice:dashboard")), "Manager")


class UsersPageRoleTests(TestCase):
    def setUp(self):
        sign_in(self.client, make_user())

    def test_create_viewer(self):
        self.client.post(reverse("backoffice:user_new"), {
            "name": "Accounts", "username": "accounts", "role": "viewer",
            "active": "on", "password": "long-enough-pass",
        })
        self.assertEqual(Staff.objects.get(username="accounts").role, "viewer")

    def test_unknown_role_falls_back_to_manager(self):
        self.client.post(reverse("backoffice:user_new"), {
            "name": "X", "username": "x", "role": "superuser",
            "active": "on", "password": "long-enough-pass",
        })
        self.assertEqual(Staff.objects.get(username="x").role, "cashier")

    def test_list_labels(self):
        make_user(username="mgr", email="mgr@example.com", role="cashier")
        make_user(username="vw", email="vw@example.com", role="viewer")
        response = self.client.get(reverse("backoffice:user_list"))
        self.assertContains(response, ">Manager<")
        self.assertContains(response, ">Viewer<")
        self.assertNotContains(response, ">Cashier<")


class ViewerNeverOpensATillTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="Silom")
        self.viewer = make_user(username="vw", email="vw@example.com", role="viewer")
        self.viewer.set_pin("4321")
        self.viewer.save()
        self.viewer.branches.add(self.branch)
        self.api = APIClient()

    def test_pin_login_refused(self):
        response = self.api.post("/api/auth/pin-login", {
            "branch_id": str(self.branch.id), "staff_id": str(self.viewer.id), "pin": "4321",
        }, format="json")
        self.assertEqual(response.status_code, 403)

    def test_not_on_the_pin_picker(self):
        response = self.api.get("/api/auth/branch-users", {"branch_id": str(self.branch.id)})
        self.assertNotIn(str(self.viewer.id), [u["id"] for u in response.json()["users"]])
