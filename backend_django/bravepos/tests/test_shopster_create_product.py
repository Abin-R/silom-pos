"""Shopster creates BIO HOUSE products with a bare POST — no session, no branch.

Run:
    python manage.py test bravepos.tests.test_shopster_create_product \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

import base64
import io
from datetime import date
from unittest import mock

from django.test import TestCase
from PIL import Image

from bravepos.models import Category, Product

from .factories import make_branch

URL = "/api/shopster/create-product/"


def _png_b64(size=(40, 30)) -> str:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 120, 40)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


class ShopsterCreateProductTests(TestCase):

    def setUp(self):
        self.branch = make_branch(name="BIO HOUSE")
        self.other = make_branch(name="Shopster Other")
        self.cat = Category.objects.create(branch=self.branch, name="Bakery")

    def _post(self, body):
        return self.client.post(URL, body, content_type="application/json")

    def test_creates_product_at_bio_house_without_auth(self):
        res = self._post({
            "name": "Croissant", "price": 85, "cost": 30, "stock": 12, "sku": "CR-01",
        })
        self.assertEqual(res.status_code, 201, res.content)
        p = Product.objects.get(id=res.json()["id"])
        self.assertEqual((p.branch, p.name, p.stock, p.sku),
                         (self.branch, "Croissant", 12, "CR-01"))

    def test_branch_in_body_is_ignored(self):
        res = self._post({"name": "Bagel", "branch": "Shopster Other",
                          "branch_id": str(self.other.id)})
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(Product.objects.get(name="Bagel").branch, self.branch)

    def test_missing_bio_house_is_an_error_not_a_stray_product(self):
        self.branch.name = "Renamed"
        self.branch.save(update_fields=["name"])
        res = self._post({"name": "Orphan"})
        self.assertEqual(res.status_code, 503)
        self.assertFalse(Product.objects.filter(name="Orphan").exists())

    def test_category_by_name(self):
        res = self._post({"category": "bakery", "name": "Bagel", "price": 60})
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(Product.objects.get(name="Bagel").category, self.cat)

    def test_unknown_category_name_is_rejected_not_created(self):
        res = self._post({"category": "Drinks", "name": "Latte"})
        self.assertEqual(res.status_code, 400)
        self.assertFalse(Category.objects.filter(name="Drinks").exists())

    def test_category_from_another_branch_is_rejected(self):
        foreign = Category.objects.create(branch=self.other, name="Bakery")
        res = self._post({"category_id": str(foreign.id), "name": "Scone"})
        self.assertEqual(res.status_code, 400)

    def test_name_is_required(self):
        self.assertEqual(self._post({"price": 10}).status_code, 400)

    def test_caller_cannot_choose_the_id(self):
        existing = Product.objects.create(branch=self.other, name="Keep me")
        res = self._post({"name": "Intruder", "id": str(existing.id)})
        self.assertEqual(res.status_code, 201, res.content)
        self.assertNotEqual(res.json()["id"], str(existing.id))
        existing.refresh_from_db()
        self.assertEqual((existing.name, existing.branch), ("Keep me", self.other))

    # ── image ────────────────────────────────────────────────────────────
    def test_image_as_hosted_url(self):
        url = "https://cdn.example.com/croissant.jpg"
        res = self._post({"name": "Croissant", "image": url})
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(Product.objects.get(name="Croissant").image_url, url)

    def test_image_as_bare_base64(self):
        res = self._post({"name": "Croissant", "image": _png_b64()})
        self.assertEqual(res.status_code, 201, res.content)
        stored = Product.objects.get(name="Croissant").image_url
        self.assertTrue(stored.startswith("data:image/png;base64,"), stored[:40])

    def test_image_as_data_uri_is_downscaled(self):
        uri = "data:image/png;base64," + _png_b64(size=(1600, 1200))
        res = self._post({"name": "Croissant", "image": uri})
        self.assertEqual(res.status_code, 201, res.content)
        stored = Product.objects.get(name="Croissant").image_url
        self.assertTrue(stored.startswith("data:image/jpeg;base64,"))
        raw = base64.b64decode(stored.split(",", 1)[1])
        self.assertLessEqual(max(Image.open(io.BytesIO(raw)).size), 512)

    def test_junk_image_is_rejected(self):
        res = self._post({"name": "Croissant", "image": "not an image"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("image", res.json())
        self.assertFalse(Product.objects.filter(name="Croissant").exists())

    # ── sale windows ─────────────────────────────────────────────────────
    def test_sale_windows_are_stored(self):
        with mock.patch("bravepos.sale_windows.today", return_value=date(2026, 10, 15)):
            res = self._post({"name": "Mooncake", "sale_windows": [
                {"launch_date": "2026-10-01", "discontinue_date": "2026-10-31"},
                {"launch_date": "2026-12-01", "discontinue_date": None},
            ]})
        self.assertEqual(res.status_code, 201, res.content)
        p = Product.objects.get(name="Mooncake")
        self.assertEqual(
            [(w.launch_date, w.discontinue_date) for w in p.sale_windows.all()],
            [(date(2026, 10, 1), date(2026, 10, 31)), (date(2026, 12, 1), None)])
        self.assertTrue(p.active)
        self.assertEqual(len(res.json()["sale_windows"]), 2)

    def test_future_launch_is_created_off_sale_until_then(self):
        with mock.patch("bravepos.sale_windows.today", return_value=date(2026, 10, 15)):
            res = self._post({"name": "Stollen", "sale_windows": [
                {"launch_date": "2026-12-01", "discontinue_date": "2026-12-31"},
            ]})
        self.assertEqual(res.status_code, 201, res.content)
        self.assertFalse(res.json()["active"])
        p = Product.objects.get(name="Stollen")
        self.assertEqual((p.active, p.removed_by_schedule), (False, True))

    def test_no_sale_windows_means_always_on_sale(self):
        res = self._post({"name": "Baguette"})
        self.assertEqual(res.status_code, 201, res.content)
        p = Product.objects.get(name="Baguette")
        self.assertEqual((p.active, p.sale_windows.count()), (True, 0))
        self.assertEqual(res.json()["sale_windows"], [])

    def test_discontinue_before_launch_is_rejected(self):
        res = self._post({"name": "Backwards", "sale_windows": [
            {"launch_date": "2026-12-31", "discontinue_date": "2026-12-01"},
        ]})
        self.assertEqual(res.status_code, 400)
        self.assertIn("sale_windows", res.json())
        self.assertFalse(Product.objects.filter(name="Backwards").exists())

    def test_bad_date_is_rejected(self):
        res = self._post({"name": "Junk", "sale_windows": [{"launch_date": "soon"}]})
        self.assertEqual(res.status_code, 400)
        self.assertFalse(Product.objects.filter(name="Junk").exists())
