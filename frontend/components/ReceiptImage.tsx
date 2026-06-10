// Receipt rendered as a React Native view, then captured to a PNG by
// react-native-view-shot and sent to the printer via addImage().  This
// is the *only* path that gets Thai glyphs onto the TM-T82X (firmware
// is ANK / Latin-only, so addText() Thai bytes print as `?`).
//
// Layout matches the reference receipt the user provided (the original
// "The rolling pinn" Silom POS receipt) — full Thai labels, company
// header, Tax ID / POS ID, VAT 7% breakdown, etc.  Width is fixed at
// 576 px to match the TM-T82X print head exactly.

import React, { forwardRef } from "react";
import { View, Text, Image, StyleSheet } from "react-native";
// eslint-disable-next-line @typescript-eslint/no-var-requires
const qrcode: (typeNumber: number, errorCorrectionLevel: string) => {
  addData: (data: string) => void;
  make: () => void;
  getModuleCount: () => number;
  isDark: (row: number, col: number) => boolean;
} = require("qrcode-generator");
import type { ReceiptOrder, ReceiptShop } from "../lib/starPrinter";

const LOGO = require("../assets/images/rolling-pinn-logo.png");

// Base URL the receipt QR resolves to.  Per-receipt URLs are formed by
// appending the order number so each receipt lands on its own customer
// page (Issue Tax Invoice + Leave a Review buttons).  Matches the prod
// backend host from .env.production (EXPO_PUBLIC_BACKEND_URL).
const QR_BASE_URL = "https://pos.rollingpinn.com/receipt";

// Renders a QR code as a grid of black/white View cells.  Pure JS, no
// native dep — works through view-shot capture exactly like the rest
// of the receipt.  Cell size is floored to an integer so the thermal
// printer maps each module to a whole number of dots, keeping the
// code crisp instead of antialiased.
function QrCode({ value, targetSize }: { value: string; targetSize: number }) {
  const qr = qrcode(0, "M");
  qr.addData(value);
  qr.make();
  const modules = qr.getModuleCount();
  const cell = Math.max(2, Math.floor(targetSize / modules));
  const quiet = cell * 4; // QR spec: ≥4-module quiet zone for reliable scanning
  return (
    <View style={{ alignSelf: "center", backgroundColor: "#FFF", padding: quiet }}>
      {Array.from({ length: modules }).map((_, row) => (
        <View key={row} style={{ flexDirection: "row" }}>
          {Array.from({ length: modules }).map((_, col) => (
            <View
              key={col}
              style={{
                width: cell,
                height: cell,
                backgroundColor: qr.isDark(row, col) ? "#000" : "#FFF",
              }}
            />
          ))}
        </View>
      ))}
    </View>
  );
}

// 80mm paper @ 180 dpi = 576 dots printable width.  useStarPrinter
// passes the same constant to captureRef.width so the captured PNG is
// exactly this wide regardless of device density.
export const RECEIPT_WIDTH = 576;

const FONT = "Sarabun";
const BOLD = "Sarabun-Bold";

// Final fallbacks used only when the backend Settings row is empty
// (e.g. fresh dev install). All of these are now editable in the
// backoffice Shop page — once a value is saved there, it flows through
// /api/settings → ReceiptShop → this component without a code change.
const SHOP_DEFAULTS = {
  shop_name: "The rolling pinn",
  branch: "Samyan",
  company: "บริษัท เบรฟ แบรนด์ จำกัด",
  // Legacy hardcoded address — only rendered when both address_line_1
  // and address_line_2 are blank.
  address: [
    "55 อาคารไบโอเฮ้าส์ ชั้น5 ห้องเลขที่508 ซอยสุขุมวิท",
    "39 ถนนสุขุมวิท แขวงคลองตันเหนือ เขตวัฒนา",
    "กรุงเทพมหานคร 10110",
  ],
  phone: "0644184887",
  tax_id: "0105563083534",
  pos_id: "E020140003A0087",
  pos_number: "001",
  tax_percent: 7,
};

const thb = (n: number) =>
  (Number(n) || 0).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

const Dash = () => <View style={s.dash} />;

type Props = {
  order: ReceiptOrder;
  shop?: ReceiptShop;
  // Fires once the bundled logo PNG has decoded and rendered.  The
  // print hook gates captureRef on this — without it, view-shot can
  // snapshot the view before the image is ready, producing a receipt
  // with a blank space where the logo should be.
  onLogoReady?: () => void;
};

