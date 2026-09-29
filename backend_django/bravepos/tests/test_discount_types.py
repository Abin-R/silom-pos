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
    Branch, BranchSession, Category, DiscountCondition, DiscountType, OrderItem, Staff,
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
                                           kind="fixed", value=30, applies_to="products")
        some.products.set([self.cake])

        res = self.client.get("/api/discount-types", **self.auth).json()
        self.assertTrue(res["enabled"])
        by_name = {d["name"]: d for d in res["discount_types"]}
        self.assertEqual(set(by_name), {"Staff", "Cake ฿30"})
        self.assertEqual(by_name["Staff"]["product_ids"], [])
        self.assertTrue(by_name["Staff"]["all_products"])
        self.assertEqual(by_name["Cake ฿30"]["product_ids"], [str(self.cake.id)])
        self.assertEqual(by_name["Cake ฿30"]["kind"], "fixed")

    def test_category_preset_covers_its_products_including_new_ones(self):
        cookies = Category.objects.create(name="Cookies", branch=self.branch)
        self.latte.category = cookies
        self.latte.save()
        dt = DiscountType.objects.create(branch=self.branch, name="Cookie 10%",
                                         value=10, applies_to="categories")
        dt.categories.set([cookies])

        def offered():
            res = self.client.get("/api/discount-types", **self.auth).json()
            (row,) = res["discount_types"]
            return row

        row = offered()
        self.assertFalse(row["all_products"])
        self.assertEqual(row["applies_to"], "categories")
        self.assertEqual(row["category_ids"], [str(cookies.id)])
        self.assertEqual(row["product_ids"], [str(self.latte.id)])

        # Added to the category after the preset was made: covered, no edit.
        new = make_product(self.branch, name="Oat cookie", price="80.00")
        new.category = cookies
        new.save()
        self.assertEqual(set(offered()["product_ids"]), {str(self.latte.id), str(new.id)})

        # Retired, or moved out of the category: dropped.
        new.active = False
        new.save()
        self.latte.category = None
        self.latte.save()
        self.assertEqual(offered()["product_ids"], [])

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
            "applies_to": "products", "products": [str(self.cake.id)],
        })
        self.assertEqual(res.status_code, 302)
        dt = DiscountType.objects.get()
        self.assertEqual((dt.branch, dt.kind, dt.value, dt.applies_to),
                         (self.branch, "fixed", Decimal("30"), "products"))
        self.assertEqual(list(dt.products.all()), [self.cake])

    def test_create_for_categories_then_switch_to_all(self):
        cakes = Category.objects.create(name="Cakes", branch=self.branch)
        res = self.client.post(reverse("backoffice:discount_new") + self.qs, {
            "name": "Cake week", "kind": "percent", "value": "15", "active": "on",
            "applies_to": "categories", "categories": [str(cakes.id)],
        })
        self.assertEqual(res.status_code, 302)
        dt = DiscountType.objects.get()
        self.assertEqual(dt.applies_to, "categories")
        self.assertEqual(list(dt.categories.all()), [cakes])
        self.assertContains(self.client.get(reverse("backoffice:discount_list") + self.qs), "Cakes")

        # Switching mode must not leave the old categories quietly attached.
        self.client.post(reverse("backoffice:discount_detail", args=[dt.id]) + self.qs, {
            "name": "Cake week", "kind": "percent", "value": "15", "active": "on",
            "applies_to": "all",
        })
        dt.refresh_from_db()
        self.assertEqual(dt.applies_to, "all")
        self.assertFalse(dt.categories.exists())

    def test_rejects_bad_input(self):
        for bad in (
            {"name": "", "kind": "percent", "value": "10", "applies_to": "all"},
            {"name": "x", "kind": "percent", "value": "150", "applies_to": "all"},
            {"name": "x", "kind": "fixed", "value": "0", "applies_to": "all"},
            {"name": "x", "kind": "fixed", "value": "5", "applies_to": "products"},
            {"name": "x", "kind": "fixed", "value": "5", "applies_to": "categories"},
            # "Other" is the till's own hand-entered entry.
            {"name": "other", "kind": "fixed", "value": "5", "applies_to": "all"},
        ):
            res = self.client.post(reverse("backoffice:discount_new") + self.qs, bad)
            self.assertEqual(res.status_code, 200, bad)
        self.assertFalse(DiscountType.objects.exists())


