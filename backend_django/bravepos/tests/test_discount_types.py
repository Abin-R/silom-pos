"""Preset discounts at the till, and the SeaTalk alert for "Other".

What these pin down:

* a branch without ``discount_types_enabled`` sees no presets and raises no
  alert — this ships dark and is switched on at the test branch only;
* the feed only offers this branch's active presets, with the product list
  for the ones that do not apply to everything;
* the preset name and the cashier's reason are stored on the order line;
* an "Other" discount on a saved sale posts one alert with the reason, and
  a failing SeaTalk never costs the sale.

SeaTalk is mocked throughout — these tests make no network calls.
"""
from __future__ import annotations

from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from bravepos import seatalk
from bravepos.models import (
    Branch, BranchSession, DiscountType, OrderItem, Staff,
)
from bravepos.tests.factories import make_branch, make_product, make_shop, open_shift


class TillTestCase(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch(name="test branch")
        self.branch.discount_types_enabled = True
        self.branch.save()
        self.latte = make_product(self.branch, name="Latte", price="100.00")
        self.cake = make_product(self.branch, name="Cake", price="200.00")
        open_shift(self.branch)
        staff = Staff.objects.create(name="Ploy", email="ploy@test.local",
                                     password_hash="x", role="cashier")
        staff.branches.add(self.branch)
        session = BranchSession.objects.create(token="dis" * 12, branch=self.branch, staff=staff)
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {session.token}"}

    def sell(self, item_extra):
        item = {"product_id": str(self.latte.id), "name": "Latte", "price": 100,
                "qty": 2, "discount": 20, **item_extra}
        return self.client.post("/api/orders", {
            "items": [item], "subtotal": 200, "total": 180, "discount_amount": 20,
            "payment_method": "cash", "paid_amount": 180, "staff": "Ploy",
        }, content_type="application/json", **self.auth)


class FeedTests(TillTestCase):
    def test_new_branches_default_to_off(self):
        self.assertFalse(Branch.objects.create(name="Fresh").discount_types_enabled)

    def test_branch_switched_off_gets_nothing(self):
        DiscountType.objects.create(branch=self.branch, name="Staff", value=10)
        self.branch.discount_types_enabled = False
        self.branch.save()
        res = self.client.get("/api/discount-types", **self.auth)
        self.assertEqual(res.json(), {"enabled": False, "discount_types": []})

    def test_offers_active_presets_of_this_branch_only(self):
        DiscountType.objects.create(branch=self.branch, name="Staff", value=10)
        DiscountType.objects.create(branch=self.branch, name="Old", value=5, active=False)
        other = make_branch(name="Silom")
        DiscountType.objects.create(branch=other, name="Elsewhere", value=5)
        some = DiscountType.objects.create(branch=self.branch, name="Cake ฿30",
                                           kind="fixed", value=30, all_products=False)
        some.products.set([self.cake])

        res = self.client.get("/api/discount-types", **self.auth).json()
        self.assertTrue(res["enabled"])
        by_name = {d["name"]: d for d in res["discount_types"]}
        self.assertEqual(set(by_name), {"Staff", "Cake ฿30"})
        self.assertEqual(by_name["Staff"]["product_ids"], [])
        self.assertTrue(by_name["Staff"]["all_products"])
        self.assertEqual(by_name["Cake ฿30"]["product_ids"], [str(self.cake.id)])
        self.assertEqual(by_name["Cake ฿30"]["kind"], "fixed")

    def test_needs_a_session(self):
        self.assertEqual(self.client.get("/api/discount-types").status_code, 401)


@mock.patch("bravepos.seatalk.send_group_text_async")
class AlertTests(TillTestCase):
    def test_preset_is_stored_and_not_alerted(self, send):
        res = self.sell({"discount_label": "Staff"})
        self.assertEqual(res.status_code, 201)
        line = OrderItem.objects.get()
        self.assertEqual(line.discount_label, "Staff")
        self.assertEqual(line.discount_reason, "")
        send.assert_not_called()

    def test_other_discount_alerts_with_the_reason(self, send):
        with mock.patch.dict("os.environ", {"SEATALK_DISCOUNT_GROUP_ID": "grp"}):
            res = self.sell({"discount_label": "Other",
                             "discount_reason": "Box dented in delivery"})
        self.assertEqual(res.status_code, 201)
        line = OrderItem.objects.get()
        self.assertEqual(line.discount_reason, "Box dented in delivery")
        send.assert_called_once()
        group, text = send.call_args[0]
        self.assertEqual(group, "grp")
        self.assertIn("Box dented in delivery", text)
        self.assertIn("test branch", text)
        self.assertIn(res.json()["order_number"], text)
        self.assertIn("Latte × 2", text)
        self.assertIn("฿20.00 (10%)", text)
        self.assertIn("Ploy", text)

    def test_no_alert_when_branch_switched_off(self, send):
        self.branch.discount_types_enabled = False
        self.branch.save()
        self.sell({"discount_label": "Other", "discount_reason": "x"})
        send.assert_not_called()

    def test_a_failing_alert_never_costs_the_sale(self, send):
        send.side_effect = RuntimeError("seatalk down")
        res = self.sell({"discount_label": "Other", "discount_reason": "x"})
        self.assertEqual(res.status_code, 201)

    def test_old_till_without_the_fields_is_unchanged(self, send):
        res = self.sell({})
        self.assertEqual(res.status_code, 201)
        line = OrderItem.objects.get()
        self.assertEqual((line.discount_label, line.discount_reason), ("", ""))
        self.assertEqual(line.discount, Decimal("20"))
        send.assert_not_called()


class SeatalkClientTests(TestCase):
    def test_unconfigured_sends_nothing(self):
        with mock.patch.dict("os.environ", {"SEATALK_APP_ID": "", "SEATALK_APP_SECRET": ""}), \
                mock.patch("bravepos.seatalk.httpx.post") as post:
            self.assertFalse(seatalk.send_group_text("grp", "hi"))
            self.assertIsNone(seatalk.send_group_text_async("grp", "hi"))
        post.assert_not_called()

    def test_posts_text_to_the_group(self):
        token = mock.Mock(**{"json.return_value": {"code": 0, "app_access_token": "T"}})
        sent = mock.Mock(**{"json.return_value": {"code": 0}})
        env = {"SEATALK_APP_ID": "a", "SEATALK_APP_SECRET": "s"}
        with mock.patch.dict("os.environ", env), \
                mock.patch("bravepos.seatalk.cache.get", return_value=None), \
                mock.patch("bravepos.seatalk.httpx.post", side_effect=[token, sent]) as post:
            self.assertTrue(seatalk.send_group_text("grp", "hello"))
        body = post.call_args_list[1].kwargs["json"]
        self.assertEqual(body, {"group_id": "grp",
                                "message": {"tag": "text", "text": {"content": "hello"}}})
        self.assertEqual(post.call_args_list[1].kwargs["headers"]["Authorization"], "Bearer T")

    def test_network_error_is_swallowed(self):
        env = {"SEATALK_APP_ID": "a", "SEATALK_APP_SECRET": "s"}
        with mock.patch.dict("os.environ", env), \
                mock.patch("bravepos.seatalk.httpx.post", side_effect=OSError("down")):
            self.assertFalse(seatalk.send_group_text("grp", "hello"))


class BackofficeTests(TestCase):
    def setUp(self):
        self.admin = Staff(name="Admin", username="adm", email="adm@therollingpinn.com",
                           role="admin", active=True, backoffice_access=True)
        self.admin.set_password("pw-long-enough")
        self.admin.save()
        make_shop()
        self.branch = make_branch(name="test branch")
        self.cake = make_product(self.branch, name="Cake", price="200.00")
        self.client.post(reverse("backoffice:login"),
                         {"username": "adm", "password": "pw-long-enough"})
        self.qs = f"?branch={self.branch.id}"

    def test_list_and_form_render(self):
        DiscountType.objects.create(branch=self.branch, name="Staff", value=10)
        self.assertContains(self.client.get(reverse("backoffice:discount_list") + self.qs), "Staff")
        self.assertContains(self.client.get(reverse("backoffice:discount_new") + self.qs), "Cake")

    def test_admin_switches_the_branch_on(self):
        self.client.post(reverse("backoffice:discount_list") + self.qs, {"enabled": "on"})
        self.branch.refresh_from_db()
        self.assertTrue(self.branch.discount_types_enabled)
        self.client.post(reverse("backoffice:discount_list") + self.qs, {})
        self.branch.refresh_from_db()
        self.assertFalse(self.branch.discount_types_enabled)

    def test_create_for_selected_products(self):
        res = self.client.post(reverse("backoffice:discount_new") + self.qs, {
            "name": "Cake deal", "kind": "fixed", "value": "30", "active": "on",
            "applies_to": "selected", "products": [str(self.cake.id)],
        })
        self.assertEqual(res.status_code, 302)
        dt = DiscountType.objects.get()
        self.assertEqual((dt.branch, dt.kind, dt.value, dt.all_products),
                         (self.branch, "fixed", Decimal("30"), False))
        self.assertEqual(list(dt.products.all()), [self.cake])

    def test_rejects_bad_input(self):
        for bad in (
            {"name": "", "kind": "percent", "value": "10", "applies_to": "all"},
            {"name": "x", "kind": "percent", "value": "150", "applies_to": "all"},
            {"name": "x", "kind": "fixed", "value": "0", "applies_to": "all"},
            {"name": "x", "kind": "fixed", "value": "5", "applies_to": "selected"},
            # "Other" is the till's own hand-entered entry.
            {"name": "other", "kind": "fixed", "value": "5", "applies_to": "all"},
        ):
            res = self.client.post(reverse("backoffice:discount_new") + self.qs, bad)
            self.assertEqual(res.status_code, 200, bad)
        self.assertFalse(DiscountType.objects.exists())
