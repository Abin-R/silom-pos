"""Order creation — the one place a sale comes into existence.

Extracted from ``views.py`` because there are now two callers that must behave
identically: a cashier ringing up at the till, and a customer's self-order being
confirmed as paid.  Keeping it here (rather than in views) also breaks what
would otherwise be a circular import between the views and the self-order
service layer.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import F, Max
from django.utils import timezone as djtz

from .gateways import (
    BEAM_CARD_METHOD,
    CARD_METHOD_PREFIX,
    compute_order_charges,
    get_shop_settings,
)
from .models import Branch, Order, OrderItem, Product, Shift, StockMovement

# Sentinel so a caller can pass ``shift=None`` to mean "no shift" and be
# distinguished from "caller didn't say — look up the currently-open one".
_UNSET = object()


# ─── Order numbering ─────────────────────────────────────────────────────────
# How many times to re-derive an order number when another writer takes it
# first.  Two cashiers, or a cashier and a self-order being confirmed, can read
# the same max() and race for the same PS number; order_number is unique=True so
# the loser gets an IntegrityError.  Retrying is enough because the window is
# tiny and each retry re-reads the (now higher) max.
ORDER_NUMBER_MAX_RETRIES = 6


def _next_order_number() -> str:
    """``PS`` + 9-digit running number, matching the FastAPI format.

    Global, not per-branch.  Inherently racy on its own — always call inside
    ``_create_order_with_retry``'s loop, never bare.
    """
    last = (
        Order.objects
        .order_by('-order_number')
        .values_list('order_number', flat=True)
        .first()
    )
    n = 1
    if last and last.startswith('PS') and last[2:].isdigit():
        n = int(last[2:]) + 1
    return f'PS{n:09d}'


def _next_queue_number(branch) -> int:
    """The number the customer is *called* by — per branch, per day.

    Previously derived on the client as the last two digits of the global PS
    sequence, which meant two branches shared a counter and the same number came
    round again every 100 orders.  Safe against races because every caller holds
    the branch row lock taken in ``_create_order_with_retry``.
    """
    today = djtz.localdate()
    last = (
        Order.objects
        .filter(branch=branch, created_at__date=today)
        .aggregate(m=Max('queue_number'))['m']
    )
    return (last or 0) + 1


def create_order_from_items(
    *,
    branch,
    items: list[dict],
    payment_method: str = '',
    goods_total: Decimal = Decimal('0'),
    charges: dict | None = None,
    subtotal: Decimal = Decimal('0'),
    discount_type: str = 'none',
    discount_value: Decimal = Decimal('0'),
    discount_amount: Decimal = Decimal('0'),
    paid_amount: Decimal | None = None,
    # Paid means done.  The kanban that used to walk a bill through
    # new → preparing → completed is switched off, so anything left in an
    # earlier state would sit there forever with nothing to advance it.
    status: str = 'completed',
    source: str = 'table',
    staff: str = '',
    customer_id=None,
    customer_name: str = '',
    gateway_ids: dict | None = None,
    delivery_provider: str = '',
    delivery_status: str = '',
    trust_item_prices: bool = True,
    shift=_UNSET,
) -> Order:
    """Create an Order + its items, decrement stock, log the movements.

    Shared by the two things that can produce a sale: a cashier ringing up at
    the till (``orders_list_create``) and a customer's self-order being
    confirmed as paid (``public_views``/the sweeper).

    ``charges``: pass ``None`` to recompute VAT + card fee from ``goods_total``
    — correct at the till, where the sale is rung up seconds after the price was
    read.  Self-ordering passes **frozen** charges instead: its build→pay window
    is minutes, the fee/VAT percentages live in an editable Settings singleton,
    and the gateway has *already captured* a specific number of satang.
    Recomputing there could make Order.total disagree with the money actually
    taken.

    ``trust_item_prices``: the till sends prices from its own product cache and
    is trusted.  Public callers must pass items already re-priced from the DB.
    """
    gateway_ids = gateway_ids or {}

    # Resolve products once, scoped to the branch, so an order can only ever
    # reference products that live at that branch.
    product_ids = [it.get('product_id') for it in items if it.get('product_id')]
    products_by_id: dict[str, Product] = {}
    cats_by_pid: dict[str, tuple[str | None, str]] = {}
    if product_ids:
        for p in (
            Product.objects
            .filter(id__in=product_ids, branch=branch)
            .select_related('category')
        ):
            products_by_id[str(p.id)] = p
            cats_by_pid[str(p.id)] = (
                str(p.category_id) if p.category_id else None,
                p.category.name if p.category else 'Other',
            )

    # Money.  is_card is detected from the method label — that is what decides
    # whether the customer also covers the processing fee.
    is_beam_card = payment_method == BEAM_CARD_METHOD
    is_card = payment_method.startswith(CARD_METHOD_PREFIX) or is_beam_card
    if charges is None:
        s = get_shop_settings()
        # Beam card carries its own surcharge; Omise card uses the Omise rate.
        fee_percent = s.beam_card_fee_percent if is_beam_card else None
        charges = compute_order_charges(
            goods_total, is_card=is_card, settings=s, fee_percent=fee_percent,
        )
    grand_total = charges['total']

    # Card charges are paid in full via the gateway (no cash tendered, no
    # change); other methods keep the cashier-entered tendered amount.
    if paid_amount is None:
        paid_amount = grand_total if is_card else Decimal('0')
    paid_amount = grand_total if is_card else Decimal(str(paid_amount))
    change = max(Decimal('0'), paid_amount - grand_total)

    # The shift this sale belongs to, stamped explicitly so a summary never has
    # to infer it from created_at.  The till doesn't pass one → use the branch's
    # currently-open shift (a cashier is always inside one).  A self-order passes
    # the shift it was *placed* in, which may already be closed by the time the
    # payment is confirmed — that is the whole reason this is a parameter.
    if shift is _UNSET:
        shift = Shift.objects.filter(branch=branch, status='open').first()

    last_error: Exception | None = None
    for _attempt in range(ORDER_NUMBER_MAX_RETRIES):
        try:
            with transaction.atomic():
                # Serialise order creation for this branch.  Makes the
                # read-then-insert of queue_number safe; order_number is global
                # so it can still collide across branches — the retry covers that.
                Branch.objects.select_for_update().filter(pk=branch.pk).first()

                order = Order.objects.create(
                    branch=branch,
                    order_number=_next_order_number(),
                    queue_number=_next_queue_number(branch),
                    shift=shift,
                    subtotal=Decimal(str(subtotal)),
                    discount_type=discount_type,
                    discount_value=Decimal(str(discount_value)),
                    discount_amount=Decimal(str(discount_amount)),
                    total=grand_total,
                    vat_amount=charges['vat_amount'],
                    processing_fee=charges['processing_fee'],
                    processing_fee_vat=charges['processing_fee_vat'],
                    payment_method=payment_method,
                    paid_amount=paid_amount,
                    change=change,
                    status=status,
                    source=source,
                    customer_id=customer_id,
                    customer_name=customer_name or '',
                    beam_charge_id=gateway_ids.get('beam_charge_id', '') or '',
                    beam_link_id=gateway_ids.get('beam_link_id', '') or '',
                    omise_link_id=gateway_ids.get('omise_link_id', '') or '',
                    omise_charge_id=gateway_ids.get('omise_charge_id', '') or '',
                    delivery_provider=delivery_provider or '',
                    delivery_status=delivery_status or '',
                    created_time=datetime.now().strftime('%H:%M'),
                    staff=staff or '',
                )

                for it in items:
                    pid = str(it.get('product_id')) if it.get('product_id') else ''
                    prod = products_by_id.get(pid)
                    cat_id, cat_name = cats_by_pid.get(
                        pid, (it.get('category_id'), it.get('category_name', '')),
                    )
                    qty = int(it.get('qty', 1))

                    # A public caller's prices are never trusted; re-read them
                    # from the product row.  A deleted/unknown product falls back
                    # to the sent price, which for public callers can only happen
                    # for a line we ourselves priced a moment ago.
                    if trust_item_prices or prod is None:
                        price = Decimal(str(it.get('price', 0)))
                    else:
                        price = Decimal(str(prod.price))

                    OrderItem.objects.create(
                        order=order,
                        product_id=it.get('product_id'),
                        name=it.get('name', '') or (prod.name if prod else ''),
                        price=price,
                        qty=qty,
                        discount=Decimal(str(it.get('discount', 0) or 0)),
                        category_id=cat_id,
                        category_name=cat_name or '',
                    )

                    if prod:
                        # Decrement in the DB, not from the copy we read earlier.
                        # The old code did a read-modify-write on a stale in-memory
                        # value (its select_for_update had already been committed
                        # and the locks dropped), so two concurrent orders for the
                        # same product silently lost one of the decrements.
                        #
                        # Stock is allowed to go negative — matches the existing
                        # demo data and lets a cashier oversell deliberately.
                        Product.objects.filter(pk=prod.pk).update(
                            stock=F('stock') - qty,
                        )
                        StockMovement.objects.create(
                            branch=branch,
                            product=prod,
                            product_name=prod.name,
                            type='out',
                            qty=qty,
                            note=f'Order {order.order_number}',
                            document_no=order.order_number,
                        )
                return order
        except IntegrityError as e:
            # Almost certainly the unique order_number. Re-derive and try again.
            last_error = e
            continue

    raise IntegrityError(
        f'Could not allocate a unique order number after '
        f'{ORDER_NUMBER_MAX_RETRIES} attempts: {last_error}'
    )
