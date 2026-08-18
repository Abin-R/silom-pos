"""A branch name belongs to exactly one branch.

It is the only handle a branch has: staff pick a till by it on the POS login
screen, reports filter by it, and it prints at the top of the receipt.  The
column is unique, so a repeat used to be a 500 — on 2026-08-18 production
saved "Khlong San", the form was submitted again, and the second save hit
``bravepos_branch_name_key`` and handed the admin an error page instead of
saying the branch already existed.

Run:
    python manage.py test bravepos.tests.test_branch_name \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from bravepos.models import Branch, Settings, Staff


class BranchNameConstraintTests(TestCase):
    """The database is the backstop."""

    def test_two_branches_cannot_share_a_name(self):
        Branch.objects.create(name="Khlong San")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Branch.objects.create(name="Khlong San")

    def test_a_branch_keeps_its_own_name_on_save(self):
        b = Branch.objects.create(name="Khlong San")
        b.phone = "0644184887"
        b.save()  # its own name must not read as a clash with itself
        b.refresh_from_db()
        self.assertEqual(b.name, "Khlong San")


class BranchNameBackofficeTests(TestCase):
    """The branch form refuses the save and says the branch already exists."""

    @classmethod
    def setUpTestData(cls):
        cls.password = "correct-horse-battery"
        cls.admin = Staff(
            name="Branch Name Admin", username="branchname",
            email="branchname@therollingpinn.com", role="admin",
            active=True, backoffice_access=True,
        )
        cls.admin.set_password(cls.password)
        cls.admin.save()
        Settings.objects.get_or_create(id="shop")
        cls.khlong_san = Branch.objects.create(name="Khlong San")
        cls.silom = Branch.objects.create(name="Silom", code="SLM")

    def setUp(self):
        self.assertTrue(
            self.client.login(username="branchname", password=self.password))

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

    def test_the_production_500_is_now_a_form_error(self):
        """The exact resubmit that broke: "Khlong San" posted a second time."""
        response = self.client.post(
            reverse("backoffice:branch_new"),
            self._form(name="Khlong San", code=""),
        )
        self.assertEqual(response.status_code, 200, "a clash must not redirect")
        self.assertContains(response, "already exists")
        self.assertEqual(Branch.objects.filter(name="Khlong San").count(), 1)

    def test_a_name_differing_only_in_case_is_refused_too(self):
        """Postgres would take it; two tills nobody can tell apart is worse."""
        response = self.client.post(
            reverse("backoffice:branch_new"),
            self._form(name="khlong san", code=""),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")
        self.assertFalse(Branch.objects.filter(name="khlong san").exists())

    def test_surrounding_whitespace_is_not_a_new_branch(self):
        response = self.client.post(
            reverse("backoffice:branch_new"),
            self._form(name="  Khlong San  ", code=""),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Branch.objects.filter(name__iexact="khlong san").count(), 1)

    def test_a_free_name_saves(self):
        response = self.client.post(
            reverse("backoffice:branch_new"),
            self._form(name="Vibhavadi", code="VBV"),
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Branch.objects.filter(name="Vibhavadi").exists())

    def test_renaming_onto_a_taken_name_does_not_save(self):
        response = self.client.post(
            reverse("backoffice:branch_detail", args=[self.silom.id]),
            self._form(name="Khlong San", phone="021234567"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")
        self.silom.refresh_from_db()
        self.assertEqual(self.silom.name, "Silom")
        # The rest of the edit survives the rejection, so the fix is one field.
        self.assertContains(response, "021234567")

    def test_a_branch_can_be_edited_without_renaming_it(self):
        self.client.post(
            reverse("backoffice:branch_detail", args=[self.silom.id]),
            self._form(phone="0644184887"),
        )
        self.silom.refresh_from_db()
        self.assertEqual(self.silom.phone, "0644184887")
        self.assertEqual(self.silom.name, "Silom")

    def test_a_nameless_branch_is_refused_rather_than_saved(self):
        """The input is `required`; a hand-made POST is not, and a blank name
        would save once and then clash on the next one."""
        response = self.client.post(
            reverse("backoffice:branch_new"), self._form(name="", code=""))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Branch name is required")
        self.assertFalse(Branch.objects.filter(name="").exists())

    def test_the_form_ships_the_taken_names_for_the_live_warning(self):
        page = self.client.get(
            reverse("backoffice:branch_detail", args=[self.silom.id]))
        self.assertContains(page, "khlong san")   # folded key, in data-taken
        self.assertNotContains(page, "&quot;silom&quot;")  # not its own name
