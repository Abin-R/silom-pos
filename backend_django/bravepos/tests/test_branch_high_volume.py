"""The branch form's "High volume branch" box drives self_order_enabled.

That one flag gates the customer menu, the QR and the till's print poller, so
this box is what opens and closes self-order at a branch.

Run:
    python manage.py test bravepos.tests.test_branch_high_volume \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from bravepos.models import Branch, Settings, Staff


class HighVolumeToggleTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.password = "correct-horse-battery"
        cls.admin = Staff(
            name="High Volume Admin", username="highvolume",
            email="highvolume@therollingpinn.com", role="admin",
            active=True, backoffice_access=True,
        )
        cls.admin.set_password(cls.password)
        cls.admin.save()
        Settings.objects.get_or_create(id="shop")
        cls.silom = Branch.objects.create(name="Silom", code="SLM")

    def setUp(self):
        self.assertTrue(
            self.client.login(username="highvolume", password=self.password))
        self.url = reverse("backoffice:branch_detail", args=[self.silom.id])

    def _form(self, **overrides):
        data = {
            "name": "Silom", "code": "SLM", "tax_id": "", "pos_id": "",
            "address": "", "phone": "", "logo_url": "",
            "open_time": "09:00", "close_time": "22:00",
            "peak_account_code": "BSV003", "active": "on",
            "payment_mode": "test", "beam_merchant_id": "", "beam_api_key": "",
            "beam_card_fee_percent": "3.65", "omise_public_key": "",
            "omise_secret_key": "", "omise_fee_percent": "3.65",
        }
        data.update(overrides)
        return data

    def _flag(self):
        return Branch.objects.get(pk=self.silom.pk).self_order_enabled

    def test_ticking_turns_self_order_on(self):
        response = self.client.post(self.url, self._form(self_order="on"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self._flag())

    def test_unticking_turns_self_order_off(self):
        Branch.objects.filter(pk=self.silom.pk).update(self_order_enabled=True)
        response = self.client.post(self.url, self._form())
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self._flag())

    def _minimum(self):
        return Branch.objects.get(pk=self.silom.pk).self_order_min_stock

    def test_the_minimum_stock_is_saved(self):
        self.client.post(self.url, self._form(self_order="on", self_order_min_stock="3"))
        self.assertEqual(self._minimum(), 3)

    def test_the_minimum_survives_unticking(self):
        self.client.post(self.url, self._form(self_order_min_stock="3"))
        self.assertEqual(self._minimum(), 3)

    def test_a_blank_or_negative_minimum_is_zero(self):
        for value in ("", "-2"):
            with self.subTest(value=value):
                Branch.objects.filter(pk=self.silom.pk).update(self_order_min_stock=3)
                self.client.post(self.url, self._form(self_order_min_stock=value))
                self.assertEqual(self._minimum(), 0)

    def test_a_non_numeric_minimum_keeps_the_old_value(self):
        Branch.objects.filter(pk=self.silom.pk).update(self_order_min_stock=3)
        self.client.post(self.url, self._form(self_order_min_stock="abc"))
        self.assertEqual(self._minimum(), 3)

    def test_the_form_shows_the_saved_state(self):
        Branch.objects.filter(pk=self.silom.pk).update(self_order_enabled=True)
        self.assertContains(
            self.client.get(self.url),
            '<input type="checkbox" name="self_order" checked>', html=True)

    def test_a_refused_save_keeps_the_tick_and_changes_nothing(self):
        Branch.objects.create(name="Khlong San")
        response = self.client.post(
            self.url, self._form(name="Khlong San", self_order="on"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, '<input type="checkbox" name="self_order" checked>', html=True)
        self.assertFalse(self._flag())

    def test_a_new_branch_starts_off(self):
        response = self.client.post(
            reverse("backoffice:branch_new"),
            self._form(name="Brand New", code="NEW"))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Branch.objects.get(name="Brand New").self_order_enabled)