class ComboAndFreeFeedTests(TillTestCase):
    """Combination and free-item discounts reach only a till that asks for v2."""

    def setUp(self):
        super().setUp()
        self.drinks = Category.objects.create(name="Drinks", branch=self.branch)
        self.latte.category = self.drinks
        self.latte.save()
        self.combo = DiscountType.objects.create(
            branch=self.branch, name="Cake + drink", kind="fixed", value=30,
            applies_to="combo")
        DiscountCondition.objects.create(discount_type=self.combo, product=self.cake,
                                         min_qty=1, sort_order=0)
        DiscountCondition.objects.create(discount_type=self.combo, category=self.drinks,
                                         min_qty=2, sort_order=1)
        self.free = DiscountType.objects.create(
            branch=self.branch, name="Free latte", kind="free",
            free_product=self.latte, free_qty=2)
        DiscountType.objects.create(branch=self.branch, name="Staff", value=10)

    def feed(self, v2):
        url = "/api/discount-types" + ("?v=2" if v2 else "")
        return {d["name"]: d for d in self.client.get(url, **self.auth).json()["discount_types"]}

    def test_old_till_never_sees_combos_or_free_items(self):
        self.assertEqual(set(self.feed(False)), {"Staff"})

    def test_v2_carries_conditions_and_free_item(self):
        rows = self.feed(True)
        self.assertEqual(set(rows), {"Staff", "Cake + drink", "Free latte"})
        combo = rows["Cake + drink"]
        self.assertFalse(combo["all_products"])
        self.assertEqual(combo["product_ids"], [])
        self.assertEqual(combo["conditions"], [
            {"product_ids": [str(self.cake.id)], "min_qty": 1},
            {"product_ids": [str(self.latte.id)], "min_qty": 2},
        ])
        free = rows["Free latte"]
        self.assertEqual(free["free_product"], {"id": str(self.latte.id), "name": "Latte",
                                                "price": 100.0})
        self.assertEqual(free["free_qty"], 2)

    def test_free_item_whose_product_is_retired_is_dropped(self):
        self.latte.active = False
        self.latte.save()
        self.assertNotIn("Free latte", self.feed(True))


class ComboFormTests(TestCase):
    def setUp(self):
        admin = Staff(name="Admin", username="adm", email="adm@therollingpinn.com",
                      role="admin", active=True, backoffice_access=True)
        admin.set_password("pw-long-enough")
        admin.save()
        make_shop()
        self.branch = make_branch(name="test branch")
        self.cookies = Category.objects.create(name="Cookies", branch=self.branch)
        self.cookie = make_product(self.branch, name="Choc chip", price="95.00")
        self.cookie.category = self.cookies
        self.cookie.save()
        self.latte = make_product(self.branch, name="Latte", price="100.00")
        self.client.post(reverse("backoffice:login"),
                         {"username": "adm", "password": "pw-long-enough"})
        self.url = reverse("backoffice:discount_new") + f"?branch={self.branch.id}"

    def post(self, **fields):
        body = {"name": "Set", "active": "on", "applies_to": "combo", "kind": "fixed",
                "value": "30", "cond_target": [f"p:{self.latte.id}", f"c:{self.cookies.id}"],
                "cond_qty": ["1", "1"]}
        body.update(fields)
        return self.client.post(self.url, body)

    def test_creates_rows_in_order(self):
        self.assertEqual(self.post().status_code, 302)
        dt = DiscountType.objects.get()
        rows = [(c.product, c.category, c.min_qty) for c in dt.conditions.all()]
        self.assertEqual(rows, [(self.latte, None, 1), (None, self.cookies, 1)])
        page = self.client.get(reverse("backoffice:discount_list") + f"?branch={self.branch.id}")
        self.assertContains(page, "Latte + Cookies")
        edit = self.client.get(reverse("backoffice:discount_detail", args=[dt.id])
                               + f"?branch={self.branch.id}")
        # The saved rows come back selected when the discount is edited.
        html = edit.content.decode()
        self.assertRegex(html, rf'value="c:{self.cookies.id}"[^>]*\s+selected')
        self.assertRegex(html, rf'value="p:{self.latte.id}"[^>]*\s+selected')

    def test_fixed_discount_must_be_under_the_cheapest_combo(self):
        # Latte 100 + cheapest cookie 95 = 195.
        self.assertEqual(self.post(value="195").status_code, 200)
        self.assertEqual(self.post(value="194.99").status_code, 302)

    def test_quantity_counts_toward_the_cheapest_combo(self):
        self.assertEqual(self.post(value="250", cond_qty=["1", "2"]).status_code, 302)  # 100+190

    def test_empty_category_can_never_be_met(self):
        empty = Category.objects.create(name="Empty", branch=self.branch)
        res = self.post(cond_target=[f"c:{empty.id}"], cond_qty=["1"])
        self.assertEqual(res.status_code, 200)
        self.assertFalse(DiscountType.objects.exists())

    def test_rejects_no_rows_foreign_targets_and_free_on_combo(self):
        other = make_product(make_branch(name="Silom"), name="Elsewhere")
        for bad in ({"cond_target": [""], "cond_qty": ["1"]},
                    {"cond_target": [f"p:{other.id}"], "cond_qty": ["1"]},
                    {"kind": "free", "free_product": str(self.latte.id)}):
            self.assertEqual(self.post(**bad).status_code, 200, bad)
        self.assertFalse(DiscountType.objects.exists())

    def test_free_item_discount(self):
        res = self.client.post(self.url, {
            "name": "Free latte with cookie", "active": "on", "applies_to": "categories",
            "categories": [str(self.cookies.id)], "kind": "free",
            "free_product": str(self.latte.id), "free_qty": "1"})
        self.assertEqual(res.status_code, 302)
        dt = DiscountType.objects.get()
        self.assertEqual((dt.kind, dt.free_product, dt.free_qty, dt.value),
                         ("free", self.latte, 1, Decimal(0)))
        # Missing product → refused.
        self.assertEqual(self.client.post(self.url, {
            "name": "x", "applies_to": "all", "kind": "free", "free_qty": "1"}).status_code, 200)

    def test_switching_away_from_combo_drops_its_rows(self):
        self.post()
        dt = DiscountType.objects.get()
        self.client.post(reverse("backoffice:discount_detail", args=[dt.id])
                         + f"?branch={self.branch.id}",
                         {"name": "Set", "active": "on", "applies_to": "all",
                          "kind": "percent", "value": "10"})
        dt.refresh_from_db()
        self.assertEqual(dt.applies_to, "all")
        self.assertFalse(dt.conditions.exists())


