// The navy navigation rail, in its two forms.
//
//   <NavRail>       195px, always on screen. Tablet only.
//   <NavDrawer>     the same rail as a slide-over modal. Phone only.
//
// Both render `RailBody`, so an item added to SIDEBAR_ITEMS appears in both
// without a second edit — the drawer/rail split was previously the source of
// the two screens disagreeing about what the hamburger did.
//
// Screens decide *what* navigating means (Sale pushes to /admin, Admin sets
// its own section state) by passing `onNavigate`; the rail only reports which
// key was tapped.

import React, { useEffect, useRef } from "react";
import {
  Animated,
  Image,
  Modal,
  ScrollView,
  StyleSheet,
  Text,
  TouchableOpacity,
  useWindowDimensions,
  View,
} from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import * as Updates from "expo-updates";
import { C, R } from "../lib/theme";
import { t as tr, useT } from "../lib/i18n";

export type SidebarItem = {
  key: string;
  /** i18n key, not display text — resolved at render so the menu follows the
   *  language. Holding the English word here would freeze it at import time,
   *  because this array is built once when the module loads. */
  labelKey: string;
  icon: any;
  adminOnly?: boolean;
};

// Single source of truth for the menu. Keep the keys in sync with admin.tsx's
// section switch — the label is presentation, the key is the contract.
// All tabs are visible to both roles; cashier-vs-admin gating happens at the
// action level (e.g. product add/edit/delete is hidden for cashier).
export const SIDEBAR_ITEMS: SidebarItem[] = [
  { key: "shop", labelKey: "nav.sale", icon: "cart-outline" },
  { key: "transactions", labelKey: "common.orders", icon: "receipt-outline" },
  { key: "customers", labelKey: "nav.customers", icon: "people-outline" },
  { key: "products", labelKey: "common.products", icon: "cube-outline" },
  { key: "inventory", labelKey: "common.stock", icon: "layers-outline" },
  { key: "reports", labelKey: "nav.reports", icon: "bar-chart-outline" },
  { key: "drawer", labelKey: "common.cash", icon: "wallet-outline" },
  { key: "settings", labelKey: "common.settings", icon: "settings-outline" },
];

export const RAIL_WIDTH = 195;
/** Icon-only rail. Collapsing frees ~123px — enough for the category column
 *  to come back beside the grid on a 1200px tablet. */
export const RAIL_COLLAPSED_WIDTH = 72;
/** One duration for every part of the collapse, so the width, the labels and
 *  the chevron all land together instead of arriving in three waves. */
const COLLAPSE_MS = 220;

/** 0 = expanded, 1 = collapsed. Drives width, padding and label opacity. */
function useCollapseAnim(collapsed?: boolean) {
  const t = useRef(new Animated.Value(collapsed ? 1 : 0)).current;
  useEffect(() => {
    Animated.timing(t, {
      toValue: collapsed ? 1 : 0,
      duration: COLLAPSE_MS,
      // Width and padding can't run on the native driver.
      useNativeDriver: false,
    }).start();
  }, [collapsed, t]);
  return t;
}

type BodyProps = {
  staff: string;
  role: string;
  branchName?: string;
  activeKey: string | null;
  onNavigate: (key: string) => void;
  onLogout: () => void;
  /** Drawer shows the build/OTA footer; the always-on rail keeps it for the
   *  user card instead, since staff read the rail all day. */
  showBuildInfo?: boolean;
  /** Icon-only. Labels are dropped, not shrunk — a truncated word is worse
   *  than an icon the eye already knows the position of. */
  collapsed?: boolean;
  /** Renders the collapse control at the top of the rail. */
  onToggleCollapse?: () => void;
};

// First glyph of the staff name — reads as an avatar at a glance and works
// for Thai names, where an English-style two-letter initial does not.
function initial(name: string): string {
  const t = (name || "").trim();
  return t ? Array.from(t)[0].toUpperCase() : "?";
}

