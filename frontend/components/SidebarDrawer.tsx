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

export type SidebarItem = {
  key: string;
  label: string;
  icon: any;
  adminOnly?: boolean;
};

// Single source of truth for the menu.  Keep in sync with admin.tsx's
// internal `allItems` list (admin section keys must match).
// Cashier role only sees "Shop" — every other entry is admin-only.
export const SIDEBAR_ITEMS: SidebarItem[] = [
  { key: "shop", label: "Shop", icon: "home-outline" },
  { key: "transactions", label: "Transactions", icon: "swap-horizontal-outline", adminOnly: true },
  { key: "reports", label: "Reports", icon: "pie-chart-outline", adminOnly: true },
  { key: "inventory", label: "Inventory", icon: "cube-outline", adminOnly: true },
  { key: "customers", label: "Customers", icon: "people-outline", adminOnly: true },
  { key: "products", label: "Products", icon: "gift-outline", adminOnly: true },
  { key: "drawer", label: "Drawer", icon: "calculator-outline", adminOnly: true },
  { key: "settings", label: "Settings", icon: "settings-outline", adminOnly: true },
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
            <View style={s.avatarCircle}>
              <Ionicons name="person" size={32} color="#475569" />
            </View>
            <Text style={s.avatarText}>{staff || "Admin"}</Text>
            {!!branchName && (
              <View style={s.branchChip}>
                <Ionicons name="storefront-outline" size={12} color="#00B14F" />
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
                  color={activeKey === it.key ? "#00B14F" : "#475569"}
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
            <Ionicons name="log-out-outline" size={18} color="#EF4444" />
            <Text style={s.logoutText}>Log out</Text>
          </TouchableOpacity>

          <View style={s.footer}>
            <Ionicons name="refresh-circle-outline" size={14} color="#94A3B8" />
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
    backgroundColor: "rgba(15,23,42,0.4)",
  },
  panel: {
    width: 240,
    backgroundColor: "#FFFFFF",
    borderRightWidth: 1,
    borderRightColor: "#E2E8F0",
    paddingHorizontal: 12,
  },
  avatarBox: { alignItems: "center", marginBottom: 16 },
  avatarCircle: {
    width: 56,
    height: 56,
    borderRadius: 28,
    borderWidth: 2,
    borderColor: "#CBD5E1",
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: "#F1F5F9",
  },
  avatarText: { fontSize: 14, color: "#475569", marginTop: 6, fontWeight: "600" },
  branchChip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 8,
    paddingVertical: 3,
    backgroundColor: "#ECFDF5",
    borderRadius: 12,
    marginTop: 6,
    maxWidth: 200,
  },
  branchChipText: { fontSize: 11, color: "#00B14F", fontWeight: "600" },
  item: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    padding: 12,
    borderRadius: 10,
    marginBottom: 4,
  },
  itemActive: { backgroundColor: "#E5F7ED" },
  label: { fontSize: 14, color: "#475569", fontWeight: "500" },
  labelActive: { color: "#00B14F", fontWeight: "700" },
  logoutBtn: {
    flexDirection: "row",
    gap: 8,
    padding: 12,
    alignItems: "center",
  },
  logoutText: { color: "#EF4444", fontSize: 13, fontWeight: "600" },
  footer: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 12,
    paddingTop: 6,
    paddingBottom: 2,
    borderTopWidth: 1,
    borderTopColor: "#F1F5F9",
  },
  footerDate: { fontSize: 10, color: "#94A3B8" },
  version: { fontSize: 10, color: "#CBD5E1", textAlign: "center", marginTop: 4 },
});