class PromotionPeriodTests(TillTestCase):
    """Promotion ID, start/end dates, and the status derived from them."""

    def test_ids_are_sequential_and_permanent(self):
        a = DiscountType.objects.create(branch=self.branch, name="A", value=5)
        b = DiscountType.objects.create(branch=self.branch, name="B", value=5)
        self.assertEqual((a.code, b.code), ("PR-0001", "PR-0002"))
        a.name = "A renamed"
        a.save()
        a.refresh_from_db()
        self.assertEqual(a.code, "PR-0001")
        b.delete()
        self.assertEqual(DiscountType.objects.create(branch=self.branch, name="C", value=5).code,
                         "PR-0002")  # next after the highest still on file

    def test_status_follows_the_dates(self):
        from datetime import date
        d = DiscountType(start_date=date(2026, 9, 10), end_date=date(2026, 9, 20), active=True)
        self.assertEqual(d.status_on(date(2026, 9, 9)), "scheduled")
        self.assertEqual(d.status_on(date(2026, 9, 10)), "active")   # inclusive
        self.assertEqual(d.status_on(date(2026, 9, 20)), "active")   # inclusive
        self.assertEqual(d.status_on(date(2026, 9, 21)), "ended")
        d.active = False
        self.assertEqual(d.status_on(date(2026, 9, 15)), "paused")
        self.assertEqual(DiscountType(active=True).status_on(date(2030, 1, 1)), "active")

    def test_feed_offers_only_promotions_running_today(self):
        from datetime import timedelta
        from django.utils import timezone
        today = timezone.localdate()
        DiscountType.objects.create(branch=self.branch, name="Open", value=5)
        DiscountType.objects.create(branch=self.branch, name="Today only", value=5,
                                    start_date=today, end_date=today)
        DiscountType.objects.create(branch=self.branch, name="Tomorrow", value=5,
                                    start_date=today + timedelta(days=1))
        DiscountType.objects.create(branch=self.branch, name="Yesterday", value=5,
                                    end_date=today - timedelta(days=1))
        rows = {d["name"]: d for d in
                self.client.get("/api/discount-types?v=2", **self.auth).json()["discount_types"]}
        self.assertEqual(set(rows), {"Open", "Today only"})
        self.assertEqual(rows["Today only"]["start_date"], today.isoformat())
        self.assertEqual(rows["Open"]["end_date"], None)
        self.assertEqual(rows["Open"]["summary"], "All products")
        self.assertTrue(rows["Open"]["code"].startswith("PR-"))


