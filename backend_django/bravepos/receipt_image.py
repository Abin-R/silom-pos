"""The customer's slip as a PNG, for the self-order page.

A Pillow port of the abbreviated layout in ``frontend/components/ReceiptImage.tsx``.
That view only exists inside the till app, where it is captured to an image and
sent to the printer, so a customer's phone needs its own copy of the drawing.
Same sections, sizes, margins and labels: **change one, change the other.**

Two differences on purpose:

* 426 px wide, not 576.  The content column is the same 386 px, but the till's
  extra right padding only keeps text out of the printer's clipped edge, and
  would leave the slip sitting off-centre on a phone.
* Drawn at 2x, so it stays sharp on a phone screen.

Only the abbreviated slip is here.  The packer's copy and the full tax invoice
are till documents, and a self-order customer is never handed either.
"""
from __future__ import annotations

import io
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import segno
from django.conf import settings as django_settings
from django.utils import timezone as djtz
from PIL import Image, ImageDraw, ImageFont

from .models import Settings

ASSETS = Path(__file__).resolve().parent / 'assets' / 'receipt'

SCALE = 2
PADDING_X = 20
PADDING_Y = 24
CONTENT_WIDTH = 386
WIDTH = CONTENT_WIDTH + PADDING_X * 2
AMOUNT_MIN_WIDTH = 90
AMOUNT_GAP = 8
QR_TARGET_SIZE = 180
LOGO_SIZE = 140

BLACK = 0
GREY = 0x55

# Only used when the Settings row is empty.  Same values as SHOP_DEFAULTS in
# ReceiptImage.tsx, and likewise no POS ID: that one is per branch.
SHOP_DEFAULTS = {
    'company': 'บริษัท เบรฟ แบรนด์ จำกัด',
    'address': [
        '55 อาคารไบโอเฮ้าส์ ชั้น5 ห้องเลขที่508 ซอยสุขุมวิท',
        '39 ถนนสุขุมวิท แขวงคลองตันเหนือ เขตวัฒนา',
        'กรุงเทพมหานคร 10110',
    ],
    'branch': 'Samyan',
    'phone': '0644184887',
    'tax_id': '0105563083534',
    'pos_number': '001',
    'tax_percent': 7,
}

ORDER_TYPE_LABELS = {
    'counter': 'เคาน์เตอร์ / Counter Order',
    'self_order': 'สั่งเอง / Self Order',
    'delivery': 'เดลิเวอรี / Delivery',
}

# A line can't start with a vowel that follows its consonant, and can't end on
# one that is written before it.  Combining marks are caught by category.
THAI_NO_LINE_START = 'ะาำๅๆ'
THAI_NO_LINE_END = 'เแโใไ'


@lru_cache(maxsize=None)
def _font(size: int, bold: bool = False):
    name = 'Sarabun-Bold.ttf' if bold else 'Sarabun-Regular.ttf'
    return ImageFont.truetype(str(ASSETS / name), size * SCALE)


@lru_cache(maxsize=1)
def _logo():
    size = LOGO_SIZE * SCALE
    return Image.open(ASSETS / 'rolling-pinn-logo.png').convert('L').resize(
        (size, size), Image.LANCZOS,
    )


def _thb(n) -> str:
    return f'{float(n or 0):,.2f}'


def _can_break_at(text: str, i: int) -> bool:
    """Whether a line may end just before ``text[i]``."""
    ch = text[i]
    if unicodedata.category(ch) == 'Mn' or ch in THAI_NO_LINE_START:
        return False
    return text[i - 1] not in THAI_NO_LINE_END


def _split_word(word: str, font, width: float) -> tuple[str, str]:
    """Cut a word too long for ``width``.  Returns (what fits, the rest)."""
    fit = 1
    while fit < len(word) and font.getlength(word[:fit + 1]) <= width:
        fit += 1
    if fit == len(word):
        return word, ''
    for i in range(fit, 0, -1):
        if _can_break_at(word, i):
            return word[:i], word[i:]
    # Nothing breakable fits: take the whole cluster, however it overhangs.
    end = next((i for i in range(fit + 1, len(word)) if _can_break_at(word, i)), len(word))
    return word[:end], word[end:]


