/**
 * Thermal printer wrapper — talks to a Star TSP100III (or any ESC/POS-
 * compatible USB thermal printer) directly from this tablet.
 *
 * SDK: `react-native-thermal-receipt-printer-image-qr` (ESC/POS over USB
 * via Android's UsbManager).  This is the same approach commercial POS
 * apps (B-POS, Shopify POS) use — vendor-neutral, works with the printer's
 * default factory firmware (no StarPRNT flash required).
 *
 * For Thai characters we encode the receipt content in TIS-620 (CP874) and
 * tell the printer to interpret bytes that way via `ESC t 21`.
 *
 * Filename kept as `starPrinter.ts` to avoid a churn of imports across the
 * app — the public API is unchanged from the previous Star-SDK version.
 */
import { USBPrinter, type IUSBPrinter } from 'react-native-thermal-receipt-printer-image-qr';

// ─── Public types (unchanged from prior wrapper) ─────────────────────────────
export type DiscoveredPrinter = {
  /** "<vendorId>:<productId>" — used as both identifier and lookup key */
  identifier: string;
  interfaceType: 'Usb' | 'Bluetooth' | 'BluetoothLE' | 'Lan';
  model?: string;
};

export type PrinterConfig = {
  enabled: boolean;
  interface: 'Usb' | 'Bluetooth' | 'BluetoothLE' | 'Lan';
  identifier: string;       // "<vendorId>:<productId>"
  paperWidth?: 80 | 58;
};

export type ReceiptOrder = {
  order_number: string;
  queue_number?: string | number;
  items: Array<{ name: string; qty: number; price: number }>;
  total: number;
  payment_method?: string;
  paid_amount?: number;
  change?: number;
  created_at_local?: string;
  staff?: string;
};

export type ReceiptShop = {
  shop_name?: string;
  branch?: string;
  address?: string;
  phone?: string;
  tax_id?: string;
  pos_id?: string;
  pos_number?: string;
  tax_percent?: number;
  tax_mode?: 'inclusive' | 'exclusive';
};

// ─── ESC/POS byte helpers ────────────────────────────────────────────────────
const ESC = 0x1b;
const GS  = 0x1d;
const LF  = 0x0a;

const CMD = {
  INIT:               [ESC, 0x40],               // ESC @
  CODEPAGE_THAI:      [ESC, 0x74, 0x15],         // ESC t 21 — TIS-620 / CP874
  ALIGN_LEFT:         [ESC, 0x61, 0x00],
  ALIGN_CENTER:       [ESC, 0x61, 0x01],
  ALIGN_RIGHT:        [ESC, 0x61, 0x02],
  BOLD_ON:            [ESC, 0x45, 0x01],
  BOLD_OFF:           [ESC, 0x45, 0x00],
  SIZE_NORMAL:        [GS,  0x21, 0x00],         // 1x1
  SIZE_DOUBLE:        [GS,  0x21, 0x11],         // 2x2
  SIZE_DOUBLE_WIDTH:  [GS,  0x21, 0x10],         // 2x1
  CUT_PARTIAL:        [GS,  0x56, 0x42, 0x10],   // partial cut with feed
};

/** TIS-620 / CP874 encoder.  Maps Thai unicode → printer code page bytes.
 *  Anything outside ASCII + Thai script gets a space fallback. */
function tis620(str: string): number[] {
  const out: number[] = [];
  for (const ch of str) {
    const cp = ch.codePointAt(0) ?? 0x20;
    if (cp < 0x80) {
      out.push(cp);
    } else if (cp >= 0x0e01 && cp <= 0x0e5b) {
      // Thai script: U+0E01..U+0E5B → 0xA1..0xFB in TIS-620
      out.push(cp - 0x0e01 + 0xa1);
    } else {
      out.push(0x20); // unsupported char → space
    }
  }
  return out;
}

class EscPosBuilder {
  private bytes: number[] = [];

  raw(arr: number[]): this { this.bytes.push(...arr); return this; }

  text(s: string): this { this.bytes.push(...tis620(s)); return this; }

  /** Pads to ~48 chars on 80mm paper (Font A is 12 dots wide, 576 dots / 12 = 48). */
  row(left: string, right: string, width = 48): this {
    const padCount = Math.max(1, width - left.length - right.length);
    return this.text(left + ' '.repeat(padCount) + right + '\n');
  }

  newline(n = 1): this { for (let i = 0; i < n; i++) this.bytes.push(LF); return this; }

  toBase64(): string {
    // RN bridge wants base64.  Use btoa over a Latin-1 byte string (each
    // value 0..255 maps 1:1 onto a JS char code in that range).
    let bin = '';
    for (const b of this.bytes) bin += String.fromCharCode(b);
    // global.btoa is available in RN Hermes.
    return (global as any).btoa(bin);
  }
}

