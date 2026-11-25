"""The payment filter on Transactions: what it offers, and what each matches.

It matches the strings the till and self-order actually store ("Beam QR",
"Custom · EDC Bangkok"), not words inside them.  The old filter searched for
"PromptPay" and "Card" as text, which no Beam QR or Custom Pay bill contains.
"""
from __future__ import annotations

from django.db.models import Q

from bravepos.gateways import BEAM_APP_METHODS
from bravepos.models import PaymentType

CUSTOM_PREFIX = "Custom · "

# Button → the stored methods it covers, each with or without a " · detail".
GROUPS = {
    "Cash": ("Cash",),
    "PromptPay": ("Beam QR", "Beam", "PromptPay", "QR Kbank"),
    "Card": ("Beam Card", "Credit Card", "Credit", "Card"),
    "Custom Pay": ("Custom",),
}
_APP_METHODS = [method for _, method in BEAM_APP_METHODS.values()]


def payment_q(value):
    """What ``?payment=`` matches: a button's group, else one stored method."""
    if value not in GROUPS:
        return Q(payment_method__iexact=value)
    match = Q()
    for method in GROUPS[value]:
        match |= (Q(payment_method__iexact=method)
                  | Q(payment_method__istartswith=f"{method} · "))
    return match


def _in_a_group(method):
    method = method.casefold()
    return any(method == m.casefold() or method.startswith(f"{m} · ".casefold())
               for methods in GROUPS.values() for m in methods)


def more_types(branch, branches, used, selected=""):
    """The "More types" dropdown: ``[(heading, [(value, label), …]), …]``.

    Custom Pay is each branch's current list plus whatever is on the bills in
    view (``used``), since a type renamed or deleted since still has its
    sales.  ``selected`` stays listed even when the dates changed under it.
    """
    used = [m for m in used if m]
    if selected and selected not in GROUPS:
        used = used + [selected]

    types = PaymentType.objects.filter(active=True)
    types = types.filter(branch=branch) if branch else types.filter(branch__in=branches)
    names = {}  # case-insensitive, first spelling wins: the tills' before the bills'
    for name in types.order_by("sort_order", "name").values_list("name", flat=True):
        names.setdefault(name.casefold(), name)
    for method in used:
        if method.casefold().startswith(CUSTOM_PREFIX.casefold()):
            name = method[len(CUSTOM_PREFIX):]
            names.setdefault(name.casefold(), name)
    custom = [(CUSTOM_PREFIX + name, name) for name in names.values()]
    if any(m.casefold() == "custom" for m in used):
        custom.append(("Custom", "No type chosen"))

    apps = [(m, m.split(" ", 1)[1]) for m in _APP_METHODS]
    known = {m.casefold() for m in _APP_METHODS}
    other = {}
    for method in used:
        if method.casefold() not in known and not _in_a_group(method):
            other.setdefault(method.casefold(), method)

    groups = [("Custom Pay", custom), ("Banking apps & e-wallets", apps),
              ("Other", [(m, m) for m in other.values()])]
    return [(heading, options) for heading, options in groups if options]
