// Shared UI primitives for the POS design system.
//
// Every screen composes from these rather than restyling a View each time, so
// a tag is the same tag on Orders and on Products, and changing a card radius
// is one edit here instead of forty across two 5,000-line screens.
//
// Nothing in this file knows about POS data — these are presentation shells.
// Anything that fetches, totals, or mutates belongs in the screen.

import React from "react";
import {
  ActivityIndicator,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  View,
  ViewStyle,
  TextStyle,
  StyleProp,
} from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { C, MONO, R } from "./theme";
import { t as tr } from "./i18n";

// ── Figures ────────────────────────────────────────────────────────────────
// Money, counts, times, reference numbers. Anything that sits in a column and
// should not shift as its digits change.

export function Money({
  children,
  style,
  numberOfLines,
  testID,
}: {
  children: React.ReactNode;
  style?: StyleProp<TextStyle>;
  numberOfLines?: number;
  testID?: string;
}) {
  return (
    <Text style={[MONO, style]} numberOfLines={numberOfLines} testID={testID}>
      {children}
    </Text>
  );
}

// Micro caps label — "AMOUNT DUE", "USUAL ORDER". Sets the register for a
// block without taking the visual weight a heading would.
export function Lbl({
  children,
  style,
}: {
  children: React.ReactNode;
  style?: StyleProp<TextStyle>;
}) {
  return <Text style={[s.lbl, style]}>{children}</Text>;
}

export function Spacer() {
  return <View style={{ flex: 1 }} />;
}

// ── Tag ────────────────────────────────────────────────────────────────────
// Status pills. The tone carries the meaning, so callers pass intent
// ("refunded") rather than a colour.

export type TagTone = "ok" | "low" | "out" | "info" | "purple" | "red";

const TAG_TONES: Record<TagTone, { bg: string; fg: string }> = {
  ok: { bg: C.okTint, fg: C.okDark },
  low: { bg: C.warnTint, fg: C.warnDark },
  out: { bg: C.neutralTint, fg: C.ink2Soft },
  info: { bg: C.brandTintSoft, fg: C.brand },
  purple: { bg: C.accentTint, fg: C.accentDark },
  red: { bg: C.dangerTint, fg: C.dangerDark },
};

export function Tag({
  children,
  tone = "info",
  icon,
  style,
  mono,
}: {
  children: React.ReactNode;
  tone?: TagTone;
  icon?: any;
  style?: StyleProp<ViewStyle>;
  mono?: boolean;
}) {
  const t = TAG_TONES[tone];
  return (
    <View style={[s.tag, { backgroundColor: t.bg }, style]}>
      {!!icon && <Ionicons name={icon} size={12} color={t.fg} />}
      <Text style={[s.tagText, mono && MONO, { color: t.fg }]}>{children}</Text>
    </View>
  );
}

// ── Button ─────────────────────────────────────────────────────────────────
// `blue` is the one commit action per screen. `navy` is an escape hatch that
// must not be hit by reflex (manual override, force-close). `red` destroys.

export type BtnVariant = "default" | "blue" | "red" | "navy" | "ghost";

export function Btn({
  label,
  icon,
  iconRight,
  variant = "default",
  onPress,
  disabled,
  busy,
  height = 48,
  style,
  textStyle,
  testID,
}: {
  label?: string;
  icon?: any;
  iconRight?: any;
  variant?: BtnVariant;
  onPress?: () => void;
  disabled?: boolean;
  busy?: boolean;
  height?: number;
  style?: StyleProp<ViewStyle>;
  textStyle?: StyleProp<TextStyle>;
  testID?: string;
}) {
  const v = BTN_VARIANTS[variant];
  const dim = disabled || busy;
  return (
    <TouchableOpacity
      style={[
        s.btn,
        { height, borderRadius: height >= 60 ? R.card : R.control },
        { backgroundColor: v.bg, borderColor: v.border },
        dim && { opacity: 0.5 },
        style,
      ]}
      onPress={onPress}
      disabled={dim}
      activeOpacity={0.75}
      testID={testID}
    >
      {busy ? (
        <ActivityIndicator color={v.fg} size="small" />
      ) : (
        <>
          {!!icon && <Ionicons name={icon} size={18} color={v.fg} />}
          {!!label && (
            <Text style={[s.btnText, { color: v.fg }, textStyle]}>{label}</Text>
          )}
          {!!iconRight && <Ionicons name={iconRight} size={18} color={v.fg} />}
        </>
      )}
    </TouchableOpacity>
  );
}

