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
//
// Prints are SERIALISED through a FIFO job list.  This used to be a single
// `pending` slot, which meant a second printReceipt() while one was in
// flight silently overwrote the first: the clobbered job was never printed,
// never enqueued for retry, and its promise never resolved.  Rare when the
// only caller was a cashier tapping Pay, but self-ordering auto-prints on a
// timer and can hand us two receipts at once — so the slot had to become a
// queue.  Only the head of the list is captured at any time; there is one
// hidden overlay and one receiptRef, so concurrent captures are not possible.

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

  // FIFO of prints awaiting capture.  jobs[0] is the one being worked on.
  // Appending never changes jobs[0]'s identity, so enqueuing a new print
  // can't restart the in-flight capture effect.
  const [jobs, setJobs] = useState<PendingPrint[]>([]);
  const active = jobs[0] ?? null;

  // Mirror of `jobs` for the drainer, which must read the current length
  // from inside a long-lived interval without re-creating that interval
  // on every job change.
  const jobsRef = useRef<PendingPrint[]>([]);
  useEffect(() => { jobsRef.current = jobs; }, [jobs]);

  // Promise that resolves when the brand logo inside ReceiptImage finishes
  // decoding.  We reset it per-print so each capture waits on its own load
  // signal — view-shot will otherwise snapshot before the bundled PNG is
  // ready and the receipt prints with a blank space where the logo should
  // be.  Falls back to a 2-second timeout in case onLoadEnd never fires
  // (e.g. cached image, or RN platform quirk) so prints can't hang.
  const logoReadyRef = useRef<{ promise: Promise<void>; resolve: () => void } | null>(null);

  // ─── Capture + send the job at the head of the queue ───────────────
  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    let settled = false;

    // Fresh logo-ready promise for this print.  Resolved by the Image's
    // onLoadEnd callback inside ReceiptOverlay below.
    let resolveLogo: () => void = () => {};
    logoReadyRef.current = {
      promise: new Promise<void>((res) => { resolveLogo = res; }),
      resolve: () => resolveLogo(),
    };

    const run = async () => {
      // Wait for both: layout settle (Sarabun glyph shaping) AND logo
      // decode.  Race the logo wait against a 2s timeout as a safety net.
      await new Promise((r) => setTimeout(r, LAYOUT_SETTLE_MS));
      if (cancelled) return;
      await Promise.race([
        logoReadyRef.current?.promise ?? Promise.resolve(),
        new Promise<void>((r) => setTimeout(r, 2000)),
      ]);
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
            // CRITICAL: force the PNG to be exactly RECEIPT_WIDTH pixels wide
            // regardless of device density (otherwise it's device-pixel-wide →
            // e.g. 1152px on a 2x screen → clips).  Right-edge clipping on
            // narrow printers is handled by the receipt's rightPad, not here —
            // this width option scales inconsistently across devices.
            width: RECEIPT_WIDTH,
          });
          if (cancelled) return;
          const res = await BravePosPrinter.printImage(
            active.config.identifier,
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
        if (result.ok && active.queueId) {
          await queue.removeJob(active.queueId);
        } else if (!result.ok && !active.queueId) {
          await queue.enqueue(active.config, active.order, active.shop, result.error);
        } else if (!result.ok && active.queueId) {
          await queue.recordAttempt(active.queueId, result.error);
        }
      } catch {/* AsyncStorage failures shouldn't block resolve */}

      // Release the in-flight claim so another drainer can retry on the
      // next tick (failure path) or so the slot is freed up for cleanup
      // (success path — removeJob already cleared it but this is safe).
      settled = true;
      if (active.queueId) queue.release(active.queueId);

      active.resolve(result);
      setJobs((prev) => prev.slice(1));   // pop the head; the next job runs
    };

    run();

    return () => {
      cancelled = true;
      // Tearing down before the job settled (screen unmounted mid-capture).
      // Hand the claim back, or printerQueue's module-level inFlight Set
      // holds it forever and nextDueJob skips the job until app restart.
      // The AsyncStorage record survives, so it retries on the next mount.
      if (!settled && active.queueId) queue.release(active.queueId);
    };
  }, [active]);

  // ─── Auto-retry queue drainer ─────────────────────────────────────
  // Only pulls from the persisted retry queue when nothing is already in
  // flight, so live prints (a cashier tapping Pay) always take priority.
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setInterval> | null = null;

    const tryDrain = async () => {
      if (stopped || jobsRef.current.length > 0) return;
      const job = await queue.nextDueJob(QUEUE_POLL_MS);
      if (!job) return;
      // nextDueJob has already *claimed* the job in the inFlight Set.  If we
      // bail now without releasing it, that claim leaks for the process
      // lifetime and the receipt never retries.
      if (stopped) { queue.release(job.id); return; }
      setJobs((prev) => [...prev, {
        config: job.config,
        order: job.order,
        shop: job.shop,
        queueId: job.id,
        resolve: () => {/* queue path — caller already returned */},
      }]);
    };

    tryDrain();
    timer = setInterval(tryDrain, QUEUE_POLL_MS);
    return () => { stopped = true; if (timer) clearInterval(timer); };
  }, []);

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
        // Append — never overwrite.  Two receipts handed to us at once must
        // both print.
        setJobs((prev) => [...prev, { config, order, shop, resolve }]);
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
    if (!active) return null;
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
          // Remount per job so a queued receipt never captures the previous
          // one's laid-out view (and so onLogoReady fires again for each).
          // doc_type is part of the key because the same order can be printed
          // twice with different layouts (abbreviated slip, then full tax
          // invoice) — without it React reuses the mounted tree and view-shot
          // can capture the previous document's layout.
          key={`${active.order.order_number ?? active.queueId}:${active.order.doc_type ?? "abbreviated"}`}
          ref={receiptRef}
          order={active.order}
          shop={active.shop}
          rightPad={active.config.receiptRightPad}
          onLogoReady={() => logoReadyRef.current?.resolve()}
        />
      </View>
    );
  }, [active]);

  return { printReceipt, reprint, ReceiptOverlay };
}
