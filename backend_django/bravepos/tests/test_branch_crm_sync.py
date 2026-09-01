"""A POS branch says which CRM shop it is, chosen on the branch form.

The CRM (crm.rollingpinn.com) keeps its own branch list for the customer-facing
loyalty app.  The branch form offers that list as a dropdown plus a "none of
the above" entry that creates the CRM branch on the spot, and stores the id it
gets back in ``Branch.crm_branch_id``.

The rules worth pinning down are the ones about *not* writing:

* a CRM branch is created only once the rest of the form has passed, so a save
  refused for a duplicate name doesn't leave a stray row over there — and a
  retry doesn't leave a second one;
* a CRM that is down or unconfigured must leave the branch form working, and
  must never quietly unlink a branch that was linked.

The CRM client itself is mocked throughout — these tests make no network calls.

Run:
    python manage.py test bravepos.tests.test_branch_crm_sync \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from unittest import mock

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from bravepos.crm import CrmError
from bravepos.models import Branch, Settings, Staff


CRM_ROWS = [
    {"id": 1, "name": "The Rolling Pinn - Siam Paragon (G Floor)", "active": True,
     "display_order": 1, "neighborhood": "Pathum Wan", "hours_display": "10:00-22:00"},
    {"id": 4, "name": "The Rolling Pinn - Khlong San", "active": True,
     "display_order": 2, "neighborhood": "Khlong San", "hours_display": "08:00-20:00"},
    {"id": 9, "name": "The Rolling Pinn - Silom", "active": False,
     "display_order": 3, "neighborhood": "Bang Rak", "hours_display": "09:00-21:00"},
]


class CrmBranchIdConstraintTests(TestCase):
    """The database is the backstop: one CRM shop, one POS branch."""

    def test_two_branches_cannot_claim_one_crm_branch(self):
        Branch.objects.create(name="Paragon", crm_branch_id=1)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Branch.objects.create(name="Paragon 2", crm_branch_id=1)

    def test_any_number_of_branches_can_be_unlinked(self):
        Branch.objects.create(name="Paragon")
        Branch.objects.create(name="Khlong San")
        self.assertEqual(
            Branch.objects.filter(
                name__in=["Paragon", "Khlong San"], crm_branch_id=None).count(), 2)


class BranchFormCrmTests(TestCase):
    """The dropdown, and what a save does with it."""

    @classmethod
    def setUpTestData(cls):
        cls.password = "correct-horse-battery"
        cls.admin = Staff(
            name="CRM Admin", username="crmadmin",
            email="crmadmin@therollingpinn.com", role="admin",
            active=True, backoffice_access=True,
        )
        cls.admin.set_password(cls.password)
        cls.admin.save()
        Settings.objects.get_or_create(id="shop")
        cls.existing = Branch.objects.create(name="Khlong San")

    def setUp(self):
        self.assertTrue(
            self.client.login(username="crmadmin", password=self.password))
        # Configured and reachable unless a test says otherwise.
        configured = mock.patch("bravepos.crm.is_configured", return_value=True)
        listing = mock.patch("bravepos.crm.list_branches", return_value=CRM_ROWS)
        self.addCleanup(configured.stop)
        self.addCleanup(listing.stop)
        configured.start()
        self.listing = listing.start()

    def _form(self, **overrides):
        data = {
            "name": "Siam Paragon", "code": "PGN", "tax_id": "", "pos_id": "",
            "address": "", "phone": "", "logo_url": "",
            "open_time": "10:00", "close_time": "22:00",
            "peak_account_code": "BSV003", "active": "on",
            "payment_mode": "test", "beam_merchant_id": "", "beam_api_key": "",
            "beam_card_fee_percent": "3.65", "omise_public_key": "",
            "omise_secret_key": "", "omise_fee_percent": "3.65",
            "crm_branch": "",
        }
        data.update(overrides)
        return data

    # ── The dropdown ────────────────────────────────────────────────────
    def test_the_add_form_lists_every_crm_branch_and_the_create_entry(self):
        response = self.client.get(reverse("backoffice:branch_new"))
        self.assertEqual(response.status_code, 200)
        for row in CRM_ROWS:
            self.assertContains(response, row["name"])
        self.assertContains(response, "None of the above")
        self.assertContains(response, 'value="__create__"')

    def test_a_crm_branch_another_branch_holds_cannot_be_picked(self):
        self.existing.crm_branch_id = 4
        self.existing.save()
        response = self.client.get(reverse("backoffice:branch_new"))
        self.assertContains(response, "already linked to Khlong San")
        self.assertRegex(response.content.decode(),
                         r'<option value="4"[^>]*\bdisabled')

    def test_a_branch_edits_with_its_own_link_selected(self):
        self.existing.crm_branch_id = 4
        self.existing.save()
        response = self.client.get(
            reverse("backoffice:branch_detail", args=[self.existing.id]))
        self.assertRegex(response.content.decode(),
                         r'<option value="4"[^>]*\bselected')

    # ── Picking one that already exists ─────────────────────────────────
    def test_picking_an_existing_crm_branch_stores_its_id(self):
        with mock.patch("bravepos.crm.create_branch") as create:
            response = self.client.post(
                reverse("backoffice:branch_new"), self._form(crm_branch="1"))
        self.assertEqual(response.status_code, 302)
        create.assert_not_called()  # nothing is created for a branch that exists
        self.assertEqual(Branch.objects.get(name="Siam Paragon").crm_branch_id, 1)

    def test_picking_one_another_branch_holds_is_refused(self):
        self.existing.crm_branch_id = 1
        self.existing.save()
        response = self.client.post(
            reverse("backoffice:branch_new"), self._form(crm_branch="1"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already linked to")
        self.assertFalse(Branch.objects.filter(name="Siam Paragon").exists())

    def test_not_linked_saves_a_branch_with_no_crm_id(self):
        response = self.client.post(
            reverse("backoffice:branch_new"), self._form(crm_branch=""))
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(Branch.objects.get(name="Siam Paragon").crm_branch_id)

    def test_a_link_can_be_removed_on_edit(self):
        self.existing.crm_branch_id = 4
        self.existing.save()
        self.client.post(
            reverse("backoffice:branch_detail", args=[self.existing.id]),
            self._form(name="Khlong San", crm_branch=""))
        self.existing.refresh_from_db()
        self.assertIsNone(self.existing.crm_branch_id)

    # ── Creating one in the CRM ─────────────────────────────────────────
    def test_none_of_the_above_creates_the_crm_branch_and_stores_its_id(self):
        with mock.patch("bravepos.crm.create_branch", return_value=17) as create:
            response = self.client.post(
                reverse("backoffice:branch_new"),
                self._form(crm_branch="__create__", crm_neighborhood="Pathum Wan"))
        self.assertEqual(response.status_code, 302)
        create.assert_called_once_with(
            "Siam Paragon", neighborhood="Pathum Wan",
            hours_display="10:00-22:00", active=True)
        self.assertEqual(Branch.objects.get(name="Siam Paragon").crm_branch_id, 17)

    def test_a_refused_branch_never_reaches_the_crm(self):
        """The stray-row case: the save bounces off the duplicate name first."""
        with mock.patch("bravepos.crm.create_branch") as create:
            response = self.client.post(
                reverse("backoffice:branch_new"),
                self._form(name="Khlong San", crm_branch="__create__"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")
        create.assert_not_called()

    def test_a_crm_that_refuses_the_create_saves_nothing_here_either(self):
        with mock.patch("bravepos.crm.create_branch",
                        side_effect=CrmError("Couldn't reach the CRM (ConnectError).")):
            response = self.client.post(
                reverse("backoffice:branch_new"), self._form(crm_branch="__create__"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "couldn&#x27;t be created")
        self.assertFalse(Branch.objects.filter(name="Siam Paragon").exists())
        # The rest of the form is handed back filled in, choice included.
        self.assertRegex(response.content.decode(),
                         r'<option value="__create__"[^>]*\bselected')

    # ── Degrading ───────────────────────────────────────────────────────
    def test_no_crm_key_means_no_panel_and_a_form_that_still_works(self):
        with mock.patch("bravepos.crm.is_configured", return_value=False):
            response = self.client.get(reverse("backoffice:branch_new"))
            self.assertNotContains(response, "None of the above")
            saved = self.client.post(
                reverse("backoffice:branch_new"), self._form(crm_branch="1"))
        self.assertEqual(saved.status_code, 302)
        # The dropdown was never offered, so a `crm_branch` in the POST is not
        # a choice anyone made here — it is ignored rather than obeyed.
        self.assertIsNone(Branch.objects.get(name="Siam Paragon").crm_branch_id)

    def test_a_crm_that_is_down_warns_but_leaves_the_branch_editable(self):
        self.listing.side_effect = CrmError("Couldn't reach the CRM (ConnectError).")
        response = self.client.get(reverse("backoffice:branch_new"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "didn't load")
        self.assertNotContains(response, "None of the above")

    def test_a_crm_that_is_down_does_not_unlink_a_linked_branch(self):
        """No dropdown was rendered, so the save says nothing about the link."""
        self.existing.crm_branch_id = 4
        self.existing.save()
        self.listing.side_effect = CrmError("Couldn't reach the CRM (ConnectError).")
        form = self._form(name="Khlong San", phone="021234567")
        form.pop("crm_branch")  # exactly what the page posts with no dropdown
        response = self.client.post(
            reverse("backoffice:branch_detail", args=[self.existing.id]), form)
        self.assertEqual(response.status_code, 302)
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.crm_branch_id, 4)
        self.assertEqual(self.existing.phone, "021234567")
