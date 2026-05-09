"""Receipt printing for Star TSP100III (and any ESC/POS-compatible thermal printer).

Renders the receipt as a 1-bit PNG with full Thai text support (PIL + Noto Sans
Thai), then dispatches the bitmap to the printer over one of three transports:

  - "file"     : write to a Linux usblp device, e.g. /dev/usb/lp0 (USB plug-and-play)
  - "network"  : open a TCP socket to host:9100 (Ethernet / Wi-Fi printers)
  - "disabled" : no-op (default; safe to leave on a dev machine without a printer)

Set `printer_*` fields on the shop Settings document to configure.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("printer")

# 80mm paper at 203 DPI on TSP100 = 576 dots wide. 58mm paper = 384 dots.
PAPER_DOTS = {80: 576, 58: 384}

# Sarabun ships with the project (backend/fonts/) so receipts render correctly
# on any host without an apt install.  Sarabun has full Thai + Latin coverage.
_BUNDLED_FONTS = Path(__file__).parent / "fonts"
_FONT_CANDIDATES = [
    str(_BUNDLED_FONTS / "Sarabun-Regular.ttf"),
    "/usr/share/fonts/truetype/tlwg/Loma.ttf",  # apt: fonts-thai-tlwg
    "/usr/share/fonts/truetype/sarabun/Sarabun-Regular.ttf",
]
_FONT_BOLD_CANDIDATES = [
    str(_BUNDLED_FONTS / "Sarabun-Bold.ttf"),
    "/usr/share/fonts/truetype/tlwg/Loma-Bold.ttf",
    "/usr/share/fonts/truetype/sarabun/Sarabun-Bold.ttf",
]


def _find_font(candidates: list[str]) -> str:
    for p in candidates:
        if Path(p).exists():
            return p
    raise FileNotFoundError(
        "No Thai font found. Install one with: sudo apt install fonts-noto-thai"
    )


def _thb(n: float) -> str:
    return f"{n:,.2f}"


def render_receipt(order: dict, settings: dict, paper_width_mm: int = 80) -> Image.Image:
    """Build a 1-bit PNG of the receipt that mirrors the reference layout.

    Layout (top to bottom):
        Queue number (large, centered)
        Shop name (bold, centered)
        Branch / address / phone / tax ID / POS ID  (small, centered)
        Divider
        Order Ref / date / invoice # / POS # / staff
        Divider
        Items: qty | name | total
        Divider
        Subtotal (total before VAT) / VAT 7% / Grand total (bold large)
        Payment line
        Divider
        Footer: thank you, tel, "Powered by …"
    """
    width = PAPER_DOTS.get(paper_width_mm, 576)
    pad = 12

    font_normal = ImageFont.truetype(_find_font(_FONT_CANDIDATES), 22)
    font_small = ImageFont.truetype(_find_font(_FONT_CANDIDATES), 18)
    font_bold = ImageFont.truetype(_find_font(_FONT_BOLD_CANDIDATES), 24)
    font_big = ImageFont.truetype(_find_font(_FONT_BOLD_CANDIDATES), 38)
    font_total = ImageFont.truetype(_find_font(_FONT_BOLD_CANDIDATES), 32)

    # Compose into an oversized white canvas; we'll crop to actual content height.
    canvas = Image.new("L", (width, 4000), color=255)
    draw = ImageDraw.Draw(canvas)
    y = pad

    def text_size(s: str, font: ImageFont.FreeTypeFont) -> tuple[int, int]:
        bbox = draw.textbbox((0, 0), s, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]

    def line(s: str, font: ImageFont.FreeTypeFont, *, align: str = "left", indent: int = 0):
        nonlocal y
        for sub in _wrap(s, font, width - 2 * pad - indent, draw):
            w, h = text_size(sub, font)
            if align == "center":
                x = (width - w) // 2
            elif align == "right":
                x = width - pad - w
            else:
                x = pad + indent
            draw.text((x, y), sub, font=font, fill=0)
            y += h + 4

    def hr(char: str = "-"):
        nonlocal y
        # Dashed divider matching the reference receipt style.
        y += 6
        dash_w = max(1, int(draw.textlength(char, font=font_small)))
        n = max(1, (width - 2 * pad) // dash_w)
        draw.text((pad, y), char * n, font=font_small, fill=0)
        y += text_size(char, font_small)[1] + 6

    def kv_row(label: str, value: str, *, font_l=None, font_v=None):
        nonlocal y
        font_l = font_l or font_normal
        font_v = font_v or font_normal
        wv, hv = text_size(value, font_v)
        draw.text((pad, y), label, font=font_l, fill=0)
        draw.text((width - pad - wv, y), value, font=font_v, fill=0)
        y += max(text_size(label, font_l)[1], hv) + 4

    # ---- Queue number ----
    # Prefer an explicit per-day counter if the order has one; otherwise fall
    # back to the last 2 digits of the invoice number (cheap approximation).
    queue_raw = order.get("queue_number")
    if queue_raw is None:
        queue_raw = (order.get("order_number") or "")[-2:].lstrip("0") or "1"
    line(f"คิวที่ {queue_raw}", font_big, align="center")
    y += 6

    # ---- Logo (optional) ----
    logo_path = settings.get("logo_path") or settings.get("logo_url")
    if logo_path and Path(str(logo_path)).exists():
        try:
            logo = Image.open(str(logo_path)).convert("L")
            target_w = min(width - 2 * pad, 240)
            ratio = target_w / logo.width
            logo = logo.resize((target_w, int(logo.height * ratio)))
            x = (width - logo.width) // 2
            canvas.paste(logo, (x, y))
            y += logo.height + 4
        except Exception:
            pass

    # ---- Shop ----
    line(settings.get("shop_name", ""), font_bold, align="center")
    branch = settings.get("branch")
    if branch:
        line(f"สาขา: {branch}", font_small, align="center")
    addr = settings.get("address")
    if addr:
        line(addr, font_small, align="center")
    phone = settings.get("phone")
    if phone:
        line(phone, font_small, align="center")
    tax_id = settings.get("tax_id")
    if tax_id:
        line(f"เลขประจำตัวผู้เสียภาษี: {tax_id}", font_small, align="center")
    pos_id = settings.get("pos_id")
    if pos_id:
        line(f"POS ID: {pos_id}", font_small, align="center")

    line("ใบเสร็จรับเงิน/ใบกำกับภาษีอย่างย่อ", font_small, align="center")

    hr()

    # ---- Order meta (matches reference layout) ----
    order_ref = order.get("order_ref", "")
    line(f"Order Ref:{(' ' + order_ref) if order_ref else ''}", font_small)
    line(f"วันที่: {order.get('created_at_local', order.get('created_at', ''))}", font_small)
    line(f"Invoice #: {order.get('order_number', '')}", font_small)
    pos_num = settings.get("pos_number", "001")
    staff = order.get("staff", "")
    kv_row(f"POS #: {pos_num}", f"ชื่อพนักงาน: {staff}" if staff else "", font_l=font_small, font_v=font_small)

    hr()

    # ---- Items header ----
    kv_row("Qty รายละเอียด", "Total", font_l=font_small, font_v=font_small)

    # ---- Items ----
    for it in order.get("items", []):
        name = it.get("name", "")
        qty = it.get("qty", 0)
        total = it.get("price", 0) * qty
        # Left side: "1  Item name"
        left = f"{qty}  {name}"
        # Two-column layout — wrap the name if it's too long
        right = _thb(total)
        wv, _ = text_size(right, font_small)
        max_left_w = width - 2 * pad - wv - 12
        # wrap name lines
        wrapped = _wrap(left, font_small, max_left_w, draw)
        for i, sub in enumerate(wrapped):
            draw.text((pad, y), sub, font=font_small, fill=0)
            if i == 0:
                draw.text((width - pad - wv, y), right, font=font_small, fill=0)
            y += text_size(sub, font_small)[1] + 4

    hr()

    # ---- Totals ----
    total = float(order.get("total", 0) or 0)
    tax_percent = float(settings.get("tax_percent", 7) or 7)
    tax_mode = settings.get("tax_mode", "inclusive")
    if tax_mode == "inclusive":
        # Total includes VAT. Show: รวมเป็นเงิน=total, มูลค่าก่อน VAT=total/1.07, VAT=diff
        before_vat = round(total / (1 + tax_percent / 100), 2)
        vat = round(total - before_vat, 2)
    else:
        before_vat = total
        vat = round(total * tax_percent / 100, 2)

    kv_row("รวมเป็นเงิน", _thb(total), font_l=font_small, font_v=font_small)
    kv_row("มูลค่าสินค้าก่อน VAT", _thb(before_vat), font_l=font_small, font_v=font_small)
    kv_row(f"คิดเป็นมูลค่าภาษี {int(tax_percent)}%", _thb(vat), font_l=font_small, font_v=font_small)

    qty_total = sum(it.get("qty", 0) for it in order.get("items", []))
    kv_row(f"จำนวน {qty_total} ชิ้น     รวมมูลค่า", _thb(total), font_l=font_normal, font_v=font_total)

    hr()

    # ---- Payment ----
    line("การชำระเงิน", font_small)
    pm = order.get("payment_method", "Cash")
    kv_row(pm, _thb(float(order.get("paid_amount", total) or total)), font_l=font_small, font_v=font_small)
    change = float(order.get("change", 0) or 0)
    if change > 0:
        kv_row("เงินทอน", _thb(change), font_l=font_small, font_v=font_small)

    hr()

    line("ราคาสินค้ารวมภาษีมูลค่าเพิ่มแล้ว", font_small, align="center")
    line("THANK YOU FOR YOUR SHOPPING", font_bold, align="center")
    if phone:
        line(f"Tel. {phone}", font_small, align="center")
    line("Powered by Brave POS", font_small, align="center")

    y += pad

    # Crop to actual height + binarize.
    cropped = canvas.crop((0, 0, width, y))
    return cropped.convert("1")


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_w: int, draw: ImageDraw.ImageDraw) -> list[str]:
    """Word-wrap that handles Thai (no spaces) with character-level fallback."""
    if not text:
        return [""]
    out: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split(" ")
        cur = ""
        for w in words:
            trial = (cur + " " + w).strip() if cur else w
            tw = draw.textlength(trial, font=font)
            if tw <= max_w:
                cur = trial
            else:
                if cur:
                    out.append(cur)
                # If single word still too long, char-wrap (Thai)
                if draw.textlength(w, font=font) > max_w:
                    chunk = ""
                    for ch in w:
                        if draw.textlength(chunk + ch, font=font) <= max_w:
                            chunk += ch
                        else:
                            out.append(chunk)
                            chunk = ch
                    cur = chunk
                else:
                    cur = w
        if cur:
            out.append(cur)
    return out or [""]


# ============================================================================
# Dispatch
# ============================================================================

class PrinterError(RuntimeError):
    pass


def dispatch(image: Image.Image, *, transport: str, address: Optional[str]) -> None:
    """Send an already-rendered receipt image to the printer.

    transport: "file" | "network" | "disabled"
    address:
        for "file"    : device path, e.g. "/dev/usb/lp0"
        for "network" : "host:port" (defaults to port 9100 if omitted)
    """
    if transport == "disabled":
        logger.info("Printer disabled — skipping print.")
        return

    if transport == "file":
        path = address or "/dev/usb/lp0"
        if not os.path.exists(path):
            raise PrinterError(f"Printer device not found at {path}. Is the USB cable connected?")
        from escpos.printer import File
        p = File(path)
        try:
            p.image(image, impl="bitImageRaster")
            p.cut()
        finally:
            p.close()
        return

    if transport == "network":
        if not address:
            raise PrinterError("Network printer requires address (host or host:port).")
        host, _, port_s = address.partition(":")
        port = int(port_s) if port_s else 9100
        from escpos.printer import Network
        p = Network(host, port=port, timeout=5)
        try:
            p.image(image, impl="bitImageRaster")
            p.cut()
        finally:
            p.close()
        return

    if transport == "windows":
        if not address:
            raise PrinterError("Windows printer requires address (the exact installed printer name).")
        from escpos.printer import Win32Raw
        p = Win32Raw(address)
        try:
            p.image(image, impl="bitImageRaster")
            p.cut()
        finally:
            p.close()
        return

    if transport == "windows_driver":
        # Print via the Windows print spooler + installed driver (GDI), not raw ESC/POS.
        # Works regardless of the printer's STARPRNT/ESC/POS emulation mode.
        if not address:
            raise PrinterError("windows_driver requires address (the exact installed printer name).")
        try:
            import win32ui
            import win32print
            from PIL import ImageWin
        except ImportError as e:
            raise PrinterError(f"pywin32 not installed: {e}")
        hDC = win32ui.CreateDC()
        hDC.CreatePrinterDC(address)
        hDC.StartDoc("Receipt")
        hDC.StartPage()
        try:
            printable_w = hDC.GetDeviceCaps(8)   # HORZRES
            printable_h = hDC.GetDeviceCaps(10)  # VERTRES
            img_w, img_h = image.size
            # Scale to printable width while preserving aspect ratio.
            scale = printable_w / img_w if img_w else 1.0
            out_w = int(img_w * scale)
            out_h = int(img_h * scale)
            if out_h > printable_h and printable_h > 0:
                out_h = printable_h
            dib = ImageWin.Dib(image.convert("RGB"))
            dib.draw(hDC.GetHandleOutput(), (0, 0, out_w, out_h))
        finally:
            hDC.EndPage()
            hDC.EndDoc()
            hDC.DeleteDC()
        return

    raise PrinterError(f"Unknown printer transport: {transport!r}")


def print_receipt(order: dict, settings: dict) -> None:
    """Render and dispatch in one call. Honors settings.printer_enabled."""
    if not settings.get("printer_enabled"):
        return
    transport = settings.get("printer_transport", "disabled")
    address = settings.get("printer_address")
    paper = int(settings.get("printer_paper_width", 80) or 80)
    img = render_receipt(order, settings, paper_width_mm=paper)
    dispatch(img, transport=transport, address=address)
