"""Stock-in type defaults, management, API and immutable document snapshots."""
from importlib import import_module
from types import SimpleNamespace

from django.apps import apps
from django.db import connection
from django.test import Client, TestCase
from django.urls import reverse

from bravepos.models import (
    BranchSession, DEFAULT_STOCK_IN_REASONS, Staff, StockDocument,
    StockInReason, StockMovement,
)
from bravepos.tests.factories import make_branch, make_product, make_shop
from bravepos.tests.test_user_management_and_audit import make_user, sign_in


class StockInReasonTests(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch(name="Receiving")
        self.other = make_branch(name="Other")
        self.product = make_product(self.branch, stock=10)
        sign_in(self.client, make_user(username="receiving", email="receiving@test.local", role="admin"))
        self.url = reverse("backoffice:stock_in_types") + f"?branch={self.branch.id}"

    def test_new_branch_defaults_and_resave(self):
        self.assertEqual(list(self.branch.stock_in_reasons.values_list("name", flat=True)),
                         DEFAULT_STOCK_IN_REASONS)
        self.branch.save()
        self.assertEqual(self.branch.stock_in_reasons.count(), 3)

    def test_migration_seeds_existing_branches_without_changing_documents(self):
        StockInReason.objects.all().delete()
        doc = StockDocument.objects.create(branch=self.branch, type="in", document_no="old")
        migration = import_module("bravepos.migrations.0093_stock_in_reasons")
        migration.seed(apps, SimpleNamespace(connection=connection))
        for branch in (self.branch, self.other):
            self.assertEqual(list(branch.stock_in_reasons.values_list("name", flat=True)),
                             DEFAULT_STOCK_IN_REASONS)
        doc.refresh_from_db()
        self.assertEqual(doc.reason, "")

    def test_add_duplicate_and_sync(self):
        closed = make_branch(name="Closed")
        closed.active = False
        closed.save()
        StockInReason.objects.create(branch=self.other, name="customer return")
        response = self.client.post(self.url, {
            "name": "Customer Return", "name_th": "คืนสินค้า", "sync_all": "1",
        }, follow=True)
        self.assertContains(response, "Added “Customer Return”.")
        self.assertEqual(self.other.stock_in_reasons.filter(name__iexact="Customer Return").count(), 1)
        self.assertEqual(self.other.stock_in_reasons.get(name="customer return").name_th, "คืนสินค้า")
        self.assertFalse(closed.stock_in_reasons.filter(name="Customer Return").exists())
        self.assertContains(self.client.post(self.url, {"name": "customer return"}), "already a type")
        self.assertContains(self.client.post(self.url, {"name": " "}), "Enter a type name")

    def test_local_add_and_delete_do_not_touch_other_branch(self):
        self.client.post(self.url, {"name": "Local"})
        self.assertFalse(self.other.stock_in_reasons.filter(name="Local").exists())
        row = self.branch.stock_in_reasons.get(name="Shop Supply")
        self.client.get(reverse("backoffice:stock_in_type_delete", args=[row.id]))
        self.assertTrue(StockInReason.objects.filter(pk=row.pk).exists())
        self.client.post(reverse("backoffice:stock_in_type_delete", args=[row.id]))
        self.assertTrue(self.other.stock_in_reasons.filter(name="Shop Supply").exists())

    def test_type_required_and_form_values_preserved(self):
        url = reverse("backoffice:stock_in_new") + f"?branch={self.branch.id}"
        for reason in ("", "Expired", "Hidden", "Elsewhere"):
            self.branch.stock_in_reasons.get_or_create(name="Hidden", defaults={"active": False})
            self.other.stock_in_reasons.get_or_create(name="Elsewhere")
            response = self.client.post(url, {
                "reason": reason, "vendor": "Kitchen", "product_id": [str(self.product.id)],
                "qty": ["2"], "price": ["5"],
            })
            self.assertContains(response, "Choose a type.")
            self.assertContains(response, 'value="Kitchen"')
            self.assertEqual(response.context["posted_lines"][0]["qty"], "2")
        self.assertFalse(StockDocument.objects.exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)

    def test_saved_type_reaches_report_export_and_survives_synced_delete(self):
        url = reverse("backoffice:stock_in_new") + f"?branch={self.branch.id}"
        response = self.client.post(url, {
            "reason": "Shop Supply", "product_id": [str(self.product.id)], "qty": ["2"], "price": ["5"],
        }, follow=True)
        self.assertContains(response, "Shop Supply")
        doc = StockDocument.objects.get(type="in")
        self.assertEqual(doc.reason, "Shop Supply")
        self.assertEqual(StockMovement.objects.get().note, "Shop Supply")
        row = self.branch.stock_in_reasons.get(name="Shop Supply")
        self.client.post(reverse("backoffice:stock_in_type_delete", args=[row.id]), {"sync_all": "1"})
        self.assertFalse(StockInReason.objects.filter(name="Shop Supply").exists())
        doc.refresh_from_db()
        self.assertEqual(doc.reason, "Shop Supply")
        self.assertContains(self.client.get(reverse("backoffice:stock_in")), "Shop Supply")
        export = self.client.get(reverse("backoffice:stock_in_export")).content.decode("utf-8-sig")
        self.assertIn("Type", export)
        self.assertIn("Shop Supply", export)

    def test_viewer_and_packer_cannot_manage(self):
        for role in ("viewer", "packer"):
            client = Client()
            sign_in(client, make_user(username=role, email=f"{role}@test.local", role=role))
            self.assertEqual(client.get(self.url).status_code, 403)
            self.assertEqual(client.post(self.url, {"name": "No"}).status_code, 403)
            row = self.branch.stock_in_reasons.first()
            self.assertEqual(client.post(reverse("backoffice:stock_in_type_delete", args=[row.id])).status_code, 403)


class StockInReasonApiTests(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch()
        self.product = make_product(self.branch, stock=10)
        self.staff = Staff.objects.create(name="Stock Admin", role="admin")
        self.staff.branches.add(self.branch)
        session = BranchSession.objects.create(branch=self.branch, staff=self.staff, token="stock-in-tests")
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {session.token}"}

    def post_document(self, **extra):
        return self.client.post("/api/stock-documents", {
            "type": "in", "items": [{"product_id": str(self.product.id), "qty": 2}], **extra,
        }, content_type="application/json", **self.auth)

    def test_session_and_branch_scoping_and_active_filter(self):
        self.assertEqual(self.client.get("/api/stock-in-reasons").status_code, 401)
        other = make_branch(name="Other")
        foreign = StockInReason.objects.create(branch=other, name="Foreign")
        self.branch.stock_in_reasons.filter(name="Shop Supply").update(active=False)
        response = self.client.get("/api/stock-in-reasons?active=true", **self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r["name"] for r in response.json()], ["Entry Correction", "Branch Transfer"])
        self.assertEqual(self.client.delete(f"/api/stock-in-reasons/{foreign.id}", **self.auth).status_code, 404)

    def test_receipt_snapshot_and_old_client_compatibility(self):
        response = self.post_document(reason="Branch Transfer")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["reason"], "Branch Transfer")
        self.branch.stock_in_reasons.filter(name="Branch Transfer").update(name="Transfer Receipt")
        self.assertEqual(StockDocument.objects.get().reason, "Branch Transfer")
        self.assertEqual(StockMovement.objects.get().note, "Branch Transfer")
        self.assertEqual(self.post_document().status_code, 201)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 14)

    def test_invalid_hidden_or_foreign_type_does_not_move_stock(self):
        self.branch.stock_in_reasons.filter(name="Shop Supply").update(active=False)
        other = make_branch(name="Other")
        StockInReason.objects.create(branch=other, name="Foreign")
        for reason in ("Expired", "Shop Supply", "Foreign"):
            self.assertEqual(self.post_document(reason=reason).status_code, 400)
        self.assertFalse(StockDocument.objects.exists())
        self.assertFalse(StockMovement.objects.exists())

    def test_inventory_permission_gates_type_writes(self):
        self.staff.role = "cashier"
        self.staff.permissions = []
        self.staff.save()
        response = self.client.post("/api/stock-in-reasons", {"name": "No"}, content_type="application/json", **self.auth)
        self.assertEqual(response.status_code, 403, response.content)
