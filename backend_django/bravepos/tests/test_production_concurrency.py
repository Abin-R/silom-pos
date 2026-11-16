"""Real row-lock regressions; SQLite cannot verify these guarantees.

Run against an isolated PostgreSQL database with a short lock timeout.
Never run with the default settings, which point at production.
"""
from __future__ import annotations

import datetime
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection, connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from bravepos import production
from bravepos.models import (
    Branch, Product, ProductionCutoffPolicy, ProductionRequest, ProductionRequestItem, Settings,
)
from bravepos.production_policy import update_cutoff


@skipUnless(connection.vendor == "postgresql", "Requires real PostgreSQL row locks")
class ProductionConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.settings, _ = Settings.objects.get_or_create(id="shop")
        self.branch = Branch.objects.create(name="Concurrent production")
        self.product = Product.objects.create(branch=self.branch, name="Cookie", barcode="concurrent-1")
        self.other = Product.objects.create(branch=self.branch, name="Mousse", barcode="concurrent-2")
        self.day = timezone.localdate() + datetime.timedelta(days=3)

    def seed(self):
        req = production.save_request(self.branch, self.day, {
            str(self.product.pk): 7, str(self.other.pk): 5,
        })
        return req, list(req.items.order_by("pk"))

    def parallel(self, *calls):
        barrier = threading.Barrier(len(calls))

        def worker(call):
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute("SET statement_timeout = '12s'")
                barrier.wait(timeout=5)
                call()
                return "saved"
            except production.RevisionConflict:
                return "conflict"
            except production.RequestLocked:
                return "locked"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            futures = [pool.submit(worker, call) for call in calls]
            return [future.result(timeout=20) for future in futures]

    def test_simultaneous_first_saves_have_one_conflict_not_integrity_error(self):
        def save(qty):
            return lambda: production.save_request(
                self.branch, self.day, {str(self.product.pk): qty},
                expected_request_id=None, expected_revision=0, strict=True,
            )

        results = self.parallel(save(8), save(9))
        self.assertCountEqual(results, ["saved", "conflict"])
        self.assertEqual(ProductionRequest.objects.count(), 1)
        self.assertIn(ProductionRequestItem.objects.get().requested_qty, (8, 9))

    def test_same_supervisor_revision_cannot_overwrite_a_new_save(self):
        req, _items = self.seed()

        def save(qty):
            return lambda: production.save_request(
                self.branch, self.day, {str(self.product.pk): qty},
                expected_request_id=str(req.pk), expected_revision=req.revision, strict=True,
            )

        self.assertCountEqual(self.parallel(save(8), save(9)), ["saved", "conflict"])
        self.assertIn(req.items.get(product=self.product).requested_qty, (8, 9))

    def test_two_packers_changing_the_same_cell_have_one_conflict(self):
        _req, items = self.seed()
        item = items[0]

        def pack(qty):
            return lambda: production.save_packed(
                self.day, {str(item.pk): qty},
                expected_revisions={str(item.pk): item.revision}, strict=True,
            )

        self.assertCountEqual(self.parallel(pack(6), pack(8)), ["saved", "conflict"])
        item.refresh_from_db()
        self.assertIn(item.packed_qty, (6, 8))

    def test_two_packers_changing_different_cells_both_save(self):
        _req, items = self.seed()

        def pack(item, qty):
            return lambda: production.save_packed(
                self.day, {str(item.pk): qty},
                expected_revisions={str(item.pk): item.revision}, strict=True,
            )

        self.assertEqual(self.parallel(pack(items[0], 6), pack(items[1], 8)), ["saved", "saved"])
        for item, qty in zip(items, (6, 8)):
            item.refresh_from_db()
            self.assertEqual(item.packed_qty, qty)

    def test_opposite_order_multi_parent_batches_do_not_deadlock(self):
        req, items = self.seed()
        second_branch = Branch.objects.create(name="Other concurrent branch")
        second_product = Product.objects.create(branch=second_branch, name="Cookie", barcode="concurrent-1")
        second = production.save_request(second_branch, self.day, {str(second_product.pk): 3}).items.get()
        first = req.items.get(product=self.product)
        rows = [(str(first.pk), 6), (str(second.pk), 2)]
        forward = dict(rows)
        reverse = dict(reversed(rows))
        expected = {str(first.pk): first.revision, str(second.pk): second.revision}
        results = self.parallel(
            lambda: production.save_packed(self.day, forward, expected_revisions=expected, strict=True),
            lambda: production.save_packed(self.day, reverse, expected_revisions=expected, strict=True),
        )
        self.assertCountEqual(results, ["saved", "conflict"])

    def test_supervisor_and_packer_share_parent_first_lock_order(self):
        req, _items = self.seed()
        item = req.items.get(product=self.product)
        results = self.parallel(
            lambda: production.save_request(
                self.branch, self.day, {str(self.product.pk): 9},
                expected_request_id=str(req.pk), expected_revision=req.revision, strict=True,
            ),
            lambda: production.save_packed(
                self.day, {str(item.pk): 6},
                expected_revisions={str(item.pk): item.revision}, strict=True,
            ),
        )
        self.assertEqual(results[0], "saved")
        self.assertIn(results[1], ("saved", "conflict"))
        item.refresh_from_db()
        self.assertEqual(item.requested_qty, 9)
        self.assertEqual(item.packed_qty, 6 if results[1] == "saved" else None)

    def _wait_for_database_lock(self, backend_pid):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_stat_clear_snapshot()")
                cursor.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", [backend_pid])
                row = cursor.fetchone()
            if row and row[0] == "Lock":
                return
            time.sleep(0.01)
        self.fail("The worker did not wait on the held database row")

    def assert_lock_wait_cannot_cross_cutoff(self, lock_kind):
        req, items = self.seed()
        clock = [production.locks_at(self.day) - datetime.timedelta(seconds=1)]
        after = clock[0] + datetime.timedelta(seconds=2)
        ready = threading.Event()
        pid = []

        def worker():
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SET lock_timeout = '8s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    pid.append(cursor.fetchone()[0])
                ready.set()
                production.save_request(self.branch, self.day, {str(self.product.pk): 9})
                return "saved"
            except production.RequestLocked:
                return "locked"
            finally:
                connections.close_all()

        with patch("django.utils.timezone.now", side_effect=lambda: clock[0]):
            with ThreadPoolExecutor(max_workers=1) as pool:
                with transaction.atomic():
                    if lock_kind == "parent":
                        ProductionRequest.objects.select_for_update().get(pk=req.pk)
                    elif lock_kind == "item":
                        ProductionRequestItem.objects.select_for_update().get(pk=items[0].pk)
                    else:
                        Settings.objects.select_for_update().get(pk="shop")
                    future = pool.submit(worker)
                    self.assertTrue(ready.wait(timeout=5))
                    self._wait_for_database_lock(pid[0])
                    clock[0] = after
                result = future.result(timeout=15)
        self.assertEqual(result, "locked")
        self.assertEqual(req.items.get(product=self.product).requested_qty, 7)

    def test_parent_lock_wait_cannot_authorize_a_post_cutoff_write(self):
        self.assert_lock_wait_cannot_cross_cutoff("parent")

    def test_item_lock_wait_cannot_authorize_a_post_cutoff_write(self):
        self.assert_lock_wait_cannot_cross_cutoff("item")

    def test_settings_lock_wait_cannot_authorize_a_post_cutoff_write(self):
        self.assert_lock_wait_cannot_cross_cutoff("settings")

    def test_cutoff_change_racing_first_request_cannot_reopen_a_closed_date(self):
        ProductionCutoffPolicy.objects.all().delete()
        ProductionCutoffPolicy.objects.create(
            effective_from=datetime.date.min, cutoff=datetime.time(18),
            activated_at=timezone.now(), source="legacy_baseline",
        )
        Settings.objects.filter(pk="shop").update(production_cutoff=datetime.time(18))
        deadline = production.locks_at(self.day)
        after_cutoff = deadline + datetime.timedelta(minutes=30)
        with patch("django.utils.timezone.now", return_value=after_cutoff):
            results = self.parallel(
                lambda: update_cutoff(datetime.time(20)),
                lambda: production.save_request(
                    self.branch, self.day, {str(self.product.pk): 9},
                    expected_request_id=None, expected_revision=0, strict=True,
                ),
            )
        self.assertEqual(results, ["saved", "locked"])
        self.assertEqual(production.locks_at(self.day), deadline)
        self.assertFalse(ProductionRequest.objects.filter(branch=self.branch, delivery_date=self.day).exists())
