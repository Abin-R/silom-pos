"""What a till staff member may do — ticked per person on Backoffice › Staff.

The role (admin / cashier) only supplies the starting ticks. Once an admin
saves the boxes, ``Staff.permissions`` holds the exact list and the role no
longer decides any of these. ``None`` means "never customised — use the role
defaults", so staff created by provisioning, scripts or the Users page get a
sensible set without every creation path having to remember to seed one.

The till hides what a person can't do, but the API is the actual gate: each
key below is checked server-side by ``views.require_perm``.
"""
from __future__ import annotations

# (key, label, help) — order is the order shown on the staff form.
PERMISSIONS = [
    ("manage_products", "Manage products",
     "Add, edit and delete products, and mark favourites."),
    ("manage_inventory", "Manage inventory",
     "Stock in, stock out and check stock."),
    ("adjust_stock", "Adjust stock",
     "Overwrite on-hand stock with a counted number."),
    ("manage_customers", "Manage customers",
     "Add customers on the Customers screen and edit their details. "
     "Registering a customer during a sale is always allowed."),
    ("manage_settings", "Manage settings",
     "Move this till to another branch."),
]

KEYS = [key for key, _, _ in PERMISSIONS]
LABELS = {key: label for key, label, _ in PERMISSIONS}

# Only meaningful alongside its parent: without inventory there is no
# stock screen to adjust from.
PARENT = {"adjust_stock": "manage_inventory"}

# What each role could do before permissions existed. Migration 0073 inlines
# its own copy so editing this later can't rewrite history.
ROLE_DEFAULTS = {
    "admin": list(KEYS),
    "cashier": ["manage_inventory", "adjust_stock", "manage_customers"],
}


def defaults_for(role):
    return list(ROLE_DEFAULTS.get(role, []))


def clean(keys):
    """Known keys only, in catalogue order, children dropped without parent."""
    chosen = set(keys or [])
    return [
        k for k in KEYS
        if k in chosen and (k not in PARENT or PARENT[k] in chosen)
    ]


def effective(staff):
    if staff.permissions is None:
        return defaults_for(staff.role)
    return clean(staff.permissions)


def has_perm(staff, key):
    return key in effective(staff)


def missing_defaults(staff):
    """Role-default permissions this person has had taken away — what the
    staff list flags, so a locked-down cashier stands out."""
    granted = set(effective(staff))
    return [k for k in defaults_for(staff.role) if k not in granted]
