// iPad printer: Star TSP143IIIU over its MFi USB cable, driven by Star's
// StarXpand SDK (react-native-star-io10).
//
// Exposes the same methods as the Android `BravePosPrinter` native module so
// the rest of the app doesn't know which printer it is talking to. Every
// receipt, packer slip and shift summary arrives here as a 576-px PNG via
// printImage(), so the image path is the one that matters. The text-section
// printReceipt/printKitchenTicket methods have no callers and are not ported.
//
// The SDK allows one open connection per printer, and usePrinterStatus pings
// every 30 s while receipts print on their own schedule, so every call goes
// through one queue: a ping can never collide with a print.
//
// Info.plist needs UISupportedExternalAccessoryProtocols = jp.star-m.starpro
// (app.json → ios.infoPlist). App Store release also needs Star's MFi app
// approval: https://star-m.jp/eng/products/s_print/apple_app_mfi.html
import {
  InterfaceType,
  StarConnectionSettings,
  StarDeviceDiscoveryManagerFactory,
  StarPrinter,
  StarXpandCommand,
} from "react-native-star-io10";

type PrintResult = { ok: boolean; error?: string };
type Found = { identifier: string; model: string; interfaceType: string };

const DISCOVERY_MS = 3000;

let queue: Promise<unknown> = Promise.resolve();

function serial<T>(fn: () => Promise<T>): Promise<T> {
  const run = queue.then(fn, fn);
  queue = run.catch(() => {});
  return run;
}

function message(e: any): string {
  return e?.message || String(e);
}

async function withPrinter<T>(identifier: string, fn: (p: StarPrinter) => Promise<T>): Promise<T> {
  const settings = new StarConnectionSettings();
  settings.interfaceType = InterfaceType.Usb;
  settings.identifier = identifier;
  const printer = new StarPrinter(settings);
  try {
    await printer.open();
    try {
      return await fn(printer);
    } finally {
      await printer.close().catch(() => {});
    }
  } finally {
    await printer.dispose().catch(() => {});
  }
}

// Build the commands for one printer document, ending with a feed-and-cut
// like the Epson module's CUT_FEED.
function document(build: (p: StarXpandCommand.PrinterBuilder) => StarXpandCommand.PrinterBuilder): Promise<string> {
  const printer = build(new StarXpandCommand.PrinterBuilder())
    .actionCut(StarXpandCommand.Printer.CutType.Partial);
  return new StarXpandCommand.StarXpandCommandBuilder()
    .addDocument(new StarXpandCommand.DocumentBuilder().addPrinter(printer))
    .getCommands();
}

async function print(identifier: string, commands: Promise<string>): Promise<PrintResult> {
  return serial(async () => {
    try {
      const c = await commands;
      await withPrinter(identifier, (p) => p.print(c));
      return { ok: true };
    } catch (e) {
      return { ok: false, error: message(e) };
    }
  });
}

const notPorted = async (): Promise<PrintResult> =>
  ({ ok: false, error: "Text printing is not available on iPad" });

export const BravePosPrinter = {
  list(): Promise<Found[]> {
    return serial(async () => {
      const manager = await StarDeviceDiscoveryManagerFactory.create([InterfaceType.Usb]);
      const found: Found[] = [];
      manager.discoveryTime = DISCOVERY_MS;
      await new Promise<void>((resolve, reject) => {
        manager.onPrinterFound = (p) => {
          found.push({
            identifier: p.connectionSettings.identifier,
            model: p.information?.model ?? "Star printer",
            interfaceType: "Usb",
          });
        };
        manager.onDiscoveryFinished = () => resolve();
        manager.startDiscovery().catch(reject);
      });
      return found;
    });
  },

  printImage(identifier: string, base64Png: string, widthDots: number): Promise<PrintResult> {
    return print(identifier, document((p) =>
      p.actionPrintImage(new StarXpandCommand.Printer.ImageParameter(base64Png, widthDots))));
  },

  testPrint(identifier: string): Promise<PrintResult> {
    return print(identifier, document((p) =>
      p.styleAlignment(StarXpandCommand.Printer.Alignment.Center)
        .actionPrintText("BRAVE POS iPAD TEST\n")
        .actionPrintText("If you can read this line\n")
        .actionPrintText("the printer is fully working.\n")
        .actionFeedLine(1)
        .actionPrintText("Printed via Star StarXpand SDK\n")));
  },

  ping(identifier: string): Promise<{ online: boolean; latencyMs?: number; error?: string }> {
    return serial(async () => {
      const started = Date.now();
      try {
        const status = await withPrinter(identifier, (p) => p.getStatus());
        const problem = status.coverOpen ? "Cover open" : status.paperEmpty ? "Out of paper" : undefined;
        return { online: true, latencyMs: Date.now() - started, error: problem };
      } catch (e) {
        return { online: false, error: message(e) };
      }
    });
  },

  printReceipt: notPorted,
  printKitchenTicket: notPorted,
};
