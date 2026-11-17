// iPad slip layout (Star TSP143IIIU). Android keeps its values in
// receiptMargin.ts.
//
// The Star prints the whole 576-dot image (72 mm, 203 dpi). On iOS
// view-shot ignores captureRef's `width` (it only resizes when a height is
// given too), so the PNG is the view's layout size × the iPad's pixel ratio
// and the Star scales it to the 576 dots printImage() asks for. The print size
// is therefore set by the layout width alone: laid out at the full 576 the
// 15-16 px body text came out under 2 mm tall.
//
// 426 = 20 left + 386 content + 20 right. 386 is the content width the
// Android slip has (576 − 20 − 170), so every line wraps exactly as it does
// on the tills, and the Star enlarges it all 576/426 ≈ 1.35× to fill the paper.
export const RECEIPT_LAYOUT_WIDTH = 426;

export const DEFAULT_RECEIPT_RIGHT_PAD = 20;

// Picker values are layout px; 1 px ≈ 0.169 mm on paper (576/426 dots × 0.125 mm).
export const RIGHT_PAD_OPTIONS: { mm: number; v: number | undefined }[] = [
  { mm: 3, v: undefined },
  { mm: 7, v: 40 },
  { mm: 10, v: 60 },
  { mm: 17, v: 100 },
];
