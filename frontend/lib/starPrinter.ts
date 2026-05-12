/**
 * Star printer wrapper — talks directly to a USB/LAN/Bluetooth Star printer
 * from this Android tablet (no backend round-trip).
 *
 * Why this exists: when the backend lives on Azure, the backend's process
 * has no idea what's plugged into the cashier's tablet via USB-C.  Star's
 * Android SDK lets the app drive the printer itself.  We use the StarIO10
 * RN bindings — same SDK Silom POS and similar apps use.
 *
 * Receipt layout mirrors the Python renderer in backend/printer.py.
 */
import {
  InterfaceType,
  StarConnectionSettings,
  StarDeviceDiscoveryManagerFactory,
  StarPrinter,
  StarXpandCommand,
} from 'react-native-star-io10';

// ─── Types ──────────────────────────────────────────────────────────────────
export type DiscoveredPrinter = {
  /** Device identifier the SDK uses on subsequent connects */
  identifier: string;
  /** "USB", "BT", "BTLE", "LAN" */
  interfaceType: 'Usb' | 'Bluetooth' | 'BluetoothLE' | 'Lan';
  /** Human-readable model name when the SDK could resolve it */
  model?: string;
};

export type PrinterConfig = {
  enabled: boolean;
  interface: 'Usb' | 'Bluetooth' | 'BluetoothLE' | 'Lan';
  identifier: string;        // USB device id, BT MAC, or IP
  paperWidth?: 80 | 58;      // mm
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

const SATANG_PER_THB = 100;

// ─── Discovery ──────────────────────────────────────────────────────────────
/**
 * Scan for connected Star printers.  Pass the interface types you want to
 * check.  Default is just USB — Bluetooth scanning needs runtime permission
 * which we'd ask for separately.
 */
export async function discoverPrinters(
  interfaces: Array<DiscoveredPrinter['interfaceType']> = ['Usb'],
  timeoutMs = 4000,
): Promise<DiscoveredPrinter[]> {
  const found: DiscoveredPrinter[] = [];
  const manager = await StarDeviceDiscoveryManagerFactory.create(
    interfaces.map((i) => InterfaceType[i]),
  );
  manager.discoveryTime = timeoutMs;
  manager.onPrinterFound = (p) => {
    found.push({
      identifier: p.connectionSettings.identifier,
      interfaceType: p.connectionSettings.interfaceType as any,
      model: (p.information as any)?.model,
    });
  };
  await manager.startDiscovery();
  // Wait for the SDK to finish + a small buffer
  await new Promise((r) => setTimeout(r, timeoutMs + 200));
  await manager.stopDiscovery().catch(() => {});
  return found;
}

// ─── Receipt builder ─────────────────────────────────────────────────────────
function thb(n: number): string {
  return (Number(n) || 0).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function buildReceipt(order: ReceiptOrder, shop: ReceiptShop): any {
  const printer = new StarXpandCommand.PrinterBuilder();

  // Queue number — big and centred
  const queue =
    order.queue_number !== undefined
      ? String(order.queue_number)
      : (order.order_number || '').slice(-2).replace(/^0+/, '') || '1';
  printer
    .styleAlignment(StarXpandCommand.Printer.Alignment.Center)
    .styleMagnification(new StarXpandCommand.MagnificationParameter(2, 2))
    .actionPrintText(`คิวที่ ${queue}\n`)
    .styleMagnification(new StarXpandCommand.MagnificationParameter(1, 1))
    .actionFeedLine(1);

  // Shop header (centered)
  if (shop.shop_name) {
    printer
      .styleBold(true)
      .actionPrintText(`${shop.shop_name}\n`)
      .styleBold(false);
  }
  if (shop.branch) printer.actionPrintText(`สาขา: ${shop.branch}\n`);
  if (shop.address) printer.actionPrintText(`${shop.address}\n`);
  if (shop.phone) printer.actionPrintText(`${shop.phone}\n`);
  if (shop.tax_id)
    printer.actionPrintText(`เลขประจำตัวผู้เสียภาษี: ${shop.tax_id}\n`);
  if (shop.pos_id) printer.actionPrintText(`POS ID: ${shop.pos_id}\n`);
  printer.actionPrintText('ใบเสร็จรับเงิน/ใบกำกับภาษีอย่างย่อ\n');

  // Divider
  printer
    .styleAlignment(StarXpandCommand.Printer.Alignment.Left)
    .actionPrintText('--------------------------------\n')
    .actionPrintText(`Order Ref:\n`)
    .actionPrintText(`วันที่: ${order.created_at_local ?? ''}\n`)
    .actionPrintText(`Invoice #: ${order.order_number}\n`);

  const posNum = shop.pos_number || '001';
  const staff = order.staff || '';
  printer.actionPrintText(
    `POS #: ${posNum}${staff ? `        ชื่อพนักงาน: ${staff}` : ''}\n`,
  );
  printer.actionPrintText('--------------------------------\n');

  // Items
  printer.actionPrintText('Qty รายละเอียด              Total\n');
  for (const it of order.items) {
    const lineTotal = (Number(it.price) || 0) * (Number(it.qty) || 0);
    // Two-line layout: "qty name" then "  total" right-aligned
    printer.actionPrintText(`${it.qty}  ${it.name}\n`);
    printer
      .styleAlignment(StarXpandCommand.Printer.Alignment.Right)
      .actionPrintText(`${thb(lineTotal)}\n`)
      .styleAlignment(StarXpandCommand.Printer.Alignment.Left);
  }
  printer.actionPrintText('--------------------------------\n');

  // Totals
  const total = Number(order.total) || 0;
  const taxPct = Number(shop.tax_percent ?? 7);
  const inclusive = (shop.tax_mode ?? 'inclusive') === 'inclusive';
  const beforeVat = inclusive
    ? Math.round((total / (1 + taxPct / 100)) * 100) / 100
    : total;
  const vat = inclusive ? Math.round((total - beforeVat) * 100) / 100
                        : Math.round((total * taxPct / 100) * 100) / 100;

  const row = (label: string, value: string) => {
    const pad = Math.max(1, 32 - label.length - value.length);
    printer.actionPrintText(`${label}${' '.repeat(pad)}${value}\n`);
  };
  row('รวมเป็นเงิน', thb(total));
  row('มูลค่าสินค้าก่อน VAT', thb(beforeVat));
  row(`คิดเป็นมูลค่าภาษี ${taxPct}%`, thb(vat));

  const qtyTotal = order.items.reduce((s, i) => s + (Number(i.qty) || 0), 0);
  printer
    .styleBold(true)
    .styleMagnification(new StarXpandCommand.MagnificationParameter(2, 1));
  row(`จำนวน ${qtyTotal} ชิ้น   รวมมูลค่า`, thb(total));
  printer
    .styleBold(false)
    .styleMagnification(new StarXpandCommand.MagnificationParameter(1, 1));

  printer.actionPrintText('--------------------------------\n');

  // Payment
  printer.actionPrintText('การชำระเงิน\n');
  const paid = Number(order.paid_amount ?? order.total) || 0;
  row(order.payment_method || 'Cash', thb(paid));
  const change = Number(order.change) || 0;
  if (change > 0) row('เงินทอน', thb(change));

  printer.actionPrintText('--------------------------------\n');

  // Footer
  printer
    .styleAlignment(StarXpandCommand.Printer.Alignment.Center)
    .actionPrintText('ราคาสินค้ารวมภาษีมูลค่าเพิ่มแล้ว\n')
    .styleBold(true)
    .actionPrintText('THANK YOU FOR YOUR SHOPPING\n')
    .styleBold(false);
  if (shop.phone) printer.actionPrintText(`Tel. ${shop.phone}\n`);
  printer.actionPrintText('Powered by Brave POS\n');

  // Feed + partial cut
  printer
    .actionFeedLine(2)
    .actionCut(StarXpandCommand.Printer.CutType.Partial);

  const builder = new StarXpandCommand.StarXpandCommandBuilder();
  builder.addDocument(
    new StarXpandCommand.DocumentBuilder().addPrinter(printer),
  );
  return builder;
}

// ─── Public API ──────────────────────────────────────────────────────────────
export async function printReceipt(
  config: PrinterConfig,
  order: ReceiptOrder,
  shop: ReceiptShop,
): Promise<{ ok: true } | { ok: false; error: string }> {
  if (!config?.enabled) return { ok: false, error: 'Printer is disabled' };
  if (!config.identifier) return { ok: false, error: 'No printer identifier configured' };

  const settings = new StarConnectionSettings();
  settings.interfaceType = InterfaceType[config.interface];
  settings.identifier = config.identifier;

  const printer = new StarPrinter(settings);
  try {
    await printer.open();
    const cmds = await buildReceipt(order, shop).getCommands();
    await printer.print(cmds);
    return { ok: true };
  } catch (e: any) {
    return { ok: false, error: e?.message || String(e) };
  } finally {
    try { await printer.close(); } catch { /* ignore */ }
    try { await printer.dispose(); } catch { /* ignore */ }
  }
}

/** Sends a tiny "Test print OK" slip to verify the printer is reachable. */
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