const BTN_VARIANTS: Record<BtnVariant, { bg: string; border: string; fg: string }> = {
  default: { bg: C.surface, border: C.line, fg: C.ink2 },
  blue: { bg: C.brand, border: C.brand, fg: C.surface },
  red: { bg: C.surface, border: C.dangerSoft, fg: C.danger },
  navy: { bg: C.navDark, border: C.navDark, fg: C.surface },
  ghost: { bg: "transparent", border: "transparent", fg: C.ink2 },
};

// Square icon-only button used in the top bar.
export function IconBtn({
  icon,
  onPress,
  size = 52,
  color = C.ink2,
  style,
  testID,
}: {
  icon: any;
  onPress?: () => void;
  size?: number;
  color?: string;
  style?: StyleProp<ViewStyle>;
  testID?: string;
}) {
  return (
    <TouchableOpacity
      style={[s.iconBtn, { width: size, height: size }, style]}
      onPress={onPress}
      activeOpacity={0.7}
      testID={testID}
    >
      <Ionicons name={icon} size={Math.round(size * 0.42)} color={color} />
    </TouchableOpacity>
  );
}

// ── Panel ──────────────────────────────────────────────────────────────────
// The white card everything else sits inside.

export function Panel({
  children,
  style,
  testID,
}: {
  children?: React.ReactNode;
  style?: StyleProp<ViewStyle>;
  testID?: string;
}) {
  return (
    <View style={[s.panel, style]} testID={testID}>
      {children}
    </View>
  );
}

export function PanelHead({
  title,
  note,
  right,
  style,
}: {
  title: React.ReactNode;
  note?: string;
  right?: React.ReactNode;
  style?: StyleProp<ViewStyle>;
}) {
  return (
    <View style={[s.panelHead, style]}>
      {typeof title === "string" ? (
        <Text style={s.panelTitle}>{title}</Text>
      ) : (
        title
      )}
      {!!note && <Text style={s.panelNote}>{note}</Text>}
      <Spacer />
      {right}
    </View>
  );
}

// ── Pill ───────────────────────────────────────────────────────────────────
// Filter row above a table — Today / This week / Custom.

export function Pill({
  label,
  active,
  onPress,
  testID,
}: {
  label: string;
  active?: boolean;
  onPress?: () => void;
  testID?: string;
}) {
  return (
    <TouchableOpacity
      style={[s.pill, active && s.pillOn]}
      onPress={onPress}
      activeOpacity={0.75}
      testID={testID}
    >
      <Text style={[s.pillText, active && s.pillTextOn]}>{label}</Text>
    </TouchableOpacity>
  );
}

// ── Toggle ─────────────────────────────────────────────────────────────────

export function Toggle({
  on,
  onPress,
  disabled,
  testID,
}: {
  on?: boolean;
  onPress?: () => void;
  disabled?: boolean;
  testID?: string;
}) {
  return (
    <TouchableOpacity
      style={[s.tog, on && s.togOn, disabled && { opacity: 0.45 }]}
      onPress={onPress}
      disabled={disabled}
      activeOpacity={0.8}
      testID={testID}
    >
      <View style={[s.togKnob, on && s.togKnobOn]} />
    </TouchableOpacity>
  );
}

// ── Search field ───────────────────────────────────────────────────────────
// `rounded` is the sale-screen search that spans the top bar; the squared
// version is the one that sits inside a panel header.

