"""What self-order and Grab may sell of a product, in one place.

Three channels share a branch's shelf: the till, the self-order QR menu and
Grab.  A Grab order is deducted from stock the moment it is accepted (see
``grab_sync``), so it needs no reservation here.  What Grab should show::

    grab_target = max(0, stock - self_held - counter_stock)

* ``self_held`` — units under a live self-order hold (a time on the draft, not
  a stock move; see ``selforder``).
* ``counter_stock`` — the reserve ``Product.counter_stock`` that self-order
  already leaves for the counter.  Grab shares it.

The till sells from ``stock`` exactly as before; nothing here concerns it.
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Max
from django.utils import timezone as djtz

from . import selforder
from .models import Product, SelfOrder, StockMovement

# How far back ``target_changed_at`` looks.  The poller only asks whether
# something changed since its last cycle, so a day is far more than it needs.
CHANGE_WINDOW = timedelta(hours=24)


def self_held(branch, exclude_pk=None) -> dict[str, int]:
    """Units per product id under a live self-order hold at this branch."""
    return selforder.held_quantities(branch, exclude_pk=exclude_pk)


def availability(branch, products=None, *, selfh=None) -> dict[str, dict]:
    """Per product id: stock, counter_stock, self_held and what Grab should
    show.  ``products`` defaults to the branch's active, barcoded products —
    everything Grab can map.  ``selfh`` may be passed in when the caller read
    it first (see ``grab_sync.stock_for_branch``)."""
    if selfh is None:
        selfh = self_held(branch)
    if products is None:
        products = Product.objects.filter(branch=branch, active=True).exclude(barcode='')
    out: dict[str, dict] = {}
    for p in products:
        pid = str(p.id)
        stock = int(p.stock or 0)
        counter = int(p.counter_stock or 0)
        sh = selfh.get(pid, 0)
        out[pid] = {
            'stock': stock,
            'counter_stock': counter,
            'self_held': sh,
            'grab_target': max(0, stock - sh - counter),
        }
    return out


def target_changed_at(branch, products) -> dict[str, object]:
    """``{barcode: when}`` for the products whose target something touched in
    the last day: the newest stock movement (a Grab deduction is one too), or
    self-order hold starting or ending.  The poller uses it to tell a change
    that explains the target moving from one nobody explained."""
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

    drafts = SelfOrder.objects.filter(branch=branch, hold_expires_at__gte=since)
    for items, expires in drafts.values_list('items', 'hold_expires_at'):
        for pid in selforder._line_quantities(items):
            note(pid, expires - timedelta(minutes=selforder.HOLD_MINUTES))   # the hold began
            note(pid, expires)                                     # and ended
    return {barcode_of[pid]: when for pid, when in newest.items()}
