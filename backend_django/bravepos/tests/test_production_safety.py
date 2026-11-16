"""Focused service-level safety checks for production request writes."""
import datetime
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from bravepos import production
from bravepos.models import Branch, Product, ProductionRequest, ProductionRequestItem


def open_day():
    return timezone.localdate() + datetime.timedelta(days=3)


class ProductionSafetyTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="Alpha")
        self.other_branch = Branch.objects.create(name="Beta")
        self.product = Product.objects.create(branch=self.branch, name="Cookie", barcode="one")
        self.other_product = Product.objects.create(
            branch=self.other_branch, name="Other cookie", barcode="other",
        )
        self.day = open_day()

    def request(self, wanted, **kwargs):
        return production.save_request(self.branch, self.day, wanted, **kwargs)

    def test_strict_empty_baseline_creates_identity_and_stales_another_tab(self):
        req = self.request(
            {str(self.product.id): 3},
            expected_request_id=None,
            expected_revision=0,
            strict=True,
        )
        self.assertEqual(req.revision, 1)
        with self.assertRaises(production.RevisionConflict):
            self.request(
                {str(self.product.id): 4},
                expected_request_id=None,
                expected_revision=0,
                strict=True,
            )

    def test_clearing_keeps_item_and_parent_tombstones(self):
        req = self.request({str(self.product.id): 3})
        item = req.items.get()
        result = self.request(
            {f"item:{item.id}": 0},
            expected_request_id=str(req.id),
            expected_revision=req.revision,
            strict=True,
        )
        self.assertIsNone(result)
        self.assertTrue(ProductionRequest.objects.filter(pk=req.pk).exists())
        item.refresh_from_db()
        self.assertEqual((item.requested_qty, item.packed_qty, item.revision), (0, None, 2))

    def test_open_form_uses_snapshot_and_refuses_reused_barcode(self):
        req = self.request({str(self.product.id): 3})
        item = req.items.get()
        self.product.name = "Renamed cookie"
        self.product.barcode = "two"
        self.product.save(update_fields=["name", "barcode"])
        reused = Product.objects.create(branch=self.branch, name="Replacement", barcode="one")

        rows = production.request_form_rows(self.branch, req)
        saved = next(row for row in rows if row["key"] == f"item:{item.id}")
        replacement = next(row for row in rows if row["key"] == str(reused.id))
        self.assertEqual(saved["name"], "Cookie")
        self.assertTrue(replacement["unavailable"])
        with self.assertRaises(production.CatalogueConflict):
            self.request(
                {str(reused.id): 2},
                expected_request_id=str(req.id),
                expected_revision=req.revision,
                strict=True,
            )

    def test_packing_stale_cell_rejects_entire_changed_batch(self):
        second = Product.objects.create(branch=self.branch, name="Brownie", barcode="brownie")
        req = self.request({str(self.product.id): 3, str(second.id): 4})
        first, other = req.items.order_by("product_name")
        first_revision = first.revision
        other_revision = other.revision
        # Establish a newer version for one cell.
        self.assertEqual(production.save_packed(
            self.day, {str(first.id): 2}, expected_revisions={str(first.id): first_revision}, strict=True,
        ), 1)
        with self.assertRaises(production.RevisionConflict):
            production.save_packed(
                self.day,
                {str(first.id): 1, str(other.id): 4},
                expected_revisions={str(first.id): first_revision, str(other.id): other_revision},
                strict=True,
            )
        first.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((first.packed_qty, other.packed_qty), (2, None))

    def test_packing_rejects_a_tombstoned_unpacked_cell(self):
        req = self.request({str(self.product.id): 3})
        item = req.items.get()
        self.request({f"item:{item.id}": 0})
        item.refresh_from_db()
        with self.assertRaises(production.InvalidRows):
            production.save_packed(
                self.day, {str(item.id): 1},
                expected_revisions={str(item.id): item.revision}, strict=True,
            )
        item.refresh_from_db()
        self.assertIsNone(item.packed_qty)

    def test_integrity_command_reports_without_repairing_links(self):
        req = self.request({str(self.product.id): 3})
        item = req.items.get()
        # This is deliberately suspect evidence, created directly to make sure
        # the diagnostic reports rather than "fixes" it.
        item.product = self.other_product
        item.save(update_fields=["product"])
        output = StringIO()
        call_command("check_production_integrity", stdout=output)
        item.refresh_from_db()
        self.assertEqual(item.product_id, self.other_product.id)
        self.assertIn("cross-branch product link", output.getvalue())

    def test_integrity_command_reports_a_bounded_incomplete_inspection(self):
        another = Product.objects.create(branch=self.branch, name="Brownie", barcode="two")
        self.request({str(self.product.id): 3, str(another.id): 4})
        output = StringIO()
        call_command("check_production_integrity", limit=1, stdout=output)
        self.assertIn("Inspected 1 production item(s)", output.getvalue())
        self.assertIn("INCOMPLETE", output.getvalue())
        self.assertEqual(ProductionRequestItem.objects.count(), 2)

    def test_integrity_command_can_narrow_the_database_scope(self):
        self.request({str(self.product.id): 3})
        production.save_request(self.other_branch, self.day, {str(self.other_product.id): 4})
        output = StringIO()
        call_command("check_production_integrity", branch=self.branch.pk,
                     delivery_date=self.day, stdout=output)
        self.assertIn("Inspected 1 production item(s)", output.getvalue())
        self.assertNotIn("INCOMPLETE", output.getvalue())
