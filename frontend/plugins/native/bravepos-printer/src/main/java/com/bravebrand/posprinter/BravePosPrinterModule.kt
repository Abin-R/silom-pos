package com.bravebrand.posprinter

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.Base64
import android.util.Log
import com.facebook.react.bridge.Arguments
import com.facebook.react.bridge.Promise
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.bridge.ReactContextBaseJavaModule
import com.facebook.react.bridge.ReactMethod
import com.facebook.react.bridge.ReadableArray
import com.facebook.react.bridge.ReadableMap
import com.facebook.react.bridge.WritableArray
import com.facebook.react.bridge.WritableMap
import com.epson.epos2.Epos2CallbackCode
import com.epson.epos2.Epos2Exception
import com.epson.epos2.discovery.DeviceInfo
import com.epson.epos2.discovery.Discovery
import com.epson.epos2.discovery.DiscoveryListener
import com.epson.epos2.discovery.FilterOption
import com.epson.epos2.printer.Printer
import com.epson.epos2.printer.PrinterStatusInfo
import com.epson.epos2.printer.ReceiveListener

/**
 * Brave POS USB printer bridge — Epson ePOS SDK (vendored locally as
 * `libs/ePOS2.jar` + `jniLibs/<abi>/libepos2.so`).  Drives an Epson
 * TM-T82X (M352 and M352A variants share the same SDK constant and
 * command set) over USB OTG.
 *
 * JS-facing API (unchanged from the StarPRNT build):
 *   list(promise)                                    → Array<{ identifier, model, interfaceType }>
 *   testPrint(identifier, promise)                   → { ok, error? }
 *   printReceipt(identifier, sections, opts, promise)
 *   printKitchenTicket(identifier, sections, opts, promise)
 *
 * Identifier format: opaque target string from Discovery, e.g. "USB:000000000000000000".
 *
 * Async model: Epson's sendData() is non-blocking — completion fires via
 * ReceiveListener.onPtrReceive on the SDK's thread.  We bridge that to the
 * RN Promise by waiting on a lock that the listener releases.
 */
