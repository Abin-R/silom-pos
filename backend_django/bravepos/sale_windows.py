"""Launch and discontinue dates: taking products on and off sale by the calendar.

A product's ``sale_windows`` say when it sells.  None at all means always —
the state of every product made before this existed, so nothing changes for
them.  With windows, the product is on sale on any day some window covers and
off sale on every other day: before its first launch, in the gap between two
windows, and after its last discontinue date.

"Off sale" is not a new state.  It is the same ``active=False`` the Remove
button sets, so the product lands under **Removed** in the catalogue, the
till's product listing drops it (server-side — no app release involved), the
self-order menu hides it, and Restore brings it back.  The only addition is
``removed_by_schedule``, which remembers *why* it was removed: a product the
dates took off sale is put back by the dates when its next launch arrives,
while one an admin removed by hand stays removed.

`apply` is idempotent and runs in two places: right after a product form is
saved, so a date already in the past takes effect at once, and from
``manage.py apply_sale_windows`` on a cron, which is what moves products as the
days pass.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from .models import Product, ProductSaleWindow


def today():
    """The shop's date — Bangkok's, not the server's UTC one."""
    return timezone.localdate()


def covers(window, day) -> bool:
    return ((window.launch_date is None or window.launch_date <= day)
            and (window.discontinue_date is None or day <= window.discontinue_date))


def on_sale(windows, day) -> bool:
    """Whether ``windows`` allow selling on ``day``.  No windows: always."""
    windows = list(windows)
    return not windows or any(covers(w, day) for w in windows)


def next_launch(windows, day):
    """The first launch date after ``day``, or None if none is planned."""
    upcoming = [w.launch_date for w in windows
                if w.launch_date is not None and w.launch_date > day]
    return min(upcoming) if upcoming else None


def apply(product, day=None) -> str | None:
    """Bring ``product.active`` in line with its dates.

    Returns ``"removed"`` or ``"restored"`` when it changed something, else
    None.  Saves only the two flags, so it cannot clobber anything an admin or
    a till is editing at the same moment.
    """
    day = day or today()
    windows = list(product.sale_windows.all())
    if not windows:
        if product.removed_by_schedule:
            # The dates that removed it were deleted.  It stays in Removed —
            # bringing it back is Restore's job — but it is now an ordinary
            # removed product, so no future date can resurrect it.
            product.removed_by_schedule = False
            product.save(update_fields=["removed_by_schedule"])
        return None

    selling = on_sale(windows, day)
    if product.active and not selling:
        product.active = False
        product.removed_by_schedule = True
        product.save(update_fields=["active", "removed_by_schedule"])
        return "removed"
    if not product.active and selling and product.removed_by_schedule:
        product.active = True
        product.removed_by_schedule = False
        product.save(update_fields=["active", "removed_by_schedule"])
        return "restored"
    return None


def apply_all(day=None):
    """`apply` to every product that has dates.  Returns ``[(product, change)]``."""
    day = day or today()
    candidates = (
        Product.objects.filter(sale_windows__isnull=False)
        | Product.objects.filter(removed_by_schedule=True)
    ).distinct().prefetch_related("sale_windows").select_related("branch")
    changes = []
    for product in candidates:
        change = apply(product, day)
        if change:
            changes.append((product, change))
    return changes


def restore(product, day=None) -> bool:
    """Put ``product`` back on sale from ``day``, without its dates undoing it.

    Restore means "sell it now".  If no window covers today the next `apply`
    would take it straight back off, so a window is added starting today: up
    to the day before the next launch already planned, so that one still
    happens and carries on to its own end, or open-ended when nothing is
    planned.  Past windows are left as they were — they are the history of
    when it sold.  Returns whether a window was added.

    Sets ``active`` but does not save it; the caller saves.
    """
    day = day or today()
    windows = list(product.sale_windows.all())
    product.active = True
    product.removed_by_schedule = False
    if on_sale(windows, day):
        return False
    upcoming = next_launch(windows, day)
    ProductSaleWindow.objects.create(
        product=product, launch_date=day,
        discontinue_date=upcoming - timedelta(days=1) if upcoming else None,
    )
    return True


def replace(product, windows):
    """Make ``product``'s windows exactly ``windows`` — ``[(launch, discontinue)]``."""
    current = sorted(((w.launch_date, w.discontinue_date)
                      for w in product.sale_windows.all()), key=_sort_key)
    if current == sorted(windows, key=_sort_key):
        return False
    product.sale_windows.all().delete()
    ProductSaleWindow.objects.bulk_create([
        ProductSaleWindow(product=product, launch_date=a, discontinue_date=b)
        for a, b in windows
    ])
    return True


def _sort_key(pair):
    # None sorts first, as it would against any date: open start = earliest.
    a, b = pair
    return (a is not None, a, b is not None, b)


def status(windows, day=None) -> str:
    """One line describing where a product is in its schedule, or ""."""
    day = day or today()
    windows = sorted(windows, key=lambda w: _sort_key((w.launch_date, w.discontinue_date)))
    if not windows:
        return ""
    current = [w for w in windows if covers(w, day)]
    if current:
        ends = [w.discontinue_date for w in current]
        if any(e is None for e in ends):
            return "On sale — no end date"
        return f"On sale until {_fmt(max(ends))}"
    nxt = next_launch(windows, day)
    if nxt:
        started = any(w.discontinue_date is not None and w.discontinue_date < day
                      for w in windows)
        return f"{'Back on sale' if started else 'Launches'} {_fmt(nxt)}"
    last = max(w.discontinue_date for w in windows if w.discontinue_date is not None)
    return f"Discontinued {_fmt(last)}"


def _fmt(d) -> str:
    return f"{d.day} {d:%b %Y}"