function buildReceipt(order: ReceiptOrder, shop: ReceiptShop): string {
  const b = new EscPosBuilder();

  // Init + select Thai code page
  b.raw(CMD.INIT).raw(CMD.CODEPAGE_THAI);

  // Queue number — centered, 2x
  const queue =
    order.queue_number !== undefined
      ? String(order.queue_number)
      : (order.order_number || '').slice(-2).replace(/^0+/, '') || '1';
  b.raw(CMD.ALIGN_CENTER).raw(CMD.SIZE_DOUBLE)
    .text(`คิวที่ ${queue}\n`)
    .raw(CMD.SIZE_NORMAL).newline();

  // Shop header (still centered)
  if (shop.shop_name) b.raw(CMD.BOLD_ON).text(`${shop.shop_name}\n`).raw(CMD.BOLD_OFF);
  if (shop.branch) b.text(`สาขา: ${shop.branch}\n`);
  if (shop.address) b.text(`${shop.address}\n`);
  if (shop.phone) b.text(`${shop.phone}\n`);
  if (shop.tax_id) b.text(`เลขประจำตัวผู้เสียภาษี: ${shop.tax_id}\n`);
  if (shop.pos_id) b.text(`POS ID: ${shop.pos_id}\n`);
  b.text('ใบเสร็จรับเงิน/ใบกำกับภาษีอย่างย่อ\n');

  // Body — left aligned
  b.raw(CMD.ALIGN_LEFT)
    .text('------------------------------------------------\n')
    .text(`วันที่: ${order.created_at_local ?? ''}\n`)
    .text(`Invoice #: ${order.order_number}\n`);

  const posNum = shop.pos_number || '001';
  const staff = order.staff || '';
  b.text(`POS #: ${posNum}${staff ? `  พนักงาน: ${staff}` : ''}\n`)
    .text('------------------------------------------------\n');

  // Items
  b.text('Qty  Item                                  Total\n');
  for (const it of order.items) {
    const lineTotal = (Number(it.price) || 0) * (Number(it.qty) || 0);
    const name = String(it.name).slice(0, 34);
    b.row(`${it.qty}  ${name}`, thb(lineTotal));
  }
  b.text('------------------------------------------------\n');

  // Totals
  const total = Number(order.total) || 0;
  const taxPct = Number(shop.tax_percent ?? 7);
  const inclusive = (shop.tax_mode ?? 'inclusive') === 'inclusive';
  const beforeVat = inclusive
    ? Math.round((total / (1 + taxPct / 100)) * 100) / 100
    : total;
  const vat = inclusive
    ? Math.round((total - beforeVat) * 100) / 100
    : Math.round((total * taxPct) / 100) / 100;

  b.row('รวมเป็นเงิน', thb(total));
  b.row('มูลค่าสินค้าก่อน VAT', thb(beforeVat));
  b.row(`คิดเป็นมูลค่าภาษี ${taxPct}%`, thb(vat));

  const qtyTotal = order.items.reduce((s, i) => s + (Number(i.qty) || 0), 0);
  b.raw(CMD.BOLD_ON).raw(CMD.SIZE_DOUBLE_WIDTH);
  b.row(`จำนวน ${qtyTotal} ชิ้น  รวมมูลค่า`, thb(total), 24); // 24 chars at 2x width
  b.raw(CMD.BOLD_OFF).raw(CMD.SIZE_NORMAL);

  b.text('------------------------------------------------\n');

  // Payment
  b.text('การชำระเงิน\n');
  const paid = Number(order.paid_amount ?? order.total) || 0;
  b.row(order.payment_method || 'Cash', thb(paid));
  const change = Number(order.change) || 0;
  if (change > 0) b.row('เงินทอน', thb(change));

  b.text('------------------------------------------------\n');

  // Footer
  b.raw(CMD.ALIGN_CENTER)
    .text('ราคาสินค้ารวมภาษีมูลค่าเพิ่มแล้ว\n')
    .raw(CMD.BOLD_ON).text('THANK YOU FOR YOUR SHOPPING\n').raw(CMD.BOLD_OFF);
  if (shop.phone) b.text(`Tel. ${shop.phone}\n`);
  b.text('Powered by Brave POS\n');

  b.newline(2).raw(CMD.CUT_PARTIAL);

  return b.toBase64();
}

function thb(n: number): string {
  return (Number(n) || 0).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

// ─── Public API ──────────────────────────────────────────────────────────────
export async function discoverPrinters(
  _interfaces: Array<DiscoveredPrinter['interfaceType']> = ['Usb'],
  _timeoutMs = 4000,
): Promise<DiscoveredPrinter[]> {
  await USBPrinter.init();
  const list = await USBPrinter.getDeviceList();
  return (list || []).map((p: IUSBPrinter) => ({
    identifier: `${p.vendor_id}:${p.product_id}`,
    interfaceType: 'Usb' as const,
    model: p.device_name,
  }));
}

export async function printReceipt(
  config: PrinterConfig,
  order: ReceiptOrder,
  shop: ReceiptShop,
): Promise<{ ok: true } | { ok: false; error: string }> {
  if (!config?.enabled) return { ok: false, error: 'Printer is disabled' };
  if (!config.identifier)
    return { ok: false, error: 'No printer identifier configured' };

  const [vendorIdStr, productIdStr] = config.identifier.split(':');
  if (!vendorIdStr || !productIdStr) {
    return { ok: false, error: `Invalid identifier (expected vid:pid): ${config.identifier}` };
  }
  // Native Android binding expects Integer for vid/pid even though the
  // library's TypeScript .d.ts declares string parameters.  Pass numbers.
  const vendorId = Number(vendorIdStr);
  const productId = Number(productIdStr);

  try {
    await USBPrinter.init();
    await USBPrinter.connectPrinter(vendorId as any, productId as any);
    const data = buildReceipt(order, shop);
    USBPrinter.printRaw(data);
    return { ok: true };
  } catch (e: any) {
    return { ok: false, error: e?.message || String(e) };
  } finally {
    try { await USBPrinter.closeConn(); } catch {}
  }
}

export async function testPrint(
  config: PrinterConfig,
  shop: ReceiptShop,
): Promise<{ ok: true } | { ok: false; error: string }> {
  return printReceipt(
    config,
    {
      order_number: 'TEST000001',
      queue_number: 'TEST',
      items: [{ name: 'Test print — ทดสอบ', qty: 1, price: 0 }],
      total: 0,
      payment_method: 'TEST',
      paid_amount: 0,
      change: 0,
      created_at_local: new Date().toLocaleString('en-GB'),
      staff: '',
    },
    shop,
  );
}
