"""The till's Stock Movement tab: gone, and its past entries filed as documents.

* Migration 0104 turns each past entry that moved stock into a one-line Stock In
  or Stock Out of type "Stock Movement" (an Adjust by its audited change).
* Nobody can create a type of that name, or file a new document under it.
* ``POST /api/stock-movements`` is refused.
* Past-date inventory and the repeat-stock alert are unaffected.

Run:
    python manage.py test bravepos.tests.test_stock_movement_removed \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

import importlib
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from bravepos import branch_sync
from bravepos import inventory_history as history
from bravepos.models import (
    STOCK_MOVEMENT_TYPE, AuditLog, BranchSession, Staff, StockDocument, StockDocumentAlertDay,
    StockInReason, StockMovement, StockOutReason, is_reserved_stock_type,
)
from bravepos.tests.test_user_management_and_audit import make_user, sign_in
from bravepos.views import save_stock_in_document

from .factories import make_branch, make_product

MIGRATION = importlib.import_module("bravepos.migrations.0104_stock_movement_to_documents")
FILL_STAFF = importlib.import_module("bravepos.migrations.0105_stock_movement_documents_staff")
_state_apps = None


def run_migration():
    """The data migration on historical models, as ``migrate`` runs it."""
    global _state_apps
    if _state_apps is None:
        loader = MigrationLoader(connection)
        _state_apps = loader.project_state(
            ("bravepos", "0104_stock_movement_to_documents")).apps
    MIGRATION.forwards(_state_apps, SimpleNamespace(connection=connection))


class _Base(TestCase):
    def setUp(self):
        self.branch = make_branch(name="Central World")
        self.staff = Staff.objects.create(name="Cashier1", email="c1@test.local",
                                          password_hash="x", role="cashier")
        self.staff.branches.add(self.branch)
        self.cake = make_product(self.branch, name="Mango Cake", stock=10)
        # Made long ago, so past-date inventory lists it.
        AuditLog.objects.update(at=timezone.now() - timedelta(days=30))
        self.seq = 0
        sender = mock.patch("bravepos.seatalk.send_group_text_async")
        self.send = sender.start()
        self.addCleanup(sender.stop)

    def entry(self, kind, qty, product=None, days=0):
        """What the old tab saved: stock written through ``Product.save()``
        (so an audit row) and one ledger row numbered SM…"""
        product = product or self.cake
        product.refresh_from_db()
        if kind == "in":
            product.stock += qty
        elif kind == "out":
            product.stock -= qty
        else:
            product.stock = qty
        product.save(update_fields=["stock"])
        self.seq += 1
        mv = StockMovement.objects.create(
            branch=self.branch, product=product, product_name=product.name, type=kind,
            qty=qty, note=f"Admin {kind}", document_no=f"SM2611190000{self.seq:02d}",
            staff=self.staff, created_by="Cashier1")
        if days:
            when = timezone.now() - timedelta(days=days)
            StockMovement.objects.filter(pk=mv.pk).update(created_at=when)
            AuditLog.objects.filter(model="Product", object_id=str(product.id)).filter(
                at__gte=timezone.now() - timedelta(seconds=30)).update(at=when)
            mv.refresh_from_db()
        return mv

    def migrated(self):
        return StockDocument.objects.filter(reason=STOCK_MOVEMENT_TYPE).order_by("created_at")


class MigrationTests(_Base):
    def test_each_entry_that_moved_stock_becomes_a_document(self):
        got_in = self.entry("in", 5)             # 10 -> 15
        got_out = self.entry("out", 3)           # 15 -> 12
        up = self.entry("adjust", 20)            # 12 -> 20: in 8
        down = self.entry("adjust", 14)          # 20 -> 14: out 6
        self.entry("adjust", 14)                 # typed what was there: nothing
        self.entry("in", 0)                      # nothing
        stock = self.cake.stock
        moves = StockMovement.objects.count()

        run_migration()

        docs = list(self.migrated())
        self.assertEqual(
            [(d.type, d.document_no, d.items.get().qty) for d in docs],
            [("in", got_in.document_no, 5), ("out", got_out.document_no, 3),
             ("in", up.document_no, 8), ("out", down.document_no, 6)])
        for doc, mv in zip(docs, (got_in, got_out, up, down)):
            self.assertEqual((doc.created_at, doc.created_by, doc.branch_id),
                             (mv.created_at, "Cashier1", self.branch.id))
            line = doc.items.get()
            self.assertEqual((line.product_id, line.product_name, line.price),
                             (self.cake.id, "Mango Cake", 0))
        self.assertEqual((docs[2].items.get().before_qty, docs[2].items.get().reconcile_qty),
                         (Decimal(12), Decimal(20)))
        # The stock moved when the entry was saved; nothing moves again.
        self.cake.refresh_from_db()
        self.assertEqual((self.cake.stock, StockMovement.objects.count()), (stock, moves))

    def test_running_it_again_files_nothing_twice(self):
        self.entry("in", 5)
        self.entry("adjust", 2)
        run_migration()
        run_migration()
        self.assertEqual(self.migrated().count(), 2)

    def test_two_entries_in_the_same_second_are_both_filed(self):
        other = make_product(self.branch, name="Pie", stock=4)
        a = self.entry("in", 1)
        b = self.entry("in", 2, product=other)
        StockMovement.objects.filter(pk=b.pk).update(document_no=a.document_no)
        run_migration()
        self.assertEqual(self.migrated().count(), 2)

    def test_an_adjust_with_no_audit_row_is_skipped(self):
        StockMovement.objects.create(branch=self.branch, product=self.cake, type="adjust",
                                     qty=99, document_no="SM261119000099")
        run_migration()
        self.assertFalse(self.migrated().exists())

    def test_other_ledger_rows_are_left_alone(self):
        StockMovement.objects.create(branch=self.branch, product=self.cake, type="out",
                                     qty=2, document_no="#261119-ABCD1234")
        StockMovement.objects.create(branch=self.branch, product=self.cake, type="adjust",
                                     qty=-3, note="Opening stock")
        run_migration()
        self.assertFalse(self.migrated().exists())

    def test_a_blank_staff_name_comes_from_the_audit_log(self):
        named = self.entry("in", 5)
        unknown = self.entry("out", 1, product=make_product(self.branch, name="Pie", stock=4))
        StockMovement.objects.update(created_by="", staff=None)   # before 26 Oct
        AuditLog.objects.filter(object_id=str(self.cake.id)).update(actor_label="Da")
        run_migration()
        self.assertEqual(set(self.migrated().values_list("created_by", flat=True)), {""})
        FILL_STAFF.forwards(_state_apps, SimpleNamespace(connection=connection))
        self.assertEqual(
            dict(self.migrated().values_list("document_no", "created_by")),
            {named.document_no: "Da", unknown.document_no: ""})

    def test_reverse_removes_only_the_filed_ones(self):
        self.entry("in", 5)
        kept = save_stock_in_document(self.branch, [(self.cake, Decimal(1), Decimal(0), Decimal(0))])
        run_migration()
        MIGRATION.backwards(_state_apps, SimpleNamespace(connection=connection))
        self.assertEqual(list(StockDocument.objects.values_list("id", flat=True)), [kept.id])

    def test_past_date_inventory_still_counts_the_change(self):
        self.entry("adjust", 4, days=5)          # 10 -> 4, five days ago
        run_migration()
        self.cake.refresh_from_db()
        before = history.rewind_stock([self.cake], timezone.localdate() - timedelta(days=6))
        self.assertEqual(before[0].stock, 10)
        self.cake.refresh_from_db()
        after = history.rewind_stock([self.cake], timezone.localdate() - timedelta(days=4))
        self.assertEqual(after[0].stock, 4)

    def test_filed_today_they_do_not_set_off_the_repeat_alert(self):
        self.entry("in", 5)
        run_migration()
        StockDocumentAlertDay.objects.all().delete()
        with self.captureOnCommitCallbacks(execute=True):
            save_stock_in_document(self.branch, [(self.cake, Decimal(1), Decimal(0), Decimal(0))])
        self.assertEqual(StockDocumentAlertDay.objects.get(document_type="in").document_count, 1)
        self.send.assert_not_called()


class ReservedTypeTests(_Base):
    def setUp(self):
        super().setUp()
        self.token = BranchSession.objects.create(
            token="tk8" * 12, branch=self.branch, staff=self.staff).token
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}

    def test_any_case_or_spacing(self):
        for name in ("Stock Movement", "stock movement", "  STOCK   movement "):
            self.assertTrue(is_reserved_stock_type(name), name)
        for name in ("", None, "Stock Movements", "Movement"):
            self.assertFalse(is_reserved_stock_type(name), name)

    def test_backoffice_manage_types_refuses_it(self):
        sign_in(self.client, make_user(username="bo", email="bo@example.com", role="admin"))
        for url, model in (("stock_in_types", StockInReason), ("stock_out_types", StockOutReason)):
            res = self.client.post(reverse(f"backoffice:{url}") + f"?branch={self.branch.id}",
                                   {"name": "stock  Movement"})
            self.assertContains(res, "is reserved")
            self.assertFalse(model.objects.filter(name__iexact="stock  movement").exists())

    def test_till_types_api_refuses_it(self):
        for path in ("/api/stock-in-reasons", "/api/stock-out-reasons"):
            res = self.client.post(path, {"name": "Stock Movement"},
                                   content_type="application/json", **self.auth)
            self.assertEqual(res.status_code, 400, (path, res.content))
        row = StockInReason.objects.create(branch=self.branch, name="Purchase")
        res = self.client.patch(f"/api/stock-in-reasons/{row.id}", {"name": "STOCK MOVEMENT"},
                                content_type="application/json", **self.auth)
        self.assertEqual(res.status_code, 400)

    def test_branch_sync_refuses_it(self):
        for add in (branch_sync.add_stock_in_type, branch_sync.add_stock_out_type):
            with self.assertRaises(ValueError):
                add(self.branch, "Stock Movement", "")

    def test_till_stock_out_cannot_be_filed_under_it(self):
        res = self.client.post("/api/stock-documents", {
            "type": "out", "reason": "Stock Movement",
            "items": [{"product_id": str(self.cake.id), "qty": 1}]},
            content_type="application/json", **self.auth)
        self.assertEqual(res.status_code, 400)
        self.cake.refresh_from_db()
        self.assertEqual(self.cake.stock, 10)

    def test_a_filed_one_keeps_it_and_no_other_document_can_take_it(self):
        self.entry("out", 2)
        run_migration()
        filed = self.migrated().get()
        StockOutReason.objects.get_or_create(branch=self.branch, name="Expired")
        res = self.client.patch(f"/api/stock-documents/{filed.id}", {"note": "counted"},
                                content_type="application/json", **self.auth)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["reason"], STOCK_MOVEMENT_TYPE)
        other = StockDocument.objects.create(branch=self.branch, type="out",
                                             document_no="#1", reason="Expired")
        res = self.client.patch(f"/api/stock-documents/{other.id}", {"reason": STOCK_MOVEMENT_TYPE},
                                content_type="application/json", **self.auth)
        self.assertEqual(res.status_code, 400)


class EndpointTests(_Base):
    def test_post_is_refused_and_stock_stays(self):
        token = BranchSession.objects.create(
            token="tk9" * 12, branch=self.branch, staff=self.staff).token
        for kind in ("in", "out", "adjust"):
            res = self.client.post("/api/stock-movements", {
                "product_id": str(self.cake.id), "type": kind, "qty": 3},
                content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}")
            self.assertEqual(res.status_code, 400)
            self.assertIn("Stock-In", res.json()["detail"])
        self.cake.refresh_from_db()
        self.assertEqual(self.cake.stock, 10)
        self.assertFalse(StockMovement.objects.exists())
