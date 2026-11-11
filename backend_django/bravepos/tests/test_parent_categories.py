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


class ParentCategorySyncTests(TestCase):
    """"Sync to all branches" on the parent category form, and parents
    travelling with `catalog.copy_catalogue`."""

    @classmethod
    def setUpTestData(cls):
        cls.password = "correct-horse-battery"
        cls.admin = Staff(
            name="Admin", username="parentsync", email="parentsync@therollingpinn.com",
            role="admin", active=True, backoffice_access=True,
        )
        cls.admin.set_password(cls.password)
        cls.admin.save()
        Settings.objects.get_or_create(id="shop")
        cls.main = Branch.objects.create(name="Main")
        cls.silom = Branch.objects.create(name="Silom")
        cls.closed = Branch.objects.create(name="Closed", active=False)
        cls.admin.branches.add(cls.main, cls.silom)

    def setUp(self):
        self.assertTrue(self.client.login(username="parentsync", password=self.password))
        self.scope = f"?branch={self.main.id}"

    def test_new_parent_without_the_box_stays_here(self):
        self.client.post(reverse("backoffice:parent_category_new") + self.scope,
                         {"name": "Bakery", "order": "1"})
        self.assertFalse(ParentCategory.objects.filter(branch=self.silom).exists())

    def test_new_parent_syncs_to_other_active_branches(self):
        page = self.client.post(
            reverse("backoffice:parent_category_new") + self.scope,
            {"name": "Bakery", "name_th": "เบเกอรี่", "order": "3", "sync_all": "1"},
            follow=True)
        copy = ParentCategory.objects.get(branch=self.silom)
        self.assertEqual((copy.name, copy.name_th, copy.order), ("Bakery", "เบเกอรี่", 3))
        self.assertFalse(ParentCategory.objects.filter(branch=self.closed).exists())
        message = [str(m) for m in page.context["messages"]][0]
        self.assertIn("Also added to", message)
        self.assertIn("Silom", message)
        self.assertNotIn("Closed", message)

    def test_edit_renames_the_copy_and_files_matching_categories(self):
        here = ParentCategory.objects.create(branch=self.main, name="Bakery")
        there = ParentCategory.objects.create(branch=self.silom, name="bakery")
        drinks = ParentCategory.objects.create(branch=self.silom, name="Drinks")
        Category.objects.create(branch=self.main, name="Croissants", parent_category=here)
        Category.objects.create(branch=self.main, name="Cookies", parent_category=here)
        far_croissants = Category.objects.create(branch=self.silom, name="croissants",
                                                 parent_category=drinks)
        far_only = Category.objects.create(branch=self.silom, name="Tea",
                                           parent_category=there)

        self.client.post(
            reverse("backoffice:parent_category_detail", args=[here.id]) + self.scope,
            {"name": "Pastry", "order": "2", "sync_all": "1"})

        there.refresh_from_db()
        self.assertEqual((there.name, there.order), ("Pastry", 2))
        self.assertEqual(ParentCategory.objects.filter(branch=self.silom).count(), 2)
        far_croissants.refresh_from_db()
        self.assertEqual(far_croissants.parent_category, there)
        # Not created at Silom, and Silom's own child is left in the group.
        self.assertFalse(Category.objects.filter(branch=self.silom, name="Cookies").exists())
        far_only.refresh_from_db()
        self.assertEqual(far_only.parent_category, there)

    def test_copy_catalogue_brings_parents(self):
        from bravepos import catalog
        here = ParentCategory.objects.create(branch=self.main, name="Bakery", order=4)
        Category.objects.create(branch=self.main, name="Croissants", parent_category=here)
        Category.objects.create(branch=self.main, name="Cookies", parent_category=here)
        ParentCategory.objects.create(branch=self.main, name="Empty")
        mine = ParentCategory.objects.create(branch=self.silom, name="Mine")
        kept = Category.objects.create(branch=self.silom, name="Cookies", parent_category=mine)

        self.assertEqual(catalog.preview(self.main, self.silom)["parents_adding"], ["Bakery"])
        catalog.copy_catalogue(self.main, self.silom)

        copy = ParentCategory.objects.get(branch=self.silom, name="Bakery")
        self.assertEqual(copy.order, 4)
        self.assertEqual(Category.objects.get(branch=self.silom, name="Croissants")
                         .parent_category, copy)
        # The page only adds: Silom's own filing of Cookies is left alone.
        kept.refresh_from_db()
        self.assertEqual(kept.parent_category, mine)
        self.assertFalse(ParentCategory.objects.filter(branch=self.silom, name="Empty").exists())