export const ReceiptImage = forwardRef<View, Props>(({ order, shop, onLogoReady }, ref) => {
  // Prefer the backoffice-edited address_line_1/2 fields; fall back to the
  // legacy `address` blob (split on newlines) and only use the hardcoded
  // default block when no shop data exists at all.
  const addressLines: string[] = (() => {
    const lines = [shop?.address_line_1, shop?.address_line_2]
      .map((l) => (l || "").trim())
      .filter((l) => l.length > 0);
    if (lines.length) return lines;
    const blob = (shop?.address || "").trim();
    if (blob) return blob.split(/\r?\n/).filter((l) => l.trim().length > 0);
    return SHOP_DEFAULTS.address;
  })();

  const sh = {
    shop_name: shop?.shop_name || SHOP_DEFAULTS.shop_name,
    branch: shop?.branch || SHOP_DEFAULTS.branch,
    company: shop?.company_name || SHOP_DEFAULTS.company,
    address: addressLines,
    phone: shop?.phone || SHOP_DEFAULTS.phone,
    tax_id: shop?.tax_id || SHOP_DEFAULTS.tax_id,
    pos_id: shop?.pos_id || SHOP_DEFAULTS.pos_id,
    pos_number: shop?.pos_number || SHOP_DEFAULTS.pos_number,
    tax_percent: Number(shop?.tax_percent ?? SHOP_DEFAULTS.tax_percent),
  };

  const queue =
    order.queue_number !== undefined
      ? String(order.queue_number)
      : (order.order_number || "").slice(-2).replace(/^0+/, "") || "1";

  const grandTotal = Number(order.total) || 0;
  const taxRate = sh.tax_percent / 100;
  const subtotalBeforeVat = taxRate > 0 ? grandTotal / (1 + taxRate) : grandTotal;
  const vat = grandTotal - subtotalBeforeVat;
  const itemCount = order.items.reduce((sum, it) => sum + (Number(it.qty) || 0), 0);
  const paid = Number(order.paid_amount ?? order.total) || 0;
  const change = Number(order.change) || 0;

  return (
    <View ref={ref} collapsable={false} style={s.root}>
      {/* ─── Void banner ──────────────────────────────────────────── */}
      {order.voided && (
        <View style={s.voidBanner}>
          <Text style={s.voidBannerText}>** ยกเลิก / VOIDED **</Text>
          {!!order.voided_by && (
            <Text style={s.voidBannerSub}>โดย / by: {order.voided_by}</Text>
          )}
        </View>
      )}

      {/* ─── Big queue number ─────────────────────────────────────── */}
      <Text style={s.queue} numberOfLines={1}>คิวที่ {queue}</Text>

      {/* ─── Shop logo (replaces the text title) ──────────────────── */}
      <Image
        source={LOGO}
        style={s.logo}
        resizeMode="contain"
        onLoadEnd={onLogoReady}
      />

      {/* ─── Branch + company info ────────────────────────────────── */}
      <Text style={s.branchLine} numberOfLines={1}>สาขา: {sh.branch}</Text>
      <Text style={s.companyCenter}>{sh.company}</Text>
      {sh.address.map((line, i) => (
        <Text key={i} style={s.companyCenter}>{line}</Text>
      ))}
      <Text style={s.companyCenter}>{sh.phone}</Text>
      <Text style={s.companyCenter}>เลขประจำตัวผู้เสียภาษี: {sh.tax_id}</Text>
      <Text style={s.companyCenter}>POS ID: {sh.pos_id}</Text>

      <Text style={s.receiptType}>ใบเสร็จรับเงิน/ใบกำกับภาษีอย่างย่อ</Text>

      <Dash />

      {/* ─── Order metadata ───────────────────────────────────────── */}
      <Text style={s.line}>Order Ref:</Text>
      <Text style={s.line}>วันที่: {order.created_at_local ?? ""}</Text>
      <Text style={s.line}>Invoice #: {order.order_number}</Text>
      <View style={s.row}>
        <Text style={s.line} numberOfLines={1}>POS #: {sh.pos_number}</Text>
        <Text style={[s.line, s.staffName]} numberOfLines={1}>ชื่อพนักงาน: {order.staff || ""}</Text>
      </View>

      <Dash />

      {/* ─── Items ────────────────────────────────────────────────── */}
      <View style={s.row}>
        <Text style={[s.line, s.bold, { flex: 1 }]} numberOfLines={1}>Qty  รายละเอียด</Text>
        <Text style={[s.line, s.bold, s.amount]} numberOfLines={1}>Total</Text>
      </View>
      {order.items.map((it, i) => {
        const lineTotal = (Number(it.price) || 0) * (Number(it.qty) || 0);
        return (
          <View key={i} style={s.row}>
            <Text style={[s.line, s.itemName]}>{it.qty}    {it.name}</Text>
            <Text style={[s.line, s.amount]} numberOfLines={1}>{thb(lineTotal)}</Text>
          </View>
        );
      })}

      <Dash />

      {/* ─── VAT breakdown ────────────────────────────────────────── */}
      <View style={s.row}>
        <Text style={[s.line, s.vatLabel]} numberOfLines={1}>รวมเป็นเงิน</Text>
        <Text style={[s.line, s.amount]} numberOfLines={1}>{thb(grandTotal)}</Text>
      </View>
      <View style={s.row}>
        <Text style={[s.line, s.vatLabel]} numberOfLines={1}>มูลค่าสินค้าก่อน Vat</Text>
        <Text style={[s.line, s.amount]} numberOfLines={1}>{thb(subtotalBeforeVat)}</Text>
      </View>
      <View style={s.row}>
        <Text style={[s.line, s.vatLabel]} numberOfLines={1}>คิดเป็นมูลค่าภาษี {sh.tax_percent}%</Text>
        <Text style={[s.line, s.amount]} numberOfLines={1}>{thb(vat)}</Text>
      </View>
      <View style={[s.row, { marginTop: 4, alignItems: "center" }]}>
        <Text style={[s.line, { flex: 1 }]} numberOfLines={1}>จำนวน {itemCount} ชิ้น</Text>
        <Text style={s.line} numberOfLines={1}>รวมมูลค่า</Text>
        <Text style={[s.grandTotal, s.amount]} numberOfLines={1}>{thb(grandTotal)}</Text>
      </View>

      <Dash />

      {/* ─── Payment ──────────────────────────────────────────────── */}
      <Text style={s.line}>การชำระเงิน</Text>
      <View style={s.row}>
        <Text style={[s.line, { flex: 1 }]} numberOfLines={1}>{order.payment_method || "Cash"}</Text>
        <Text style={[s.line, s.amount]} numberOfLines={1}>{thb(paid)}</Text>
      </View>
      {change > 0 && (
        <View style={s.row}>
          <Text style={[s.line, { flex: 1 }]} numberOfLines={1}>เงินทอน</Text>
          <Text style={[s.line, s.amount]} numberOfLines={1}>{thb(change)}</Text>
        </View>
      )}

      {/* ─── QR code ──────────────────────────────────────────────── */}
      <View style={s.qrSection}>
        <QrCode value={`${QR_BASE_URL}/${order.order_number || ""}/`} targetSize={180} />
        <Text style={s.qrCaption}>สแกนเพื่อดูใบเสร็จ / Scan to view receipt</Text>
      </View>

      {/* ─── Footer ───────────────────────────────────────────────── */}
      <Text style={s.smallCenter}>ราคาสินค้ารวมภาษีมูลค่าเพิ่มแล้ว</Text>
      <Text style={s.thankYou}>THANK YOU FOR YOUR SHOPPING</Text>
      <Text style={s.centerText}>Tel. {sh.phone}</Text>
      <Text style={s.poweredBy}>Powered by Brave POS</Text>
    </View>
  );
});

