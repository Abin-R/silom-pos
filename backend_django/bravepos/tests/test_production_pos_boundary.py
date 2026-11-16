"""Independent receiving uses the POS, never production request figures."""
from __future__ import annotations

import datetime

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from bravepos import production
from bravepos.models import Branch, Product, StockDocument, StockMovement
from bravepos.tests.test_user_management_and_audit import PASSWORD, make_user, sign_in


class ProductionPOSBoundaryTests(TestCase):
    production_pages = (
        "production_request", "production_requests", "production_kitchen",
        "production_kitchen_export", "production_packing", "production_packing_export",
    )

    def setUp(self):
        self.branch = Branch.objects.create(name="Receiving boundary")
        self.product = Product.objects.create(
            branch=self.branch, name="Counted cookie", barcode="boundary-cookie", stock=5,
        )
        self.day = timezone.localdate() + datetime.timedelta(days=3)
        self.request = production.save_request(
            self.branch, self.day, {str(self.product.pk): 12},
        )
        self.item = self.request.items.get()
        production.save_packed(self.day, {str(self.item.pk): 7})
        self.cashier = make_user(
            username="receiving", email="receiving@example.com",
            role="cashier", backoffice_access=False,
        )
        self.cashier.branches.add(self.branch)
        self.cashier.set_pin("4321")
        self.cashier.save()
        self.api = APIClient()
        login = self.api.post("/api/auth/pin-login", {
            "branch_id": str(self.branch.pk), "staff_id": str(self.cashier.pk),
            "pin": "4321",
        }, format="json")
        self.assertEqual(login.status_code, 200, login.content)
        self.token = login.json()["token"]
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}")

    def assert_production_unavailable(self, client):
        for name in self.production_pages:
            url = reverse(f"backoffice:{name}")
            with self.subTest(name=name):
                self.assertEqual(client.get(url).status_code, 302)
                self.assertEqual(client.post(url, {}).status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual((self.item.requested_qty, self.item.packed_qty), (12, 7))

    def test_real_pos_token_does_not_authenticate_any_production_page(self):
        self.assert_production_unavailable(self.api)

    def test_pos_cashier_cannot_use_backoffice_password_login(self):
        client = Client()
        response = client.post(reverse("backoffice:login"), {
            "username": self.cashier.username, "password": PASSWORD,
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", client.session)
        self.assert_production_unavailable(client)

    def test_revoking_backoffice_access_ends_existing_session_but_not_pos_receiving(self):
        self.cashier.backoffice_access = True
        self.cashier.save(update_fields=["backoffice_access"])
        client = Client()
        sign_in(client, self.cashier)
        self.cashier.backoffice_access = False
        self.cashier.save(update_fields=["backoffice_access"])
        self.assert_production_unavailable(client)
        self.assertEqual(self.api.get("/api/stock-documents").status_code, 200)

    def test_stock_in_records_only_the_cashiers_independent_count(self):
        self.assertFalse(StockDocument.objects.exists())
        self.assertFalse(StockMovement.objects.exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        response = self.api.post("/api/stock-documents", {
            "type": "in", "items": [{
                "product_id": str(self.product.pk), "qty": 3, "price": "0", "total": "0",
            }],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 8)
        self.assertEqual(StockDocument.objects.get().items.get().qty, 3)
        self.item.refresh_from_db()
        self.assertEqual((self.item.requested_qty, self.item.packed_qty), (12, 7))

    def test_pos_responses_do_not_offer_requested_or_packed_quantities(self):
        forbidden = {"requested_qty", "packed_qty", "production_requests", "production_request_items"}

        def check(value):
            if isinstance(value, dict):
                self.assertFalse(forbidden.intersection(value))
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)

        for url in ("/api/products", "/api/stock-documents", "/api/settings", "/api/auth/me"):
            with self.subTest(url=url):
                response = self.api.get(url)
                self.assertEqual(response.status_code, 200)
                check(response.json())

    def test_authorized_manager_keeps_production_oversight(self):
        manager = make_user(username="manager", email="manager@example.com", role="cashier")
        client = Client()
        sign_in(client, manager)
        for name in self.production_pages:
            with self.subTest(name=name):
                self.assertEqual(client.get(reverse(f"backoffice:{name}")).status_code, 200)
