"""Every backoffice page and every form, against data that is legal but ugly.

The existing smoke test builds a tidy shop — every product has a category, a
unit and a branch; every order has a customer — and so it proved only that
the pages work on data that never occurs. A single uncategorised product was
enough to 500 the live Inventory page, because a Django filter *argument*
that dereferences a null FK raises `VariableDoesNotExist` instead of
resolving to empty, and no tidy fixture ever hits that path.

So this suite is deliberately hostile. Every nullable FK is actually null
somewhere, every optional string is blank somewhere, and there is a product
that has never sold, an order with no lines, a customer who has never bought
and a shift nobody closed. Then it walks every page, every tab and filter
variant, every export, and POSTs every form.

Run:
    python manage.py test bravepos.tests.test_backoffice_hostile_data \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

import re

from decimal import Decimal

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from bravepos.models import (
    AuditLog, Branch, Category, Customer, Order, OrderItem, Product,
    Settings, Shift, Staff, StockMovement, Unit,
)


class HostileDataMixin:
    """A shop where everything optional is missing from something."""

    @classmethod
    def build_shop(cls):
        cls.password = "correct-horse-battery"
        cls.admin = Staff(
            name="Hostile Admin", username="hostile",
            email="hostile@therollingpinn.com", role="admin",
            active=True, backoffice_access=True,
        )
        cls.admin.set_password(cls.password)
        cls.admin.save()

        Settings.objects.get_or_create(id="shop")
        cls.branch = Branch.objects.create(name="Hostile Branch", code="HST")
        cls.other_branch = Branch.objects.create(name="Second Branch")
        cls.admin.branches.add(cls.branch)

        cls.category = Category.objects.create(name="Bakery", branch=cls.branch)
        cls.unit = Unit.objects.create(name="ชิ้น", branch=cls.branch)

        # A well-formed product, so the happy path is still covered.
        cls.good = Product.objects.create(
            name="Butter Croissant", name_th="ครัวซองต์", branch=cls.branch,
            category=cls.category, unit=cls.unit, price=Decimal("75"),
            cost=Decimal("28"), stock=4, par_level=36, sku="BK-001",
        )
        # The one that broke production: no category, no unit, no Thai name,
        # no SKU, no barcode, no par level, never sold, zero price.
        cls.bare = Product.objects.create(
            name="Uncategorised Thing", branch=cls.branch,
            category=None, unit=None, price=Decimal("0"), cost=Decimal("0"),
            stock=0, par_level=0,
        )
        # Not attached to any branch at all.
        cls.orphan = Product.objects.create(
            name="Branchless Product", branch=None, category=None, unit=None,
            price=Decimal("50"), cost=Decimal("10"), stock=7,
        )

        # A customer who has never bought anything, with no branch and no phone.
        cls.silent_customer = Customer.objects.create(name="Never Bought")
        cls.customer = Customer.objects.create(
            name="Khun Ploy", phone="0812345678", branch=cls.branch,
        )

        cls.open_shift = Shift.objects.create(
            branch=cls.branch, status="open", opened_by="Nok",
            total_sales_cash=Decimal("1400"),
        )
        # Closed but never counted — `actual_in_drawer` stays null.
        Shift.objects.create(
            branch=cls.branch, status="closed", opened_by="Ploy",
            closed_at=timezone.now(), expected_in_drawer=Decimal("900"),
            actual_in_drawer=None,
        )

        now = timezone.now()
        # A normal paid bill.
        cls.order = Order.objects.create(
            branch=cls.branch, order_number="HST-0001",
            subtotal=Decimal("150"), total=Decimal("150"),
            vat_amount=Decimal("9.81"), payment_method="cash",
            status="completed", customer=cls.customer, staff="Nok",
            created_at=now,
        )
        OrderItem.objects.create(
            order=cls.order, product=cls.good, name=cls.good.name,
            category_name="Bakery", price=Decimal("75"), qty=2,
        )
        # No branch, no customer, no cashier, no payment method, no lines.
        cls.bare_order = Order.objects.create(
            branch=None, order_number="HST-0002",
            subtotal=Decimal("0"), total=Decimal("0"),
            payment_method="", status="completed", customer=None, staff="",
            created_at=now,
        )
        # A void, so refund/void branches render.
        cls.void_order = Order.objects.create(
            branch=cls.branch, order_number="HST-0003",
            subtotal=Decimal("75"), total=Decimal("75"),
            payment_method="promptpay", status="cancel", created_at=now,
        )
        # A line whose product was deleted since — product_id goes null.
        deleted_source = Product.objects.create(
            name="Since Deleted", branch=cls.branch, price=Decimal("10"),
        )
        OrderItem.objects.create(
            order=cls.order, product=deleted_source, name="Since Deleted",
            price=Decimal("10"), qty=1,
        )
        deleted_source.delete()

        StockMovement.objects.create(
            branch=cls.branch, product=cls.good, type="in", qty=24,
        )

        # Audit rows in all three change shapes, plus a null actor.
        AuditLog.objects.create(
            actor=cls.admin, actor_label=cls.admin.name, actor_role="admin",
            action="update", model="Product", object_label="Butter Croissant",
            branch=cls.branch, source="backoffice", method="POST",
            changes={"price": {"from": "70.00", "to": "75.00"}},
        )
        AuditLog.objects.create(
            actor=None, actor_label="", action="login_failed",
            model="", object_label="", source="backoffice", changes={},
        )
        AuditLog.objects.create(
            actor=cls.admin, actor_label=cls.admin.name, action="create",
            model="Shift", object_label="abc123", branch=None,
            changes={"status": "open", "closed_by": None, "note": ""},
        )


class HostilePageTests(HostileDataMixin, TestCase):
    """Nothing 5xxs, whatever the data looks like."""

    @classmethod
    def setUpTestData(cls):
        cls.build_shop()

    def setUp(self):
        ok = self.client.login(username="hostile", password=self.password)
        self.assertTrue(ok, "hostile admin could not sign in")

    def _get(self, url, **params):
        response = self.client.get(url, params)
        self.assertLess(
            response.status_code, 500,
            f"{url} {params} -> {response.status_code}",
        )
        return response

    def test_every_page_survives_null_relations(self):
        scope = {"branch": str(self.branch.id)}
        for name in [
            "dashboard", "transactions", "report_daily", "report_sell",
            "report_sku", "report_tax", "inventory", "product_list",
            "category_list", "unit_list", "staff_list", "customer_list",
            "branch_list", "user_list", "audit_log", "shop_settings",
            "product_new", "product_bulk_add", "product_bulk_edit",
            "category_new", "unit_new", "staff_new", "branch_new", "user_new",
        ]:
            with self.subTest(page=name):
                self._get(reverse(f"backoffice:{name}"), **scope)

    def test_pages_without_a_branch_filter(self):
        """No `?branch=` at all — the views fall back to the first branch."""
        for name in ["dashboard", "inventory", "product_list", "report_sku",
                     "customer_list", "transactions"]:
            with self.subTest(page=name):
                self._get(reverse(f"backoffice:{name}"))

    def test_inventory_renders_a_product_with_no_category(self):
        """The exact production 500: a filter argument dereferencing a null FK."""
        page = self._get(reverse("backoffice:inventory"),
                         branch=str(self.branch.id), level="all")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Uncategorised Thing")

    def test_every_inventory_tab(self):
        for level in ["attention", "out", "low", "all"]:
            with self.subTest(level=level):
                self._get(reverse("backoffice:inventory"),
                          branch=str(self.branch.id), level=level)

    def test_every_transactions_filter(self):
        scope = {"branch": str(self.branch.id)}
        for params in [
            {"status": "all"}, {"status": "paid"}, {"status": "voided"},
            {"payment": "Cash"}, {"payment": "Card"},
            {"q": "HST-0001"}, {"q": "150"}, {"q": "nonsense"},
            {"q": "0812345678"},
            {"o": str(self.bare_order.id)},
        ]:
            with self.subTest(**params):
                self._get(reverse("backoffice:transactions"), **scope, **params)

    def test_every_customer_tier(self):
        for tier in ["all", "members", "regulars", "lapsed", "new"]:
            with self.subTest(tier=tier):
                self._get(reverse("backoffice:customer_list"),
                          branch=str(self.branch.id), tier=tier)

    def test_customer_search_and_selection(self):
        self._get(reverse("backoffice:customer_list"), q="Ploy")
        self._get(reverse("backoffice:customer_list"), q="zzz-no-match")
        self._get(reverse("backoffice:customer_list"),
                  c=str(self.silent_customer.id))

    def test_catalogue_filters(self):
        scope = {"branch": str(self.branch.id)}
        self._get(reverse("backoffice:product_list"), **scope,
                  cat=str(self.category.id))
        self._get(reverse("backoffice:product_list"), **scope, q="Croissant")
        for sort in ["name", "price_max", "newest", "category"]:
            with self.subTest(sort=sort):
                self._get(reverse("backoffice:product_list"), **scope, sort=sort)

    def test_audit_filters(self):
        for params in [
            {"action": "update"}, {"action": "login_failed"},
            {"model": "Product"}, {"actor": str(self.admin.id)},
            {"branch": str(self.branch.id)}, {"q": "Croissant"},
            {"from": "2020-01-01", "to": "2030-01-01"},
            {"from": "not-a-date"},
        ]:
            with self.subTest(**params):
                self._get(reverse("backoffice:audit_log"), **params)

    def test_detail_pages(self):
        scope = {"branch": str(self.branch.id)}
        for name, arg in [
            ("product_detail", self.bare.id),
            ("product_detail", self.orphan.id),
            ("category_detail", self.category.id),
            ("unit_detail", self.unit.id),
            ("branch_detail", self.branch.id),
            ("staff_detail", self.admin.id),
            ("user_detail", self.admin.id),
            ("customer_detail", self.silent_customer.id),
            ("report_daily_detail", timezone.localdate().isoformat()),
            ("receipt_print", self.bare_order.order_number),
        ]:
            with self.subTest(page=name, arg=arg):
                self._get(reverse(f"backoffice:{name}", args=[arg]), **scope)

    def test_every_export(self):
        scope = {"branch": str(self.branch.id)}
        for name in ["transactions_export", "report_daily_export",
                     "report_sell_export", "report_sku_export",
                     "report_tax_export", "inventory_export",
                     "audit_log_export"]:
            with self.subTest(export=name):
                response = self._get(reverse(f"backoffice:{name}"), **scope)
                self.assertEqual(response.status_code, 200)

    def test_dashboard_on_an_empty_window(self):
        """A date range with no trade at all — every divisor is zero."""
        self._get(reverse("backoffice:dashboard"),
                  branch=str(self.branch.id),
                  **{"from": "2019-01-01", "to": "2019-01-07"})
        self._get(reverse("backoffice:report_daily"),
                  branch=str(self.branch.id),
                  **{"from": "2019-01-01", "to": "2019-01-07"})
        self._get(reverse("backoffice:report_sku"),
                  branch=str(self.branch.id),
                  **{"from": "2019-01-01", "to": "2019-01-07"})

    def test_reversed_and_single_day_windows(self):
        today = timezone.localdate().isoformat()
        self._get(reverse("backoffice:dashboard"), **{"from": today, "to": today})
        # `to` before `from` — the views swap them rather than returning nothing.
        self._get(reverse("backoffice:dashboard"),
                  **{"from": "2030-01-01", "to": "2020-01-01"})


class HostileFormTests(HostileDataMixin, TestCase):
    """Every form actually saves, and the change lands in the database."""

    @classmethod
    def setUpTestData(cls):
        cls.build_shop()

    def setUp(self):
        self.assertTrue(
            self.client.login(username="hostile", password=self.password))
        self.scope = f"?branch={self.branch.id}"

    def _post(self, url, data):
        response = self.client.post(url, data)
        self.assertLess(response.status_code, 500,
                        f"{url} -> {response.status_code}")
        return response

    def test_create_and_edit_a_product(self):
        self._post(
            reverse("backoffice:product_new") + self.scope,
            {"name": "New Cookie", "price": "45", "cost": "12", "stock": "10",
             "par_level": "20", "category": str(self.category.id),
             "unit": str(self.unit.id), "product_type": "P", "tax_type": "V",
             "sku": "CK-999", "barcode": "", "name_th": "", "image_url": ""},
        )
        created = Product.objects.get(name="New Cookie")
        self.assertEqual(created.par_level, 20, "par level was not saved")

        self._post(
            reverse("backoffice:product_detail", args=[created.id]) + self.scope,
            {"name": "Renamed Cookie", "price": "50", "cost": "12",
             "stock": "10", "par_level": "8", "category": "", "unit": "",
             "product_type": "P", "tax_type": "V", "sku": "", "barcode": "",
             "name_th": "", "image_url": ""},
        )
        created.refresh_from_db()
        self.assertEqual(created.name, "Renamed Cookie")
        self.assertEqual(created.par_level, 8)
        self.assertIsNone(created.category_id, "clearing the category failed")

        # And the catalogue still renders it with no category.
        page = self.client.get(reverse("backoffice:inventory"),
                               {"branch": str(self.branch.id), "level": "all"})
        self.assertEqual(page.status_code, 200)

    def test_create_a_category_and_a_unit(self):
        self._post(reverse("backoffice:category_new") + self.scope,
                   {"name": "Pastry", "order": "2", "color": "#2563EB",
                    "name_th": "", "active": "on"})
        self.assertTrue(Category.objects.filter(name="Pastry").exists())

        self._post(reverse("backoffice:unit_new") + self.scope,
                   {"name": "Box", "order": "3", "active": "on"})
        self.assertTrue(Unit.objects.filter(name="Box").exists())

    def test_create_and_edit_staff(self):
        self._post(reverse("backoffice:staff_new") + self.scope,
                   {"name": "New Cashier", "role": "cashier", "pin": "4321",
                    "active": "on"})
        member = Staff.objects.get(name="New Cashier")
        self._post(
            reverse("backoffice:staff_detail", args=[member.id]) + self.scope,
            {"name": "Renamed Cashier", "role": "cashier", "pin": "",
             "active": "on"},
        )
        member.refresh_from_db()
        self.assertEqual(member.name, "Renamed Cashier")

    def test_create_a_backoffice_user(self):
        response = self._post(reverse("backoffice:user_new"), {
            "name": "Accounting", "role": "cashier", "username": "accounting",
            "email": "accounting@therollingpinn.com", "password": "",
            "active": "on", "branches": [str(self.branch.id)],
        })
        self.assertEqual(response.status_code, 302, "user was not created")
        user = Staff.objects.get(username="accounting")
        self.assertTrue(user.backoffice_access)
        # The generated password is handed over exactly once, on the next page.
        page = self.client.get(reverse("backoffice:user_list"))
        self.assertContains(page, "Credentials for")

    def test_edit_a_customer(self):
        self._post(
            reverse("backoffice:customer_detail", args=[self.customer.id]),
            {"name": "Khun Ploy", "last_name": "Chai", "phone": "0899999999",
             "email": ""},
        )
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.phone, "0899999999")

    def test_edit_a_branch(self):
        self._post(reverse("backoffice:branch_detail", args=[self.branch.id]), {
            "name": "Hostile Branch", "code": "HST", "tax_id": "", "pos_id": "",
            "address": "12 Test Road", "phone": "021234567", "logo_url": "",
            "open_time": "07:00", "close_time": "20:00",
            "peak_account_code": "BSV003", "active": "on",
            "payment_mode": "test", "beam_merchant_id": "", "beam_api_key": "",
            "beam_card_fee_percent": "3.65", "omise_public_key": "",
            "omise_secret_key": "", "omise_fee_percent": "3.65",
        })
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.open_time, "07:00")

    def test_save_shop_settings(self):
        self._post(reverse("backoffice:shop_settings") + self.scope, {
            "shop_name": "The Rolling Pinn", "business_type": "Bakery",
            "company_name": "The Rolling Pinn Co., Ltd.", "currency": "THB",
            "open_time": "07:00", "close_time": "20:00",
            "tax_mode": "inclusive", "tax_percent": "7", "service_charge": "0",
            "branch_name": "Hostile Branch", "tax_id": "0105561000000",
            "phone": "021234567", "address_line_1": "12 Test Road",
            "address_line_2": "", "logo_url": "",
            "payment_mode": "test", "beam_merchant_id": "", "beam_api_key": "",
            "beam_card_fee_percent": "3.65", "omise_public_key": "",
            "omise_secret_key": "", "omise_fee_percent": "3.65",
        })
        settings_row = Settings.objects.first()
        self.assertEqual(settings_row.company_name, "The Rolling Pinn Co., Ltd.")

    def test_bulk_add_and_bulk_edit(self):
        self._post(reverse("backoffice:product_bulk_add") + self.scope, {
            "branch": str(self.branch.id),
            "barcode": ["", ""], "name": ["Bulk One", ""],
            "description": ["", ""], "category": ["", ""], "unit": ["", ""],
            "price": ["12", "0"], "product_type": ["P", "P"],
        })
        self.assertTrue(Product.objects.filter(name="Bulk One").exists(),
                        "bulk add saved nothing")

        self._post(reverse("backoffice:product_bulk_edit") + self.scope, {
            "id": [str(self.good.id)], "barcode": [""],
            "name": ["Butter Croissant"], "description": [""],
            "category": [str(self.category.id)], "unit": [str(self.unit.id)],
            "price": ["80"], "cost": ["28"],
        })
        self.good.refresh_from_db()
        self.assertEqual(self.good.price, Decimal("80"),
                         "bulk edit did not save the price")

    def test_delete_a_category_leaves_its_products(self):
        doomed = Category.objects.create(name="Doomed", branch=self.branch)
        product = Product.objects.create(
            name="Orphan Soon", branch=self.branch, category=doomed,
            price=Decimal("10"),
        )
        self._post(reverse("backoffice:category_delete", args=[doomed.id]), {})
        product.refresh_from_db()
        self.assertIsNone(product.category_id)
        # And the pages that show it still render.
        for name in ["inventory", "product_list", "report_sku"]:
            page = self.client.get(reverse(f"backoffice:{name}"),
                                   {"branch": str(self.branch.id), "level": "all"})
            self.assertEqual(page.status_code, 200, name)

    def test_reset_a_user_password(self):
        response = self._post(
            reverse("backoffice:user_reset_password", args=[self.admin.id]), {})
        self.assertEqual(response.status_code, 302)


class HostileDestructiveTests(HostileDataMixin, TestCase):
    """Deletes, permission gates, pagination, and the pages a customer sees."""

    @classmethod
    def setUpTestData(cls):
        cls.build_shop()

    def setUp(self):
        self.assertTrue(
            self.client.login(username="hostile", password=self.password))

    def test_deleting_a_unit_leaves_its_products(self):
        unit = Unit.objects.create(name="Doomed Unit", branch=self.branch)
        product = Product.objects.create(
            name="Uses Doomed Unit", branch=self.branch, unit=unit,
            price=Decimal("10"),
        )
        response = self.client.post(
            reverse("backoffice:unit_delete", args=[unit.id]))
        self.assertLess(response.status_code, 500)
        product.refresh_from_db()
        self.assertIsNone(product.unit_id)
        page = self.client.get(reverse("backoffice:product_list"),
                               {"branch": str(self.branch.id)})
        self.assertEqual(page.status_code, 200)

    def test_deleting_staff_keeps_their_orders_readable(self):
        member = Staff.objects.create(name="Temp", email="temp@x.local")
        member.branches.add(self.branch)
        response = self.client.post(
            reverse("backoffice:staff_delete", args=[member.id]))
        self.assertLess(response.status_code, 500)
        self.assertFalse(Staff.objects.filter(id=member.id).exists())
        page = self.client.get(reverse("backoffice:transactions"),
                               {"branch": str(self.branch.id)})
        self.assertEqual(page.status_code, 200)

    def test_you_cannot_revoke_your_own_access(self):
        self.client.post(reverse("backoffice:user_delete", args=[self.admin.id]))
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.backoffice_access,
                        "signed-in admin revoked their own access")

    def test_a_cashier_is_refused_the_admin_pages(self):
        cashier = Staff(name="Cashier", username="till", email="till@x.local",
                        role="cashier", active=True, backoffice_access=True)
        cashier.set_password(self.password)
        cashier.save()
        # A fresh client, not `logout()`: Django's test-client logout hydrates
        # the user through the default auth backend, which int-coerces the
        # session PK and blows up on this project's UUID-keyed Staff.
        self.client = Client()
        self.assertTrue(self.client.login(username="till",
                                          password=self.password))
        for name in ["user_list", "audit_log"]:
            with self.subTest(page=name):
                response = self.client.get(reverse(f"backoffice:{name}"))
                self.assertLess(response.status_code, 500)
                self.assertNotEqual(response.status_code, 200,
                                    f"{name} let a cashier in")
        # …but the reports they are entitled to still work.
        for name in ["dashboard", "inventory", "transactions", "product_list"]:
            with self.subTest(page=name):
                self.assertEqual(
                    self.client.get(reverse(f"backoffice:{name}")).status_code,
                    200, f"{name} refused a cashier")

    def test_pagination_past_the_first_page(self):
        for i in range(60):
            Order.objects.create(
                branch=self.branch, order_number=f"PAGE-{i:04d}",
                subtotal=Decimal("10"), total=Decimal("10"),
                payment_method="cash", status="completed",
            )
        scope = {"branch": str(self.branch.id)}
        for name in ["transactions", "report_sell", "report_sku", "inventory",
                     "product_list"]:
            for page in ["2", "999", "not-a-number"]:
                with self.subTest(page=name, n=page):
                    response = self.client.get(
                        reverse(f"backoffice:{name}"), {**scope, "page": page,
                                                        "level": "all"})
                    self.assertEqual(response.status_code, 200,
                                     f"{name} page={page}")

    def test_the_stylesheet_and_customer_facing_pages(self):
        self.assertEqual(
            self.client.get(reverse("backoffice:app_css")).status_code, 200)
        # The QR on every printed receipt points here; it must not need a login.
        signed_out = Client()
        for name in ["customer_receipt", "create_tax_invoice"]:
            with self.subTest(page=name):
                response = signed_out.get(
                    reverse(name, args=[self.order.order_number]))
                self.assertLess(response.status_code, 500)

    def test_a_failed_sign_in_renders_the_error(self):
        response = Client().post(reverse("backoffice:login"), {
            "username": "hostile", "password": "wrong-password",
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Server Error", response.content)


class CataloguePageWeightTests(HostileDataMixin, TestCase):
    """Product photos must not be inlined into list markup.

    `public_views.product_image` exists because pasting base64 data: URIs into
    the customer menu made that page 648 KB. The backoffice catalogue repeated
    the mistake — 574 KB against ~20 KB for every other list — so this pins the
    fix: the bytes are served per-image and cached, not embedded.
    """

    @classmethod
    def setUpTestData(cls):
        cls.build_shop()
        # A 1x1 PNG as a data: URI, the shape the product form stores.
        cls.data_uri = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
            "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        cls.photo = Product.objects.create(
            name="Has A Photo", branch=cls.branch, price=Decimal("10"),
            image_url=cls.data_uri,
        )

    def setUp(self):
        self.assertTrue(
            self.client.login(username="hostile", password=self.password))

    def test_catalogue_does_not_inline_image_data(self):
        page = self.client.get(reverse("backoffice:product_list"),
                               {"branch": str(self.branch.id)})
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b"data:image", page.content,
                         "catalogue is inlining base64 image data again")
        self.assertIn(
            reverse("backoffice:product_image", args=[self.photo.id]).encode(),
            page.content, "catalogue does not link the image endpoint")

    def test_bulk_edit_does_not_inline_image_data(self):
        page = self.client.get(reverse("backoffice:product_bulk_edit"),
                               {"branch": str(self.branch.id)})
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b"data:image", page.content,
                         "quick edit is inlining base64 image data again")

    def test_the_image_endpoint_serves_and_caches(self):
        response = self.client.get(
            reverse("backoffice:product_image", args=[self.photo.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertIn("max-age", response["Cache-Control"])
        # A matching ETag means the browser keeps it rather than refetching.
        again = self.client.get(
            reverse("backoffice:product_image", args=[self.photo.id]),
            HTTP_IF_NONE_MATCH=response["ETag"])
        self.assertEqual(again.status_code, 304)

    def test_the_image_endpoint_404s_when_there_is_no_photo(self):
        response = self.client.get(
            reverse("backoffice:product_image", args=[self.bare.id]))
        self.assertEqual(response.status_code, 404)

    def test_a_hosted_url_redirects_to_its_origin(self):
        hosted = Product.objects.create(
            name="Hosted Photo", branch=self.branch, price=Decimal("10"),
            image_url="https://example.com/pic.jpg",
        )
        response = self.client.get(
            reverse("backoffice:product_image", args=[hosted.id]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://example.com/pic.jpg")

    def test_bulk_edit_still_saves_with_the_columns_deferred(self):
        self.client.post(
            reverse("backoffice:product_bulk_edit")
            + f"?branch={self.branch.id}",
            {"id": [str(self.photo.id)], "barcode": [""],
             "name": ["Has A Photo"], "description": [""], "category": [""],
             "unit": [""], "price": ["99"], "cost": ["5"]},
        )
        self.photo.refresh_from_db()
        self.assertEqual(self.photo.price, Decimal("99"))
        self.assertEqual(self.photo.image_url, self.data_uri,
                         "saving a deferred row wiped the photo")


class PaymentIsAdminOnlyTests(HostileDataMixin, TestCase):
    """Where the money lands is admin-only, to read and to write.

    Hiding the inputs is the cosmetic half. The half that matters is the
    write guard: a crafted POST from a cashier must not be able to swap the
    merchant account or flip a live branch into Test, which would leave every
    screen saying "paid" while no real money arrived.
    """

    @classmethod
    def setUpTestData(cls):
        cls.build_shop()
        cls.branch.beam_merchant_id = "merchant-xyzzy-7f3a"
        cls.branch.beam_api_key = "live-secret-key-quux9182"
        cls.branch.omise_public_key = "pkey_live_plugh4421"
        cls.branch.omise_secret_key = "skey_live_thud5566"
        cls.branch.beam_sandbox = False          # a LIVE branch
        cls.branch.save()

        cls.cashier = Staff(
            name="Till Cashier", username="tillcashier",
            email="till@therollingpinn.com", role="cashier",
            active=True, backoffice_access=True,
        )
        cls.cashier.set_password(cls.password)
        cls.cashier.save()
        cls.cashier.branches.add(cls.branch)

    def as_cashier(self):
        client = Client()
        self.assertTrue(client.login(username="tillcashier",
                                     password=self.password))
        return client

    def as_admin(self):
        client = Client()
        self.assertTrue(client.login(username="hostile",
                                     password=self.password))
        return client

    # ── Reading ─────────────────────────────────────────────────────────
    def test_a_cashier_sees_no_payment_details_anywhere(self):
        client = self.as_cashier()
        pages = [
            (reverse("backoffice:shop_settings"), {"branch": str(self.branch.id)}),
            (reverse("backoffice:branch_detail", args=[self.branch.id]), {}),
            (reverse("backoffice:branch_new"), {}),
            (reverse("backoffice:branch_list"), {}),
        ]
        # Everything that identifies the collecting account, including the
        # masked tail — four characters of a live key is still four characters.
        leaks = [b"merchant-xyzzy-7f3a", b"pkey_live_plugh4421", b"quux9182",
                 b"9182",  # the masked tail is still four live characters
                 b"beam_api_key", b"omise_secret_key", b"payment_mode"]
        for url, params in pages:
            response = client.get(url, params)
            self.assertEqual(response.status_code, 200, url)
            for leak in leaks:
                self.assertNotIn(leak, response.content,
                                 f"{url} leaked {leak!r} to a cashier")

    def test_an_admin_still_sees_the_payment_block(self):
        client = self.as_admin()
        response = client.get(reverse("backoffice:branch_detail",
                                      args=[self.branch.id]))
        self.assertContains(response, "beam_merchant_id")
        self.assertContains(response, "payment_mode")

    def test_the_branches_list_hides_the_lane_from_a_cashier(self):
        cashier_page = self.as_cashier().get(reverse("backoffice:branch_list"))
        self.assertNotContains(cashier_page, "Payment lane")
        admin_page = self.as_admin().get(reverse("backoffice:branch_list"))
        self.assertContains(admin_page, "Payment lane")

    # ── Writing ─────────────────────────────────────────────────────────
    def test_a_cashier_cannot_rewrite_a_branch_payment_account(self):
        """The attack this guard exists for: redirect the shop's takings.

        The payload is deliberately *valid* — a Live lane with live-format
        Omise keys — so `payment_errors` has no reason to refuse it. An
        invalid one would be rejected by validation and the test would pass
        whether or not the permission guard existed at all.
        """
        self.as_cashier().post(
            reverse("backoffice:branch_detail", args=[self.branch.id]), {
                "name": self.branch.name, "code": self.branch.code,
                "tax_id": "", "pos_id": "", "address": "", "phone": "",
                "logo_url": "", "open_time": "09:00", "close_time": "22:00",
                "peak_account_code": "BSV003", "active": "on",
                "payment_mode": "live",
                "beam_merchant_id": "attacker-merchant",
                "beam_api_key": "attacker-key",
                "beam_card_fee_percent": "99",
                "omise_public_key": "pkey_attacker",
                "omise_secret_key": "skey_attacker",
                "omise_fee_percent": "99",
            })
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.beam_merchant_id, "merchant-xyzzy-7f3a")
        self.assertEqual(self.branch.beam_api_key, "live-secret-key-quux9182")
        self.assertEqual(self.branch.omise_secret_key, "skey_live_thud5566")
        self.assertEqual(self.branch.beam_card_fee_percent, Decimal("3.65"))

    def test_a_cashier_cannot_flip_a_live_branch_into_test_mode(self):
        """The quieter attack: no key changes, just the lane.

        Clearing the Omise keys keeps the payload valid for a Test lane, so
        without the guard this save would succeed — and every screen would
        keep saying "paid" while no real money arrived.
        """
        self.as_cashier().post(
            reverse("backoffice:branch_detail", args=[self.branch.id]), {
                "name": self.branch.name, "code": self.branch.code,
                "tax_id": "", "pos_id": "", "address": "", "phone": "",
                "logo_url": "", "open_time": "09:00", "close_time": "22:00",
                "peak_account_code": "BSV003", "active": "on",
                "payment_mode": "test",
                "beam_merchant_id": self.branch.beam_merchant_id,
                "beam_api_key": "", "beam_card_fee_percent": "3.65",
                "omise_public_key": "", "omise_secret_key": "",
                "omise_secret_key_clear": "on", "omise_fee_percent": "3.65",
            })
        self.branch.refresh_from_db()
        self.assertFalse(self.branch.beam_sandbox,
                         "a cashier flipped a live branch into Test mode")

    def test_a_cashier_can_still_edit_the_rest_of_a_branch(self):
        """The guard is surgical: it takes payment, not the whole form."""
        self.as_cashier().post(
            reverse("backoffice:branch_detail", args=[self.branch.id]), {
                "name": self.branch.name, "code": self.branch.code,
                "tax_id": "", "pos_id": "", "address": "9 New Road",
                "phone": "021112222", "logo_url": "", "open_time": "06:30",
                "close_time": "21:00", "peak_account_code": "BSV003",
                "active": "on",
            })
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.open_time, "06:30")
        self.assertEqual(self.branch.phone, "021112222")

    def test_a_cashier_cannot_rewrite_the_shop_payment_template(self):
        settings_row = Settings.objects.first()
        settings_row.beam_merchant_id = "shop-merchant"
        settings_row.beam_api_key = "shop-key"
        settings_row.save()

        self.as_cashier().post(
            reverse("backoffice:shop_settings") + f"?branch={self.branch.id}", {
                "shop_name": "The Rolling Pinn", "business_type": "Bakery",
                "company_name": "", "currency": "THB", "open_time": "07:00",
                "close_time": "20:00", "tax_mode": "inclusive",
                "tax_percent": "7", "service_charge": "0",
                "branch_name": self.branch.name, "tax_id": "", "phone": "",
                "address_line_1": "", "address_line_2": "", "logo_url": "",
                # Valid on its own terms, so only the permission guard can
                # stop it — see the branch test above.
                "payment_mode": "live",
                "beam_merchant_id": "attacker-merchant",
                "beam_api_key": "attacker-key",
                "beam_card_fee_percent": "99",
                "omise_public_key": "", "omise_secret_key": "",
                "omise_fee_percent": "99",
            })
        settings_row.refresh_from_db()
        self.assertEqual(settings_row.beam_merchant_id, "shop-merchant")
        self.assertEqual(settings_row.beam_api_key, "shop-key")
        # …but their legitimate edits landed.
        self.assertEqual(settings_row.open_time, "07:00")

    def test_an_admin_can_still_change_payment(self):
        self.as_admin().post(
            reverse("backoffice:branch_detail", args=[self.branch.id]), {
                "name": self.branch.name, "code": self.branch.code,
                "tax_id": "", "pos_id": "", "address": "", "phone": "",
                "logo_url": "", "open_time": "09:00", "close_time": "22:00",
                "peak_account_code": "BSV003", "active": "on",
                "payment_mode": "live",
                "beam_merchant_id": "newmerchant",
                "beam_api_key": "", "beam_card_fee_percent": "3.65",
                "omise_public_key": "pkey_live_new", "omise_secret_key": "",
                "omise_fee_percent": "3.65",
            })
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.beam_merchant_id, "newmerchant",
                         "the guard also blocked an admin")


class StaffDeletionTests(HostileDataMixin, TestCase):
    """Deleting a till login: possible, guarded, and honest about what it costs.

    Both refusals are unrecoverable from inside the product — you cannot undo
    deleting the account you are signed in as, and you cannot promote anyone
    once the last admin is gone — so each is enforced in the view, not just
    greyed out in the template.
    """

    @classmethod
    def setUpTestData(cls):
        cls.build_shop()
        cls.second_admin = Staff(
            name="Second Admin", username="second", email="second@x.local",
            role="admin", active=True, backoffice_access=True,
        )
        cls.second_admin.set_password(cls.password)
        cls.second_admin.save()

    def setUp(self):
        self.assertTrue(
            self.client.login(username="hostile", password=self.password))

    def _cashier_client(self):
        cashier = Staff(name="Till", username="tilluser", email="till2@x.local",
                        role="cashier", active=True, backoffice_access=True)
        cashier.set_password(self.password)
        cashier.save()
        client = Client()
        self.assertTrue(client.login(username="tilluser", password=self.password))
        return client

    def test_the_form_offers_a_delete_button(self):
        member = Staff.objects.create(name="Deletable", email="del@x.local",
                                      role="cashier")
        page = self.client.get(
            reverse("backoffice:staff_detail", args=[member.id]))
        self.assertContains(page, reverse("backoffice:staff_delete", args=[member.id]))
        self.assertContains(page, "Delete")

    def test_deleting_a_staff_member_works(self):
        member = Staff.objects.create(name="Goodbye", email="bye@x.local",
                                      role="cashier")
        response = self.client.post(
            reverse("backoffice:staff_delete", args=[member.id]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Staff.objects.filter(id=member.id).exists())

    def test_history_survives_the_deletion(self):
        """Bills and shifts store the name as text, so the record stays."""
        member = Staff.objects.create(name="Nok Srisai", email="nok@x.local",
                                      role="cashier")
        AuditLog.objects.create(
            actor=member, actor_label=member.name, actor_role="cashier",
            action="update", model="Product", object_label="Croissant",
        )
        Order.objects.create(
            branch=self.branch, order_number="DEL-0001", staff=member.name,
            subtotal=Decimal("10"), total=Decimal("10"), status="completed",
        )
        self.client.post(reverse("backoffice:staff_delete", args=[member.id]))

        entry = AuditLog.objects.get(object_label="Croissant")
        self.assertIsNone(entry.actor_id, "the FK should go null, not cascade")
        self.assertEqual(entry.actor_label, "Nok Srisai",
                         "the audit log forgot who did it")
        self.assertEqual(
            Order.objects.get(order_number="DEL-0001").staff, "Nok Srisai",
            "the bill forgot its cashier")

    def test_you_cannot_delete_yourself(self):
        self.client.post(reverse("backoffice:staff_delete", args=[self.admin.id]))
        self.assertTrue(Staff.objects.filter(id=self.admin.id).exists(),
                        "deleted the signed-in account")

    def test_you_cannot_delete_the_last_admin(self):
        # Sign in as the second admin, then try to delete the first…
        self.second_admin.role = "admin"
        self.second_admin.save()
        client = Client()
        self.assertTrue(client.login(username="second", password=self.password))
        client.post(reverse("backoffice:staff_delete", args=[self.admin.id]))
        self.assertFalse(Staff.objects.filter(id=self.admin.id).exists(),
                         "two admins exist, so deleting one is fine")

        # …now only `second_admin` is left, and it is signed in as itself.
        client.post(reverse("backoffice:staff_delete", args=[self.second_admin.id]))
        self.assertTrue(Staff.objects.filter(id=self.second_admin.id).exists())

    def test_the_form_names_the_last_admin_reason_over_the_self_one(self):
        """Both refusals can apply at once; the actionable one should lead.

        Being the last admin is only ever reachable on your own record — an
        admin viewing another admin means two exist — so if the self-reason
        won, the message would never tell anyone to promote a colleague.
        """
        Staff.objects.filter(role="admin").exclude(id=self.admin.id).update(
            role="cashier")
        page = self.client.get(
            reverse("backoffice:staff_detail", args=[self.admin.id]))
        self.assertContains(page, "last admin")
        self.assertContains(page, "Make someone else an admin first")

    def test_a_cashier_cannot_delete_staff(self):
        member = Staff.objects.create(name="Target", email="target@x.local",
                                      role="cashier")
        response = self._cashier_client().post(
            reverse("backoffice:staff_delete", args=[member.id]))
        self.assertNotEqual(response.status_code, 302)
        self.assertTrue(Staff.objects.filter(id=member.id).exists(),
                        "a cashier deleted a colleague")

    def test_a_get_never_deletes(self):
        """A crawler or a prefetch must not be able to remove someone."""
        member = Staff.objects.create(name="Safe", email="safe@x.local",
                                      role="cashier")
        self.client.get(reverse("backoffice:staff_delete", args=[member.id]))
        self.assertTrue(Staff.objects.filter(id=member.id).exists())


class LastAdminRevokeTests(HostileDataMixin, TestCase):
    """The Users page footer promises this; until now it was only text."""

    @classmethod
    def setUpTestData(cls):
        cls.build_shop()
        cls.other_admin = Staff(
            name="Other Admin", username="other", email="other@x.local",
            role="admin", active=True, backoffice_access=True,
        )
        cls.other_admin.set_password(cls.password)
        cls.other_admin.save()

    def test_revoking_the_last_admin_is_refused(self):
        client = Client()
        self.assertTrue(client.login(username="other", password=self.password))
        # Two admins: revoking one is allowed.
        client.post(reverse("backoffice:user_delete", args=[self.admin.id]))
        self.admin.refresh_from_db()
        self.assertFalse(self.admin.backoffice_access)

        # `other` is now the only admin, and cannot revoke itself either way.
        client.post(reverse("backoffice:user_delete", args=[self.other_admin.id]))
        self.other_admin.refresh_from_db()
        self.assertTrue(self.other_admin.backoffice_access,
                        "the last admin revoked their own access")


class BranchPickerTests(HostileDataMixin, TestCase):
    """The header's branch control.

    It drew two chevrons for a while — the native `<select>` arrow plus the
    one the design system adds — which is invisible to any assertion about
    status codes and only shows up on screen. These pin the parts that *are*
    checkable: one arrow's worth of markup, a real label for screen readers,
    and no picker at all when there is nothing to pick.
    """

    @classmethod
    def setUpTestData(cls):
        cls.build_shop()

    def setUp(self):
        self.assertTrue(
            self.client.login(username="hostile", password=self.password))

    def test_several_branches_get_a_select(self):
        self.assertGreater(Branch.objects.count(), 1)
        page = self.client.get(reverse("backoffice:dashboard"))
        self.assertContains(page, 'name="branch"')
        self.assertContains(page, 'aria-label="Branch"')

    def test_the_native_arrow_is_suppressed(self):
        """Both arrows render unless the stylesheet drops the native one."""
        css = self.client.get(reverse("backoffice:app_css")).content.decode()
        select_rule = css[css.index(".ctl > select {"):]
        self.assertIn("appearance: none", select_rule[:400])

    def test_one_branch_gets_a_label_not_a_picker(self):
        """A dropdown whose only option is already selected is a dead click."""
        Branch.objects.exclude(id=self.branch.id).update(active=False)
        page = self.client.get(reverse("backoffice:dashboard"))
        self.assertNotContains(page, 'name="branch"')
        self.assertContains(page, "is-static")
        self.assertContains(page, self.branch.name)

    def test_one_branch_still_scopes_the_figures(self):
        """Dropping the select must not drop the filtering with it — the views
        fall back to the only branch when `?branch=` is absent."""
        Branch.objects.exclude(id=self.branch.id).update(active=False)
        for name in ["dashboard", "inventory", "transactions", "report_daily"]:
            with self.subTest(page=name):
                response = self.client.get(reverse(f"backoffice:{name}"))
                self.assertEqual(response.status_code, 200)

    def test_switching_branch_keeps_the_other_filters(self):
        """The picker posts into `filterForm`, which carries the page's own
        hidden filters — changing shop must not silently reset them."""
        page = self.client.get(reverse("backoffice:transactions"),
                               {"branch": str(self.branch.id),
                                "status": "voided", "payment": "Cash"})
        body = page.content.decode()
        form = body[body.index('id="filterForm"'):]
        self.assertIn('name="status" value="voided"', form)
        self.assertIn('name="payment" value="Cash"', form)


class ResponsiveShellTests(HostileDataMixin, TestCase):
    """Phone and tablet layout.

    Layout itself is not assertable from a test client, so these pin the
    pieces that are: the drawer's markup exists, no template pins a width
    inline where a media query needs to override it, and the stylesheet still
    carries the breakpoints. The bug this replaces was a rail that went
    `position: fixed` under 900px and was never hidden — it simply covered the
    page, which no status-code test would ever notice.
    """

    @classmethod
    def setUpTestData(cls):
        cls.build_shop()

    def setUp(self):
        self.assertTrue(
            self.client.login(username="hostile", password=self.password))
        self.css = self.client.get(
            reverse("backoffice:app_css")).content.decode()
        # Comments explain the rules and often quote the very thing a rule
        # removed, so assertions about what the stylesheet *does* have to look
        # at the declarations alone.
        self.rules = re.sub(r"/\*.*?\*/", "", self.css, flags=re.S)

    def test_the_page_declares_a_viewport(self):
        """Without it a phone renders at 980px and scales the lot down."""
        page = self.client.get(reverse("backoffice:dashboard"))
        self.assertContains(page, 'name="viewport"')
        self.assertContains(page, "width=device-width")

    def test_the_drawer_has_a_backdrop_and_a_labelled_toggle(self):
        page = self.client.get(reverse("backoffice:dashboard")).content.decode()
        self.assertIn('id="railBackdrop"', page)
        self.assertIn('id="sidebarToggle"', page)
        self.assertIn("matchMedia", page, "the toggle does not know about width")

    def test_the_drawer_starts_closed(self):
        """A rail remembered as expanded on a laptop must not open itself over
        the page on a phone — that was the original bug."""
        page = self.client.get(reverse("backoffice:dashboard")).content.decode()
        rail = page[page.index('id="sidebar"'):page.index('id="sidebar"') + 120]
        self.assertNotIn("open", rail)
        self.assertIn("hidden", page[page.index('id="railBackdrop"'):
                                     page.index('id="railBackdrop"') + 80])

    def test_the_breakpoints_are_present(self):
        for query in ["max-width: 1023px", "max-width: 720px", "max-width: 460px"]:
            with self.subTest(query=query):
                self.assertIn(query, self.rules)
        # The rail must actually be moved off-screen, not just repositioned.
        narrow = self.rules[self.rules.index("@media (max-width: 1023px)"):]
        self.assertIn("translateX(-100%)", narrow[:1600])

    def test_nothing_pins_the_page_to_the_viewport(self):
        """A 100vh page with `overflow: hidden` does not scroll — it clips.

        Every list and report page used to opt into that, betting the content
        always fits. On a short window it did not, and the overflow was not
        reachable by any means: no scrollbar, no keyboard, nothing.
        """
        self.assertNotIn("fixed-height", self.rules)
        for name in ["dashboard", "transactions", "inventory", "report_sku",
                     "report_daily", "customer_list", "audit_log",
                     "product_list", "staff_list", "report_sell"]:
            with self.subTest(page=name):
                page = self.client.get(reverse(f"backoffice:{name}"))
                self.assertNotContains(page, "fixed-height")

        # The rail and header keep their place while the document scrolls —
        # that was the part of the pinned layout worth keeping.
        self.assertIn("position: sticky", self.rules)

    def test_the_chart_cannot_collapse(self):
        """Chart.js fills its container; unbounded, that can be ~0px."""
        chart = self.rules[self.rules.index(".chart {"):]
        self.assertIn("min-height: 240px", chart[:200])

    def test_wide_tables_get_a_scroll_floor(self):
        """Twelve columns on a phone should scroll sideways, not crush."""
        narrow = self.rules[self.rules.index("@media (max-width: 1023px)"):]
        self.assertIn("overflow-x: auto", narrow[:1800])
        self.assertIn(".panel-scroll .tbl", narrow[:1800])

    def test_no_template_pins_a_control_width_inline(self):
        """An inline `style="width:230px"` beats every media query, so the
        search chips stayed slivers on a phone. They carry classes now."""
        import pathlib
        from django.conf import settings as dj

        offenders = []
        root = pathlib.Path(__file__).resolve().parents[2] / "backoffice" / "templates" / "backoffice"
        for path in root.glob("*.html"):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if 'class="ctl' in line and "style=\"width:" in line:
                    offenders.append(f"{path.name}:{number}")
                if 'class="detail"' in line and "style=\"width:" in line:
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual(offenders, [], "inline widths override the media queries")
