// Shared sidebar drawer used by both the Shop (/pos) screen and the
// Admin (/admin) screen.  Renders the same avatar + branch chip +
// scrollable nav items + logout + version footer no matter which
// screen opens it.
//
// Why a shared component: the user reported the hamburger behaviour
// was inconsistent between screens (Shop → jumped to /admin/reports,
// Admin → opened its own modal).  This drawer makes both screens use
// the exact same UI; the only difference is what `onNavigate(key)`
// does — Shop pushes to /admin for non-shop keys and closes for
// "shop"; Admin sets its section state for non-shop keys and pops
// the stack back to /pos for "shop".

import React from "react";
import {
  Image,
  Modal,
  ScrollView,
  StyleSheet,
  Text,
  TouchableOpacity,
  View,
} from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import * as Updates from "expo-updates";
import { C } from "../lib/theme";

export type SidebarItem = {
  key: string;
  label: string;
  icon: any;
  adminOnly?: boolean;
};

// Single source of truth for the menu.  Keep in sync with admin.tsx's
// internal `allItems` list (admin section keys must match).
// All tabs are visible to both roles; cashier-vs-admin gating happens at
// the action level (e.g. product add/edit/delete is hidden for cashier).
export const SIDEBAR_ITEMS: SidebarItem[] = [
  { key: "shop", label: "Shop", icon: "home-outline" },
  { key: "transactions", label: "Transactions", icon: "swap-horizontal-outline" },
  { key: "reports", label: "Reports", icon: "pie-chart-outline" },
  { key: "inventory", label: "Inventory", icon: "cube-outline" },
  { key: "customers", label: "Customers", icon: "people-outline" },
  { key: "products", label: "Products", icon: "gift-outline" },
  { key: "drawer", label: "Drawer", icon: "calculator-outline" },
  { key: "settings", label: "Settings", icon: "settings-outline" },
];

type Props = {
  visible: boolean;
  onClose: () => void;
  staff: string;
  role: string;
  branchName?: string;
  // null means we're on Shop (no admin section is "active")
  activeKey: string | null;
  onNavigate: (key: string) => void;
  onLogout: () => void;
};