function RailBody({
  staff,
  role,
  branchName,
  activeKey,
  onNavigate,
  onLogout,
  showBuildInfo,
  collapsed,
  onToggleCollapse,
  anim,
}: BodyProps & { anim?: Animated.Value }) {
  useT(); // re-render this screen when the language changes
  const isAdmin = (role || "").toLowerCase() === "admin";
  const items = SIDEBAR_ITEMS.filter((it) => !it.adminOnly || isAdmin);
  // A 712px-tall tablet can't fit eight 52px rows plus the logo and user card
  // without clipping one mid-row, which reads as broken rather than scrollable.
  const { height } = useWindowDimensions();
  const dense = height < 800;
  const rowH = dense ? 44 : 52;
  // Labels stay mounted and fade — unmounting them makes the text vanish a
  // frame before the rail starts moving, which reads as a glitch.
  const t = anim ?? new Animated.Value(collapsed ? 1 : 0);
  const labelOpacity = t.interpolate({
    inputRange: [0, 0.55, 1],
    outputRange: [1, 0, 0],
  });
  // Collapsed, a row is RAIL_COLLAPSED_WIDTH minus the list's own padding —
  // 48px. Centring a 21px icon in that needs 13.5, not the 25 that was pushing
  // every glyph right of its own highlight.
  const rowPadLeft = t.interpolate({
    inputRange: [0, 1],
    outputRange: [16, (RAIL_COLLAPSED_WIDTH - 24 - 21) / 2],
  });
  // Same sum for the 18px collapse chevron.
  const chevronPadLeft = t.interpolate({
    inputRange: [0, 1],
    outputRange: [12, (RAIL_COLLAPSED_WIDTH - 24 - 18) / 2],
  });
  // ...and for the 34px logo tile, which spans the full rail (no list padding).
  const logoPadLeft = t.interpolate({
    inputRange: [0, 1],
    outputRange: [18, (RAIL_COLLAPSED_WIDTH - 34) / 2],
  });

  return (
    <>
      <Animated.View
        style={[
          s.logo,
          { paddingLeft: logoPadLeft },
          dense && { minHeight: 76, paddingVertical: 10 },
        ]}
      >
        <View style={s.logoTile}>
          <Image
            source={require("../assets/images/icon.png")}
            style={s.logoImg}
            resizeMode="cover"
          />
        </View>
        <Animated.View
          style={{ flex: 1, minWidth: 0, opacity: labelOpacity }}
          pointerEvents={collapsed ? "none" : "auto"}
        >
          <Text style={s.logoName} numberOfLines={1}>
            Rolling Pinn
          </Text>
          {!!branchName && (
            <Text style={s.logoBranch} numberOfLines={1}>
              {branchName}
            </Text>
          )}
        </Animated.View>
      </Animated.View>

      {!!onToggleCollapse && (
        <TouchableOpacity onPress={onToggleCollapse} activeOpacity={0.8} testID="rail-collapse">
          <Animated.View style={[s.collapseBtn, { paddingLeft: chevronPadLeft }]}>
          <Animated.View
            style={{
              transform: [
                {
                  rotate: t.interpolate({
                    inputRange: [0, 1],
                    outputRange: ["0deg", "180deg"],
                  }),
                },
              ],
            }}
          >
            <Ionicons name="chevron-back" size={18} color={C.navIcon} />
          </Animated.View>
            <Animated.Text
              style={[s.collapseText, { opacity: labelOpacity }]}
              numberOfLines={1}
            >
              {tr("nav.collapse")}
            </Animated.Text>
          </Animated.View>
        </TouchableOpacity>
      )}

      <ScrollView
        style={{ flex: 1 }}
        contentContainerStyle={[s.navList, dense && { paddingTop: 10, gap: 2 }]}
        showsVerticalScrollIndicator={false}
      >
        {items.map((it) => {
          const on = activeKey === it.key;
          return (
            <TouchableOpacity
              key={it.key}
              onPress={() => onNavigate(it.key)}
              activeOpacity={0.8}
              testID={`side-${it.key}`}
            >
              <Animated.View
                style={[s.nv, { height: rowH, paddingLeft: rowPadLeft }, on && s.nvOn]}
              >
                <Ionicons
                  name={it.icon}
                  size={21}
                  color={on ? C.surface : C.navIcon}
                />
                <Animated.Text
                  style={[s.nvText, on && s.nvTextOn, { opacity: labelOpacity }]}
                  numberOfLines={1}
                >
                  {tr(it.labelKey)}
                </Animated.Text>
              </Animated.View>
            </TouchableOpacity>
          );
        })}
      </ScrollView>

      <View style={s.foot}>
        <View style={s.sep} />
        <TouchableOpacity
          onPress={onLogout}
          activeOpacity={0.8}
          testID="sidebar-logout"
        >
          <Animated.View style={[s.nv, { height: rowH, paddingLeft: rowPadLeft }]}>
            <Ionicons name="log-out-outline" size={21} color={C.navIcon} />
            <Animated.Text style={[s.nvText, { opacity: labelOpacity }]} numberOfLines={1}>
              {tr("nav.logout")}
            </Animated.Text>
          </Animated.View>
        </TouchableOpacity>

        <View
          style={[
            s.userCard,
            dense && { padding: 9, marginTop: 8 },
            collapsed && s.userCardCollapsed,
          ]}
        >
          <View style={s.av}>
            <Text style={s.avText}>{initial(staff)}</Text>
          </View>
          <Animated.View
            style={{ flex: 1, minWidth: 0, opacity: labelOpacity }}
            pointerEvents={collapsed ? "none" : "auto"}
          >
            <Text style={s.userName} numberOfLines={1}>
              {staff || tr("common.admin")}
            </Text>
            <Text style={s.userRole} numberOfLines={1}>
              {isAdmin ? tr("common.admin") : tr("common.cashier")}
            </Text>
          </Animated.View>
        </View>

        {showBuildInfo && !collapsed && <BuildInfo />}
      </View>
    </>
  );
}

