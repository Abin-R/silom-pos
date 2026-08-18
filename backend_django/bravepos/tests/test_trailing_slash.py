"""Both spellings of every URL work — with the trailing slash and without.

On 2026-08-18 ``/backoffice/branches/`` was a bare "Not Found" page while
``/backoffice/branches`` served the branch list: the URLconf declares that
route without a slash and nothing reconciled the two spellings.  The pages
Django itself declares (``/backoffice/login/``) had the mirror-image hole.

Run:
    python manage.py test bravepos.tests.test_trailing_slash \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

import uuid

from django.test import TestCase

from bravepos.models import BranchSession, Staff

from .factories import make_branch, make_shop


class SlashSpellingTests(TestCase):
    """A slash typed or omitted lands on the page, not on a 404."""

    def test_extra_slash_redirects_to_the_declared_url(self):
        r = self.client.get("/backoffice/branches/")
        self.assertEqual(r.status_code, 301)
        self.assertEqual(r["Location"], "/backoffice/branches")

    def test_missing_slash_redirects_to_the_declared_url(self):
        # The mirror direction: LoginView is declared *with* a slash.
        r = self.client.get("/backoffice/login")
        self.assertEqual(r.status_code, 301)
        self.assertEqual(r["Location"], "/backoffice/login/")

    def test_the_redirect_actually_arrives(self):
        r = self.client.get("/backoffice/login", follow=True)
        self.assertEqual(r.status_code, 200)

    def test_the_query_string_survives(self):
        r = self.client.get("/backoffice/transactions/", {"branch": "silom"})
        self.assertEqual(r.status_code, 301)
        self.assertEqual(r["Location"], "/backoffice/transactions?branch=silom")

    def test_the_api_answers_to_both_spellings(self):
        r = self.client.get("/api/orders/")
        self.assertEqual(r.status_code, 301)
        self.assertEqual(r["Location"], "/api/orders")


class UnsafeMethodTests(TestCase):
    """A redirected POST keeps its method and its body.

    This is why APPEND_SLASH is off: its 301 makes clients re-send a POST as a
    GET, dropping the form.  308 is the status they must replay verbatim.
    """

    def test_a_post_is_redirected_with_308(self):
        r = self.client.post("/api/orders/", data="{}",
                             content_type="application/json")
        self.assertEqual(r.status_code, 308)
        self.assertEqual(r["Location"], "/api/orders")

    def test_a_delete_is_redirected_with_308(self):
        pid = uuid.uuid4()
        r = self.client.delete(f"/api/parked-orders/{pid}/")
        self.assertEqual(r.status_code, 308)
        self.assertEqual(r["Location"], f"/api/parked-orders/{pid}")


class RealNotFoundTests(TestCase):
    """A 404 the view meant stays a 404."""

    def setUp(self):
        make_shop()
        self.branch = make_branch()
        self.staff = Staff.objects.create(
            name="Nok", email="nok@test.local", password_hash="x", role="admin",
        )
        self.staff.branches.add(self.branch)
        session = BranchSession.objects.create(
            token="tok" * 12, branch=self.branch, staff=self.staff,
        )
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {session.token}"}

    def test_a_missing_record_is_not_bounced_between_spellings(self):
        # Both /api/products/<uuid> and /api/products/<uuid>/ route (the DRF
        # routers register each ViewSet twice), so a product nobody has must
        # not ping-pong between the two forever.
        for url in (f"/api/products/{uuid.uuid4()}", f"/api/products/{uuid.uuid4()}/"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url, **self.auth).status_code, 404)

    def test_a_url_nobody_declared_is_still_a_404(self):
        self.assertEqual(self.client.get("/backoffice/nope").status_code, 404)
        self.assertEqual(self.client.get("/backoffice/nope/").status_code, 404)

    def test_the_site_root_is_left_alone(self):
        self.assertEqual(self.client.get("/").status_code, 404)
