"""Admins can read a staff member's current till PIN on the staff page;
nobody else can.

The PIN field is masked and only an admin gets its Show toggle. The *current*
PIN comes from an encrypted copy (``pin_vault``), fetched by an admin-only
POST that writes an audit row — the digits are never in the page source.

Run:
    python manage.py test bravepos.tests.test_staff_pin_reveal \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from bravepos import pin_vault
from bravepos.models import AuditLog, Branch, Staff


class PinVaultTests(TestCase):

    def test_round_trip(self):
        self.assertEqual(pin_vault.unseal(pin_vault.seal("4821")), "4821")

    def test_same_pin_seals_differently(self):
        self.assertNotEqual(pin_vault.seal("4821"), pin_vault.seal("4821"))

    def test_blank_or_tampered_reads_as_unknown(self):
        self.assertIsNone(pin_vault.unseal(""))
        self.assertIsNone(pin_vault.unseal(pin_vault.seal("4821")[:-2] + "xx"))

    def test_set_pin_keeps_the_hash_working(self):
        member = Staff(name="X", email="x@x.invalid", role="cashier")
        member.set_pin("4821")
        self.assertTrue(member.check_pin("4821"))
        self.assertEqual(member.current_pin(), "4821")
        self.assertNotIn("4821", member.pin_encrypted)


class StaffPinRevealTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.password = "correct-horse-battery"
        cls.branch = Branch.objects.create(name="Test Branch")
        cls.member = Staff.objects.create(
            name="Cashier", email="cashier@tills.invalid", role="cashier",
        )
        cls.member.set_pin("4821")
        cls.member.save()
        cls.member.branches.add(cls.branch)

    def account(self, username, role):
        user = Staff(name=username, username=username, email=f"{username}@x.invalid",
                     role=role, active=True, backoffice_access=True)
        user.set_password(self.password)
        user.save()
        user.branches.add(self.branch)
        self.assertTrue(self.client.login(username=username, password=self.password))

    def page(self):
        return self.client.get(reverse("backoffice:staff_detail", args=[self.member.id]))

    def reveal(self, member=None):
        return self.client.post(
            reverse("backoffice:staff_pin_reveal", args=[(member or self.member).id]))

    def test_admin_gets_both_toggles_and_no_digits_in_the_source(self):
        self.account("boss", "admin")
        html = self.page().content.decode()
        self.assertIn('id="f-pin" type="password"', html)
        self.assertIn('id="pinToggle"', html)
        self.assertIn('id="curPinBtn"', html)
        self.assertNotIn("4821", html)

    def test_a_non_admin_gets_neither(self):
        self.account("manager", "cashier")
        response = self.page()
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('id="f-pin" type="password"', html)
        self.assertNotIn('id="pinToggle"', html)
        self.assertNotIn('id="curPinBtn"', html)

    def test_admin_reads_the_pin_and_it_is_audited(self):
        self.account("boss", "admin")
        response = self.reveal()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"pin": "4821"})
        self.assertEqual(response["Cache-Control"], "no-store")
        entry = AuditLog.objects.get(action="reveal")
        self.assertEqual(entry.object_id, str(self.member.id))
        self.assertEqual(entry.actor.username, "boss")
        self.assertNotIn("4821", str(entry.changes) + entry.note)

    def test_a_non_admin_cannot_read_it(self):
        self.account("manager", "cashier")
        response = self.reveal()
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("4821", response.content.decode())
        self.assertFalse(AuditLog.objects.filter(action="reveal").exists())

    def test_get_is_refused(self):
        self.account("boss", "admin")
        response = self.client.get(
            reverse("backoffice:staff_pin_reveal", args=[self.member.id]))
        self.assertEqual(response.status_code, 405)

    def test_a_pin_from_before_reads_as_unknown(self):
        Staff.objects.filter(pk=self.member.pk).update(pin_encrypted="")
        self.account("boss", "admin")
        self.assertEqual(self.reveal().json(), {"pin": None})
