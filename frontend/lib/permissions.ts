// Till permissions ticked per staff member on Backoffice › Staff
// (backend_django/bravepos/permissions.py is the source of truth). The server
// refuses anything not granted; this only decides what the till shows.

export type Permission =
  | "manage_products"
  | "manage_inventory"
  | "adjust_stock"
  | "manage_customers"
  | "manage_settings";

// A backend from before permissions sends none — fall back to what each
// role could do then, so an older server keeps today's behaviour.
const ROLE_DEFAULTS: Record<string, Permission[]> = {
  admin: ["manage_products", "manage_inventory", "adjust_stock", "manage_customers", "manage_settings"],
  cashier: ["manage_inventory", "adjust_stock", "manage_customers"],
};

export function permissionsOf(staff: { role?: string; permissions?: unknown } | null | undefined): Set<Permission> {
  if (Array.isArray(staff?.permissions)) return new Set(staff!.permissions as Permission[]);
  return new Set(ROLE_DEFAULTS[staff?.role || ""] || []);
}
