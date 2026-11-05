/**
 * Local-device printer config: which Star printer this specific tablet
 * should print to.  Stored in AsyncStorage because it's hardware-specific
 * (a USB device id on tablet A is meaningless on tablet B).  The cloud
 * Settings record stays as the cross-device default for network printers.
 */
import AsyncStorage from '@react-native-async-storage/async-storage';
import type { PrinterConfig } from './starPrinter';

const KEY = 'bravepos:local-printer:v1';

// The second printer at a high-volume branch, which takes the packer slips.
// KEY above stays as the customer-slip printer, so a tablet set up before this
// existed keeps printing exactly as it did.
const PACKER_KEY = 'bravepos:local-printer:packer:v1';

export type PrinterRole = 'customer' | 'packer';

const DEFAULT: PrinterConfig = {
  enabled: false,
  interface: 'Usb',
  identifier: '',
  paperWidth: 80,
};

async function load(key: string): Promise<PrinterConfig> {
  try {
    const raw = await AsyncStorage.getItem(key);
    if (!raw) return { ...DEFAULT };
    return { ...DEFAULT, ...JSON.parse(raw) };
  } catch {
    return { ...DEFAULT };
  }
}

export function loadLocalPrinterConfig(): Promise<PrinterConfig> {
  return load(KEY);
}

export function loadPackerPrinterConfig(): Promise<PrinterConfig> {
  return load(PACKER_KEY);
}

export async function savePackerPrinterConfig(c: PrinterConfig): Promise<void> {
  await AsyncStorage.setItem(PACKER_KEY, JSON.stringify(c));
}

/**
 * Which printer a slip goes to.  A packer slip with no usable packer printer
 * falls back to the customer printer: an order must never lose its slip because
 * the second printer hasn't been set up or has been switched off.
 */
export function printerForSlip(
  docType: string | undefined,
  customer: PrinterConfig,
  packer: PrinterConfig,
): PrinterConfig {
  return docType === 'packer' && packer.enabled && packer.identifier ? packer : customer;
}

export async function saveLocalPrinterConfig(c: PrinterConfig): Promise<void> {
  await AsyncStorage.setItem(KEY, JSON.stringify(c));
}

export async function clearLocalPrinterConfig(): Promise<void> {
  await AsyncStorage.removeItem(KEY);
}