ReceiptImage.displayName = "ReceiptImage";

const s = StyleSheet.create({
  root: {
    width: RECEIPT_WIDTH,
    backgroundColor: "#FFFFFF",
    paddingVertical: 24,
    // Asymmetric horizontal padding.  Empirically the printer's effective
    // right print boundary lands around dot ~420 of the 576-dot image:
    // paddingRight:130 still clipped "Admin"→"Adm", "299.00"→"299.0".
    // 170 gives a generous safety zone so longer dynamic values (bigger
    // amounts, longer staff names, etc.) still print intact.
    paddingLeft: 20,
    paddingRight: 170,
  },
  voidBanner: {
    borderWidth: 2,
    borderColor: "#000000",
    paddingVertical: 6,
    marginBottom: 12,
    alignItems: "center",
  },
  voidBannerText: {
    fontFamily: BOLD,
    fontSize: 26,
    color: "#000000",
    textAlign: "center",
  },
  voidBannerSub: {
    fontFamily: FONT,
    fontSize: 15,
    color: "#000000",
    textAlign: "center",
    marginTop: 2,
  },
  queue: {
    fontFamily: BOLD,
    fontSize: 40,
    color: "#000000",
    textAlign: "center",
    marginTop: 6,
    marginBottom: 16,
  },
  shopTitle: {
    fontFamily: BOLD,
    fontSize: 22,
    color: "#000000",
    textAlign: "center",
    marginBottom: 10,
  },
  // Brand logo printed in place of the shop-name text.  alignSelf:
  // center + a width smaller than the receipt content area keeps the
  // logo centered on the printed strip and away from the right-edge
  // clip zone.  Source is 100x100 — scaling up to 140 keeps the
  // dithered thermal output readable without going blurry.
  logo: {
    alignSelf: "center",
    width: 140,
    height: 140,
    marginTop: 4,
    marginBottom: 10,
  },
  branchLine: {
    fontFamily: FONT,
    fontSize: 16,
    color: "#000000",
  },
  companyCenter: {
    fontFamily: FONT,
    fontSize: 15,
    color: "#000000",
    textAlign: "center",
    marginTop: 2,
  },
  receiptType: {
    fontFamily: BOLD,
    fontSize: 16,
    color: "#000000",
    textAlign: "center",
    marginTop: 12,
    marginBottom: 4,
  },
  dash: {
    height: 1,
    borderStyle: "dashed",
    borderBottomWidth: 1,
    borderColor: "#000000",
    marginVertical: 8,
  },
  line: {
    fontFamily: FONT,
    fontSize: 16,
    color: "#000000",
  },
  bold: { fontFamily: BOLD },
  row: {
    flexDirection: "row",
    alignItems: "flex-start",
    justifyContent: "space-between",
    marginVertical: 1,
  },
  // Right-aligned numeric column.  Fixed minWidth keeps prices in a
  // tidy vertical strip; textAlign: right anchors them to the right
  // gutter so the printer can never wrap or clip a price.  Width tuned
  // down to 90 so amounts don't push the column past the safe zone.
  amount: {
    minWidth: 90,
    textAlign: "right",
    marginLeft: 8,
  },
  // Staff label/value occupies whatever's left after POS # on the
  // same row.  flexShrink + textAlign:right means a long staff name
  // gets ellipsised rather than pushed off the right edge.
  staffName: {
    flexShrink: 1,
    textAlign: "right",
    marginLeft: 8,
  },
  // Item description.  flex:1 fills the left column; flexShrink
  // guarantees the amount column always gets its 90px even when the
  // product name is huge (the name wraps to additional lines).
  itemName: {
    flex: 1,
    flexShrink: 1,
  },
  // Left-side VAT label.  flex: 1 + numberOfLines: 1 prevents the long
  // Thai labels (e.g. "มูลค่าสินค้าก่อน Vat") from pushing the amount
  // off the right edge.
  vatLabel: {
    flex: 1,
    textAlign: "right",
    marginRight: 8,
  },
  grandTotal: {
    fontFamily: BOLD,
    fontSize: 24,
    color: "#000000",
  },
  smallCenter: {
    fontFamily: FONT,
    fontSize: 13,
    color: "#000000",
    textAlign: "center",
    marginTop: 4,
  },
  thankYou: {
    fontFamily: BOLD,
    fontSize: 16,
    color: "#000000",
    textAlign: "center",
    marginTop: 6,
  },
  centerText: {
    fontFamily: FONT,
    fontSize: 14,
    color: "#000000",
    textAlign: "center",
    marginTop: 2,
  },
  poweredBy: {
    fontFamily: FONT,
    fontSize: 12,
    color: "#555555",
    textAlign: "center",
    marginTop: 8,
  },
  qrSection: {
    alignItems: "center",
    marginTop: 12,
    marginBottom: 8,
  },
  qrCaption: {
    fontFamily: FONT,
    fontSize: 12,
    color: "#000000",
    textAlign: "center",
    marginTop: 6,
  },
});
