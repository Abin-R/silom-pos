"""What each channel may sell of a product, in one place.

Three channels share a branch's shelf: the till, the self-order QR menu and
Grab.  They each hear about the others' sales a little late, so the numbers
below are how they stay out of each other's way::

    self-order sells = stock - self_held - grab_held - outage_shown - counter_stock
    grab_target      = max(0, stock - self_held - grab_held - counter_stock)
    till sells       = stock - grab_held - outage_shown

* ``self_held`` — units under a live self-order hold (a time on the draft, not
  a stock move; see ``selforder``).
* ``grab_held`` — units of Grab orders we know of that have not been packed
  yet (``GrabOrderHold`` rows with ``status='held'``).
* ``outage_shown`` — while the poller is down and we cannot push, Grab's count
  can only fall (auto-restock is off), so what it last showed is the most it can
  still sell.  Empty unless the branch is in an outage; see ``in_outage``.
* ``counter_stock`` — the reserve ``Product.counter_stock`` that self-order
  already leaves for the counter.  Grab shares it.  The till ignores it.

Every reader goes through here, so the till, self-order and the Grab poller all
see the same numbers.  A branch with no ``grab_merchant_id`` has no Grab holds
and no outage: its rows are ignored, which is also the per-branch off switch.
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Max
from django.utils import timezone as djtz

from .models import (
    GrabOrderHold, GrabSyncStatus, Product, SelfOrder, StockMovement,
)

# No cycle_ok heartbeat for this long, with live pushes on, is an outage.
OUTAGE_AFTER = timedelta(minutes=5)

# How far back ``target_changed_at`` looks.  The poller only asks whether
# something changed since its last cycle, so a day is far more than it needs.
CHANGE_WINDOW = timedelta(hours=24)

_UNSET = object()


def line_quantities(items: list[dict]) -> dict[str, int]:
    """Units per product id in one list of lines.  A product can sit on two
    lines, once paid and once free."""
    out: dict[str, int] = {}
    for it in items or []:
        pid = str(it.get('product_id') or '')
        if pid:
            out[pid] = out.get(pid, 0) + int(it.get('qty') or 0)
    return out


def _add(into: dict[str, int], more: dict[str, int]) -> dict[str, int]:
    for pid, qty in more.items():
        into[pid] = into.get(pid, 0) + qty
    return into


def _units(item_lists) -> dict[str, int]:
    """Units per product id over many lists of lines."""
    held: dict[str, int] = {}
    for items in item_lists:
        _add(held, line_quantities(items))
    return held


# ── The three reductions ────────────────────────────────────────────────────
def self_held(branch, exclude_pk=None) -> dict[str, int]:
    """Units per product id under a live self-order hold at this branch.

    Free lines count: they leave the shelf like any other.  A draft stops
    counting the moment its hold lapses, it fails, or it is paid, and paying
    moves the stock in the same transaction, so a unit is never counted twice.
    """
    drafts = SelfOrder.objects.filter(
        branch=branch, status='pending', hold_expires_at__gt=djtz.now(),
    )
    if exclude_pk is not None:
        drafts = drafts.exclude(pk=exclude_pk)
    return _units(drafts.values_list('items', flat=True))


def grab_held(branch) -> dict[str, int]:
    """Units per product id in Grab orders we know of that are not packed yet.

    No time limit: a held order stays held until it is converted, released or
    settled by staff.  That is the safe direction — Grab stays low and the till
    stays blocked.
    """
    if not branch.grab_merchant_id:
        return {}
    return _units(GrabOrderHold.objects.filter(
        branch=branch, status=GrabOrderHold.HELD,
    ).values_list('items', flat=True))


def sync_status(branch):
    return GrabSyncStatus.objects.filter(branch=branch).first()


def in_outage(status, now=None) -> bool:
    """True when live pushes are on and the poller has gone quiet or says it is
    broken.  With pushes off nothing can be out of date, so it is never an
    outage."""
    if status is None or not status.push_live:
        return False
    if status.problem or status.last_ok_at is None:
        return True
    return (now or djtz.now()) - status.last_ok_at > OUTAGE_AFTER


def outage_shown(branch, status=_UNSET) -> dict[str, int]:
    """In an outage, ``{product_id: units Grab can still sell}``; else empty."""
    if not branch.grab_merchant_id:
        return {}
    if status is _UNSET:
        status = sync_status(branch)
    if not in_outage(status):
        return {}
    shown = {}
    for barcode, units in (status.grab_shown or {}).items():
        try:
            units = int(units)
        except (TypeError, ValueError):
            continue
        if units > 0:
            shown[str(barcode)] = units
    if not shown:
        return {}
    pairs = Product.objects.filter(
        branch=branch, active=True, barcode__in=list(shown),
    ).values_list('barcode', 'id')
    return {str(pid): shown[barcode] for barcode, pid in pairs}


def withheld_for_other_channels(branch, exclude_pk=None) -> dict[str, int]:
    """What self-order must keep off the shelf: its own holds, Grab's holds and,
    in an outage, whatever Grab can still sell."""
    held = self_held(branch, exclude_pk=exclude_pk)
    _add(held, grab_held(branch))
    _add(held, outage_shown(branch))
    return held


# ── Availability ────────────────────────────────────────────────────────────
def availability(branch, products=None) -> dict[str, dict]:
    """Per product id: stock, counter_stock, the holds, and what Grab and the
    till may sell.  ``products`` defaults to the branch's active, barcoded
    products — everything Grab can map."""
    if products is None:
        products = Product.objects.filter(branch=branch, active=True).exclude(barcode='')
    selfh = self_held(branch)
    grabh = grab_held(branch)
    outage = outage_shown(branch)
    out: dict[str, dict] = {}
    for p in products:
        pid = str(p.id)
        stock = int(p.stock or 0)
        counter = int(p.counter_stock or 0)
        sh, gh, os_ = selfh.get(pid, 0), grabh.get(pid, 0), outage.get(pid, 0)
        out[pid] = {
            'stock': stock,
            'counter_stock': counter,
            'self_held': sh,
            'grab_held': gh,
            'outage_shown': os_,
            'grab_target': max(0, stock - sh - gh - counter),
            'till_can_sell': stock - gh - os_,
        }
    return out


def till_shortfalls(branch, wanted: dict[str, int], stocks: dict[str, int] | None = None):
    """The products in ``wanted`` (``{product_id: units}``) the till may not sell
    that many of, as ``[{product_id, wanted, can_sell, held_for_grab}]``.

    Only a product that has Grab holds or an outage figure is ever listed; the
    rest of the shelf is not the till's to be stopped on.  ``stocks`` is
    ``{product_id: stock}`` when the caller has just read the rows under a lock.
    """
    if not wanted or not branch.grab_merchant_id:
        return []
    held = grab_held(branch)
    _add(held, outage_shown(branch))
    held = {pid: qty for pid, qty in held.items() if qty > 0 and pid in wanted}
    if not held:
        return []
    if stocks is None:
        stocks = {
            str(pk): int(stock or 0) for pk, stock in
            Product.objects.filter(branch=branch, pk__in=list(held)).values_list('pk', 'stock')
        }
    out = []
    for pid, units in held.items():
        if pid not in stocks:
            continue
        can_sell = stocks[pid] - units
        if wanted[pid] > can_sell:
            out.append({
                'product_id': pid, 'wanted': wanted[pid],
                'can_sell': max(0, can_sell), 'held_for_grab': units,
            })
    return out


def target_changed_at(branch, products) -> dict[str, object]:
    """``{barcode: when}`` for the products whose target something touched in
    the last day: the newest stock movement, Grab hold change, or self-order
    hold starting or ending.  The poller uses it to tell a stock-in or a
    release (a reason for the target to rise) from a change nobody explained."""
    # Imported here: ``selforder`` imports this module.
    from .selforder import HOLD_MINUTES

    now = djtz.now()
    since = now - CHANGE_WINDOW
    barcode_of = {str(p.id): p.barcode for p in products}
    newest: dict[str, object] = {}

    def note(pid, when):
        if when is None or pid not in barcode_of:
            return
        if when > now:
            return
        if pid not in newest or when > newest[pid]:
            newest[pid] = when

    moves = (
        StockMovement.objects.filter(branch=branch, created_at__gte=since)
        .values('product_id').annotate(newest=Max('created_at'))
    )
    for row in moves:
        note(str(row['product_id']), row['newest'])

    holds = GrabOrderHold.objects.filter(branch=branch, changed_at__gte=since)
    for items, when in holds.values_list('items', 'changed_at'):
        for pid in line_quantities(items):
            note(pid, when)

    drafts = SelfOrder.objects.filter(branch=branch, hold_expires_at__gte=since)
    for items, expires in drafts.values_list('items', 'hold_expires_at'):
        for pid in line_quantities(items):
            note(pid, expires - timedelta(minutes=HOLD_MINUTES))   # the hold began
            note(pid, expires)                                     # and ended
    return {barcode_of[pid]: when for pid, when in newest.items()}
