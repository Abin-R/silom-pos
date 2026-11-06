"""The customer's slip on the self-order page: who gets it, and how it's served."""
from __future__ import annotations

import io
import unicodedata
from decimal import Decimal

from django.test import Client, TestCase
from PIL import Image

from bravepos import receipt_image, selforder
from bravepos.models import SelfOrder

from .factories import StubGateway, make_branch, make_product, make_shop, open_shift


class ReceiptPngTests(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch()
        open_shift(self.branch)
        self.product = make_product(self.branch, price='100.00', stock=50)
        self.client = Client()

    def _draft(self, qty=1):
        with StubGateway():
            d = selforder.create_draft(
                self.branch, [{'product_id': str(self.product.id), 'qty': qty}],
            )
            selforder.start_payment(d, 'qr', redirect_url='https://x/')
        return SelfOrder.objects.get(pk=d.pk)

    def _paid(self, qty=1):
        d = self._draft(qty)
        with StubGateway(paid=True):
            selforder.promote(d)
        return SelfOrder.objects.get(pk=d.pk)

    def _url(self, draft):
        return f'/order/s/{draft.token}/receipt.png'

    def test_no_slip_until_paid(self):
        res = self.client.get(self._url(self._draft()))
        self.assertEqual(res.status_code, 404)
        # Cloudflare would otherwise hold this 404 past the payment landing.
        self.assertEqual(res['Cache-Control'], 'no-store')

    def test_unknown_token_is_404(self):
        self.assertEqual(self.client.get('/order/s/nope/receipt.png').status_code, 404)

    def test_paid_draft_gets_an_image(self):
        res = self.client.get(self._url(self._paid()))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'], 'image/png')
        self.assertEqual(res['Cache-Control'], 'no-store')
        self.assertNotIn('Content-Disposition', res)
        image = Image.open(io.BytesIO(res.content))
        self.assertEqual(image.width, receipt_image.WIDTH * receipt_image.SCALE)

    def test_download_saves_under_the_order_number(self):
        draft = self._paid()
        res = self.client.get(self._url(draft) + '?download=1')
        self.assertEqual(
            res['Content-Disposition'],
            f'attachment; filename="receipt-{draft.order.order_number}.png"',
        )

    def test_status_points_at_the_slip(self):
        draft = self._paid()
        data = self.client.get(f'/order/s/{draft.token}/status/').json()
        self.assertEqual(data['receipt_url'], self._url(draft))

    def test_every_line_gets_drawn(self):
        def height(order):
            return Image.open(io.BytesIO(receipt_image.render_customer_slip(order))).height

        order = self._paid().order
        short = height(order)
        for i in range(5):
            order.items.create(name=f'Extra {i}', price=Decimal('10.00'), qty=1)
        self.assertGreater(height(order), short)

    def test_a_long_name_and_a_free_item_render(self):
        order = self._paid().order
        order.items.create(name='ครัวซองต์อัลมอนด์ราดช็อกโกแลตเบลเยียมพิเศษ' * 3,
                           price=Decimal('10.00'), qty=2)
        order.items.create(name='Free cookie', price=Decimal('0'), qty=1, is_free=True)
        self.assertTrue(receipt_image.render_customer_slip(order))

    def test_a_void_redraws_the_slip(self):
        draft = self._paid()
        before = self.client.get(self._url(draft)).content
        draft.order.status = 'cancel'
        draft.order.save(update_fields=['status'])
        self.assertNotEqual(self.client.get(self._url(draft)).content, before)


class WrapTests(TestCase):
    def setUp(self):
        self.font = receipt_image._font(16)

    def test_thai_never_splits_a_cluster(self):
        text = 'น้ำผึ้งเกล็ดขนมปังกรอบชาเขียวมัทฉะเย็นปั่นไม่ใส่น้ำตาล' * 4
        lines = receipt_image.wrap(text, self.font, 300)
        self.assertGreater(len(lines), 1)
        self.assertEqual(''.join(lines), text)
        for line in lines:
            self.assertNotEqual(unicodedata.category(line[0]), 'Mn', line)
            self.assertNotIn(line[0], receipt_image.THAI_NO_LINE_START)
            self.assertNotIn(line[-1], receipt_image.THAI_NO_LINE_END)
            self.assertLessEqual(self.font.getlength(line), 300)

    def test_english_breaks_at_spaces(self):
        lines = receipt_image.wrap('Almond croissant with extra dark chocolate', self.font, 200)
        self.assertEqual(' '.join(lines), 'Almond croissant with extra dark chocolate')

    def test_quantity_stays_with_its_item(self):
        name = 'ครัวซองต์อัลมอนด์ราดช็อกโกแลตเบลเยียม'
        width = self.font.getlength(name) + 2
        lines = receipt_image.wrap(f'2    {name}', self.font, width)
        self.assertGreater(len(lines), 1)
        self.assertNotEqual(lines[0].strip(), '2')

    def test_word_that_exactly_fits_the_room(self):
        self.assertEqual(receipt_image._split_word('ab', self.font, 10_000), ('ab', ''))
