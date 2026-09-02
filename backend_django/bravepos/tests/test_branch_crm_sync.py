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

import os
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
    """The CRM panel on the branch form, and what a save does with it."""

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
        # Configured, reachable, and every branch in the rollout unless a test
        # says otherwise.  `CRM_BRANCHES="*"` is the real allowlist rather than
        # a stub of it, so these tests still run through `branch_allowed`;
        # `CrmRolloutAllowlistTests` is what pins the narrowing down.
        allowed = mock.patch.dict(os.environ, {"CRM_BRANCHES": "*"})
        configured = mock.patch("bravepos.crm.is_configured", return_value=True)
        listing = mock.patch("bravepos.crm.list_branches", return_value=CRM_ROWS)
        self.addCleanup(allowed.stop)
        self.addCleanup(configured.stop)
        self.addCleanup(listing.stop)
        allowed.start()
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
            # The real panel always carries both: `crm_panel` is the marker
            # that says it was rendered, and `crm_loyalty` is absent unless the
            # box is ticked (an unticked checkbox submits nothing).
            "crm_panel": "1", "crm_branch": "",
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

    # ── The loyalty switch ──────────────────────────────────────────────
    # The per-branch rollout control *and* kill switch: the till's loyalty
    # calls run inside a checkout, so this is what gets a slow CRM out of a
    # cashier's way without a deploy.

    def test_the_switch_is_off_on_a_new_branch(self):
        response = self.client.post(
            reverse("backoffice:branch_new"), self._form(crm_branch="1"))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Branch.objects.get(name="Siam Paragon").crm_loyalty_enabled)

    def test_ticking_it_turns_loyalty_on_for_that_branch(self):
        response = self.client.post(
            reverse("backoffice:branch_new"),
            self._form(crm_branch="1", crm_loyalty="on"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Branch.objects.get(name="Siam Paragon").crm_loyalty_enabled)

    def test_unticking_it_turns_loyalty_back_off(self):
        self.existing.crm_branch_id = 4
        self.existing.crm_loyalty_enabled = True
        self.existing.save()
        # An unticked checkbox submits nothing at all — only `crm_panel` says
        # the form had one to untick.
        response = self.client.post(
            reverse("backoffice:branch_detail", args=[self.existing.id]),
            self._form(name="Khlong San", crm_branch="4"))
        self.assertEqual(response.status_code, 302)
        self.existing.refresh_from_db()
        self.assertFalse(self.existing.crm_loyalty_enabled)

    def test_a_post_without_the_panel_leaves_the_switch_alone(self):
        """A form that never rendered the CRM panel says nothing about it, and
        must not read its own silence as "switch loyalty off"."""
        self.existing.crm_branch_id = 4
        self.existing.crm_loyalty_enabled = True
        self.existing.save()
        form = self._form(name="Khlong San")
        del form["crm_panel"]
        del form["crm_branch"]
        response = self.client.post(
            reverse("backoffice:branch_detail", args=[self.existing.id]), form)
        self.assertEqual(response.status_code, 302)
        self.existing.refresh_from_db()
        self.assertTrue(self.existing.crm_loyalty_enabled)
        self.assertEqual(self.existing.crm_branch_id, 4)

    def test_the_switch_survives_a_bounced_save(self):
        """A form refused for something else comes back with the box as the
        admin left it, not as the database still has it."""
        self.existing.crm_branch_id = 1
        self.existing.save()
        response = self.client.post(
            reverse("backoffice:branch_new"),
            self._form(crm_branch="1", crm_loyalty="on"))
        self.assertEqual(response.status_code, 200)  # refused: id already held
        self.assertRegex(response.content.decode(),
                         r'name="crm_loyalty"[^>]*\bchecked')

    def test_the_switch_still_renders_when_the_branch_list_fails_to_load(self):
        """It is the panel's one control that has to survive a CRM outage —
        turning loyalty off is exactly what an outage calls for."""
        self.listing.side_effect = CrmError("Couldn't reach the CRM (ConnectError).")
        response = self.client.get(reverse("backoffice:branch_new"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="crm_loyalty"')


class CrmRolloutAllowlistTests(TestCase):
    """``CRM_BRANCHES`` decides which branches may touch the CRM at all.

    The rollout control for the testing period: exactly one branch is meant to
    reach crm.rollingpinn.com, and this is what makes that true for every other
    one — no panel on the page, and no link or loyalty switch applied even to a
    POST that names them anyway.

    It is an env var rather than a per-branch checkbox on purpose.  A checkbox
    can be ticked on the wrong branch by anyone who can reach the form; this
    cannot be widened without a deploy.
    """

    @classmethod
    def setUpTestData(cls):
        cls.password = "correct-horse-battery"
        cls.admin = Staff(
            name="Rollout Admin", username="rollout",
            email="rollout@therollingpinn.com", role="admin",
            active=True, backoffice_access=True,
        )
        cls.admin.set_password(cls.password)
        cls.admin.save()
        Settings.objects.get_or_create(id="shop")
        cls.test_branch = Branch.objects.create(name="test branch")
        cls.live_branch = Branch.objects.create(name="Silom")

    def setUp(self):
        self.assertTrue(
            self.client.login(username="rollout", password=self.password))
        configured = mock.patch("bravepos.crm.is_configured", return_value=True)
        listing = mock.patch("bravepos.crm.list_branches", return_value=CRM_ROWS)
        self.addCleanup(configured.stop)
        self.addCleanup(listing.stop)
        configured.start()
        listing.start()

    def _allow(self, value):
        patch = mock.patch.dict(os.environ, {"CRM_BRANCHES": value})
        self.addCleanup(patch.stop)
        patch.start()

    def _page(self, branch):
        return self.client.get(
            reverse("backoffice:branch_detail", args=[branch.id]))

    def _form(self, **overrides):
        data = {
            "name": "test branch", "code": "", "tax_id": "", "pos_id": "",
            "address": "", "phone": "", "logo_url": "",
            "open_time": "09:00", "close_time": "22:00",
            "peak_account_code": "BSV003", "active": "on",
            "payment_mode": "test", "beam_merchant_id": "", "beam_api_key": "",
            "beam_card_fee_percent": "3.65", "omise_public_key": "",
            "omise_secret_key": "", "omise_fee_percent": "3.65",
        }
        data.update(overrides)
        return data

    # ── Who sees the panel ──────────────────────────────────────────────
    def test_the_named_branch_gets_the_panel(self):
        self._allow("test branch")
        self.assertContains(self._page(self.test_branch), "None of the above")

    def test_every_other_branch_is_untouched(self):
        self._allow("test branch")
        page = self._page(self.live_branch)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "None of the above")
        self.assertNotContains(page, 'name="crm_branch"')
        # The rest of the branch form is exactly as it was.
        self.assertContains(page, "Offer this branch on the till's sign-in screen")

    def test_the_name_is_matched_case_insensitively(self):
        self._allow("TEST BRANCH")
        self.assertContains(self._page(self.test_branch), "None of the above")

    def test_a_branch_id_works_too_so_a_rename_cannot_break_it(self):
        self._allow(str(self.test_branch.id))
        self.assertContains(self._page(self.test_branch), "None of the above")

    def test_several_branches_can_be_listed(self):
        self._allow("test branch, Silom")
        self.assertContains(self._page(self.test_branch), "None of the above")
        self.assertContains(self._page(self.live_branch), "None of the above")

    def test_a_star_ends_the_testing_period(self):
        self._allow("*")
        self.assertContains(self._page(self.live_branch), "None of the above")

    def test_unset_means_no_branch_at_all(self):
        """Fail-closed: a key configured but no allowlist stays dark."""
        self._allow("")
        self.assertNotContains(self._page(self.test_branch), "None of the above")
        self.assertNotContains(self._page(self.live_branch), "None of the above")

    def test_adding_a_branch_offers_no_panel_during_the_testing_period(self):
        """A branch being created has no name yet, so it matches nothing."""
        self._allow("test branch")
        page = self.client.get(reverse("backoffice:branch_new"))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "None of the above")

    # ── And a hand-made POST cannot get around it ───────────────────────
    def test_an_excluded_branch_cannot_be_linked_by_a_crafted_post(self):
        self._allow("test branch")
        response = self.client.post(
            reverse("backoffice:branch_detail", args=[self.live_branch.id]),
            self._form(name="Silom", crm_branch="1"))
        self.assertEqual(response.status_code, 302)
        self.live_branch.refresh_from_db()
        self.assertIsNone(self.live_branch.crm_branch_id)

    def test_an_excluded_branch_cannot_have_loyalty_switched_on(self):
        self._allow("test branch")
        self.client.post(
            reverse("backoffice:branch_detail", args=[self.live_branch.id]),
            self._form(name="Silom", crm_panel="1", crm_loyalty="on"))
        self.live_branch.refresh_from_db()
        self.assertFalse(self.live_branch.crm_loyalty_enabled)

    def test_an_excluded_branch_never_reaches_the_crm(self):
        self._allow("test branch")
        with mock.patch("bravepos.crm.create_branch") as create:
            self.client.post(
                reverse("backoffice:branch_detail", args=[self.live_branch.id]),
                self._form(name="Silom", crm_branch="__create__"))
        create.assert_not_called()
