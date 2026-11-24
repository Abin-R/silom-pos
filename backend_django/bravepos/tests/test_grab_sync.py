"""Grab stock sync, BravePOS half: deduct / heartbeat / stock endpoints, the
watchdog and the branch form.

A poller outside this repo calls these with an API key.  What matters is that a
Grab order's units come off stock once, the first time it is seen (Grab deducts
at acceptance too), however often it is sent; that an edit deducts only units
added; that nothing ever puts stock back; that the "inventory as of" report
still adds up; and that nothing changes unless the write switch is on.

Run:
    python manage.py test bravepos.tests.test_grab_sync \\
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

import os
from datetime import timedelta, timezone as dt_timezone
from unittest import mock, skipUnless

from django.db import IntegrityError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from bravepos import grab_sync
from bravepos.models import (
    AuditLog, Branch, GrabOrderDeduction, GrabSyncStatus, Settings, Staff, StockMovement,
)

from .factories import make_branch, make_product, make_shop
from .test_selforder import run_concurrently

KEY = "test-grab-key"
MERCHANT = "3-C2M2RUXENZCZGT"
ORDER = "001553556136-C8LJEFNGJPAVCA"

DEDUCT = "/api/grab/orders/deduct"
HEARTBEAT = "/api/grab/heartbeat"
STOCK = "/api/grab/stock"
LATTE_LINE = {"barcode": "8812", "qty": 2, "grab_item_id": "ITEM1", "name": "Latte"}


def sync_env(key=KEY):
    """The environment a deployed poller would meet."""
    return mock.patch.dict(os.environ, {grab_sync.API_KEY_ENV: key})


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

    def deduct(self, items=None, order_id=ORDER, merchant_id=MERCHANT, dry_run=None, **extra):
        order = {"order_id": order_id, "display_id": "GF-921",
                 "placed_at": "2026-11-16T10:00:00Z",
                 "items": items if items is not None else [dict(LATTE_LINE)]}
        order.update(extra)
        body = {"merchant_id": merchant_id, "orders": [order]}
        if dry_run is not None:
            body["dry_run"] = dry_run
        return self.post(DEDUCT, body)

    def result(self, res):
        self.assertEqual(res.status_code, 200, res.content)
        (row,) = res.json()["results"]
        return row

    def beat(self, **extra):
        body = {"merchant_id": MERCHANT, "cycle_ok": True, "problem": ""}
        body.update(extra)
        return self.post(HEARTBEAT, body)

    def stock_of(self, product):
        product.refresh_from_db()
        return product.stock


class AuthTests(GrabTestCase):
    def test_missing_key_is_401(self):
        for url in (DEDUCT, HEARTBEAT):
            self.assertEqual(self.post(url, {}, key=None).status_code, 401, url)
        self.assertEqual(self.client.get(STOCK, {"merchant_id": MERCHANT}).status_code, 401)

    def test_wrong_key_is_401_and_changes_nothing(self):
        res = self.post(DEDUCT, {"merchant_id": MERCHANT, "orders": [
            {"order_id": ORDER, "items": [{"barcode": "8812", "qty": 1}]}]}, key="nope")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(self.stock_of(self.latte), 10)
        res = self.client.get(STOCK, {"merchant_id": MERCHANT}, HTTP_X_API_KEY="nope")
        self.assertEqual(res.status_code, 401)

    def test_non_ascii_key_is_401_not_a_500(self):
        self.assertEqual(self.post(DEDUCT, {}, key="clé").status_code, 401)

    def test_unset_key_env_is_503_for_every_call(self):
        with mock.patch.dict(os.environ, {grab_sync.API_KEY_ENV: ""}):
            self.assertEqual(self.deduct().status_code, 503)
            self.assertEqual(self.beat().status_code, 503)
            self.assertEqual(
                self.client.get(STOCK, {"merchant_id": MERCHANT}, HTTP_X_API_KEY=KEY).status_code,
                503)
            # Even the right-looking empty key does not get in.
            self.assertEqual(self.post(DEDUCT, {}, key="").status_code, 503)

    def test_unknown_merchant_is_404(self):
        self.assertEqual(self.deduct(merchant_id="3-UNKNOWN").status_code, 404)
        self.assertEqual(self.beat(merchant_id="3-UNKNOWN").status_code, 404)
        res = self.client.get(STOCK, {"merchant_id": "3-UNKNOWN"}, HTTP_X_API_KEY=KEY)
        self.assertEqual(res.status_code, 404)

    def test_inactive_branch_is_404(self):
        Branch.objects.filter(pk=self.silom.pk).update(active=False)
        self.assertEqual(self.deduct().status_code, 404)
        res = self.client.get(STOCK, {"merchant_id": MERCHANT}, HTTP_X_API_KEY=KEY)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(self.stock_of(self.latte), 10)

    def test_a_blank_merchant_id_is_400_and_never_matches_an_unsynced_branch(self):
        # paragon has grab_merchant_id "" — a blank lookup must not find it.
        self.assertEqual(self.deduct(merchant_id="").status_code, 400)
        self.assertEqual(self.deduct(merchant_id="  ").status_code, 400)
        self.assertEqual(self.client.get(STOCK, HTTP_X_API_KEY=KEY).status_code, 400)

    def test_merchant_id_is_matched_case_insensitively(self):
        self.assertEqual(self.deduct(merchant_id=MERCHANT.lower()).status_code, 200)

    def test_bad_bodies_are_400(self):
        self.assertEqual(self.post(DEDUCT, [1, 2]).status_code, 400)
        self.assertEqual(self.post(DEDUCT, {"merchant_id": MERCHANT, "orders": "x"}).status_code, 400)
        self.assertEqual(self.post(DEDUCT, {"merchant_id": MERCHANT,
                                            "orders": [{}] * 501}).status_code, 400)
        self.assertEqual(self.stock_of(self.latte), 10)


class DeductTests(GrabTestCase):
    def test_the_first_sighting_deducts_and_writes_the_movement(self):
        row = self.result(self.deduct())
        self.assertEqual(row["status"], "deducted")
        self.assertEqual(row["lines"], [{
            "barcode": "8812", "product_id": str(self.latte.id),
            "qty": 2, "stock_after": 8, "skipped_reason": None,
        }])
        self.assertEqual(self.stock_of(self.latte), 8)
        move = StockMovement.objects.get(product=self.latte)
        self.assertEqual((move.type, move.qty, move.document_no, move.note, move.created_by),
                         ("out", 2, ORDER, "Grab GF-921", "Grab sync"))
        self.assertEqual(move.branch, self.silom)
        saved = GrabOrderDeduction.objects.get()
        self.assertEqual(saved.deducted, {str(self.latte.id): 2})

    def test_it_writes_an_audit_row_with_the_stock_change(self):
        AuditLog.objects.all().delete()
        self.deduct()
        row = AuditLog.objects.get(model="Product", object_id=str(self.latte.id))
        self.assertEqual(row.changes, {"stock": {"from": 10, "to": 8}})
        self.assertEqual(row.source, "grab-sync")

    def test_sending_it_again_changes_nothing(self):
        self.deduct()
        for _ in range(3):
            self.assertEqual(self.result(self.deduct())["status"], "unchanged")
        self.assertEqual(self.stock_of(self.latte), 8)
        self.assertEqual(StockMovement.objects.filter(document_no=ORDER).count(), 1)

    def test_an_edit_deducts_only_what_was_added(self):
        self.deduct()
        row = self.result(self.deduct(items=[{"barcode": "8812", "qty": 3},
                                             {"barcode": "8813", "qty": 1}]))
        self.assertEqual(row["status"], "updated")
        self.assertEqual((self.stock_of(self.latte), self.stock_of(self.cake)), (7, 4))
        self.assertEqual(GrabOrderDeduction.objects.get().deducted,
                         {str(self.latte.id): 3, str(self.cake.id): 1})

    def test_units_removed_by_an_edit_are_never_put_back(self):
        self.deduct()
        row = self.result(self.deduct(items=[{"barcode": "8812", "qty": 1}]))
        self.assertEqual(row["status"], "unchanged")
        self.assertEqual(self.stock_of(self.latte), 8)
        self.assertFalse(StockMovement.objects.filter(type="return").exists())
        # Adding back up to the original deducts nothing either.
        self.assertEqual(self.result(self.deduct())["status"], "unchanged")
        self.assertEqual(self.stock_of(self.latte), 8)

    def test_an_order_emptied_or_unmapped_later_changes_nothing(self):
        self.deduct()
        self.assertEqual(self.result(self.deduct(items=[]))["status"], "unchanged")
        self.assertEqual(self.stock_of(self.latte), 8)

    def test_two_orders_with_the_same_display_id_are_separate(self):
        self.deduct()
        self.deduct(order_id="OTHER-ORDER")          # same GF-921
        self.assertEqual(self.stock_of(self.latte), 6)

    def test_duplicate_barcodes_are_merged(self):
        self.deduct(items=[{"barcode": "8812", "qty": 1}, {"barcode": "8812", "qty": 2}])
        self.assertEqual(self.stock_of(self.latte), 7)
        self.assertEqual(StockMovement.objects.get(product=self.latte).qty, 3)

    def test_unknown_barcodes_are_skipped_and_the_rest_deducted(self):
        row = self.result(self.deduct(items=[
            {"barcode": "8812", "qty": 1},
            {"barcode": "9001", "qty": 1, "grab_item_id": "GONE"},
            {"barcode": "", "qty": 1, "grab_item_id": "NOCODE"},
        ]))
        self.assertEqual(row["status"], "deducted")
        self.assertEqual(row["skipped"], [{"barcode": "9001", "reason": "not_at_branch"},
                                          {"barcode": "", "reason": "no_barcode"}])
        self.assertEqual(self.stock_of(self.latte), 9)

    def test_an_inactive_product_or_another_branchs_is_untouched(self):
        self.latte.active = False
        self.latte.save()
        theirs = make_product(self.paragon, name="Latte", stock=10)
        theirs.barcode = "8812"
        theirs.save()
        row = self.result(self.deduct())
        self.assertEqual(row["status"], "unchanged")
        self.assertEqual((self.stock_of(self.latte), self.stock_of(theirs)), (10, 10))

    def test_an_order_with_nothing_mapped_is_remembered_and_deducts_later(self):
        self.assertEqual(self.result(self.deduct(items=[{"barcode": "9001", "qty": 1}]))["status"],
                         "unchanged")
        # The product is added at the branch later; the next sighting deducts.
        p = make_product(self.silom, name="New", stock=4)
        p.barcode = "9001"
        p.save()
        self.assertEqual(self.result(self.deduct(items=[{"barcode": "9001", "qty": 1}]))["status"],
                         "updated")
        self.assertEqual(self.stock_of(p), 3)

    def test_stock_may_go_negative(self):
        self.deduct(items=[{"barcode": "8813", "qty": 7}])
        self.assertEqual(self.stock_of(self.cake), -2)

    def test_a_batch_deducts_every_order_and_one_bad_order_does_not_stop_it(self):
        res = self.post(DEDUCT, {"merchant_id": MERCHANT, "orders": [
            {"order_id": "A", "items": [{"barcode": "8812", "qty": 1}]},
            {"order_id": "", "items": []},
            {"order_id": "B", "items": [{"barcode": "8812", "qty": "2"}]},
            {"order_id": "C", "items": [{"barcode": "8813", "qty": 2}]},
            {"order_id": "A", "items": []},
        ]})
        body = res.json()
        self.assertEqual([r["order_id"] for r in body["results"]], ["A", "C"])
        self.assertEqual([i["index"] for i in body["invalid"]], [1, 2, 4])
        self.assertEqual((self.stock_of(self.latte), self.stock_of(self.cake)), (9, 3))

    def test_a_display_id_up_to_64_characters_is_fine(self):
        self.assertEqual(self.deduct(display_id="x" * 64).status_code, 200)
        self.assertEqual(self.deduct(display_id="x" * 65).json()["invalid"][0]["index"], 0)


class LinkTests(GrabTestCase):
    """Linking a branch to its Grab outlet is the one switch."""

    def outlets(self):
        return self.client.get("/api/grab/outlets", HTTP_X_API_KEY=KEY)

    def test_outlets_lists_active_linked_branches_only(self):
        linked = timezone.now()
        Branch.objects.filter(pk=self.silom.pk).update(grab_linked_at=linked)
        closed = make_branch(name="Closed")
        Branch.objects.filter(pk=closed.pk).update(grab_merchant_id="3-C7MCT7BAEJ2ZGX", active=False)
        res = self.outlets()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"outlets": [{
            "merchant_id": MERCHANT, "branch": "Silom",
            "linked_at": linked.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}]})

    def test_outlets_needs_the_key(self):
        self.assertEqual(self.client.get("/api/grab/outlets").status_code, 401)

    def test_an_order_placed_before_linking_is_never_deducted(self):
        Branch.objects.filter(pk=self.silom.pk).update(
            grab_linked_at=timezone.now() - timedelta(minutes=5))
        old = (timezone.now() - timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        new = (timezone.now() - timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(self.result(self.deduct(placed_at=old))["status"], "before_link")
        self.assertEqual(self.stock_of(self.latte), 10)
        self.assertFalse(GrabOrderDeduction.objects.exists())
        self.assertEqual(self.result(self.deduct(order_id="NEW", placed_at=new))["status"],
                         "deducted")
        self.assertEqual(self.stock_of(self.latte), 8)


class DryRunTests(GrabTestCase):
    def test_dry_run_in_the_body_changes_nothing_and_returns_the_lines(self):
        row = self.result(self.deduct(dry_run=True))
        self.assertEqual(row["status"], "dry_run")
        self.assertEqual(row["lines"][0]["stock_after"], 8)
        self.assertEqual(self.stock_of(self.latte), 10)
        self.assertFalse(GrabOrderDeduction.objects.exists())
        self.assertFalse(StockMovement.objects.exists())

    def test_dry_run_of_an_order_already_deducted_is_unchanged(self):
        self.deduct()
        self.assertEqual(self.result(self.deduct(dry_run=True))["status"], "unchanged")
        self.assertEqual(self.result(self.deduct(
            items=[{"barcode": "8812", "qty": 5}], dry_run=True))["status"], "dry_run")
        self.assertEqual(self.stock_of(self.latte), 8)

    def test_a_non_true_dry_run_value_does_not_count(self):
        self.assertEqual(self.result(self.deduct(dry_run="yes"))["status"], "deducted")


class HeartbeatTests(GrabTestCase):
    def status(self):
        return GrabSyncStatus.objects.get(branch=self.silom)

    def test_a_good_cycle_stamps_the_status(self):
        self.assertEqual(self.beat().json(), {"ok": True})
        self.assertIsNotNone(self.status().last_ok_at)

    def test_a_problem_is_recorded_and_a_good_cycle_clears_it(self):
        self.beat(cycle_ok=False, problem="Grab is rate limiting")
        self.assertEqual(self.status().problem, "Grab is rate limiting")
        self.assertIsNone(self.status().last_ok_at)
        self.beat()
        self.assertEqual(self.status().problem, "")

    def test_old_poller_fields_are_ignored(self):
        res = self.beat(push_live=True, grab_shown={"8812": 4})
        self.assertEqual(res.status_code, 200)

    def test_bad_bodies_are_400(self):
        self.assertEqual(self.beat(cycle_ok="yes").status_code, 400)
        self.assertEqual(self.beat(problem=5).status_code, 400)

    def test_a_dry_run_beat_records_nothing(self):
        self.beat(dry_run=True)
        self.assertFalse(GrabSyncStatus.objects.exists())


class WatchdogTests(GrabTestCase):
    def setUp(self):
        super().setUp()
        sender = mock.patch("bravepos.seatalk.send_group_text", return_value=True)
        self.sent = sender.start()
        self.addCleanup(sender.stop)

    def run_watchdog(self, *args):
        from django.core.management import call_command
        call_command("grab_sync_watchdog", *args, stdout=open(os.devnull, "w"))

    def quiet(self, minutes=16):
        GrabSyncStatus.objects.update_or_create(branch=self.silom, defaults={
            "last_ok_at": timezone.now() - timedelta(minutes=minutes), "problem": ""})

    def test_nothing_for_a_healthy_branch_or_one_never_seen(self):
        self.run_watchdog()
        self.quiet(minutes=2)
        self.run_watchdog()
        self.sent.assert_not_called()

    def test_a_quiet_poller_alerts_once_and_recovery_alerts_once(self):
        self.quiet()
        self.run_watchdog()
        self.run_watchdog()
        self.assertEqual(self.sent.call_count, 1)
        self.assertIn("not affected", self.sent.call_args[0][1])
        self.assertIn("not being deducted", self.sent.call_args[0][1])
        self.quiet(minutes=0)
        self.run_watchdog()
        self.run_watchdog()
        self.assertEqual(self.sent.call_count, 2)
        self.assertIn("back", self.sent.call_args[0][1])

    def test_a_failed_send_is_retried_next_run(self):
        self.quiet()
        self.sent.return_value = False
        self.run_watchdog()
        self.assertIsNone(GrabSyncStatus.objects.get().quiet_alerted_at)
        self.sent.return_value = True
        self.run_watchdog()
        self.assertIsNotNone(GrabSyncStatus.objects.get().quiet_alerted_at)

    def test_a_branch_without_a_merchant_id_is_skipped(self):
        self.quiet()
        Branch.objects.filter(pk=self.silom.pk).update(grab_merchant_id="")
        self.run_watchdog()
        self.sent.assert_not_called()

    def test_dry_run_sends_nothing(self):
        self.quiet()
        self.run_watchdog("--dry-run")
        self.sent.assert_not_called()


class StockEndpointTests(GrabTestCase):
    def get(self, merchant=MERCHANT):
        return self.client.get(STOCK, {"merchant_id": merchant}, HTTP_X_API_KEY=KEY)

    def test_returns_active_barcoded_products_of_that_branch_only(self):
        make_product(self.silom, name="Loose", stock=3)
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
                {"barcode": "8812", "product_id": str(self.latte.id), "name": "Latte",
                 "stock": 10, "counter_stock": 0, "self_held": 0, "grab_target": 10},
                {"barcode": "8813", "product_id": str(self.cake.id), "name": "Cake",
                 "stock": 5, "counter_stock": 0, "self_held": 0, "grab_target": 5},
            ],
            "target_changed_at": {},
        })

    def test_the_target_leaves_the_counter_reserve(self):
        self.latte.counter_stock = 3
        self.latte.save()
        row = next(p for p in self.get().json()["products"] if p["barcode"] == "8812")
        self.assertEqual((row["stock"], row["grab_target"]), (10, 7))
        self.latte.stock = 2
        self.latte.save()
        row = next(p for p in self.get().json()["products"] if p["barcode"] == "8812")
        self.assertEqual(row["grab_target"], 0)

    def test_a_deduction_shows_in_stock_and_target_changed_at(self):
        self.deduct()
        body = self.get().json()
        stock = {p["barcode"]: p["stock"] for p in body["products"]}
        self.assertEqual(stock["8812"], 8)
        self.assertIn("8812", body["target_changed_at"])

    def test_the_stock_read_takes_no_locks(self):
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as ctx:
            self.get()
        self.assertFalse([q["sql"] for q in ctx.captured_queries if "FOR UPDATE" in q["sql"]])

    def test_the_slash_spelling_redirects_to_the_declared_url(self):
        res = self.client.get(STOCK + "/", {"merchant_id": MERCHANT}, HTTP_X_API_KEY=KEY)
        self.assertEqual(res.status_code, 301)
        self.assertEqual(res["Location"], STOCK + "?merchant_id=" + MERCHANT)

    def test_stock_is_read_only(self):
        self.assertEqual(self.post(STOCK, {"merchant_id": MERCHANT}).status_code, 405)

    def test_the_old_order_endpoints_are_gone(self):
        for path in ("hold", "convert", "release", "restore", "correct", "review"):
            self.assertEqual(self.post(f"/api/grab/orders/{path}", {}).status_code, 404, path)
        self.assertEqual(self.post("/api/grab/till-check", {}).status_code, 404)


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
        return _rewind_stock([product], self.yesterday)[0].stock

    def test_stock_yesterday_is_before_todays_grab_order(self):
        self.deduct()
        self.assertEqual(self.stock_of(self.latte), 8)
        self.assertEqual(self.rewound(self.latte), 10)

    def test_a_grab_order_from_before_the_day_stays(self):
        self.deduct()
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
        self.assertEqual((self.silom.grab_merchant_id, self.silom.grab_linked_at), ("", None))

    def test_linking_stamps_the_time_and_resaving_keeps_it(self):
        self._post(self.emq, grab_merchant_id="3-C7MCT7BAEJ2ZGX")
        self.emq.refresh_from_db()
        first = self.emq.grab_linked_at
        self.assertIsNotNone(first)
        self._post(self.emq, grab_merchant_id="3-C7MCT7BAEJ2ZGX")
        self.emq.refresh_from_db()
        self.assertEqual(self.emq.grab_linked_at, first)

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

    def sale_lines(self, *products):
        return [{"product_id": str(p.id), "name": p.name, "qty": 1, "price": "10"}
                for p in products]

    def order(self, order_id=ORDER, *lines):
        return {"order_id": order_id, "display_id": "GF-921", "placed_at": None,
                "items": [{"barcode": b, "qty": q, "grab_item_id": "", "name": "",
                           "bundle_item_id": ""} for b, q in (lines or [("8812", 2)])]}

    def test_the_same_order_sent_at_once_deducts_once(self):
        results = []

        def call():
            results.append(grab_sync.deduct_orders(self.branch, [self.order()])["results"][0]["status"])

        errors = run_concurrently([call, call, call])
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), ["deducted", "unchanged", "unchanged"])
        self.latte.refresh_from_db()
        self.assertEqual(self.latte.stock, 8)
        self.assertEqual(GrabOrderDeduction.objects.count(), 1)
        connections.close_all()

    def test_till_sales_and_grab_deductions_in_opposite_orders_do_not_deadlock(self):
        from bravepos.models import Order
        from bravepos.orders import create_order_from_items
        make_shop()
        cake = make_product(self.branch, name="Cake", stock=1000)
        cake.barcode = "8813"
        cake.save()
        self.latte.stock = 1000
        self.latte.save()
        rounds = 10
        fns = []
        for n in range(rounds):
            fns.append(lambda: create_order_from_items(
                branch=self.branch, items=self.sale_lines(self.latte, cake),   # latte first
                goods_total=20, subtotal=20, payment_method="Cash", paid_amount=20))
            fns.append(lambda n=n: grab_sync.deduct_orders(self.branch, [self.order(
                f"O{n}", ("8813", 1), ("8812", 1))]))                          # cake first
        errors = run_concurrently(fns)
        self.assertEqual(errors, [])
        self.assertEqual(Order.objects.count(), rounds)
        cake.refresh_from_db()
        self.latte.refresh_from_db()
        self.assertEqual((cake.stock, self.latte.stock), (1000 - 2 * rounds, 1000 - 2 * rounds))
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
        mod = importlib.import_module("bravepos.migrations.0102_stockmovement_branch_created_index")
        self.assertFalse(mod.Migration.atomic)
        from django.contrib.postgres.operations import AddIndexConcurrently
        self.assertTrue(all(isinstance(op, AddIndexConcurrently)
                            for op in mod.Migration.operations))
        self.assertEqual(mod.Migration.dependencies, [("bravepos", "0101_grab_stock_sync")])
