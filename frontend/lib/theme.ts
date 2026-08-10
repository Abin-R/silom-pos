// Single source of truth for app colours.
//
// The palette is the approved POS design system: a deep navy rail carrying the
// product identity, a single blue reserved for the one commit action on each
// screen, and white cards floating on a cool grey surface. Green means money
// actually landed, amber means "notice this", red means destructive, purple
// marks refunds so they are never mistaken for voids.
//
// Nothing here is imported for its own sake — screens should reference these
// tokens instead of inlining hex, so a rebrand is one file.

// Values are typed as plain `string` (no `as const`) — literal types would
// pin a variable to whichever token happened to be assigned first.
export const C: Record<string, string> = {
  // Brand — primary actions, active tabs, links, focus rings.
  brand: "#2563EB",
  brandDark: "#1D4ED8",
  brandTint: "#DBE7FE",
  brandTintSoft: "#EFF5FF",
  // Secondary brand accent. Use sparingly, for highlights that must not read
  // as a primary action — refunds, the third slice of a mix chart.
  accent: "#7C3AED",
  accentTint: "#F3E8FF",
  accentDark: "#6D28D9",

  // The navigation rail. Deliberately outside the blue ramp: the rail is
  // chrome and must never compete with the blue commit button sitting on it.
  nav: "#102A63",
  navDark: "#0B2050",
  navText: "#DCE4F4",
  navIcon: "#AEBFDF",
  navMuted: "#9FB2D6",

  // Type.
  ink: "#111827",
  inkStrong: "#0B1220",
  ink2: "#374151",
  ink2Soft: "#6B7280",
  ink3: "#9AA3B2",

  // Surfaces and rules. `line2` is the hairline used *inside* a card, where
  // `line` would read as a second border against the card's own edge.
  surface: "#FFFFFF",
  bg: "#F5F7FA",
  bgSoft: "#F7F9FC",
  sunk: "#F7F9FC",
  line: "#E7EBF1",
  line2: "#F0F3F7",
  lineStrong: "#D6DBE3",

  // Success — paid, completed, positive movement. Never a primary action.
  ok: "#16A34A",
  okDark: "#15803D",
  okTint: "#DCFCE7",
  okBorder: "#BBF7D0",

  // Destructive / error.
  danger: "#DC2626",
  dangerDark: "#B91C1C",
  dangerTint: "#FEE2E2",
  dangerSoft: "#F5CFCF",

  // Warning / "read this before you act".
  warn: "#B45309",
  warnDark: "#92400E",
  warnTint: "#FEF3C7",
  warnBorder: "#E7C88C",

  // Neutral chip — sold out, inert states.
  neutralTint: "#F3F4F6",

  // Scrims behind modals and sheets.
  scrim: "rgba(12,22,45,0.50)",
  scrimSoft: "rgba(12,22,45,0.30)",
};

// Type faces. Sarabun carries Thai and English together; the mono face is
// reserved for figures — money, counts, times, references — so columns of
// numbers line up and a price never reflows as its digits change.
export const F = {
  ui: "Sarabun",
  bold: "Sarabun-Bold",
  mono: "SpaceMono",
};

// Money and any other tabular figure. Spread onto a Text style.
export const MONO = {
  fontFamily: F.mono,
  fontVariant: ["tabular-nums" as const],
  letterSpacing: -0.2,
};

// Shared geometry, so a card radius is one number rather than forty.
export const R = {
  chip: 8,
  control: 12,
  card: 14,
  modal: 18,
  pill: 999,
};

export default C;
