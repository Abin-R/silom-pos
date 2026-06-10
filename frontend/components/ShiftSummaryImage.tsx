// Close-shift summary (ใบสรุปปิดรอบการขาย) rendered as a React Native view,
// captured to a 576-px PNG by react-native-view-shot and sent to the printer
// via printImage() — the same Thai-capable image path the order receipt uses
// (the TM-T82X firmware is ANK-only, so Thai text bytes print as `?`).
//
// Mirrors the reference SilomPOS close-shift slip the user provided.

import React, { forwardRef } from "react";
import { View, Text, StyleSheet } from "react-native";
import { RECEIPT_WIDTH } from "./ReceiptImage";
import type { ReceiptShop } from "../lib/starPrinter";

const FONT = "Sarabun";
const BOLD = "Sarabun-Bold";

export type ShiftMovementLine = { category?: string; note?: string; amount: number };
export type ShiftPaymentLine = { method: string; amount: number; count?: number };

// Mirrors the backend `_shift_summary` payload.
export type ShiftSummary = {
  round_number: number;
  opened_at?: string | null;
  opened_by?: string;
  closed_at?: string | null;
  closed_by?: string;
  invoice_first?: string;
  invoice_last?: string;
  bill_count: number;
  start_cash: number;
  cash_sales: number;
  paid_in: number;
  paid_out: number;
  actual_in_drawer: number;
  expected_in_drawer: number;
  difference: number;
  paid_in_items: ShiftMovementLine[];
  paid_out_items: ShiftMovementLine[];
  payments: ShiftPaymentLine[];
  sales_total: number;
  discount_total: number;
  cancelled_total: number;
  cancelled_count: number;
};

const thb = (n: number) =>
  (Number(n) || 0).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

const pad = (n: number) => String(n).padStart(2, "0");

