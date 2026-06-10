/**
 * Thermal printer wrapper — talks to an Epson TM-T82X (M352/M352A
 * variants) via our in-app BravePosPrinter native module, which uses
 * Epson's official ePOS SDK for Android (vendored locally — JAR + .so).
 *
 * History: originally targeted Star TSP143IIIU.  We went through Star's
 * StarXpand (stario10) and StarPRNT SDK (stario + starioextension)
 * before the hardware was swapped to an Epson TM-T82X.  Epson printers
 * are dramatically easier to drive — pure ESC/POS, no firmware modes,
 * official SDK that handles USB enumeration and permissions cleanly.
 *
 * Identifier format: opaque string from Epson Discovery, e.g.
 *   "USB:000000000000000000"
 * Pass it back to the native module verbatim — don't parse.
 */
import { NativeModules } from 'react-native';
// useStarPrinter hook + image-printing types defined below.  Keeping
// `.ts` extension (not .tsx) means React/JSX-using parts live in a
// separate .tsx file — see useStarPrinter.tsx.

// ─── Native module types ───────────────────────────────────────────────────
type Section =
  | { type: 'text'; text: string; align?: 'left' | 'center' | 'right'; bold?: boolean; double?: boolean }
  | { type: 'row'; left: string; right: string; align?: 'left' | 'center' | 'right'; bold?: boolean; double?: boolean }
  | { type: 'divider' }
  | { type: 'feed'; lines?: number };

type PrintOptions = {
  cut?: 'partial' | 'full' | 'none';
  width?: number;
};

type PrintResult = { ok: boolean; error?: string; bytesSent?: number };

type NativePrinter = {
  list(): Promise<Array<{ identifier: string; model: string; macAddress?: string; interfaceType: string }>>;
  testPrint(identifier: string): Promise<PrintResult>;
  printReceipt(identifier: string, sections: Section[], options: PrintOptions): Promise<PrintResult>;
  printKitchenTicket(identifier: string, sections: Section[], options: PrintOptions): Promise<PrintResult>;
  // Image-based printing (used for Thai-language receipts that the
  // printer's ANK firmware can't render as text bytes).
  printImage(identifier: string, base64Png: string, widthDots: number): Promise<PrintResult>;
};

const BravePosPrinter: NativePrinter = NativeModules.BravePosPrinter;

// ─── Public types ──────────────────────────────────────────────────────────
export type DiscoveredPrinter = {
  identifier: string;
  interfaceType: 'Usb' | 'Bluetooth' | 'BluetoothLE' | 'Lan';
  model?: string;
};

export type PrinterConfig = {
  enabled: boolean;
  interface: 'Usb' | 'Bluetooth' | 'BluetoothLE' | 'Lan';
  identifier: string;
  model?: string;
  paperWidth?: 80 | 58;
  charsPerLine?: number;        // default 48 for 80mm
  cutType?: 'partial' | 'full' | 'none';
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
  // When true the receipt is reprinted as a void copy: a "ยกเลิก / VOIDED"
  // banner is stamped on the image so the cancelled bill is unmistakable.
  voided?: boolean;
  voided_by?: string;
};

export type ReceiptShop = {
  shop_name?: string;
  branch?: string;
  // Legacy free-form blob; new code prefers the split address_line_1/2 fields.
  address?: string;
  address_line_1?: string;
  address_line_2?: string;
  company_name?: string;
  phone?: string;
  tax_id?: string;
  pos_id?: string;
  pos_number?: string;
  tax_percent?: number;
  tax_mode?: 'inclusive' | 'exclusive';
  currency?: string;
  business_type?: string;
};

// ─── Discovery ─────────────────────────────────────────────────────────────
export async function discoverPrinters(
  _interfaces: Array<DiscoveredPrinter['interfaceType']> = ['Usb'],
  _timeoutMs = 4000,
): Promise<DiscoveredPrinter[]> {
  if (!BravePosPrinter) throw new Error('BravePosPrinter native module not linked');
  const found = await BravePosPrinter.list();
  return found.map((d) => ({
    identifier: d.identifier,
    interfaceType: (d.interfaceType as DiscoveredPrinter['interfaceType']) || 'Usb',
    model: d.model,
  }));
}

