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
import { View, Text, StyleSheet } from "react-native";
import type { ReceiptOrder, ReceiptShop } from "../lib/starPrinter";

// 80mm paper @ 180 dpi = 576 dots printable width.  useStarPrinter
// passes the same constant to captureRef.width so the captured PNG is
// exactly this wide regardless of device density.
export const RECEIPT_WIDTH = 576;

const FONT = "Sarabun";
const BOLD = "Sarabun-Bold";

// Hardcoded company info matching the reference receipt.  Falls back
// gracefully — backend shop data overrides where present.
const SHOP_DEFAULTS = {
  shop_name: "The rolling pinn",
  branch: "Samyan",
  company: "บริษัท เบรฟ แบรนด์ จำกัด",
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

type Props = { order: ReceiptOrder; shop?: ReceiptShop };

export const ReceiptImage = forwardRef<View, Props>(({ order, shop }, ref) => {
  const sh = {
    shop_name: shop?.shop_name || SHOP_DEFAULTS.shop_name,
    branch: shop?.branch || SHOP_DEFAULTS.branch,
    company: SHOP_DEFAULTS.company,
    address: SHOP_DEFAULTS.address,
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
      {/* ─── Big queue number ─────────────────────────────────────── */}
      <Text style={s.queue}>คิวที่ {queue}</Text>

      {/* ─── Shop title ───────────────────────────────────────────── */}
      <Text style={s.shopTitle}>{sh.shop_name}</Text>

      {/* ─── Branch + company info ────────────────────────────────── */}
      <Text style={s.branchLine}>สาขา: {sh.branch}</Text>
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
        <Text style={s.line}>POS #: {sh.pos_number}</Text>
        <Text style={s.line}>ชื่อพนักงาน: {order.staff || ""}</Text>
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
            <Text style={[s.line, { flex: 1 }]}>{it.qty}    {it.name}</Text>
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
    // Asymmetric horizontal padding: extra right gutter so right-aligned
    // amounts stay inside the printer's effective printable area (the
    // TSP143IIIU clips the last ~30-50 dots in practice even with a 576-
    // dot image).  Without this, prices and the right portion of long
    // Thai VAT labels get cut off.
    paddingLeft: 20,
    paddingRight: 44,
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
  // gutter so the printer can never wrap or clip a price.
  amount: {
    minWidth: 110,
    textAlign: "right",
    marginLeft: 8,
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
});
