"""The category form's "Sync to all branches", and both category forms'
boxes being ticked by default.

Run:
    python manage.py test bravepos.tests.test_category_sync \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from bravepos.models import Branch, Category, ParentCategory, Product, Settings, Staff


class CategorySyncTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.password = "correct-horse-battery"
        admin = Staff(
            name="Admin", username="catsync", email="catsync@therollingpinn.com",
            role="admin", active=True, backoffice_access=True,
        )
        admin.set_password(cls.password)
        admin.save()
        Settings.objects.get_or_create(id="shop")
        cls.main = Branch.objects.create(name="Main")
        cls.silom = Branch.objects.create(name="Silom")
        cls.closed = Branch.objects.create(name="Closed", active=False)
        admin.branches.add(cls.main, cls.silom)
        cls.cake_group = ParentCategory.objects.create(branch=cls.main, name="Cake")

    def setUp(self):
        self.assertTrue(self.client.login(username="catsync", password=self.password))

    def post_new(self, **data):
        return self.client.post(
            reverse("backoffice:category_new") + f"?branch={self.main.id}",
            {"order": "1", "color": "#ff0000", "active": "on", **data})

    def post_edit(self, category, **data):
        return self.client.post(
            reverse("backoffice:category_detail", args=[category.id])
            + f"?branch={self.main.id}",
            {"order": "1", "color": "#ff0000", "active": "on", **data})

    def test_the_box_is_ticked_on_both_forms(self):
        for url in (reverse("backoffice:category_new"),
                    reverse("backoffice:parent_category_new")):
            response = self.client.get(url + f"?branch={self.main.id}")
            self.assertContains(
                response, 'name="sync_all" value="1" checked', html=False)

    def test_a_new_category_reaches_other_active_branches_with_its_parent(self):
        self.post_new(name="Mini Cake", name_th="เค้กมินิ",
                      parent_category=str(self.cake_group.id), sync_all="1")
        copy = Category.objects.get(branch=self.silom, name="Mini Cake")
        self.assertEqual(copy.name_th, "เค้กมินิ")
        self.assertEqual(copy.color, "#ff0000")
        self.assertEqual(copy.parent_category.name, "Cake")
        self.assertEqual(copy.parent_category.branch, self.silom)
        self.assertFalse(Category.objects.filter(branch=self.closed).exists())

    def test_unticked_stays_at_this_branch(self):
        self.post_new(name="Mini Cake")
        self.assertFalse(Category.objects.filter(branch=self.silom).exists())

    def test_a_rename_renames_the_copy_instead_of_adding_one(self):
        here = Category.objects.create(branch=self.main, name="Cookies")
        there = Category.objects.create(branch=self.silom, name="Cookies", order=7)
        product = Product.objects.create(branch=self.silom, name="Choc", price=1,
                                         category=there)
        self.post_edit(here, name="Large Cookie", order="3", sync_all="1")
        there.refresh_from_db()
        self.assertEqual(there.name, "Large Cookie")
        self.assertEqual(there.order, 3)
        self.assertEqual(Category.objects.filter(branch=self.silom).count(), 1)
        product.refresh_from_db()
        self.assertEqual(product.category_id, there.id)

    def test_ungrouping_here_ungroups_the_copy(self):
        here = Category.objects.create(branch=self.main, name="Brownie",
                                       parent_category=self.cake_group)
        silom_group = ParentCategory.objects.create(branch=self.silom, name="Cake")
        there = Category.objects.create(branch=self.silom, name="Brownie",
                                        parent_category=silom_group)
        self.post_edit(here, name="Brownie", sync_all="1")
        there.refresh_from_db()
        self.assertIsNone(there.parent_category_id)

    def test_turning_it_off_turns_the_copy_off(self):
        here = Category.objects.create(branch=self.main, name="Candle")
        there = Category.objects.create(branch=self.silom, name="Candle")
        self.client.post(
            reverse("backoffice:category_detail", args=[here.id])
            + f"?branch={self.main.id}",
            {"name": "Candle", "order": "0", "color": "#000000", "sync_all": "1"})
        there.refresh_from_db()
        self.assertFalse(there.active)

    def test_the_message_names_the_branches(self):
        response = self.post_new(name="Mochi", sync_all="1")
        msgs = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any("added to" in m and "Silom" in m and "Closed" not in m for m in msgs), msgs)
