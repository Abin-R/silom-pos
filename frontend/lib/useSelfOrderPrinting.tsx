// Auto-prints receipts for orders customers placed on their own phones.
//
// Printing is client-side — the Epson native module lives in this app, not on
// the server — so a self-order's slip can only come out of a tablet that asks
// for it. This hook is that asker: it polls the backend, and whatever it is
// handed, it prints.
//
// The backend decides who prints what. `GET /api/self-orders/pending` *claims*
// the rows it returns, so two tablets at one branch can both poll and only one
// is ever given a given order. Do not add client-side dedupe here and do not
// re-poll on failure hoping for a retry — printerQueue already owns retries,
// and a second physical receipt with the same queue number is a customer
// dispute at the counter. A missing one is recoverable via admin Reprint.
//
// Mount this ONCE, in pos.tsx only. admin.tsx also mounts useStarPrinter, and
// in an expo-router stack both screens can be mounted at the same time.

import { useEffect, useRef } from "react";
import { apiFetch } from "./api";
import { loadLocalPrinterConfig } from "./localPrinterConfig";
import type { PrinterConfig, ReceiptOrder, ReceiptShop } from "./starPrinter";

const POLL_MS = 10_000;

type PrintFn = (
  config: PrinterConfig,
  order: ReceiptOrder,
  shop: ReceiptShop,
) => Promise<{ ok: true } | { ok: false; error: string }>;

type ApiOrder = {
  order_number: string;
  queue_number?: number | null;
  items: Array<{ name: string; qty: number; price: number }>;
  subtotal?: number;
  discount_amount?: number;
  vat_amount?: number;
  processing_fee?: number;
  processing_fee_vat?: number;
  total: number;
  payment_method?: string;
  paid_amount?: number;
  created_at?: string;
  staff?: string;
};

function toReceiptOrder(o: ApiOrder): ReceiptOrder {
  return {
    order_number: o.order_number,
    // Server-assigned, per branch per day. Without this the receipt falls back
    // to slicing the global order_number, and the number on the paper wouldn't
    // match the one the customer was shown on their phone.
    queue_number: o.queue_number ?? undefined,
    items: (o.items || []).map((i) => ({
      name: i.name,
      qty: Number(i.qty) || 0,
      price: Number(i.price) || 0,
    })),
    subtotal: Number(o.subtotal) || 0,
    discount_amount: Number(o.discount_amount) || 0,
    vat_amount: Number(o.vat_amount) || 0,
    processing_fee: Number(o.processing_fee) || 0,
    processing_fee_vat: Number(o.processing_fee_vat) || 0,
    total: Number(o.total) || 0,
    payment_method: o.payment_method || "",
    paid_amount: Number(o.paid_amount) || 0,
    change: 0,                       // paid in full via the gateway
    created_at_local: o.created_at
      ? new Date(o.created_at).toLocaleString("en-GB")
      : new Date().toLocaleString("en-GB"),
    staff: o.staff || "Self Order",
  };
}

/**
 * @param printReceipt  from the screen's existing useStarPrinter() — do NOT
 *                      mount a second one, it would create a second hidden
 *                      overlay and receiptRef for view-shot to fight over.
 */
export function useSelfOrderPrinting(
  printReceipt: PrintFn,
  activeBranchName?: string,
  enabled: boolean = true,
) {
  // Keep the latest values without restarting the interval on every render.
  const printRef = useRef(printReceipt);
  const branchRef = useRef(activeBranchName);
  useEffect(() => { printRef.current = printReceipt; }, [printReceipt]);
  useEffect(() => { branchRef.current = activeBranchName; }, [activeBranchName]);

  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    let busy = false;

    const tick = async () => {
      // Skip if the previous tick is still printing — the poll claims rows, so
      // overlapping ticks would claim work we haven't printed yet.
      if (stopped || busy) return;
      busy = true;
      try {
        const cfg = await loadLocalPrinterConfig();
        if (!cfg.enabled || !cfg.identifier) return;  // no printer on this tablet

        const res = await apiFetch("/self-orders/pending");
        if (!res.ok || stopped) return;
        const orders: ApiOrder[] = await res.json();
        if (!orders?.length) return;

        const shopRes = await apiFetch("/settings");
        const shop = shopRes.ok ? await shopRes.json() : {};
        // Settings.branch is a shop-wide default ("Main"), not this tablet's
        // branch — same override the cashier's own print path does.
        if (branchRef.current) shop.branch = branchRef.current;

        for (const o of orders) {
          if (stopped) return;
          // Sequential on purpose. useStarPrinter queues these, but awaiting
          // each keeps the order deterministic and the log readable.
          const r = await printRef.current(cfg, toReceiptOrder(o), shop);
          if (!r.ok) {
            // Not dropped — useStarPrinter has persisted it to printerQueue
            // and will retry. Nothing to do but note it.
            console.warn(`self-order ${o.order_number} print queued:`, r.error);
          }
        }
      } catch (e) {
        console.warn("self-order poll failed", e);
      } finally {
        busy = false;
      }
    };

    tick();
    const timer = setInterval(tick, POLL_MS);
    return () => { stopped = true; clearInterval(timer); };
  }, [enabled]);
}