// "3/06/2569 22:17:51" — Thai Buddhist year (+543), matching the reference slip.
function fmtDT(iso?: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "-";
  const date = `${d.getDate()}/${pad(d.getMonth() + 1)}/${d.getFullYear() + 543}`;
  return `${date} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// Friendly Thai labels for the payment methods our POS stores.
const METHOD_LABELS: Record<string, string> = {
  Cash: "เงินสด",
  "Easy Pay": "Easy Pay",
  Credit: "บัตรเครดิต/เดบิต",
  Beam: "Beam",
  "Beam QR": "Beam",
  PromptPay: "พร้อมเพย์",
  "QR Kbank": "QR Kbank",
  EDC: "บัตรเครดิต/เดบิต",
};
const methodLabel = (m: string) => METHOD_LABELS[m] || m || "เงินสด";

const Dash = () => <View style={s.dash} />;
const SectionTitle = ({ children }: { children: string }) => (
  <Text style={s.sectionTitle}>{children}</Text>
);
const Row = ({ label, value, bold }: { label: string; value: string; bold?: boolean }) => (
  <View style={s.row}>
    <Text style={[s.line, bold && s.bold, { flex: 1 }]} numberOfLines={1}>{label}</Text>
    <Text style={[s.line, bold && s.bold, s.amount]} numberOfLines={1}>{value}</Text>
  </View>
);

type Props = {
  summary: ShiftSummary;
  shop?: ReceiptShop;
  printedAt?: string; // ISO; defaults handled by the caller
};

export const ShiftSummaryImage = forwardRef<View, Props>(({ summary, shop, printedAt }, ref) => {
  const taxPercent = Number(shop?.tax_percent ?? 7);
  const posNumber = shop?.pos_number || "001";

  const total = Number(summary.sales_total) || 0;
  const rate = taxPercent / 100;
  const preVat = rate > 0 ? total / (1 + rate) : total;
  const vat = total - preVat;

  const paymentsTotal = summary.payments.reduce((sum, p) => sum + (Number(p.amount) || 0), 0);

  const movementLines = (items: ShiftMovementLine[]) =>
    items.length === 0 ? (
      <Text style={s.noItems}>ไม่มีรายการ</Text>
    ) : (
      items.map((m, i) => (
        <Row key={i} label={m.category || m.note || "-"} value={thb(m.amount)} />
      ))
    );

  return (
    <View ref={ref} collapsable={false} style={s.root}>
      <Text style={s.title}>ใบสรุปปิดรอบการขาย</Text>

      {/* ─── Header meta ─────────────────────────────────────────── */}
      <Text style={s.line}>รอบที่ {summary.round_number}</Text>
      <Text style={s.line}>เวลาเปิดรอบ {fmtDT(summary.opened_at)}</Text>
      <Text style={s.line}>เปิดรอบขายโดย {summary.opened_by || "-"}</Text>
      <Text style={s.line}>เวลาปิดรอบ {fmtDT(summary.closed_at)}</Text>
      <Text style={s.line}>ปิดรอบขายโดย {summary.closed_by || "-"}</Text>
      <Text style={s.line}>POS #: {posNumber}</Text>
      <Text style={s.line}>เลขที่ใบกำกับ: {summary.invoice_first || "-"}-{summary.invoice_last || "-"}</Text>
      <Text style={s.line}>จำนวนบิล {summary.bill_count}</Text>

      {/* ─── Cash drawer ─────────────────────────────────────────── */}
      <Dash />
      <SectionTitle>ลิ้นชักเก็บเงิน</SectionTitle>
      <Dash />
      <Row label="เงินทอนเริ่มต้น" value={thb(summary.start_cash)} />
      <Row label="ยอดขายด้วยเงินสด" value={thb(summary.cash_sales)} />
      <Row label="เงินสดเข้า" value={thb(summary.paid_in)} />
      <Row label="เงินสดออก" value={thb(summary.paid_out)} />
      <Row label="จำนวนเงินที่นับได้ในลิ้นชัก" value={thb(summary.actual_in_drawer)} />
      <Row label="จำนวนเงินที่ควรมีในลิ้นชัก" value={thb(summary.expected_in_drawer)} />
      <Row label="ส่วนต่าง" value={thb(summary.difference)} bold />

      {/* ─── Paid-in items ───────────────────────────────────────── */}
      <Dash />
      <SectionTitle>รายการเงินเข้า</SectionTitle>
      <Dash />
      {movementLines(summary.paid_in_items)}

      {/* ─── Paid-out items ──────────────────────────────────────── */}
      <Dash />
      <SectionTitle>รายการเงินออก</SectionTitle>
      <Dash />
      {movementLines(summary.paid_out_items)}

      {/* ─── Payment summary ─────────────────────────────────────── */}
      <Dash />
      <SectionTitle>สรุปการชำระเงิน</SectionTitle>
      <Dash />
      {summary.payments.length === 0 ? (
        <Row label="เงินสด" value={thb(0)} />
      ) : (
        summary.payments.map((p, i) => (
          <Row key={i} label={methodLabel(p.method)} value={thb(p.amount)} />
        ))
      )}
      <Row label="รวม" value={thb(paymentsTotal)} bold />

      {/* ─── Sales summary ───────────────────────────────────────── */}
      <Dash />
      <SectionTitle>สรุปการขาย</SectionTitle>
      <Dash />
      <Row label="สินค้ามีภาษี" value={thb(total)} />
      <Row label="สินค้าไม่มีภาษี" value={thb(0)} />
      <Row label="มูลค่าสินค้าก่อน Vat" value={thb(preVat)} />
      <Row label={`คิดเป็นมูลค่าภาษี ${taxPercent}%`} value={thb(vat)} />
      <Row label="ปัดเศษ" value={thb(0)} />
      <Row label="รวมมูลค่า" value={thb(total)} bold />
      <Row label="จำนวนบิล" value={String(summary.bill_count)} />

      {/* ─── Cancelled ───────────────────────────────────────────── */}
      <Dash />
      <SectionTitle>ยกเลิกการขาย</SectionTitle>
      <Dash />
      <Row label="รวมมูลค่า" value={thb(summary.cancelled_total)} />
      <Row label="จำนวนบิล" value={String(summary.cancelled_count)} />

      {/* ─── Discounts ───────────────────────────────────────────── */}
      <Dash />
      <SectionTitle>ส่วนลด</SectionTitle>
      <Dash />
      <Row label="ส่วนลด(ไม่ได้ใช้คูปอง)" value={thb(summary.discount_total)} />
      <Row label="ส่วนลด(แลกใช้คูปอง)" value={thb(0)} />
      <Row label="แคมเปญ" value={thb(0)} />

      <Text style={s.printedAt}>เวลาพิมพ์: {fmtDT(printedAt)}</Text>
    </View>
  );
});

ShiftSummaryImage.displayName = "ShiftSummaryImage";

const s = StyleSheet.create({
  root: {
    width: RECEIPT_WIDTH,
    backgroundColor: "#FFFFFF",
    paddingVertical: 24,
    // Same asymmetric padding as ReceiptImage: the printer's effective right
    // boundary lands well before dot 576, so the amount column needs a wide
    // right gutter to avoid clipping.
    paddingLeft: 20,
    paddingRight: 170,
  },
  title: {
    fontFamily: BOLD,
    fontSize: 22,
    color: "#000000",
    textAlign: "center",
    marginBottom: 16,
  },
  sectionTitle: {
    fontFamily: FONT,
    fontSize: 16,
    color: "#000000",
    textAlign: "center",
  },
  dash: {
    height: 1,
    borderStyle: "dashed",
    borderBottomWidth: 1,
    borderColor: "#000000",
    marginVertical: 8,
  },
  line: { fontFamily: FONT, fontSize: 16, color: "#000000" },
  bold: { fontFamily: BOLD },
  row: {
    flexDirection: "row",
    alignItems: "flex-start",
    justifyContent: "space-between",
    marginVertical: 1,
  },
  amount: {
    minWidth: 90,
    textAlign: "right",
    marginLeft: 8,
  },
  noItems: { fontFamily: FONT, fontSize: 16, color: "#000000" },
  printedAt: {
    fontFamily: FONT,
    fontSize: 13,
    color: "#000000",
    textAlign: "center",
    marginTop: 16,
  },
});
