"""Inventory history: 15 days worked out, older days saved for good, and the
bills of a saved day closed to void and refund."""
from __future__ import annotations

import csv
import io
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from bravepos import audit
from bravepos import inventory_history as history
from bravepos.models import (
    AuditLog, Branch, BranchSession, InventorySnapshot, Order, Product, Staff,
    StockMovement,
)
from bravepos.orders import create_order_from_items

from .factories import make_branch, make_product, make_shop, open_shift


class HistoryTestCase(TestCase):
    def setUp(self):
        make_shop()
        admin = Staff(name="Bo", username="bo", email="bo@therollingpinn.com",
                      role="admin", active=True, backoffice_access=True)
        admin.set_password("correct-horse-battery")
        admin.save()
        self.client.post(reverse("backoffice:login"),
                         {"username": "bo", "password": "correct-horse-battery"})
        self.silom = make_branch(name="Silom")
        self.paragon = make_branch(name="Paragon")
        self.cake = make_product(self.silom, name="Mooncake", stock=10)
        self.pie = make_product(self.paragon, name="Mooncake", stock=4)
        Product.objects.update(cost=Decimal("20.00"))
        self.today = timezone.localdate()
        AuditLog.objects.update(at=self.ago(30))
        sender = mock.patch("bravepos.seatalk.send_group_text", return_value=True)
        self.send = sender.start()
        self.addCleanup(sender.stop)

    def ago(self, days):
        return timezone.now() - timedelta(days=days)

    def day(self, days):
        return self.today - timedelta(days=days)

    def sell(self, product, qty, days=0):
        order = create_order_from_items(
            branch=product.branch,
            items=[{"product_id": str(product.id), "name": product.name,
                    "price": "100.00", "qty": qty}],
            payment_method="Cash", goods_total=Decimal("100.00"),
            paid_amount=Decimal("100.00"),
        )
        if days:
            Order.objects.filter(pk=order.pk).update(created_at=self.ago(days))
            StockMovement.objects.filter(document_no=order.order_number).update(
                created_at=self.ago(days))
        return order

    def edit(self, product, days, **values):
        """A product-form edit made ``days`` ago."""
        product.refresh_from_db()
        for k, v in values.items():
            setattr(product, k, v)
        product.save()
        row = AuditLog.objects.filter(
            model="Product", object_id=str(product.id), action="update").latest("at")
        AuditLog.objects.filter(pk=row.pk).update(at=self.ago(days))

    def rewound(self, product, days):
        product.refresh_from_db()
        rows = history.rewind_stock([product], self.day(days))
        return rows[0] if rows else None

    def run_job(self, **kw):
        out = StringIO()
        call_command("save_inventory_snapshots", stdout=out, stderr=StringIO(), **kw)
        return out.getvalue()

    def page(self, days, **params):
        params.setdefault("level", "all")
        params.setdefault("branch", str(self.silom.id))
        res = self.client.get(reverse("backoffice:inventory"),
                              {"as_of": self.day(days).isoformat(), **params})
        self.assertEqual(res.status_code, 200)
        return res

    def row(self, res, name="Mooncake"):
        rows = [p for p in res.context["products"] if p.name == name]
        return rows[0] if rows else None


