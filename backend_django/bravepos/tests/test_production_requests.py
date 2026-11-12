"""Production requests: stock supervisor asks, kitchen reads, packer packs.

Run:
    python manage.py test bravepos.tests.test_production_requests \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

import datetime

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from bravepos import production
from bravepos.models import (
    Branch, Category, ParentCategory, Product, ProductionRequest,
    ProductionRequestItem, Settings, StockDocument, StockMovement,
)
from bravepos.tests.test_user_management_and_audit import make_user, sign_in

TZ = datetime.timezone(datetime.timedelta(hours=7))


def aware(y, m, d, hh, mm=0):
    return datetime.datetime(y, m, d, hh, mm, tzinfo=TZ)


def today():
    return timezone.localdate()


def open_day():
    """A delivery date whose requests are always still open."""
    return today() + datetime.timedelta(days=3)


def locked_day():
    """A delivery date whose requests have always already locked."""
    return today() - datetime.timedelta(days=1)


class Catalogue:
    """Two branches with overlapping menus, the way synced branches look."""

    def build(self):
        self.emq = Branch.objects.create(name="Alpha Mall")
        self.icon = Branch.objects.create(name="Beta Mall")
        self.products = {}
        for branch in (self.emq, self.icon):
            cookie = ParentCategory.objects.create(branch=branch, name="Cookie")
            large = Category.objects.create(branch=branch, name="Large Cookie", order=1,
                                            parent_category=cookie)
            mousse = Category.objects.create(branch=branch, name="Mousse Pop", order=2)
            for name, cat, order in (("Red Velvet Cookie", large, 1), ("Oreo Confetti Cookie", large, 2),
                                     ("Strawberry Mousse Pop", mousse, 1)):
                self.products[(branch.name, name)] = Product.objects.create(
                    branch=branch, name=name, category=cat, sort_order=order)

    def pid(self, branch, name):
        return str(self.products[(branch.name, name)].id)


# ── The cut-off ─────────────────────────────────────────────────────────────
class CutoffTests(TestCase):
    def test_locks_the_day_before_at_the_cutoff(self):
        locks = production.locks_at(datetime.date(2026, 11, 13), datetime.time(18, 0))
        self.assertEqual(locks, aware(2026, 11, 12, 18))

    def test_open_until_the_cutoff_and_not_after(self):
        cutoff = datetime.time(18, 0)
        day = datetime.date(2026, 11, 13)
        self.assertTrue(production.is_open(day, aware(2026, 11, 12, 17, 59), cutoff))
        self.assertFalse(production.is_open(day, aware(2026, 11, 12, 18, 0), cutoff))

    def test_earliest_open_date_skips_tomorrow_after_the_cutoff(self):
        cutoff = datetime.time(18, 0)
        self.assertEqual(production.earliest_open_date(aware(2026, 11, 10, 9), cutoff),
                         datetime.date(2026, 11, 11))
        self.assertEqual(production.earliest_open_date(aware(2026, 11, 10, 19), cutoff),
                         datetime.date(2026, 11, 12))

    def test_kitchen_default_is_the_soonest_locked_date(self):
        cutoff = datetime.time(18, 0)
        self.assertEqual(production.latest_locked_date(aware(2026, 11, 10, 9), cutoff),
                         datetime.date(2026, 11, 10))
        self.assertEqual(production.latest_locked_date(aware(2026, 11, 10, 19), cutoff),
                         datetime.date(2026, 11, 11))

    def test_cutoff_comes_from_shop_settings(self):
        Settings.objects.create(id="shop", production_cutoff=datetime.time(15, 30))
        self.assertEqual(production.cutoff_time(), datetime.time(15, 30))


# ── Saving a request ────────────────────────────────────────────────────────
class SaveRequestTests(Catalogue, TestCase):
    def setUp(self):
        self.build()
        self.sup = make_user(username="sup", email="sup@example.com", role="stock_supervisor")

    def save(self, wanted, day=None):
        return production.save_request(self.emq, day or open_day(), wanted, self.sup)

    def test_creates_one_request_with_snapshots(self):
        req = self.save({self.pid(self.emq, "Red Velvet Cookie"): 12,
                         self.pid(self.emq, "Oreo Confetti Cookie"): 0})
        item = req.items.get()
        self.assertEqual((item.product_name, item.requested_qty), ("Red Velvet Cookie", 12))
        self.assertEqual(item.category_name, "Large Cookie")
        self.assertEqual(item.parent_category_name, "Cookie")
        self.assertEqual(req.created_by_name, self.sup.name)

    def test_saving_again_edits_the_same_request(self):
        self.save({self.pid(self.emq, "Red Velvet Cookie"): 12})
        self.save({self.pid(self.emq, "Red Velvet Cookie"): 5,
                   self.pid(self.emq, "Strawberry Mousse Pop"): 3})
        self.assertEqual(ProductionRequest.objects.count(), 1)
        self.assertEqual(
            dict(ProductionRequestItem.objects.values_list("product_name", "requested_qty")),
            {"Red Velvet Cookie": 5, "Strawberry Mousse Pop": 3})

    def test_all_zero_removes_the_request(self):
        self.save({self.pid(self.emq, "Red Velvet Cookie"): 12})
        self.assertIsNone(self.save({self.pid(self.emq, "Red Velvet Cookie"): 0}))
        self.assertFalse(ProductionRequest.objects.exists())

    def test_zeroing_a_packed_line_keeps_the_packed_number(self):
        req = self.save({self.pid(self.emq, "Red Velvet Cookie"): 12,
                         self.pid(self.emq, "Oreo Confetti Cookie"): 4})
        item = req.items.get(product_name="Red Velvet Cookie")
        production.save_packed(open_day(), {str(item.id): 10})
        self.save({self.pid(self.emq, "Red Velvet Cookie"): 0})
        item.refresh_from_db()
        self.assertEqual((item.requested_qty, item.packed_qty), (0, 10))

    def test_locked_date_refused(self):
        with self.assertRaises(production.RequestLocked):
            self.save({self.pid(self.emq, "Red Velvet Cookie"): 12}, day=locked_day())
        self.assertFalse(ProductionRequest.objects.exists())

    def test_another_branchs_product_is_ignored(self):
        self.assertIsNone(self.save({self.pid(self.icon, "Red Velvet Cookie"): 12}))

    def test_duplicate_names_listed_once(self):
        Product.objects.create(branch=self.emq, name="red velvet cookie", sort_order=9)
        names = [p.name.lower() for p in production.requestable_products(self.emq)]
        self.assertEqual(names.count("red velvet cookie"), 1)

    def test_inactive_products_not_offered(self):
        Product.objects.create(branch=self.emq, name="Old Cookie", active=False)
        names = {p.name for p in production.requestable_products(self.emq)}
        self.assertNotIn("Old Cookie", names)


# ── Kitchen and packer views of the data ────────────────────────────────────
class TotalsAndGridTests(Catalogue, TestCase):
    def setUp(self):
        self.build()
        self.day = open_day()
        production.save_request(self.emq, self.day, {
            self.pid(self.emq, "Red Velvet Cookie"): 12,
            self.pid(self.emq, "Strawberry Mousse Pop"): 8})
        production.save_request(self.icon, self.day, {
            self.pid(self.icon, "Red Velvet Cookie"): 4,
            self.pid(self.icon, "Oreo Confetti Cookie"): 3})

    def test_kitchen_adds_branches_up_by_product(self):
        data = production.kitchen_totals(self.day)
        lines = {r["name"]: (r["total"], r["branches"]) for c in data["categories"] for r in c["rows"]}
        self.assertEqual(lines["Red Velvet Cookie"], (16, 2))
        self.assertEqual(lines["Oreo Confetti Cookie"], (3, 1))
        self.assertEqual(data["total"], 27)
        self.assertEqual([c["name"] for c in data["categories"]], ["Large Cookie", "Mousse Pop"])

    def test_kitchen_group_filter(self):
        cookie = production.kitchen_totals(self.day, "Cookie")
        self.assertEqual(cookie["total"], 19)
        ungrouped = production.kitchen_totals(self.day, "")
        self.assertEqual(ungrouped["total"], 8)
        self.assertEqual(production.kitchen_groups(self.day), ["Cookie", ""])

    def test_packing_grid_is_branches_by_products(self):
        table = production.packing_tables(self.day, "Large Cookie")[0]
        self.assertEqual(table["columns"], ["Red Velvet Cookie", "Oreo Confetti Cookie"])
        self.assertEqual([r["branch"].name for r in table["rows"]], ["Alpha Mall", "Beta Mall"])
        emq, icon = table["rows"]
        self.assertIsNone(emq["cells"][1])  # Alpha Mall asked for no Oreo
        self.assertEqual(icon["cells"][1].requested_qty, 3)
        self.assertEqual(table["col_requested"], [16, 3])

    def test_save_packed_only_touches_that_date(self):
        item = ProductionRequestItem.objects.get(request__branch=self.icon, product_name="Oreo Confetti Cookie")
        self.assertEqual(production.save_packed(self.day + datetime.timedelta(days=1), {str(item.id): 2}), 0)
        self.assertEqual(production.save_packed(self.day, {str(item.id): 2}), 1)
        item.refresh_from_db()
        self.assertEqual(item.packed_qty, 2)
        self.assertEqual(production.save_packed(self.day, {str(item.id): None}), 1)
        item.refresh_from_db()
        self.assertIsNone(item.packed_qty)


# ── Matching by barcode (the Shopster id) ───────────────────────────────────
class BarcodeMatchingTests(TestCase):
    """The same Shopster product, named differently at different branches."""

    def setUp(self):
        self.day = open_day()
        self.central = Branch.objects.create(name="BIO HOUSE")
        self.alpha = Branch.objects.create(name="Alpha Mall")
        self.beta = Branch.objects.create(name="Beta Mall")
        self.cat = {}
        for b in (self.central, self.alpha, self.beta):
            self.cat[b.pk] = Category.objects.create(branch=b, name="Large Cookie", order=1)

    def product(self, branch, name, barcode="", category=None):
        return Product.objects.create(branch=branch, name=name, barcode=barcode,
                                      category=category or self.cat[branch.pk])

    def ask(self, branch, product, qty):
        production.save_request(branch, self.day, {str(product.id): qty})

    def test_different_names_one_barcode_are_one_product(self):
        self.ask(self.central, self.product(self.central, "Red Velvet Cookie", "30879"), 10)
        self.ask(self.alpha, self.product(self.alpha, "Red velvet cookies", "30879"), 4)
        self.ask(self.beta, self.product(self.beta, "RV Cookie", "30879"), 2)

        rows = [r for c in production.kitchen_totals(self.day)["categories"] for r in c["rows"]]
        self.assertEqual([(r["name"], r["total"], r["branches"]) for r in rows],
                         [("Red Velvet Cookie", 16, 3)])
        table = production.packing_tables(self.day)[0]
        self.assertEqual(table["columns"], ["Red Velvet Cookie"])
        self.assertEqual(table["col_requested"], [16])

    def test_same_name_different_barcodes_stay_apart(self):
        self.ask(self.alpha, self.product(self.alpha, "Gift Card", "1001"), 1)
        self.ask(self.beta, self.product(self.beta, "Gift Card", "1002"), 1)
        rows = [r for c in production.kitchen_totals(self.day)["categories"] for r in c["rows"]]
        self.assertEqual(len(rows), 2)

    def test_without_bio_house_the_common_name_wins(self):
        gamma = Branch.objects.create(name="Gamma Mall")
        gamma_cat = Category.objects.create(branch=gamma, name="Large Cookie", order=1)
        self.ask(self.alpha, self.product(self.alpha, "Mama OG Cookie", "30893"), 1)
        self.ask(self.beta, self.product(self.beta, "Mama OG Cookie", "30893"), 1)
        self.ask(gamma, self.product(gamma, "Mama OG Cooki", "30893", gamma_cat), 1)
        self.assertEqual(production.packing_tables(self.day)[0]["columns"], ["Mama OG Cookie"])

    def test_one_product_in_one_category_whatever_branches_file_it_under(self):
        other = Category.objects.create(branch=self.alpha, name="Cookies", order=5)
        self.ask(self.central, self.product(self.central, "Oreo Confetti Cookie", "38021"), 3)
        self.ask(self.alpha, self.product(self.alpha, "Oreo Confetti Cookie", "38021", other), 2)
        tables = production.packing_tables(self.day)
        self.assertEqual([t["name"] for t in tables], ["Large Cookie"])
        self.assertEqual(len(tables[0]["rows"]), 2)
        self.assertEqual(production.packing_categories(self.day), ["Large Cookie"])

    def test_no_barcode_falls_back_to_the_name(self):
        self.ask(self.alpha, self.product(self.alpha, "เทียนพาสเทล"), 5)
        self.ask(self.beta, self.product(self.beta, "เทียนพาสเทล "), 2)
        rows = [r for c in production.kitchen_totals(self.day)["categories"] for r in c["rows"]]
        self.assertEqual([(r["total"], r["branches"]) for r in rows], [(7, 2)])

    def test_renamed_product_keeps_its_line(self):
        p = self.product(self.alpha, "Red Velvet Cookie", "30879")
        self.ask(self.alpha, p, 4)
        p.name = "Red Velvet Cookie NEW"
        p.save()
        self.ask(self.alpha, p, 6)
        item = ProductionRequestItem.objects.get()
        self.assertEqual((item.product_name, item.requested_qty, item.match_key),
                         ("Red Velvet Cookie NEW", 6, "b:30879"))


# ── The pages, per role ─────────────────────────────────────────────────────
class StockSupervisorPageTests(Catalogue, TestCase):
    def setUp(self):
        self.build()
        self.sup = make_user(username="sup", email="sup@example.com", role="stock_supervisor")
        sign_in(self.client, self.sup)

    def url(self, branch=None, day=None):
        return (reverse("backoffice:production_request")
                + f"?branch={(branch or self.emq).id}&date={(day or open_day()).isoformat()}")

    def test_home_and_dashboard_land_on_request(self):
        for name in ("home", "dashboard"):
            with self.subTest(name=name):
                self.assertRedirects(self.client.get(reverse(f"backoffice:{name}")),
                                     reverse("backoffice:production_request"))

    def test_form_lists_products_grouped(self):
        response = self.client.get(self.url())
        self.assertContains(response, "Red Velvet Cookie")
        self.assertContains(response, "Large Cookie")
        self.assertContains(response, "Stock supervisor")

    def test_saves_and_shows_on_history(self):
        response = self.client.post(self.url(), {
            f"qty_{self.pid(self.emq, 'Red Velvet Cookie')}": "12",
            f"qty_{self.pid(self.emq, 'Oreo Confetti Cookie')}": ""})
        self.assertRedirects(response, self.url())
        self.assertEqual(ProductionRequestItem.objects.get().requested_qty, 12)
        history = self.client.get(reverse("backoffice:production_requests"))
        self.assertContains(history, "Red Velvet Cookie ×12")
        self.assertContains(history, "Open")

    def test_bad_number_saves_nothing(self):
        response = self.client.post(self.url(), {f"qty_{self.pid(self.emq, 'Red Velvet Cookie')}": "1.5"})
        self.assertContains(response, "whole numbers")
        self.assertFalse(ProductionRequest.objects.exists())

    def test_locked_date_is_read_only(self):
        response = self.client.post(self.url(day=locked_day()), {
            f"qty_{self.pid(self.emq, 'Red Velvet Cookie')}": "12"})
        self.assertContains(response, "Nothing was saved")
        self.assertFalse(ProductionRequest.objects.exists())
        self.assertNotContains(self.client.get(self.url(day=locked_day())), 'name="qty_')

    def test_never_sees_packed_numbers(self):
        production.save_request(self.emq, open_day(), {self.pid(self.emq, "Red Velvet Cookie"): 12})
        item = ProductionRequestItem.objects.get()
        production.save_packed(open_day(), {str(item.id): 98765})
        # Standalone only: tokens and ids are random and can contain the digits.
        for url in (self.url(), reverse("backoffice:production_requests")):
            body = self.client.get(url).content.decode()
            self.assertNotRegex(body, r"(?<![\w-])98,?765(?![\w-])")

    def test_any_branch_whatever_is_ticked(self):
        # Branches ticked on the Users page don't limit a supervisor.
        self.sup.branches.add(self.icon)
        for branch, qty in ((self.emq, 9), (self.icon, 5)):
            response = self.client.post(self.url(branch=branch), {
                f"qty_{self.pid(branch, 'Red Velvet Cookie')}": str(qty)})
            self.assertEqual(response.status_code, 302)
        self.assertEqual(
            dict(ProductionRequestItem.objects.values_list("request__branch__name", "requested_qty")),
            {"Alpha Mall": 9, "Beta Mall": 5})
        history = self.client.get(reverse("backoffice:production_requests"))
        self.assertContains(history, "Alpha Mall")
        self.assertContains(history, "Beta Mall")

    def test_everything_else_refused(self):
        for name in ("production_kitchen", "production_packing", "transactions", "report_daily",
                     "inventory", "check_stock", "stock_in_new", "product_list", "shop_settings",
                     "user_list"):
            with self.subTest(name=name):
                self.assertContains(self.client.get(reverse(f"backoffice:{name}")),
                                    "Request production only", status_code=403)

    def test_rail_shows_only_production_request(self):
        response = self.client.get(self.url())
        self.assertContains(response, reverse("backoffice:production_requests") + '"')
        for name in ("dashboard", "production_kitchen", "production_packing", "shop_settings"):
            self.assertNotContains(response, reverse(f"backoffice:{name}") + '"')

    def test_can_sign_out(self):
        self.assertEqual(self.client.post(reverse("backoffice:logout")).status_code, 302)


class KitchenViewerPageTests(Catalogue, TestCase):
    def setUp(self):
        self.build()
        production.save_request(self.emq, open_day(), {self.pid(self.emq, "Red Velvet Cookie"): 12})
        production.save_request(self.icon, open_day(), {self.pid(self.icon, "Red Velvet Cookie"): 4})
        sign_in(self.client, make_user(username="kitchen", email="k@example.com", role="viewer"))

    def test_totals_page(self):
        response = self.client.get(reverse("backoffice:production_kitchen") + f"?date={open_day().isoformat()}")
        self.assertContains(response, "Red Velvet Cookie")
        self.assertContains(response, ">16<")
        self.assertContains(response, "Not final")

    def test_export(self):
        response = self.client.get(reverse("backoffice:production_kitchen_export") + f"?date={open_day().isoformat()}")
        body = response.content.decode("utf-8-sig")
        self.assertIn("Large Cookie,Red Velvet Cookie,2,16", body)

    def test_read_only(self):
        for name in ("production_request", "production_packing"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(f"backoffice:{name}")).status_code, 403)
        self.assertEqual(self.client.post(reverse("backoffice:production_kitchen")).status_code, 403)


class PackerPageTests(Catalogue, TestCase):
    def setUp(self):
        self.build()
        self.day = open_day()
        production.save_request(self.emq, self.day, {self.pid(self.emq, "Red Velvet Cookie"): 12})
        production.save_request(self.icon, self.day, {self.pid(self.icon, "Red Velvet Cookie"): 4})
        self.packer = make_user(username="packer", email="p@example.com", role="packer")
        sign_in(self.client, self.packer)
        self.url = reverse("backoffice:production_packing") + f"?date={self.day.isoformat()}"

    def test_grid_shows_requested_and_boxes(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Alpha Mall")
        self.assertContains(response, "Total requested")
        self.assertContains(response, 'name="packed_', count=2)

    def test_saves_packed_and_moves_no_stock(self):
        item = ProductionRequestItem.objects.get(request__branch=self.emq)
        response = self.client.post(self.url, {f"packed_{item.id}": "11"})
        self.assertRedirects(response, self.url)
        item.refresh_from_db()
        self.assertEqual((item.packed_qty, item.packed_by_name), (11, self.packer.name))
        self.assertFalse(StockDocument.objects.exists())
        self.assertFalse(StockMovement.objects.exists())

    def test_packed_history_and_export(self):
        item = ProductionRequestItem.objects.get(request__branch=self.emq)
        production.save_packed(self.day, {str(item.id): 11}, self.packer)
        history = self.client.get(reverse("backoffice:production_packing") + "?view=packed")
        self.assertContains(history, ">11<")
        self.assertNotContains(history, 'name="packed_')
        self.assertNotContains(history, "Beta Mall")  # nothing packed for it yet
        export = self.client.get(reverse("backoffice:production_packing_export") + f"?date={self.day.isoformat()}")
        self.assertIn("Large Cookie,Alpha Mall,Red Velvet Cookie,12,11", export.content.decode("utf-8-sig"))

    def test_bad_number_saves_nothing(self):
        item = ProductionRequestItem.objects.get(request__branch=self.emq)
        response = self.client.post(self.url, {f"packed_{item.id}": "-1"})
        self.assertContains(response, "whole numbers")
        item.refresh_from_db()
        self.assertIsNone(item.packed_qty)

    def test_cannot_open_request_or_kitchen(self):
        for name in ("production_request", "production_requests", "production_kitchen"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(f"backoffice:{name}")).status_code, 403)


class AdminCannotEditAfterCutoffTests(Catalogue, TestCase):
    def test_admin_refused_too(self):
        self.build()
        sign_in(self.client, make_user())
        url = (reverse("backoffice:production_request")
               + f"?branch={self.emq.id}&date={locked_day().isoformat()}")
        response = self.client.post(url, {f"qty_{self.pid(self.emq, 'Red Velvet Cookie')}": "12"})
        self.assertContains(response, "Nothing was saved")
        self.assertFalse(ProductionRequest.objects.exists())

    def test_admin_sees_all_four_pages(self):
        self.build()
        sign_in(self.client, make_user())
        for name in ("production_request", "production_requests", "production_kitchen", "production_packing"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(f"backoffice:{name}")).status_code, 200)


class CutoffSettingTests(TestCase):
    def post(self, user, value):
        sign_in(self.client, user)
        Branch.objects.get_or_create(name="Silom")
        self.client.post(reverse("backoffice:shop_settings"), {
            "shop_name": "Rolling Pinn", "production_cutoff": value})
        return Settings.objects.get(id="shop").production_cutoff

    def test_admin_changes_it(self):
        self.assertEqual(self.post(make_user(), "16:30"), datetime.time(16, 30))

    def test_manager_cannot(self):
        manager = make_user(username="mgr", email="mgr@example.com", role="cashier")
        self.assertEqual(self.post(manager, "16:30"), datetime.time(23, 30))


class StockSupervisorNeverOpensATillTests(TestCase):
    def test_pin_login_refused_and_not_on_picker(self):
        branch = Branch.objects.create(name="Silom")
        sup = make_user(username="sup", email="sup@example.com", role="stock_supervisor")
        sup.set_pin("4321")
        sup.save()
        sup.branches.add(branch)
        api = APIClient()
        response = api.post("/api/auth/pin-login", {
            "branch_id": str(branch.id), "staff_id": str(sup.id), "pin": "4321",
        }, format="json")
        self.assertEqual(response.status_code, 403)
        users = api.get("/api/auth/branch-users", {"branch_id": str(branch.id)}).json()["users"]
        self.assertNotIn(str(sup.id), [u["id"] for u in users])

    def test_created_from_users_page(self):
        sign_in(self.client, make_user())
        self.client.post(reverse("backoffice:user_new"), {
            "name": "Best", "username": "best", "role": "stock_supervisor",
            "active": "on", "password": "long-enough-pass",
        })
        from bravepos.models import Staff
        self.assertEqual(Staff.objects.get(username="best").role, "stock_supervisor")
