// Cross-platform confirm/alert.
//
// React Native Web ships `Alert` as `static alert() {}` — a literal no-op. So
// on web every confirmation silently did nothing (Cancel bill looked dead)
// and every "Save failed" message was swallowed, which is worse: the cashier
// is told nothing at all.
//
// This renders an in-app dialog instead, on every platform, so the same code
// path is exercised on the tablet and in the browser. The API mirrors
// Alert.alert so call sites read the same.
//
// Mount <DialogHost /> once, at the root (see app/_layout.tsx).

import React, { useEffect, useState } from "react";
import { Modal, Pressable, StyleSheet, Text, View } from "react-native";
import { C, R } from "./theme";
import { t as tr } from "./i18n";
import { Btn } from "./ui";

export type DialogButton = {
  text: string;
  onPress?: () => void;
  style?: "default" | "cancel" | "destructive";
};

type Dialog = {
  title: string;
  message?: string;
  buttons: DialogButton[];
};

let listener: ((d: Dialog | null) => void) | null = null;
// A dialog raised before the host mounts would be dropped; hold one so the
// very first error after boot still reaches the user.
let pending: Dialog | null = null;

/** Drop-in replacement for Alert.alert. */
export function showAlert(
  title: string,
  message?: string,
  buttons?: DialogButton[],
) {
  const d: Dialog = {
    title,
    message,
    buttons: buttons && buttons.length ? buttons : [{ text: tr("dialog.ok") }],
  };
  if (listener) listener(d);
  else pending = d;
}

/** Promise form, for `if (await confirmDialog(...))` call sites. */
export function confirmDialog(
  title: string,
  message?: string,
  confirmText?: string,
  destructive = true,
): Promise<boolean> {
  return new Promise((resolve) => {
    showAlert(title, message, [
      { text: tr("dialog.cancel"), style: "cancel", onPress: () => resolve(false) },
      {
        text: confirmText ?? tr("dialog.confirm"),
        style: destructive ? "destructive" : "default",
        onPress: () => resolve(true),
      },
    ]);
  });
}

// ── Toasts ──────────────────────────────────────────────────────────────
//
// A dialog is the wrong shape for "that won't work, and here is why". It stops
// the cashier, takes a tap to clear, and there is a customer at the counter.
// A toast says the same thing and gets out of the way on its own.
//
// It renders inside a Modal on purpose. The things that raise one — the cart
// sheet on a phone, the payment screen — are Modals themselves, and a plain
// view at the root of the tree renders *underneath* those on Android, so the
// message would simply never be seen. The cost of a Modal is that it captures
// touches for as long as it is up, so it also dismisses on any tap: a cashier
// who wants to carry on immediately just taps, and never waits on it.

const TOAST_MS = 2600;

let toastListener: ((t: string | null) => void) | null = null;
let toastPending: string | null = null;

/** Brief, self-dismissing message. Not for anything that needs a decision. */
export function showToast(message: string) {
  if (toastListener) toastListener(message);
  else toastPending = message;
}

export function ToastHost() {
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    toastListener = setMessage;
    if (toastPending) {
      setMessage(toastPending);
      toastPending = null;
    }
    return () => {
      toastListener = null;
    };
  }, []);

  useEffect(() => {
    if (message === null) return;
    // Keyed on the message so a second toast restarts the clock rather than
    // inheriting what was left of the first one's.
    const id = setTimeout(() => setMessage(null), TOAST_MS);
    return () => clearTimeout(id);
  }, [message]);

  if (message === null) return null;

  return (
    <Modal visible transparent animationType="fade" onRequestClose={() => setMessage(null)}>
      <Pressable style={s.toastWrap} onPress={() => setMessage(null)} testID="app-toast">
        <View style={s.toast}>
          <Text style={s.toastText}>{message}</Text>
        </View>
      </Pressable>
    </Modal>
  );
}


export function DialogHost() {
  const [dialog, setDialog] = useState<Dialog | null>(null);

  useEffect(() => {
    listener = setDialog;
    if (pending) {
      setDialog(pending);
      pending = null;
    }
    return () => {
      listener = null;
    };
  }, []);

  if (!dialog) return null;

  const close = (b?: DialogButton) => {
    setDialog(null);
    // Let the modal unmount before the handler runs — a handler that opens
    // another dialog would otherwise race this one's dismissal.
    if (b?.onPress) setTimeout(b.onPress, 0);
  };

  return (
    <Modal
      visible
      transparent
      animationType="fade"
      onRequestClose={() => close(dialog.buttons.find((b) => b.style === "cancel"))}
    >
      <View style={s.overlay}>
        <View style={s.card} testID="app-dialog">
          <Text style={s.title}>{dialog.title}</Text>
          {!!dialog.message && <Text style={s.message}>{dialog.message}</Text>}
          <View style={s.row}>
            {dialog.buttons.map((b, i) => (
              <Btn
                key={`${b.text}-${i}`}
                label={b.text}
                variant={
                  b.style === "destructive"
                    ? "red"
                    : b.style === "cancel"
                      ? "default"
                      : "blue"
                }
                height={48}
                style={{ flex: 1 }}
                onPress={() => close(b)}
                testID={`dialog-${b.style === "cancel" ? "cancel" : "confirm"}`}
              />
            ))}
          </View>
        </View>
      </View>
    </Modal>
  );
}

const s = StyleSheet.create({
  overlay: {
    flex: 1,
    backgroundColor: C.scrim,
    alignItems: "center",
    justifyContent: "center",
    padding: 24,
  },
  card: {
    width: "100%",
    maxWidth: 440,
    backgroundColor: C.surface,
    borderRadius: R.modal,
    padding: 26,
  },
  title: {
    fontSize: 19,
    fontWeight: "800",
    color: C.ink,
    letterSpacing: -0.4,
  },
  message: {
    fontSize: 15,
    color: C.ink2Soft,
    lineHeight: 22,
    marginTop: 10,
  },
  row: { flexDirection: "row", gap: 12, marginTop: 24 },

  // Low on the screen, clear of the header and of a cashier's own hand on a
  // tablet held at the counter. No scrim: this is a remark, not a barrier.
  toastWrap: {
    flex: 1,
    justifyContent: "flex-end",
    alignItems: "center",
    padding: 28,
  },
  toast: {
    maxWidth: 520,
    backgroundColor: C.inkStrong,
    borderRadius: R.control,
    paddingVertical: 14,
    paddingHorizontal: 20,
  },
  toastText: {
    color: C.surface,
    fontSize: 15,
    fontWeight: "600",
    lineHeight: 21,
    textAlign: "center",
  },
});
