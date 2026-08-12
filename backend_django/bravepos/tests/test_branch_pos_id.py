"""A POS ID belongs to exactly one branch.

It is the machine number the Revenue Department issues to a single till, and it
prints on that till's full tax invoices — so a branch pasting another branch's
number would be filing its sales under someone else's registration. The number
is optional (a branch that has not been registered yet simply has none, and
cannot issue a full tax invoice), which is why the uniqueness is conditional:
blank is exempt, everything else is not.

Run:
    python manage.py test bravepos.tests.test_branch_pos_id \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from bravepos.models import Branch, Settings, Staff
from bravepos.serializers import BranchSerializer


class BranchPosIdConstraintTests(TestCase):
    """The database is the backstop, under every route into it."""

    def test_two_branches_cannot_share_a_pos_id(self):
        Branch.objects.create(name="Pos ID Bio", pos_id="E020140003A1363")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Branch.objects.create(name="Pos ID EMQ", pos_id="E020140003A1363")

    def test_any_number_of_branches_may_have_none(self):
        """Blank is "not registered yet", not a value — it cannot clash."""
        Branch.objects.create(name="Pos ID Bio", pos_id="")
        Branch.objects.create(name="Pos ID EMQ", pos_id="")
        Branch.objects.create(name="Pos ID Siam", pos_id="")
        self.assertEqual(
            Branch.objects.filter(pos_id="", name__startswith="Pos ID").count(), 3)

    def test_a_branch_keeps_its_own_pos_id_on_save(self):
        b = Branch.objects.create(name="Pos ID Bio", pos_id="E020140003A1363")
        b.phone = "0644184887"
        b.save()  # its own value must not read as a clash with itself
        b.refresh_from_db()
        self.assertEqual(b.pos_id, "E020140003A1363")


class BranchPosIdApiTests(TestCase):
    """The POS API answers 400 with a name, not a 500 from the constraint."""

    def setUp(self):
        self.existing = Branch.objects.create(
            name="Pos ID Bio", pos_id="E020140003A1363")

    def test_duplicate_is_rejected_with_the_owning_branch_named(self):
        ser = BranchSerializer(data={"name": "Pos ID EMQ",
                                     "pos_id": "E020140003A1363"})
        self.assertFalse(ser.is_valid())
        self.assertIn("Pos ID Bio", str(ser.errors["pos_id"]))

    def test_a_branch_may_be_updated_without_changing_its_pos_id(self):
        ser = BranchSerializer(
            self.existing, data={"phone": "0644184887"}, partial=True)
        self.assertTrue(ser.is_valid(), ser.errors)

    def test_blank_is_always_accepted(self):
        ser = BranchSerializer(data={"name": "Pos ID EMQ", "pos_id": ""})
        self.assertTrue(ser.is_valid(), ser.errors)


class BranchPosIdBackofficeTests(TestCase):
    """The branch form refuses the save and says whose number it is."""

    @classmethod
    def setUpTestData(cls):
        cls.password = "correct-horse-battery"
        cls.admin = Staff(
            name="POS ID Admin", username="posid",
            email="posid@therollingpinn.com", role="admin",
            active=True, backoffice_access=True,
        )
        cls.admin.set_password(cls.password)
        cls.admin.save()
        Settings.objects.get_or_create(id="shop")
        cls.bio = Branch.objects.create(name="Pos ID Bio", pos_id="E020140003A1363")
        cls.emq = Branch.objects.create(name="Pos ID EMQ", code="EMQ", pos_id="")

    def setUp(self):
        self.assertTrue(self.client.login(username="posid", password=self.password))

    def _form(self, **overrides):
        data = {
            "name": "Pos ID EMQ", "code": "EMQ", "tax_id": "", "pos_id": "",
            "address": "", "phone": "", "logo_url": "",
            "open_time": "09:00", "close_time": "22:00",
            "peak_account_code": "BSV003", "active": "on",
            "payment_mode": "test", "beam_merchant_id": "", "beam_api_key": "",
            "beam_card_fee_percent": "3.65", "omise_public_key": "",
            "omise_secret_key": "", "omise_fee_percent": "3.65",
        }
        data.update(overrides)
        return data

    def test_pasting_another_branchs_pos_id_does_not_save(self):
        response = self.client.post(
            reverse("backoffice:branch_detail", args=[self.emq.id]),
            self._form(pos_id="E020140003A1363", phone="021234567"),
        )
        self.assertEqual(response.status_code, 200, "a clash must not redirect")
        self.assertContains(response, "already belongs to Pos ID Bio")
        self.emq.refresh_from_db()
        self.assertEqual(self.emq.pos_id, "")
        # The rest of the edit survives the rejection, so the fix is one field.
        self.assertContains(response, "021234567")

    def test_its_own_pos_id_still_saves(self):
        self.client.post(
            reverse("backoffice:branch_detail", args=[self.bio.id]),
            self._form(name="Pos ID Bio", code="", pos_id="E020140003A1363",
                       phone="0644184887"),
        )
        self.bio.refresh_from_db()
        self.assertEqual(self.bio.phone, "0644184887")
        self.assertEqual(self.bio.pos_id, "E020140003A1363")

    def test_a_free_pos_id_saves(self):
        self.client.post(
            reverse("backoffice:branch_detail", args=[self.emq.id]),
            self._form(pos_id="E020140003A9999"),
        )
        self.emq.refresh_from_db()
        self.assertEqual(self.emq.pos_id, "E020140003A9999")

    def test_a_new_branch_cannot_claim_a_taken_pos_id(self):
        response = self.client.post(
            reverse("backoffice:branch_new"),
            self._form(name="Siam Paragon", code="SPG",
                       pos_id="E020140003A1363"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already belongs to Pos ID Bio")
        self.assertFalse(Branch.objects.filter(name="Siam Paragon").exists())

    def test_the_form_ships_the_taken_numbers_for_the_live_warning(self):
        page = self.client.get(
            reverse("backoffice:branch_detail", args=[self.emq.id]))
        self.assertContains(page, "E020140003A1363")   # in data-taken
        self.assertContains(page, "data-taken")