class PromotionFormTests(ComboFormTests):
    def test_saves_dates_and_lists_them(self):
        res = self.client.post(self.url, {
            "name": "September", "active": "on", "applies_to": "all", "kind": "percent",
            "value": "10", "start_date": "2026-09-01", "end_date": "2026-09-30"})
        self.assertEqual(res.status_code, 302)
        dt = DiscountType.objects.get()
        self.assertEqual((str(dt.start_date), str(dt.end_date)), ("2026-09-01", "2026-09-30"))
        page = self.client.get(reverse("backoffice:discount_list") + f"?branch={self.branch.id}")
        self.assertContains(page, "1 Sep 2026")
        self.assertContains(page, "30 Sep 2026")
        self.assertContains(page, dt.code)

    def test_end_before_start_is_refused(self):
        res = self.client.post(self.url, {
            "name": "Backwards", "active": "on", "applies_to": "all", "kind": "percent",
            "value": "10", "start_date": "2026-09-30", "end_date": "2026-09-01"})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(DiscountType.objects.exists())


class BranchSyncTests(TestCase):
    """One promotion at several branches: a copy per branch, same ID,
    pointing at each branch's own products and categories."""

    def setUp(self):
        admin = Staff(name="Admin", username="adm", email="adm@therollingpinn.com",
                      role="admin", active=True, backoffice_access=True)
        admin.set_password("pw-long-enough")
        admin.save()
        make_shop()
        self.a = make_branch(name="Alpha")
        self.b = make_branch(name="Bravo")
        self.c = make_branch(name="Charlie")
        self.menu = {}
        for br in (self.a, self.b, self.c):
            cookies = Category.objects.create(name="Cookies", branch=br)
            cookie = make_product(br, name="Choc chip", price="95.00")
            cookie.category = cookies
            cookie.save()
            self.menu[br.name] = {"cookies": cookies, "cookie": cookie}
        # Only Alpha and Bravo sell lattes.
        for br in (self.a, self.b):
            self.menu[br.name]["latte"] = make_product(br, name="LATTE", price="100.00")
        self.client.post(reverse("backoffice:login"),
                         {"username": "adm", "password": "pw-long-enough"})
        self.new_url = reverse("backoffice:discount_new") + f"?branch={self.a.id}"

    def create(self, **fields):
        body = {"name": "Set", "active": "on", "applies_to": "combo", "kind": "fixed",
                "value": "30",
                "cond_target": [f"p:{self.menu['Alpha']['latte'].id}",
                                f"c:{self.menu['Alpha']['cookies'].id}"],
                "cond_qty": ["1", "1"], "sync_scope": "all"}
        body.update(fields)
        return self.client.post(self.new_url, body, follow=True)

    def test_all_branches_copies_with_local_products_and_skips_what_is_missing(self):
        res = self.create()
        src = DiscountType.objects.get(branch=self.a)
        copy = DiscountType.objects.get(branch=self.b)
        self.assertEqual((copy.code, copy.group_id, copy.value, copy.applies_to),
                         (src.code, src.group_id, src.value, "combo"))
        rows = [(r.product, r.category) for r in copy.conditions.all()]
        # Matched by name, case-insensitively, at Bravo's own rows.
        self.assertEqual(rows, [(self.menu["Bravo"]["latte"], None),
                                (None, self.menu["Bravo"]["cookies"])])
        # Charlie has no latte: skipped, and the admin is told.
        self.assertFalse(DiscountType.objects.filter(branch=self.c).exists())
        self.assertContains(res, "Not added at Charlie")
        self.assertContains(res, "LATTE")

    def test_editing_a_copy_updates_the_others_and_unticking_removes(self):
        self.create()
        copy = DiscountType.objects.get(branch=self.b)
        url = reverse("backoffice:discount_detail", args=[copy.id]) + f"?branch={self.b.id}"
        body = {"name": "Set v2", "active": "on", "applies_to": "all", "kind": "percent",
                "value": "10", "sync_scope": "selected", "sync_branches": [str(self.a.id)]}
        self.client.post(url, body)
        src = DiscountType.objects.get(branch=self.a)
        self.assertEqual((src.name, src.kind, src.value), ("Set v2", "percent", Decimal("10")))
        self.assertFalse(src.conditions.exists())
        # Now only this branch: Alpha's copy goes.
        body.update(sync_scope="this", sync_branches=[])
        self.client.post(url, body)
        self.assertEqual(list(DiscountType.objects.values_list("branch__name", flat=True)),
                         ["Bravo"])

    def test_only_this_branch_is_the_default(self):
        self.create(sync_scope="this")
        self.assertEqual(DiscountType.objects.count(), 1)

    def test_fixed_combo_skips_a_branch_where_it_would_go_below_zero(self):
        cheap = self.menu["Bravo"]["latte"]
        cheap.price = Decimal("5.00")
        cheap.save()
        # Bravo: 5 + 95 = 100, discount 120 at Alpha is fine (195) but not there.
        res = self.create(value="120")
        self.assertFalse(DiscountType.objects.filter(branch=self.b).exists())
        self.assertContains(res, "Not added at Bravo")

    def test_deleting_removes_only_this_copy(self):
        self.create()
        src = DiscountType.objects.get(branch=self.a)
        res = self.client.post(reverse("backoffice:discount_delete", args=[src.id])
                               + f"?branch={self.a.id}", follow=True)
        self.assertTrue(DiscountType.objects.filter(branch=self.b).exists())
        self.assertContains(res, "still running at Bravo")

    def test_list_and_form_show_where_it_runs(self):
        self.create()
        page = self.client.get(reverse("backoffice:discount_list") + f"?branch={self.a.id}")
        self.assertContains(page, "Also at 1 other branch")
        src = DiscountType.objects.get(branch=self.a)
        form = self.client.get(reverse("backoffice:discount_detail", args=[src.id])
                               + f"?branch={self.a.id}")
        self.assertRegex(form.content.decode(),
                         r'name="sync_scope" value="selected"\s+checked')


