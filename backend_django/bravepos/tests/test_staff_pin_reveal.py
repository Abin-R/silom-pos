"""The staff form's PIN field is masked, and only a backoffice admin gets the
Show toggle. It reveals what is being typed, never the stored PIN — that is a
one-way hash and the page must not carry it in any form.

Run:
    python manage.py test bravepos.tests.test_staff_pin_reveal \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from bravepos.models import Branch, Staff


class StaffPinRevealTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.password = "correct-horse-battery"
        cls.branch = Branch.objects.create(name="Test Branch")
        cls.member = Staff.objects.create(
            name="Cashier", email="cashier@tills.invalid", role="cashier",
        )
        cls.member.set_pin("0000")
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

    def test_admin_gets_the_toggle_on_a_masked_field(self):
        self.account("boss", "admin")
        html = self.page().content.decode()
        self.assertIn('id="f-pin" type="password"', html)
        self.assertIn('id="pinToggle"', html)
        self.assertNotIn("0000", html)

    def test_a_non_admin_gets_no_toggle(self):
        self.account("manager", "cashier")
        response = self.page()
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('id="f-pin" type="password"', html)
        self.assertNotIn('id="pinToggle"', html)
