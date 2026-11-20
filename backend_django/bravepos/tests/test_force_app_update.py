"""The off switch for forced till updates (frontend lib/storeUpdate.ts).

Tills read it from the public /api/app-config before anyone signs in; only a
backoffice admin can change it.
"""
import json

from django.test import TestCase
from django.urls import reverse

from bravepos.models import Branch, BranchSession, Settings, Staff
from bravepos.tests.test_user_management_and_audit import make_user, sign_in


class AppConfigEndpointTests(TestCase):
    def test_on_by_default_without_a_settings_row(self):
        Settings.objects.all().delete()
        response = self.client.get("/api/app-config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"force_app_update": True})

    def test_needs_no_session_and_reports_the_switch(self):
        Settings.objects.update_or_create(id="shop", defaults={"force_app_update": False})
        response = self.client.get("/api/app-config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"force_app_update": False})


class TillCannotChangeSwitchTests(TestCase):
    def test_settings_put_from_a_till_ignores_the_switch(self):
        Settings.objects.update_or_create(id="shop", defaults={"force_app_update": True})
        branch = Branch.objects.create(name="Switch Branch")
        staff = Staff(name="Till Admin", email="till-admin@example.com", role="admin")
        staff.set_password("pw")
        staff.save()
        staff.branches.add(branch)
        session = BranchSession.objects.create(
            branch=branch, staff=staff, token=BranchSession.new_token(),
        )
        response = self.client.put(
            "/api/settings",
            data=json.dumps({"shop_name": "From the till", "force_app_update": False}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {session.token}",
        )
        self.assertEqual(response.status_code, 200)
        row = Settings.objects.get(id="shop")
        self.assertEqual(row.shop_name, "From the till")
        self.assertTrue(row.force_app_update)


class BackofficeSwitchTests(TestCase):
    def setUp(self):
        Settings.objects.update_or_create(id="shop", defaults={"force_app_update": True})

    def post(self, **extra):
        return self.client.post(reverse("backoffice:shop_settings"), {"shop_name": "Shop", **extra})

    def test_admin_unticks_and_reticks(self):
        sign_in(self.client, make_user(username="upd-admin", email="upd-admin@example.com"))
        page = self.client.get(reverse("backoffice:shop_settings"))
        self.assertContains(page, 'name="force_app_update"')

        self.assertEqual(self.post(force_app_update_shown="1").status_code, 302)
        self.assertFalse(Settings.objects.get(id="shop").force_app_update)

        self.post(force_app_update_shown="1", force_app_update="on")
        self.assertTrue(Settings.objects.get(id="shop").force_app_update)

    def test_form_without_the_field_leaves_it_alone(self):
        sign_in(self.client, make_user(username="upd-admin2", email="upd-admin2@example.com"))
        self.post()
        self.assertTrue(Settings.objects.get(id="shop").force_app_update)

    def test_cashier_cannot_switch_it_off(self):
        sign_in(self.client, make_user(
            username="upd-cashier", email="upd-cashier@example.com", role="cashier"))
        page = self.client.get(reverse("backoffice:shop_settings"))
        self.assertNotContains(page, 'name="force_app_update"')
        self.post(force_app_update_shown="1")
        self.assertTrue(Settings.objects.get(id="shop").force_app_update)