// Which bundle is this device actually running? Without it, "did the OTA land"
// is unanswerable from the shop floor.
function BuildInfo() {
  const version =
    require("expo-constants").default.expoConfig?.version || "?";
  let ota = "OTA: ?";
  try {
    if (Updates.isEmbeddedLaunch) ota = "embedded (no OTA)";
    else ota = Updates.updateId ? `OTA …${Updates.updateId.slice(-6)}` : "embedded";
  } catch {
    // Updates is unavailable in Expo Go / web — leave the fallback.
  }
  return (
    <Text style={s.build}>
      v{version} · {ota}
    </Text>
  );
}

/** Always-on rail. Render as the first child of a row-direction screen. */
export function NavRail(props: BodyProps) {
  const insets = useSafeAreaInsets();
  const t = useCollapseAnim(props.collapsed);
  const width = t.interpolate({
    inputRange: [0, 1],
    outputRange: [RAIL_WIDTH, RAIL_COLLAPSED_WIDTH],
  });
  return (
    <Animated.View
      style={[
        s.rail,
        { width, paddingTop: insets.top, paddingBottom: insets.bottom },
      ]}
      testID="nav-rail"
    >
      <RailBody {...props} anim={t} />
    </Animated.View>
  );
}

/** Phone form — the same rail, slid over the screen. */
export function NavDrawer({
  visible,
  onClose,
  ...props
}: BodyProps & { visible: boolean; onClose: () => void }) {
  const insets = useSafeAreaInsets();
  return (
    <Modal
      visible={visible}
      animationType="slide"
      transparent
      onRequestClose={onClose}
    >
      <View style={s.overlay}>
        <View
          style={[
            s.rail,
            s.drawerPanel,
            { paddingTop: insets.top, paddingBottom: insets.bottom },
          ]}
          testID="sidebar-drawer"
        >
          <RailBody {...props} showBuildInfo />
        </View>
        <TouchableOpacity style={{ flex: 1 }} activeOpacity={1} onPress={onClose} />
      </View>
    </Modal>
  );
}