class RewindTests(HistoryTestCase):
    def test_bills_before_and_after_an_edit_are_both_right(self):
        self.sell(self.cake, 2, days=8)         # 10 -> 8
        self.edit(self.cake, 5, stock=50)       # 8 -> 50
        self.sell(self.cake, 3, days=2)         # 50 -> 47
        for days, stock in ((9, 10), (7, 8), (4, 50), (1, 47)):
            self.assertEqual(self.rewound(self.cake, days).stock, stock, days)

    def test_an_unlogged_change_after_the_next_edit_does_not_reach_back(self):
        self.edit(self.cake, 5, stock=50)
        Product.objects.filter(pk=self.cake.pk).update(stock=999)   # no record at all
        self.assertEqual(self.rewound(self.cake, 6).stock, 10)

    def test_stock_put_back_as_a_stock_in_on_the_bill_is_seen(self):
        # How the system cancelled duplicate bills on 27 Oct 2026.
        order = self.sell(self.cake, 3, days=8)
        Product.objects.filter(pk=self.cake.pk).update(stock=10)
        StockMovement.objects.create(branch=self.silom, product=self.cake, type="in",
                                     qty=3, document_no=order.order_number)
        self.assertEqual(self.rewound(self.cake, 2).stock, 7)

    def test_cost_and_par_level_are_the_days_own(self):
        self.edit(self.cake, 5, cost=Decimal("35.00"), par_level=12)
        before = self.rewound(self.cake, 6)
        self.assertEqual((before.cost, before.par_level), (Decimal("20.00"), 0))
        after = self.rewound(self.cake, 4)
        self.assertEqual((after.cost, after.par_level), (Decimal("35.00"), 12))

    def test_a_cost_not_entered_yet_is_the_first_one_entered(self):
        Product.objects.filter(pk=self.cake.pk).update(cost=0)
        self.edit(self.cake, 8, cost=Decimal("35.00"))
        self.edit(self.cake, 5, cost=Decimal("40.00"))
        self.assertEqual(self.rewound(self.cake, 9).cost, Decimal("35.00"))
        self.assertEqual(self.rewound(self.cake, 6).cost, Decimal("35.00"))
        self.assertEqual(self.rewound(self.cake, 4).cost, Decimal("40.00"))

    def test_a_cost_cleared_since_is_still_the_days_own(self):
        self.edit(self.cake, 5, cost=Decimal("0.00"))
        self.assertEqual(self.rewound(self.cake, 6).cost, Decimal("20.00"))
        self.assertEqual(self.rewound(self.cake, 4).cost, Decimal("0.00"))

    def test_a_product_removed_since_is_listed_while_it_was_on_sale(self):
        self.edit(self.cake, 5, active=False)
        self.assertEqual(self.row(self.page(6)).stock, 10)
        self.assertIsNone(self.row(self.page(4)))

    def test_a_product_put_back_on_sale_is_not_listed_while_it_was_off(self):
        self.edit(self.cake, 8, active=False)
        self.edit(self.cake, 3, active=True)
        self.assertIsNone(self.rewound(self.cake, 5))
        self.assertIsNotNone(self.rewound(self.cake, 2))
        self.assertIsNotNone(self.rewound(self.cake, 9))