class BravePosPrinterModule(private val reactContext: ReactApplicationContext) :
    ReactContextBaseJavaModule(reactContext) {

    companion object {
        private const val TAG = "BravePosPrinter"
        // Reasonable upper bound: USB connect + send + ACK on TM-T82X
        // is typically <1s; TCP can be 2–3s.  Allow generous margin for
        // first-time perms or slow Wi-Fi.
        private const val PRINT_TIMEOUT_MS = 20_000L
        // TCP discovery needs ~5s for UDP broadcast + responses; USB is
        // sub-second.  6s covers both.
        private const val DISCOVERY_TIMEOUT_MS = 6_000L
        // The v2.37 SDK doesn't expose a separate TM_T82X constant — it's
        // wire-compatible with TM_T82 and shares the command set.  Both
        // M352 and M352A variants run fine with this.
        private const val PRINTER_SERIES = Printer.TM_T82
        // MODEL_ANK is the universal Latin/ASCII model that every
        // TM-T82X firmware supports.  Earlier we tried MODEL_THAI but
        // the SDK threw ERR_UNSUPPORTED on connect — the printer's
        // firmware doesn't have the Thai font ROM installed.  ANK is
        // the safe baseline (covers all our current English receipts).
        // If we ever ship to a printer with the Thai firmware variant,
        // switch to EposShim.MODEL_THAI.
        private const val PRINTER_LANG = EposShim.MODEL_ANK
    }

    private val printerLock = Object()

    override fun getName(): String = "BravePosPrinter"

    @ReactMethod fun addListener(eventName: String) { /* No-op */ }
    @ReactMethod fun removeListeners(count: Int) { /* No-op */ }

    // ─── Discovery ────────────────────────────────────────────────────────

    @ReactMethod
    fun list(promise: Promise) {
        Thread {
            val found = mutableListOf<DeviceInfo>()
            val lock = Object()
            var stopped = false

            val filter = FilterOption().apply {
                deviceType = Discovery.TYPE_PRINTER
                // PORTTYPE_ALL covers TCP (network/router) + USB + Bluetooth.
                // The user's printer is now plugged into the router, so it'll
                // come back as a TCP target like "TCP:192.168.1.100".  We
                // intentionally don't lock to PORTTYPE_TCP so a USB fallback
                // still works if someone plugs it in directly.
                portType = Discovery.PORTTYPE_ALL
                epsonFilter = Discovery.FILTER_NAME
            }

            val listener = DiscoveryListener { deviceInfo ->
                synchronized(lock) { found.add(deviceInfo) }
            }

            try {
                Discovery.start(reactContext, filter, listener)
            } catch (e: Epos2Exception) {
                Log.e(TAG, "Discovery.start failed: ${e.errorStatus}", e)
                promise.reject("DISCOVERY_START_FAILED", "Epos2 ${e.errorStatus}: ${e.message}")
                return@Thread
            }

            // Wait the discovery window (no "finished" callback in this SDK).
            synchronized(lock) {
                try { lock.wait(DISCOVERY_TIMEOUT_MS) } catch (_: InterruptedException) {}
            }

            // Stop discovery, retrying once on ERR_PROCESSING per SDK guidance.
            for (i in 1..3) {
                try {
                    Discovery.stop()
                    stopped = true
                    break
                } catch (e: Epos2Exception) {
                    if (e.errorStatus == Epos2Exception.ERR_PROCESSING) {
                        try { Thread.sleep(150) } catch (_: InterruptedException) {}
                    } else {
                        Log.w(TAG, "Discovery.stop failed: ${e.errorStatus}", e)
                        break
                    }
                }
            }
            if (!stopped) Log.w(TAG, "Discovery.stop did not complete cleanly")

            val result: WritableArray = Arguments.createArray()
            for (info in found) {
                val map: WritableMap = Arguments.createMap()
                val target = info.target ?: ""
                map.putString("identifier", target)
                map.putString("model", info.deviceName ?: "Epson Printer")
                // Infer interface from the target prefix the SDK gave us.
                // Examples: "TCP:192.168.1.100" / "USB:000..." / "BT:..."
                map.putString("interfaceType", when {
                    target.startsWith("TCP:") -> "Lan"
                    target.startsWith("USB:") -> "Usb"
                    target.startsWith("BT:")  -> "Bluetooth"
                    target.startsWith("BLE:") -> "BluetoothLE"
                    else -> "Usb"
                })
                map.putString("macAddress", info.macAddress ?: "")
                map.putString("ipAddress", info.ipAddress ?: "")
                result.pushMap(map)
            }
            promise.resolve(result)
        }.start()
    }

    // ─── Print API ────────────────────────────────────────────────────────

    @ReactMethod
    fun testPrint(identifier: String, promise: Promise) {
        runPrintJob(identifier, promise) { p ->
            p.addTextAlign(EposShim.ALIGN_CENTER)
            p.addTextStyle(EposShim.FALSE, EposShim.FALSE, EposShim.TRUE, EposShim.COLOR_1)
            p.addText("BRAVE POS USB TEST\n")
            p.addTextStyle(EposShim.FALSE, EposShim.FALSE, EposShim.FALSE, EposShim.COLOR_1)
            p.addText("If you can read this line\n")
            p.addText("the printer is fully working.\n")
            p.addFeedLine(2)
            p.addTextAlign(EposShim.ALIGN_LEFT)
            p.addText("Printed via Epson ePOS SDK\n")
            p.addFeedLine(2)
            p.addCut(EposShim.CUT_FEED)
        }
    }

    @ReactMethod
    fun printReceipt(
        identifier: String,
        sections: ReadableArray,
        options: ReadableMap,
        promise: Promise,
    ) {
        runPrintJob(identifier, promise) { p ->
            applySections(p, sections, options)
            val cutKind = if (options.hasKey("cut")) options.getString("cut") else "partial"
            when (cutKind) {
                "none" -> { /* no cut */ }
                "full" -> p.addCut(EposShim.CUT_FEED)  // ePOS only exposes CUT_FEED + CUT_NO_FEED; both end in a partial cut at the cutter mechanism on TM-T82X
                else   -> p.addCut(EposShim.CUT_FEED)
            }
        }
    }

    @ReactMethod
    fun printKitchenTicket(
        identifier: String,
        sections: ReadableArray,
        options: ReadableMap,
        promise: Promise,
    ) {
        printReceipt(identifier, sections, options, promise)
    }

    /**
     * Print a pre-rendered PNG bitmap.  JS renders the entire receipt
     * (with Thai font + custom layout) using react-native-view-shot and
     * passes the base64-encoded PNG here.  We decode it to a Bitmap and
     * send via Printer.addImage which is firmware-agnostic and supports
     * arbitrary Unicode content (Thai, emoji, logo glyphs, etc.).
     *
     * @param identifier  Epson target string from discovery (e.g. "TCP:192.168.1.62")
     * @param base64Png   Base-64 string (no `data:` prefix) of the PNG bitmap
     * @param widthDots   Print width in dots (typically 576 for 80mm paper).
     *                    Image will be scaled to this width preserving aspect.
     */
    @ReactMethod
    fun printImage(
        identifier: String,
        base64Png: String,
        widthDots: Int,
        promise: Promise,
    ) {
        // Decode off the worker thread we'll already be on inside runPrintJob.
        runPrintJob(identifier, promise) { p ->
            val cleaned = base64Png.substringAfter(",", base64Png)  // strip data URL if present
            val bytes = Base64.decode(cleaned, Base64.DEFAULT)
            val decoded = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
                ?: throw IllegalArgumentException("PNG decode returned null bitmap")
            // addImage signature (full): addImage(bitmap, x, y, width, height,
            //   color, mode, halftone, brightness, compress)
            // HIGH_DENSITY + DITHER renders Thai/Latin text smoothly without
            // jagged edges from monochrome thresholding.
            p.addImage(
                decoded,
                0, 0,
                decoded.width, decoded.height,
                EposShim.COLOR_1,
                EposShim.MODE_MONO_HIGH_DENSITY,
                EposShim.HALFTONE_DITHER,
                EposShim.PARAM_DEFAULT.toDouble(),  // brightness = default
                EposShim.COMPRESS_AUTO,
            )
            p.addCut(EposShim.CUT_FEED)
        }
    }

    /**
     * Cheap liveness check used by the UI's online/offline indicator.
     *
     * For TCP printers we open a raw socket to the parsed host:port (port
     * 9100 by default — Epson's JetDirect endpoint), with a 2-second
     * timeout, then close immediately.  This is much faster and lighter
     * than a full Printer.connect() and doesn't tie up the SDK while the
     * indicator polls every 30s.
     *
     * For USB printers we optimistically return online=true — a USB
     * device that's physically plugged in IS reachable; we'd need full
     * UsbManager enumeration to do better, and that's overkill for a
     * "is the printer alive" pill.
     */
    @ReactMethod
    fun ping(identifier: String, promise: Promise) {
        Thread {
            val out: WritableMap = Arguments.createMap()
            out.putString("identifier", identifier)
            try {
                if (identifier.startsWith("TCP:")) {
                    val rest = identifier.substring(4).trim()
                    // Distinguish three forms the SDK can hand us:
                    //   "192.168.1.62"            → IPv4, can socket-ping directly
                    //   "192.168.1.62:9100"       → IPv4 with explicit port
                    //   "50:57:9C:09:A1:D3"       → MAC address (the SDK
                    //                                resolves it to IP via ARP
                    //                                at print time, so we can't
                    //                                socket-ping it here)
                    val ipv4Regex = Regex("^(\\d{1,3}\\.){3}\\d{1,3}(:\\d+)?$")
                    if (ipv4Regex.matches(rest)) {
                        val parts = rest.split(":")
                        val host = parts[0]
                        val port = if (parts.size > 1) parts[1].toIntOrNull() ?: 9100 else 9100
                        val socket = java.net.Socket()
                        val started = System.currentTimeMillis()
                        try {
                            socket.connect(java.net.InetSocketAddress(host, port), 2_000)
                            out.putBoolean("online", true)
                            out.putInt("latencyMs", (System.currentTimeMillis() - started).toInt())
                        } finally {
                            try { socket.close() } catch (_: Throwable) {}
                        }
                    } else {
                        // MAC-based or other non-IPv4 identifier — we can't
                        // socket-ping a MAC.  Trust the SDK's internal ARP
                        // resolution: if discovery returned this identifier,
                        // the printer is reachable.  Optimistically report
                        // online; the actual print attempt is what tells us
                        // for certain.
                        out.putBoolean("online", true)
                        out.putString("note", "MAC-based identifier; ping is optimistic")
                    }
                } else {
                    // USB or unknown scheme — optimistic for the same reason.
                    out.putBoolean("online", true)
                    out.putString("note", "non-TCP identifier; ping is optimistic")
                }
            } catch (t: Throwable) {
                out.putBoolean("online", false)
                out.putString("error", t.message ?: t.javaClass.simpleName)
            }
            promise.resolve(out)
        }.start()
    }

    // ─── Plumbing ─────────────────────────────────────────────────────────

    /**
     * Run a print job: instantiate Printer, build content via `build`,
     * connect, sendData (async), wait for ReceiveListener callback,
     * disconnect.  Resolves the JS Promise with {ok, error?}.
     */
    private fun runPrintJob(
        target: String,
        promise: Promise,
        build: (Printer) -> Unit,
    ) {
        Thread {
            val out: WritableMap = Arguments.createMap()
            synchronized(printerLock) {
                var printer: Printer? = null
                val callbackLock = Object()
                var resultCode: Int = -1
                var resultStatus: PrinterStatusInfo? = null
                var fired = false

                try {
                    printer = Printer(PRINTER_SERIES, PRINTER_LANG, reactContext)
                    printer.setReceiveEventListener(object : ReceiveListener {
                        override fun onPtrReceive(
                            p: Printer?,
                            code: Int,
                            status: PrinterStatusInfo?,
                            printJobId: String?,
                        ) {
                            synchronized(callbackLock) {
                                resultCode = code
                                resultStatus = status
                                fired = true
                                callbackLock.notifyAll()
                            }
                        }
                    })

                    build(printer)
                    printer.connect(target, EposShim.PARAM_DEFAULT)
                    printer.beginTransaction()
                    printer.sendData(EposShim.PARAM_DEFAULT)

                    synchronized(callbackLock) {
                        if (!fired) {
                            try { callbackLock.wait(PRINT_TIMEOUT_MS) } catch (_: InterruptedException) {}
                        }
                    }

                    if (!fired) {
                        out.putBoolean("ok", false)
                        out.putString("error", "Timeout waiting for printer ACK")
                    } else if (resultCode == Epos2CallbackCode.CODE_SUCCESS) {
                        out.putBoolean("ok", true)
                    } else {
                        out.putBoolean("ok", false)
                        out.putString("error", buildErrorMessage(resultCode, resultStatus))
                    }
                } catch (e: Epos2Exception) {
                    Log.e(TAG, "ePOS exception (status=${e.errorStatus})", e)
                    out.putBoolean("ok", false)
                    out.putString("error", "Epos2 ${e.errorStatus}: ${e.message ?: "no message"}")
                } catch (t: Throwable) {
                    Log.e(TAG, "Print job fatal", t)
                    out.putBoolean("ok", false)
                    out.putString("error", "${t.javaClass.simpleName}: ${t.message ?: "no message"}")
                } finally {
                    // Disconnect on a separate thread per the SDK sample — calling
                    // disconnect() from inside the callback path can deadlock.
                    val p = printer
                    if (p != null) {
                        Thread {
                            try { p.endTransaction() } catch (_: Throwable) {}
                            try { p.disconnect() } catch (_: Throwable) {}
                            try { p.clearCommandBuffer() } catch (_: Throwable) {}
                            try { p.setReceiveEventListener(null) } catch (_: Throwable) {}
                        }.start()
                    }
                    promise.resolve(out)
                }
            }
        }.start()
    }

    private fun buildErrorMessage(code: Int, status: PrinterStatusInfo?): String {
        // Code constants live in Epos2CallbackCode (verified via JAR
        // strings dump on v2.37).  CODE_ERR_FAILURE/DISCONNECT/PARAM
        // from older docs don't exist in this SDK version.
        val codeName = when (code) {
            Epos2CallbackCode.CODE_SUCCESS -> "SUCCESS"
            Epos2CallbackCode.CODE_ERR_TIMEOUT -> "TIMEOUT"
            Epos2CallbackCode.CODE_ERR_NOT_FOUND -> "NOT_FOUND"
            Epos2CallbackCode.CODE_ERR_AUTORECOVER -> "AUTORECOVER"
            Epos2CallbackCode.CODE_ERR_COVER_OPEN -> "COVER_OPEN"
            Epos2CallbackCode.CODE_ERR_CUTTER -> "CUTTER_ERROR"
            Epos2CallbackCode.CODE_ERR_MECHANICAL -> "MECHANICAL_ERROR"
            Epos2CallbackCode.CODE_ERR_EMPTY -> "PAPER_EMPTY"
            Epos2CallbackCode.CODE_ERR_UNRECOVERABLE -> "UNRECOVERABLE"
            Epos2CallbackCode.CODE_ERR_SYSTEM -> "SYSTEM_ERROR"
            Epos2CallbackCode.CODE_ERR_PORT -> "PORT_ERROR"
            Epos2CallbackCode.CODE_ERR_BATTERY_LOW -> "BATTERY_LOW"
            Epos2CallbackCode.CODE_ERR_TOO_MANY_REQUESTS -> "TOO_MANY_REQUESTS"
            Epos2CallbackCode.CODE_CANCELED -> "CANCELED"
            Epos2CallbackCode.CODE_PRINTING -> "PRINTING"
            else -> "code=$code"
        }
        val statusBits = mutableListOf<String>()
        if (status != null) {
            // PrinterStatusInfo in v2.37 doesn't expose getOnline() — the
            // connection field is implicit (if we got a callback at all,
            // the connection was opened).  Just surface user-actionable
            // fields: cover open, paper state, error code.
            if (status.coverOpen == EposShim.TRUE) statusBits.add("cover open")
            if (status.paper == EposShim.PAPER_EMPTY) statusBits.add("paper empty")
            if (status.paper == EposShim.PAPER_NEAR_END) statusBits.add("paper near end")
            if (status.errorStatus != EposShim.NO_ERR) statusBits.add("errStatus=${status.errorStatus}")
        }
        return if (statusBits.isEmpty()) codeName else "$codeName ($statusBits.joinToString())"
    }

    // ─── Receipt section renderer ────────────────────────────────────────

    private fun applySections(p: Printer, sections: ReadableArray, options: ReadableMap) {
        val width = if (options.hasKey("width")) options.getInt("width") else 48

        for (i in 0 until sections.size()) {
            val s = sections.getMap(i) ?: continue
            val type = s.getString("type") ?: "text"

            // Style
            val align = if (s.hasKey("align")) s.getString("align") else null
            p.addTextAlign(when (align) {
                "center" -> EposShim.ALIGN_CENTER
                "right"  -> EposShim.ALIGN_RIGHT
                else     -> EposShim.ALIGN_LEFT
            })
            val bold = s.hasKey("bold") && s.getBoolean("bold")
            val dbl = s.hasKey("double") && s.getBoolean("double")
            // addTextStyle(reverse, ul, em, color) — em = emphasis = bold
            p.addTextStyle(
                EposShim.FALSE,
                EposShim.FALSE,
                if (bold) EposShim.TRUE else EposShim.FALSE,
                EposShim.COLOR_1,
            )
            // addTextSize(w, h) — double-height for queue numbers
            if (dbl) p.addTextSize(2, 2) else p.addTextSize(1, 1)

            when (type) {
                "text" -> {
                    val raw = s.getString("text") ?: ""
                    val withNl = if (raw.endsWith("\n")) raw else raw + "\n"
                    p.addText(asciiSafe(withNl))
                }
                "row" -> {
                    val left = asciiSafe(s.getString("left") ?: "")
                    val right = asciiSafe(s.getString("right") ?: "")
                    val pad = (width - left.length - right.length).coerceAtLeast(1)
                    p.addText(left + " ".repeat(pad) + right + "\n")
                }
                "divider" -> {
                    p.addText("-".repeat(width) + "\n")
                }
                "feed" -> {
                    val n = if (s.hasKey("lines")) s.getInt("lines").coerceAtLeast(1) else 1
                    p.addFeedLine(n)
                }
            }

            // Reset to defaults for next iteration.
            p.addTextStyle(EposShim.FALSE, EposShim.FALSE, EposShim.FALSE, EposShim.COLOR_1)
            p.addTextSize(1, 1)
        }
    }

    /** ASCII-only — Thai/Unicode rendering requires image printing (out of scope). */
    private fun asciiSafe(s: String): String {
        val sb = StringBuilder(s.length)
        for (ch in s) {
            val cp = ch.code
            sb.append(if (cp in 0x20..0x7E || cp == 0x0A) ch else '?')
        }
        return sb.toString()
    }
}
