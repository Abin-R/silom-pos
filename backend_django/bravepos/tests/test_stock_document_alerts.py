"""Second stock-document alerts: exact threshold, local days, commit and paths."""
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.db import close_old_connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from bravepos.models import (
    BranchSession, Staff, StockDocument, StockDocumentAlertDay, StockDocumentAlertEvent,
    StockMovement,
)
from bravepos.orders import _record_free_items
from bravepos.stock_alerts import BOT_TESTING_GROUP_ID, process_day, record_document
from bravepos.tests.factories import make_branch, make_product
from bravepos.tests.test_user_management_and_audit import make_user, sign_in
from bravepos.views import save_stock_in_document, save_stock_out_document


@override_settings(TIME_ZONE="Asia/Bangkok")
class StockDocumentAlertTests(TestCase):
    def setUp(self):
        self.branch = make_branch(name="Silom Alerts")
        self.product = make_product(self.branch, name="Croissant", stock=20)
        self.sequence = 0
        now = mock.patch("django.utils.timezone.now", return_value=datetime(2026, 11, 18, 5, tzinfo=dt_timezone.utc))
        self.clock = now.start()
        self.addCleanup(now.stop)
        send = mock.patch("bravepos.seatalk.send_group_text_async")
        self.send = send.start()
        self.addCleanup(send.stop)
        configured = mock.patch("bravepos.seatalk.is_configured", return_value=True)
        self.configured = configured.start()
        self.addCleanup(configured.stop)

    def create(self, kind="in", branch=None, **fields):
        self.sequence += 1
        with self.captureOnCommitCallbacks(execute=True):
            return StockDocument.objects.create(
                branch=branch or self.branch, type=kind,
                document_no=f"DOC-{self.sequence}", **fields,
            )

    def test_second_and_every_later_creation_sends_to_bot_testing_for_each_type(self):
        for kind in ("in", "out"):
            before = self.send.call_count
            self.create(kind)
            self.assertEqual(self.send.call_count, before)
            second = self.create(kind, created_by="Nok", reason="Shop Supply", total=Decimal("125.50"))
            self.assertEqual(self.send.call_count, before + 1)
            group, text = self.send.call_args.args
            self.assertEqual(group, BOT_TESTING_GROUP_ID)
            self.assertEqual(text, "\n".join([
                "BravePOS: repeat stock submission",
                "Branch: Silom Alerts",
                "Date: 18/11/2026",
                f"Type: {'Stock In' if kind == 'in' else 'Stock Out'}",
                "Times today: 2",
                "Document: https://pos.rollingpinn.com/backoffice/inventory/"
                f"stock-{kind}/{second.id}",
            ]))
            for count in (3, 4):
                later = self.create(kind)
                self.assertEqual(self.send.call_count, before + count - 1)
                group, text = self.send.call_args.args
                self.assertEqual(group, BOT_TESTING_GROUP_ID)
                self.assertIn(f"Times today: {count}", text)
            daily = StockDocumentAlertDay.objects.get(document_type=kind)
            self.assertEqual(daily.document_count, 4)
            self.assertIsNotNone(daily.triggered_at)

    def test_group_comes_from_env_and_falls_back_to_bot_testing(self):
        with mock.patch.dict("os.environ", {"SEATALK_STOCK_GROUP_ID": " STOCK-AUDIT "}):
            self.create()
            self.create()
        self.assertEqual(self.send.call_args.args[0], "STOCK-AUDIT")
        with mock.patch.dict("os.environ", {"SEATALK_STOCK_GROUP_ID": ""}):
            self.create()
        self.assertEqual(self.send.call_args.args[0], BOT_TESTING_GROUP_ID)

    def test_branches_and_types_count_independently(self):
        other = make_branch(name="Other")
        self.create("in")
        self.create("out")
        self.create("in", branch=other)
        self.send.assert_not_called()
        self.create("in", branch=other)
        self.assertEqual(self.send.call_count, 1)
        self.assertIn("Branch: Other", self.send.call_args.args[1])
        self.create("out")
        self.assertEqual(self.send.call_count, 2)

    def test_bangkok_midnight_resets_count_even_with_another_active_timezone(self):
        self.clock.return_value = datetime(2026, 11, 18, 16, 59, tzinfo=dt_timezone.utc)
        with timezone.override("UTC"):
            self.create()
            self.clock.return_value = datetime(2026, 11, 18, 17, 0, tzinfo=dt_timezone.utc)
            self.create()
            self.send.assert_not_called()
            self.create()
        self.assertEqual(self.send.call_count, 1)
        self.assertIn("Date: 19/11/2026", self.send.call_args.args[1])
        self.assertEqual(list(StockDocumentAlertDay.objects.order_by("day").values_list("document_count", flat=True)), [1, 2])

    def test_edits_checks_adjustments_and_branchless_documents_do_not_count(self):
        doc = self.create()
        doc.note = "Edited"
        doc.save(update_fields=["note"])
        self.create("check")
        self.create("adjust")
        StockDocument.objects.create(type="in", document_no="no-branch")
        self.send.assert_not_called()
        self.assertEqual(StockDocumentAlertDay.objects.count(), 1)
        self.assertEqual(StockDocumentAlertDay.objects.get().document_count, 1)

    def test_rolled_back_second_document_does_not_alert_or_use_threshold(self):
        self.create()
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    StockDocument.objects.create(branch=self.branch, type="in", document_no="rolled-back")
                    raise RuntimeError("line save failed")
        self.send.assert_not_called()
        self.assertEqual(StockDocument.objects.count(), 1)
        self.assertEqual(StockDocumentAlertDay.objects.get().document_count, 1)
        self.create()
        self.assertEqual(self.send.call_count, 1)

    def test_alert_waits_for_outer_commit(self):
        self.create()
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                doc = StockDocument.objects.create(branch=self.branch, type="in", document_no="finished")
                self.send.assert_not_called()
                doc.total = Decimal("400")
                doc.save(update_fields=["total"])
            self.send.assert_not_called()
        self.assertIn("Times today: 2", self.send.call_args.args[1])

    def test_deletion_does_not_reset_daily_creation_count(self):
        first = self.create()
        first.delete()
        second = self.create()
        self.assertEqual(self.send.call_count, 1)
        second.delete()
        self.create()
        self.assertEqual(self.send.call_count, 2)
        self.assertIn("Times today: 3", self.send.call_args.args[1])
        self.assertEqual(StockDocumentAlertDay.objects.get().document_count, 3)

    def test_midday_rollout_alerts_new_documents_only_with_the_correct_count(self):
        for kind, previous in (("in", 1), ("out", 3)):
            StockDocument.objects.bulk_create([
                StockDocument(branch=self.branch, type=kind, document_no=f"old-{kind}-{i}")
                for i in range(previous)
            ])
            new = self.create(kind)
            self.assertIn(f"Times today: {previous + 1}", self.send.call_args.args[1])
        self.assertEqual(self.send.call_count, 2)
        self.assertIn("Type: Stock Out\nTimes today: 4", self.send.call_args.args[1])
        self.assertEqual(StockDocumentAlertDay.objects.get(document_type="out").document_count, 4)

    def test_no_seatalk_credentials_is_safe_and_does_not_send_elsewhere(self):
        self.configured.return_value = False
        self.create()
        with self.assertLogs("bravepos.stock_alerts", level="WARNING"):
            self.create()
        self.send.assert_not_called()
        self.assertIsNotNone(StockDocumentAlertDay.objects.get().triggered_at)

    def test_sender_exception_does_not_undo_saved_stock_document(self):
        self.create()
        self.send.side_effect = RuntimeError("sender unavailable")
        with self.assertLogs("bravepos.stock_alerts", level="ERROR"):
            second = self.create()
        self.assertTrue(StockDocument.objects.filter(pk=second.pk).exists())
        self.assertEqual(StockDocumentAlertDay.objects.get().document_count, 2)

    def test_reprocessing_creation_events_does_not_count_or_alert_twice(self):
        self.create()
        second = self.create()
        with self.captureOnCommitCallbacks(execute=True):
            record_document(second)
            process_day(self.branch.pk, "in", timezone.localdate(second.created_at))
        self.assertEqual(StockDocumentAlertEvent.objects.count(), 2)
        self.assertEqual(StockDocumentAlertDay.objects.get().document_count, 2)
        self.assertEqual(self.send.call_count, 1)

    def test_batched_events_keep_each_count_and_do_not_resend_on_replay(self):
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                docs = [StockDocument.objects.create(
                    branch=self.branch, type="in", document_no=f"batch-{i}",
                ) for i in range(1, 5)]
        self.assertEqual(self.send.call_count, 3)
        for count, call in enumerate(self.send.call_args_list, start=2):
            self.assertIn(f"Times today: {count}", call.args[1])
        with self.captureOnCommitCallbacks(execute=True):
            process_day(self.branch.pk, "in", timezone.localdate(docs[0].created_at))
        self.assertEqual(self.send.call_count, 3)
        self.assertEqual(StockDocumentAlertDay.objects.get().document_count, 4)

    def test_processing_failure_keeps_document_and_retries_pending_events(self):
        with mock.patch("bravepos.stock_alerts.StockDocumentAlertDay.objects.using",
                        side_effect=RuntimeError("counter unavailable")):
            with self.assertLogs("bravepos.stock_alerts", level="ERROR"):
                first = self.create()
        self.assertTrue(StockDocument.objects.filter(pk=first.pk).exists())
        self.assertEqual(StockDocumentAlertEvent.objects.filter(processed=False).count(), 1)
        self.create()
        self.assertEqual(StockDocumentAlertEvent.objects.filter(processed=False).count(), 0)
        self.assertEqual(StockDocumentAlertDay.objects.get().document_count, 2)
        self.assertEqual(self.send.call_count, 1)

    def test_backoffice_helpers_and_till_share_the_same_daily_count(self):
        staff = Staff.objects.create(name="Till Admin", role="admin")
        staff.branches.add(self.branch)
        session = BranchSession.objects.create(branch=self.branch, staff=staff, token="stock-alerts-test")
        for kind in ("in", "out"):
            helper = save_stock_in_document if kind == "in" else save_stock_out_document
            with self.captureOnCommitCallbacks(execute=True):
                helper(self.branch, [(self.product, Decimal(1), Decimal(10), Decimal(0))], reason="Shop Supply")
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post("/api/stock-documents", {
                    "type": kind, "reason": "Shop Supply" if kind == "in" else "Expired",
                    "items": [{"product_id": str(self.product.id), "qty": 1}],
                }, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {session.token}")
            self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.send.call_count, 2)

    def test_promotion_free_items_are_not_a_submission(self):
        # A sale's free item leaves stock through a history line, not a
        # stock-out document, so it never counts toward the alert.
        self.create("out")
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                _record_free_items(self.branch, SimpleNamespace(order_number="ORDER-1", customer_name="Nok"),
                                   [{"product_id": str(self.product.id), "qty": 1}],
                                   {str(self.product.id): self.product}, {}, "Cashier")
        self.send.assert_not_called()
        self.assertEqual(StockDocument.objects.filter(type="out").count(), 1)
        self.assertEqual(StockDocumentAlertDay.objects.get(document_type="out").document_count, 1)
        move = StockMovement.objects.get(document_no="ORDER-1")
        self.assertEqual((move.type, move.qty, move.created_by), ("out", 1, "Cashier"))
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 19)

    def test_backoffice_forms_send_on_second_valid_save_only(self):
        sign_in(self.client, make_user(username="stockalerts", email="alerts@test.local", role="admin"))
        for kind in ("in", "out"):
            url = reverse(f"backoffice:stock_{kind}_new") + f"?branch={self.branch.id}"
            # Different quantities: the same form posted twice is one document.
            for qty in ("1", "2"):
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.client.post(url, {
                        "reason": "Shop Supply" if kind == "in" else "Expired",
                        "product_id": [str(self.product.id)], "qty": [qty], "price": ["10"],
                    })
                self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual(self.send.call_count, 2)