export function SearchField({
  value,
  onChangeText,
  placeholder,
  rounded,
  height = 48,
  style,
  onSubmitEditing,
  autoFocus,
  testID,
}: {
  value?: string;
  onChangeText?: (t: string) => void;
  placeholder?: string;
  rounded?: boolean;
  height?: number;
  style?: StyleProp<ViewStyle>;
  onSubmitEditing?: () => void;
  autoFocus?: boolean;
  testID?: string;
}) {
  return (
    <View
      style={[
        s.search,
        { height, borderRadius: rounded ? height / 2 : R.control },
        style,
      ]}
    >
      <Ionicons name="search" size={18} color={C.ink3} />
      <TextInput
        style={s.searchInput}
        value={value}
        onChangeText={onChangeText}
        placeholder={placeholder}
        placeholderTextColor={C.ink3}
        onSubmitEditing={onSubmitEditing}
        autoFocus={autoFocus}
        returnKeyType="search"
        testID={testID}
      />
      {!!value && !!onChangeText && (
        <TouchableOpacity onPress={() => onChangeText("")} hitSlop={8}>
          <Ionicons name="close-circle" size={18} color={C.ink3} />
        </TouchableOpacity>
      )}
    </View>
  );
}

// ── Stat card ──────────────────────────────────────────────────────────────
// A figure is not actionable without its comparison, so `delta` is a first
// class prop rather than something callers bolt on underneath.

export function Stat({
  icon,
  tint,
  tintFg,
  label,
  value,
  delta,
  deltaDir,
  spark,
  sparkAccent,
  dense,
  style,
}: {
  icon: any;
  tint?: string;
  tintFg?: string;
  label: string;
  value: string;
  delta?: string;
  deltaDir?: "up" | "down" | "flat";
  spark?: number[];
  sparkAccent?: boolean;
  /** Short viewports: shrink the tile and drop the sparkline so a row of
   *  stats plus a chart still fits without scrolling. */
  dense?: boolean;
  style?: StyleProp<ViewStyle>;
}) {
  const dCol =
    deltaDir === "up" ? C.ok : deltaDir === "down" ? C.danger : C.ink2Soft;
  return (
    <View style={[s.stat, dense && { padding: 14 }, style]}>
      <View
        style={[
          s.statIcon,
          { backgroundColor: tint || C.brandTintSoft },
          dense && { width: 34, height: 34, borderRadius: 10, marginBottom: 10 },
        ]}
      >
        <Ionicons name={icon} size={dense ? 17 : 21} color={tintFg || C.brand} />
      </View>
      <Text style={[s.statLabel, dense && { fontSize: 13 }]}>{label}</Text>
      <Text style={[s.statValue, MONO, dense && { fontSize: 23, marginTop: 4 }]}>
        {value}
      </Text>
      {!!delta && (
        <View style={s.statDelta}>
          {deltaDir && deltaDir !== "flat" && (
            <Ionicons
              name={deltaDir === "up" ? "arrow-up" : "arrow-down"}
              size={13}
              color={dCol}
            />
          )}
          <Text style={[s.statDeltaText, { color: dCol }]}>{delta}</Text>
        </View>
      )}
      {!dense && !!spark && spark.length > 0 && (
        <Sparkline data={spark} accent={sparkAccent} />
      )}
    </View>
  );
}

// Micro bar chart. Only the headline metric should pass `accent` — if every
// card is blue the eye has nowhere to land first.
export function Sparkline({
  data,
  accent,
  height = 38,
}: {
  data: number[];
  accent?: boolean;
  height?: number;
}) {
  const max = Math.max(...data, 1);
  const last = data.length - 1;
  return (
    <View style={[s.spark, { height }]}>
      {data.map((v, i) => (
        <View
          key={i}
          style={{
            flex: 1,
            height: Math.max(2, (v / max) * height),
            borderRadius: 2,
            backgroundColor: accent
              ? i === last
                ? C.brand
                : C.brandTint
              : i === last
                ? C.ink3
                : C.line,
          }}
        />
      ))}
    </View>
  );
}

// ── Mix row ────────────────────────────────────────────────────────────────
// Payment mix, expense breakdown — a labelled proportion bar.