class MinMaxAndOrTests(ComboFormTests):
    """Minimum order amount, maximum discount, and "any one of these"."""

    def test_saves_min_order_and_max_discount_and_sends_them_to_the_till(self):
        res = self.client.post(self.url, {
            "name": "Big bill", "active": "on", "applies_to": "all", "kind": "percent",
            "value": "20", "min_order_amount": "300", "max_discount": "100"})
        self.assertEqual(res.status_code, 302)
        dt = DiscountType.objects.get()
        self.assertEqual((dt.min_order_amount, dt.max_discount), (Decimal("300"), Decimal("100")))
        page = self.client.get(reverse("backoffice:discount_list") + f"?branch={self.branch.id}")
        self.assertContains(page, "max ฿100.00 per bill")
        self.assertContains(page, "bill ≥ ฿300.00")

        self.branch.discount_types_enabled = True
        self.branch.save()
        staff = Staff.objects.create(name="C", email="c@x.io", password_hash="x", role="cashier")
        session = BranchSession.objects.create(token="mm" * 18, branch=self.branch, staff=staff)
        row = self.client.get("/api/discount-types?v=2",
                              HTTP_AUTHORIZATION=f"Bearer {session.token}").json()["discount_types"][0]
        self.assertEqual((row["min_order_amount"], row["max_discount"], row["match"]),
                         (300.0, 100.0, "all"))

    def test_blank_means_none_and_bad_values_are_refused(self):
        self.assertEqual(self.client.post(self.url, {
            "name": "Plain", "active": "on", "applies_to": "all", "kind": "percent",
            "value": "5", "min_order_amount": "", "max_discount": ""}).status_code, 302)
        dt = DiscountType.objects.get()
        self.assertEqual((dt.min_order_amount, dt.max_discount), (None, None))
        for bad in ({"min_order_amount": "0"}, {"max_discount": "-5"}, {"min_order_amount": "abc"}):
            body = {"name": "Bad", "active": "on", "applies_to": "all", "kind": "percent", "value": "5"}
            body.update(bad)
            self.assertEqual(self.client.post(self.url, body).status_code, 200, bad)

    def test_free_item_has_no_cap(self):
        self.client.post(self.url, {
            "name": "Free", "active": "on", "applies_to": "all", "kind": "free",
            "free_product": str(self.latte.id), "free_qty": "1", "max_discount": "50"})
        self.assertIsNone(DiscountType.objects.get().max_discount)

    def test_any_combo_checks_the_cheapest_single_row(self):
        # Rows: Latte 100, any cookie 95. "Any": cheapest set is one row, 95.
        self.assertEqual(self.post(combo_match="any", value="95").status_code, 200)
        res = self.post(combo_match="any", value="94")
        self.assertEqual(res.status_code, 302)
        dt = DiscountType.objects.get()
        self.assertEqual(dt.combo_match, "any")
        page = self.client.get(reverse("backoffice:discount_list") + f"?branch={self.branch.id}")
        self.assertContains(page, "Latte or Cookies")
