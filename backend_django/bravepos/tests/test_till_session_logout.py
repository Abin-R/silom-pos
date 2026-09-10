"""Getting a staff account off a till it can no longer reach.

A staff account holds one till session at a time: `auth_pin_login` refuses a
second login with "already signed in at X". That rule is right while the
tablet is in someone's hands and wrong the moment it is not — a till that
went flat, crashed, dropped off the network or went away for repair keeps the
row, and the account is locked out of every other device until somebody
presses Log out on the machine that is the problem.

Two ways out, both covered here: the Force logout button on the staff form,
and a PIN reset, which is what an admin reaches for first when someone cannot
get in and so has to clear the session as well or it fixes nothing.

Run:
    python manage.py test bravepos.tests.test_till_session_logout \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from django.test import Client, TestCase
from django.urls import reverse

from bravepos.models import AuditLog, Branch, BranchSession, Staff


class TillSessionMixin:
    """One branch, two cashiers, both signed in on a till."""

    @classmethod
    def build(cls):
        cls.password = "correct-horse-battery"
        cls.admin = Staff(
            name="Backoffice Admin", username="boss",
            email="boss@therollingpinn.com", role="admin",
            active=True, backoffice_access=True,
        )
        cls.admin.set_password(cls.password)
        cls.admin.save()

        cls.branch = Branch.objects.create(name="Siam Paragon (Mooncake)", code="SPM")
        cls.other_branch = Branch.objects.create(name="The Mall Bangkae")

        cls.cashier = Staff.objects.create(
            name="Cashier", email="cashier@tills.invalid", role="cashier",
        )
        cls.cashier.set_pin("0000")
        cls.cashier.save()
        cls.cashier.branches.add(cls.branch, cls.other_branch)

        cls.colleague = Staff.objects.create(
            name="Da", email="da@tills.invalid", role="cashier",
        )
        cls.colleague.set_pin("1111")
        cls.colleague.save()
        cls.colleague.branches.add(cls.branch)

    def sign_in_on_a_till(self, member, branch=None):
        return BranchSession.objects.create(
            branch=branch or self.branch, staff=member,
            token=BranchSession.new_token(),
        )

    def form_url(self, member):
        return reverse("backoffice:staff_detail", args=[member.id])

    def logout_url(self, member):
        return reverse("backoffice:staff_force_logout", args=[member.id])


class ForceLogoutTests(TillSessionMixin, TestCase):
    """The button on the staff form."""

    @classmethod
    def setUpTestData(cls):
        cls.build()

    def setUp(self):
        self.assertTrue(self.client.login(username="boss", password=self.password))

    def test_it_ends_the_session_and_the_pin_works_on_another_device(self):
        self.sign_in_on_a_till(self.cashier)

        # Before: the PIN pad on a second tablet is refused.
        blocked = Client().post(
            "/api/auth/pin-login",
            {"staff_id": str(self.cashier.id), "pin": "0000",
             "branch_id": str(self.other_branch.id)},
            content_type="application/json",
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json().get("code"), "user_already_signed_in")

        response = self.client.post(self.logout_url(self.cashier))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(BranchSession.objects.filter(staff=self.cashier).exists())

        # After: the same PIN gets in, on a different tablet at a different branch.
        allowed = Client().post(
            "/api/auth/pin-login",
            {"staff_id": str(self.cashier.id), "pin": "0000",
             "branch_id": str(self.other_branch.id)},
            content_type="application/json",
        )
        self.assertEqual(allowed.status_code, 200, allowed.content)
        self.assertTrue(allowed.json().get("token"))

    def test_the_old_tablets_token_stops_working(self):
        """The forced logout has to reach the device, not just the database.

        Nothing can be pushed to a tablet that is off or out of range, so the
        token going dead on the next call is the whole mechanism: the app
        turns that 401 into a trip back to the PIN pad.
        """
        session = self.sign_in_on_a_till(self.cashier)
        till = Client(HTTP_AUTHORIZATION=f"Bearer {session.token}")
        self.assertEqual(till.get("/api/auth/me").status_code, 200)

        self.client.post(self.logout_url(self.cashier))

        self.assertEqual(till.get("/api/auth/me").status_code, 401)

    def test_it_leaves_a_colleague_on_the_same_branch_alone(self):
        self.sign_in_on_a_till(self.cashier)
        self.sign_in_on_a_till(self.colleague)

        self.client.post(self.logout_url(self.cashier))

        self.assertFalse(BranchSession.objects.filter(staff=self.cashier).exists())
        self.assertTrue(
            BranchSession.objects.filter(staff=self.colleague).exists(),
            "Da was signed out of a till she had nothing to do with",
        )

    def test_nobody_signed_in_is_not_an_error(self):
        response = self.client.post(self.logout_url(self.cashier))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(BranchSession.objects.filter(staff=self.cashier).exists())

    def test_a_get_does_not_sign_anyone_out(self):
        """Ending a session must not be reachable by following a link — a
        crawler, a prefetch or a bookmarked URL would empty the tills."""
        self.sign_in_on_a_till(self.cashier)
        self.client.get(self.logout_url(self.cashier))
        self.assertTrue(BranchSession.objects.filter(staff=self.cashier).exists())

    def test_it_needs_a_backoffice_login(self):
        self.sign_in_on_a_till(self.cashier)
        Client().post(self.logout_url(self.cashier))
        self.assertTrue(BranchSession.objects.filter(staff=self.cashier).exists())

    def test_the_audit_log_names_who_did_it_and_where(self):
        self.sign_in_on_a_till(self.cashier)
        self.client.post(self.logout_url(self.cashier))

        entry = AuditLog.objects.filter(action="logout", model="BranchSession").first()
        self.assertIsNotNone(entry, "forcing a logout left no trace")
        self.assertEqual(entry.actor_label, "Backoffice Admin")
        self.assertIn("Cashier", entry.object_label)
        self.assertIn(self.branch.name, entry.object_label)
        self.assertEqual(entry.note, "forced from the backoffice")


class ForceLogoutPanelTests(TillSessionMixin, TestCase):
    """What the staff form says before you press it."""

    @classmethod
    def setUpTestData(cls):
        cls.build()

    def setUp(self):
        self.assertTrue(self.client.login(username="boss", password=self.password))

    def test_it_names_the_till_holding_the_account(self):
        self.sign_in_on_a_till(self.cashier)
        page = self.client.get(self.form_url(self.cashier))
        self.assertContains(page, "Signed in at")
        self.assertContains(page, self.branch.name)
        self.assertContains(page, self.logout_url(self.cashier))

    def test_an_account_on_no_till_is_not_offered_the_button(self):
        page = self.client.get(self.form_url(self.cashier))
        self.assertContains(page, "Not signed in on any till")
        self.assertNotContains(page, self.logout_url(self.cashier))

    def test_a_new_staff_form_has_no_session_panel(self):
        page = self.client.get(reverse("backoffice:staff_new"))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Till session")


class PinResetEndsTheSessionTests(TillSessionMixin, TestCase):
    """Resetting the PIN is the other half.

    It is what an admin reaches for when someone says they cannot get in, and
    on its own it fixes nothing: the new PIN is refused on every other device
    for exactly the same reason the old one was.
    """

    @classmethod
    def setUpTestData(cls):
        cls.build()

    def setUp(self):
        self.assertTrue(self.client.login(username="boss", password=self.password))

    def edit(self, member, **fields):
        data = {"name": member.name, "role": member.role, "pin": "", "active": "on"}
        data.update(fields)
        return self.client.post(self.form_url(member), data)

    def test_a_new_pin_signs_them_off_the_till(self):
        self.sign_in_on_a_till(self.cashier)
        self.edit(self.cashier, pin="4321")

        self.cashier.refresh_from_db()
        self.assertTrue(self.cashier.check_pin("4321"))
        self.assertFalse(
            BranchSession.objects.filter(staff=self.cashier).exists(),
            "the new PIN would still be refused everywhere",
        )

    def test_it_does_not_touch_a_colleague(self):
        self.sign_in_on_a_till(self.cashier)
        self.sign_in_on_a_till(self.colleague)
        self.edit(self.cashier, pin="4321")
        self.assertTrue(BranchSession.objects.filter(staff=self.colleague).exists())

    def test_saving_without_changing_the_pin_leaves_them_signed_in(self):
        """Renaming somebody, or flipping a role, must not empty their till."""
        self.sign_in_on_a_till(self.cashier)
        self.edit(self.cashier, name="Cashier 1")

        self.cashier.refresh_from_db()
        self.assertEqual(self.cashier.name, "Cashier 1")
        self.assertTrue(
            BranchSession.objects.filter(staff=self.cashier).exists(),
            "a rename signed the cashier out mid-sale",
        )

    def test_a_refused_pin_changes_nothing_at_all(self):
        """A 5-digit PIN is rejected (it can never be typed on the pad). The
        session must survive with it — otherwise a typo empties the till and
        leaves the old PIN in place."""
        self.sign_in_on_a_till(self.cashier)
        self.edit(self.cashier, pin="12345")

        self.cashier.refresh_from_db()
        self.assertTrue(self.cashier.check_pin("0000"))
        self.assertTrue(BranchSession.objects.filter(staff=self.cashier).exists())
