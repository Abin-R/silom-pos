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
import { Modal, StyleSheet, Text, View } from "react-native";
import { C, R } from "./theme";
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
    buttons: buttons && buttons.length ? buttons : [{ text: "OK" }],
  };
  if (listener) listener(d);
  else pending = d;
}

/** Promise form, for `if (await confirmDialog(...))` call sites. */
export function confirmDialog(
  title: string,
  message?: string,
  confirmText = "Confirm",
  destructive = true,
): Promise<boolean> {
  return new Promise((resolve) => {
    showAlert(title, message, [
      { text: "Cancel", style: "cancel", onPress: () => resolve(false) },
      {
        text: confirmText,
        style: destructive ? "destructive" : "default",
        onPress: () => resolve(true),
      },
    ]);
  });
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
});