export function MixRow({
  label,
  value,
  pct,
  color,
}: {
  label: string;
  value: string;
  pct: number;
  color?: string;
}) {
  return (
    <View style={s.mixRow}>
      <View style={s.mixTop}>
        <Text style={s.mixLabel}>{label}</Text>
        <Text style={[s.mixValue, MONO]}>{value}</Text>
      </View>
      <View style={s.track}>
        <View
          style={{
            width: `${Math.max(0, Math.min(100, pct))}%`,
            height: "100%",
            borderRadius: 5,
            backgroundColor: color || C.brand,
          }}
        />
      </View>
    </View>
  );
}

// ── Table ──────────────────────────────────────────────────────────────────
// RN has no <table>, so these are flex rows sharing one column spec. Callers
// define widths once and pass the same array to head and body.

export type Col = {
  key: string;
  /** i18n key (e.g. "admin.col_product"), resolved by THead. Column arrays are
   *  module constants, so a literal here would be captured at import time and
   *  never follow a language change. Plain text still renders as-is — an
   *  unknown key falls through to itself (see lib/i18n.ts). */
  title: string;
  // flex OR width — width wins when both are given.
  flex?: number;
  width?: number;
  right?: boolean;
};

export function THead({ cols }: { cols: Col[] }) {
  return (
    <View style={s.thead}>
      {cols.map((c) => (
        <Text
          key={c.key}
          style={[
            s.th,
            c.width ? { width: c.width } : { flex: c.flex ?? 1 },
            c.right && { textAlign: "right" },
          ]}
          numberOfLines={1}
        >
          {tr(c.title)}
        </Text>
      ))}
    </View>
  );
}

export function TRow({
  children,
  selected,
  onPress,
  dim,
  last,
  testID,
}: {
  children: React.ReactNode;
  selected?: boolean;
  onPress?: () => void;
  dim?: boolean;
  last?: boolean;
  testID?: string;
}) {
  return (
    <TouchableOpacity
      style={[
        s.trow,
        selected && { backgroundColor: C.brandTintSoft },
        last && { borderBottomWidth: 0 },
        dim && { opacity: 0.6 },
      ]}
      onPress={onPress}
      disabled={!onPress}
      activeOpacity={0.75}
      testID={testID}
    >
      {children}
    </TouchableOpacity>
  );
}

export function TCell({
  col,
  children,
  style,
}: {
  col: Col;
  children?: React.ReactNode;
  style?: StyleProp<ViewStyle>;
}) {
  return (
    <View
      style={[
        col.width ? { width: col.width } : { flex: col.flex ?? 1 },
        col.right && { alignItems: "flex-end" },
        { justifyContent: "center" },
        style,
      ]}
    >
      {children}
    </View>
  );
}

// Plain text inside a cell, so callers don't restate the type ramp each time.
export function TText({
  children,
  strong,
  muted,
  mono,
  strike,
  color,
  numberOfLines = 1,
}: {
  children: React.ReactNode;
  strong?: boolean;
  muted?: boolean;
  mono?: boolean;
  strike?: boolean;
  color?: string;
  numberOfLines?: number;
}) {
  return (
    <Text
      numberOfLines={numberOfLines}
      style={[
        s.td,
        mono && MONO,
        strong && { fontWeight: "600", color: C.ink },
        muted && { color: C.ink2Soft },
        strike && { textDecorationLine: "line-through" },
        !!color && { color },
      ]}
    >
      {children}
    </Text>
  );
}

// Ranked list marker — "1", "2", "3" beside a top-products row.
export function Rank({ n }: { n: number }) {
  return (
    <View style={s.rank}>
      <Text style={[s.rankText, MONO]}>{n}</Text>
    </View>
  );
}

// ── Notice ─────────────────────────────────────────────────────────────────
// A standing banner, not a dialog. Facts that stay true stay on screen.

