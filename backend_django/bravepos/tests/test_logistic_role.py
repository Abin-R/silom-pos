"""The "logistic" role: the Ninja delivery windows page in the backoffice, never a till.

Run:
    python manage.py test bravepos.tests.test_logistic_role \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from unittest import mock

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from bravepos.models import Branch, Staff
from bravepos.tests.test_ninja_windows_page import API_ENV, LOCATIONS, _resp
from bravepos.tests.test_user_management_and_audit import make_user, sign_in


@mock.patch.dict("os.environ", API_ENV)
@mock.patch("backoffice.views.httpx.get", return_value=_resp(200, LOCATIONS))
class LogisticBackofficeTests(TestCase):
    def setUp(self):
        self.logistic = make_user(username="logistics", email="logistics@example.com", role="logistic")
        sign_in(self.client, self.logistic)

    def test_ninja_windows_opens(self, _get):
        response = self.client.get(reverse("backoffice:ninja_windows"))
        self.assertContains(response, "Silom")
        self.assertContains(response, "Logistic")

    @mock.patch("backoffice.views.httpx.post", return_value=_resp(201, {"location": {}}))
    def test_can_save_a_window(self, post, _get):
        response = self.client.post(reverse("backoffice:ninja_windows"), {
            "location_2": "Ari", "start_2": "09:00", "end_2": "10:00"})
        self.assertEqual(response.status_code, 302)
        post.assert_called_once()

    def test_home_and_dashboard_land_on_ninja_windows(self, _get):
        for name in ("home", "dashboard"):
            with self.subTest(name=name):
                self.assertRedirects(self.client.get(reverse(f"backoffice:{name}")),
                                     reverse("backoffice:ninja_windows"))

    def test_everything_else_refused(self, _get):
        for name in ("transactions", "report_daily", "inventory", "stock_in", "stock_out",
                     "check_stock", "product_list", "discount_list", "branch_list", "staff_list",
                     "customer_list", "shop_settings", "user_list", "audit_log",
                     "report_daily_export"):
            with self.subTest(name=name):
                response = self.client.get(reverse(f"backoffice:{name}"))
                self.assertContains(response, "Ninja windows only", status_code=403)

    def test_writes_elsewhere_refused(self, _get):
        self.assertEqual(self.client.post(reverse("backoffice:product_new")).status_code, 403)

    def test_rail_shows_only_ninja_windows(self, _get):
        response = self.client.get(reverse("backoffice:ninja_windows"))
        for name in ("dashboard", "transactions", "report_daily", "check_stock",
                     "product_list", "shop_settings"):
            self.assertNotContains(response, reverse(f"backoffice:{name}") + '"')

    def test_can_sign_out(self, _get):
        self.assertEqual(self.client.post(reverse("backoffice:logout")).status_code, 302)


class UsersPageLogisticTests(TestCase):
    def test_create_logistic(self):
        sign_in(self.client, make_user())
        self.client.post(reverse("backoffice:user_new"), {
            "name": "Logistics Team", "username": "logistics", "role": "logistic",
            "active": "on", "password": "long-enough-pass",
        })
        self.assertEqual(Staff.objects.get(username="logistics").role, "logistic")


class LogisticNeverOpensATillTests(TestCase):
    def test_pin_login_refused_and_not_on_picker(self):
        branch = Branch.objects.create(name="Silom")
        logistic = make_user(username="lg", email="lg@example.com", role="logistic")
        logistic.set_pin("4321")
        logistic.save()
        logistic.branches.add(branch)
        api = APIClient()
        response = api.post("/api/auth/pin-login", {
            "branch_id": str(branch.id), "staff_id": str(logistic.id), "pin": "4321",
        }, format="json")
        self.assertEqual(response.status_code, 403)
        users = api.get("/api/auth/branch-users", {"branch_id": str(branch.id)}).json()["users"]
        self.assertNotIn(str(logistic.id), [u["id"] for u in users])
