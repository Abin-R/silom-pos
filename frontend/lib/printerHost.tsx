// The one receipt printer for the whole app, mounted in the root layout.
//
// Every slip is drawn into a hidden view and photographed (see useStarPrinter).
// That view used to live inside each screen.  On Android, opening Stock or
// Orders pushes the admin screen on top and takes the Sale screen off the
// display, but self-orders keep printing from it: text still drew, the logo
// image never loaded, and slips came out with a blank where the logo goes.
// Mounted here, beside the navigator rather than inside a screen, the hidden
// view stays on the display whichever screen is open.
//
// One host also means one retry drainer, where /pos and /admin used to run one
// each against the same on-disk queue.

import React, { createContext, useContext, useMemo } from "react";
import { useStarPrinter } from "./useStarPrinter";

type Printer = Pick<
  ReturnType<typeof useStarPrinter>,
  "printReceipt" | "reprint" | "retryQueuedNow"
>;

const PrinterContext = createContext<Printer | null>(null);

export function PrinterHost({ children }: { children: React.ReactNode }) {
  const { printReceipt, reprint, retryQueuedNow, ReceiptOverlay } = useStarPrinter();
  // All three are stable callbacks, so screens don't re-render on every print.
  const value = useMemo(
    () => ({ printReceipt, reprint, retryQueuedNow }),
    [printReceipt, reprint, retryQueuedNow],
  );
  return (
    <PrinterContext.Provider value={value}>
      {children}
      <ReceiptOverlay />
    </PrinterContext.Provider>
  );
}

export function usePrinter(): Printer {
  const printer = useContext(PrinterContext);
  if (!printer) throw new Error("usePrinter must be used inside <PrinterHost>");
  return printer;
}