export function SidebarDrawer({
  visible,
  onClose,
  staff,
  role,
  branchName,
  activeKey,
  onNavigate,
  onLogout,
}: Props) {
  const insets = useSafeAreaInsets();
  const isAdmin = (role || "").toLowerCase() === "admin";
  const items = SIDEBAR_ITEMS.filter((it) => !it.adminOnly || isAdmin);

  return (
    <Modal
      visible={visible}
      animationType="slide"
      transparent
      onRequestClose={onClose}
    >
      <View style={s.overlay}>
        {/* Direct row child → cross-axis stretch gives full height; the
            ScrollView inside finally has somewhere to grow. */}
        <View
          style={[
            s.panel,
            { paddingTop: 20 + insets.top, paddingBottom: 20 + insets.bottom },
          ]}
          testID="sidebar-drawer"
        >
          <View style={s.avatarBox}>
            <View style={s.badge}>
              <Image
                source={require("../assets/images/rolling-pinn-logo.png")}
                style={s.badgeLogo}
                resizeMode="contain"
              />
            </View>
            <Text style={s.avatarText} numberOfLines={1}>{staff || "Admin"}</Text>
            {!!branchName && (
              <View style={s.branchChip}>
                <Ionicons name="storefront-outline" size={12} color={C.surface} />
                <Text style={s.branchChipText} numberOfLines={1}>
                  {branchName}
                </Text>
              </View>
            )}
          </View>

          <ScrollView style={{ flex: 1 }} contentContainerStyle={{ paddingBottom: 4 }}>
            {items.map((it) => (
              <TouchableOpacity
                key={it.key}
                style={[s.item, activeKey === it.key && s.itemActive]}
                onPress={() => onNavigate(it.key)}
                testID={`side-${it.key}`}
              >
                <Ionicons
                  name={it.icon}
                  size={20}
                  color={activeKey === it.key ? C.brand : "rgba(255,255,255,0.85)"}
                />
                <Text
                  style={[s.label, activeKey === it.key && s.labelActive]}
                >
                  {it.label}
                </Text>
              </TouchableOpacity>
            ))}
          </ScrollView>

          <TouchableOpacity
            style={s.logoutBtn}
            onPress={onLogout}
            testID="sidebar-logout"
          >
            <Ionicons name="log-out-outline" size={18} color={C.surface} />
            <Text style={s.logoutText}>Log out</Text>
          </TouchableOpacity>

          <View style={s.footer}>
            <Ionicons name="refresh-circle-outline" size={14} color="rgba(255,255,255,0.6)" />
            <Text style={s.footerDate}>
              {new Date().toLocaleDateString("en-GB", {
                day: "2-digit",
                month: "short",
                year: "numeric",
              })}{" "}
              {new Date().toLocaleTimeString("en-GB", {
                hour: "2-digit",
                minute: "2-digit",
              })}
            </Text>
          </View>
          <Text style={s.version}>
            Version{" "}
            {require("expo-constants").default.expoConfig?.version || "?"}
          </Text>
          {/* OTA marker — short, last 6 chars of the active update ID, or
              "embedded" when running the JS bundled in the APK with no
              OTA applied yet.  Use this to confirm whether the device is
              actually on the latest OTA push or still on the embedded
              bundle / a stale OTA. */}
          <Text style={s.version}>
            {(() => {
              try {
                if (Updates.isEmbeddedLaunch) return "Build: embedded (no OTA)";
                const id = Updates.updateId;
                if (!id) return "Build: embedded";
                return `OTA: …${id.slice(-6)}`;
              } catch {
                return "OTA: ?";
              }
            })()}
          </Text>
        </View>

        {/* Tap outside the panel to dismiss */}
        <TouchableOpacity
          style={{ flex: 1 }}
          activeOpacity={1}
          onPress={onClose}
        />
      </View>
    </Modal>
  );
}

const s = StyleSheet.create({
  overlay: {
    flex: 1,
    flexDirection: "row",
    backgroundColor: C.scrim,
  },
  panel: {
    width: 248,
    backgroundColor: C.brand,
    paddingHorizontal: 12,
  },
  avatarBox: { alignItems: "center", marginBottom: 16 },
  badge: {
    width: 52,
    height: 52,
    borderRadius: 15,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: C.surface,
    padding: 6,
  },
  badgeLogo: { width: "100%", height: "100%" },
  avatarText: { fontSize: 14, color: C.surface, marginTop: 8, fontWeight: "700" },
  branchChip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 8,
    paddingVertical: 3,
    backgroundColor: "rgba(255,255,255,0.18)",
    borderRadius: 12,
    marginTop: 6,
    maxWidth: 200,
  },
  branchChipText: { fontSize: 11, color: C.surface, fontWeight: "600" },
  item: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    padding: 12,
    borderRadius: 10,
    marginBottom: 4,
  },
  itemActive: { backgroundColor: C.surface },
  label: { fontSize: 14, color: "rgba(255,255,255,0.85)", fontWeight: "500" },
  labelActive: { color: C.brand, fontWeight: "700" },
  logoutBtn: {
    flexDirection: "row",
    gap: 8,
    padding: 12,
    alignItems: "center",
  },
  logoutText: { color: C.surface, fontSize: 13, fontWeight: "600" },
  footer: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 12,
    paddingTop: 6,
    paddingBottom: 2,
    borderTopWidth: 1,
    borderTopColor: "rgba(255,255,255,0.18)",
  },
  footerDate: { fontSize: 10, color: "rgba(255,255,255,0.6)" },
  version: { fontSize: 10, color: "rgba(255,255,255,0.5)", textAlign: "center", marginTop: 4 },
});
