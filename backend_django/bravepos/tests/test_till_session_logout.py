"""A PIN reset has to get the staff member off the till as well.

A staff account holds one till session at a time: `auth_pin_login` refuses a
second login with "already signed in at X". That rule is right while the
tablet is in someone's hands and wrong the moment it is not — a till that
went flat, crashed, dropped off the network or went away for repair keeps the
row, and the account is locked out of every other device.

Resetting the PIN is what an admin reaches for when someone says they cannot
get in, so it is the reset that has to clear the session. On its own it fixes
nothing: the new PIN is refused everywhere else for exactly the same reason
the old one was.

Run:
    python manage.py test bravepos.tests.test_till_session_logout \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from django.test import Client, TestCase
from django.urls import reverse

from bravepos.models import AuditLog, Branch, BranchSession, Staff


class PinResetMixin:
    """One branch, two cashiers, either of whom may be holding a till."""

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

    def pin_login(self, member, pin, branch):
        return Client().post(
            "/api/auth/pin-login",
            {"staff_id": str(member.id), "pin": pin, "branch_id": str(branch.id)},
            content_type="application/json",
        )


class PinResetEndsTheSessionTests(PinResetMixin, TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.build()

    def setUp(self):
        self.assertTrue(self.client.login(username="boss", password=self.password))

    def edit(self, member, **fields):
        data = {"name": member.name, "role": member.role, "pin": "", "active": "on"}
        data.update(fields)
        return self.client.post(self.form_url(member), data)

    def test_the_new_pin_works_on_a_different_device(self):
        """The whole point. Without the session going too, the reset hands
        someone a PIN that is refused everywhere, for the same reason the old
        one was."""
        self.sign_in_on_a_till(self.cashier)

        blocked = self.pin_login(self.cashier, "0000", self.other_branch)
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json().get("code"), "user_already_signed_in")

        self.edit(self.cashier, pin="4321")

        allowed = self.pin_login(self.cashier, "4321", self.other_branch)
        self.assertEqual(allowed.status_code, 200, allowed.content)
        self.assertTrue(allowed.json().get("token"))

    def test_the_old_tablets_token_stops_working(self):
        """Nothing can be pushed to a tablet that is off or out of range, so
        the token going dead on its next call is the whole mechanism."""
        session = self.sign_in_on_a_till(self.cashier)
        till = Client(HTTP_AUTHORIZATION=f"Bearer {session.token}")
        self.assertEqual(till.get("/api/auth/me").status_code, 200)

        self.edit(self.cashier, pin="4321")

        self.assertEqual(till.get("/api/auth/me").status_code, 401)

    def test_it_does_not_touch_a_colleague(self):
        self.sign_in_on_a_till(self.cashier)
        self.sign_in_on_a_till(self.colleague)

        self.edit(self.cashier, pin="4321")

        self.assertFalse(BranchSession.objects.filter(staff=self.cashier).exists())
        self.assertTrue(
            BranchSession.objects.filter(staff=self.colleague).exists(),
            "Da was signed out of a till she had nothing to do with",
        )

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
        """A 5-digit PIN is rejected — it can never be typed on the pad. The
        session must survive it, or a typo empties the till *and* leaves the
        old PIN in place."""
        self.sign_in_on_a_till(self.cashier)
        self.edit(self.cashier, pin="12345")

        self.cashier.refresh_from_db()
        self.assertTrue(self.cashier.check_pin("0000"))
        self.assertTrue(BranchSession.objects.filter(staff=self.cashier).exists())

    def test_merely_opening_the_form_signs_nobody_out(self):
        self.sign_in_on_a_till(self.cashier)
        self.assertEqual(self.client.get(self.form_url(self.cashier)).status_code, 200)
        self.assertTrue(BranchSession.objects.filter(staff=self.cashier).exists())

    def test_it_needs_a_backoffice_login(self):
        self.sign_in_on_a_till(self.cashier)
        Client().post(self.form_url(self.cashier),
                      {"name": "Cashier", "role": "cashier", "pin": "4321",
                       "active": "on"})
        self.assertTrue(BranchSession.objects.filter(staff=self.cashier).exists())
        self.cashier.refresh_from_db()
        self.assertTrue(self.cashier.check_pin("0000"))

    def test_the_audit_log_names_who_did_it_and_where(self):
        self.sign_in_on_a_till(self.cashier)
        self.edit(self.cashier, pin="4321")

        entry = AuditLog.objects.filter(action="logout", model="BranchSession").first()
        self.assertIsNotNone(entry, "the session was ended with no trace")
        self.assertEqual(entry.actor_label, "Backoffice Admin")
        self.assertIn("Cashier", entry.object_label)
        self.assertIn(self.branch.name, entry.object_label)
        self.assertEqual(entry.note, "till PIN reset")

    def test_a_staff_member_on_no_till_resets_fine(self):
        self.edit(self.cashier, pin="4321")
        self.cashier.refresh_from_db()
        self.assertTrue(self.cashier.check_pin("4321"))
