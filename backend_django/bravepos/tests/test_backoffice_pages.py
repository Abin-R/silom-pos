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

from django.test import TestCase, override_settings
from django.urls import URLPattern, URLResolver, reverse
from django.utils import timezone

from bravepos.models import Branch, Category, Order, OrderItem, Product, Settings, Staff


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
