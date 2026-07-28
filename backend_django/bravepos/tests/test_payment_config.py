"""Payment credentials are per-branch and backoffice-only.

Two rules are load-bearing enough to pin down here:

  * a POS session can neither read nor write payment config — the app has no
    payment screen, and an older build still in the field must not be able to
    change where money lands; and
  * the backoffice form is write-only on secrets — submitting the page without
    retyping a key leaves the stored key alone, because "I edited the phone
    number" must never silently un-configure a live branch.
"""
from __future__ import annotations

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from backoffice.views import mask_secret
from bravepos.models import Branch, BranchSession, Settings, Staff


def _shop_with_keys() -> Settings:
    s, _ = Settings.objects.get_or_create(id="shop")
    s.beam_merchant_id = "SHOP-MERCHANT"
    s.beam_api_key = "shop-live-key-8888"
    s.omise_public_key = "pkey_shop"
    s.omise_secret_key = "skey_shop_9999"
    s.beam_sandbox = False
    s.save()
    return s


# ─── Masking ─────────────────────────────────────────────────────────────────
class MaskSecretTests(TestCase):
    def test_shows_only_the_last_four(self):
        self.assertEqual(mask_secret("skey_live_abcd1234"), "••••1234")

    def test_short_keys_are_masked_whole(self):
        """Never leak most of a short key just because it's short."""
        self.assertEqual(mask_secret("abcd"), "••••")

    def test_blank_stays_blank(self):
        self.assertEqual(mask_secret(""), "")
        self.assertEqual(mask_secret(None), "")


# ─── POS API ─────────────────────────────────────────────────────────────────
class SettingsApiHidesPaymentTests(TestCase):
    """/api/settings is the endpoint the tablets talk to."""

    def setUp(self):
        self.shop = _shop_with_keys()
        self.branch = Branch.objects.create(name="API Branch")
        staff = Staff(name="Admin", email="admin@x.test", role="admin", active=True)
        staff.set_password("pw")
        staff.save()
        staff.branches.add(self.branch)
        self.session = BranchSession.objects.create(
            branch=self.branch, staff=staff, token=BranchSession.new_token(),
        )
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.session.token}"}

    def test_get_does_not_expose_credentials(self):
        response = self.client.get("/api/settings", **self.auth)
        # Assert the call actually succeeded first — a 401 body contains no
        # payment fields either, and would pass the loop below for free.
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("shop_name", body)
        for field in ("beam_merchant_id", "beam_api_key", "beam_sandbox",
                      "omise_public_key", "omise_secret_key"):
            self.assertNotIn(field, body, f"{field} leaked to a POS session")

    def test_put_cannot_change_credentials(self):
        """An older app build echoing its stale payment fields back must be a
        no-op, not a redirect of the merchant account."""
        response = self.client.put(
            "/api/settings",
            data={
                "shop_name": "Renamed",
                "beam_merchant_id": "ATTACKER-MERCHANT",
                "beam_api_key": "attacker-key",
                "beam_sandbox": True,
            },
            content_type="application/json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)

        self.shop.refresh_from_db()
        self.assertEqual(self.shop.beam_merchant_id, "SHOP-MERCHANT")
        self.assertEqual(self.shop.beam_api_key, "shop-live-key-8888")
        self.assertFalse(self.shop.beam_sandbox)
        # The non-payment part of the same request still applied.
        self.assertEqual(self.shop.shop_name, "Renamed")


# ─── Deploy-day backfill ─────────────────────────────────────────────────────
class BackfillMigrationTests(TestCase):
    """0029's data step, exercised directly.

    A fresh test database has no pre-existing branches, so running the suite
    proves the migration *applies* but never that it *copies anything* — and
    copying is the whole reason tomorrow's first sale still goes through. This
    calls the backfill against the live app registry instead; the historical and
    current models are identical across every field it touches.
    """

    BLANK = {
        "beam_merchant_id": "", "beam_api_key": "",
        "omise_public_key": "", "omise_secret_key": "",
    }

    def setUp(self):
        self.shop = _shop_with_keys()
        # The pre_save signal seeds anything created normally, so blank the row
        # afterwards with an UPDATE to reproduce a branch as it exists today.
        self.branch = Branch.objects.create(name="Legacy Branch")
        Branch.objects.filter(pk=self.branch.pk).update(**self.BLANK)

    @staticmethod
    def _backfill():
        from importlib import import_module

        from django.apps import apps as live_apps

        module = import_module("bravepos.migrations.0029_branch_owns_payment_config")
        module.seed_branches_from_shop(live_apps, None)

    def test_existing_branch_inherits_the_shop_account(self):
        self._backfill()
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.beam_merchant_id, "SHOP-MERCHANT")
        self.assertEqual(self.branch.beam_api_key, "shop-live-key-8888")
        self.assertEqual(self.branch.omise_secret_key, "skey_shop_9999")
        # Lane comes across too, so a backfilled branch is live like the shop.
        self.assertFalse(self.branch.beam_sandbox)

    def test_the_branch_keeps_trading_on_the_same_account(self):
        """The point of the backfill: resolution before and after must land on
        the same merchant account."""
        from bravepos.gateways import resolve_payment_config

        before = resolve_payment_config(Branch.objects.get(pk=self.branch.pk))
        self._backfill()
        after = resolve_payment_config(Branch.objects.get(pk=self.branch.pk))

        self.assertEqual(before.beam_merchant_id, after.beam_merchant_id)
        self.assertEqual(before.beam_api_key, after.beam_api_key)

    def test_an_already_configured_branch_is_left_alone(self):
        Branch.objects.filter(pk=self.branch.pk).update(
            beam_merchant_id="BRANCH-OWN", beam_api_key="branch-own-key",
        )
        self._backfill()
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.beam_merchant_id, "BRANCH-OWN")
        self.assertEqual(self.branch.beam_api_key, "branch-own-key")

    def test_running_twice_changes_nothing(self):
        """Migrations get re-run against restored snapshots; the second pass
        must be a no-op rather than re-copying over a since-edited key."""
        self._backfill()
        Branch.objects.filter(pk=self.branch.pk).update(beam_api_key="rotated-later")
        self._backfill()
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.beam_api_key, "rotated-later")


