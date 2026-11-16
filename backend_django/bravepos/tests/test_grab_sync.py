"""Grab stock sync, BravePOS half: hold / convert / release / restore / correct /
review / heartbeat / stock endpoints.

A poller outside this repo calls these with an API key.  What matters is that a
Grab order is held from the moment Grab lowers its count, takes stock out exactly
once however often it is sent, that a cancelled one puts it back exactly once and
is never deducted again, that an edit writes only the difference, that the
"inventory as of" report still adds up afterwards, and that nothing changes
unless the write switch is on.

Run:
    python manage.py test bravepos.tests.test_grab_sync \\
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

import os
import threading
from datetime import timedelta
from unittest import mock, skipUnless

from django.db import IntegrityError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from bravepos import grab_sync
from bravepos.models import (
    AuditLog, Branch, GrabOrderHold, GrabSyncStatus, Product, Settings, Staff, StockMovement,
)

from .factories import make_branch, make_product, make_shop
from .test_selforder import run_concurrently

KEY = "test-grab-key"
MERCHANT = "3-C2M2RUXENZCZGT"
ORDER = "001553556136-C8LJEFNGJPAVCA"

HOLD = "/api/grab/orders/hold"
CONVERT = "/api/grab/orders/convert"
RELEASE = "/api/grab/orders/release"
RESTORE = "/api/grab/orders/restore"
CORRECT = "/api/grab/orders/correct"
REVIEW = "/api/grab/orders/review"
HEARTBEAT = "/api/grab/heartbeat"
STOCK = "/api/grab/stock"
LATTE_LINE = {"barcode": "8812", "qty": 2, "grab_item_id": "ITEM1", "name": "Latte"}


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

    def convert(self, items=None, **extra):
        body = {
            "merchant_id": MERCHANT, "order_id": ORDER, "display_id": "GF-921",
            "items": items if items is not None else [
                {"barcode": "8812", "qty": 2, "grab_item_id": "ITEM1", "name": "Latte"}],
        }
        body.update(extra)
        return self.post(CONVERT, body)

    def restore(self, **extra):
        body = {"merchant_id": MERCHANT, "order_id": ORDER, "display_id": "GF-921",
                "reason": "CANCELLED_MAX"}
        body.update(extra)
        return self.post(RESTORE, body)

    def hold(self, items=None, order_id=ORDER, **extra):
        order = {
            "order_id": order_id, "display_id": "GF-921", "grab_state": "PreparingV2",
            "placed_at": "2026-11-16T10:00:00Z",
            "items": items if items is not None else [dict(LATTE_LINE)],
        }
        order.update(extra)
        return self.post(HOLD, {"merchant_id": MERCHANT, "orders": [order]})

    def release(self, **extra):
        body = {"merchant_id": MERCHANT, "order_id": ORDER, "reason": "CANCELLED_MAX"}
        body.update(extra)
        return self.post(RELEASE, body)

    def correct(self, items, **extra):
        body = {"merchant_id": MERCHANT, "order_id": ORDER, "display_id": "GF-921",
                "items": items}
        body.update(extra)
        return self.post(CORRECT, body)

    def review(self, reason="unclear_pickup", **extra):
        body = {"merchant_id": MERCHANT, "order_id": ORDER, "display_id": "GF-921",
                "reason": reason, "detail": {"seen": "CANCELLED"}}
        body.update(extra)
        return self.post(REVIEW, body)

    def beat(self, **extra):
        body = {"merchant_id": MERCHANT, "cycle_ok": True, "push_live": True,
                "problem": "", "grab_shown": {"8812": 4}}
        body.update(extra)
        return self.post(HEARTBEAT, body)

    def row(self, order_id=ORDER):
        return GrabOrderHold.objects.get(branch=self.silom, order_id=order_id)

    def stock_of(self, product):
        product.refresh_from_db()
        return product.stock


class AuthTests(GrabTestCase):
    def test_missing_key_is_401(self):
        for url, body in ((HOLD, {}), (CONVERT, {}), (RELEASE, {}), (RESTORE, {}),
                          (CORRECT, {}), (REVIEW, {}), (HEARTBEAT, {})):
            self.assertEqual(self.post(url, body, key=None).status_code, 401, url)
        self.assertEqual(self.client.get(STOCK, {"merchant_id": MERCHANT}).status_code, 401)

    def test_wrong_key_is_401_and_changes_nothing(self):
        res = self.post(CONVERT, {"merchant_id": MERCHANT, "order_id": ORDER,
                                "items": [{"barcode": "8812", "qty": 1}]}, key="nope")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(self.stock_of(self.latte), 10)
        res = self.client.get(STOCK, {"merchant_id": MERCHANT}, HTTP_X_API_KEY="nope")
        self.assertEqual(res.status_code, 401)

    def test_non_ascii_key_is_401_not_a_500(self):
        self.assertEqual(self.post(CONVERT, {}, key="clé").status_code, 401)

    def test_unset_key_env_is_503_for_every_call(self):
        with mock.patch.dict(os.environ, {grab_sync.API_KEY_ENV: ""}):
            self.assertEqual(self.convert().status_code, 503)
            self.assertEqual(self.restore().status_code, 503)
            for call in (self.hold, self.release, self.review, self.beat):
                self.assertEqual(call().status_code, 503, call.__name__)
            self.assertEqual(self.correct([dict(LATTE_LINE)]).status_code, 503)
            self.assertEqual(
                self.client.get(STOCK, {"merchant_id": MERCHANT}, HTTP_X_API_KEY=KEY).status_code,
                503)
            # Even the right-looking empty key does not get in.
            self.assertEqual(self.post(CONVERT, {}, key="").status_code, 503)

    def test_unknown_merchant_is_404(self):
        self.assertEqual(self.convert(merchant_id="3-UNKNOWN").status_code, 404)
        self.assertEqual(self.restore(merchant_id="3-UNKNOWN").status_code, 404)
        self.assertEqual(self.release(merchant_id="3-UNKNOWN").status_code, 404)
        self.assertEqual(self.review(merchant_id="3-UNKNOWN").status_code, 404)
        self.assertEqual(self.beat(merchant_id="3-UNKNOWN").status_code, 404)
        self.assertEqual(self.correct([dict(LATTE_LINE)], merchant_id="3-UNKNOWN").status_code, 404)
        res = self.post(HOLD, {"merchant_id": "3-UNKNOWN", "orders": []})
        self.assertEqual(res.status_code, 404)
        res = self.client.get(STOCK, {"merchant_id": "3-UNKNOWN"}, HTTP_X_API_KEY=KEY)
        self.assertEqual(res.status_code, 404)

    def test_inactive_branch_is_404(self):
        Branch.objects.filter(pk=self.silom.pk).update(active=False)
        self.assertEqual(self.convert().status_code, 404)
        self.assertEqual(self.restore().status_code, 404)
        res = self.client.get(STOCK, {"merchant_id": MERCHANT}, HTTP_X_API_KEY=KEY)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(self.stock_of(self.latte), 10)

    def test_a_blank_merchant_id_is_400_and_never_matches_an_unsynced_branch(self):
        # paragon has grab_merchant_id "" — a blank lookup must not find it.
        self.assertEqual(self.convert(merchant_id="").status_code, 400)
        self.assertEqual(self.convert(merchant_id="  ").status_code, 400)
        res = self.client.get(STOCK, HTTP_X_API_KEY=KEY)
        self.assertEqual(res.status_code, 400)

    def test_merchant_id_is_matched_case_insensitively(self):
        self.assertEqual(self.convert(merchant_id=MERCHANT.lower()).status_code, 200)

    def test_bad_bodies_are_400(self):
        self.assertEqual(self.convert(order_id="").status_code, 400)
        self.assertEqual(self.convert(order_id="x" * 65).status_code, 400)
        self.assertEqual(self.convert(items="8812").status_code, 400)
        self.assertEqual(self.convert(items=[{"barcode": "8812", "qty": 0}]).status_code, 400)
        self.assertEqual(self.convert(items=[{"barcode": "8812", "qty": "2"}]).status_code, 400)
        self.assertEqual(self.convert(items=[{"barcode": "8812", "qty": True}]).status_code, 400)
        self.assertEqual(self.post(CONVERT, [1, 2]).status_code, 400)
        self.assertEqual(self.stock_of(self.latte), 10)


class ConvertTests(GrabTestCase):
    def test_convert_takes_stock_out_and_writes_the_movement(self):
        res = self.convert()
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["status"], "converted")
        self.assertEqual(body["lines"], [{
            "barcode": "8812", "product_id": str(self.latte.id),
            "qty": 2, "stock_after": 8, "skipped_reason": None,
        }])
        self.assertEqual(self.stock_of(self.latte), 8)
        move = StockMovement.objects.get(product=self.latte)
        self.assertEqual(move.type, "out")
        self.assertEqual(move.qty, 2)
        self.assertEqual(move.document_no, ORDER)
        self.assertEqual(move.note, "Grab GF-921")
        self.assertEqual(move.created_by, "Grab sync")
        self.assertEqual(move.branch, self.silom)

    def test_convert_writes_an_audit_row_with_the_stock_change(self):
        AuditLog.objects.all().delete()
        self.convert()
        rows = AuditLog.objects.filter(model="Product", object_id=str(self.latte.id))
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.action, "update")
        self.assertEqual(row.changes, {"stock": {"from": 10, "to": 8}})
        self.assertEqual(row.source, "grab-sync")
        self.assertEqual(row.branch_id, self.silom.id)

    def test_a_repeat_is_already_converted_and_stock_is_unchanged(self):
        self.convert()
        res = self.convert()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "already_converted")
        self.assertEqual(self.stock_of(self.latte), 8)
        self.assertEqual(StockMovement.objects.filter(document_no=ORDER).count(), 1)

    def test_a_repeat_with_different_items_still_does_nothing(self):
        self.convert()
        res = self.convert(items=[{"barcode": "8813", "qty": 4}])
        self.assertEqual(res.json()["status"], "already_converted")
        self.assertEqual(self.stock_of(self.cake), 5)

    def test_two_orders_with_the_same_display_id_are_separate(self):
        self.convert()
        res = self.convert(order_id="OTHER-ORDER")      # same GF-921
        self.assertEqual(res.json()["status"], "converted")
        self.assertEqual(self.stock_of(self.latte), 6)

    def test_duplicate_barcodes_are_merged(self):
        res = self.convert(items=[
            {"barcode": "8812", "qty": 1, "grab_item_id": "A"},
            {"barcode": "8812", "qty": 2, "grab_item_id": "B"},
        ])
        body = res.json()
        self.assertEqual(len(body["lines"]), 1)
        self.assertEqual(body["lines"][0]["qty"], 3)
        self.assertEqual(self.stock_of(self.latte), 7)
        self.assertEqual(StockMovement.objects.filter(document_no=ORDER).count(), 1)

    def test_several_products_in_one_order(self):
        res = self.convert(items=[{"barcode": "8812", "qty": 1}, {"barcode": "8813", "qty": 2}])
        self.assertEqual(res.json()["status"], "converted")
        self.assertEqual(self.stock_of(self.latte), 9)
        self.assertEqual(self.stock_of(self.cake), 3)
        self.assertEqual(StockMovement.objects.filter(document_no=ORDER).count(), 2)

    def test_unknown_barcodes_are_skipped_and_the_rest_converted(self):
        res = self.convert(items=[
            {"barcode": "8812", "qty": 1},
            {"barcode": "9001", "qty": 1, "grab_item_id": "GONE"},
            {"barcode": "", "qty": 1, "grab_item_id": "NOCODE"},
        ])
        body = res.json()
        self.assertEqual(body["status"], "converted")
        self.assertEqual(
            sorted((l["barcode"], l["skipped_reason"] or "") for l in body["lines"]),
            [("", "no_barcode"), ("8812", ""), ("9001", "not_at_branch")])
        self.assertEqual(self.row().review_reason, "unmapped_item")
        self.assertEqual(self.stock_of(self.latte), 9)

    def test_an_inactive_product_is_skipped(self):
        Product.objects.filter(pk=self.latte.pk).update(active=False)
        res = self.convert()
        body = res.json()
        self.assertEqual(body["status"], "nothing_to_convert")
        self.assertEqual(body["lines"][0]["skipped_reason"], "not_at_branch")
        self.assertEqual(self.stock_of(self.latte), 10)
        self.assertFalse(StockMovement.objects.exists())

    def test_nothing_to_convert_writes_nothing_and_is_not_a_ledger_entry(self):
        res = self.convert(items=[{"barcode": "9001", "qty": 1}])
        self.assertEqual(res.json()["status"], "nothing_to_convert")
        self.assertFalse(StockMovement.objects.exists())
        # So once the product exists, the same order can still be applied.
        other = make_product(self.silom, name="Late arrival", stock=4)
        other.barcode = "9001"
        other.save()
        res = self.convert(items=[{"barcode": "9001", "qty": 1}])
        self.assertEqual(res.json()["status"], "converted")

    def test_an_empty_order_is_nothing_to_convert(self):
        self.assertEqual(self.convert(items=[]).json()["status"], "nothing_to_convert")

    def open_order_ids(self):
        res = self.client.get(STOCK, {"merchant_id": MERCHANT}, HTTP_X_API_KEY=KEY)
        return [o["order_id"] for o in res.json()["open_orders"]]

    def test_a_convert_with_no_hold_and_nothing_mappable_leaves_no_held_row(self):
        for items in ([], [{"barcode": "9001", "qty": 1}]):
            order_id = f"NOHOLD{len(items)}"
            res = self.convert(order_id=order_id, items=items)
            self.assertEqual(res.json()["status"], "nothing_to_convert")
            self.assertNotIn(order_id, self.open_order_ids())
            row = self.row(order_id)
            self.assertNotEqual(row.status, "held")
            self.assertEqual(self.stock_of(self.latte), 10)
        self.assertFalse(StockMovement.objects.exists())

    def test_the_unmapped_review_survives_on_the_closed_row(self):
        self.convert(items=[{"barcode": "9001", "qty": 1}])
        row = self.row()
        self.assertEqual((row.status, row.review_status, row.review_reason),
                         ("released", "open", "unmapped_item"))

    def test_a_row_closed_for_having_nothing_can_still_be_converted_later(self):
        self.convert(items=[{"barcode": "9001", "qty": 1}])
        self.assertEqual(self.row().close_reason, "nothing_to_convert")
        other = make_product(self.silom, name="Late arrival", stock=4)
        other.barcode = "9001"
        other.save()
        self.assertEqual(self.convert(items=[{"barcode": "9001", "qty": 1}]).json()["status"],
                         "converted")
        row = self.row()
        self.assertEqual((row.status, row.close_reason, row.closed_at), ("converted", "", None))
        self.assertEqual(self.stock_of(other), 3)

    def test_a_hold_that_has_items_stays_held_when_convert_maps_nothing(self):
        self.hold()
        self.convert(items=[{"barcode": "9001", "qty": 1}])
        self.assertEqual(self.row().status, "held")

    def test_another_branchs_product_with_the_same_barcode_is_untouched(self):
        theirs = make_product(self.paragon, name="Latte", stock=20)
        theirs.barcode = "8812"
        theirs.save()
        self.convert()
        self.assertEqual(self.stock_of(theirs), 20)
        self.assertEqual(self.stock_of(self.latte), 8)
        self.assertFalse(StockMovement.objects.filter(product=theirs).exists())

    def test_stock_may_go_negative(self):
        res = self.convert(items=[{"barcode": "8813", "qty": 8}])
        self.assertEqual(res.json()["lines"][0]["stock_after"], -3)
        self.assertEqual(self.stock_of(self.cake), -3)

    def test_a_blank_display_id_still_applies(self):
        res = self.convert(display_id="")
        self.assertEqual(res.json()["status"], "converted")
        self.assertEqual(StockMovement.objects.get().note, "Grab")


class DryRunTests(GrabTestCase):
    def assert_untouched(self):
        self.assertEqual(self.stock_of(self.latte), 10)
        self.assertFalse(StockMovement.objects.exists())

    def test_dry_run_in_the_body_changes_nothing_and_returns_the_lines(self):
        AuditLog.objects.all().delete()
        res = self.convert(dry_run=True)
        body = res.json()
        self.assertEqual(body["status"], "dry_run")
        self.assertEqual(body["lines"][0]["stock_after"], 8)
        self.assert_untouched()
        self.assertFalse(AuditLog.objects.exists())

    def test_writes_off_is_a_dry_run_even_without_the_flag(self):
        with sync_env(writes=False):
            res = self.convert()
            self.assertEqual(res.json()["status"], "dry_run")
            self.assertEqual(res.json()["lines"][0]["stock_after"], 8)
            self.assert_untouched()
        # Only the exact string 1 turns writes on.
        with sync_env(writes=False), mock.patch.dict(os.environ, {grab_sync.WRITES_ENV: "true"}):
            self.assertEqual(self.convert().json()["status"], "dry_run")
        self.assert_untouched()

    def test_dry_run_still_reports_skipped_items(self):
        res = self.convert(dry_run=True, items=[{"barcode": "9001", "qty": 1}])
        self.assertEqual(res.json()["status"], "nothing_to_convert")
        self.assertEqual(res.json()["lines"][0]["skipped_reason"], "not_at_branch")

    def test_dry_run_of_an_order_already_converted_reports_the_ledger(self):
        self.convert()
        res = self.convert(dry_run=True)
        self.assertEqual(res.json()["status"], "already_converted")
        self.assertEqual(self.stock_of(self.latte), 8)

    def test_dry_run_does_not_mark_the_order_so_a_live_call_still_applies(self):
        self.convert(dry_run=True)
        self.assertEqual(self.convert().json()["status"], "converted")
        self.assertEqual(self.stock_of(self.latte), 8)

    def test_restore_dry_run_changes_nothing(self):
        self.convert()
        res = self.restore(dry_run=True)
        self.assertEqual(res.json()["status"], "dry_run")
        self.assertEqual(res.json()["lines"][0]["stock_after"], 10)
        self.assertEqual(self.stock_of(self.latte), 8)
        self.assertFalse(StockMovement.objects.filter(type="return").exists())

    def test_restore_with_writes_off_changes_nothing(self):
        self.convert()
        with sync_env(writes=False):
            self.assertEqual(self.restore().json()["status"], "dry_run")
        self.assertEqual(self.stock_of(self.latte), 8)

    def test_a_non_true_dry_run_value_does_not_count(self):
        # "false" is a truthy string; only JSON true is a dry run.
        self.assertEqual(self.convert(dry_run="false").json()["status"], "converted")


class RestoreTests(GrabTestCase):
    def test_restore_puts_stock_back_and_writes_return_movements(self):
        self.convert(items=[{"barcode": "8812", "qty": 2}, {"barcode": "8813", "qty": 1}])
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
        self.convert()
        AuditLog.objects.all().delete()
        self.restore()
        row = AuditLog.objects.get(model="Product", object_id=str(self.latte.id))
        self.assertEqual(row.changes, {"stock": {"from": 8, "to": 10}})
        self.assertEqual(row.source, "grab-sync")

    def test_a_repeat_restore_is_already_restored(self):
        self.convert()
        self.restore()
        res = self.restore()
        self.assertEqual(res.json()["status"], "already_restored")
        self.assertEqual(self.stock_of(self.latte), 10)
        self.assertEqual(StockMovement.objects.filter(type="return").count(), 1)

    def test_restore_before_convert_is_not_converted(self):
        res = self.restore()
        self.assertEqual(res.json()["status"], "not_converted")
        self.assertEqual(self.stock_of(self.latte), 10)
        self.assertFalse(StockMovement.objects.exists())

    def test_convert_after_restore_is_already_restored_and_deducts_nothing(self):
        self.convert()
        self.restore()
        res = self.convert()
        self.assertEqual(res.json()["status"], "already_restored")
        self.assertEqual(self.stock_of(self.latte), 10)
        self.assertEqual(StockMovement.objects.filter(type="out").count(), 1)

    def test_restore_puts_back_what_was_taken_not_what_the_caller_says(self):
        self.convert(items=[{"barcode": "8812", "qty": 2}])
        res = self.restore(items=[{"barcode": "8812", "qty": 9}])
        self.assertEqual(res.json()["lines"][0]["qty"], 2)
        self.assertEqual(self.stock_of(self.latte), 10)

    def test_restore_keeps_working_after_the_product_was_taken_off_sale(self):
        self.convert()
        Product.objects.filter(pk=self.latte.pk).update(active=False)
        self.assertEqual(self.restore().json()["status"], "restored")
        self.assertEqual(self.stock_of(self.latte), 10)

    def test_restore_only_touches_this_branchs_movements(self):
        other = make_product(self.paragon, name="Latte", stock=7)
        StockMovement.objects.create(
            branch=self.paragon, product=other, type="out", qty=3,
            document_no=ORDER, created_by="Grab sync")
        self.assertEqual(self.restore().json()["status"], "not_converted")
        self.assertEqual(self.stock_of(other), 7)

    def test_one_orders_restore_leaves_another_order_converted(self):
        self.convert()
        self.convert(order_id="OTHER")
        self.restore()
        self.assertEqual(self.stock_of(self.latte), 8)
        self.assertEqual(self.restore(order_id="OTHER").json()["status"], "restored")
        self.assertEqual(self.stock_of(self.latte), 10)

    def test_restore_without_a_reason_has_a_plain_note(self):
        self.convert()
        self.restore(reason="")
        self.assertEqual(StockMovement.objects.get(type="return").note, "Grab GF-921 cancelled")


class HoldTests(GrabTestCase):
    def test_hold_creates_a_held_row_with_the_mapped_items(self):
        res = self.hold()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"results": [{
            "order_id": ORDER, "status": "held", "created": True, "skipped": []}],
            "invalid": []})
        row = self.row()
        self.assertEqual(row.status, "held")
        self.assertEqual(row.display_id, "GF-921")
        self.assertEqual(row.grab_state, "PreparingV2")
        self.assertEqual(row.items, [{
            "product_id": str(self.latte.id), "barcode": "8812", "qty": 2,
            "grab_item_id": "ITEM1", "name": "Latte", "bundle_item_id": ""}])
        self.assertIsNotNone(row.placed_at)
        # Held, not deducted: the shelf is untouched.
        self.assertEqual(self.stock_of(self.latte), 10)
        self.assertFalse(StockMovement.objects.exists())

    def test_a_repeat_is_one_row_and_not_created_again(self):
        self.hold()
        res = self.hold()
        self.assertFalse(res.json()["results"][0]["created"])
        self.assertEqual(res.json()["results"][0]["status"], "held")
        self.assertEqual(GrabOrderHold.objects.count(), 1)

    def test_an_edit_while_held_updates_the_items_and_moves_no_stock(self):
        self.hold()
        self.hold(items=[{"barcode": "8812", "qty": 3}, {"barcode": "8813", "qty": 1}],
                  grab_state="Upcoming")
        row = self.row()
        self.assertEqual({i["barcode"]: i["qty"] for i in row.items}, {"8812": 3, "8813": 1})
        self.assertEqual(row.grab_state, "Upcoming")
        self.assertEqual(self.stock_of(self.latte), 10)
        self.assertFalse(StockMovement.objects.exists())

    def test_nothing_changes_after_convert_or_release(self):
        self.hold()
        self.convert()
        res = self.hold(items=[{"barcode": "8813", "qty": 4}])
        self.assertEqual(res.json()["results"][0]["status"], "converted")
        self.assertEqual(self.row().items[0]["barcode"], "8812")
        self.hold(order_id="OTHER")
        self.release(order_id="OTHER")
        res = self.hold(order_id="OTHER", items=[{"barcode": "8813", "qty": 4}])
        self.assertEqual(res.json()["results"][0]["status"], "released")
        self.assertEqual(self.row("OTHER").status, "released")

    def test_skipped_lines_are_reported_and_open_a_review(self):
        res = self.hold(items=[dict(LATTE_LINE), {"barcode": "9001", "qty": 1},
                               {"barcode": "", "qty": 1, "grab_item_id": "NOCODE"}])
        skipped = res.json()["results"][0]["skipped"]
        self.assertEqual(skipped, [{"barcode": "9001", "reason": "not_at_branch"},
                                   {"barcode": "", "reason": "no_barcode"}])
        row = self.row()
        self.assertEqual((row.review_status, row.review_reason), ("open", "unmapped_item"))
        self.assertEqual(len(row.items), 1)          # the mapped line is still held

    def test_an_order_with_no_lines_is_kept_and_holds_nothing(self):
        res = self.hold(items=[])
        self.assertTrue(res.json()["results"][0]["created"])
        self.assertEqual(self.row().items, [])
        self.assertEqual(self.row().review_status, "")      # the poller calls review

    def test_a_batch_holds_every_order(self):
        res = self.post(HOLD, {"merchant_id": MERCHANT, "orders": [
            {"order_id": "A", "items": [{"barcode": "8812", "qty": 1}]},
            {"order_id": "B", "items": [{"barcode": "8813", "qty": 2}]}]})
        self.assertEqual([r["order_id"] for r in res.json()["results"]], ["A", "B"])
        self.assertEqual(GrabOrderHold.objects.count(), 2)

    def test_dry_run_and_writes_off_write_nothing(self):
        res = self.post(HOLD, {"merchant_id": MERCHANT, "dry_run": True,
                               "orders": [{"order_id": ORDER, "items": [dict(LATTE_LINE)]}]})
        self.assertTrue(res.json()["results"][0]["created"])
        with sync_env(writes=False):
            self.hold(order_id="X")
        self.assertFalse(GrabOrderHold.objects.exists())

    def test_bad_bodies_are_400(self):
        # Only a fault in the call as a whole rejects it.
        self.assertEqual(self.post(HOLD, {"merchant_id": MERCHANT, "orders": "x"}).status_code, 400)
        self.assertEqual(self.post(HOLD, {"merchant_id": "", "orders": []}).status_code, 400)
        self.assertEqual(self.post(HOLD, [1]).status_code, 400)

    def test_a_display_id_up_to_64_characters_is_held(self):
        res = self.hold(display_id="G" * 64)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["invalid"], [])
        self.assertEqual(self.row().display_id, "G" * 64)

    def test_one_bad_order_does_not_reject_the_batch(self):
        good = {"order_id": "A", "items": [{"barcode": "8812", "qty": 1}]}
        res = self.post(HOLD, {"merchant_id": MERCHANT, "orders": [
            good,
            {"order_id": "B", "items": [{"barcode": "8812", "qty": 0}]},        # bad qty
            {"order_id": "C", "display_id": "x" * 65, "items": []},              # too long
            {"order_id": "", "items": []},                                       # no id
            "not an object",
            {"order_id": "A", "items": []},                                      # repeated id
            {"order_id": "D", "placed_at": "soon", "items": []},                 # bad time
            {"order_id": "E", "items": [{"barcode": "8813", "qty": 2}]},
        ]})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual([r["order_id"] for r in body["results"]], ["A", "E"])
        self.assertEqual([(i["index"], i["order_id"]) for i in body["invalid"]],
                         [(1, "B"), (2, "C"), (3, ""), (4, ""), (5, "A"), (6, "D")])
        self.assertTrue(all(i["detail"] for i in body["invalid"]))
        self.assertEqual(sorted(GrabOrderHold.objects.values_list("order_id", flat=True)),
                         ["A", "E"])
        # The first A was held with its own lines, not the repeat's empty ones.
        self.assertEqual(len(GrabOrderHold.objects.get(order_id="A").items), 1)

    def test_the_invalid_entry_has_a_fixed_shape_and_a_whole_bad_batch_is_still_200(self):
        res = self.post(HOLD, {"merchant_id": MERCHANT, "orders": [
            {"order_id": "B", "items": [{"barcode": "8812", "qty": 0}]}]})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["results"], [])
        (entry,) = body["invalid"]
        self.assertEqual(set(entry), {"index", "order_id", "detail"})
        self.assertEqual((entry["index"], entry["order_id"]), (0, "B"))
        self.assertIsInstance(entry["detail"], str)

    def test_dry_run_also_reports_invalid_orders(self):
        res = self.post(HOLD, {"merchant_id": MERCHANT, "dry_run": True, "orders": [
            {"order_id": "A", "items": []}, {"order_id": "B", "display_id": "x" * 65}]})
        body = res.json()
        self.assertEqual([r["order_id"] for r in body["results"]], ["A"])
        self.assertEqual([i["order_id"] for i in body["invalid"]], ["B"])
        self.assertFalse(GrabOrderHold.objects.exists())

    def test_convert_takes_a_64_character_display_id_too(self):
        self.assertEqual(self.convert(display_id="G" * 64).status_code, 200)
        self.assertEqual(self.convert(order_id="Z", display_id="G" * 65).status_code, 400)

    def test_a_held_row_of_a_branch_without_a_merchant_id_is_ignored(self):
        self.hold()
        self.silom.grab_merchant_id = ""
        self.silom.save()
        from bravepos import stock_pool
        self.assertEqual(stock_pool.grab_held(self.silom), {})


class ConvertHoldTests(GrabTestCase):
    def test_convert_ends_the_hold_and_deducts_in_one_step(self):
        self.hold()
        res = self.convert(ready_at="2026-11-16T10:20:00Z")
        self.assertEqual(res.json()["status"], "converted")
        row = self.row()
        self.assertEqual(row.status, "converted")
        self.assertEqual(row.deducted, {str(self.latte.id): 2})
        self.assertEqual(self.stock_of(self.latte), 8)
        from bravepos import stock_pool
        self.assertEqual(stock_pool.grab_held(self.silom), {})    # never counted twice

    def test_a_repeat_is_already_converted_with_the_lines(self):
        self.hold()
        self.convert()
        res = self.convert()
        self.assertEqual(res.json()["status"], "already_converted")
        self.assertEqual(res.json()["lines"][0]["qty"], 2)
        self.assertEqual(self.stock_of(self.latte), 8)

    def test_with_no_hold_it_creates_the_row_and_converts_at_once(self):
        self.assertEqual(self.convert().json()["status"], "converted")
        self.assertEqual(self.row().status, "converted")

    def test_with_no_items_it_uses_the_hold_items(self):
        self.hold()
        res = self.post(CONVERT, {"merchant_id": MERCHANT, "order_id": ORDER, "display_id": "GF-921"})
        self.assertEqual(res.json()["status"], "converted")
        self.assertEqual(self.stock_of(self.latte), 8)

    def test_the_ready_time_re_read_decides_what_is_deducted(self):
        self.hold()                                            # held 2 lattes
        self.convert(items=[{"barcode": "8812", "qty": 3}])      # edited to 3 before Ready
        self.assertEqual(self.stock_of(self.latte), 7)

    def test_released_orders_are_not_converted(self):
        self.hold()
        self.release()
        self.assertEqual(self.convert().json()["status"], "released_earlier")
        self.assertEqual(self.stock_of(self.latte), 10)

    def test_since_after_ready_closes_it_with_no_movements(self):
        res = self.convert(ready_at="2026-11-16T09:00:00Z", since="2026-11-16T10:00:00Z")
        self.assertEqual(res.json()["status"], "converted")
        self.assertEqual(res.json()["lines"][0]["skipped_reason"], "before_go_live")
        row = self.row()
        self.assertEqual((row.status, row.close_reason), ("converted", "before_go_live"))
        self.assertEqual(self.stock_of(self.latte), 10)
        self.assertFalse(StockMovement.objects.exists())

    def test_without_since_there_is_no_go_live_check(self):
        self.convert(ready_at="2000-01-01T00:00:00Z")
        self.assertEqual(self.stock_of(self.latte), 8)

    def test_ready_after_since_deducts_normally(self):
        self.convert(ready_at="2026-11-16T11:00:00Z", since="2026-11-16T10:00:00Z")
        self.assertEqual(self.stock_of(self.latte), 8)

    def test_a_before_go_live_hold_is_closed_and_no_longer_held(self):
        self.hold()
        self.convert(ready_at="2026-11-16T09:00:00Z", since="2026-11-16T10:00:00Z")
        from bravepos import stock_pool
        self.assertEqual(stock_pool.grab_held(self.silom), {})

    def test_a_stock_count_after_packing_means_the_line_is_not_deducted(self):
        ready = timezone.now() - timedelta(minutes=10)
        StockMovement.objects.create(branch=self.silom, product=self.latte, type="adjust",
                                     qty=7, note="count")
        res = self.convert(ready_at=ready.isoformat(),
                           items=[dict(LATTE_LINE), {"barcode": "8813", "qty": 1}])
        lines = {l["barcode"]: l for l in res.json()["lines"]}
        self.assertEqual(lines["8812"]["skipped_reason"], "counted_after_packing")
        self.assertIsNone(lines["8813"]["skipped_reason"])
        self.assertEqual(self.stock_of(self.latte), 10)
        self.assertEqual(self.stock_of(self.cake), 4)

    def test_a_count_before_packing_does_not_block(self):
        StockMovement.objects.create(branch=self.silom, product=self.latte, type="adjust",
                                     qty=7, note="count")
        ready = timezone.now() + timedelta(minutes=1)
        self.convert(ready_at=ready.isoformat())
        self.assertEqual(self.stock_of(self.latte), 8)

    def test_unmapped_lines_on_convert_open_a_review(self):
        self.convert(items=[dict(LATTE_LINE), {"barcode": "9001", "qty": 1}])
        self.assertEqual(self.row().review_reason, "unmapped_item")

    def test_dry_run_leaves_the_hold_alone(self):
        self.hold()
        self.assertEqual(self.convert(dry_run=True).json()["status"], "dry_run")
        self.assertEqual(self.row().status, "held")
        self.assertEqual(self.stock_of(self.latte), 10)

    def test_ready_at_must_be_a_time(self):
        self.assertEqual(self.convert(ready_at="soon").status_code, 400)


class ReleaseTests(GrabTestCase):
    def test_release_drops_the_hold_and_moves_no_stock(self):
        self.hold()
        res = self.release()
        self.assertEqual(res.json(), {"status": "released"})
        row = self.row()
        self.assertEqual((row.status, row.close_reason), ("released", "CANCELLED_MAX"))
        self.assertEqual(self.stock_of(self.latte), 10)
        self.assertFalse(StockMovement.objects.exists())

    def test_a_repeat_is_already_released(self):
        self.hold()
        self.release()
        self.assertEqual(self.release().json(), {"status": "already_released"})

    def test_a_converted_order_is_left_for_restore(self):
        self.hold()
        self.convert()
        self.assertEqual(self.release().json(), {"status": "converted"})
        self.assertEqual(self.row().status, "converted")
        self.assertEqual(self.stock_of(self.latte), 8)

    def test_an_unknown_order_is_unknown(self):
        self.assertEqual(self.release().json(), {"status": "unknown"})

    def test_dry_run_does_not_release(self):
        self.hold()
        self.assertEqual(self.release(dry_run=True).json(), {"status": "released"})
        self.assertEqual(self.row().status, "held")


class RestoreHoldTests(GrabTestCase):
    def test_restore_of_a_held_order_is_not_converted(self):
        self.hold()
        self.assertEqual(self.restore().json()["status"], "not_converted")
        self.assertEqual(self.row().status, "held")

    def test_restore_of_a_released_order_is_not_converted(self):
        self.hold()
        self.release()
        self.assertEqual(self.restore().json()["status"], "not_converted")

    def test_restore_returns_what_was_deducted_and_marks_the_row(self):
        self.hold()
        self.convert()
        self.assertEqual(self.restore().json()["status"], "restored")
        row = self.row()
        self.assertEqual((row.status, row.deducted), ("restored", {}))
        self.assertEqual(self.stock_of(self.latte), 10)

    def test_a_restored_order_stays_out_of_the_held_units(self):
        self.hold()
        self.convert()
        self.restore()
        from bravepos import stock_pool
        self.assertEqual(stock_pool.grab_held(self.silom), {})

    def test_restore_after_a_correction_returns_the_corrected_amount(self):
        self.hold()
        self.convert()
        self.correct([{"barcode": "8812", "qty": 3}])
        self.restore()
        self.assertEqual(self.stock_of(self.latte), 10)


class CorrectTests(GrabTestCase):
    def converted(self):
        self.hold()
        self.convert()

    def test_correct_writes_only_the_difference(self):
        self.converted()
        res = self.correct([{"barcode": "8812", "qty": 3}])
        self.assertEqual(res.json()["status"], "corrected")
        self.assertEqual(self.stock_of(self.latte), 7)
        move = StockMovement.objects.filter(note="Grab GF-921 edit").get()
        self.assertEqual((move.type, move.qty), ("out", 1))
        self.assertEqual(self.row().deducted, {str(self.latte.id): 3})

    def test_a_repeat_is_no_change(self):
        self.converted()
        self.correct([{"barcode": "8812", "qty": 3}])
        res = self.correct([{"barcode": "8812", "qty": 3}])
        self.assertEqual(res.json()["status"], "no_change")
        self.assertEqual(self.stock_of(self.latte), 7)
        self.assertEqual(StockMovement.objects.filter(note="Grab GF-921 edit").count(), 1)

    def test_fewer_units_go_back_as_a_return(self):
        self.converted()
        self.correct([{"barcode": "8812", "qty": 1}])
        self.assertEqual(self.stock_of(self.latte), 9)
        self.assertEqual(StockMovement.objects.filter(type="return").get().qty, 1)

    def test_an_added_and_a_removed_product_in_one_edit(self):
        self.converted()
        self.correct([{"barcode": "8813", "qty": 2}])
        self.assertEqual((self.stock_of(self.latte), self.stock_of(self.cake)), (10, 3))
        self.assertEqual(self.row().deducted, {str(self.cake.id): 2})

    def test_an_order_never_converted_is_not_converted(self):
        self.assertEqual(self.correct([dict(LATTE_LINE)]).json()["status"], "not_converted")
        self.hold()
        self.assertEqual(self.correct([dict(LATTE_LINE)]).json()["status"], "not_converted")
        self.assertEqual(self.stock_of(self.latte), 10)

    def test_a_skipped_line_puts_no_stock_back_and_opens_the_review(self):
        self.converted()
        res = self.correct([{"barcode": "8812", "qty": 1}, {"barcode": "9001", "qty": 1}])
        self.assertEqual(res.json()["status"], "no_change")
        self.assertEqual(self.stock_of(self.latte), 8)               # nothing went back
        self.assertFalse(StockMovement.objects.filter(type="return").exists())
        self.assertEqual(self.row().deducted, {str(self.latte.id): 2})
        row = self.row()
        self.assertEqual((row.review_status, row.review_reason), ("open", "unmapped_item"))

    def test_a_skipped_line_still_lets_a_unit_added_elsewhere_be_deducted(self):
        self.converted()
        res = self.correct([{"barcode": "8812", "qty": 2}, {"barcode": "8813", "qty": 1},
                            {"barcode": "9001", "qty": 1}])
        self.assertEqual(res.json()["status"], "corrected")
        self.assertEqual((self.stock_of(self.latte), self.stock_of(self.cake)), (8, 4))
        self.assertEqual(self.row().review_reason, "unmapped_item")

    def test_an_inactive_product_line_counts_as_skipped(self):
        self.converted()
        Product.objects.filter(pk=self.latte.pk).update(active=False)
        self.correct([{"barcode": "8812", "qty": 1}, {"barcode": "8813", "qty": 1}])
        self.assertEqual(self.stock_of(self.latte), 8)
        self.assertEqual(self.row().review_reason, "unmapped_item")

    def test_removing_a_unit_with_every_line_mapped_still_puts_it_back(self):
        self.converted()
        self.correct([{"barcode": "8812", "qty": 1}])
        self.assertEqual(self.stock_of(self.latte), 9)
        self.assertEqual(self.row().review_status, "")

    def test_final_items_that_map_to_nothing_open_the_review_and_keep_the_deduction(self):
        self.converted()
        self.correct([{"barcode": "9001", "qty": 1}])
        self.assertEqual(self.stock_of(self.latte), 8)
        self.assertEqual(self.row().review_reason, "unmapped_item")

    def test_final_items_that_map_to_nothing_do_not_wipe_the_deduction(self):
        self.converted()
        res = self.correct([{"barcode": "9001", "qty": 1}])
        self.assertEqual(res.json()["status"], "no_change")
        self.assertEqual(self.stock_of(self.latte), 8)

    def test_a_restored_order_is_not_corrected(self):
        self.converted()
        self.restore()
        self.assertEqual(self.correct([{"barcode": "8812", "qty": 5}]).json()["status"],
                         "not_converted")
        self.assertEqual(self.stock_of(self.latte), 10)

    def test_a_correction_is_audited(self):
        self.converted()
        AuditLog.objects.all().delete()
        self.correct([{"barcode": "8812", "qty": 3}])
        row = AuditLog.objects.get(model="Product", object_id=str(self.latte.id))
        self.assertEqual(row.changes, {"stock": {"from": 8, "to": 7}})

    def test_dry_run_changes_nothing(self):
        self.converted()
        self.assertEqual(self.correct([{"barcode": "8812", "qty": 3}], dry_run=True)
                         .json()["status"], "dry_run")
        self.assertEqual(self.stock_of(self.latte), 8)

    def test_the_state_is_recorded(self):
        self.converted()
        self.correct([dict(LATTE_LINE)], grab_state="COMPLETED")
        self.assertEqual(self.row().grab_state, "COMPLETED")


class SeaTalkMixin:
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"SEATALK_GRAB_GROUP_ID": "grp-1"})
        env.start()
        self.addCleanup(env.stop)
        sender = mock.patch("bravepos.seatalk.send_group_text_async", return_value=True)
        self.sent = sender.start()
        self.addCleanup(sender.stop)


class ReviewTests(SeaTalkMixin, GrabTestCase):
    def test_review_opens_once_and_alerts_once_per_reason(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.review().json(), {"review_status": "open"})
            self.review()
            self.review()
        row = self.row()
        self.assertEqual((row.review_status, row.review_reason), ("open", "unclear_pickup"))
        self.assertEqual(self.sent.call_count, 1)
        group, text = self.sent.call_args.args
        self.assertEqual(group, "grp-1")
        self.assertIn("GF-921", text)

    def test_a_second_reason_alerts_again(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.review("unclear_pickup")
            self.review("bundle_mismatch")
        self.assertEqual(self.sent.call_count, 2)
        self.assertEqual(self.row().review_reason, "bundle_mismatch")

    def test_every_reason_the_poller_uses_is_accepted(self):
        for n, reason in enumerate(["unclear_pickup", "unmapped_item", "bundle_mismatch",
                                    "large_order_mismatch", "stale"]):
            self.assertEqual(self.review(reason, order_id=f"R{n}").status_code, 200, reason)

    def test_an_unknown_reason_is_400(self):
        self.assertEqual(self.review("because").status_code, 400)
        self.assertEqual(self.review(detail="text").status_code, 400)

    def test_review_keeps_a_held_order_held(self):
        self.hold()
        self.review()
        self.assertEqual(self.row().status, "held")
        from bravepos import stock_pool
        self.assertEqual(stock_pool.grab_held(self.silom), {str(self.latte.id): 2})

    def test_review_on_an_order_not_held_yet_creates_the_row(self):
        self.review()
        self.assertEqual(self.row().status, "held")
        self.assertEqual(self.row().items, [])

    def test_a_settled_review_does_not_reopen_for_the_same_reason(self):
        self.review()
        GrabOrderHold.objects.update(review_status="done", resolution="ignore")
        self.assertEqual(self.review().json(), {"review_status": "done"})
        self.assertEqual(self.row().review_status, "done")

    def test_no_group_configured_means_no_alert_and_no_error(self):
        with mock.patch.dict(os.environ, {"SEATALK_GRAB_GROUP_ID": ""}):
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(self.review().status_code, 200)
        self.assertEqual(self.sent.call_count, 0)

    def test_review_dry_run_writes_nothing(self):
        self.assertEqual(self.review(dry_run=True).json(), {"review_status": "open"})
        self.assertFalse(GrabOrderHold.objects.exists())


class HeartbeatTests(GrabTestCase):
    def test_a_good_cycle_stamps_the_status(self):
        res = self.beat()
        self.assertEqual(res.json(), {"outage": False})
        st = GrabSyncStatus.objects.get(branch=self.silom)
        self.assertTrue(st.push_live)
        self.assertEqual(st.grab_shown, {"8812": 4})
        self.assertIsNotNone(st.last_ok_at)
        self.assertEqual(st.problem, "")

    def test_a_named_problem_is_an_outage_and_the_next_good_cycle_ends_it(self):
        self.beat()
        res = self.beat(cycle_ok=False, problem="login")
        self.assertEqual(res.json(), {"outage": True})
        self.assertEqual(GrabSyncStatus.objects.get().problem, "login")
        self.assertEqual(self.beat().json(), {"outage": False})
        self.assertEqual(GrabSyncStatus.objects.get().problem, "")

    def test_a_bad_cycle_does_not_refresh_last_ok_or_the_shown_figures(self):
        self.beat()
        before = GrabSyncStatus.objects.get()
        self.beat(cycle_ok=False, problem="shape", grab_shown={"8812": 99})
        after = GrabSyncStatus.objects.get()
        self.assertEqual(after.last_ok_at, before.last_ok_at)
        self.assertEqual(after.grab_shown, {"8812": 4})

    def test_a_named_problem_on_a_good_cycle_is_an_outage(self):
        # The cycle read Grab fine; the push is what failed.
        self.beat()
        res = self.beat(cycle_ok=True, problem="push_failed")
        self.assertEqual(res.json(), {"outage": True})
        st = GrabSyncStatus.objects.get()
        self.assertEqual(st.problem, "push_failed")
        self.assertIsNotNone(st.last_ok_at)
        stock = self.client.get(STOCK, {"merchant_id": MERCHANT}, HTTP_X_API_KEY=KEY).json()
        self.assertTrue(stock["outage"])
        # The next clean cycle ends it.
        self.assertEqual(self.beat().json(), {"outage": False})
        self.assertEqual(GrabSyncStatus.objects.get().problem, "")

    def test_a_named_problem_with_push_off_is_no_outage(self):
        self.assertEqual(self.beat(push_live=False, problem="push_failed").json(),
                         {"outage": False})

    def test_zero_units_shown_is_accepted_and_kept(self):
        # The poller clamps what Grab shows at 0; 0 is a real figure, not an error.
        res = self.beat(grab_shown={"8812": 0, "8813": 0})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(GrabSyncStatus.objects.get().grab_shown, {"8812": 0, "8813": 0})

    def test_a_bad_cycle_with_no_problem_is_not_an_outage_by_itself(self):
        self.beat()
        self.assertEqual(self.beat(cycle_ok=False).json(), {"outage": False})

    def test_silence_for_five_minutes_is_an_outage(self):
        self.beat()
        GrabSyncStatus.objects.update(last_ok_at=timezone.now() - timedelta(minutes=6))
        self.assertTrue(self.client.get(STOCK, {"merchant_id": MERCHANT},
                                        HTTP_X_API_KEY=KEY).json()["outage"])

    def test_with_push_off_there_is_never_an_outage(self):
        self.beat(push_live=False, cycle_ok=False, problem="login")
        GrabSyncStatus.objects.update(last_ok_at=timezone.now() - timedelta(hours=3))
        self.assertFalse(self.client.get(STOCK, {"merchant_id": MERCHANT},
                                         HTTP_X_API_KEY=KEY).json()["outage"])

    def test_bad_bodies_are_400(self):
        self.assertEqual(self.beat(cycle_ok="yes").status_code, 400)
        self.assertEqual(self.beat(push_live=None).status_code, 400)
        self.assertEqual(self.beat(grab_shown=[1]).status_code, 400)
        self.assertEqual(self.beat(grab_shown={"8812": -1}).status_code, 400)

    def test_writes_off_records_nothing(self):
        with sync_env(writes=False):
            self.assertEqual(self.beat().json(), {"outage": False})
        self.assertFalse(GrabSyncStatus.objects.exists())


class StockReadTests(GrabTestCase):
    def get(self):
        return self.client.get(STOCK, {"merchant_id": MERCHANT}, HTTP_X_API_KEY=KEY).json()

    def test_it_carries_the_target_with_holds_and_the_reserve(self):
        self.latte.counter_stock = 3
        self.latte.save()
        self.hold()
        row = next(p for p in self.get()["products"] if p["barcode"] == "8812")
        self.assertEqual((row["stock"], row["counter_stock"], row["grab_held"], row["grab_target"]),
                         (10, 3, 2, 5))

    def test_open_orders_are_held_rows_and_recent_unfinished_converted_ones(self):
        self.hold(order_id="HELD")
        self.hold(order_id="DONE")
        self.convert(order_id="DONE")
        self.hold(order_id="COMPLETED")
        self.convert(order_id="COMPLETED", grab_state="COMPLETED")
        self.hold(order_id="GONE")
        self.release(order_id="GONE")
        self.convert(order_id="OLDGOLIVE", ready_at="2026-11-16T09:00:00Z",
                     since="2026-11-16T10:00:00Z")
        open_ids = {o["order_id"]: o for o in self.get()["open_orders"]}
        self.assertEqual(set(open_ids), {"HELD", "DONE"})
        self.assertEqual(open_ids["HELD"], {"order_id": "HELD", "status": "held",
                                            "grab_state": "PreparingV2", "review_status": "",
                                            "review_reason": None})
        self.assertEqual(open_ids["DONE"]["status"], "converted")

    def test_open_orders_carry_the_open_reviews_reason(self):
        for oid in ("U", "S", "M", "NONE", "SETTLED"):
            self.hold(order_id=oid)
        for oid, reason in (("U", "unclear_pickup"), ("S", "stale"), ("M", "unmapped_item"),
                            ("SETTLED", "unclear_pickup")):
            self.review(reason, order_id=oid)
        GrabOrderHold.objects.filter(order_id="SETTLED").update(review_status="done")
        reasons = {o["order_id"]: o["review_reason"] for o in self.get()["open_orders"]}
        self.assertEqual(reasons, {"U": "unclear_pickup", "S": "stale", "M": "unmapped_item",
                                   "NONE": None, "SETTLED": None})

    def test_a_converted_order_keeps_its_open_review_reason(self):
        self.hold()
        self.convert()
        self.review("stale")
        (order,) = self.get()["open_orders"]
        self.assertEqual((order["status"], order["review_reason"]), ("converted", "stale"))

    def test_a_converted_order_older_than_a_day_drops_out(self):
        self.hold()
        self.convert()
        GrabOrderHold.objects.update(converted_at=timezone.now() - timedelta(hours=25))
        self.assertEqual(self.get()["open_orders"], [])

    def test_target_changed_at_names_the_newest_touch(self):
        self.assertEqual(self.get()["target_changed_at"], {})
        self.hold()
        changed = self.get()["target_changed_at"]
        self.assertEqual(set(changed), {"8812"})
        self.assertRegex(changed["8812"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        StockMovement.objects.create(branch=self.silom, product=self.cake, type="in", qty=1)
        self.assertEqual(set(self.get()["target_changed_at"]), {"8812", "8813"})


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
            "branch": "Silom", "outage": False,
            "products": [
                {"barcode": "8812", "product_id": str(self.latte.id), "name": "Latte",
                 "stock": 10, "counter_stock": 0, "self_held": 0, "grab_held": 0,
                 "grab_target": 10},
                {"barcode": "8813", "product_id": str(self.cake.id), "name": "Cake",
                 "stock": 5, "counter_stock": 0, "self_held": 0, "grab_held": 0,
                 "grab_target": 5},
            ],
            "open_orders": [],
            "target_changed_at": {},
        })

    def test_reflects_a_convert(self):
        self.convert()
        stock = {p["barcode"]: p["stock"] for p in self.get().json()["products"]}
        self.assertEqual(stock["8812"], 8)

    def test_the_slash_spelling_redirects_to_the_declared_url(self):
        """The routes are declared without a slash; SlashTolerantMiddleware
        sends the other spelling there, and a POST keeps its body (308)."""
        res = self.client.get(STOCK + "/", {"merchant_id": MERCHANT}, HTTP_X_API_KEY=KEY)
        self.assertEqual(res.status_code, 301)
        self.assertEqual(res["Location"], STOCK + "?merchant_id=" + MERCHANT)
        res = self.client.post(
            CONVERT + "/", {"merchant_id": MERCHANT, "order_id": "SLASH", "items": []},
            content_type="application/json", HTTP_X_API_KEY=KEY, follow=True)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "nothing_to_convert")

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
        self.convert()
        self.assertEqual(self.stock_of(self.latte), 8)
        self.assertEqual(self.rewound(self.latte), 10)

    def test_convert_then_restore_rewinds_to_the_same_figure(self):
        self.convert()
        self.restore()
        self.assertEqual(self.rewound(self.latte), 10)

    def test_a_grab_order_from_before_the_day_stays(self):
        self.convert()
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


class BranchDropdownTests(BranchFormTests):
    """The merchant ID is a dropdown of the known Grab outlets, or blank."""

    def test_the_form_offers_the_known_outlets_by_grab_name(self):
        res = self.client.get(reverse("backoffice:branch_detail", args=[self.emq.id]))
        html = res.content.decode()
        self.assertIn('<select id="f-grabid" name="grab_merchant_id">', html)
        for merchant_id, name in grab_sync.GRAB_OUTLETS.items():
            self.assertIn(f'value="{merchant_id}"', html)
            self.assertIn(name, html)
        self.assertIn("Not on Grab", html)

    def test_chidlom_is_not_offered_and_maneeya_is(self):
        self.assertEqual(len(grab_sync.GRAB_OUTLETS), 12)
        self.assertNotIn("Grab Chidlom", grab_sync.GRAB_OUTLETS.values())
        self.assertEqual(grab_sync.GRAB_OUTLETS["3-C6WAWE2YTTVUGN"], "Grab Maneeya Center")

    def test_the_branchs_own_outlet_is_selected(self):
        res = self.client.get(reverse("backoffice:branch_detail", args=[self.silom.id]))
        self.assertContains(res, f'<option value="{MERCHANT}" selected>')

    def test_an_unknown_id_is_refused_with_a_form_error(self):
        res = self._post(self.emq, grab_merchant_id="3-NOTREAL", phone="021234567")
        self.assertEqual(res.status_code, 200, "an unknown ID must not redirect")
        self.assertContains(res, "not one of the known Grab outlets")
        self.assertContains(res, "021234567")
        self.emq.refresh_from_db()
        self.assertEqual(self.emq.grab_merchant_id, "")

    def test_a_known_outlet_saves_and_blank_unlinks(self):
        self._post(self.emq, grab_merchant_id="3-C7DUDGJTLE4GKA")
        self.emq.refresh_from_db()
        self.assertEqual(self.emq.grab_merchant_id, "3-C7DUDGJTLE4GKA")
        self._post(self.emq, grab_merchant_id="")
        self.emq.refresh_from_db()
        self.assertEqual(self.emq.grab_merchant_id, "")


class ReviewPageTests(SeaTalkMixin, GrabTestCase):
    """Backoffice > Grab review: Deduct / Restore / Ignore."""

    def setUp(self):
        super().setUp()
        self.password = "correct-horse-battery"
        admin = Staff(name="Rev Admin", username="revadmin", email="rev@therollingpinn.com",
                      role="admin", active=True, backoffice_access=True)
        admin.set_password(self.password)
        admin.save()
        Settings.objects.get_or_create(id="shop")
        self.assertTrue(self.client.login(username="revadmin", password=self.password))
        self.page = reverse("backoffice:grab_review")

    def settle(self, hold, action):
        return self.client.post(
            reverse("backoffice:grab_review_settle", args=[hold.id, action]),
            {"next": self.page})

    def open_held(self, reason="unclear_pickup", order_id=ORDER):
        self.hold(order_id=order_id)
        self.review(reason, order_id=order_id)
        return self.row(order_id)

    def open_converted(self, reason="unclear_pickup"):
        self.hold()
        self.convert()
        self.review(reason)
        return self.row()

    def test_the_page_lists_open_reviews_with_three_buttons(self):
        hold = self.open_held()
        res = self.client.get(self.page + f"?branch={self.silom.id}")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "GF-921")
        self.assertContains(res, "Latte ×2")
        for action in ("deduct", "restore", "ignore"):
            self.assertContains(res, reverse("backoffice:grab_review_settle", args=[hold.id, action]))

    def test_a_self_order_took_held_row_says_self_order(self):
        self.hold()
        hold = self.row()
        product = hold.items[0]["product_id"]
        for source, expected in (("self_order", "self-order"), ("counter", "The till sold")):
            hold.review_status = ""
            hold.save(update_fields=["review_status"])
            with self.captureOnCommitCallbacks(execute=True):
                grab_sync.till_took_held(
                    self.silom, "PS1",
                    [{"product_id": product, "wanted": 3, "can_sell": 2, "held_for_grab": 1}],
                    source)
            res = self.client.get(self.page + f"?branch={self.silom.id}")
            self.assertContains(res, expected)

    def test_the_nav_links_to_it_with_a_count(self):
        self.open_held()
        res = self.client.get(reverse("backoffice:dashboard"))
        self.assertContains(res, self.page)
        self.assertContains(res, "1 Grab order to review")

    def test_an_empty_page_says_so(self):
        res = self.client.get(self.page + f"?branch={self.silom.id}")
        self.assertContains(res, "Nothing to review.")

    def test_deduct_a_held_order_converts_it(self):
        hold = self.open_held()
        self.settle(hold, "deduct")
        hold.refresh_from_db()
        self.assertEqual((hold.status, hold.review_status, hold.resolution, hold.resolved_by),
                         ("converted", "done", "deduct", "Rev Admin"))
        self.assertIsNotNone(hold.resolved_at)
        self.assertEqual(self.stock_of(self.latte), 8)
        self.assertEqual(StockMovement.objects.filter(type="out").count(), 1)

    def test_deduct_a_converted_order_leaves_it_as_it_is(self):
        hold = self.open_converted()
        self.settle(hold, "deduct")
        hold.refresh_from_db()
        self.assertEqual((hold.status, hold.review_status, hold.resolution),
                         ("converted", "done", "deduct"))
        self.assertEqual(self.stock_of(self.latte), 8)
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_restore_a_held_order_releases_it(self):
        hold = self.open_held()
        self.settle(hold, "restore")
        hold.refresh_from_db()
        self.assertEqual((hold.status, hold.review_status, hold.resolution),
                         ("released", "done", "restore"))
        self.assertEqual(self.stock_of(self.latte), 10)
        self.assertFalse(StockMovement.objects.exists())

    def test_restore_a_converted_order_puts_the_stock_back(self):
        hold = self.open_converted()
        self.settle(hold, "restore")
        hold.refresh_from_db()
        self.assertEqual((hold.status, hold.review_status, hold.resolution),
                         ("restored", "done", "restore"))
        self.assertEqual(self.stock_of(self.latte), 10)
        move = StockMovement.objects.get(type="return")
        self.assertEqual(move.note, "Grab GF-921 cancelled (staff:restore)")

    def test_ignore_a_held_order_releases_it_without_deducting(self):
        hold = self.open_held()
        self.settle(hold, "ignore")
        hold.refresh_from_db()
        self.assertEqual((hold.status, hold.review_status, hold.resolution),
                         ("released", "done", "ignore"))
        self.assertEqual(self.stock_of(self.latte), 10)
        self.assertFalse(StockMovement.objects.exists())

    def test_ignore_a_converted_order_keeps_its_deduction(self):
        hold = self.open_converted()
        self.settle(hold, "ignore")
        hold.refresh_from_db()
        self.assertEqual((hold.status, hold.review_status, hold.resolution),
                         ("converted", "done", "ignore"))
        self.assertEqual(self.stock_of(self.latte), 8)

    def test_a_settled_review_cannot_be_settled_again(self):
        hold = self.open_held()
        self.settle(hold, "deduct")
        res = self.settle(hold, "restore")
        self.assertEqual(res.status_code, 302)
        hold.refresh_from_db()
        self.assertEqual((hold.status, hold.resolution), ("converted", "deduct"))
        self.assertEqual(self.stock_of(self.latte), 8)

    def test_a_get_changes_nothing_and_a_bad_action_is_404(self):
        hold = self.open_held()
        self.client.get(reverse("backoffice:grab_review_settle", args=[hold.id, "deduct"]))
        hold.refresh_from_db()
        self.assertEqual(hold.review_status, "open")
        self.assertEqual(self.settle(hold, "explode").status_code, 404)

    def test_after_ignoring_the_same_reason_does_not_reopen_it(self):
        hold = self.open_held()
        self.settle(hold, "ignore")
        self.review()
        hold.refresh_from_db()
        self.assertEqual(hold.review_status, "done")

    def test_the_page_shows_only_the_selected_branchs_orders(self):
        mine = self.open_held()
        other = make_product(self.paragon, name="Latte", stock=5)
        other.barcode = "8812"
        other.save()
        self.paragon.grab_merchant_id = "3-C7DUDGJTLE4GKA"
        self.paragon.save()
        theirs = GrabOrderHold.objects.create(
            branch=self.paragon, order_id="THEIRS", display_id="GF-THEIRS",
            review_status="open", review_reason="stale",
            items=[{"product_id": str(other.id), "barcode": "8812", "qty": 1, "name": "Latte"}])
        res = self.client.get(self.page + f"?branch={self.silom.id}")
        self.assertContains(res, "GF-921")
        self.assertNotContains(res, "GF-THEIRS")
        self.assertContains(res, "1 at other branches")
        res = self.client.get(self.page + f"?branch={self.paragon.id}")
        self.assertContains(res, "GF-THEIRS")
        self.assertNotContains(res, "GF-921")
        self.assertEqual(mine.review_status, "open")
        self.assertEqual(theirs.review_status, "open")

    def test_an_order_at_an_inactive_branch_cannot_be_settled(self):
        hold = self.open_held()
        Branch.objects.filter(pk=self.silom.pk).update(active=False)
        self.assertEqual(self.settle(hold, "deduct").status_code, 404)

    def test_a_viewer_cannot_open_or_use_it(self):
        viewer = Staff(name="Vi", username="viewer1", email="vi@therollingpinn.com",
                       role="viewer", active=True, backoffice_access=True)
        viewer.set_password(self.password)
        viewer.save()
        hold = self.open_held()
        from django.test import Client
        viewer_client = Client()           # logout() cannot read a UUID-keyed session
        self.assertTrue(viewer_client.login(username="viewer1", password=self.password))
        self.assertEqual(viewer_client.get(self.page).status_code, 403)
        res = viewer_client.post(
            reverse("backoffice:grab_review_settle", args=[hold.id, "deduct"]))
        self.assertEqual(res.status_code, 403)
        hold.refresh_from_db()
        self.assertEqual(hold.review_status, "open")

    def test_settled_ones_are_listed_underneath(self):
        hold = self.open_held()
        self.settle(hold, "ignore")
        res = self.client.get(self.page + f"?branch={self.silom.id}")
        self.assertContains(res, "Ignored")
        self.assertContains(res, "Rev Admin")


class WatchdogTests(SeaTalkMixin, GrabTestCase):
    def run_dog(self, *args):
        from django.core.management import call_command
        from io import StringIO
        with self.captureOnCommitCallbacks(execute=True):
            call_command("grab_sync_watchdog", *args, stdout=StringIO())

    def quiet(self, minutes=10, **extra):
        fields = dict(push_live=True, last_ok_at=timezone.now() - timedelta(minutes=minutes))
        fields.update(extra)
        return GrabSyncStatus.objects.update_or_create(branch=self.silom, defaults=fields)[0]

    def test_an_outage_alerts_once_however_often_it_runs(self):
        self.quiet()
        self.run_dog()
        self.run_dog()
        self.run_dog()
        self.assertEqual(self.sent.call_count, 1)
        group, text = self.sent.call_args.args
        self.assertEqual(group, "grp-1")
        self.assertIn("Silom", text)
        self.assertIn("down", text)

    def test_recovery_alerts_once_and_a_second_outage_alerts_again(self):
        self.quiet()
        self.run_dog()
        self.beat()                                    # a good cycle
        self.run_dog()
        self.run_dog()
        self.assertEqual(self.sent.call_count, 2)
        self.assertIn("back", self.sent.call_args.args[1])
        self.quiet()
        self.run_dog()
        self.assertEqual(self.sent.call_count, 3)

    def test_a_named_problem_is_reported_in_the_text(self):
        self.quiet(minutes=0, problem="login")
        self.run_dog()
        self.assertIn("login", self.sent.call_args.args[1])

    def test_nothing_for_a_healthy_branch_or_one_with_push_off(self):
        self.quiet(minutes=1)
        self.run_dog()
        self.quiet(minutes=60, push_live=False)
        self.run_dog()
        self.assertEqual(self.sent.call_count, 0)

    def test_a_branch_without_a_merchant_id_is_skipped(self):
        self.quiet()
        self.silom.grab_merchant_id = ""
        self.silom.save()
        self.run_dog()
        self.assertEqual(self.sent.call_count, 0)

    def test_dry_run_sends_and_remembers_nothing(self):
        self.quiet()
        self.run_dog("--dry-run")
        self.assertEqual(self.sent.call_count, 0)
        self.assertIsNone(GrabSyncStatus.objects.get().outage_alerted_at)

    def test_with_no_group_nothing_is_sent_and_the_outage_is_still_announced_later(self):
        self.quiet()
        with mock.patch.dict(os.environ, {"SEATALK_GRAB_GROUP_ID": ""}):
            self.run_dog()
        self.assertEqual(self.sent.call_count, 0)
        self.run_dog()
        self.assertEqual(self.sent.call_count, 1)


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

    def test_a_double_convert_takes_stock_out_once(self):
        results = []
        items = [{"barcode": "8812", "qty": 2, "grab_item_id": ""}]

        def call():
            results.append(
                grab_sync.convert_order(self.branch, ORDER, "GF-921", items)["status"])

        errors = run_concurrently([call, call, call])
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), ["already_converted", "already_converted", "converted"])
        self.latte.refresh_from_db()
        self.assertEqual(self.latte.stock, 8)
        self.assertEqual(StockMovement.objects.filter(document_no=ORDER).count(), 1)
        connections.close_all()

    def test_a_concurrent_hold_of_the_same_order_makes_one_row(self):
        results = []
        order = {"order_id": ORDER, "display_id": "GF-921", "grab_state": "PreparingV2",
                 "placed_at": None, "items": [{"barcode": "8812", "qty": 2, "grab_item_id": "",
                                               "name": "Latte", "bundle_item_id": ""}]}

        def call():
            results.append(grab_sync.hold_orders(self.branch, [order])["results"][0]["created"])

        errors = run_concurrently([call, call, call, call])
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False, False, False, True])
        self.assertEqual(GrabOrderHold.objects.filter(order_id=ORDER).count(), 1)
        connections.close_all()

    def test_a_concurrent_convert_and_release_end_in_one_state(self):
        grab_sync.hold_orders(self.branch, [{
            "order_id": ORDER, "display_id": "GF-921", "grab_state": "", "placed_at": None,
            "items": [{"barcode": "8812", "qty": 2, "grab_item_id": "", "name": "", "bundle_item_id": ""}]}])
        items = [{"barcode": "8812", "qty": 2, "grab_item_id": "", "name": "", "bundle_item_id": ""}]
        errors = run_concurrently([
            lambda: grab_sync.convert_order(self.branch, ORDER, "GF-921", items),
            lambda: grab_sync.release_order(self.branch, ORDER, "CANCELLED"),
            lambda: grab_sync.convert_order(self.branch, ORDER, "GF-921", items),
        ])
        self.assertEqual(errors, [])
        hold = GrabOrderHold.objects.get(order_id=ORDER)
        self.latte.refresh_from_db()
        if hold.status == "converted":
            self.assertEqual(self.latte.stock, 8)
        else:
            self.assertEqual((hold.status, self.latte.stock), ("released", 10))
        self.assertLessEqual(StockMovement.objects.filter(document_no=ORDER).count(), 1)
        connections.close_all()

    def sale_lines(self, *products):
        return [{"product_id": str(p.id), "name": p.name, "qty": 1, "price": "10"}
                for p in products]

    def test_the_hold_row_lock_does_not_lock_the_branch(self):
        """``select_for_update(of=("self",))``: the till's branch lock is never
        taken by a Grab call, so it cannot block a checkout."""
        from django.db import connection as conn
        grab_sync.hold_orders(self.branch, [{
            "order_id": ORDER, "display_id": "G", "grab_state": "", "placed_at": None,
            "items": []}])
        outcome = []

        def other_connection():
            try:
                with transaction.atomic():
                    Branch.objects.select_for_update(nowait=True).get(pk=self.branch.pk)
                outcome.append("locked")
            except Exception as e:      # noqa: BLE001 — OperationalError when it is held
                outcome.append(repr(e))

        with transaction.atomic():
            from django.test.utils import CaptureQueriesContext
            with CaptureQueriesContext(conn) as ctx:
                grab_sync._lock_hold(self.branch, ORDER)
            self.assertIn('FOR UPDATE OF "bravepos_graborderhold"',
                          ctx.captured_queries[0]["sql"])
            self.assertEqual(run_concurrently([other_connection]), [])
        self.assertEqual(outcome, ["locked"])

    def test_the_review_page_settle_and_till_gate_lock_only_the_hold_row(self):
        from django.db import connection as conn
        from django.test.utils import CaptureQueriesContext
        hold = GrabOrderHold.objects.create(
            branch=self.branch, order_id=ORDER, review_status="open",
            review_reason="unclear_pickup", items=[])
        with CaptureQueriesContext(conn) as ctx:
            grab_sync.settle_review(hold.pk, "ignore", "tester")
            grab_sync.till_took_held(self.branch, "PS1", [])
        locking = [q["sql"] for q in ctx.captured_queries if "FOR UPDATE" in q["sql"]]
        self.assertTrue(locking)
        self.assertTrue(all("FOR UPDATE OF" in q and "branch" not in q.split("FOR UPDATE OF")[1]
                            for q in locking), locking)

    def test_till_sales_and_grab_conversions_in_opposite_orders_do_not_deadlock(self):
        from bravepos.orders import create_order_from_items
        make_shop()
        cake = make_product(self.branch, name="Cake", stock=1000)
        cake.barcode = "8813"
        cake.save()
        self.latte.stock = 1000
        self.latte.save()
        rounds = 10
        for n in range(rounds):
            grab_sync.hold_orders(self.branch, [{
                "order_id": f"O{n}", "display_id": f"G{n}", "grab_state": "", "placed_at": None,
                "items": [{"barcode": "8813", "qty": 1, "grab_item_id": "", "name": "Cake",
                           "bundle_item_id": ""},
                          {"barcode": "8812", "qty": 1, "grab_item_id": "", "name": "Latte",
                           "bundle_item_id": ""}]}])
        fns = []
        for n in range(rounds):
            fns.append(lambda: create_order_from_items(
                branch=self.branch, items=self.sale_lines(self.latte, cake),   # latte first
                goods_total=20, subtotal=20, payment_method="Cash", paid_amount=20))
            fns.append(lambda n=n: grab_sync.convert_order(
                self.branch, f"O{n}", f"G{n}", None))                           # cake first
            fns.append(lambda n=n: grab_sync.correct_order(
                self.branch, f"O{n}", f"G{n}", [
                    {"barcode": "8812", "qty": 2, "grab_item_id": "", "name": "", "bundle_item_id": ""},
                    {"barcode": "8813", "qty": 1, "grab_item_id": "", "name": "", "bundle_item_id": ""}]))
        errors = run_concurrently(fns)
        self.assertEqual(errors, [])
        from bravepos.models import Order
        self.assertEqual(Order.objects.count(), rounds)
        cake.refresh_from_db()
        self.latte.refresh_from_db()
        # Every sale took 1 of each; every order that converted took 1 cake and
        # 1 latte (2 after a correction that ran after it).
        converted = GrabOrderHold.objects.filter(status="converted").count()
        self.assertEqual(cake.stock, 1000 - rounds - converted)
        self.assertGreaterEqual(1000 - self.latte.stock, rounds + converted)
        connections.close_all()

    def test_a_till_sale_that_took_held_units_opens_its_review_while_the_order_converts(self):
        from bravepos.orders import create_order_from_items
        make_shop()
        self.latte.stock = 6
        self.latte.save()
        grab_sync.hold_orders(self.branch, [{
            "order_id": ORDER, "display_id": "G1", "grab_state": "", "placed_at": None,
            "items": [{"barcode": "8812", "qty": 5, "grab_item_id": "", "name": "Latte",
                       "bundle_item_id": ""}]}])
        errors = run_concurrently([
            lambda: create_order_from_items(
                branch=self.branch,
                items=[{"product_id": str(self.latte.id), "name": "Latte", "qty": 3, "price": "10"}],
                goods_total=30, subtotal=30, payment_method="Cash", paid_amount=30),
            lambda: grab_sync.convert_order(self.branch, ORDER, "G1", None),
        ])
        self.assertEqual(errors, [])
        self.latte.refresh_from_db()
        self.assertEqual(self.latte.stock, 6 - 3 - 5)
        connections.close_all()

    def test_sale_reviews_the_captured_hold_after_a_waiting_convert_finishes(self):
        """The sale has the product lock, then the convert takes the hold lock
        and waits for that product.  The post-commit review must still find the
        same hold after the convert changes it to ``converted``."""
        from bravepos.orders import _grab_shortfalls, create_order_from_items

        make_shop()
        self.latte.stock = 7
        self.latte.save()
        hold = GrabOrderHold.objects.create(
            branch=self.branch, order_id=ORDER, display_id="G1",
            items=[{"product_id": str(self.latte.id), "barcode": "8812", "qty": 5,
                    "name": "Latte"}],
        )
        convert_waiting_for_product = threading.Event()
        convert_errors = []
        real_products_by_barcode = grab_sync._products_by_barcode

        def signal_convert_before_product_lock(branch, barcodes, *, lock=False):
            if lock and threading.current_thread().name == "waiting-convert":
                convert_waiting_for_product.set()
            return real_products_by_barcode(branch, barcodes, lock=lock)

        def convert():
            try:
                grab_sync.convert_order(self.branch, ORDER, "G1", None)
            except Exception as error:  # noqa: BLE001 — asserted on the parent thread
                convert_errors.append(error)
            finally:
                connections.close_all()

        converter = threading.Thread(target=convert, name="waiting-convert")

        def capture_then_start_convert(branch, items, free_items):
            shortfalls = _grab_shortfalls(branch, items, free_items)
            self.assertEqual(shortfalls[0]["_hold_id"], str(hold.pk))
            converter.start()
            self.assertTrue(convert_waiting_for_product.wait(timeout=10),
                            "convert did not take the hold lock before seeking the product")
            return shortfalls

        with mock.patch("bravepos.grab_sync._products_by_barcode",
                        side_effect=signal_convert_before_product_lock), \
                mock.patch("bravepos.orders._grab_shortfalls", side_effect=capture_then_start_convert):
            sale = create_order_from_items(
                branch=self.branch,
                items=[{"product_id": str(self.latte.id), "name": "Latte", "qty": 3,
                        "price": "10"}],
                goods_total=30, subtotal=30, payment_method="Cash", paid_amount=30,
            )
        converter.join(timeout=10)
        self.assertFalse(converter.is_alive(), "convert did not finish")
        self.assertEqual(convert_errors, [])
        hold.refresh_from_db()
        self.assertEqual((hold.status, hold.review_status, hold.review_reason),
                         ("converted", "open", "till_took_held"))
        self.assertEqual(hold.review_detail["order_number"], sale.order_number)
        self.latte.refresh_from_db()
        self.assertEqual(self.latte.stock, -1)
        connections.close_all()


class StockMovementIndexTests(TestCase):
    def test_the_branch_and_time_index_exists(self):
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(
                cursor, StockMovement._meta.db_table)
        index = constraints.get("stockmov_branch_created_idx")
        self.assertIsNotNone(index)
        self.assertEqual(index["columns"], ["branch_id", "created_at"])

    def test_the_migration_builds_it_concurrently_outside_a_transaction(self):
        import importlib
        mod = importlib.import_module("bravepos.migrations.0093_stockmovement_branch_created_index")
        self.assertFalse(mod.Migration.atomic)
        from django.contrib.postgres.operations import AddIndexConcurrently
        self.assertTrue(all(isinstance(op, AddIndexConcurrently)
                            for op in mod.Migration.operations))
        self.assertEqual(mod.Migration.dependencies, [("bravepos", "0092_grab_stock_sync")])
