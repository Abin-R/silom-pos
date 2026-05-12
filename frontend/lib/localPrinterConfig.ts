/**
 * Local-device printer config: which Star printer this specific tablet
 * should print to.  Stored in AsyncStorage because it's hardware-specific
 * (a USB device id on tablet A is meaningless on tablet B).  The cloud
 * Settings record stays as the cross-device default for network printers.
 */
import AsyncStorage from '@react-native-async-storage/async-storage';
import type { PrinterConfig } from './starPrinter';

const KEY = 'bravepos:local-printer:v1';

const DEFAULT: PrinterConfig = {
  enabled: false,
  interface: 'Usb',
  identifier: '',
  paperWidth: 80,
};

export async function loadLocalPrinterConfig(): Promise<PrinterConfig> {
  try {
    const raw = await AsyncStorage.getItem(KEY);
    if (!raw) return { ...DEFAULT };
    return { ...DEFAULT, ...JSON.parse(raw) };
  } catch {
    return { ...DEFAULT };
  }
}

export async function saveLocalPrinterConfig(c: PrinterConfig): Promise<void> {
  await AsyncStorage.setItem(KEY, JSON.stringify(c));
}

export async function clearLocalPrinterConfig(): Promise<void> {
  await AsyncStorage.removeItem(KEY);
}
