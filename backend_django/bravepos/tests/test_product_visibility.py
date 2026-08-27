"""A product removed in the backoffice must not reach a cashier.

The till asks ``/api/products`` with no filter, so the *default* is what decides
this — and fixing the default fixes tills that are already installed, which an
app-side change cannot.

Run:
    python manage.py test bravepos.tests.test_product_visibility \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from django.test import TestCase

from bravepos.models import BranchSession, Staff

from .factories import make_branch, make_product


class RemovedProductsAreNotOfferedTests(TestCase):
    """A product taken off sale must not reach a cashier taking an order.

    The till asks `/api/products` with no filter, so the default is what
    decides this — and fixing the default fixes tills that are already
    installed, which an app-side change cannot.
    """

    def setUp(self):
        self.branch = make_branch(name="BIO HOUSE")
        staff = Staff.objects.create(
            name="Nok", email="nok@removed.local", password_hash="x", role="admin",
        )
        staff.branches.add(self.branch)
        session = BranchSession.objects.create(
            token="rmvd" * 9, branch=self.branch, staff=staff,
        )
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {session.token}"}

    def _retire(self, name):
        product = make_product(self.branch, name=name)
        product.active = False
        product.save(update_fields=["active"])
        return product

    def _names(self, query=""):
        res = self.client.get(f"/api/products{query}", **self.auth)
        self.assertEqual(res.status_code, 200)
        return [p["name"] for p in res.json()]

    def test_the_till_is_not_offered_a_removed_product(self):
        make_product(self.branch, name="On Sale Cookie")
        gone = make_product(self.branch, name="Retired Cookie")

        self.assertIn("Retired Cookie", self._names())

        gone.active = False
        gone.save(update_fields=["active"])

        # The unfiltered call the till actually makes.
        self.assertEqual(self._names(), ["On Sale Cookie"])

    def test_asking_for_removed_ones_explicitly_still_works(self):
        self._retire("Retired Cookie")
        self.assertEqual(self._names("?active=false"), ["Retired Cookie"])

    def test_a_removed_product_is_still_reachable_by_id(self):
        """Only listings are narrowed — the sync actions still need to resolve
        a deactivated row, and so does any edit that puts it back on sale."""
        gone = self._retire("Retired Cookie")
        res = self.client.get(f"/api/products/{gone.id}", **self.auth)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["name"], "Retired Cookie")