const s = StyleSheet.create({
  rail: {
    width: RAIL_WIDTH,
    flexShrink: 0,
    backgroundColor: C.nav,
    // Clips the labels as the rail narrows, so they slide out of view rather
    // than reflowing into the icons.
    overflow: "hidden",
  },
  drawerPanel: { width: 232 },
  overlay: { flex: 1, flexDirection: "row", backgroundColor: C.scrim },

  logo: {
    minHeight: 96,
    backgroundColor: C.navDark,
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingRight: 18,
    paddingVertical: 14,
    overflow: "hidden",
  },
  logoTile: {
    width: 34,
    height: 34,
    borderRadius: 9,
    overflow: "hidden",
    backgroundColor: "rgba(255,255,255,0.14)",
  },
  logoImg: { width: "100%", height: "100%" },
  collapseBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    marginHorizontal: 12,
    marginTop: 10,
    height: 36,
    paddingRight: 12,
    borderRadius: 9,
    backgroundColor: "rgba(255,255,255,0.06)",
    overflow: "hidden",
  },
  collapseText: { color: C.navIcon, fontSize: 12.5, fontWeight: "600" },
  logoName: {
    fontSize: 16,
    fontWeight: "800",
    color: C.surface,
    letterSpacing: -0.3,
  },
  logoBranch: { fontSize: 11.5, color: C.navMuted, marginTop: 2 },

  navList: { paddingHorizontal: 12, paddingTop: 16, gap: 4 },
  nv: {
    height: 52,
    borderRadius: 12,
    flexDirection: "row",
    alignItems: "center",
    gap: 14,
    paddingRight: 16,
    overflow: "hidden",
  },
  // With no label to read, the highlight is the only thing saying where you
  // are — so it gets a lift off the navy rather than a flat fill.
  nvOn: {
    backgroundColor: C.brand,
    shadowColor: C.brand,
    shadowOffset: { width: 0, height: 4 },
    shadowOpacity: 0.45,
    shadowRadius: 10,
    elevation: 4,
  },
  nvText: {
    fontSize: 15,
    fontWeight: "500",
    color: C.navText,
    letterSpacing: -0.15,
    // Fixed, not flexible — a shrinking label would re-wrap on every frame of
    // the animation instead of being clipped cleanly by the rail.
    width: RAIL_WIDTH - 16 - 21 - 14 - 16,
  },
  nvTextOn: { color: C.surface, fontWeight: "700" },

  foot: { paddingHorizontal: 12, paddingBottom: 18 },
  sep: {
    height: 1,
    backgroundColor: "rgba(255,255,255,0.12)",
    marginHorizontal: 4,
    marginBottom: 10,
  },
  userCard: {
    backgroundColor: C.navDark,
    borderRadius: R.control,
    padding: 12,
    flexDirection: "row",
    alignItems: "center",
    gap: 11,
    marginTop: 14,
  },
  av: {
    width: 38,
    height: 38,
    borderRadius: 19,
    backgroundColor: C.brand,
    alignItems: "center",
    justifyContent: "center",
  },
  avText: { color: C.surface, fontSize: 15, fontWeight: "700" },
  userCardCollapsed: { padding: 7, justifyContent: "center", gap: 0 },
  userName: {
    color: C.surface,
    fontSize: 13.5,
    fontWeight: "700",
    letterSpacing: -0.15,
  },
  userRole: { color: C.navMuted, fontSize: 12, marginTop: 2 },

  build: {
    fontSize: 10,
    color: "rgba(255,255,255,0.45)",
    textAlign: "center",
    marginTop: 10,
  },
});