# ─── Backoffice form ─────────────────────────────────────────────────────────
class BranchPaymentFormTests(TestCase):
    def setUp(self):
        self.shop = _shop_with_keys()
        self.branch = Branch.objects.create(name="Form Branch", code="FRM")

        admin = Staff(
            name="BO Admin", username="bo", email="bo@x.test",
            role="admin", active=True, backoffice_access=True,
        )
        admin.set_password("pw-pw-pw")
        admin.save()
        self.client.post(reverse("backoffice:login"),
                         {"username": "bo", "password": "pw-pw-pw"})
        self.url = reverse("backoffice:branch_detail", kwargs={"branch_id": self.branch.id})

    def _post(self, **overrides):
        data = {
            "name": self.branch.name, "code": self.branch.code,
            "open_time": "09:00", "close_time": "22:00",
            "peak_account_code": "BSV003", "active": "on",
            "payment_mode": "live",
            "beam_merchant_id": "SHOP-MERCHANT",
            "omise_public_key": "pkey_shop",
            "beam_card_fee_percent": "3.65",
            "omise_fee_percent": "3.65",
        }
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_page_never_renders_a_stored_key(self):
        html = self.client.get(self.url).content.decode()
        self.assertNotIn("shop-live-key-8888", html)
        self.assertNotIn("skey_shop_9999", html)
        self.assertIn("••••8888", html)  # the mask, as placeholder text

    def test_blank_key_field_leaves_the_stored_key_alone(self):
        self._post(beam_api_key="", omise_secret_key="")
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.beam_api_key, "shop-live-key-8888")
        self.assertEqual(self.branch.omise_secret_key, "skey_shop_9999")

    def test_echoing_the_mask_back_leaves_the_stored_key_alone(self):
        """A browser that autofills the placeholder must not overwrite the key
        with a row of dots."""
        self._post(beam_api_key="••••8888")
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.beam_api_key, "shop-live-key-8888")

    def test_a_typed_key_replaces_the_stored_one(self):
        self._post(beam_api_key="branch-key-4321")
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.beam_api_key, "branch-key-4321")

    def test_clearing_requires_the_explicit_checkbox(self):
        self._post(beam_api_key="", beam_api_key_clear="on")
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.beam_api_key, "")

    def test_mode_switches_the_lane(self):
        self._post(payment_mode="test")
        self.branch.refresh_from_db()
        self.assertTrue(self.branch.beam_sandbox)

        self._post(payment_mode="live")
        self.branch.refresh_from_db()
        self.assertFalse(self.branch.beam_sandbox)

    def test_fee_percentages_are_saved(self):
        self._post(beam_card_fee_percent="2.50", omise_fee_percent="4.00")
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.beam_card_fee_percent, Decimal("2.50"))
        self.assertEqual(self.branch.omise_fee_percent, Decimal("4.00"))

    def test_a_junk_fee_value_is_ignored_not_saved_as_zero(self):
        """A typo must not silently drop the branch's processing fee to nothing."""
        self._post(beam_card_fee_percent="three point six five")
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.beam_card_fee_percent, Decimal("3.65"))


