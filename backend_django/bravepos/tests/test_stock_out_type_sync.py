"""Stock-out types: "Sync to all branches" on add and delete.

Run:
    python manage.py test bravepos.tests.test_stock_out_type_sync \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from bravepos.models import Branch, Settings, Staff, StockOutReason


class StockOutTypeSyncTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.password = "correct-horse-battery"
        admin = Staff(
            name="Admin", username="sotsync", email="sotsync@therollingpinn.com",
            role="admin", active=True, backoffice_access=True,
        )
        admin.set_password(cls.password)
        admin.save()
        Settings.objects.get_or_create(id="shop")
        Branch.objects.update(active=False)
        cls.westgate = Branch.objects.create(name="Central Westgate")
        cls.silom = Branch.objects.create(name="Silom")
        cls.closed = Branch.objects.create(name="Closed", active=False)

    def setUp(self):
        self.assertTrue(self.client.login(username="sotsync", password=self.password))
        self.url = reverse("backoffice:stock_out_types") + f"?branch={self.westgate.id}"

    def names(self, branch):
        return set(StockOutReason.objects.filter(branch=branch).values_list("name", flat=True))

    def test_the_box_is_ticked_on_the_page(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'name="sync_all" value="1" checked', html=False)

    def test_adding_with_the_box_reaches_every_active_branch(self):
        response = self.client.post(
            self.url, {"name": "Borrow", "name_th": "ยืม", "sync_all": "1"}, follow=True)
        self.assertIn("Borrow", self.names(self.silom))
        self.assertNotIn("Borrow", self.names(self.closed))
        self.assertEqual(
            StockOutReason.objects.get(branch=self.silom, name="Borrow").name_th, "ยืม")
        self.assertContains(response, "Also added to Silom")

    def test_a_branch_that_has_it_gets_no_second_copy(self):
        StockOutReason.objects.create(branch=self.silom, name="borrow")
        self.client.post(self.url, {"name": "Borrow", "sync_all": "1"})
        self.assertEqual(StockOutReason.objects.filter(
            branch=self.silom, name__iexact="borrow").count(), 1)

    def test_without_the_box_only_this_branch(self):
        self.client.post(self.url, {"name": "Borrow"})
        self.assertNotIn("Borrow", self.names(self.silom))

    def test_deleting_with_the_box_deletes_everywhere(self):
        here = StockOutReason.objects.create(branch=self.westgate, name="Lent out")
        StockOutReason.objects.create(branch=self.silom, name="Lent out")
        self.client.post(reverse("backoffice:stock_out_type_delete", args=[here.id]),
                         {"sync_all": "1"})
        self.assertNotIn("Lent out", self.names(self.silom))
        self.assertNotIn("Lent out", self.names(self.westgate))

    def test_deleting_without_the_box_leaves_the_others(self):
        here = StockOutReason.objects.create(branch=self.westgate, name="Lent out")
        StockOutReason.objects.create(branch=self.silom, name="Lent out")
        self.client.post(reverse("backoffice:stock_out_type_delete", args=[here.id]),
                         {"sync_all": ""})
        self.assertIn("Lent out", self.names(self.silom))
