"""Every backoffice page and CSV export loads without a server error.

The suite had good coverage of individual features but nothing that simply
*visited* each page, so a `QuerySet.iterator()` call that Django 5.0 turned
from a deprecation warning into a hard ValueError sat undetected in the
Transactions export until a Django 6 upgrade surfaced it. This walks the
whole URLconf instead of trusting a hand-written list to stay current.

Run:
    python manage.py test bravepos.tests.test_backoffice_pages \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from decimal import Decimal

from django.test import Client, TestCase, override_settings
from django.urls import URLPattern, URLResolver, reverse
from django.utils import timezone

from bravepos.models import (
    AuditLog, Branch, Category, Order, OrderItem, Product, Settings, Staff,
)


def _no_arg_backoffice_urls() -> list[str]:
    """Every `backoffice:` route that takes no arguments.

    Read off the URLconf rather than hard-coded, so a page added later is
    covered the day it lands instead of whenever someone remembers.
    """
    from backoffice import urls as backoffice_urls

    names = []
    for pattern in backoffice_urls.urlpatterns:
        if isinstance(pattern, URLResolver) or not isinstance(pattern, URLPattern):
            continue
        name = pattern.name
        if not name or pattern.pattern.regex.groups:
            continue  # takes <uuid:…>/<str:…> arguments; covered elsewhere
        names.append(name)
    return names


class BackofficePageSmokeTests(TestCase):
    """Nothing under /backoffice/ may 5xx for a signed-in admin."""

    @classmethod
    def setUpTestData(cls):
        cls.password = "correct-horse-battery"
        cls.admin = Staff(
            name="Smoke Admin", username="smoke", email="smoke@therollingpinn.com",
            role="admin", active=True, backoffice_access=True,
        )
        cls.admin.set_password(cls.password)
        cls.admin.save()

        Settings.objects.get_or_create(id="shop")
        branch = Branch.objects.create(name="Smoke Branch", code="SMK")
        cls.branch = branch
        category = Category.objects.create(name="Drinks", branch=branch)
        product = Product.objects.create(
            name="Latte", price=Decimal("100.00"), stock=10,
            branch=branch, category=category,
        )
        # A real order so the transaction/sales exports have rows to stream —
        # an empty queryset would sail past bugs that only bite mid-iteration.
        order = Order.objects.create(
            branch=branch, order_number="SMK-0001", total=Decimal("100.00"),
            created_at=timezone.now(),
        )
        OrderItem.objects.create(
            order=order, product=product, name="Latte", qty=1,
            price=Decimal("100.00"), category_name="Drinks",
        )

    def setUp(self):
        response = self.client.post(reverse("backoffice:login"), {
            "username": "smoke", "password": self.password,
        })
        self.assertEqual(response.status_code, 302, "smoke admin could not sign in")

    def test_every_no_arg_page_loads(self):
        failures = []
        for name in _no_arg_backoffice_urls():
            if name in ("login", "logout"):
                continue
            url = reverse(f"backoffice:{name}")
            try:
                status = self.client.get(url).status_code
            except Exception as exc:  # noqa: BLE001 — report, don't abort the sweep
                failures.append(f"{name} ({url}) raised {type(exc).__name__}: {exc}")
                continue
            if status >= 500:
                failures.append(f"{name} ({url}) -> {status}")
        self.assertEqual(failures, [], "Backoffice pages erroring:\n  " + "\n  ".join(failures))

    def test_csv_exports_stream_rows(self):
        """Exports specifically — they stream querysets, which is where the
        iterator()/prefetch_related incompatibility hid."""
        for name in ("transactions_export", "report_daily_export",
                     "report_sell_export", "report_sku_export",
                     "report_tax_export", "inventory_export", "audit_log_export"):
            with self.subTest(export=name):
                response = self.client.get(reverse(f"backoffice:{name}"))
                self.assertEqual(response.status_code, 200)
                self.assertIn("text/csv", response["Content-Type"])

    def test_transactions_export_includes_the_order(self):
        """Streams actual rows, not just headers.

        The branch has to be passed explicitly: a data migration seeds a
        default branch, and `_common_filters` falls back to the first one,
        which would scope the export away from the order created here.
        """
        scope = {"branch": str(self.branch.id)}

        # Transactions = one row per bill, so the receipt number appears but
        # line items don't.
        bills = self.client.get(reverse("backoffice:transactions_export"), scope)
        self.assertIn("SMK-0001", bills.content.decode())

        # Bill Detail = one row per line item, so the product name does.
        lines = self.client.get(reverse("backoffice:report_sell_export"), scope)
        self.assertIn("Latte", lines.content.decode())

    def test_bill_detail_reports_the_line_discount(self):
        """Bill Detail used to hardcode Discount to 0.00, so a bill that Sales
        by Date showed as discounted looked full-price line by line."""
        order = Order.objects.create(
            branch=self.branch, order_number="SMK-0002",
            subtotal=Decimal("160.00"), discount_amount=Decimal("110.00"),
            total=Decimal("50.00"), created_at=timezone.now(),
        )
        OrderItem.objects.create(
            order=order, name="Red Velvet Cookie", qty=1,
            price=Decimal("160.00"), discount=Decimal("110.00"),
        )
        scope = {"branch": str(self.branch.id)}

        page = self.client.get(reverse("backoffice:report_sell"), scope)
        self.assertContains(page, "110.00")

        row = next(
            line for line in
            self.client.get(reverse("backoffice:report_sell_export"), scope)
            .content.decode().splitlines()
            if "Red Velvet Cookie" in line
        )
        # …,Sub Total,Discount,Total
        self.assertTrue(row.endswith("160.00,110.00,50.00"), row)


class ProductArchiveTests(TestCase):
    """The catalogue had no way to take a product off sale.

    Removing archives (``active=False``) rather than deleting the row, which is
    what the rest of the system already means by retiring a product. These pin
    both halves of what the red panel promises: it stops being sellable, and
    the reports do not move.
    """

    @classmethod
    def setUpTestData(cls):
        cls.password = "correct-horse-battery"
        cls.admin = Staff(
            name="Arc Admin", username="arcadmin", email="arc@therollingpinn.com",
            role="admin", active=True, backoffice_access=True,
        )
        cls.admin.set_password(cls.password)
        cls.admin.save()

        Settings.objects.get_or_create(id="shop")
        cls.branch = Branch.objects.create(name="Arc Branch", code="ARC")
        cls.category = Category.objects.create(name="Bakery", branch=cls.branch)

    def setUp(self):
        self.client.post(reverse("backoffice:login"), {
            "username": "arcadmin", "password": self.password,
        })

    def _product(self, name="Mooncake Box"):
        return Product.objects.create(
            name=name, price=Decimal("599.00"), cost=Decimal("400.00"), stock=4,
            branch=self.branch, category=self.category,
        )

    def _sold(self, product):
        order = Order.objects.create(
            branch=self.branch, order_number="ARC-0001",
            subtotal=Decimal("599.00"), total=Decimal("599.00"),
            created_at=timezone.now(),
        )
        OrderItem.objects.create(
            order=order, product=product, name=product.name, qty=1,
            price=Decimal("599.00"), category_name="Bakery",
        )
        return order

    # ── The control exists ──────────────────────────────────────────────
    def test_the_edit_form_offers_remove(self):
        product = self._product()
        page = self.client.get(
            reverse("backoffice:product_detail", args=[product.id]))
        self.assertContains(
            page, reverse("backoffice:product_archive", args=[product.id]))

    def test_an_archived_product_is_offered_restore_instead(self):
        product = self._product()
        product.active = False
        product.save(update_fields=["active"])
        page = self.client.get(
            reverse("backoffice:product_detail", args=[product.id]))
        self.assertContains(
            page, reverse("backoffice:product_restore", args=[product.id]))
        self.assertNotContains(
            page, reverse("backoffice:product_archive", args=[product.id]))

    def test_a_new_product_form_offers_neither(self):
        page = self.client.get(reverse("backoffice:product_new"))
        self.assertNotContains(page, "Remove from the catalogue")

    # ── What it does ────────────────────────────────────────────────────
    def test_post_archives_and_returns_to_the_catalogue(self):
        product = self._product()
        response = self.client.post(
            reverse("backoffice:product_archive", args=[product.id]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("backoffice:product_list"), response["Location"])
        product.refresh_from_db()
        self.assertFalse(product.active)

    def test_get_does_not_archive(self):
        """It has to be a POST — otherwise a link prefetch, a crawler or a
        stray tap on browser history empties the Sale screen."""
        product = self._product()
        self.client.get(reverse("backoffice:product_archive", args=[product.id]))
        product.refresh_from_db()
        self.assertTrue(product.active)

    def test_it_needs_a_login(self):
        # A fresh client rather than `logout()`: the test client's logout
        # hydrates request.user through `get_user_model()`, which is auth.User
        # with an integer PK, while our Staff PK is a UUID — see
        # `backoffice.middleware.StaffAuthMiddleware`.
        product = self._product()
        response = Client().post(
            reverse("backoffice:product_archive", args=[product.id]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("backoffice:login"), response["Location"])
        product.refresh_from_db()
        self.assertTrue(product.active)

    def test_a_cashier_is_no_longer_offered_it(self):
        """The claim on the panel. `menu_for` is the public self-order menu;
        the till gets the same answer because `ProductViewSet` narrows its
        listings to active rows."""
        from bravepos.selforder import menu_for

        product = self._product()
        self.assertIn("Mooncake Box", [i["name"] for i in menu_for(self.branch)])

        self.client.post(reverse("backoffice:product_archive", args=[product.id]))
        self.assertNotIn("Mooncake Box", [i["name"] for i in menu_for(self.branch)])

    # ── What it must NOT do ─────────────────────────────────────────────
    def test_the_reports_do_not_move(self):
        """The whole reason this archives instead of deleting.

        `OrderItem` snapshots name, price and category but never cost, so
        `_profit_expr` reads `product__cost` live across the FK. Destroy the
        row and that FK goes NULL, cost coalesces to 0, and a closed day's
        profit silently jumps to equal its revenue while Product Performance
        relabels the line "(deleted product)". Archiving keeps the row, so
        every figure is identical before and after — and this fails the moment
        someone swaps the archive back for a `.delete()`.
        """
        from backoffice.views import _report_daily_rows, _report_sku_rows

        product = self._product()
        self._sold(product)
        today = timezone.localdate()

        before_sku = _report_sku_rows(self.branch, today, today)
        before_daily = _report_daily_rows(self.branch, today, today)

        self.client.post(reverse("backoffice:product_archive", args=[product.id]))

        self.assertEqual(_report_sku_rows(self.branch, today, today), before_sku)
        self.assertEqual(_report_daily_rows(self.branch, today, today), before_daily)

        row = _report_sku_rows(self.branch, today, today)[0]
        self.assertEqual(row["name"], "Mooncake Box")
        self.assertEqual(row["category"], "Bakery")
        self.assertEqual(row["profit"], Decimal("199.00"))

    def test_past_bills_keep_the_line(self):
        product = self._product()
        order = self._sold(product)
        self.client.post(reverse("backoffice:product_archive", args=[product.id]))

        line = OrderItem.objects.get(order=order)
        self.assertEqual(line.product_id, product.id)
        self.assertEqual(line.name, "Mooncake Box")

        export = self.client.get(reverse("backoffice:report_sell_export"),
                                 {"branch": str(self.branch.id)})
        self.assertIn("Mooncake Box", export.content.decode())

    # ── Getting it back ─────────────────────────────────────────────────
    def test_the_catalogue_hides_it_and_the_removed_view_finds_it(self):
        product = self._product()
        scope = {"branch": str(self.branch.id)}
        # `follow=True` so the success banner — which names the product — is
        # consumed on the redirect rather than landing on the page under test.
        self.client.post(reverse("backoffice:product_archive", args=[product.id]),
                         follow=True)

        on_sale = self.client.get(reverse("backoffice:product_list"), scope)
        self.assertNotContains(on_sale, "Mooncake Box")

        removed = self.client.get(reverse("backoffice:product_list"),
                                  {**scope, "archived": "1"})
        self.assertContains(removed, "Mooncake Box")
        self.assertContains(
            removed, reverse("backoffice:product_restore", args=[product.id]))

    def test_restore_puts_it_back_on_sale(self):
        product = self._product()
        self.client.post(reverse("backoffice:product_archive", args=[product.id]))
        self.client.post(reverse("backoffice:product_restore", args=[product.id]),
                         follow=True)

        product.refresh_from_db()
        self.assertTrue(product.active)
        self.assertContains(
            self.client.get(reverse("backoffice:product_list"),
                            {"branch": str(self.branch.id)}),
            "Mooncake Box",
        )

    def test_editing_an_archived_product_does_not_silently_restore_it(self):
        """`_apply_product_form` doesn't touch `active`, and it must not start
        to — otherwise saving a typo on a removed product puts it back on every
        till without anyone asking for that."""
        product = self._product()
        self.client.post(reverse("backoffice:product_archive", args=[product.id]))

        self.client.post(reverse("backoffice:product_detail", args=[product.id]), {
            "name": "Mooncake Box 2026", "price": "599", "cost": "400",
            "stock": "4", "par_level": "0", "category": str(self.category.id),
            "tax_type": "V", "product_type": "P",
        })

        product.refresh_from_db()
        self.assertFalse(product.active)

    def test_the_audit_log_names_who_removed_it(self):
        product = self._product()
        self.client.post(reverse("backoffice:product_archive", args=[product.id]))

        entry = AuditLog.objects.filter(model="Product", action="update").latest("at")
        self.assertEqual(entry.object_id, str(product.id))
        self.assertEqual(entry.actor_id, self.admin.id)
        self.assertEqual(entry.source, "backoffice")
        self.assertEqual(entry.changes.get("active"), {"from": True, "to": False})


@override_settings(DEBUG=False)
class BackofficeStylesheetTests(TestCase):
    """The design system's stylesheet must survive a production deploy.

    Production runs `DJANGO_DEBUG=0`, where `django.contrib.staticfiles` stops
    serving files, and this box has no STATIC_ROOT and no nginx
    `location /static/`. A `{% static %}` link would 404 there and every
    backoffice page would render unstyled — a failure no page-status smoke
    test catches, because an unstyled page is still a 200.

    So the stylesheet is served by a view, and these tests pin that: it must
    be reachable with DEBUG off, reachable signed out (the login page needs
    it), and actually linked by a page.
    """

    def test_stylesheet_is_served_with_debug_off(self):
        response = self.client.get(reverse("backoffice:app_css"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/css")
        self.assertIn(b"--navy", response.content, "not the design-system CSS")

    def test_stylesheet_does_not_need_a_login(self):
        """The sign-in page loads it before anyone is authenticated."""
        response = self.client.get(reverse("backoffice:app_css"))
        self.assertEqual(response.status_code, 200)

    def test_login_page_links_the_stylesheet(self):
        page = self.client.get(reverse("backoffice:login"))
        self.assertContains(page, reverse("backoffice:app_css"))

    def test_only_the_versioned_url_is_cached_forever(self):
        """`pos.rollingpinn.com` is behind Cloudflare. An unconditional
        `immutable` pinned the bare URL in its edge cache for a year against a
        file that changes every deploy, with no way to bust it."""
        bare = self.client.get(reverse("backoffice:app_css"))
        self.assertNotIn("immutable", bare["Cache-Control"])

        version = bare["ETag"].strip('"')
        versioned = self.client.get(reverse("backoffice:app_css"), {"v": version})
        self.assertIn("immutable", versioned["Cache-Control"])

    def test_a_matching_etag_is_not_resent(self):
        first = self.client.get(reverse("backoffice:app_css"))
        again = self.client.get(reverse("backoffice:app_css"),
                                HTTP_IF_NONE_MATCH=first["ETag"])
        self.assertEqual(again.status_code, 304)
