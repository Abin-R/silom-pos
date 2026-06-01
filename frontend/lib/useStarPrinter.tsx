// Hook wrapping the receipt-printing flow.
//
// We render the receipt as a 576-px-wide PNG via react-native-view-shot
// (Sarabun font for Thai support), then send the PNG to the native
// module's printImage().  This is the only way to get Thai text on the
// TM-T82X (firmware ANK doesn't have Thai font ROM, ESC/POS text would
// print `?` for every Thai glyph).
//
// captureRef.width is forced to 576 so the captured PNG is exactly that
// many *pixels* regardless of the device's pixel density.  Without that
// the image would be 1152 or 1728 pixels wide on a 2x/3x device, which
// is bigger than the printer's 576-dot print head — the right edge
// gets clipped.
//
// Auto-retry queue + reprint() are kept the same as before so power
// outages still result in receipts arriving when the printer returns.

import React, { useCallback, useEffect, useRef, useState } from "react";
import { View, NativeModules } from "react-native";
import { captureRef } from "react-native-view-shot";
import { ReceiptImage, RECEIPT_WIDTH } from "../components/ReceiptImage";
import type { PrinterConfig, ReceiptOrder, ReceiptShop } from "./starPrinter";
import { loadLocalPrinterConfig } from "./localPrinterConfig";
import * as queue from "./printerQueue";

const BravePosPrinter = (NativeModules as any).BravePosPrinter as {
  printImage(identifier: string, base64Png: string, widthDots: number): Promise<{ ok: boolean; error?: string }>;
};

type PendingPrint = {
  config: PrinterConfig;
  order: ReceiptOrder;
  shop: ReceiptShop;
  resolve: (r: { ok: true } | { ok: false; error: string }) => void;
  queueId?: string;
};

const QUEUE_POLL_MS = 30_000;
const LAYOUT_SETTLE_MS = 300;     // wait for Sarabun glyphs to shape

export function useStarPrinter() {
  const receiptRef = useRef<View>(null);
  const [pending, setPending] = useState<PendingPrint | null>(null);

  // ─── Capture + send when a print is pending ────────────────────────
  useEffect(() => {
    if (!pending) return;
    let cancelled = false;

    const run = async () => {
      await new Promise((r) => setTimeout(r, LAYOUT_SETTLE_MS));
      if (cancelled) return;

      let result: { ok: true } | { ok: false; error: string };

      try {
        if (!receiptRef.current) {
          result = { ok: false, error: "Receipt view ref not ready" };
        } else if (!BravePosPrinter || typeof BravePosPrinter.printImage !== "function") {
          result = { ok: false, error: "Native printImage method not linked (rebuild needed)" };
        } else {
          const base64 = await captureRef(receiptRef, {
            format: "png",
            quality: 1,
            result: "base64",
            // CRITICAL: forces the PNG to be exactly RECEIPT_WIDTH pixels
            // wide regardless of device density.  Without this the PNG is
            // device-pixel-wide → 1152px on 2x → printer clips right edge.
            width: RECEIPT_WIDTH,
          });
          if (cancelled) return;
          const res = await BravePosPrinter.printImage(
            pending.config.identifier,
            base64,
            RECEIPT_WIDTH,
          );
          result = res.ok ? { ok: true } : { ok: false, error: res.error || "Print failed" };
        }
      } catch (e: any) {
        result = { ok: false, error: e?.message || String(e) };
      }

      if (cancelled) return;

      // Queue persistence: success removes; first-time failure enqueues;
      // retry-from-queue failure bumps the attempts counter.
      try {
        if (result.ok && pending.queueId) {
          await queue.removeJob(pending.queueId);
        } else if (!result.ok && !pending.queueId) {
          await queue.enqueue(pending.config, pending.order, pending.shop, result.error);
        } else if (!result.ok && pending.queueId) {
          await queue.recordAttempt(pending.queueId, result.error);
        }
      } catch {/* AsyncStorage failures shouldn't block resolve */}

      pending.resolve(result);
      setPending(null);
    };

    run();
    return () => { cancelled = true; };
  }, [pending]);

  // ─── Auto-retry queue drainer ─────────────────────────────────────
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setInterval> | null = null;

    const tryDrain = async () => {
      if (stopped || pending) return;
      const job = await queue.nextDueJob(QUEUE_POLL_MS);
      if (!job || stopped) return;
      setPending({
        config: job.config,
        order: job.order,
        shop: job.shop,
        queueId: job.id,
        resolve: () => {/* queue path — caller already returned */},
      });
    };

    tryDrain();
    timer = setInterval(tryDrain, QUEUE_POLL_MS);
    return () => { stopped = true; if (timer) clearInterval(timer); };
  }, [pending]);

  // ─── Public API ────────────────────────────────────────────────────
  const printReceipt = useCallback(
    (config: PrinterConfig, order: ReceiptOrder, shop: ReceiptShop) =>
      new Promise<{ ok: true } | { ok: false; error: string }>((resolve) => {
        if (!config?.enabled) {
          resolve({ ok: false, error: "Printer is disabled" });
          return;
        }
        if (!config.identifier) {
          resolve({ ok: false, error: "No printer identifier configured" });
          return;
        }
        setPending({ config, order, shop, resolve });
      }),
    [],
  );

  const reprint = useCallback(
    async (order: ReceiptOrder, shop: ReceiptShop) => {
      const cfg = await loadLocalPrinterConfig();
      return printReceipt(cfg, order, shop);
    },
    [printReceipt],
  );

  // Hidden off-screen overlay where the receipt JSX is rendered for
  // view-shot to capture.  Position-absolute + off-screen + opacity 0
  // + pointerEvents=none → never visible, never receives touches.
  const ReceiptOverlay = useCallback(() => {
    if (!pending) return null;
    return (
      <View
        pointerEvents="none"
        style={{
          position: "absolute",
          left: -10000,
          top: 0,
          opacity: 0,
        }}
      >
        <ReceiptImage
          ref={receiptRef}
          order={pending.order}
          shop={pending.shop}
        />
      </View>
    );
  }, [pending]);

  return { printReceipt, reprint, ReceiptOverlay };
}