def wrap(text: str, font, width: float) -> list[str]:
    """Greedy wrap: at spaces, and only inside a word that won't fit a line.

    Thai has no spaces between words and there's no dictionary here, so a long
    Thai run breaks between character clusters rather than where the till's
    ICU would.  The rows it affects are rare; a split syllable is not a thing
    this allows.
    """
    lines: list[str] = []
    current = ''
    for token in re.findall(r'\s+|\S+', text):
        if font.getlength(current + token) <= width:
            current += token
            continue
        if token.isspace():
            lines.append(current)
            current = ''
            continue
        # An item row starts with its quantity: stay next to it, don't leave it
        # alone on a line.
        beside_qty = current.strip().isdigit()
        if font.getlength(token) > width or beside_qty:
            while font.getlength(current + token) > width:
                room = width - font.getlength(current)
                head, tail = _split_word(token, font, room)
                if not tail and current.strip():
                    break
                lines.append((current + head).rstrip())
                current, token = '', tail
            current += token
        else:
            lines.append(current.rstrip())
            current = token
    if current.strip() or not lines:
        lines.append(current.rstrip())
    return lines


def _ellipsize(text: str, font, width: float) -> str:
    """``numberOfLines={1}``."""
    if font.getlength(text) <= width:
        return text
    while text and font.getlength(text + '…') > width:
        text = text[:-1]
    return text.rstrip() + '…'


def _line_height(font) -> int:
    ascent, descent = font.getmetrics()
    return ascent + descent


