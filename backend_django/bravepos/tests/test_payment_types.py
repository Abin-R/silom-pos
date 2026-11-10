"""Custom Pay types: the backoffice's list, its sync, and the till's feed.

Run:
    python manage.py test bravepos.tests.test_payment_types \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

import importlib
from decimal import Decimal

from django.apps import apps as django_apps
from django.test import Client, TestCase
from django.urls import reverse

from bravepos.models import (
    DEFAULT_PAYMENT_TYPES, PAYMENT_TYPE_NAME_MAX, BranchSession, Order, PaymentType, Staff,
)
from bravepos.orders import create_order_from_items
from bravepos.tests.test_user_management_and_audit import make_user, sign_in

from .factories import make_branch, make_product, make_shop


def names(branch):
    return list(PaymentType.objects.filter(branch=branch).values_list("name", flat=True))


class _Base(TestCase):
    def setUp(self):
        self.silom = make_branch(name="Silom")
        self.ari = make_branch(name="Ari")
        sign_in(self.client, make_user(username="bo", email="bo@example.com", role="admin"))

    def _list_url(self, branch=None):
        return reverse("backoffice:payment_types") + f"?branch={(branch or self.silom).id}"

    def _row(self, name, branch=None):
        return PaymentType.objects.get(branch=branch or self.silom, name=name)

    def _act(self, action, row, *args, **data):
        return self.client.post(
            reverse(f"backoffice:payment_type_{action}", args=[row.id, *args]), data)


class SeedTests(_Base):
    def test_new_branch_starts_with_the_old_till_list(self):
        self.assertEqual(names(self.silom), DEFAULT_PAYMENT_TYPES)

    def test_migration_seeds_every_branch_once(self):
        PaymentType.objects.all().delete()
        migration = importlib.import_module("bravepos.migrations.0083_paymenttype")
        migration.seed(django_apps, None)
        for branch in (self.silom, self.ari):
            self.assertEqual(names(branch), DEFAULT_PAYMENT_TYPES)
        self.assertEqual(migration.TILL_LIST, DEFAULT_PAYMENT_TYPES)


class ManageTests(_Base):
    def test_add_goes_last_at_this_branch_only(self):
        self.client.post(self._list_url(), {"name": "  Grab  "})
        self.assertEqual(names(self.silom)[-1], "Grab")
        self.assertNotIn("Grab", names(self.ari))

    def test_blank_duplicate_and_overlong_are_refused(self):
        for name, message in [
            ("  ", "Enter a payment type name."),
            ("edc kbank", "is already a payment type"),
            ("x" * (PAYMENT_TYPE_NAME_MAX + 1), "Keep the name to"),
        ]:
            with self.subTest(name=name):
                self.assertContains(self.client.post(self._list_url(), {"name": name}), message)
        self.assertEqual(names(self.silom), DEFAULT_PAYMENT_TYPES)

    def test_edit_renames(self):
        self._act("edit", self._row("QR"), name="Grab")
        self.assertIn("Grab", names(self.silom))
        self.assertNotIn("QR", names(self.silom))

    def test_edit_can_change_only_the_case(self):
        self._act("edit", self._row("QR"), name="qr")
        self.assertIn("qr", names(self.silom))

    def test_edit_refuses_another_types_name(self):
        res = self._act("edit", self._row("QR"), name="EDC SCB")
        self.assertContains(res, "is already a payment type")
        self.assertIn("QR", names(self.silom))

    def test_hide_and_show(self):
        row = self._row("QR")
        self._act("toggle", row)
        row.refresh_from_db()
        self.assertFalse(row.active)
        self.assertContains(self.client.get(self._list_url()), "Hidden")
        self._act("toggle", row)
        row.refresh_from_db()
        self.assertTrue(row.active)

    def test_delete(self):
        self._act("delete", self._row("QR"))
        self.assertNotIn("QR", names(self.silom))
        self.assertIn("QR", names(self.ari))

    def test_move_swaps_neighbours_and_stops_at_the_ends(self):
        self._act("move", self._row("EDC Bangkok"), "up")
        self.assertEqual(names(self.silom)[:2], ["EDC Bangkok", "EDC Kbank"])
        self._act("move", self._row("EDC Kbank"), "down")
        self.assertEqual(names(self.silom)[:3], ["EDC Bangkok", "Brave Brand Co.,Ltd", "EDC Kbank"])
        before = names(self.silom)
        self._act("move", self._row(before[0]), "up")
        self._act("move", self._row(before[-1]), "down")
        self.assertEqual(names(self.silom), before)

    def test_unknown_direction_is_a_404(self):
        self.assertEqual(self._act("move", self._row("QR"), "sideways").status_code, 404)

    def test_get_on_an_action_changes_nothing(self):
        row = self._row("QR")
        for action, args in (("toggle", []), ("delete", []), ("move", ["up"])):
            self.client.get(reverse(f"backoffice:payment_type_{action}", args=[row.id, *args]))
        self.assertEqual(names(self.silom), DEFAULT_PAYMENT_TYPES)
        row.refresh_from_db()
        self.assertTrue(row.active)


class SyncToAllBranchesTests(_Base):
    def test_add_reaches_other_branches_and_names_them(self):
        res = self.client.post(self._list_url(), {"name": "Grab", "sync_all": "1"}, follow=True)
        self.assertIn("Grab", names(self.ari))
        self.assertContains(res, "added to Ari")

    def test_inactive_branch_is_left_alone(self):
        closed = make_branch(name="Closed")
        closed.active = False
        closed.save()
        self.client.post(self._list_url(), {"name": "Grab", "sync_all": "1"})
        self.assertNotIn("Grab", names(closed))

    def test_rename_finds_the_copies_under_the_old_name(self):
        self._act("edit", self._row("QR"), name="Grab", sync_all="1")
        self.assertIn("Grab", names(self.ari))
        self.assertNotIn("QR", names(self.ari))

    def test_rename_onto_a_name_a_branch_already_has_does_not_crash(self):
        PaymentType.objects.create(branch=self.ari, name="Grab", sort_order=99)
        self._act("edit", self._row("QR"), name="Grab", sync_all="1")
        self.assertEqual(names(self.ari).count("Grab"), 1)
        self.assertIn("QR", names(self.ari))

    def test_hide_and_delete_reach_copies_matched_ignoring_case(self):
        PaymentType.objects.filter(branch=self.ari, name="QR").update(name="qr")
        self._act("toggle", self._row("QR"), sync_all="1")
        self.assertFalse(PaymentType.objects.get(branch=self.ari, name__iexact="qr").active)
        self._act("delete", self._row("QR"), sync_all="1")
        self.assertFalse(PaymentType.objects.filter(branch=self.ari, name__iexact="qr").exists())

    def test_hidden_type_is_not_created_at_a_branch_without_it(self):
        PaymentType.objects.filter(branch=self.ari, name="QR").delete()
        self._act("toggle", self._row("QR"), sync_all="1")
        self.assertNotIn("QR", names(self.ari))

    def test_order_copies_and_a_local_type_stays_after_shared_ones(self):
        PaymentType.objects.create(branch=self.ari, name="Local", sort_order=-1)
        self._act("move", self._row("QR"), "up", sync_all="1")
        self.assertEqual(names(self.ari), names(self.silom) + ["Local"])

    def test_without_the_tick_other_branches_do_not_change(self):
        self._act("toggle", self._row("QR"))
        self._act("edit", self._row("EDC SCB"), name="SCB")
        self._act("move", self._row("EDC Bangkok"), "up")
        self._act("delete", self._row("EDC Kbank"))
        self.assertEqual(names(self.ari), DEFAULT_PAYMENT_TYPES)
        self.assertTrue(self._row("QR", self.ari).active)


class TillFeedTests(TestCase):
    def setUp(self):
        self.silom = make_branch(name="Silom")
        self.ari = make_branch(name="Ari")
        staff = Staff.objects.create(name="Nok", email="nok@example.com", role="cashier", active=True)
        session = BranchSession.objects.create(
            branch=self.silom, staff=staff, token=BranchSession.new_token())
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {session.token}"}

    def _feed(self):
        res = self.client.get("/api/payment-types", **self.auth)
        self.assertEqual(res.status_code, 200)
        return [t["name"] for t in res.json()["payment_types"]]

    def test_needs_a_session(self):
        self.assertIn(self.client.get("/api/payment-types").status_code, (401, 403))

    def test_lists_this_branch_in_order_without_hidden(self):
        PaymentType.objects.filter(branch=self.ari).update(active=False)
        PaymentType.objects.filter(branch=self.silom, name="QR").update(active=False)
        PaymentType.objects.filter(branch=self.silom, name="EDC SCB").update(sort_order=-1)
        expected = ["EDC SCB"] + [n for n in DEFAULT_PAYMENT_TYPES if n not in ("QR", "EDC SCB")]
        self.assertEqual(self._feed(), expected)

    def test_all_hidden_is_an_empty_list(self):
        PaymentType.objects.filter(branch=self.silom).update(active=False)
        self.assertEqual(self._feed(), [])


class SaleRecordingTests(TestCase):
    """What a sale under a configured type does to money, stock and history."""

    def setUp(self):
        make_shop()
        self.branch = make_branch(name="Silom")
        self.product = make_product(self.branch, name="Cake", price="100.00", stock=5)

    def _sell(self, name):
        return create_order_from_items(
            branch=self.branch,
            items=[{"product_id": str(self.product.id), "qty": 1}],
            payment_method=f"Custom · {name}",
            goods_total=Decimal("100"), subtotal=Decimal("100"),
            paid_amount=Decimal("100"),
        )

    def test_longest_name_fits_the_stored_column(self):
        self.assertLessEqual(
            len("Custom · ") + PAYMENT_TYPE_NAME_MAX,
            Order._meta.get_field("payment_method").max_length,
        )

    def test_a_name_with_card_words_pays_no_card_fee_and_takes_stock(self):
        order = self._sell("Credit Card")
        self.assertEqual(order.total, Decimal("100"))
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 4)

    def test_rename_and_delete_leave_past_sales_alone(self):
        row = PaymentType.objects.create(branch=self.branch, name="Grab", sort_order=50)
        order = self._sell("Grab")
        row.name = "GrabFood"
        row.save()
        row.delete()
        order.refresh_from_db()
        self.assertEqual(order.payment_method, "Custom · Grab")


class RoleTests(TestCase):
    def setUp(self):
        self.branch = make_branch(name="Silom")

    def test_rail_and_page_are_closed_to_reports_and_warehouse_roles(self):
        url = reverse("backoffice:payment_types")
        for i, role in enumerate(("viewer", "packer", "logistic")):
            with self.subTest(role=role):
                client = Client()
                sign_in(client, make_user(username=f"u{i}", email=f"u{i}@example.com", role=role))
                self.assertEqual(client.get(url).status_code, 403)

    def test_manager_gets_the_rail_link(self):
        sign_in(self.client, make_user(username="mgr", email="mgr@example.com", role="cashier"))
        self.assertContains(self.client.get(reverse("backoffice:payment_types")), "Payment types")
