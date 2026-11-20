"""Delivery dates and production lead times.

Logistics ticks which days the kitchen delivers to each branch and sets each
branch's lead time; supervisors can only request for ticked, still-open dates;
a request locks at the cut-off time, lead-time days before delivery.

Run:
    python manage.py test bravepos.tests.test_production_delivery_dates \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

import datetime
from unittest import mock

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from bravepos import production, production_policy
from bravepos.models import (
    Branch, Category, Product, ProductionCutoffPolicy, ProductionDeliveryDate,
    ProductionLeadTime, ProductionRequest,
)
from bravepos.tests.test_user_management_and_audit import make_user, sign_in


def today():
    return timezone.localdate()


def days(n):
    return today() + datetime.timedelta(days=n)


def at(day, hh, mm=0):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hh, mm)))


def tick(branch, *offsets):
    for n in offsets:
        ProductionDeliveryDate.objects.get_or_create(branch=branch, delivery_date=days(n))


class LeadTimeDeadlineTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="Lead Mall")

    def test_default_is_the_day_before(self):
        self.assertEqual(production.locks_at(days(10), datetime.time(18, 0), branch=self.branch),
                         at(days(9), 18))

    def test_lead_time_moves_the_deadline_back(self):
        production_policy.update_lead_days(self.branch, 3)
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.production_lead_days, 3)
        self.assertEqual(production.locks_at(days(10), datetime.time(18, 0), branch=self.branch),
                         at(days(7), 18))
        self.assertEqual(production.locks_at(days(10), branch=self.branch).date(), days(7))

    def test_other_branches_keep_their_own_lead_time(self):
        other = Branch.objects.create(name="Other Mall")
        production_policy.update_lead_days(self.branch, 4)
        self.assertEqual(production.locks_at(days(10), branch=other).date(), days(9))

    def test_shortening_never_reopens_a_closed_date(self):
        production_policy.update_lead_days(self.branch, 3)
        self.assertFalse(production.is_open(days(2), branch=self.branch))  # locked yesterday
        production_policy.update_lead_days(self.branch, 1)
        self.assertFalse(production.is_open(days(2), branch=self.branch))
        # Dates still open when the change was made get the shorter lead time.
        self.assertEqual(production.locks_at(days(10), branch=self.branch).date(), days(9))

    def test_lengthening_closes_affected_dates_now_not_retroactively(self):
        self.assertTrue(production.is_open(days(3), branch=self.branch))
        before = timezone.now()
        production_policy.update_lead_days(self.branch, 5)
        self.assertFalse(production.is_open(days(3), branch=self.branch))
        self.assertGreaterEqual(production.locks_at(days(3), branch=self.branch), before)

    def test_lead_time_bounds(self):
        for bad in (0, 31, True, "2"):
            with self.subTest(bad=bad), self.assertRaises((ValueError, TypeError)):
                production_policy.update_lead_days(self.branch, bad)

    def test_cutoff_change_does_not_reopen_a_long_lead_branch(self):
        """At 20:00 with an 18:00 cut-off, a lead-3 branch's date 3 days out
        has closed today.  Moving the cut-off to 23:00 must not reopen it."""
        ProductionCutoffPolicy.objects.all().delete()
        ProductionCutoffPolicy.objects.create(
            effective_from=datetime.date.min, cutoff=datetime.time(18, 0),
            activated_at=at(days(-30), 9), source="admin_change")
        Branch.objects.exclude(pk=self.branch.pk).update(active=False)
        ProductionLeadTime.objects.create(branch=self.branch, lead_days=3, effective_from=datetime.date.min,
                                          activated_at=at(days(-30), 9))
        with mock.patch("django.utils.timezone.now", return_value=at(today(), 20)):
            self.assertFalse(production.is_open(days(3), branch=self.branch))
            production_policy.update_cutoff(datetime.time(23, 0))
            self.assertFalse(production.is_open(days(3), branch=self.branch))


class RequestableDatesTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="Pick Mall")

    def test_only_ticked_open_dates(self):
        tick(self.branch, -1, 1, 4, 6)
        production_policy.update_lead_days(self.branch, 2)
        offered = production.requestable_dates(self.branch)
        self.assertNotIn(days(-1), offered)
        self.assertNotIn(days(1), offered)   # closed under a 2-day lead time
        self.assertEqual(offered, [days(4), days(6)])

    def test_final_at_is_the_last_branch_to_lock(self):
        far = Branch.objects.create(name="Far Mall")
        tick(self.branch, 10)
        tick(far, 10)
        production_policy.update_lead_days(far, 4)
        self.assertEqual(production.final_at(days(10)).date(), days(9))


class RequestPageWhitelistTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="Pick Mall")
        cat = Category.objects.create(branch=self.branch, name="Cookies")
        self.product = Product.objects.create(branch=self.branch, name="Pick cookie", category=cat)
        sign_in(self.client, make_user(username="sup", email="sup@example.com", role="stock_supervisor"))

    def url(self, n):
        return reverse("backoffice:production_request") + f"?branch={self.branch.id}&date={days(n).isoformat()}"

    def test_picker_lists_only_ticked_dates_and_defaults_to_the_first(self):
        tick(self.branch, 4, 6)
        page = self.client.get(reverse("backoffice:production_request") + f"?branch={self.branch.id}")
        body = page.content.decode()
        self.assertIn(f'value="{days(4).isoformat()}" selected', body)
        self.assertIn(f'value="{days(6).isoformat()}"', body)
        self.assertNotIn(f'value="{days(5).isoformat()}"', body)
        self.assertContains(page, 'name="qty_')

    def test_no_dates_says_so(self):
        page = self.client.get(reverse("backoffice:production_request") + f"?branch={self.branch.id}")
        self.assertContains(page, "No delivery dates are open for Pick Mall yet")
        self.assertNotContains(page, 'name="qty_')

    def test_unticked_date_cannot_be_saved(self):
        tick(self.branch, 4)
        self.client.get(self.url(4))  # the form a supervisor would have open
        ProductionDeliveryDate.objects.filter(branch=self.branch, delivery_date=days(5)).delete()
        response = self.client.post(self.url(5), {
            "branch_id": str(self.branch.id), "delivery_date": days(5).isoformat(),
            "request_revision": "0", f"qty_{self.product.id}": "6",
            f"base_{self.product.id}": "",
        })
        self.assertContains(response, "not a delivery date", status_code=response.status_code)
        self.assertFalse(ProductionRequest.objects.filter(delivery_date=days(5)).exists())

    def test_unticked_date_save_refused_by_the_service(self):
        with self.assertRaises(production.NotADeliveryDate):
            production.save_request(self.branch, days(5), {str(self.product.id): 3},
                                    expected_revision=0, strict=True)
        self.assertFalse(ProductionRequest.objects.exists())


class DeliveryDatesPageTests(TestCase):
    def setUp(self):
        self.alpha = Branch.objects.create(name="Alpha Mall")
        self.beta = Branch.objects.create(name="Beta Mall")
        self.logistic = make_user(username="logi", email="logi@example.com", role="logistic")
        sign_in(self.client, self.logistic)
        self.url = reverse("backoffice:production_delivery_dates")

    def key(self, branch, n):
        return f"{branch.id}_{days(n).isoformat()}"

    def post(self, cells, on=(), was=(), leads=None):
        data = {"cell": cells, "on": list(on), "was": list(was)}
        for b in (self.alpha, self.beta):
            data[f"lead_was_{b.id}"] = str(b.production_lead_days)
            data[f"lead_{b.id}"] = str((leads or {}).get(b, b.production_lead_days))
        return self.client.post(self.url, data)

    def test_page_opens_with_the_grid(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Delivery dates")
        self.assertContains(response, "Alpha Mall")
        self.assertContains(response, f'value="{self.key(self.alpha, 3)}"')

    def test_tick_and_set_lead_time(self):
        k = self.key(self.alpha, 5)
        response = self.post([k], on=[k], leads={self.beta: 3})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ProductionDeliveryDate.objects.filter(branch=self.alpha, delivery_date=days(5)).exists())
        self.beta.refresh_from_db()
        self.assertEqual(self.beta.production_lead_days, 3)
        self.assertEqual(ProductionLeadTime.objects.get().changed_by_name, self.logistic.name)

    def test_only_changed_cells_are_applied(self):
        tick(self.alpha, 6)  # someone else ticked it after this page loaded
        k5, k6 = self.key(self.alpha, 5), self.key(self.alpha, 6)
        # This page loaded with day 6 un-ticked and left it alone.
        self.post([k5, k6], on=[k5], was=[])
        self.assertTrue(ProductionDeliveryDate.objects.filter(branch=self.alpha, delivery_date=days(6)).exists())

    def test_untick_with_a_request_is_refused_and_nothing_saved(self):
        cat = Category.objects.create(branch=self.alpha, name="Cookies")
        product = Product.objects.create(branch=self.alpha, name="Cookie", category=cat)
        tick(self.alpha, 5, 6)
        production.save_request(self.alpha, days(5), {str(product.id): 4})
        k5, k6, k7 = self.key(self.alpha, 5), self.key(self.alpha, 6), self.key(self.alpha, 7)
        response = self.post([k5, k6, k7], on=[k7], was=[k5, k6])
        self.assertContains(response, "already have a production request", status_code=409)
        self.assertTrue(ProductionDeliveryDate.objects.filter(branch=self.alpha, delivery_date=days(5)).exists())
        self.assertTrue(ProductionDeliveryDate.objects.filter(branch=self.alpha, delivery_date=days(6)).exists())
        self.assertFalse(ProductionDeliveryDate.objects.filter(branch=self.alpha, delivery_date=days(7)).exists())

    def test_requested_date_is_shown_locked(self):
        cat = Category.objects.create(branch=self.alpha, name="Cookies")
        product = Product.objects.create(branch=self.alpha, name="Cookie", category=cat)
        tick(self.alpha, 5)
        production.save_request(self.alpha, days(5), {str(product.id): 4})
        body = self.client.get(self.url).content.decode()
        self.assertNotIn(f'name="cell" value="{self.key(self.alpha, 5)}"', body)
        self.assertIn("dd-req", body)

    def test_closed_dates_are_skipped(self):
        production_policy.update_lead_days(self.alpha, 5)
        k = self.key(self.alpha, 2)  # closed under a 5-day lead time
        self.post([k], on=[k])
        self.assertFalse(ProductionDeliveryDate.objects.filter(branch=self.alpha, delivery_date=days(2)).exists())
        self.assertNotIn(f'name="cell" value="{k}"', self.client.get(self.url).content.decode())

    def test_bad_lead_time_saves_nothing(self):
        k = self.key(self.alpha, 5)
        response = self.post([k], on=[k], leads={self.alpha: 0})
        self.assertContains(response, "Lead time must be", status_code=400)
        self.assertFalse(ProductionDeliveryDate.objects.exists())
        self.alpha.refresh_from_db()
        self.assertEqual(self.alpha.production_lead_days, 1)

    def test_logistic_rail_and_other_pages(self):
        response = self.client.get(reverse("backoffice:ninja_windows"))
        self.assertContains(response, reverse("backoffice:production_delivery_dates") + '"')
        self.assertContains(self.client.get(reverse("backoffice:production_request")),
                            "Ninja windows and Delivery dates only", status_code=403)


class DeliveryDatesAccessTests(TestCase):
    def test_who_can_open_it(self):
        url = reverse("backoffice:production_delivery_dates")
        for role, status in (("admin", 200), ("cashier", 200), ("logistic", 200),
                             ("viewer", 403), ("packer", 403), ("stock_supervisor", 403)):
            with self.subTest(role=role):
                client = Client()  # logout() can't handle Staff's UUID session key
                sign_in(client, make_user(username=role, email=f"{role}@example.com", role=role))
                self.assertEqual(client.get(url).status_code, status)
