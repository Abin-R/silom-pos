"""Customer identity — one person, one row, shop-wide.

The customer book used to be scoped per branch, so a regular who registered
at EmQuartier and then walked into Silom became two rows: two half-histories,
two loyalty memberships, and a cashier who could not see that the person in
front of them had been coming for a year.  The book is now shop-wide, which
makes those pre-existing pairs visible in one list — and makes folding them
back together both possible and necessary.

Matching is by phone number and nothing else.  Names are typed by hand, in two
scripts, and collide constantly ("Nok" is not an identity); a phone number is
what the till already asks for and what the CRM keys a membership on.  A
customer with no phone on file is never merged with anyone, because there is
nothing to be confident about.
"""

import re
from collections import defaultdict

from django.db.models import Count

# Carried over from a duplicate when the survivor's own field is empty.
# ``name`` is deliberately absent: it is never blank, and the survivor's is the
# one the tills have been showing.  ``color``/``last_visit`` are absent because
# they are cosmetic and always populated by their defaults.
FILLABLE = (
    "phone", "last_name", "gender", "birth_date", "group",
    "tax_id", "tax_branch", "address", "email", "branch_id",
)


def normalise_phone(raw: str) -> str:
    """The comparable form of a phone number, or ``""`` if there isn't one.

    ``+66 64 418 4887``, ``0644184887`` and ``064-418-4887`` are one number
    typed three ways; the till stores whichever the cashier keyed.  Only the
    unambiguous Thai international form is folded — an 11-digit string opening
    ``66`` — so a foreign number is left exactly as it was rather than being
    guessed at.
    """
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("66"):
        digits = "0" + digits[2:]
    return digits


def phone_search_digits(raw: str) -> str:
    """The digits of a *typed query* worth matching a stored number against.

    Numbers are stored in E.164 (``+66644184887``) but a cashier searches the
    way they read a number off a receipt or a loyalty card (``0644184887``),
    so the leading ``0``/``66`` has to come off both sides before a substring
    match means anything.  A partial number shortens to a partial key, so the
    search narrows as they keep typing.

    Returns ``""`` for a query with no digits in it, which is the signal to
    search names only rather than match every number in the book.

    The key spans ``+66``/``0``, not punctuation: it is compared against the
    stored string, so a legacy row hand-typed as ``064-418-4887`` is found by
    typing the separators, not by the bare digits.  Canonicalising stored
    numbers would close that, and is deliberately not done —
    ``loyalty.member_for_customer`` hands ``customer.phone`` to the CRM
    verbatim, where a reformatted number can read as a new member and strand
    the real one's points.
    """
    digits = normalise_phone(raw)
    return digits[1:] if digits.startswith("0") else digits


def _completeness(customer) -> int:
    """How much of a profile a row actually carries."""
    return sum(
        1 for f in FILLABLE if getattr(customer, f, None) not in (None, "")
    )


# Every field on a Customer row, so a snapshot can put one back exactly as it
# was.  Listed rather than introspected: a field added later should be a
# deliberate decision here, not silently absent from an undo tape written
# months before anyone reads it.
SNAPSHOT_FIELDS = (
    "id", "branch_id", "name", "phone", "last_visit", "color",
    "last_name", "gender", "birth_date", "group",
    "tax_id", "tax_branch", "address", "email",
)


