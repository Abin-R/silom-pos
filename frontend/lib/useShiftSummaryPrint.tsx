// Hook that prints the close-shift summary slip (ใบสรุปปิดรอบการขาย).
//
// Same image pipeline as useStarPrinter (render Sarabun-Thai view → capture a
// 576-px PNG → native printImage()), but one-shot: there's no persistent retry
// queue here.  A failed summary print surfaces an error the admin can act on by
// reprinting from the shift History tab.

import { useCallback, useEffect, useRef, useState } from "react";
import { View, NativeModules } from "react-native";
import { captureRef } from "react-native-view-shot";
import { RECEIPT_WIDTH } from "../components/ReceiptImage";
import { ShiftSummaryImage, type ShiftSummary } from "../components/ShiftSummaryImage";
import type { ReceiptShop } from "./starPrinter";
import { loadLocalPrinterConfig } from "./localPrinterConfig";

const BravePosPrinter = (NativeModules as any).BravePosPrinter as {
  printImage(identifier: string, base64Png: string, widthDots: number): Promise<{ ok: boolean; error?: string }>;
};

const LAYOUT_SETTLE_MS = 300; // wait for Sarabun glyphs to shape before capture

type PrintResult = { ok: true } | { ok: false; error: string };

type Pending = {
  summary: ShiftSummary;
  shop?: ReceiptShop;
  printedAt: string;
  identifier: string;
  resolve: (r: PrintResult) => void;
};

export function useShiftSummaryPrint() {
  const ref = useRef<View>(null);
  const [pending, setPending] = useState<Pending | null>(null);

  useEffect(() => {
    if (!pending) return;
    let cancelled = false;

    (async () => {
      await new Promise((r) => setTimeout(r, LAYOUT_SETTLE_MS));
      if (cancelled) return;

      let result: PrintResult;
      try {
        if (!ref.current) {
          result = { ok: false, error: "Summary view ref not ready" };
        } else if (!BravePosPrinter || typeof BravePosPrinter.printImage !== "function") {
          result = { ok: false, error: "Native printImage method not linked (rebuild needed)" };
        } else {
          const base64 = await captureRef(ref, {
            format: "png",
            quality: 1,
            result: "base64",
            width: RECEIPT_WIDTH,
          });
          if (cancelled) return;
          const res = await BravePosPrinter.printImage(pending.identifier, base64, RECEIPT_WIDTH);
          result = res.ok ? { ok: true } : { ok: false, error: res.error || "Print failed" };
        }
      } catch (e: any) {
        result = { ok: false, error: e?.message || String(e) };
      }

      if (cancelled) return;
      pending.resolve(result);
      setPending(null);
    })();

    return () => { cancelled = true; };
  }, [pending]);

  const printShiftSummary = useCallback(
    async (summary: ShiftSummary, shop?: ReceiptShop): Promise<PrintResult> => {
      const cfg = await loadLocalPrinterConfig();
      if (!cfg?.enabled) return { ok: false, error: "Printer is disabled" };
      if (!cfg.identifier) return { ok: false, error: "No printer identifier configured" };
      // printedAt is stamped here (device clock) so the slip shows the real
      // print moment, mirroring the reference "เวลาพิมพ์".
      return new Promise<PrintResult>((resolve) => {
        setPending({ summary, shop, printedAt: new Date().toISOString(), identifier: cfg.identifier, resolve });
      });
    },
    [],
  );

  // Off-screen capture target — never visible, never receives touches.
  const ShiftSummaryOverlay = useCallback(() => {
    if (!pending) return null;
    return (
      <View pointerEvents="none" style={{ position: "absolute", left: -10000, top: 0, opacity: 0 }}>
        <ShiftSummaryImage
          ref={ref}
          summary={pending.summary}
          shop={pending.shop}
          printedAt={pending.printedAt}
        />
      </View>
    );
  }, [pending]);

  return { printShiftSummary, ShiftSummaryOverlay };
}
