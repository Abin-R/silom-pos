package com.bravebrand.posprinter;

import com.epson.epos2.printer.Printer;

/**
 * Java shim that re-exports the Epson ePOS SDK constants we use.
 *
 * Why: the constants (ALIGN_*, CUT_*, MODEL_*, PARAM_*, FALSE/TRUE,
 * COLOR_*, PAPER_*, NO_ERR) are declared on com.epson.epos2.printer.
 * CommonPrinter, which is *package-private*.  Java is happy to follow
 * the static-field inheritance from the public `Printer` subclass, but
 * Kotlin's stricter compiler refuses ("Cannot access CommonPrinter: it
 * is package-private").  Re-exporting via this Java class sidesteps the
 * Kotlin restriction without exposing any private API.
 */
public final class EposShim {
    private EposShim() {}

    // Boolean-ish
    public static final int FALSE = Printer.FALSE;
    public static final int TRUE = Printer.TRUE;

    // Text alignment
    public static final int ALIGN_LEFT = Printer.ALIGN_LEFT;
    public static final int ALIGN_CENTER = Printer.ALIGN_CENTER;
    public static final int ALIGN_RIGHT = Printer.ALIGN_RIGHT;

    // Color (for addTextStyle)
    public static final int COLOR_1 = Printer.COLOR_1;

    // Cutter
    public static final int CUT_FEED = Printer.CUT_FEED;

    // Connection / send parameters
    public static final int PARAM_DEFAULT = Printer.PARAM_DEFAULT;

    // Paper status
    public static final int PAPER_EMPTY = Printer.PAPER_EMPTY;
    public static final int PAPER_NEAR_END = Printer.PAPER_NEAR_END;

    // Error / status
    public static final int NO_ERR = Printer.NO_ERR;

    // Character model — MODEL_ANK is the universal Latin/ASCII model that
    // any TM-T82X firmware supports, regardless of regional variant.  We
    // initially tried MODEL_THAI but the SDK threw ERR_UNSUPPORTED on
    // connect() — meaning this printer's firmware doesn't include the
    // Thai font ROM.  ANK is the safe baseline for English/ASCII receipts.
    // For Thai text we render the receipt as a PNG and use addImage()
    // instead — see printImage() in BravePosPrinterModule.kt.
    public static final int MODEL_ANK = Printer.MODEL_ANK;

    // Image-printing constants for addImage().  Defined on CommonPrinter
    // (package-private) so re-exported here for Kotlin access.
    public static final int MODE_MONO_HIGH_DENSITY = Printer.MODE_MONO_HIGH_DENSITY;
    public static final int MODE_GRAY16 = Printer.MODE_GRAY16;
    public static final int HALFTONE_DITHER = Printer.HALFTONE_DITHER;
    public static final int HALFTONE_THRESHOLD = Printer.HALFTONE_THRESHOLD;
    public static final int HALFTONE_ERROR_DIFFUSION = Printer.HALFTONE_ERROR_DIFFUSION;
    public static final int COMPRESS_AUTO = Printer.COMPRESS_AUTO;
    public static final int COMPRESS_DEFLATE = Printer.COMPRESS_DEFLATE;
    public static final int COMPRESS_NONE = Printer.COMPRESS_NONE;
}
