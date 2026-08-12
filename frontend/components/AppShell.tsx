// The frame every screen renders inside: navy rail on the left, white top bar
// across the top, content below.
//
// On tablet the rail is always on screen and the burger is hidden — there is
// nothing to reveal. On phone the rail becomes a slide-over and the burger
// comes back. Screens don't branch on width themselves; they pass content and
// let the shell decide.

import React, { useEffect, useRef } from "react";
import {
  Animated,
  StyleSheet,
  Text,
  TouchableOpacity,
  View,
  useWindowDimensions,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { C } from "../lib/theme";
import { Spacer } from "../lib/ui";
import { NavRail, NavDrawer, RAIL_WIDTH, RAIL_COLLAPSED_WIDTH } from "./NavRail";

/**
 * Tablet threshold. Below this the rail collapses and columns stack.
 *
 * 900 was too high: a Galaxy Tab A9 is ~893 CSS px in landscape, so it fell
 * seven pixels short and got the phone layout — no rail, no cart column — on
 * an actual tablet. 820 clears every tablet we ship to while still leaving
 * landscape phones (~800 and below) on the stacked layout they need.
 */
export const WIDE = 820;

export function useIsWide() {
  const { width } = useWindowDimensions();
  return width >= WIDE;
}

/**
 * Short-viewport flag. Real tablets are ~713-745px tall, not the 1024 the
 * design was drawn at, so the generous vertical rhythm turns every screen
 * into a scroll. Anything that stacks vertically should tighten on this.
 */
export function useDense() {
  const { height } = useWindowDimensions();
  return height < 820;
}

/** How much horizontal room the rail is actually taking right now. */
export function railWidth(isWide: boolean, collapsed?: boolean) {
  if (!isWide) return 0;
  return collapsed ? RAIL_COLLAPSED_WIDTH : RAIL_WIDTH;
}

type NavProps = {
  staff: string;
  role: string;
  branchName?: string;
  activeKey: string | null;
  onNavigate: (key: string) => void;
  onLogout: () => void;
};

export function AppShell({
  nav,
  drawerOpen,
  onDrawerChange,
  railCollapsed,
  onToggleRail,
  children,
  testID,
}: {
  nav: NavProps;
  drawerOpen: boolean;
  onDrawerChange: (open: boolean) => void;
  /** Tablet only: icon-only rail, freeing width for the page's own columns. */
  railCollapsed?: boolean;
  onToggleRail?: () => void;
  children: React.ReactNode;
  testID?: string;
}) {
  const isWide = useIsWide();
  return (
    <SafeAreaView
      style={s.root}
      edges={isWide ? ["top", "bottom"] : ["top", "left", "right", "bottom"]}
      testID={testID}
    >
      <View style={s.row}>
        {isWide && (
          <NavRail
            {...nav}
            collapsed={railCollapsed}
            onToggleCollapse={onToggleRail}
          />
        )}
        <View style={s.main}>{children}</View>
      </View>
      {!isWide && (
        <NavDrawer
          {...nav}
          visible={drawerOpen}
          onClose={() => onDrawerChange(false)}
        />
      )}
    </SafeAreaView>
  );
}

/**
 * The 96px header.
 *
 * Two shapes, matching the design: a *search-led* bar (the Sale screen, where
 * finding a product is the whole job) and a *title-led* bar (every back-office
 * page, where you need to know which page you're on and what it totals).
 */
export function TopBar({
  title,
  subtitle,
  search,
  actions,
  onMenu,
  menuOpen,
  compact,
}: {
  title?: string;
  subtitle?: string;
  /** Rendered between the burger and the actions. Wins over title/subtitle. */
  search?: React.ReactNode;
  actions?: React.ReactNode;
  onMenu?: () => void;
  /** Drives the burger's animation — true when the rail is expanded. */
  menuOpen?: boolean;
  compact?: boolean;
}) {
  return (
    <View style={[s.topbar, compact && { height: 76 }]}>
      {!!onMenu && <MenuButton onPress={onMenu} open={menuOpen} />}

      {search ? (
        <View style={s.searchSlot}>{search}</View>
      ) : (
        <View style={{ minWidth: 0, flexShrink: 1 }}>
          {!!title && (
            <Text style={s.title} numberOfLines={1}>
              {title}
            </Text>
          )}
          {!!subtitle && (
            <Text style={s.sub} numberOfLines={1}>
              {subtitle}
            </Text>
          )}
        </View>
      )}

      <Spacer />
      {!!actions && <View style={s.actions}>{actions}</View>}
    </View>
  );
}

/**
 * The burger. The bars shorten and fan slightly while the rail is open, so the
 * control still reflects state — but it does not rotate: a spinning hamburger
 * reads as "menu opening", which is not what this button does.
 */
function MenuButton({ onPress, open }: { onPress: () => void; open?: boolean }) {
  const t = useRef(new Animated.Value(open ? 1 : 0)).current;
  useEffect(() => {
    Animated.timing(t, {
      toValue: open ? 1 : 0,
      duration: 220,
      useNativeDriver: true,
    }).start();
  }, [open, t]);

  const bar = (i: number) => (
    <Animated.View
      key={i}
      style={[
        s.burgerBar,
        {
          // Middle bar pulls in while the rail is open; the outer two hold.
          width: t.interpolate({
            inputRange: [0, 1],
            outputRange: [18, i === 1 ? 11 : 18],
          }),
        },
      ]}
    />
  );

  return (
    <TouchableOpacity
      style={s.burger}
      onPress={onPress}
      activeOpacity={0.6}
      testID="topbar-menu"
    >
      <Animated.View style={{ alignItems: "flex-start", gap: 4 }}>
        {[0, 1, 2].map(bar)}
      </Animated.View>
    </TouchableOpacity>
  );
}

/** Standard content area under the top bar: grey, padded, gapped. */
export function Body({
  children,
  style,
  testID,
}: {
  children: React.ReactNode;
  style?: any;
  testID?: string;
}) {
  return (
    <View style={[s.body, style]} testID={testID}>
      {children}
    </View>
  );
}

const s = StyleSheet.create({
  root: { flex: 1, backgroundColor: C.bg },
  row: { flex: 1, flexDirection: "row" },
  main: { flex: 1, minWidth: 0, backgroundColor: C.bg },

  topbar: {
    height: 96,
    flexShrink: 0,
    backgroundColor: C.surface,
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 24,
    gap: 16,
    borderBottomWidth: 1,
    borderBottomColor: C.line2,
  },
  burger: {
    width: 40,
    height: 40,
    alignItems: "center",
    justifyContent: "center",
  },
  burgerBar: { height: 2, borderRadius: 1, backgroundColor: C.ink2 },
  searchSlot: { flex: 1, maxWidth: 620 },
  title: { fontSize: 24, fontWeight: "800", color: C.ink, letterSpacing: -0.62 },
  sub: { fontSize: 14, color: C.ink2Soft, marginTop: 3 },
  actions: { flexDirection: "row", alignItems: "center", gap: 12 },

  body: {
    flex: 1,
    minHeight: 0,
    paddingHorizontal: 24,
    paddingTop: 20,
    paddingBottom: 24,
    gap: 16,
  },
});
