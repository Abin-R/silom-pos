// Live online/offline indicator for the configured printer.
//
// Pings the printer's TCP socket every POLL_MS and exposes a tri-state
// status: null (no printer configured yet / first ping still in flight),
// "online", or "offline".  Used by the Printer card in admin.tsx to
// colour the status pill in real time so cashiers know if the printer
// is reachable BEFORE they take an order — rather than discovering it's
// unreachable at the moment of trying to print.
//
// Implementation: the native `ping(identifier)` method opens a raw TCP
// socket with a 2s timeout, so a single ping is fast and lightweight.
// We poll every 30s while the hook is mounted.

import { useEffect, useRef, useState } from "react";
import { NativeModules, AppState } from "react-native";

const BravePosPrinter = (NativeModules as any).BravePosPrinter as {
  ping(identifier: string): Promise<{ online: boolean; latencyMs?: number; error?: string }>;
};

const POLL_MS = 30_000;       // foreground poll cadence
const FIRST_POLL_MS = 800;    // tiny delay before first ping (UI mount settle)

export type PrinterStatus = {
  online: boolean | null;     // null = unknown (no config OR first ping not done)
  latencyMs: number | null;   // round-trip ms of the most recent successful ping
  lastError: string | null;
  lastCheckedAt: number;      // ms epoch, 0 = never
};

export function usePrinterStatus(identifier: string | null | undefined): PrinterStatus {
  const [state, setState] = useState<PrinterStatus>({
    online: null,
    latencyMs: null,
    lastError: null,
    lastCheckedAt: 0,
  });
  // Ref-based stop flag so the in-flight promise doesn't update unmounted
  // state when the identifier changes or the screen is dismissed.
  const stoppedRef = useRef(false);

  useEffect(() => {
    stoppedRef.current = false;

    // No printer configured → nothing to ping; show as unknown.
    if (!identifier) {
      setState({ online: null, latencyMs: null, lastError: null, lastCheckedAt: 0 });
      return;
    }

    if (!BravePosPrinter || typeof BravePosPrinter.ping !== "function") {
      // Native module out of date (e.g. running pre-ping build via OTA).
      // Don't spam — just mark unknown and stop.
      setState({
        online: null,
        latencyMs: null,
        lastError: "Native ping method not available (rebuild needed)",
        lastCheckedAt: 0,
      });
      return;
    }

    // Detect MAC-based TCP identifiers (e.g. "TCP:50:57:9C:09:A1:D3").
    // The Epson SDK resolves these to a real IP via ARP at connect time,
    // but a raw socket can't ping a MAC — it would always fail.  Skip
    // the native ping and report optimistic online; the actual print
    // attempt is what tells us for certain.  IPv4-style identifiers
    // ("TCP:192.168.1.62" or "TCP:192.168.1.62:9100") still go through
    // the normal socket ping below.
    const macRegex = /^TCP:[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$/;
    if (macRegex.test(identifier)) {
      setState({
        online: true,
        latencyMs: null,
        lastError: null,
        lastCheckedAt: Date.now(),
      });
      return;  // no interval — MAC identifier is always optimistic
    }

    const tick = async () => {
      if (stoppedRef.current) return;
      try {
        const r = await BravePosPrinter.ping(identifier);
        if (stoppedRef.current) return;
        setState({
          online: !!r.online,
          latencyMs: r.latencyMs ?? null,
          lastError: r.error ?? null,
          lastCheckedAt: Date.now(),
        });
      } catch (e: any) {
        if (stoppedRef.current) return;
        setState((prev) => ({
          ...prev,
          online: false,
          lastError: e?.message ?? String(e),
          lastCheckedAt: Date.now(),
        }));
      }
    };

    // Kick off the first ping shortly after mount, then poll every POLL_MS.
    const firstTimer = setTimeout(tick, FIRST_POLL_MS);
    const interval = setInterval(tick, POLL_MS);

    // Re-ping immediately when the app returns to foreground — backgrounded
    // apps don't run interval timers reliably on Android.
    const sub = AppState.addEventListener("change", (s) => {
      if (s === "active") tick();
    });

    return () => {
      stoppedRef.current = true;
      clearTimeout(firstTimer);
      clearInterval(interval);
      sub.remove();
    };
  }, [identifier]);

  return state;
}
