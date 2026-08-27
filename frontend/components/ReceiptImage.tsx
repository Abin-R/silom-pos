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
  // No pos_id default.  It used to be BIO HOUSE's real RD machine number, and
  // because the merge below used `||`, every branch with a blank POS ID fell
  // through to it and printed BIO HOUSE's number as its own.
  pos_number: "001",
  tax_percent: 7,
};

const thb = (n: number) =>
  (Number(n) || 0).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

const Dash = () => <View style={s.dash} />;

// One acknowledgement block on a full tax invoice: blank space to sign in, the
// signature rule, then a hand-written date and who is signing.  Matches the
// two blocks on the reference printout.
const SignatureBlock = ({ role }: { role: string }) => (
  <View style={s.sigBlock}>
    <View style={s.sigLine} />
    <Text style={s.sigText}>วันที่ / Date ___/___/___</Text>
    <Text style={s.sigText}>{role}</Text>
  </View>
);

type Props = {
  order: ReceiptOrder;
  shop?: ReceiptShop;
  // Fires once the bundled logo PNG has decoded and rendered.  The
  // print hook gates captureRef on this — without it, view-shot can
  // snapshot the view before the image is ready, producing a receipt
  // with a blank space where the logo should be.
  onLogoReady?: () => void;
  // Right padding of the whole slip, in the 576px image space.  Some printers
  // clip the right edge (their printable area is narrower than the 576-dot
  // head), so this pushes the content further left until it fits.  Per device,
  // from the local printer config; default 170 leaves already-fine printers
  // (e.g. biohouse) unchanged.
  rightPad?: number;
};