class AddBranchFormTests(TestCase):
    """Creating a branch through the backoffice must yield one that can trade."""

    def setUp(self):
        self.shop = _shop_with_keys()
        admin = Staff(
            name="BO Admin", username="bo", email="bo@x.test",
            role="admin", active=True, backoffice_access=True,
        )
        admin.set_password("pw-pw-pw")
        admin.save()
        self.client.post(reverse("backoffice:login"),
                         {"username": "bo", "password": "pw-pw-pw"})

    def test_new_branch_gets_a_complete_payment_config(self):
        """The add form renders the merchant IDs but leaves the key fields
        empty — they are write-only. A branch created that way must still end
        up holding the template's keys, not a merchant ID and nothing else.
        """
        response = self.client.post(reverse("backoffice:branch_new"), {
            "name": "Brand New", "code": "NEW",
            "open_time": "09:00", "close_time": "22:00",
            "peak_account_code": "BSV003", "active": "on",
            "payment_mode": "live",
            "beam_merchant_id": "SHOP-MERCHANT",
            "omise_public_key": "pkey_shop",
            "beam_card_fee_percent": "3.65",
            "omise_fee_percent": "3.65",
            # beam_api_key / omise_secret_key deliberately absent.
        })
        self.assertEqual(response.status_code, 302)

        branch = Branch.objects.get(name="Brand New")
        self.assertEqual(branch.beam_merchant_id, "SHOP-MERCHANT")
        self.assertEqual(branch.beam_api_key, "shop-live-key-8888")
        self.assertEqual(branch.omise_secret_key, "skey_shop_9999")
        self.assertFalse(branch.beam_sandbox)

    def test_a_typed_key_on_the_add_form_wins_over_the_template(self):
        self.client.post(reverse("backoffice:branch_new"), {
            "name": "Own Keys", "code": "OWN",
            "open_time": "09:00", "close_time": "22:00",
            "peak_account_code": "BSV003", "active": "on",
            "payment_mode": "test",
            "beam_merchant_id": "OWN-MERCHANT",
            "beam_api_key": "own-playground-key",
            "omise_public_key": "pkey_test_own",
            "omise_secret_key": "skey_test_own",
            "beam_card_fee_percent": "3.65",
            "omise_fee_percent": "3.65",
        })
        branch = Branch.objects.get(name="Own Keys")
        self.assertEqual(branch.beam_api_key, "own-playground-key")
        self.assertEqual(branch.omise_secret_key, "skey_test_own")
        self.assertTrue(branch.beam_sandbox)
