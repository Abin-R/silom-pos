"""Product image size caps, and keeping them out of the menu HTML.

Regression cover for a real incident: three products with unresized base64
images (one 1917x998 PNG at 262 KB) made the customer self-order menu 648 KB
and 8-13 s to load on a fast connection, with a healthy TTFB. Nothing was slow
— the document was just enormous, because ``menu_for`` inlined every image
into it.

Two things are asserted, matching the two independent defences:
  * nothing oversized can be *stored*, whichever client writes it;
  * nothing base64 reaches the menu HTML even if it somehow was stored.
"""
from __future__ import annotations

import base64
import io

from django.test import Client, TestCase

from PIL import Image

from bravepos import images, selforder
from bravepos.models import Product

from .factories import make_branch, make_product, open_shift


def data_uri(width, height, fmt='PNG', colour=(200, 30, 30)):
    """A real encodable image, not a fake string — normalize() decodes it."""
    img = Image.new('RGB', (width, height), colour)
    # Noise, so the encoder can't compress a flat colour down to nothing and
    # make an "oversized" fixture accidentally tiny.
    px = img.load()
    for x in range(0, width, 3):
        for y in range(0, height, 3):
            px[x, y] = ((x * 7) % 256, (y * 13) % 256, (x * y) % 256)
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    mime = 'image/png' if fmt == 'PNG' else 'image/jpeg'
    return f'data:{mime};base64,{base64.b64encode(buf.getvalue()).decode()}'


class NormalizeTests(TestCase):
    def test_hosted_url_untouched(self):
        url = 'https://images.pexels.com/photos/36500580/photo.jpeg?w=400'
        self.assertEqual(images.normalize(url), url)

    def test_empty_untouched(self):
        self.assertEqual(images.normalize(''), '')
        self.assertEqual(images.normalize(None), '')

    def test_oversized_is_downscaled(self):
        big = data_uri(1917, 998)
        self.assertGreater(len(big), images.MAX_CHARS)

        small = images.normalize(big)

        self.assertLessEqual(len(small), images.MAX_CHARS)
        raw, mime = images.decode(small)
        self.assertEqual(mime, 'image/jpeg')
        self.assertLessEqual(max(Image.open(io.BytesIO(raw)).size), images.MAX_DIM)

    def test_small_image_is_left_byte_for_byte_alone(self):
        """Re-saving a product must not recompress. JPEG generation loss is
        cumulative and there is no undo."""
        small = data_uri(120, 120, fmt='JPEG')
        once = images.normalize(small)
        self.assertEqual(once, small)
        self.assertEqual(images.normalize(once), once)

    def test_normalize_is_idempotent_on_a_downscaled_image(self):
        first = images.normalize(data_uri(1917, 998))
        self.assertEqual(images.normalize(first), first)

    def test_undecodable_is_dropped_not_stored(self):
        self.assertEqual(images.normalize('data:image/png;base64,bm90LWFuLWltYWdl'), '')

    def test_transparent_png_flattens_to_white_not_black(self):
        img = Image.new('RGBA', (900, 900), (255, 255, 255, 0))
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        uri = f'data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}'

        raw, _ = images.decode(images.normalize(uri))
        out = Image.open(io.BytesIO(raw)).convert('RGB')
        self.assertEqual(out.getpixel((5, 5)), (255, 255, 255))


class WritePathTests(TestCase):
    """The cap has to hold on every path that can write the column."""

    def setUp(self):
        self.branch = make_branch()

    def test_api_serializer_caps_image_url(self):
        from bravepos.serializers import ProductSerializer

        product = make_product(self.branch)
        ser = ProductSerializer(product, data={
            'name': product.name, 'price': '100.00',
            'image_url': data_uri(1917, 998),
        }, partial=True)
        self.assertTrue(ser.is_valid(), ser.errors)
        ser.save()

        product.refresh_from_db()
        self.assertLessEqual(len(product.image_url), images.MAX_CHARS)

    def test_api_serializer_caps_image_base64(self):
        from bravepos.serializers import ProductSerializer

        product = make_product(self.branch)
        ser = ProductSerializer(product, data={
            'image_base64': data_uri(1600, 1600),
        }, partial=True)
        self.assertTrue(ser.is_valid(), ser.errors)
        ser.save()

        product.refresh_from_db()
        self.assertLessEqual(len(product.image_base64), images.MAX_CHARS)

    def test_backoffice_form_caps_image_url(self):
        from backoffice.views import _apply_product_form

        product = Product(branch=self.branch)
        _apply_product_form(product, {
            'name': 'Manggo Cake', 'price': '120', 'cost': '0', 'stock': '1',
            'image_url': data_uri(1915, 1021),
        }, self.branch)

        self.assertLessEqual(len(product.image_url), images.MAX_CHARS)


