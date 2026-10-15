"""Launch and discontinue dates take products on and off sale.

Run:
    python manage.py test bravepos.tests.test_sale_windows \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from datetime import date
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from bravepos import sale_windows
from bravepos.models import Branch, Product, ProductSaleWindow, Settings, Staff
from bravepos.tests.test_product_fanout import FanoutTestCase

TODAY = date(2027, 1, 10)


def on(day):
    """Pretend the shop's date is ``day``."""
    return mock.patch("bravepos.sale_windows.today", return_value=day)


class SaleWindowTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = "correct-horse-battery"
        cls.admin = Staff(
            name="Window Admin", username="windows",
            email="windows@therollingpinn.com", role="admin",
            active=True, backoffice_access=True,
        )
        cls.admin.set_password(cls.password)
        cls.admin.save()
        Settings.objects.get_or_create(id="shop")
        cls.branch = Branch.objects.create(name="Ari", code="ARI")

    def setUp(self):
        self.assertTrue(
            self.client.login(username="windows", password=self.password))
        patcher = on(TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def create(self, windows=(), follow=False, **extra):
        data = {"name": "Pumpkin Tart", "price": "95.00",
                "launch_date": [a for a, _ in windows],
                "discontinue_date": [b for _, b in windows]}
        data.update(extra)
        return self.client.post(
            f"{reverse('backoffice:product_new')}?branch={self.branch.id}",
            data, follow=follow)

    def product(self):
        return Product.objects.get(name="Pumpkin Tart")

    def windows(self, product=None):
        return [(w.launch_date, w.discontinue_date)
                for w in (product or self.product()).sale_windows.all()]


class CreatingWithDatesTests(SaleWindowTestCase):
    def test_dates_are_optional(self):
        response = self.create()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.product().active)
        self.assertEqual(self.windows(), [])

    def test_the_form_offers_the_fields_when_creating(self):
        response = self.client.get(
            f"{reverse('backoffice:product_new')}?branch={self.branch.id}")
        self.assertContains(response, 'name="launch_date"')
        self.assertContains(response, 'name="discontinue_date"')

    def test_several_periods_are_stored(self):
        self.create([("2027-01-01", "2027-01-15"), ("2027-01-25", "2027-01-30")])
        self.assertEqual(self.windows(), [
            (date(2027, 1, 1), date(2027, 1, 15)),
            (date(2027, 1, 25), date(2027, 1, 30)),
        ])
        self.assertTrue(self.product().active)

    def test_either_end_may_be_blank(self):
        self.create([("", "2027-02-01"), ("2027-03-01", "")])
        self.assertEqual(self.windows(), [
            (None, date(2027, 2, 1)), (date(2027, 3, 1), None)])

    def test_a_future_launch_starts_it_under_removed(self):
        response = self.create([("2027-02-01", "")], follow=True)
        product = self.product()
        self.assertFalse(product.active)
        self.assertTrue(product.removed_by_schedule)
        self.assertContains(response, "launches 1 Feb 2027")

    def test_a_past_discontinue_date_removes_it_at_once(self):
        self.create([("2027-01-01", "2027-01-05")])
        self.assertFalse(self.product().active)

    def test_discontinue_before_launch_is_refused(self):
        response = self.create([("2027-01-15", "2027-01-01")])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "before its launch date")
        self.assertFalse(Product.objects.exists())
        # What was typed comes back.
        self.assertContains(response, 'value="2027-01-15"')

    def test_a_non_date_is_refused(self):
        response = self.create([("soon", "")])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "isn&#x27;t a date")

    def test_editing_replaces_the_dates(self):
        self.create([("2027-01-01", "2027-01-15")])
        product = self.product()
        self.client.post(
            reverse("backoffice:product_detail", args=[product.id]),
            {"name": "Pumpkin Tart", "price": "95.00",
             "launch_date": ["2027-01-01", "2027-01-20"],
             "discontinue_date": ["2027-01-12", ""]})
        self.assertEqual(self.windows(), [
            (date(2027, 1, 1), date(2027, 1, 12)), (date(2027, 1, 20), None)])

    def test_the_edit_page_shows_the_saved_dates(self):
        self.create([("2027-01-01", "2027-01-15")])
        response = self.client.get(
            reverse("backoffice:product_detail", args=[self.product().id]))
        self.assertContains(response, 'value="2027-01-15"')
        self.assertContains(response, "On sale until 15 Jan 2027")


