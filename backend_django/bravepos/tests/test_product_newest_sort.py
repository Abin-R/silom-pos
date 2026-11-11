"""The catalogue's "Newest" sort lists the most recently created product first.

It used to order by ``-id``, and a product's id is a random UUID, so the sort
was a shuffle: a product created a minute ago could land anywhere in the list.
It now orders by ``created_at``, which only exists from 2026-11-11 on — older
products have none and follow every dated one, alphabetically.

Run:
    python manage.py test bravepos.tests.test_product_newest_sort \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from bravepos.models import Branch, Product, Settings, Staff


class NewestSortTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.password = "correct-horse-battery"
        admin = Staff(
            name="Newest Sort Admin", username="newestsort",
            email="newestsort@therollingpinn.com", role="admin",
            active=True, backoffice_access=True,
        )
        admin.set_password(cls.password)
        admin.save()
        Settings.objects.get_or_create(id="shop")
        cls.branch = Branch.objects.create(name="Ari", code="ARI")

    def setUp(self):
        self.assertTrue(
            self.client.login(username="newestsort", password=self.password))

    def make(self, name, created_at):
        product = Product.objects.create(branch=self.branch, name=name, price=10)
        # auto_now_add ignores a value passed to create(), so set it after.
        Product.objects.filter(pk=product.pk).update(created_at=created_at)
        return product

    def listed(self):
        response = self.client.get(
            reverse("backoffice:product_list"),
            {"branch": str(self.branch.id), "sort": "newest"},
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        names = [p.name for p in Product.objects.filter(branch=self.branch)]
        return sorted(names, key=html.index)

    def test_a_new_product_gets_a_creation_time(self):
        before = timezone.now()
        product = Product.objects.create(branch=self.branch, name="Fresh", price=1)
        self.assertGreaterEqual(product.created_at, before)

    def test_newest_lists_the_latest_created_first(self):
        now = timezone.now()
        self.make("Aaa Oldest", now - timedelta(days=3))
        self.make("Zzz Newest", now)
        self.make("Mmm Middle", now - timedelta(days=1))
        self.assertEqual(self.listed(), ["Zzz Newest", "Mmm Middle", "Aaa Oldest"])

    def test_undated_products_follow_the_dated_ones_alphabetically(self):
        self.make("Bbb Undated", None)
        self.make("Aaa Undated", None)
        self.make("Zzz Dated", timezone.now())
        self.assertEqual(self.listed(), ["Zzz Dated", "Aaa Undated", "Bbb Undated"])