class MenuPayloadTests(TestCase):
    def setUp(self):
        self.branch = make_branch()
        # The template only emits the menu at all when a shift is open — no
        # shift means no `const MENU` to inspect.
        open_shift(self.branch)
        self.product = make_product(self.branch)

    def test_data_uri_becomes_a_url_not_an_inlined_blob(self):
        # Set the column directly, bypassing the serializer — the menu must be
        # safe even against a row that predates the write-time cap.
        blob = data_uri(1917, 998)
        Product.objects.filter(pk=self.product.pk).update(image_url=blob)
        self.product.refresh_from_db()

        entry = selforder.menu_for(self.branch)[0]

        self.assertFalse(entry['image_url'].startswith('data:'))
        self.assertIn(f'/order/{self.branch.id}/img/{self.product.id}/', entry['image_url'])
        self.assertIn(images.digest(blob), entry['image_url'])

    def test_hosted_url_passes_through(self):
        url = 'https://cdn.example.com/a.jpg'
        Product.objects.filter(pk=self.product.pk).update(image_url=url)
        self.product.refresh_from_db()

        self.assertEqual(selforder.menu_for(self.branch)[0]['image_url'], url)

    def test_menu_page_stays_small_with_a_huge_stored_image(self):
        """The actual regression: the rendered document, end to end."""
        Product.objects.filter(pk=self.product.pk).update(image_url=data_uri(1917, 998))

        body = Client().get(f'/order/{self.branch.id}/').content.decode()

        # Scoped to the MENU payload rather than the whole document: the page's
        # own CSS carries a small decorative base64 background, which is not
        # what broke us and is not what this guards.
        menu_line = next(l for l in body.splitlines() if 'const MENU = ' in l)
        self.assertNotIn('data:image', menu_line)
        self.assertLess(len(menu_line), 8 * 1024)


class ProductImageEndpointTests(TestCase):
    def setUp(self):
        self.branch = make_branch()
        self.product = make_product(self.branch)
        self.blob = images.normalize(data_uri(1917, 998))
        Product.objects.filter(pk=self.product.pk).update(image_url=self.blob)
        self.url = f'/order/{self.branch.id}/img/{self.product.id}/'

    def test_serves_the_decoded_bytes(self):
        res = Client().get(self.url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'], 'image/jpeg')
        self.assertEqual(res.content, images.decode(self.blob)[0])

    def test_versioned_url_is_cached_immutably(self):
        res = Client().get(self.url, {'v': images.digest(self.blob)})
        self.assertIn('immutable', res['Cache-Control'])

    def test_stale_version_is_not_cached_immutably(self):
        res = Client().get(self.url, {'v': 'stale0000000'})
        self.assertNotIn('immutable', res['Cache-Control'])

    def test_etag_revalidation_returns_304(self):
        etag = Client().get(self.url)['ETag']
        res = Client().get(self.url, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(res.status_code, 304)

    def test_404_when_self_ordering_is_off_for_the_branch(self):
        self.branch.self_order_enabled = False
        self.branch.save(update_fields=['self_order_enabled'])

        self.assertEqual(Client().get(self.url).status_code, 404)

    def test_404_for_a_product_of_another_branch(self):
        other = make_product(make_branch(name='Other'))
        res = Client().get(f'/order/{self.branch.id}/img/{other.id}/')
        self.assertEqual(res.status_code, 404)

    def test_404_when_the_product_has_a_hosted_url(self):
        Product.objects.filter(pk=self.product.pk).update(
            image_url='https://cdn.example.com/a.jpg', image_base64='')
        self.assertEqual(Client().get(self.url).status_code, 404)