def _jsonable(value):
    """UUIDs and dates through JSON and back without surprises."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _row(customer) -> dict:
    return {f: _jsonable(getattr(customer, f, None)) for f in SNAPSHOT_FIELDS}


def plan_merges(Customer, Order) -> list:
    """Group customers by phone and pick a survivor for each duplicate set.

    Returns ``[(survivor, [absorbed, ...]), ...]``, empty when there is
    nothing to merge.  Reads only — it changes nothing, which is what lets the
    same code back both the dry-run report and the merge itself, so what the
    report promises is by construction what happens.

    The survivor is the row the most bills already point at: it is the identity
    with the most to lose and the fewest references to rewrite.  Ties break on
    the fuller profile, then on id, so two runs always agree.
    """
    groups = defaultdict(list)
    for customer in Customer.objects.all():
        key = normalise_phone(customer.phone)
        if key:
            groups[key].append(customer)

    order_counts = {
        row["customer_id"]: row["n"]
        for row in (Order.objects.filter(customer__isnull=False)
                    .values("customer_id").annotate(n=Count("id")))
    }

    plan = []
    for rows in groups.values():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda c: (-order_counts.get(c.id, 0),
                                 -_completeness(c), str(c.id)))
        plan.append((rows[0], rows[1:]))
    return plan


def take_snapshot(Customer, Order, ParkedOrder, Backup, plan, note="") -> object:
    """Record everything the merge is about to change, and return the row.

    Three things, because restoring any one alone would leave the data worse
    than not restoring at all:

      * every customer row as it stands — not only the ones about to be
        deleted, since the survivor's blank fields are filled from them too;
      * the current owner of every order the merge will move;
      * the same for held carts.

    Call inside the merge's transaction, before anything is written.
    """
    loser_ids = [c.id for _, losers in plan for c in losers]
    payload = {
        "customers": [_row(c) for c in Customer.objects.all()],
        "moved_orders": {
            str(oid): str(cid) for oid, cid in
            Order.objects.filter(customer_id__in=loser_ids)
                 .values_list("id", "customer_id")
        },
        "moved_parked": {
            str(pid): str(cid) for pid, cid in
            ParkedOrder.objects.filter(customer_id__in=loser_ids)
                       .values_list("id", "customer_id")
        },
    }
    return Backup.objects.create(note=note, payload=payload)


def merge_duplicates_by_phone(Customer, Order, ParkedOrder,
                              Backup=None, note="") -> dict:
    """Fold every set of customers sharing a phone number into a single row.

    The models are passed in rather than imported so a migration can hand over
    its historical versions.

    Pass ``Backup`` to write an undo tape first — the migration always does.
    Without it the merge still runs, which is what the tests want and what a
    database that has already been backed up another way may prefer.

    ``Order.customer_name`` is left exactly as it was.  It is a snapshot of
    what was printed on that receipt, and a bill must keep saying what it said.

    Safe to run twice: the second pass finds no groups left to merge.
    """
    plan = plan_merges(Customer, Order)
    if not plan:
        return {"groups": 0, "removed": 0, "backup_id": None}

    backup = take_snapshot(Customer, Order, ParkedOrder, Backup, plan, note) \
        if Backup is not None else None

    removed = 0
    for survivor, losers in plan:
        # Merging only ever adds to what is known about someone: a value is
        # carried over only where the survivor has nothing of its own.
        filled = []
        for loser in losers:
            for field in FILLABLE:
                if (getattr(survivor, field, None) in (None, "")
                        and getattr(loser, field, None) not in (None, "")):
                    setattr(survivor, field, getattr(loser, field))
                    filled.append(field)
        if filled:
            survivor.save(update_fields=list(dict.fromkeys(filled)))

        loser_ids = [c.id for c in losers]
        Order.objects.filter(customer_id__in=loser_ids).update(customer=survivor)
        # ParkedOrder holds a bare UUID, not a foreign key, so nothing in the
        # database would follow the merge for it: a held cart would come back
        # from lunch attached to a customer that no longer exists.
        ParkedOrder.objects.filter(customer_id__in=loser_ids).update(
            customer_id=survivor.id)
        Customer.objects.filter(id__in=loser_ids).delete()
        removed += len(losers)

    return {
        "groups": len(plan),
        "removed": removed,
        "backup_id": backup.id if backup else None,
    }


def restore_snapshot(Customer, Order, ParkedOrder, backup) -> dict:
    """Put the customer book back the way the snapshot found it.

    Recreates the rows the merge deleted, undoes the fields it filled in on the
    survivors, and returns every moved bill and held cart to its previous
    owner.  Rows added *after* the snapshot are left alone — a customer
    registered since the merge is not a mistake to undo.

    Idempotent: restoring twice lands in the same place.
    """
    payload = backup.payload or {}
    rows = payload.get("customers") or []

    restored = 0
    for row in rows:
        fields = {k: v for k, v in row.items() if k != "id"}
        _, created = Customer.objects.update_or_create(
            id=row["id"], defaults=fields)
        restored += 1 if created else 0

    for order_id, customer_id in (payload.get("moved_orders") or {}).items():
        Order.objects.filter(id=order_id).update(customer_id=customer_id)
    for parked_id, customer_id in (payload.get("moved_parked") or {}).items():
        ParkedOrder.objects.filter(id=parked_id).update(customer_id=customer_id)

    return {
        "customers": len(rows),
        "recreated": restored,
        "orders": len(payload.get("moved_orders") or {}),
        "parked": len(payload.get("moved_parked") or {}),
    }