@skipUnlessDBFeature("has_select_for_update")
class ConcurrentStockDocumentAlertTests(TransactionTestCase):
    def test_concurrent_creations_alert_once_for_every_second_and_later_document(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        branch = make_branch(name="Concurrent")
        barrier = Barrier(6)

        def create(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                StockDocument.objects.create(
                    branch_id=branch.pk, type="in" if index < 3 else "out",
                    document_no=f"concurrent-{index}",
                )
            finally:
                close_old_connections()

        with mock.patch("bravepos.seatalk.is_configured", return_value=True), \
                mock.patch("bravepos.seatalk.send_group_text_async") as send:
            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = [pool.submit(create, i) for i in range(6)]
                for future in futures:
                    future.result(timeout=30)
        self.assertEqual(send.call_count, 4)
        texts = [call.args[1] for call in send.call_args_list]
        for kind in ("Stock In", "Stock Out"):
            for count in (2, 3):
                self.assertEqual(sum(f"Type: {kind}\nTimes today: {count}" in text for text in texts), 1)
        self.assertEqual(StockDocument.objects.count(), 6)
        self.assertEqual(StockDocumentAlertDay.objects.count(), 2)
        for daily in StockDocumentAlertDay.objects.all():
            self.assertEqual(daily.document_count, 3)
            self.assertIsNotNone(daily.triggered_at)
