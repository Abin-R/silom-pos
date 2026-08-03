// Single source of truth for app colours.
//
// The palette is lifted from the customer self-ordering pages
// (backend_django/bravepos/templates/selforder/base.html) so the till and the
// customer-facing screens read as one product: Rolling Pinn red on a warm
// grey surface, with green reserved for "this went through" and a deep brick
// for destructive/error states so nothing is mistaken for the primary action.
//
// Nothing here is imported for its own sake — screens should reference these
// tokens instead of inlining hex, so a rebrand is one file.

// Values are typed as plain `string` (no `as const`) — literal types would
// pin a variable to whichever token happened to be assigned first.
export const C: Record<string, string> = {
  // Brand — primary actions, active tabs, links, focus rings.
  brand: "#D61222",
  brandDark: "#A40D1A",
  brandTint: "#FCE9EB",
  brandTintSoft: "#FEF4F5",
  // Secondary brand accent (the self-order pink). Use sparingly, for
  // highlights that must not read as a primary action.
  accent: "#FFACEF",

  // Type.
  ink: "#1C1C1E",
  inkStrong: "#141416",
  ink2: "#5A5A60",
  ink2Soft: "#6B6B70",
  ink3: "#9A9AA0",

  // Surfaces and rules.
  surface: "#FFFFFF",
  bg: "#F4F4F6",
  bgSoft: "#FAFAFB",
  line: "#ECECEF",
  lineStrong: "#D9D9DE",

  // Success — paid, completed, positive movement. Never a primary action.
  ok: "#1A9C5B",
  okDark: "#147A46",
  okTint: "#E9F6EF",
  okBorder: "#B9E3CD",

  // Destructive / error. Deliberately darker and duller than `brand` so a
  // Delete button never looks like a Save button.
  danger: "#8C1D18",
  dangerDark: "#6E1512",
  dangerTint: "#FBEAE8",
  dangerSoft: "#E7A9A4",

  // Warning.
  warn: "#E8930C",
  warnDark: "#8A5A08",
  warnTint: "#FDF3E2",

  // Scrims behind modals and sheets.
  scrim: "rgba(28,28,30,0.45)",
  scrimSoft: "rgba(28,28,30,0.30)",
};

export default C;