// ─── Receipt builder (structured sections) ─────────────────────────────────
function thb(n: number): string {
  return (Number(n) || 0).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

// Build a structured receipt that matches the clean test-print layout
// (image 2 in the May 30 chat):
//
//     Queue 26                        ← big + bold + centered
//
//     Brave POS                       ← bold + centered
//     Branch: Main                    ← centered
//   ─────────────────────────────
//     Date: 30/05/2026, 15:31:25     ← left
//     Invoice #: PS000000026
//   ─────────────────────────────
//     Qty  Item                Total ← header row
//     1    Chocogems pop ...   350.00
//   ─────────────────────────────
//     Total                    350.00 ← bold
//     Cash                     350.00
//   ─────────────────────────────
//        THANK YOU FOR YOUR SHOPPING
//          Powered by Brave POS
//
// We deliberately omit the legal/VAT block (company name, address, Tax
// ID, POS ID, VAT 7% breakdown, "Prices include VAT" notice).  Those
// belong on the *image-rendered* receipt with Thai labels — the text
// path here uses ASCII only since the printer firmware is ANK.
export function buildReceiptSections(order: ReceiptOrder, shop: ReceiptShop): Section[] {
  const sections: Section[] = [];

  // ─── Queue number ──────────────────────────────────────────────────────
  const queue =
    order.queue_number !== undefined
      ? String(order.queue_number)
      : (order.order_number || '').slice(-2).replace(/^0+/, '') || '1';
  sections.push({ type: 'text', text: `Queue ${queue}`, align: 'center', bold: true, double: true });
  sections.push({ type: 'feed', lines: 1 });

  // ─── Shop name + branch (no address / tax ID / POS ID) ────────────────
  if (shop.shop_name) sections.push({ type: 'text', text: shop.shop_name, align: 'center', bold: true });
  if (shop.branch) sections.push({ type: 'text', text: `Branch: ${shop.branch}`, align: 'center' });

  // ─── Metadata ─────────────────────────────────────────────────────────
  sections.push({ type: 'divider' });
  sections.push({ type: 'text', text: `Date: ${order.created_at_local ?? ''}` });
  sections.push({ type: 'text', text: `Invoice #: ${order.order_number}` });

  // ─── Items ─────────────────────────────────────────────────────────────
  sections.push({ type: 'divider' });
  sections.push({ type: 'row', left: 'Qty  Item', right: 'Total' });
  for (const it of order.items) {
    const lineTotal = (Number(it.price) || 0) * (Number(it.qty) || 0);
    const name = String(it.name).slice(0, 34);
    sections.push({ type: 'row', left: `${it.qty}  ${name}`, right: thb(lineTotal) });
  }

  // ─── Totals + payment ─────────────────────────────────────────────────
  sections.push({ type: 'divider' });
  const grandTotal = Number(order.total) || 0;
  sections.push({ type: 'row', left: 'Total', right: thb(grandTotal), bold: true });

  const paid = Number(order.paid_amount ?? order.total) || 0;
  sections.push({ type: 'row', left: order.payment_method || 'Cash', right: thb(paid) });
  const change = Number(order.change) || 0;
  if (change > 0) sections.push({ type: 'row', left: 'Change', right: thb(change) });

  // ─── Footer ───────────────────────────────────────────────────────────
  sections.push({ type: 'divider' });
  sections.push({ type: 'text', text: 'THANK YOU FOR YOUR SHOPPING', align: 'center', bold: true });
  sections.push({ type: 'text', text: 'Powered by Brave POS', align: 'center' });

  return sections;
}

function buildKitchenTicketSections(order: ReceiptOrder, shop: ReceiptShop): Section[] {
  const sections: Section[] = [];

  const queue =
    order.queue_number !== undefined
      ? String(order.queue_number)
      : (order.order_number || '').slice(-2).replace(/^0+/, '') || '1';
  sections.push({ type: 'text', text: `KITCHEN # ${queue}`, align: 'center', bold: true, double: true });
  sections.push({ type: 'divider' });

  if (shop.branch) sections.push({ type: 'text', text: `Branch: ${shop.branch}` });
  sections.push({ type: 'text', text: `Order #: ${order.order_number}` });
  sections.push({ type: 'text', text: `Time: ${order.created_at_local ?? ''}` });
  if (order.staff) sections.push({ type: 'text', text: `Staff: ${order.staff}` });
  sections.push({ type: 'divider' });

  for (const it of order.items) {
    sections.push({ type: 'text', text: `${it.qty} x ${it.name}`, bold: true });
  }

  sections.push({ type: 'divider' });
  return sections;
}

// ─── Public print API ──────────────────────────────────────────────────────
function makeOptions(config: PrinterConfig): PrintOptions {
  return {
    cut: config.cutType ?? 'partial',
    width: config.charsPerLine ?? (config.paperWidth === 58 ? 32 : 48),
  };
}

export async function printReceipt(
  config: PrinterConfig,
  order: ReceiptOrder,
  shop: ReceiptShop,
): Promise<{ ok: true } | { ok: false; error: string }> {
  if (!config?.enabled) return { ok: false, error: 'Printer is disabled' };
  if (!config.identifier) return { ok: false, error: 'No printer identifier configured' };
  if (!BravePosPrinter) return { ok: false, error: 'Native module not linked (need rebuild)' };

  const sections = buildReceiptSections(order, shop);
  const res = await BravePosPrinter.printReceipt(config.identifier, sections, makeOptions(config));
  return res.ok ? { ok: true } : { ok: false, error: res.error || 'Print failed' };
}

export async function printKitchenTicket(
  config: PrinterConfig,
  order: ReceiptOrder,
  shop: ReceiptShop,
): Promise<{ ok: true } | { ok: false; error: string }> {
  if (!config?.enabled) return { ok: false, error: 'Printer is disabled' };
  if (!config.identifier) return { ok: false, error: 'No printer identifier configured' };
  if (!BravePosPrinter) return { ok: false, error: 'Native module not linked (need rebuild)' };

  const sections = buildKitchenTicketSections(order, shop);
  const res = await BravePosPrinter.printKitchenTicket(config.identifier, sections, makeOptions(config));
  return res.ok ? { ok: true } : { ok: false, error: res.error || 'Print failed' };
}

export async function testPrint(
  config: PrinterConfig,
  _shop: ReceiptShop,
): Promise<{ ok: true } | { ok: false; error: string }> {
  if (!config?.enabled || !config.identifier) {
    return { ok: false, error: 'Printer not configured' };
  }
  if (!BravePosPrinter) return { ok: false, error: 'Native module not linked (need rebuild)' };

  const res = await BravePosPrinter.testPrint(config.identifier);
  return res.ok ? { ok: true } : { ok: false, error: res.error || 'Print failed' };
}