class SnapshotTests(HistoryTestCase):
    def test_only_days_more_than_15_days_old_are_saved(self):
        self.run_job()
        saved = set(InventorySnapshot.objects.values_list("date", flat=True))
        self.assertIn(self.day(16), saved)
        self.assertNotIn(self.day(15), saved)
        self.assertEqual(min(saved), self.day(30))
        self.assertEqual(len(saved), 15)

    def test_an_open_day_cannot_be_saved(self):
        with self.assertRaises(ValueError):
            history.save_day(self.day(15))

    def test_a_row_holds_the_days_figures(self):
        self.sell(self.cake, 3, days=18)
        self.run_job()
        snap = InventorySnapshot.objects.get(date=self.day(17), product_id=self.cake.id)
        self.assertEqual((snap.branch_name, snap.name, snap.stock, snap.cost, snap.sold_7d),
                         ("Silom", "Mooncake", 7, Decimal("20.00"), 3))
        before = InventorySnapshot.objects.get(date=self.day(19), product_id=self.cake.id)
        self.assertEqual((before.stock, before.sold_7d), (10, 0))

    def test_the_page_shows_saved_figures_and_nothing_moves_them(self):
        self.run_job()
        self.edit(self.cake, 0, stock=99, cost=Decimal("55.00"), name="Renamed")
        AuditLog.objects.filter(model="Product", action="update").delete()
        res = self.page(16)
        self.assertTrue(res.context["saved"])
        row = self.row(res)
        self.assertEqual((row.stock, row.cost, row.value), (10, Decimal("20.00"), Decimal("200.00")))
        self.assertContains(res, "Saved figures.")

    def test_day_15_is_worked_out(self):
        self.run_job()
        res = self.page(15)
        self.assertFalse(res.context["saved"])
        self.assertContains(res, "Worked out from")

    def test_saved_and_worked_out_figures_agree(self):
        self.sell(self.cake, 3, days=18)
        self.edit(self.cake, 14, stock=40, cost=Decimal("35.00"))
        live = self.row(self.page(16))
        self.assertFalse(self.page(16).context["saved"])
        figures = lambda r: (r.stock, r.cost, r.value, r.sold_7d, r.days_cover, r.level_label)  # noqa: E731
        live = figures(live)
        self.run_job()
        self.assertTrue(self.page(16).context["saved"])
        self.assertEqual(figures(self.row(self.page(16))), live)

    def test_a_closed_day_not_saved_yet_is_worked_out(self):
        res = self.page(16)
        self.assertFalse(res.context["saved"])
        self.assertEqual(self.row(res).stock, 10)

    def test_a_saved_day_is_never_rewritten(self):
        self.run_job()
        InventorySnapshot.objects.filter(date=self.day(16)).update(stock=123)
        self.assertEqual(history.save_day(self.day(16)), 0)
        self.assertEqual(self.run_job().strip(), "Nothing to save.")
        self.assertEqual(self.row(self.page(16)).stock, 123)

    def test_merged_branches(self):
        self.run_job()
        res = self.page(16, branches=[str(self.silom.id), str(self.paragon.id)])
        row = self.row(res)
        self.assertEqual((row.stock, row.value), (14, Decimal("280.00")))
        self.assertEqual(sorted(row.by_branch), [("Paragon", 4), ("Silom", 10)])

    def test_one_branch_does_not_show_anothers(self):
        self.run_job()
        self.assertEqual(self.page(16).context["sku_count"], 1)

    def test_search_sort_and_level_work_on_saved_rows(self):
        make_product(self.silom, name="Apple pie", stock=0)
        AuditLog.objects.filter(action="create").update(at=self.ago(30))
        self.run_job()
        self.assertEqual([p.name for p in self.page(16, q="APPLE").context["products"]], ["Apple pie"])
        self.assertEqual([p.name for p in self.page(16, q="drinks", field="category").context["products"]],
                         ["Apple pie", "Mooncake"])
        self.assertEqual([p.name for p in self.page(16, q="drinks", field="name").context["products"]], [])
        self.assertEqual([p.name for p in self.page(16, sort="stock_max").context["products"]],
                         ["Mooncake", "Apple pie"])
        self.assertEqual([p.name for p in self.page(16, level="out").context["products"]], ["Apple pie"])

    def test_export_uses_saved_figures(self):
        self.run_job()
        InventorySnapshot.objects.filter(date=self.day(16)).update(stock=77)
        for params, expected in (
            ({"branch": str(self.silom.id)}, "77"),
            ({"branches": [str(self.silom.id), str(self.paragon.id)]}, "154"),
        ):
            res = self.client.get(reverse("backoffice:inventory_export"),
                                  {"as_of": self.day(16).isoformat(), **params})
            rows = list(csv.reader(io.StringIO(res.content.decode("utf-8-sig"))))
            self.assertEqual({r[2]: r[5] for r in rows[6:]}["Mooncake"], expected)

    def test_deleting_the_product_or_the_branch_keeps_its_history(self):
        self.run_job()
        Product.objects.filter(pk=self.cake.pk).delete()
        row = self.row(self.page(16))
        self.assertEqual((row.stock, row.id), (10, None))
        count = InventorySnapshot.objects.filter(branch_id=self.paragon.id).count()
        with audit.pause():     # its rows would point at the deleted branch
            Branch.objects.filter(pk=self.paragon.pk).delete()
        self.assertEqual(InventorySnapshot.objects.filter(branch_name="Paragon").count(), count)

    def test_saved_days_stay_reachable_if_the_audit_log_is_trimmed(self):
        self.run_job()
        AuditLog.objects.all().delete()
        self.assertTrue(self.page(30).context["saved"])

    def test_dry_run_writes_and_sends_nothing(self):
        self.assertIn("would save 2 rows, 2 branches, 14 units", self.run_job(dry_run=True))
        self.assertFalse(InventorySnapshot.objects.exists())
        self.send.assert_not_called()