class TheCalendarTests(SaleWindowTestCase):
    """1–15 Jan and 25–30 Jan, walked through day by day by the cron."""

    def setUp(self):
        super().setUp()
        self.create([("2027-01-01", "2027-01-15"), ("2027-01-25", "2027-01-30")])

    def run_on(self, day):
        with on(day):
            call_command("apply_sale_windows", stdout=mock.MagicMock())
        return self.product()

    def test_on_sale_through_the_last_day_inclusive(self):
        self.assertTrue(self.run_on(date(2027, 1, 15)).active)

    def test_removed_after_the_discontinue_date(self):
        product = self.run_on(date(2027, 1, 16))
        self.assertFalse(product.active)
        self.assertTrue(product.removed_by_schedule)

    def test_back_on_sale_at_the_next_launch(self):
        self.run_on(date(2027, 1, 16))
        product = self.run_on(date(2027, 1, 25))
        self.assertTrue(product.active)
        self.assertFalse(product.removed_by_schedule)

    def test_removed_for_good_after_the_last_period(self):
        self.run_on(date(2027, 1, 16))
        self.run_on(date(2027, 1, 25))
        self.assertFalse(self.run_on(date(2027, 1, 31)).active)

    def test_it_shows_under_removed_with_its_next_date(self):
        self.run_on(date(2027, 1, 16))
        with on(date(2027, 1, 16)):
            response = self.client.get(
                reverse("backoffice:product_list") + "?archived=1")
        self.assertContains(response, "Pumpkin Tart")
        self.assertContains(response, "Back on sale 25 Jan 2027")

    def test_dry_run_changes_nothing(self):
        with on(date(2027, 1, 16)):
            call_command("apply_sale_windows", "--dry-run",
                         stdout=mock.MagicMock())
        self.assertTrue(self.product().active)

    def test_a_product_removed_by_hand_is_not_brought_back(self):
        product = self.run_on(date(2027, 1, 16))
        product.active = True
        product.save()
        self.client.post(
            reverse("backoffice:product_archive", args=[product.id]))
        self.assertFalse(self.run_on(date(2027, 1, 25)).active)

    def test_tills_stop_getting_it(self):
        self.run_on(date(2027, 1, 16))
        self.assertFalse(Product.objects.filter(active=True).exists())


class RestoreTests(SaleWindowTestCase):
    def removed_on(self, day, windows):
        self.create(windows)
        with on(day):
            sale_windows.apply_all(day)
        return self.product()

    def restore(self, product, day):
        with on(day):
            return self.client.post(
                reverse("backoffice:product_restore", args=[product.id]),
                follow=True)

    def test_restoring_after_the_end_keeps_it_on_sale(self):
        product = self.removed_on(date(2027, 1, 20), [("2027-01-01", "2027-01-15")])
        self.restore(product, date(2027, 1, 20))
        product.refresh_from_db()
        self.assertTrue(product.active)
        # Past window kept as history; a new open-ended one from today.
        self.assertEqual(self.windows(), [
            (date(2027, 1, 1), date(2027, 1, 15)), (date(2027, 1, 20), None)])
        with on(date(2027, 3, 1)):
            sale_windows.apply_all()
        product.refresh_from_db()
        self.assertTrue(product.active)

    def test_restoring_in_a_gap_runs_into_the_next_period(self):
        product = self.removed_on(
            date(2027, 1, 20),
            [("2027-01-01", "2027-01-15"), ("2027-01-25", "2027-01-30")])
        self.restore(product, date(2027, 1, 20))
        self.assertIn((date(2027, 1, 20), date(2027, 1, 24)), self.windows())
        with on(date(2027, 1, 31)):
            sale_windows.apply_all()
        product.refresh_from_db()
        self.assertFalse(product.active)

    def test_restoring_a_hand_removed_product_adds_no_dates(self):
        self.create()
        product = self.product()
        self.client.post(reverse("backoffice:product_archive", args=[product.id]))
        self.restore(product, TODAY)
        product.refresh_from_db()
        self.assertTrue(product.active)
        self.assertEqual(self.windows(), [])


class StatusTests(TestCase):
    def w(self, a, b):
        return ProductSaleWindow(launch_date=a, discontinue_date=b)

    def test_lines(self):
        jan = [self.w(date(2027, 1, 1), date(2027, 1, 15)),
               self.w(date(2027, 1, 25), date(2027, 1, 30))]
        self.assertEqual(sale_windows.status([], TODAY), "")
        self.assertEqual(sale_windows.status(jan, date(2026, 12, 1)),
                         "Launches 1 Jan 2027")
        self.assertEqual(sale_windows.status(jan, TODAY),
                         "On sale until 15 Jan 2027")
        self.assertEqual(sale_windows.status(jan, date(2027, 1, 20)),
                         "Back on sale 25 Jan 2027")
        self.assertEqual(sale_windows.status(jan, date(2027, 2, 1)),
                         "Discontinued 30 Jan 2027")
        self.assertEqual(
            sale_windows.status([self.w(date(2027, 1, 1), None)], TODAY),
            "On sale — no end date")


class SyncToAllBranchesTests(FanoutTestCase):
    def setUp(self):
        super().setUp()
        patcher = on(TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def at(self, branch):
        return Product.objects.get(branch=branch, name="Cherry Mousse Pop")

    def test_dates_travel_with_the_product(self):
        self.create(launch_date=["2027-01-01"], discontinue_date=["2027-01-05"])
        for branch in (self.branch, self.silom, self.bio):
            product = self.at(branch)
            self.assertEqual(
                [(w.launch_date, w.discontinue_date) for w in product.sale_windows.all()],
                [(date(2027, 1, 1), date(2027, 1, 5))])
            self.assertFalse(product.active)
            self.assertTrue(product.removed_by_schedule)
