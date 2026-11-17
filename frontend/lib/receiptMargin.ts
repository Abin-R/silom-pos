// Right margin of the printed slip, in the 576-px image space, chosen per
// platform at bundle time (same split as printerNative.ts).
//
// Android: the Epson TM-T82X's printable area ends near dot ~420 of the
// 576-dot image, so the slip keeps a 170-dot right gutter. These are exactly
// the values the receipt, shift summary and admin picker used before the split.
//
// iOS: Metro picks receiptMargin.ios.ts instead (Star TSP143IIIU).
export const DEFAULT_RECEIPT_RIGHT_PAD = 170;

// Admin "Receipt right margin" picker. `v` is stored in the local printer
// config as receiptRightPad; undefined = DEFAULT_RECEIPT_RIGHT_PAD.
// Shown in mm (180 dpi → 1 dot ≈ 0.141 mm).
export const RIGHT_PAD_OPTIONS: { mm: number; v: number | undefined }[] = [
  { mm: 24, v: undefined },
  { mm: 30, v: 210 },
  { mm: 35, v: 250 },
  { mm: 41, v: 290 },
  { mm: 47, v: 330 },
];
