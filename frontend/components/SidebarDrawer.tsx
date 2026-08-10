// Compatibility shim.
//
// The navigation rail now lives in components/NavRail, which renders the same
// body as an always-on 195px rail (tablet) or a slide-over drawer (phone).
// This file keeps the old import path working for screens that only ever
// wanted the drawer form.
//
// New code should import { NavRail, NavDrawer } from "./NavRail" directly.

export {
  NavDrawer as SidebarDrawer,
  NavRail,
  NavDrawer,
  SIDEBAR_ITEMS,
  RAIL_WIDTH,
} from "./NavRail";
export type { SidebarItem } from "./NavRail";
