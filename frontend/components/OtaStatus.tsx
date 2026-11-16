/**
 * What the till shows while an over-the-air update comes in (lib/otaAutoUpdate).
 *
 *  - Downloading / ready: a small note at the top of the screen. It never
 *    takes a tap (pointerEvents none), so a cashier mid-sale carries on.
 *  - Installing: a full-screen "Updating Brave POS…" for the moment before
 *    the restart, so a till going blank is not mistaken for a crash.
 *
 * Mounted once in the root layout, above the stack.
 */
import { useEffect, useState } from "react";
import { ActivityIndicator, StyleSheet, Text, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { getOtaStatus, subscribeOtaStatus, type OtaStatus as Status } from "../lib/otaAutoUpdate";
import { useT } from "../lib/i18n";
import { C, R } from "../lib/theme";

export function OtaStatus() {
  const { t } = useT();
  const insets = useSafeAreaInsets();
  const [status, setStatus] = useState<Status>(getOtaStatus());
  useEffect(() => subscribeOtaStatus(setStatus), []);

  if (status === "idle") return null;

  if (status === "installing") {
    return (
      <View style={s.cover} testID="ota-installing">
        <ActivityIndicator size="large" color={C.surface} />
        <Text style={s.coverTitle}>{t("ota.installing")}</Text>
        <Text style={s.coverNote}>{t("ota.installing_note")}</Text>
      </View>
    );
  }

  return (
    <View pointerEvents="none" style={[s.pillWrap, { top: insets.top + 8 }]} testID="ota-status">
      <View style={s.pill}>
        {status === "downloading" && <ActivityIndicator size="small" color={C.surface} />}
        <Text style={s.pillText}>
          {status === "downloading" ? t("ota.downloading") : t("ota.ready")}
        </Text>
      </View>
    </View>
  );
}

const s = StyleSheet.create({
  pillWrap: {
    position: "absolute",
    left: 0,
    right: 0,
    alignItems: "center",
    zIndex: 1000,
    elevation: 1000,
  },
  pill: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    maxWidth: 520,
    backgroundColor: C.inkStrong,
    opacity: 0.92,
    borderRadius: R.control,
    paddingVertical: 8,
    paddingHorizontal: 14,
  },
  pillText: {
    color: C.surface,
    fontSize: 13,
    fontWeight: "600",
  },
  cover: {
    position: "absolute", top: 0, left: 0, right: 0, bottom: 0,
    backgroundColor: C.inkStrong,
    alignItems: "center",
    justifyContent: "center",
    gap: 12,
    zIndex: 2000,
    elevation: 2000,
  },
  coverTitle: {
    color: C.surface,
    fontSize: 22,
    fontWeight: "700",
  },
  coverNote: {
    color: C.surface,
    opacity: 0.8,
    fontSize: 15,
  },
});
