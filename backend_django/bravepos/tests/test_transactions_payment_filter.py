"""Transactions' payment filter offers, and matches, every payment type.

It used to offer Cash, PromptPay and Card only, and to search for those words
inside the stored method — so PromptPay found no "Beam QR" bill and Custom Pay
could not be picked at all.

Run:
    python manage.py test bravepos.tests.test_transactions_payment_filter \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from backoffice.templatetags.backoffice_ui import payment_label
from bravepos.models import Branch, Order, PaymentType, Settings, Staff

METHODS = {
    "A-1": "Cash", "A-2": "Beam QR", "A-3": "Beam Card",
    "A-4": "Custom · EDC", "A-5": "Custom · EDC Bangkok", "A-6": "Custom",
    "A-7": "Beam LINE Pay", "A-8": "Custom · QR", "A-9": "Voucher",
}


class PaymentFilterTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        admin = Staff(name="Pay Admin", username="payadmin",
                      email="payadmin@therollingpinn.com", role="admin",
                      active=True, backoffice_access=True)
        admin.set_password("correct-horse-battery")
        admin.save()
        Settings.objects.get_or_create(id="shop")
        Branch.objects.update(active=False)  # the seeded EmQuartier
        cls.silom = Branch.objects.create(name="Silom", code="SLM")
        cls.other = Branch.objects.create(name="Habito", code="HAB")
        PaymentType.objects.filter(branch__in=[cls.silom, cls.other]).delete()
        for name in ("EDC", "คนละครึ่ง"):
            PaymentType.objects.create(branch=cls.silom, name=name)
        PaymentType.objects.create(branch=cls.silom, name="Hidden type", active=False)
        PaymentType.objects.create(branch=cls.other, name="Grab")
        for number, method in METHODS.items():
            Order.objects.create(order_number=number, branch=cls.silom,
                                 payment_method=method, total=10, subtotal=10)

    def setUp(self):
        self.assertTrue(self.client.login(
            username="payadmin", password="correct-horse-battery"))

    def page(self, **params):
        response = self.client.get(reverse("backoffice:transactions"),
                                   {"branch": str(self.silom.id), **params})
        self.assertEqual(response.status_code, 200)
        return response

    def found(self, payment):
        return {r["order"].order_number for r in self.page(payment=payment).context["rows"]}

    def offered(self, **params):
        return {value: label for _, options in self.page(**params).context["more_payments"]
                for value, label in options}

    def test_buttons_match_what_is_stored(self):
        self.assertEqual(self.found("Cash"), {"A-1"})
        self.assertEqual(self.found("PromptPay"), {"A-2"})
        self.assertEqual(self.found("Card"), {"A-3"})
        self.assertEqual(self.found("Custom Pay"), {"A-4", "A-5", "A-6", "A-8"})

    def test_one_type_does_not_match_a_longer_name(self):
        self.assertEqual(self.found("Custom · EDC"), {"A-4"})
        self.assertEqual(self.found("Custom"), {"A-6"})
        self.assertEqual(self.found("Beam LINE Pay"), {"A-7"})

    def test_every_type_is_offered(self):
        offered = self.offered()
        for value in ("Custom · EDC", "Custom · คนละครึ่ง",   # this branch's list
                      "Custom · EDC Bangkok", "Custom · QR",  # only on its bills
                      "Custom", "Beam LINE Pay", "Beam KPlus", "Voucher"):
            self.assertIn(value, offered)
        self.assertEqual(offered["Custom · EDC"], "EDC")

    def test_other_branches_and_hidden_types_are_not(self):
        offered = self.offered()
        self.assertNotIn("Custom · Grab", offered)
        self.assertNotIn("Custom · Hidden type", offered)

    def test_picking_one_keeps_the_rest_and_shows_it_picked(self):
        self.assertIn("Custom · EDC Bangkok", self.offered(payment="Cash"))
        page = self.page(payment="Custom · EDC")
        self.assertContains(page, "selected>EDC</option>")

    def test_a_pick_with_no_bills_in_these_dates_stays_listed(self):
        self.assertIn("Custom · Old type", self.offered(payment="Custom · Old type"))

    def test_export_follows_the_filter(self):
        body = self.client.get(reverse("backoffice:transactions_export"), {
            "branch": str(self.silom.id), "payment": "Custom · EDC"}).content.decode()
        self.assertIn("A-4", body)
        self.assertNotIn("A-5", body)

    def test_labels_say_the_type(self):
        self.assertEqual(payment_label("Custom · QR"), "Custom Pay · QR")
        self.assertEqual(payment_label("Custom"), "Custom Pay")
        self.assertEqual(payment_label("Beam QR"), "PromptPay")
        self.assertEqual(payment_label("Beam Card"), "Card")
