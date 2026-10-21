"""Parent categories: the level above Category in the backoffice.

Run:
    python manage.py test bravepos.tests.test_parent_categories \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from bravepos.models import Branch, Category, ParentCategory, Settings, Staff


class ParentCategoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = "correct-horse-battery"
        cls.admin = Staff(
            name="Admin", username="parentcat", email="parentcat@therollingpinn.com",
            role="admin", active=True, backoffice_access=True,
        )
        cls.admin.set_password(cls.password)
        cls.admin.save()
        Settings.objects.get_or_create(id="shop")

        cls.branch = Branch.objects.create(name="Main")
        cls.other = Branch.objects.create(name="Other")
        cls.admin.branches.add(cls.branch, cls.other)

        cls.bakery = ParentCategory.objects.create(branch=cls.branch, name="Bakery", order=1)
        cls.foreign = ParentCategory.objects.create(branch=cls.other, name="Drinks")
        cls.croissants = Category.objects.create(
            branch=cls.branch, name="Croissants", parent_category=cls.bakery)

    def setUp(self):
        self.assertTrue(self.client.login(username="parentcat", password=self.password))
        self.scope = f"?branch={self.branch.id}"

    def test_pages_render(self):
        for url in [
            reverse("backoffice:parent_category_list"),
            reverse("backoffice:parent_category_new"),
            reverse("backoffice:parent_category_detail", args=[self.bakery.id]),
            reverse("backoffice:category_list"),
            reverse("backoffice:category_new"),
            reverse("backoffice:category_detail", args=[self.croissants.id]),
        ]:
            with self.subTest(url=url):
                page = self.client.get(url + self.scope)
                self.assertEqual(page.status_code, 200)

        listing = self.client.get(reverse("backoffice:category_list") + self.scope)
        self.assertContains(listing, "Bakery")
        detail = self.client.get(
            reverse("backoffice:parent_category_detail", args=[self.bakery.id]) + self.scope)
        self.assertContains(detail, "Croissants")

    def test_create_parent_lands_at_the_end_of_the_order(self):
        form = self.client.get(reverse("backoffice:parent_category_new") + self.scope)
        self.assertEqual(form.context["parent"].order, 2)

        self.client.post(reverse("backoffice:parent_category_new") + self.scope,
                         {"name": "Cakes", "name_th": "เค้ก", "order": "2"})
        cakes = ParentCategory.objects.get(name="Cakes")
        self.assertEqual(cakes.branch, self.branch)
        self.assertEqual(cakes.name_th, "เค้ก")

    def test_category_form_sets_and_clears_the_parent(self):
        url = reverse("backoffice:category_new") + self.scope
        self.client.post(url, {"name": "Cookies", "order": "1", "color": "#2563EB",
                               "parent_category": str(self.bakery.id), "active": "on"})
        self.assertEqual(Category.objects.get(name="Cookies").parent_category, self.bakery)

        url = reverse("backoffice:category_detail", args=[self.croissants.id]) + self.scope
        self.client.post(url, {"name": "Croissants", "order": "0", "color": "#2563EB",
                               "parent_category": "", "active": "on"})
        self.croissants.refresh_from_db()
        self.assertIsNone(self.croissants.parent_category)

    def test_category_cannot_take_another_branchs_parent(self):
        form = self.client.get(reverse("backoffice:category_new") + self.scope)
        self.assertNotIn(self.foreign, list(form.context["parent_categories"]))

        self.client.post(reverse("backoffice:category_new") + self.scope,
                         {"name": "Sneaky", "order": "0", "color": "#2563EB",
                          "parent_category": str(self.foreign.id), "active": "on"})
        self.assertIsNone(Category.objects.get(name="Sneaky").parent_category)

    def test_another_branchs_parent_is_not_reachable(self):
        for name in ["parent_category_detail", "parent_category_delete"]:
            with self.subTest(view=name):
                url = reverse(f"backoffice:{name}", args=[self.foreign.id]) + self.scope
                self.assertEqual(self.client.post(url, {"name": "x"}).status_code, 404)
        self.assertTrue(ParentCategory.objects.filter(id=self.foreign.id).exists())

    def test_deleting_a_parent_keeps_its_categories(self):
        self.client.post(
            reverse("backoffice:parent_category_delete", args=[self.bakery.id]) + self.scope)
        self.assertFalse(ParentCategory.objects.filter(id=self.bakery.id).exists())
        self.croissants.refresh_from_db()
        self.assertIsNone(self.croissants.parent_category)

    def test_catalogue_rail_groups_categories_under_parents(self):
        from bravepos.models import Product
        Product.objects.create(branch=self.branch, name="Butter", category=self.croissants)
        loose = Category.objects.create(branch=self.branch, name="Loose")
        Product.objects.create(branch=self.branch, name="Odd", category=loose)

        url = reverse("backoffice:product_list") + self.scope
        page = self.client.get(url)
        group = page.context["category_groups"][0]
        self.assertEqual(group["parent"], self.bakery)
        self.assertEqual(group["count"], 1)
        self.assertEqual([c["obj"] for c in group["children"]], [self.croissants])
        self.assertEqual([c["obj"] for c in page.context["ungrouped_categories"]], [loose])
        self.assertContains(page, "Manage parent categories")

        page = self.client.get(url + f"&pcat={self.bakery.id}")
        self.assertEqual([p.name for p in page.context["products"]], ["Butter"])

        for junk in ["not-a-uuid", str(self.foreign.id)]:
            with self.subTest(pcat=junk):
                page = self.client.get(url + f"&pcat={junk}")
                self.assertEqual(page.status_code, 200)
                self.assertEqual(len(page.context["products"]), 2)
