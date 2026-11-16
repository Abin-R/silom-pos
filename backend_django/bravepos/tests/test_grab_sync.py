"""Grab stock sync, BravePOS half: apply / restore / stock endpoints.

A poller outside this repo calls these with an API key.  What matters is that a
Grab order takes stock out exactly once however often it is sent, that a
cancelled one puts it back exactly once and is never deducted again, that the
"inventory as of" report still adds up afterwards, and that nothing changes
unless the write switch is on.

Run:
    python manage.py test bravepos.tests.test_grab_sync \\
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

import os
from datetime import timedelta
from unittest import mock, skipUnless

from django.db import IntegrityError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from bravepos import grab_sync
from bravepos.models import AuditLog, Branch, Product, Settings, Staff, StockMovement

from .factories import make_branch, make_product
from .test_selforder import run_concurrently

KEY = "test-grab-key"
MERCHANT = "3-C2M2RUXENZCZGT"
ORDER = "001553556136-C8LJEFNGJPAVCA"

APPLY = "/api/grab/orders/apply"
RESTORE = "/api/grab/orders/restore"
STOCK = "/api/grab/stock"


def sync_env(writes=True, key=KEY):
    """The environment a deployed poller would meet."""
    env = {grab_sync.WRITES_ENV: "1" if writes else ""}
    env[grab_sync.API_KEY_ENV] = key
    return mock.patch.dict(os.environ, env)


class GrabTestCase(TestCase):
    def setUp(self):
        patcher = sync_env()
        patcher.start()
        self.addCleanup(patcher.stop)
        self.silom = make_branch(name="Silom")
        self.silom.grab_merchant_id = MERCHANT
        self.silom.save()
        self.paragon = make_branch(name="Paragon")
        self.latte = make_product(self.silom, name="Latte", stock=10)
        self.latte.barcode = "8812"
        self.latte.save()
        self.cake = make_product(self.silom, name="Cake", stock=5)
        self.cake.barcode = "8813"
        self.cake.save()

    def post(self, url, body, key=KEY):
        headers = {"HTTP_X_API_KEY": key} if key is not None else {}
        return self.client.post(url, body, content_type="application/json", **headers)

    def apply(self, items=None, **extra):
        body = {
            "merchant_id": MERCHANT, "order_id": ORDER, "display_id": "GF-921",
            "items": items if items is not None else [
                {"barcode": "8812", "qty": 2, "grab_item_id": "ITEM1", "name": "Latte"}],
        }
        body.update(extra)
        return self.post(APPLY, body)

    def restore(self, **extra):
        body = {"merchant_id": MERCHANT, "order_id": ORDER, "display_id": "GF-921",
                "reason": "CANCELLED_MAX"}
        body.update(extra)
        return self.post(RESTORE, body)

    def stock_of(self, product):
        product.refresh_from_db()
        return product.stock


class AuthTests(GrabTestCase):
    def test_missing_key_is_401(self):
        for url, body in ((APPLY, {}), (RESTORE, {})):
            self.assertEqual(self.post(url, body, key=None).status_code, 401, url)
        self.assertEqual(self.client.get(STOCK, {"merchant_id": MERCHANT}).status_code, 401)

    def test_wrong_key_is_401_and_changes_nothing(self):
        res = self.post(APPLY, {"merchant_id": MERCHANT, "order_id": ORDER,
                                "items": [{"barcode": "8812", "qty": 1}]}, key="nope")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(self.stock_of(self.latte), 10)
        res = self.client.get(STOCK, {"merchant_id": MERCHANT}, HTTP_X_API_KEY="nope")
        self.assertEqual(res.status_code, 401)

    def test_non_ascii_key_is_401_not_a_500(self):
        self.assertEqual(self.post(APPLY, {}, key="clé").status_code, 401)

    def test_unset_key_env_is_503_for_every_call(self):
        with mock.patch.dict(os.environ, {grab_sync.API_KEY_ENV: ""}):
            self.assertEqual(self.apply().status_code, 503)
            self.assertEqual(self.restore().status_code, 503)
            self.assertEqual(
                self.client.get(STOCK, {"merchant_id": MERCHANT}, HTTP_X_API_KEY=KEY).status_code,
                503)
            # Even the right-looking empty key does not get in.
            self.assertEqual(self.post(APPLY, {}, key="").status_code, 503)

    def test_unknown_merchant_is_404(self):
        self.assertEqual(self.apply(merchant_id="3-UNKNOWN").status_code, 404)
        self.assertEqual(self.restore(merchant_id="3-UNKNOWN").status_code, 404)
        res = self.client.get(STOCK, {"merchant_id": "3-UNKNOWN"}, HTTP_X_API_KEY=KEY)
        self.assertEqual(res.status_code, 404)

    def test_inactive_branch_is_404(self):
        Branch.objects.filter(pk=self.silom.pk).update(active=False)
        self.assertEqual(self.apply().status_code, 404)
        self.assertEqual(self.restore().status_code, 404)
        res = self.client.get(STOCK, {"merchant_id": MERCHANT}, HTTP_X_API_KEY=KEY)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(self.stock_of(self.latte), 10)

    def test_a_blank_merchant_id_is_400_and_never_matches_an_unsynced_branch(self):
        # paragon has grab_merchant_id "" — a blank lookup must not find it.
        self.assertEqual(self.apply(merchant_id="").status_code, 400)
        self.assertEqual(self.apply(merchant_id="  ").status_code, 400)
        res = self.client.get(STOCK, HTTP_X_API_KEY=KEY)
        self.assertEqual(res.status_code, 400)

    def test_merchant_id_is_matched_case_insensitively(self):
        self.assertEqual(self.apply(merchant_id=MERCHANT.lower()).status_code, 200)

    def test_bad_bodies_are_400(self):
        self.assertEqual(self.apply(order_id="").status_code, 400)
        self.assertEqual(self.apply(order_id="x" * 65).status_code, 400)
        self.assertEqual(self.apply(items="8812").status_code, 400)
        self.assertEqual(self.apply(items=[{"barcode": "8812", "qty": 0}]).status_code, 400)
        self.assertEqual(self.apply(items=[{"barcode": "8812", "qty": "2"}]).status_code, 400)
        self.assertEqual(self.apply(items=[{"barcode": "8812", "qty": True}]).status_code, 400)
        self.assertEqual(self.post(APPLY, [1, 2]).status_code, 400)
        self.assertEqual(self.stock_of(self.latte), 10)


class ApplyTests(GrabTestCase):
    def test_apply_takes_stock_out_and_writes_the_movement(self):
        res = self.apply()
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["status"], "applied")
        self.assertEqual(body["branch"], "Silom")
        self.assertEqual(body["skipped"], [])
        self.assertEqual(body["lines"], [{
            "barcode": "8812", "product_id": str(self.latte.id),
            "qty": 2, "stock_after": 8,
        }])
        self.assertEqual(self.stock_of(self.latte), 8)
        move = StockMovement.objects.get(product=self.latte)
        self.assertEqual(move.type, "out")
        self.assertEqual(move.qty, 2)
        self.assertEqual(move.document_no, ORDER)
        self.assertEqual(move.note, "Grab GF-921")
        self.assertEqual(move.created_by, "Grab sync")
        self.assertEqual(move.branch, self.silom)

    def test_apply_writes_an_audit_row_with_the_stock_change(self):
        AuditLog.objects.all().delete()
        self.apply()
        rows = AuditLog.objects.filter(model="Product", object_id=str(self.latte.id))
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.action, "update")
        self.assertEqual(row.changes, {"stock": {"from": 10, "to": 8}})
        self.assertEqual(row.source, "grab-sync")
        self.assertEqual(row.branch_id, self.silom.id)

    def test_a_repeat_is_already_applied_and_stock_is_unchanged(self):
        self.apply()
        res = self.apply()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "already_applied")
        self.assertEqual(self.stock_of(self.latte), 8)
        self.assertEqual(StockMovement.objects.filter(document_no=ORDER).count(), 1)

    def test_a_repeat_with_different_items_still_does_nothing(self):
        self.apply()
        res = self.apply(items=[{"barcode": "8813", "qty": 4}])
        self.assertEqual(res.json()["status"], "already_applied")
        self.assertEqual(self.stock_of(self.cake), 5)

    def test_two_orders_with_the_same_display_id_are_separate(self):
        self.apply()
        res = self.apply(order_id="OTHER-ORDER")      # same GF-921
        self.assertEqual(res.json()["status"], "applied")
        self.assertEqual(self.stock_of(self.latte), 6)

    def test_duplicate_barcodes_are_merged(self):
        res = self.apply(items=[
            {"barcode": "8812", "qty": 1, "grab_item_id": "A"},
            {"barcode": "8812", "qty": 2, "grab_item_id": "B"},
        ])
        body = res.json()
        self.assertEqual(len(body["lines"]), 1)
        self.assertEqual(body["lines"][0]["qty"], 3)
        self.assertEqual(self.stock_of(self.latte), 7)
        self.assertEqual(StockMovement.objects.filter(document_no=ORDER).count(), 1)

    def test_several_products_in_one_order(self):
        res = self.apply(items=[{"barcode": "8812", "qty": 1}, {"barcode": "8813", "qty": 2}])
        self.assertEqual(res.json()["status"], "applied")
        self.assertEqual(self.stock_of(self.latte), 9)
        self.assertEqual(self.stock_of(self.cake), 3)
        self.assertEqual(StockMovement.objects.filter(document_no=ORDER).count(), 2)

    def test_unknown_barcodes_are_skipped_and_the_rest_applied(self):
        res = self.apply(items=[
            {"barcode": "8812", "qty": 1},
            {"barcode": "9001", "qty": 1, "grab_item_id": "GONE"},
            {"barcode": "", "qty": 1, "grab_item_id": "NOCODE"},
        ])
        body = res.json()
        self.assertEqual(body["status"], "applied")
        self.assertEqual(body["skipped"], [
            {"barcode": "9001", "grab_item_id": "GONE", "reason": "not_at_branch"},
            {"barcode": "", "grab_item_id": "NOCODE", "reason": "no_barcode"},
        ])
        self.assertEqual(self.stock_of(self.latte), 9)

    def test_an_inactive_product_is_skipped(self):
        Product.objects.filter(pk=self.latte.pk).update(active=False)
        res = self.apply()
        body = res.json()
        self.assertEqual(body["status"], "nothing_to_apply")
        self.assertEqual(body["skipped"][0]["reason"], "not_at_branch")
        self.assertEqual(self.stock_of(self.latte), 10)
        self.assertFalse(StockMovement.objects.exists())

    def test_nothing_to_apply_writes_nothing_and_is_not_a_ledger_entry(self):
        res = self.apply(items=[{"barcode": "9001", "qty": 1}])
        self.assertEqual(res.json()["status"], "nothing_to_apply")
        self.assertFalse(StockMovement.objects.exists())
        # So once the product exists, the same order can still be applied.
        other = make_product(self.silom, name="Late arrival", stock=4)
        other.barcode = "9001"
        other.save()
        res = self.apply(items=[{"barcode": "9001", "qty": 1}])
        self.assertEqual(res.json()["status"], "applied")

    def test_an_empty_order_is_nothing_to_apply(self):
        self.assertEqual(self.apply(items=[]).json()["status"], "nothing_to_apply")

    def test_another_branchs_product_with_the_same_barcode_is_untouched(self):
        theirs = make_product(self.paragon, name="Latte", stock=20)
        theirs.barcode = "8812"
        theirs.save()
        self.apply()
        self.assertEqual(self.stock_of(theirs), 20)
        self.assertEqual(self.stock_of(self.latte), 8)
        self.assertFalse(StockMovement.objects.filter(product=theirs).exists())

    def test_stock_may_go_negative(self):
        res = self.apply(items=[{"barcode": "8813", "qty": 8}])
        self.assertEqual(res.json()["lines"][0]["stock_after"], -3)
        self.assertEqual(self.stock_of(self.cake), -3)

    def test_a_blank_display_id_still_applies(self):
        res = self.apply(display_id="")
        self.assertEqual(res.json()["status"], "applied")
        self.assertEqual(StockMovement.objects.get().note, "Grab")


class DryRunTests(GrabTestCase):
    def assert_untouched(self):
        self.assertEqual(self.stock_of(self.latte), 10)
        self.assertFalse(StockMovement.objects.exists())

    def test_dry_run_in_the_body_changes_nothing_and_returns_the_lines(self):
        AuditLog.objects.all().delete()
        res = self.apply(dry_run=True)
        body = res.json()
        self.assertEqual(body["status"], "dry_run")
        self.assertEqual(body["lines"][0]["stock_after"], 8)
        self.assert_untouched()
        self.assertFalse(AuditLog.objects.exists())

    def test_writes_off_is_a_dry_run_even_without_the_flag(self):
        with sync_env(writes=False):
            res = self.apply()
            self.assertEqual(res.json()["status"], "dry_run")
            self.assertEqual(res.json()["lines"][0]["stock_after"], 8)
            self.assert_untouched()
        # Only the exact string 1 turns writes on.
        with sync_env(writes=False), mock.patch.dict(os.environ, {grab_sync.WRITES_ENV: "true"}):
            self.assertEqual(self.apply().json()["status"], "dry_run")
        self.assert_untouched()

    def test_dry_run_still_reports_skipped_items(self):
        res = self.apply(dry_run=True, items=[{"barcode": "9001", "qty": 1}])
        self.assertEqual(res.json()["status"], "nothing_to_apply")
        self.assertEqual(res.json()["skipped"][0]["reason"], "not_at_branch")

    def test_dry_run_of_an_order_already_applied_reports_the_ledger(self):
        self.apply()
        res = self.apply(dry_run=True)
        self.assertEqual(res.json()["status"], "already_applied")
        self.assertEqual(self.stock_of(self.latte), 8)

    def test_dry_run_does_not_mark_the_order_so_a_live_call_still_applies(self):
        self.apply(dry_run=True)
        self.assertEqual(self.apply().json()["status"], "applied")
        self.assertEqual(self.stock_of(self.latte), 8)

    def test_restore_dry_run_changes_nothing(self):
        self.apply()
        res = self.restore(dry_run=True)
        self.assertEqual(res.json()["status"], "dry_run")
        self.assertEqual(res.json()["lines"][0]["stock_after"], 10)
        self.assertEqual(self.stock_of(self.latte), 8)
        self.assertFalse(StockMovement.objects.filter(type="return").exists())

    def test_restore_with_writes_off_changes_nothing(self):
        self.apply()
        with sync_env(writes=False):
            self.assertEqual(self.restore().json()["status"], "dry_run")
        self.assertEqual(self.stock_of(self.latte), 8)

    def test_a_non_true_dry_run_value_does_not_count(self):
        # "false" is a truthy string; only JSON true is a dry run.
        self.assertEqual(self.apply(dry_run="false").json()["status"], "applied")


class RestoreTests(GrabTestCase):
    def test_restore_puts_stock_back_and_writes_return_movements(self):
        self.apply(items=[{"barcode": "8812", "qty": 2}, {"barcode": "8813", "qty": 1}])
        res = self.restore()
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["status"], "restored")
        self.assertEqual(
            sorted((l["barcode"], l["qty"], l["stock_after"]) for l in body["lines"]),
            [("8812", 2, 10), ("8813", 1, 5)])
        self.assertEqual(self.stock_of(self.latte), 10)
        self.assertEqual(self.stock_of(self.cake), 5)
        move = StockMovement.objects.get(product=self.latte, type="return")
        self.assertEqual(move.qty, 2)
        self.assertEqual(move.document_no, ORDER)
        self.assertEqual(move.note, "Grab GF-921 cancelled (CANCELLED_MAX)")
        self.assertEqual(move.created_by, "Grab sync")

    def test_restore_goes_through_save_so_it_is_audited(self):
        self.apply()
        AuditLog.objects.all().delete()
        self.restore()
        row = AuditLog.objects.get(model="Product", object_id=str(self.latte.id))
        self.assertEqual(row.changes, {"stock": {"from": 8, "to": 10}})
        self.assertEqual(row.source, "grab-sync")

    def test_a_repeat_restore_is_already_restored(self):
        self.apply()
        self.restore()
        res = self.restore()
        self.assertEqual(res.json()["status"], "already_restored")
        self.assertEqual(self.stock_of(self.latte), 10)
        self.assertEqual(StockMovement.objects.filter(type="return").count(), 1)

    def test_restore_before_apply_is_not_applied(self):
        res = self.restore()
        self.assertEqual(res.json()["status"], "not_applied")
        self.assertEqual(self.stock_of(self.latte), 10)
        self.assertFalse(StockMovement.objects.exists())

    def test_apply_after_restore_is_already_restored_and_deducts_nothing(self):
        self.apply()
        self.restore()
        res = self.apply()
        self.assertEqual(res.json()["status"], "already_restored")
        self.assertEqual(self.stock_of(self.latte), 10)
        self.assertEqual(StockMovement.objects.filter(type="out").count(), 1)

    def test_restore_puts_back_what_was_taken_not_what_the_caller_says(self):
        self.apply(items=[{"barcode": "8812", "qty": 2}])
        res = self.restore(items=[{"barcode": "8812", "qty": 9}])
        self.assertEqual(res.json()["lines"][0]["qty"], 2)
        self.assertEqual(self.stock_of(self.latte), 10)

    def test_restore_keeps_working_after_the_product_was_taken_off_sale(self):
        self.apply()
        Product.objects.filter(pk=self.latte.pk).update(active=False)
        self.assertEqual(self.restore().json()["status"], "restored")
        self.assertEqual(self.stock_of(self.latte), 10)

    def test_restore_only_touches_this_branchs_movements(self):
        other = make_product(self.paragon, name="Latte", stock=7)
        StockMovement.objects.create(
            branch=self.paragon, product=other, type="out", qty=3,
            document_no=ORDER, created_by="Grab sync")
        self.assertEqual(self.restore().json()["status"], "not_applied")
        self.assertEqual(self.stock_of(other), 7)

    def test_one_orders_restore_leaves_another_order_applied(self):
        self.apply()
        self.apply(order_id="OTHER")
        self.restore()
        self.assertEqual(self.stock_of(self.latte), 8)
        self.assertEqual(self.restore(order_id="OTHER").json()["status"], "restored")
        self.assertEqual(self.stock_of(self.latte), 10)

    def test_restore_without_a_reason_has_a_plain_note(self):
        self.apply()
        self.restore(reason="")
        self.assertEqual(StockMovement.objects.get(type="return").note, "Grab GF-921 cancelled")


class StockEndpointTests(GrabTestCase):
    def get(self, merchant=MERCHANT):
        return self.client.get(STOCK, {"merchant_id": merchant}, HTTP_X_API_KEY=KEY)

    def test_returns_active_barcoded_products_of_that_branch_only(self):
        no_barcode = make_product(self.silom, name="Loose", stock=3)
        no_barcode.save()
        off_sale = make_product(self.silom, name="Gone", stock=3)
        off_sale.barcode = "7777"
        off_sale.active = False
        off_sale.save()
        theirs = make_product(self.paragon, name="Elsewhere", stock=9)
        theirs.barcode = "5555"
        theirs.save()

        res = self.get()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {
            "branch": "Silom",
            "products": [
                {"barcode": "8812", "stock": 10, "name": "Latte"},
                {"barcode": "8813", "stock": 5, "name": "Cake"},
            ],
        })

    def test_reflects_an_apply(self):
        self.apply()
        stock = {p["barcode"]: p["stock"] for p in self.get().json()["products"]}
        self.assertEqual(stock["8812"], 8)

    def test_the_slash_spelling_redirects_to_the_declared_url(self):
        """The routes are declared without a slash; SlashTolerantMiddleware
        sends the other spelling there, and a POST keeps its body (308)."""
        res = self.client.get(STOCK + "/", {"merchant_id": MERCHANT}, HTTP_X_API_KEY=KEY)
        self.assertEqual(res.status_code, 301)
        self.assertEqual(res["Location"], STOCK + "?merchant_id=" + MERCHANT)
        res = self.client.post(
            APPLY + "/", {"merchant_id": MERCHANT, "order_id": "SLASH", "items": []},
            content_type="application/json", HTTP_X_API_KEY=KEY, follow=True)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "nothing_to_apply")

    def test_stock_is_read_only(self):
        res = self.post(STOCK, {"merchant_id": MERCHANT})
        self.assertEqual(res.status_code, 405)


class InventoryAsOfTests(GrabTestCase):
    """``_rewind_stock`` works back from today's stock; a Grab order must not
    make the past wrong."""

    def setUp(self):
        super().setUp()
        AuditLog.objects.all().update(at=timezone.now() - timedelta(days=10))
        self.yesterday = timezone.localdate() - timedelta(days=1)

    def rewound(self, product):
        from backoffice.views import _rewind_stock
        product.refresh_from_db()
        kept = _rewind_stock([product], self.yesterday)
        return kept[0].stock

    def test_stock_yesterday_is_before_todays_grab_order(self):
        self.apply()
        self.assertEqual(self.stock_of(self.latte), 8)
        self.assertEqual(self.rewound(self.latte), 10)

    def test_apply_then_restore_rewinds_to_the_same_figure(self):
        self.apply()
        self.restore()
        self.assertEqual(self.rewound(self.latte), 10)

    def test_a_grab_order_from_before_the_day_stays(self):
        self.apply()
        AuditLog.objects.all().update(at=timezone.now() - timedelta(days=3))
        self.assertEqual(self.rewound(self.latte), 8)


# ─── Branch form ─────────────────────────────────────────────────────────────
class BranchFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = "correct-horse-battery"
        cls.admin = Staff(
            name="Grab Admin", username="grabadmin",
            email="grabadmin@therollingpinn.com", role="admin",
            active=True, backoffice_access=True,
        )
        cls.admin.set_password(cls.password)
        cls.admin.save()
        Settings.objects.get_or_create(id="shop")
        cls.silom = Branch.objects.create(name="Grab Silom", grab_merchant_id=MERCHANT)
        cls.emq = Branch.objects.create(name="Grab EMQ", code="EMQ")

    def setUp(self):
        self.assertTrue(self.client.login(username="grabadmin", password=self.password))

    def _form(self, **overrides):
        data = {
            "name": "Grab EMQ", "code": "EMQ", "tax_id": "", "pos_id": "",
            "grab_merchant_id": "",
            "address": "", "phone": "", "logo_url": "",
            "open_time": "09:00", "close_time": "22:00",
            "peak_account_code": "BSV003", "active": "on",
            "payment_mode": "test", "beam_merchant_id": "", "beam_api_key": "",
            "beam_card_fee_percent": "3.65", "omise_public_key": "",
            "omise_secret_key": "", "omise_fee_percent": "3.65",
        }
        data.update(overrides)
        return data

    def _post(self, branch, **overrides):
        return self.client.post(
            reverse("backoffice:branch_detail", args=[branch.id]), self._form(**overrides))

    def test_the_form_has_the_field_after_pos_id(self):
        res = self.client.get(reverse("backoffice:branch_detail", args=[self.silom.id]))
        self.assertContains(res, 'name="grab_merchant_id"')
        self.assertContains(res, f'value="{MERCHANT}"')
        html = res.content.decode()
        self.assertLess(html.index('name="pos_id"'), html.index('name="grab_merchant_id"'))

    def test_the_field_saves_stripped_and_upper_cased(self):
        self._post(self.emq, grab_merchant_id="  3-c7mct7baej2zgx ")
        self.emq.refresh_from_db()
        self.assertEqual(self.emq.grab_merchant_id, "3-C7MCT7BAEJ2ZGX")

    def test_clearing_the_field_unlinks_the_branch(self):
        self._post(self.silom, name="Grab Silom", code="", grab_merchant_id="")
        self.silom.refresh_from_db()
        self.assertEqual(self.silom.grab_merchant_id, "")

    def test_its_own_id_still_saves(self):
        self._post(self.silom, name="Grab Silom", code="", grab_merchant_id=MERCHANT,
                   phone="0644184887")
        self.silom.refresh_from_db()
        self.assertEqual(self.silom.phone, "0644184887")
        self.assertEqual(self.silom.grab_merchant_id, MERCHANT)

    def test_a_duplicate_shows_the_form_error_not_a_500(self):
        res = self._post(self.emq, grab_merchant_id=MERCHANT.lower(), phone="021234567")
        self.assertEqual(res.status_code, 200, "a clash must not redirect")
        self.assertContains(res, "already belongs to Grab Silom")
        self.emq.refresh_from_db()
        self.assertEqual(self.emq.grab_merchant_id, "")
        self.assertContains(res, "021234567")

    def test_a_new_branch_cannot_claim_a_taken_id(self):
        res = self.client.post(
            reverse("backoffice:branch_new"),
            self._form(name="Grab Siam", code="SPG", grab_merchant_id=MERCHANT))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "already belongs to Grab Silom")
        self.assertFalse(Branch.objects.filter(name="Grab Siam").exists())

    def test_any_number_of_branches_may_have_none(self):
        Branch.objects.create(name="Grab None 1")
        Branch.objects.create(name="Grab None 2")
        self.assertEqual(
            Branch.objects.filter(grab_merchant_id="", name__startswith="Grab ").count(), 3)

    def test_the_database_refuses_a_second_branch_with_the_same_id(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Branch.objects.create(name="Grab Clash", grab_merchant_id=MERCHANT)


# ─── Concurrency ─────────────────────────────────────────────────────────────
# SQLite ignores SELECT ... FOR UPDATE, so this only means something on
# PostgreSQL — see the caveat in settings_test.py.
@skipUnless(
    connection.vendor == "postgresql",
    "concurrency tests require PostgreSQL row locking (SELECT ... FOR UPDATE)",
)
class ConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        patcher = sync_env()
        patcher.start()
        self.addCleanup(patcher.stop)
        self.branch = make_branch(name="Silom")
        self.branch.grab_merchant_id = MERCHANT
        self.branch.save()
        self.latte = make_product(self.branch, name="Latte", stock=10)
        self.latte.barcode = "8812"
        self.latte.save()

    def test_a_double_apply_takes_stock_out_once(self):
        results = []
        items = [{"barcode": "8812", "qty": 2, "grab_item_id": ""}]

        def call():
            results.append(
                grab_sync.apply_order(self.branch, ORDER, "GF-921", items)["status"])

        errors = run_concurrently([call, call, call])
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), ["already_applied", "already_applied", "applied"])
        self.latte.refresh_from_db()
        self.assertEqual(self.latte.stock, 8)
        self.assertEqual(StockMovement.objects.filter(document_no=ORDER).count(), 1)
        connections.close_all()
