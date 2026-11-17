// The printer's native module, chosen per platform at bundle time.
//
// Android: our own `bravepos-printer` Kotlin module (Epson TM-T82X over USB,
// plugins/native/). This file is exactly what every caller used to read
// inline, so the Android app behaves as before.
//
// iOS: Metro picks printerNative.ios.ts instead, which wraps Star's StarXpand
// SDK (Star TSP143IIIU over the iPad's cable) in the same method shape. The
// Star SDK never enters the Android bundle.
import { NativeModules } from "react-native";

export const BravePosPrinter = (NativeModules as any).BravePosPrinter;