class _Canvas:
    """A cursor down the slip.  All arguments are in slip pixels, as in the TSX."""

    def __init__(self, image):
        self.image = image
        self.draw = ImageDraw.Draw(image)
        self.left = PADDING_X * SCALE
        self.right = (PADDING_X + CONTENT_WIDTH) * SCALE
        self.width = CONTENT_WIDTH * SCALE
        self.y = PADDING_Y * SCALE

    def gap(self, px: int):
        self.y += px * SCALE

    def _put(self, x: float, y: float, text: str, font, fill: int = BLACK):
        self.draw.text((x, y), text, font=font, fill=fill, anchor='la')

    def center(self, text: str, size: int, bold=False, fill=BLACK, top=0, bottom=0,
               single_line=False):
        font = _font(size, bold)
        self.gap(top)
        lines = (
            [_ellipsize(text, font, self.width)] if single_line
            else wrap(text, font, self.width)
        )
        for line in lines:
            x = self.left + (self.width - font.getlength(line)) / 2
            self._put(x, self.y, line, font, fill)
            self.y += _line_height(font)
        self.gap(bottom)

    def line(self, text: str, size: int = 16):
        font = _font(size)
        for line in wrap(text, font, self.width):
            self._put(self.left, self.y, line, font)
            self.y += _line_height(font)

    def one_line(self, text: str, size: int = 16):
        font = _font(size)
        self._put(self.left, self.y, _ellipsize(text, font, self.width), font)
        self.y += _line_height(font)

    def dash(self):
        self.gap(8)
        on, off = 3 * SCALE, 3 * SCALE
        for x in range(self.left, self.right, on + off):
            self.draw.rectangle(
                [x, self.y, min(x + on, self.right) - 1, self.y + SCALE - 1], fill=BLACK,
            )
        self.gap(1)
        self.gap(8)

    def _amount(self, text: str, font, y: float) -> float:
        """Right-aligned in its column.  Returns where the column starts."""
        w = font.getlength(text)
        self._put(self.right - w, y, text, font)
        return self.right - max(w, AMOUNT_MIN_WIDTH * SCALE)

    def pos_row(self, left: str, right: str):
        font = _font(16)
        self.gap(1)
        left_w = min(font.getlength(left), self.width)
        self._put(self.left, self.y, _ellipsize(left, font, self.width), font)
        room = self.width - left_w - AMOUNT_GAP * SCALE
        shown = _ellipsize(right, font, room)
        self._put(self.right - font.getlength(shown), self.y, shown, font)
        self.y += _line_height(font)
        self.gap(1)

    def head_row(self, left: str, right: str):
        font = _font(16, bold=True)
        self.gap(1)
        self._put(self.left, self.y, left, font)
        self._amount(right, font, self.y)
        self.y += _line_height(font)
        self.gap(1)

    def item_row(self, text: str, amount: str):
        font = _font(16)
        self.gap(1)
        column = self._amount(amount, font, self.y)
        room = column - AMOUNT_GAP * SCALE - self.left
        for line in wrap(text, font, room):
            self._put(self.left, self.y, line, font)
            self.y += _line_height(font)
        self.gap(1)

    def total_row(self, label: str, amount: str):
        """A VAT-block row: the label sits right-aligned against the amount."""
        font = _font(16)
        self.gap(1)
        column = self._amount(amount, font, self.y)
        end = column - (AMOUNT_GAP + 8) * SCALE
        shown = _ellipsize(label, font, end - self.left)
        self._put(end - font.getlength(shown), self.y, shown, font)
        self.y += _line_height(font)
        self.gap(1)

    def grand_total_row(self, count: str, label: str, amount: str):
        font, big = _font(16), _font(24, bold=True)
        self.gap(5)     # marginVertical 1 + marginTop 4 on this row
        height = max(_line_height(font), _line_height(big))
        small_y = self.y + (height - _line_height(font)) / 2
        amount_w = max(big.getlength(amount), AMOUNT_MIN_WIDTH * SCALE)
        big_y = self.y + (height - _line_height(big)) / 2
        self._put(self.right - big.getlength(amount), big_y, amount, big)
        label_x = self.right - amount_w - AMOUNT_GAP * SCALE - font.getlength(label)
        self._put(label_x, small_y, label, font)
        self._put(self.left, small_y, _ellipsize(count, font, label_x - self.left), font)
        self.y += height
        self.gap(1)

    def split_row(self, left: str, amount: str):
        font = _font(16)
        self.gap(1)
        column = self._amount(amount, font, self.y)
        room = column - AMOUNT_GAP * SCALE - self.left
        self._put(self.left, self.y, _ellipsize(left, font, room), font)
        self.y += _line_height(font)
        self.gap(1)

    def logo(self):
        size = LOGO_SIZE * SCALE
        self.gap(4)
        self.image.paste(_logo(), (self.left + (self.width - size) // 2, self.y))
        self.y += size
        self.gap(10)

    def qr(self, value: str):
        matrix = segno.make_qr(value, error='m', boost_error=False).matrix
        modules = len(matrix)
        # Whole slip pixels per module, like the till, so the code stays crisp.
        cell = max(2, QR_TARGET_SIZE // modules) * SCALE
        quiet = cell * 4
        x0 = self.left + (self.width - modules * cell) // 2
        y0 = self.y + quiet
        for row, cells in enumerate(matrix):
            for col, dark in enumerate(cells):
                if dark:
                    x, y = x0 + col * cell, y0 + row * cell
                    self.draw.rectangle([x, y, x + cell - 1, y + cell - 1], fill=BLACK)
        self.y += modules * cell + quiet * 2


def _shop_lines(shop) -> list[str]:
    lines = [
        (getattr(shop, 'address_line_1', '') or '').strip(),
        (getattr(shop, 'address_line_2', '') or '').strip(),
    ]
    lines = [line for line in lines if line]
    if lines:
        return lines
    blob = (getattr(shop, 'address', '') or '').strip()
    if blob:
        return [line for line in blob.splitlines() if line.strip()]
    return SHOP_DEFAULTS['address']


def _paint(canvas: _Canvas, order, items: list[dict], shop) -> int:
    """Draw the slip top to bottom.  Returns how tall it came out."""
    field = lambda name: getattr(shop, name, '') or ''  # noqa: E731
    phone = field('phone') or SHOP_DEFAULTS['phone']
    tax_percent = float(
        shop.tax_percent if shop is not None and shop.tax_percent is not None
        else SHOP_DEFAULTS['tax_percent']
    )
    branch = order.branch.name if order.branch_id else ''
    pos_id = order.branch.pos_id if order.branch_id else ''
    number = order.order_number or ''

    if order.queue_number is not None:
        queue = str(order.queue_number)
    else:
        queue = number[-2:].lstrip('0') or '1'
    order_type = ORDER_TYPE_LABELS.get(order.receipt_order_type or '', '')

    grand_total = float(order.total or 0)
    tax_rate = tax_percent / 100
    before_vat = grand_total / (1 + tax_rate) if tax_rate > 0 else grand_total
    vat = grand_total - before_vat
    items_gross = sum(it['price'] * it['qty'] for it in items)
    gross = float(order.subtotal or 0) or items_gross
    discount = float(order.discount_amount or 0) or max(0, gross - grand_total)
    item_count = sum(it['qty'] for it in items)

    if order_type:
        canvas.center(order_type, 22, bold=True, top=6, single_line=True)
    canvas.center(f'คิวที่ {queue}', 40, bold=True, top=6, bottom=16, single_line=True)
    canvas.logo()

    canvas.one_line(f'สาขา: {branch or field("branch") or SHOP_DEFAULTS["branch"]}')
    canvas.center(field('company_name') or SHOP_DEFAULTS['company'], 15, top=2)
    for line in _shop_lines(shop):
        canvas.center(line, 15, top=2)
    canvas.center(phone, 15, top=2)
    canvas.center(f'เลขประจำตัวผู้เสียภาษี: {field("tax_id") or SHOP_DEFAULTS["tax_id"]}', 15, top=2)
    # The label always prints; the value is this branch's own machine number,
    # or blank.  Never another branch's number standing in for it.
    canvas.center(f'POS ID: {pos_id}'.rstrip(), 15, top=2)
    canvas.center('ใบเสร็จรับเงิน/ใบกำกับภาษีอย่างย่อ', 16, bold=True, top=12, bottom=4)
    if order.status == 'cancel':
        canvas.center('ยกเลิกการขาย', 20, bold=True, top=8, bottom=2)
    canvas.dash()

    created = djtz.localtime(order.created_at).strftime('%d/%m/%Y, %H:%M:%S')
    canvas.line('Order Ref:')
    canvas.line(f'วันที่: {created}')
    canvas.line(f'Invoice #: {number}')
    canvas.pos_row(
        f'POS #: {field("pos_number") or SHOP_DEFAULTS["pos_number"]}',
        f'ชื่อพนักงาน: {order.staff or "Self Order"}',
    )
    canvas.dash()

    canvas.head_row('Qty  รายละเอียด', 'Total')
    for it in items:
        canvas.item_row(f'{it["qty"]}    {it["name"]}', _thb(it['price'] * it['qty']))
    canvas.dash()

    if discount > 0:
        canvas.total_row('รวมยอดสินค้า', _thb(gross))
        canvas.total_row('ส่วนลด', f'-{_thb(discount)}')
    canvas.total_row('รวมเป็นเงิน', _thb(grand_total))
    canvas.total_row('มูลค่าสินค้าก่อน Vat', _thb(before_vat))
    canvas.total_row(f'คิดเป็นมูลค่าภาษี {tax_percent:g}%', _thb(vat))
    canvas.grand_total_row(f'จำนวน {item_count} ชิ้น', 'รวมมูลค่า', _thb(grand_total))
    canvas.dash()

    canvas.line('การชำระเงิน')
    canvas.split_row(order.payment_method or 'Cash', _thb(order.paid_amount))

    canvas.gap(12)
    base = django_settings.BRAVEPOS['PUBLIC_BASE_URL']
    canvas.qr(f'{base}/receipt/{number}/')
    canvas.center('สแกนเพื่อแสดงความคิดเห็น / Scan to leave feedback', 12, top=6, bottom=8)

    canvas.center('ราคาสินค้ารวมภาษีมูลค่าเพิ่มแล้ว', 13, top=4)
    canvas.center('THANK YOU FOR YOUR SHOPPING', 16, bold=True, top=6)
    canvas.center(f'Tel. {phone}', 14, top=2)
    canvas.center('Powered by Brave POS', 12, fill=GREY, top=8)

    return canvas.y + PADDING_Y * SCALE


def render_customer_slip(order) -> bytes:
    """The order's customer slip, as PNG bytes."""
    shop = Settings.objects.first()
    items = [
        {'name': it.name, 'qty': int(it.qty or 0), 'price': float(it.price or 0)}
        for it in order.items.all()
    ]
    # Measure on a throwaway canvas, then draw for real at the right height.
    height = _paint(_Canvas(Image.new('L', (1, 1), 255)), order, items, shop)
    image = Image.new('L', (WIDTH * SCALE, height), 255)
    _paint(_Canvas(image), order, items, shop)

    out = io.BytesIO()
    image.save(out, format='PNG')
    return out.getvalue()
