// iPad: the Star TSP143IIIU prints the whole 576-dot image (72 mm), so the
// Epson's 170-dot right gutter only left a blank ~24 mm strip down the right
// of every slip. The right margin matches the 20-dot left one instead.
// Android keeps its values in receiptMargin.ts.
export const DEFAULT_RECEIPT_RIGHT_PAD = 20;

// 203 dpi → 1 dot = 0.125 mm.
export const RIGHT_PAD_OPTIONS: { mm: number; v: number | undefined }[] = [
  { mm: 3, v: undefined },
  { mm: 8, v: 60 },
  { mm: 13, v: 100 },
  { mm: 21, v: 170 },
];