export const ReceiptImage = forwardRef<View, Props>(({ order, shop, onLogoReady, rightPad }, ref) => {
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
    // Per branch, so it comes off the order — the shop-wide Settings value
    // would put one branch's registered machine number on every branch's
    // receipts.  Blank (branch has no RD number yet) omits the line below.
    pos_id: order.branch_pos_id || "",
    pos_number: shop?.pos_number || SHOP_DEFAULTS.pos_number,
    tax_percent: Number(shop?.tax_percent ?? SHOP_DEFAULTS.tax_percent),
  };

  // Full tax invoice (ใบกำกับภาษีเต็มรูป) vs the everyday abbreviated slip.
  // Only trust the flag when buyer particulars actually came with it — a
  // "full" invoice with no named buyer is not a valid tax invoice, so fall
  // back to the abbreviated layout rather than printing an unusable document.
  const party = order.tax_invoice;
  const isFull = order.doc_type === "full" && !!party;

  // The abbreviated slip stays Thai-only (unchanged for every existing call
  // site); the full tax invoice prints "Thai / English" so the buyer's
  // accounting team can read it either way.
  const lbl = (th: string, en: string) => (isFull ? `${th} / ${en}` : th);
  const vatLines = isFull ? 2 : 1;

  const queue =
    order.queue_number !== undefined
      ? String(order.queue_number)
      : (order.order_number || "").slice(-2).replace(/^0+/, "") || "1";

  const grandTotal = Number(order.total) || 0;
  const taxRate = sh.tax_percent / 100;
  const subtotalBeforeVat = taxRate > 0 ? grandTotal / (1 + taxRate) : grandTotal;
  const vat = grandTotal - subtotalBeforeVat;
  // Discount: prefer the value the caller sent, but fall back to
  // (gross item subtotal − net total) if the caller forgot to pass
  // discount_amount.  This keeps old call sites working.
  const itemsGross = order.items.reduce(
    (sum, it) => sum + (Number(it.price) || 0) * (Number(it.qty) || 0),
    0,
  );
  const grossSubtotal = Number(order.subtotal) || itemsGross;
  const discountAmount =
    Number(order.discount_amount) || Math.max(0, grossSubtotal - grandTotal);
  const hasDiscount = discountAmount > 0;
  const itemCount = order.items.reduce((sum, it) => sum + (Number(it.qty) || 0), 0);
  const paid = Number(order.paid_amount ?? order.total) || 0;
  const change = Number(order.change) || 0;

  return (
    <View
      ref={ref}
      collapsable={false}
      style={[s.root, typeof rightPad === "number" ? { paddingRight: rightPad } : null]}
    >
      {/* ─── Big queue number ─────────────────────────────────────── */}
      {/* Omitted on a full tax invoice: it's an accounting document issued
          after the fact, not the slip the customer is called by. */}
      {!isFull && <Text style={s.queue} numberOfLines={1}>คิวที่ {queue}</Text>}

      {/* ─── Shop logo (replaces the text title) ──────────────────── */}
      <Image
        source={LOGO}
        style={s.logo}
        resizeMode="contain"
        onLoadEnd={onLogoReady}
      />

      {/* ─── Branch + company info ────────────────────────────────── */}
      <Text style={s.branchLine} numberOfLines={1}>{lbl("สาขา", "Branch")}: {sh.branch}</Text>
      <Text style={s.companyCenter}>{sh.company}</Text>
      {sh.address.map((line, i) => (
        <Text key={i} style={s.companyCenter}>{line}</Text>
      ))}
      <Text style={s.companyCenter}>{sh.phone}</Text>
      <Text style={s.companyCenter}>{lbl("เลขประจำตัวผู้เสียภาษี", "Tax ID")}: {sh.tax_id}</Text>
      {/* The label always prints, so the document keeps the shape the Revenue
          Department expects. The value is this branch's own machine number, or
          blank while the branch is still waiting for one to be issued — never
          another branch's number standing in for it. */}
      <Text style={s.companyCenter}>POS ID: {sh.pos_id}</Text>

      {isFull ? (
        <>
          <Text style={s.receiptType}>ใบเสร็จรับเงิน / ใบกำกับภาษี</Text>
          <Text style={s.receiptTypeEn}>Receipt / Tax Invoice</Text>
        </>
      ) : (
        <Text style={s.receiptType}>ใบเสร็จรับเงิน/ใบกำกับภาษีอย่างย่อ</Text>
      )}

      {/* Void marker — same position/styling as the reference void copy.
          The only difference from a normal receipt is this single line. */}
      {order.voided && <Text style={s.voidMark}>ยกเลิกการขาย</Text>}

      <Dash />

      {/* ─── Buyer block (full tax invoice only) ──────────────────── */}
      {/* Field order follows the reference printout: heading, tax ID, name,
          then address.  ``numberOfLines`` is deliberately unset — a buyer's
          registered address is long and must never be truncated on a tax
          invoice, so it wraps as far as it needs to. */}
      {isFull && party && (
        <>
          <Text style={s.line}>ลูกค้า / Customer</Text>
          <Text style={s.line}>เลขประจำตัวผู้เสียภาษี / Tax ID: {party.tax_id}</Text>
          <Text style={s.line}>
            {party.name}
            {party.tax_branch ? ` (${party.tax_branch})` : ""}
          </Text>
          <Text style={s.line}>ที่อยู่ / Address: {party.address}</Text>
          {!!party.phone && <Text style={s.line}>โทร / Tel: {party.phone}</Text>}
          {!!party.email && <Text style={s.line}>อีเมล / Email: {party.email}</Text>}
          <Dash />
        </>
      )}

      {/* ─── Order metadata ───────────────────────────────────────── */}
      {/* "Order Ref:" is an abbreviated-slip artefact (always blank); a tax
          invoice is identified by its invoice number, so it's dropped there. */}
      {!isFull && <Text style={s.line}>Order Ref:</Text>}
      <Text style={s.line}>
        {isFull ? "วันที่ / Date: " : "วันที่: "}{order.created_at_local ?? ""}
      </Text>
      <Text style={s.line}>Invoice #: {order.order_number}</Text>
      <View style={s.row}>
        <Text style={s.line} numberOfLines={1}>POS #: {sh.pos_number}</Text>
        <Text style={[s.line, s.staffName]} numberOfLines={1}>
          {isFull ? "พนักงาน / Cashier: " : "ชื่อพนักงาน: "}{order.staff || ""}
        </Text>
      </View>

      <Dash />

      {/* ─── Items ────────────────────────────────────────────────── */}
      <View style={s.row}>
        <Text style={[s.line, s.bold, { flex: 1 }]} numberOfLines={1}>
          {isFull ? "Qty  รายละเอียด / Description" : "Qty  รายละเอียด"}
        </Text>
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
      {/* Bilingual labels are up to twice as long as the Thai alone, so they
          get a second line to wrap into rather than being ellipsised — a
          truncated VAT label on a tax invoice is not acceptable. */}
      {hasDiscount && (
        <>
          <View style={s.row}>
            <Text style={[s.line, s.vatLabel]} numberOfLines={vatLines}>{lbl("รวมยอดสินค้า", "Subtotal")}</Text>
            <Text style={[s.line, s.amount]} numberOfLines={1}>{thb(grossSubtotal)}</Text>
          </View>
          <View style={s.row}>
            <Text style={[s.line, s.vatLabel]} numberOfLines={vatLines}>{lbl("ส่วนลด", "Discount")}</Text>
            <Text style={[s.line, s.amount]} numberOfLines={1}>-{thb(discountAmount)}</Text>
          </View>
        </>
      )}
      <View style={s.row}>
        <Text style={[s.line, s.vatLabel]} numberOfLines={vatLines}>{lbl("รวมเป็นเงิน", "Total")}</Text>
        <Text style={[s.line, s.amount]} numberOfLines={1}>{thb(grandTotal)}</Text>
      </View>
      <View style={s.row}>
        <Text style={[s.line, s.vatLabel]} numberOfLines={vatLines}>{lbl("มูลค่าสินค้าก่อน Vat", "Ex-VAT")}</Text>
        <Text style={[s.line, s.amount]} numberOfLines={1}>{thb(subtotalBeforeVat)}</Text>
      </View>
      <View style={s.row}>
        <Text style={[s.line, s.vatLabel]} numberOfLines={vatLines}>
          {lbl(`คิดเป็นมูลค่าภาษี ${sh.tax_percent}%`, `VAT ${sh.tax_percent}%`)}
        </Text>
        <Text style={[s.line, s.amount]} numberOfLines={1}>{thb(vat)}</Text>
      </View>
      {isFull ? (
        // Three cells on one row (the abbreviated layout) can't hold the
        // bilingual count *and* the bilingual grand-total label, so the full
        // invoice splits them across two rows.
        <>
          <Text style={[s.line, { marginTop: 4 }]} numberOfLines={1}>
            จำนวน {itemCount} ชิ้น / {itemCount} item{itemCount === 1 ? "" : "s"}
          </Text>
          <View style={[s.row, { alignItems: "center" }]}>
            <Text style={[s.line, s.vatLabel]} numberOfLines={2}>รวมมูลค่า / Grand Total</Text>
            <Text style={[s.grandTotal, s.amount]} numberOfLines={1}>{thb(grandTotal)}</Text>
          </View>
        </>
      ) : (
        <View style={[s.row, { marginTop: 4, alignItems: "center" }]}>
          <Text style={[s.line, { flex: 1 }]} numberOfLines={1}>จำนวน {itemCount} ชิ้น</Text>
          <Text style={s.line} numberOfLines={1}>รวมมูลค่า</Text>
          <Text style={[s.grandTotal, s.amount]} numberOfLines={1}>{thb(grandTotal)}</Text>
        </View>
      )}

      <Dash />

      {/* ─── Payment ──────────────────────────────────────────────── */}
      <Text style={s.line}>{lbl("การชำระเงิน", "Payment")}</Text>
      <View style={s.row}>
        <Text style={[s.line, { flex: 1 }]} numberOfLines={1}>{order.payment_method || "Cash"}</Text>
        <Text style={[s.line, s.amount]} numberOfLines={1}>{thb(paid)}</Text>
      </View>
      {change > 0 && (
        <View style={s.row}>
          <Text style={[s.line, { flex: 1 }]} numberOfLines={1}>{lbl("เงินทอน", "Change")}</Text>
          <Text style={[s.line, s.amount]} numberOfLines={1}>{thb(change)}</Text>
        </View>
      )}

      {isFull ? (
        // ─── Signature blocks ───────────────────────────────────────
        // A full tax invoice doubles as the delivery/receipt document, so it
        // carries the two acknowledgement blocks from the reference printout.
        // No QR here: the QR lands on the customer page that *offers* to issue
        // a tax invoice, which is redundant once one has been issued.
        <>
          <Dash />
          <SignatureBlock role="ผู้รับสินค้า / Goods Received By" />
          <SignatureBlock role="ผู้รับเงิน / Payment Received By" />
        </>
      ) : (
        /* ─── QR code ────────────────────────────────────────────── */
        <View style={s.qrSection}>
          <QrCode value={`${QR_BASE_URL}/${order.order_number || ""}/`} targetSize={180} />
          <Text style={s.qrCaption}>สแกนเพื่อดูใบเสร็จ / Scan to view receipt</Text>
        </View>
      )}

      {/* ─── Footer ───────────────────────────────────────────────── */}
      <Text style={s.smallCenter}>
        {lbl("ราคาสินค้ารวมภาษีมูลค่าเพิ่มแล้ว", "Prices include VAT")}
      </Text>
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
  voidMark: {
    fontFamily: BOLD,
    fontSize: 20,
    color: "#000000",
    textAlign: "center",
    marginTop: 8,
    marginBottom: 2,
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
  // English half of the full tax invoice's title, sitting under the Thai one.
  receiptTypeEn: {
    fontFamily: BOLD,
    fontSize: 15,
    color: "#000000",
    textAlign: "center",
    marginBottom: 4,
  },
  dash: {
    height: 1,
    borderStyle: "dashed",
    borderBottomWidth: 1,
    borderColor: "#000000",
    marginVertical: 8,
  },
  // Acknowledgement block.  The 40px top margin is the space someone actually
  // signs in; the rule below it is what they sign on.  Width is a fraction of
  // the content column so the block reads as a signature field rather than a
  // section divider (which is what the full-width dashes are).
  sigBlock: {
    alignItems: "center",
    marginTop: 40,
  },
  sigLine: {
    width: 220,
    borderBottomWidth: 1,
    borderColor: "#000000",
    marginBottom: 4,
  },
  sigText: {
    fontFamily: FONT,
    fontSize: 14,
    color: "#000000",
    textAlign: "center",
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
