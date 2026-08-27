"""The branch you pick in the header stays picked as you move around.

The picker submits `?branch=` on the page you are on, and nothing carried it
to the next one — so choosing "test branch" in Catalogue and clicking
Transactions silently put you back on whichever branch sorts first. Which
branch you are looking at is a property of the session, not of one URL.

Run:
    python manage.py test bravepos.tests.test_branch_scope \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

import uuid

from django.test import TestCase
from django.urls import reverse

from bravepos.models import Branch, Settings, Staff

# Pages that scope their figures to a branch, and pages that only render the
# picker as chrome (`_branch_topbar_context`) — the header has to agree on
# both, or it is showing a branch the numbers don't come from.
SCOPED_PAGES = ("dashboard", "transactions", "product_list", "inventory",
                "report_sku", "shop_settings")
CHROME_PAGES = ("user_list",)


class BranchScopeStickyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Settings.objects.get_or_create(id="shop")

        cls.password = "correct-horse-battery"
        cls.admin = Staff(
            name="Scope Admin", username="scope", email="scope@therollingpinn.com",
            role="admin", active=True, backoffice_access=True,
        )
        cls.admin.set_password(cls.password)
        cls.admin.save()

        # Named so the chosen one is deliberately *not* the alphabetical
        # default — otherwise every assertion below passes by accident.
        cls.default = Branch.objects.create(name="AAA Bio House", code="BIO")
        cls.chosen = Branch.objects.create(name="ZZZ Test Branch", code="TST")

    def setUp(self):
        signed_in = self.client.post(reverse("backoffice:login"), {
            "username": "scope", "password": self.password,
        })
        self.assertEqual(signed_in.status_code, 302, "scope admin could not sign in")

    def _branch_on(self, name, **params):
        response = self.client.get(reverse(f"backoffice:{name}"), params)
        self.assertEqual(response.status_code, 200, f"{name} did not render")
        return response.context["branch"]

    def _alphabetical_default(self):
        return Branch.objects.filter(active=True).order_by("name").first()

    def test_picked_branch_survives_a_move_to_another_page(self):
        self.assertEqual(self._branch_on("product_list", branch=str(self.chosen.id)),
                         self.chosen)
        for name in SCOPED_PAGES + CHROME_PAGES:
            with self.subTest(page=name):
                self.assertEqual(
                    self._branch_on(name), self.chosen,
                    f"{name} dropped the chosen branch and fell back to the default",
                )

    def test_an_explicit_branch_still_wins_and_becomes_the_new_scope(self):
        """A shared or bookmarked link keeps meaning what it said."""
        self._branch_on("product_list", branch=str(self.chosen.id))

        self.assertEqual(self._branch_on("transactions", branch=str(self.default.id)),
                         self.default)
        self.assertEqual(self._branch_on("product_list"), self.default)

    def test_a_branch_id_that_names_nothing_leaves_the_scope_alone(self):
        self._branch_on("product_list", branch=str(self.chosen.id))

        self.assertEqual(self._branch_on("transactions", branch=str(uuid.uuid4())),
                         self.chosen)
        self.assertEqual(self._branch_on("transactions", branch="not-a-uuid"),
                         self.chosen)

    def test_archiving_the_chosen_branch_returns_to_the_default(self):
        """A branch that is no longer a choice must not pin the whole
        backoffice to something the picker can't even show."""
        self._branch_on("product_list", branch=str(self.chosen.id))

        self.chosen.active = False
        self.chosen.save(update_fields=["active"])

        self.assertEqual(self._branch_on("transactions"), self._alphabetical_default())

    def test_a_fresh_session_starts_on_the_default(self):
        self.assertEqual(self._branch_on("transactions"), self._alphabetical_default())

    def test_the_audit_logs_own_branch_filter_is_not_the_global_scope(self):
        """Audit hides the header picker: its `?branch=` narrows that log, and
        a throwaway filter there must not follow you onto every other page."""
        self._branch_on("product_list", branch=str(self.chosen.id))

        audit = self.client.get(reverse("backoffice:audit_log"),
                                {"branch": str(self.default.id)})
        self.assertEqual(audit.status_code, 200)

        self.assertEqual(self._branch_on("transactions"), self.chosen)
