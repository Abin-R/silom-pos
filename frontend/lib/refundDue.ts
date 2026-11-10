// Self-orders whose money reached Beam after the customer's 3 minutes ran out.
// No bill exists for them, so they live under Orders › Refund due rather than
// in the bill list, and the nav rail shows how many are waiting.
//
// One shared count for the whole app: /pos and /admin can both be mounted in
// the expo-router stack, and two rails polling separately would double the
// requests and could disagree for up to a minute.  Settling one calls
// refreshRefundDue() so the badge drops at once instead of on the next tick.

import { useEffect, useState } from "react";
import { apiFetch } from "./api";

const POLL_MS = 60_000;

export type LatePayment = {
  id: string;
  reference: string;
  total: number;
  payment_method: string;
  beam_charge_id: string;
  created_at: string;
  ended_at: string | null;
  items: { name: string; qty: number; is_free: boolean }[];
  status: string;
  resolution: string;
  resolved_by: string;
  order_number: string | null;
};

let count = 0;
let rows: LatePayment[] = [];
const listeners = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | null = null;
let inFlight: Promise<void> | null = null;

function emit() {
  listeners.forEach((fn) => fn());
}

export function refreshRefundDue(): Promise<void> {
  if (inFlight) return inFlight;
  inFlight = (async () => {
    try {
      const res = await apiFetch("/self-orders/refund-due");
      // An older server has no such endpoint: show nothing rather than an error.
      if (!res.ok) return;
      const data = await res.json();
      if (!Array.isArray(data)) return;
      rows = data;
      count = data.length;
      emit();
    } catch {
      // Offline or signed out — keep the last answer; the next tick retries.
    } finally {
      inFlight = null;
    }
  })();
  return inFlight;
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  if (!timer) {
    refreshRefundDue();
    timer = setInterval(refreshRefundDue, POLL_MS);
  }
  return () => {
    listeners.delete(fn);
    if (!listeners.size && timer) {
      clearInterval(timer);
      timer = null;
    }
  };
}

/** Late payments waiting at this branch, kept fresh while anything is mounted. */
export function useRefundDue(): { count: number; rows: LatePayment[] } {
  const [, force] = useState(0);
  useEffect(() => subscribe(() => force((n) => n + 1)), []);
  return { count, rows };
}