class AlertTests(HistoryTestCase):
    def test_the_first_run_is_not_a_missed_night(self):
        self.run_job()
        self.send.assert_not_called()

    def test_a_missed_night_is_caught_up_and_alerted_to_bot_testing(self):
        self.run_job()
        InventorySnapshot.objects.filter(date=self.day(18)).delete()
        self.run_job()
        self.assertTrue(history.is_saved(self.day(18)))
        group, text = self.send.call_args[0]
        self.assertEqual(group, "MzM0NzQ2MjIyOTM5")
        self.assertIn(self.day(18).strftime("%d %b %Y"), text)
        self.assertIn("Saved late", text)

    def test_the_night_run_on_time_is_silent(self):
        self.run_job()
        InventorySnapshot.objects.filter(date=self.day(16)).delete()
        with mock.patch.object(history, "is_late", return_value=False):
            self.run_job()
        self.send.assert_not_called()

    def test_late_means_two_hours_after_midnight(self):
        midnight, _ = history.day_window(self.today, self.today)
        with mock.patch("bravepos.inventory_history.timezone.now") as now:
            now.return_value = midnight + timedelta(minutes=30)
            self.assertFalse(history.is_late(self.day(16)))
            now.return_value = midnight + timedelta(hours=8, minutes=30)
            self.assertTrue(history.is_late(self.day(16)))

    def test_a_failed_day_is_alerted_and_does_not_stop_the_rest(self):
        real = history.save_day

        def flaky(day):
            if day == self.day(20):
                raise RuntimeError("database went away")
            return real(day)

        with mock.patch.object(history, "save_day", side_effect=flaky):
            with self.assertRaises(RuntimeError):
                self.run_job()
        self.assertFalse(history.is_saved(self.day(20)))
        self.assertTrue(history.is_saved(self.day(16)))
        text = self.send.call_args[0][1]
        self.assertIn("NOT saved", text)
        self.assertIn("database went away", text)


class ClosedBillTests(HistoryTestCase):
    def setUp(self):
        super().setUp()
        open_shift(self.silom)
        staff = Staff.objects.create(name="Nok", role="cashier", active=True)
        session = BranchSession.objects.create(token="t-hist", staff=staff, branch=self.silom)
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {session.token}"}

    def put(self, order, **body):
        return self.client.put(f"/api/orders/{order.id}/status", data=body,
                               content_type="application/json", **self.auth)

    def test_day_15_can_still_be_voided(self):
        order = self.sell(self.cake, 3, days=15)
        self.assertEqual(self.put(order, status="cancel", cancel_type="void").status_code, 200)

    def test_day_16_cannot_be_voided_or_refunded(self):
        order = self.sell(self.cake, 3, days=16)
        for kind in ("void", "refund", ""):
            res = self.put(order, status="cancel", cancel_type=kind)
            self.assertEqual(res.status_code, 409, kind)
            self.assertIn("15 days", res.json()["detail"])
        order.refresh_from_db()
        self.cake.refresh_from_db()
        self.assertEqual((order.status, order.voided_at, self.cake.stock), ("completed", None, 7))

    def test_a_closed_void_cannot_be_refunded_or_brought_back(self):
        order = self.sell(self.cake, 3)
        self.put(order, status="cancel", cancel_type="void")
        Order.objects.filter(pk=order.pk).update(created_at=self.ago(16))
        self.assertEqual(self.put(order, status="cancel", cancel_type="refund").status_code, 409)
        self.assertEqual(self.put(order, status="completed").status_code, 409)
        order.refresh_from_db()
        self.assertEqual((order.status, order.cancel_type), ("cancel", "void"))

    def test_the_refusal_comes_before_beam(self):
        order = self.sell(self.cake, 1, days=16)
        Order.objects.filter(pk=order.pk).update(payment_method="Beam QR", beam_charge_id="ch_1")
        with mock.patch("bravepos.gateways.beam_refund_order") as beam:
            self.put(order, status="cancel", cancel_type="refund")
        beam.assert_not_called()

    def test_kitchen_flow_on_an_old_bill_still_works(self):
        order = self.sell(self.cake, 1, days=16)
        Order.objects.filter(pk=order.pk).update(status="preparing")
        self.assertEqual(self.put(order, status="completed").status_code, 200)

    def test_the_till_is_told_which_bills_are_closed(self):
        old, recent = self.sell(self.cake, 1, days=16), self.sell(self.cake, 1, days=15)
        rows = {o["id"]: o["cancel_closed"]
                for o in self.client.get("/api/orders", **self.auth).json()}
        self.assertEqual((rows[str(old.id)], rows[str(recent.id)]), (True, False))
