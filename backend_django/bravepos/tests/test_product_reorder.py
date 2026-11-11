"""Drag-and-drop product order: the catalogue's "Till order" view.

One category's products at one branch are dragged into order; the till's Sale
screen and the self-order menu then list them in that same order, and the
self-order menu lists its categories in the till's category order.

Run:
    python manage.py test bravepos.tests.test_product_reorder \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from bravepos import selforder
from bravepos.tests.factories import open_shift
from bravepos.models import Branch, BranchSession, Category, Product, Settings, Staff


class ReorderTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.password = "correct-horse-battery"
        admin = Staff(name="Order Admin", username="orderadmin",
                      email="orderadmin@therollingpinn.com", role="admin",
                      active=True, backoffice_access=True)
        admin.set_password(cls.password)
        admin.save()
        Settings.objects.get_or_create(id="shop")
        Branch.objects.update(active=False)  # the seeded EmQuartier
        cls.bio = Branch.objects.create(name="BIO HOUSE", code="BIO")
        cls.other = Branch.objects.create(name="Central Pinklao", code="CPK")
        cls.cakes = Category.objects.create(branch=cls.bio, name="Cake", order=2)
        cls.cookies = Category.objects.create(branch=cls.bio, name="Cookie", order=1)

    def setUp(self):
        self.assertTrue(self.client.login(username="orderadmin", password=self.password))
        self.a = self.make("Apple cake", "1")
        self.b = self.make("Banana cake", "2")
        self.c = self.make("Carrot cake", "3")

    def make(self, name, barcode, category=None, branch=None, **kw):
        return Product.objects.create(branch=branch or self.bio, name=name, barcode=barcode,
                                      category=category or self.cakes, stock=10, **kw)

    def reorder(self, products, category="cakes"):
        """``category=None`` drags "All"."""
        if category == "cakes":
            category = self.cakes
        return self.client.post(reverse("backoffice:product_reorder"), {
            "branch": str(self.bio.id), "category": str(category.id) if category else "",
            "ids": [str(p.id) for p in products]})

    def till_names(self):
        session = BranchSession.objects.create(
            token="ord0" * 9, branch=self.bio,
            staff=Staff.objects.create(name="T", email="t@ord.local", password_hash="x"))
        rows = self.client.get("/api/products",
                               HTTP_AUTHORIZATION=f"Bearer {session.token}").json()
        return [r["name"] for r in rows if r["category_id"] == str(self.cakes.id)]

    def test_a_drop_saves_the_order(self):
        self.assertEqual(self.reorder([self.c, self.a, self.b]).status_code, 200)
        self.assertEqual(
            list(Product.objects.filter(category=self.cakes)
                 .order_by("sort_order").values_list("name", flat=True)),
            ["Carrot cake", "Apple cake", "Banana cake"])

    def test_the_till_and_the_self_order_menu_follow_it(self):
        self.reorder([self.c, self.a, self.b])
        expected = ["Carrot cake", "Apple cake", "Banana cake"]
        self.assertEqual(self.till_names(), expected)
        menu = [m["name"] for m in selforder.menu_for(self.bio)
                if m["category_id"] == str(self.cakes.id)]
        self.assertEqual(menu, expected)

    def test_dragging_all_sets_the_order_across_categories(self):
        z = self.make("Zebra cookie", "9", category=self.cookies)
        self.assertEqual(self.reorder([self.b, z, self.c, self.a], category=None).status_code, 200)
        expected = ["Banana cake", "Zebra cookie", "Carrot cake", "Apple cake"]
        self.assertEqual([m["name"] for m in selforder.menu_for(self.bio)], expected)
        # The category is that list without the cookie.
        self.assertEqual(self.till_names(), ["Banana cake", "Carrot cake", "Apple cake"])

    def test_a_category_drag_keeps_other_products_in_place(self):
        z = self.make("Zebra cookie", "9", category=self.cookies)
        self.reorder([self.a, z, self.b, self.c], category=None)
        self.reorder([self.c, self.b, self.a])
        self.assertEqual([m["name"] for m in selforder.menu_for(self.bio)],
                         ["Carrot cake", "Zebra cookie", "Banana cake", "Apple cake"])

    def test_self_order_pills_follow_the_till_category_order(self):
        self.make("Zebra cookie", "9", category=self.cookies)
        ranks = {m["category_name"]: m["category_order"] for m in selforder.menu_for(self.bio)}
        self.assertLess(ranks["Cookie"], ranks["Cake"])

    def test_a_list_that_is_out_of_date_is_refused(self):
        response = self.reorder([self.c, self.a])  # Banana missing
        self.assertEqual(response.status_code, 409)
        self.assertEqual(set(Product.objects.values_list("sort_order", flat=True)), {0})

    def test_a_product_from_another_branch_is_refused(self):
        stranger = self.make("Apple cake", "1", branch=self.other)
        self.assertEqual(self.reorder([self.c, self.a, stranger]).status_code, 409)

    def test_removed_products_are_not_part_of_the_list(self):
        self.make("Old cake", "4", active=False)
        self.assertEqual(self.reorder([self.b, self.a, self.c]).status_code, 200)

    def test_signed_out_cannot_reorder(self):
        self.client = self.client_class()  # signed out
        self.assertNotEqual(self.reorder([self.c, self.a, self.b]).status_code, 200)
        self.assertEqual(set(Product.objects.values_list("sort_order", flat=True)), {0})

    def test_the_catalogue_shows_handles_for_all_and_for_a_category(self):
        url = reverse("backoffice:product_list")
        self.make("Zebra cookie", "9", category=self.cookies)
        page = self.client.get(url, {"branch": str(self.bio.id), "sort": "till",
                                     "cat": str(self.cakes.id)})
        self.assertContains(page, 'class="drag-handle"', count=3)
        page = self.client.get(url, {"branch": str(self.bio.id), "sort": "till"})
        self.assertContains(page, 'class="drag-handle"', count=4)
        page = self.client.get(url, {"branch": str(self.bio.id), "sort": "till", "q": "cake"})
        self.assertNotContains(page, 'class="drag-handle"')

    def test_the_self_order_page_renders(self):
        self.bio.self_order_enabled = True
        self.bio.save()
        open_shift(self.bio)
        z = self.make("Zebra cookie", "9", category=self.cookies)
        self.reorder([self.b, z, self.c, self.a], category=None)
        page = self.client_class().get(f"/order/{self.bio.id}/")
        import os
        out = os.environ.get("MENU_HTML_OUT")
        if out:
            with open(out, "w") as fh:
                fh.write(page.content.decode())
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "category_order")