export function Notice({
  tone = "warn",
  icon,
  children,
  right,
  style,
}: {
  tone?: "warn" | "info" | "ok" | "danger";
  icon?: any;
  children: React.ReactNode;
  right?: React.ReactNode;
  style?: StyleProp<ViewStyle>;
}) {
  const map = {
    warn: { bg: C.warnTint, fg: C.warnDark, ic: "warning-outline" },
    info: { bg: C.brandTintSoft, fg: C.brand, ic: "information-circle-outline" },
    ok: { bg: C.okTint, fg: C.okDark, ic: "checkmark-circle-outline" },
    danger: { bg: C.dangerTint, fg: C.dangerDark, ic: "alert-circle-outline" },
  }[tone];
  return (
    <View style={[s.notice, { backgroundColor: map.bg }, style]}>
      <Ionicons name={icon || (map.ic as any)} size={20} color={map.fg} />
      <View style={{ flex: 1 }}>
        {typeof children === "string" ? (
          <Text style={[s.noticeText, { color: map.fg }]}>{children}</Text>
        ) : (
          children
        )}
      </View>
      {right}
    </View>
  );
}

// ── Empty state ────────────────────────────────────────────────────────────

export function Empty({
  icon = "file-tray-outline",
  title,
  note,
  action,
}: {
  icon?: any;
  title: string;
  note?: string;
  action?: React.ReactNode;
}) {
  return (
    <View style={s.empty}>
      <Ionicons name={icon} size={40} color={C.lineStrong} />
      <Text style={s.emptyTitle}>{title}</Text>
      {!!note && <Text style={s.emptyNote}>{note}</Text>}
      {!!action && <View style={{ marginTop: 16 }}>{action}</View>}
    </View>
  );
}

// ── Key/value row ──────────────────────────────────────────────────────────

export function KV({
  k,
  v,
  mono,
  color,
}: {
  k: string;
  v: React.ReactNode;
  mono?: boolean;
  color?: string;
}) {
  return (
    <View style={s.kv}>
      <Text style={s.kvK}>{k}</Text>
      {typeof v === "string" ? (
        <Text style={[s.kvV, mono && MONO, !!color && { color }]}>{v}</Text>
      ) : (
        v
      )}
    </View>
  );
}

