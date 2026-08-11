"""Presentation helpers for the backoffice templates.

Nothing here computes business figures — that stays in the views. These are
the small formatting decisions that would otherwise be repeated inline in
thirty templates and drift apart.
"""
from django import template

register = template.Library()


# A branch's colour is carried consistently across every screen, so a mixed
# list of bills stays readable without reading the words. Derived from the
# branch id rather than stored: nobody has to pick one, it never collides
# with itself across page loads, and adding a branch needs no migration.
#
# Ordered by the artifact's own assignment — first branch blue, second
# purple, third green — so a three-shop business gets exactly that.
BRANCH_COLOURS = [
    "#2563EB",  # blue
    "#7C3AED",  # purple
    "#16A34A",  # green
    "#B45309",  # amber
    "#0EA5E9",  # sky
    "#DB2777",  # pink
    "#0D9488",  # teal
    "#65A30D",  # lime
]


@register.filter
def branch_color(branch):
    """A stable colour for a branch, or grey when there isn't one.

    Keyed on the id's hex digits rather than Python's `hash`, which is salted
    per process and would repaint every chip on each server restart.
    """
    if branch is None:
        return "#9AA3B2"
    # Accepts a Branch or a plain dict row from a `.values()` aggregate — the
    # reports work in dicts, and a chip should not need a model instance.
    if isinstance(branch, dict):
        key = branch.get("id") or branch.get("branch__id")
    else:
        key = getattr(branch, "id", None) or getattr(branch, "pk", None)
    if key is None:
        return "#9AA3B2"
    digest = int(str(key).replace("-", "")[-8:], 16)
    return BRANCH_COLOURS[digest % len(BRANCH_COLOURS)]


@register.filter
def initial(value):
    """First character, for an avatar disc. Thai names work as-is: the first
    codepoint is a real glyph, not a combining mark, for every name we see."""
    text = (str(value) if value else "").strip()
    return text[0].upper() if text else "?"


@register.filter
def initials(value):
    """Up to two initials from a full name — 'Rolling Pinn Admin' → 'RA'."""
    parts = [p for p in (str(value) if value else "").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


# ── Payment methods ─────────────────────────────────────────────────────
# The POS writes a free-text `payment_method`; these map whatever it wrote
# onto the three buckets the reports actually reason about, so "PromptPay",
# "promptpay" and "qr" don't split into three columns.
PAYMENT_TAGS = {
    "cash": ("Cash", "t-ok"),
    "promptpay": ("PromptPay", "t-info"),
    "qr": ("PromptPay", "t-info"),
    "beam": ("PromptPay", "t-info"),
    "card": ("Card", "t-purple"),
    "credit": ("Card", "t-purple"),
    "omise": ("Card", "t-purple"),
    "transfer": ("Transfer", "t-info"),
}


@register.filter
def payment_label(method):
    key = (method or "").strip().lower()
    for needle, (label, _) in PAYMENT_TAGS.items():
        if needle in key:
            return label
    return (method or "Unknown").title()


@register.filter
def payment_class(method):
    key = (method or "").strip().lower()
    for needle, (_, css) in PAYMENT_TAGS.items():
        if needle in key:
            return css
    return "t-out"


@register.filter
def pct_of(value, total):
    """`value` as a percentage of `total`, 0 when the total is empty.

    Guards the division so a template never has to, and an empty period
    renders a flat bar instead of raising.
    """
    try:
        total = float(total)
        if not total:
            return 0
        return float(value) / total * 100
    except (TypeError, ValueError):
        return 0


@register.filter
def sub(value, arg):
    """Subtraction — Django ships `add` but no counterpart."""
    try:
        return float(value) - float(arg)
    except (TypeError, ValueError):
        return 0


@register.filter
def money0(value):
    """Baht with no decimals and thousands separators, for headline figures
    where the satang are noise."""
    try:
        return f"฿{float(value):,.0f}"
    except (TypeError, ValueError):
        return "฿0"


@register.filter
def money2(value):
    try:
        return f"฿{float(value):,.2f}"
    except (TypeError, ValueError):
        return "฿0.00"


@register.simple_tag
def qs_replace(request, **kwargs):
    """Current query string with some keys replaced — for sort headers and
    filter chips that must preserve the branch and date range around them.

    A key set to None is dropped, so `{% qs_replace request page=None %}`
    resets pagination when a filter changes.
    """
    params = request.GET.copy()
    for key, value in kwargs.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = value
    return params.urlencode()
