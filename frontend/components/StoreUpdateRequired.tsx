/**
 * iPad: the full-screen "Update required" page (lib/storeUpdate).
 *
 * When a newer App Store version is live and forced updates are on, this
 * covers the whole till once it is idle, and the App Store pop-up opens over
 * it. Closing the pop-up leaves this page; Update now opens the pop-up again.
 * It never goes away by itself: iOS restarts Brave POS on the new version
 * after the update, or the backoffice switch "Force app updates on the tills"
 * is turned off.
 *
 * Android never sets the gate (Play's own full-screen updater does this job),
 * so there this renders nothing.
 *
 * Mounted once in the root layout, above the stack and dialogs, below the
 * OTA "Updating…" cover.
 */
import { useEffect, useState } from "react";
import { ActivityIndicator, Image, Pressable, StyleSheet, Text, View } from "react-native";
import {
  getStoreUpdateGate,
  openStoreUpdate,
  subscribeStoreUpdateGate,
  type StoreUpdateGate,
} from "../lib/storeUpdate";
import { useT } from "../lib/i18n";
import { C, R } from "../lib/theme";

const LOGO = require("../assets/images/rolling-pinn-logo.png");

export function StoreUpdateRequired() {
  const { t } = useT();
  const [gate, setGate] = useState<StoreUpdateGate>(getStoreUpdateGate());
  useEffect(() => subscribeStoreUpdateGate(setGate), []);

  if (!gate.required) return null;

  return (
    <View style={s.cover} testID="store-update-required">
      <View style={s.card}>
        <Image source={LOGO} style={s.logo} resizeMode="contain" />
        <Text style={s.title}>{t("store_update.required_title")}</Text>
        <Text style={s.note}>
          {gate.version
            ? t("store_update.required_note_version", { version: gate.version })
            : t("store_update.required_note")}
        </Text>
        <Pressable
          onPress={openStoreUpdate}
          disabled={gate.opening}
          style={({ pressed }) => [s.button, (pressed || gate.opening) && s.buttonPressed]}
          testID="store-update-now"
        >
          {gate.opening ? (
            <ActivityIndicator color={C.surface} />
          ) : (
            <Text style={s.buttonText}>{t("store_update.update_now")}</Text>
          )}
        </Pressable>
        <Text style={s.hint}>{t("store_update.required_hint")}</Text>
      </View>
    </View>
  );
}

const s = StyleSheet.create({
  cover: {
    position: "absolute", top: 0, left: 0, right: 0, bottom: 0,
    backgroundColor: C.bg,
    alignItems: "center",
    justifyContent: "center",
    padding: 24,
    zIndex: 1500,
    elevation: 1500,
  },
  card: {
    width: "100%",
    maxWidth: 460,
    backgroundColor: C.surface,
    borderRadius: R.modal,
    paddingVertical: 32,
    paddingHorizontal: 28,
    alignItems: "center",
    gap: 14,
  },
  logo: {
    width: 160,
    height: 90,
  },
  title: {
    color: C.ink,
    fontSize: 24,
    fontWeight: "700",
    textAlign: "center",
  },
  note: {
    color: C.ink,
    fontSize: 16,
    lineHeight: 22,
    textAlign: "center",
  },
  button: {
    alignSelf: "stretch",
    minHeight: 52,
    marginTop: 6,
    borderRadius: R.control,
    backgroundColor: C.brand,
    alignItems: "center",
    justifyContent: "center",
  },
  buttonPressed: {
    backgroundColor: C.brandDark,
  },
  buttonText: {
    color: C.surface,
    fontSize: 17,
    fontWeight: "700",
  },
  hint: {
    color: C.ink2Soft,
    fontSize: 13,
    lineHeight: 18,
    textAlign: "center",
  },
});