const s = StyleSheet.create({
  lbl: {
    fontFamily: "SpaceMono",
    fontSize: 10,
    letterSpacing: 1.3,
    color: C.ink2Soft,
    textTransform: "uppercase",
  },

  tag: {
    flexDirection: "row",
    alignItems: "center",
    gap: 5,
    paddingHorizontal: 10,
    paddingVertical: 5,
    borderRadius: R.pill,
    alignSelf: "flex-start",
  },
  tagText: { fontSize: 12, fontWeight: "600" },

  btn: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 9,
    paddingHorizontal: 20,
    borderWidth: 1,
  },
  btnText: { fontSize: 15, fontWeight: "600", letterSpacing: -0.15 },

  iconBtn: {
    alignItems: "center",
    justifyContent: "center",
    borderRadius: R.card,
    borderWidth: 1,
    borderColor: C.line,
    backgroundColor: C.surface,
  },

  panel: {
    backgroundColor: C.surface,
    borderRadius: R.card,
    borderWidth: 1,
    borderColor: C.line2,
    overflow: "hidden",
  },
  panelHead: {
    height: 60,
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 20,
    gap: 12,
    borderBottomWidth: 1,
    borderBottomColor: C.line2,
  },
  panelTitle: {
    fontSize: 16,
    fontWeight: "700",
    color: C.ink,
    letterSpacing: -0.26,
  },
  panelNote: { fontSize: 13.5, color: C.ink2Soft },

  pill: {
    height: 40,
    paddingHorizontal: 18,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: C.line,
    backgroundColor: C.surface,
    alignItems: "center",
    justifyContent: "center",
  },
  pillOn: { backgroundColor: C.brand, borderColor: C.brand },
  pillText: { fontSize: 14, fontWeight: "600", color: C.ink2 },
  pillTextOn: { color: C.surface },

  tog: {
    width: 46,
    height: 27,
    borderRadius: 14,
    backgroundColor: C.lineStrong,
    justifyContent: "center",
    padding: 3,
  },
  togOn: { backgroundColor: C.brand },
  togKnob: {
    width: 21,
    height: 21,
    borderRadius: 11,
    backgroundColor: C.surface,
  },
  togKnobOn: { alignSelf: "flex-end" },

  search: {
    flexDirection: "row",
    alignItems: "center",
    gap: 11,
    paddingHorizontal: 16,
    borderWidth: 1,
    borderColor: C.line,
    backgroundColor: C.surface,
  },
  searchInput: {
    flex: 1,
    fontSize: 15,
    color: C.ink,
    padding: 0,
    // RN web draws a focus ring that fights the border.
    outlineStyle: "none" as any,
  },

  stat: {
    flex: 1,
    backgroundColor: C.surface,
    borderRadius: R.card,
    borderWidth: 1,
    borderColor: C.line2,
    padding: 20,
  },
  statIcon: {
    width: 44,
    height: 44,
    borderRadius: R.control,
    alignItems: "center",
    justifyContent: "center",
    marginBottom: 16,
  },
  statLabel: { fontSize: 14, color: C.ink2Soft },
  statValue: {
    fontSize: 28,
    fontWeight: "700",
    color: C.ink,
    marginTop: 7,
    letterSpacing: -1,
  },
  statDelta: { flexDirection: "row", alignItems: "center", gap: 5, marginTop: 9 },
  statDeltaText: { fontSize: 13, fontWeight: "600" },
  spark: {
    flexDirection: "row",
    alignItems: "flex-end",
    gap: 3,
    marginTop: 12,
  },

  mixRow: { marginBottom: 18 },
  mixTop: {
    flexDirection: "row",
    justifyContent: "space-between",
    marginBottom: 9,
  },
  mixLabel: { fontSize: 14.5, color: C.ink2 },
  mixValue: { fontSize: 14.5, fontWeight: "700", color: C.ink },
  track: {
    height: 10,
    borderRadius: 5,
    backgroundColor: C.line2,
    overflow: "hidden",
  },

  thead: {
    flexDirection: "row",
    alignItems: "center",
    gap: 14,
    paddingHorizontal: 20,
    paddingVertical: 14,
    backgroundColor: C.sunk,
    borderBottomWidth: 1,
    borderBottomColor: C.line2,
  },
  th: { fontSize: 12.5, fontWeight: "700", color: C.ink2Soft },
  trow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 14,
    paddingHorizontal: 20,
    paddingVertical: 13,
    borderBottomWidth: 1,
    borderBottomColor: C.line2,
    minHeight: 58,
  },
  td: { fontSize: 14.5, color: C.ink2 },

  rank: {
    width: 26,
    height: 26,
    borderRadius: 8,
    backgroundColor: C.sunk,
    alignItems: "center",
    justifyContent: "center",
  },
  rankText: { fontSize: 12, fontWeight: "700", color: C.ink2Soft },

  notice: {
    flexDirection: "row",
    alignItems: "center",
    gap: 14,
    paddingHorizontal: 20,
    paddingVertical: 16,
    borderRadius: 13,
  },
  noticeText: { fontSize: 14.5, lineHeight: 21 },

  empty: { padding: 40, alignItems: "center", justifyContent: "center" },
  emptyTitle: {
    fontSize: 16,
    fontWeight: "600",
    color: C.ink2,
    marginTop: 14,
    textAlign: "center",
  },
  emptyNote: {
    fontSize: 14,
    color: C.ink3,
    marginTop: 6,
    textAlign: "center",
    // Thai has no spaces, so a long phrase cannot wrap at a word boundary and
    // overflows a narrow box instead. Give it room and let it break anywhere.
    maxWidth: 400,
    lineHeight: 20,
    paddingHorizontal: 12,
  },

  kv: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    gap: 12,
    paddingVertical: 6,
  },
  kvK: { fontSize: 14.5, color: C.ink2Soft },
  kvV: { fontSize: 14.5, fontWeight: "600", color: C.ink },
});

export { s as uiStyles };
