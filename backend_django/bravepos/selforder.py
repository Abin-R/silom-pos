"""Self-ordering service layer.

The rules that matter, in one place, so the HTTP views stay dumb:

  * **The customer's browser is not trusted.**  Prices are read from the
    Product table, never from the request.  ``price_cart`` is the only way a
    SelfOrder gets its money.
  * **One gateway object per draft, ever.**  ``start_payment`` is idempotent on
    the token.  Without that a customer could ask for a PromptPay QR *and* a
    card link — two live, payable charges for different amounts (card adds the
    fee) — and we'd have no idea which one they actually paid.
  * **Promotion is idempotent and gateway-verified.**  Several things poll the
    same draft (the customer's phone, the POS, the sweeper).  Exactly one Order
    may result.  The gateway is asked; the client is never believed.

Nothing here imports DRF or touches ``request``.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone as djtz

from . import discount_rules, discounts, gateways, images
from .gateways import BEAM_CARD_METHOD, BEAM_QR_METHOD, compute_order_charges
from .models import Order, Product, SelfOrder, Shift
from .orders import create_order_from_items

logger = logging.getLogger("bravepos")


def _placed_in_shift(draft: SelfOrder):
    """The shift open at the branch when this draft was created.

    A self-order is confirmed by a poll that can land minutes after payment —
    possibly after the cashier has closed the round.  Attributing it to the
    round it was *placed* in (rather than whatever is open at confirmation, or
    nothing) keeps it on exactly one shift summary.
    """
    when = draft.created_at
    return (
        Shift.objects.filter(branch=draft.branch, opened_at__lte=when)
        .filter(Q(closed_at__isnull=True) | Q(closed_at__gte=when))
        .order_by('-opened_at')
        .first()
    )

# Abuse limits.  There is no Redis and no CACHES block in settings, so a real
# token-bucket throttle would silently degrade to per-worker LocMemCache and be
# useless.  For a coffee-shop's volume, hard caps on the shape of a draft are
# the effective defence — they bound both gateway spam and absurd stock moves.
MAX_LINES_PER_ORDER = 40
MAX_QTY_PER_LINE = 50

# How long a customer has to pay before the sweeper gives up on the draft.
DRAFT_TTL_MINUTES = 120


class SelfOrderError(Exception):
    """Bad request from the customer's browser (surfaced as a 400)."""


def self_order_enabled(branch) -> bool:
    """The per-branch feature flag.  Deploying the code enables NO branch; each
    is turned on explicitly.  This — not the UUID in the URL — is what keeps a
    branch (e.g. biohouse) closed, because /api/branches leaks branch ids."""
    return bool(getattr(branch, 'self_order_enabled', False))


def branch_is_open(branch) -> bool:
    """Whether a customer can order from this branch right now.

    Two gates: the branch must be (1) opted into self-ordering, and (2) have an
    open shift — no open shift means nobody is at the till to see the order or
    the printed slip, so a customer could otherwise scan a leftover QR at 2am
    and pay into the void.
    """
    return self_order_enabled(branch) and Shift.objects.filter(
        branch=branch, status='open',
    ).exists()


# Below this on-hand level a product shows a "only N left" nudge (Swiggy-style).
LOW_STOCK_THRESHOLD = 5


def menu_image_ref(branch, product) -> str:
    """What the menu should put in ``image_url`` for one product.

    A hosted ``https://`` image is already small and already cacheable, so it
    passes straight through. A base64 ``data:`` URI does not: inlining those
    into the menu HTML is what made this page 648 KB, of which 583 KB was three
    products. Those become a URL to ``public_views.product_image``, carrying a
    content hash so the response can be cached hard and still never go stale.
    """
    raw = product.image_url or product.image_base64 or ''
    if not images.is_data_uri(raw):
        return raw
    return f'/order/{branch.id}/img/{product.id}/?v={images.digest(raw)}'


def menu_for(branch) -> list[dict]:
    """The public menu for one branch — every active product it stocks.

    Hand-rolled rather than reusing ProductSerializer so we control exactly what
    reaches a customer's phone: ``stock``/``sold_out`` are surfaced (so the page
    can grey out and block sold-out items, like a food-delivery app), but
    ``cost`` — the margin — is deliberately never included.
    """
    products = (
        Product.objects
        .filter(branch=branch, active=True)
        .select_related('category')
        .order_by('sort_order', 'name')
    )
    out = []
    for p in products:
        stock = int(p.stock or 0)
        out.append({
            'id': str(p.id),
            'name': p.name,
            'name_th': p.name_th or '',
            'price': float(p.price),
            'category_id': str(p.category_id) if p.category_id else '',
            'category_name': p.category.name if p.category else 'Other',
            'image_url': menu_image_ref(branch, p),
            # Inventory the customer is allowed to see. Service products (stock
            # not tracked) and anything with stock on hand are orderable.
            'stock': stock,
            'sold_out': stock <= 0 and p.product_type == 'P',
            'low_stock': 0 < stock <= LOW_STOCK_THRESHOLD and p.product_type == 'P',
        })
    return out


def discount_presets(branch) -> list[dict]:
    """The promotions a customer can pick on the menu: the till's list.

    Version "2" of the till's feed, which leaves out a free item the cashier
    picks from a category (the website has no picker for it).  "Other", the
    hand-typed discount, is not a preset and is never in it.
    """
    if not branch.discount_types_enabled:
        return []
    return discounts.preset_feed(branch, '2', self_order=True)


def _round_baht(n: float) -> Decimal:
    return Decimal(str(int(discount_rules.js_round(n))))


def price_cart(branch, raw_items: list[dict], presets: list[dict] | None = None,
               ) -> tuple[list[dict], Decimal]:
    """Re-price a customer's cart from the DB.

    Takes only ``product_id``, ``qty``, the picked ``discount_type_id`` and
    the ``suggested`` attribution from the client — everything else (name, price, category) is read from the
    Product row.  A tampered ``price: 0`` in the request body is therefore not
    just rejected, it is never looked at.

    A picked discount is kept only if the till's dropdown would have offered it
    for that line, and its amount is computed here (``discount_rules``), never
    read from the request.  Free items come back as ``is_free`` lines.
    """
    if not raw_items:
        raise SelfOrderError('Your cart is empty.')
    if len(raw_items) > MAX_LINES_PER_ORDER:
        raise SelfOrderError(f'Too many items (max {MAX_LINES_PER_ORDER}).')

    # Collapse duplicate lines for the same product before we look anything up.
    qty_by_pid: dict[str, int] = {}
    pick_by_pid: dict[str, str] = {}
    suggested_pids: set[str] = set()   # added from "frequently bought together"
    for it in raw_items:
        pid = str(it.get('product_id') or '').strip()
        if not pid:
            continue
        try:
            qty = int(it.get('qty', 1))
        except (TypeError, ValueError):
            raise SelfOrderError('Invalid quantity.')
        if qty <= 0:
            continue
        if qty > MAX_QTY_PER_LINE:
            raise SelfOrderError(f'Quantity too high (max {MAX_QTY_PER_LINE} per item).')
        qty_by_pid[pid] = qty_by_pid.get(pid, 0) + qty
        if it.get('discount_type_id'):
            pick_by_pid[pid] = str(it['discount_type_id'])
        if it.get('suggested'):
            suggested_pids.add(pid)

    if not qty_by_pid:
        raise SelfOrderError('Your cart is empty.')

    products = {
        str(p.id): p
        for p in Product.objects
        .filter(id__in=list(qty_by_pid), branch=branch, active=True)
        .select_related('category')
    }
    missing = set(qty_by_pid) - set(products)
    if missing:
        # A product was deactivated or deleted while they were browsing.
        raise SelfOrderError('An item in your cart is no longer available. Please review it.')

    lines = [
        {'product_id': pid, 'name': products[pid].name,
         'price': float(products[pid].price), 'qty': qty}
        for pid, qty in qty_by_pid.items()
    ]
    if presets is None:
        presets = discount_presets(branch)
    if presets:
        # One pick at a time, each checked against the cart as the previous
        # picks left it — the order a cashier would have applied them in.
        for pid in qty_by_pid:
            pick = pick_by_pid.get(pid)
            if not pick or not any(p['id'] == pick for p in
                                   discount_rules.offered(lines, presets, pid)):
                continue
            lines = discount_rules.reconcile([
                {**l, 'discount': None, 'discount_type_id': pick}
                if l['product_id'] == pid else l
                for l in lines
            ], presets)
        lines = discount_rules.reconcile(lines, presets)

    items: list[dict] = []
    for l in lines:
        if l.get('is_free'):
            items.append({
                'product_id': l['real_product_id'],
                'name': l['name'],
                'price': l['price'],
                'qty': l['qty'],
                'discount': l['discount'],
                'discount_type_id': l['discount_type_id'],
                'discount_label': l['discount_label'],
                'discount_logic': l.get('discount_logic') or '',
                'is_free': True,
                'free_of': l['free_of'],
            })
            continue
        p = products[l['product_id']]
        items.append({
            'product_id': l['product_id'],
            'name': p.name,
            'price': float(p.price),
            'qty': l['qty'],
            'discount': l.get('discount') or 0,
            'discount_type_id': l.get('discount_type_id') or '',
            'discount_label': l.get('discount_label') or '',
            'discount_logic': l.get('discount_logic') or '',
            'category_id': str(p.category_id) if p.category_id else None,
            'category_name': p.category.name if p.category else 'Other',
            'suggested': l['product_id'] in suggested_pids,
        })

    subtotal, discount = bill_figures(items)
    if discount > 0:
        # As at the till: only a discounted bill rounds to the whole baht.
        goods_total = _round_baht(max(0.0, float(subtotal) - float(discount)))
    else:
        goods_total = sum(
            (Decimal(str(products[it['product_id']].price)) * it['qty']
             for it in items if not it.get('is_free')),
            Decimal('0'),
        )
    return items, goods_total


def bill_figures(items: list[dict]) -> tuple[Decimal, Decimal]:
    """(subtotal, discount) of a priced cart, as the till sends them: the gross
    of the billed lines, and their discounts.  Free items are on neither."""
    subtotal = 0.0
    discount = 0.0
    for it in items:
        if it.get('is_free'):
            continue
        gross = it['price'] * it['qty']
        subtotal += gross
        discount += min(float(it.get('discount') or 0), gross)
    return Decimal(str(discount_rules.round2(subtotal))), Decimal(str(discount_rules.round2(discount)))


def preview(branch, raw_items: list[dict]) -> dict:
    """The cart as the menu page shows it: each line's price, the discount it
    carries, the discounts it could pick, and the bill.  Same rules as
    ``price_cart`` — the page never computes money itself."""
    presets = discount_presets(branch)
    items, goods_total = price_cart(branch, raw_items, presets)
    # Offered against the cart as priced, as the till's dropdown is.
    rule_lines = [
        {'product_id': it['product_id'], 'name': it['name'], 'price': it['price'],
         'qty': it['qty'], 'discount': it['discount'] or None,
         'discount_type_id': it['discount_type_id'] or None}
        for it in items if not it.get('is_free')
    ]
    subtotal, discount = bill_figures(items)

    def option(p):
        if discount_rules.is_free(p):
            fp = p['free_product']
            value = f"Free {fp['name']} ×{p.get('free_qty') or 1}"
        else:
            v = (f"{discount_rules._num(p['value'])}%" if p['kind'] == 'percent'
                 else discount_rules._baht(p['value']))
            value = f"{v} off the set" if discount_rules.is_combo(p) else f"{v} off"
        return {'id': p['id'], 'name': p['name'], 'value': value,
                'detail': discount_rules.preset_details(p)}

    lines = []
    for it in items:
        if it.get('is_free'):
            continue
        lines.append({
            'product_id': it['product_id'],
            'qty': it['qty'],
            'gross': discount_rules.round2(it['price'] * it['qty']),
            'discount': it['discount'],
            'discount_type_id': it['discount_type_id'],
            'discount_label': it['discount_label'],
            'free': [{'name': f['name'], 'qty': f['qty']} for f in items
                     if f.get('is_free') and f['free_of'] == it['product_id']],
            'options': [option(p) for p in
                        discount_rules.offered(rule_lines, presets, it['product_id'])],
        })
    net = max(Decimal('0'), subtotal - discount)
    return {
        'lines': lines,
        'subtotal': float(subtotal),
        'discount': float(discount),
        'rounding': float(goods_total - net),
        'total': float(goods_total),
    }


def create_draft(branch, raw_items: list[dict]) -> SelfOrder:
    """Price the cart and persist it as a pending draft."""
    items, goods_total = price_cart(branch, raw_items)

    # Only the goods side can be fixed now — the fee (and therefore the grand
    # total) depends on a payment method the customer hasn't picked yet.  VAT
    # is shop-wide, so the branch config vs shop makes no difference here, but
    # use the branch config for consistency with quote()/start_payment().
    cfg = gateways.resolve_payment_config(branch)
    charges = compute_order_charges(goods_total, is_card=False, settings=cfg)

    return SelfOrder.objects.create(
        token=SelfOrder.new_token(),
        branch=branch,
        items=items,
        goods_total=goods_total,
        vat_amount=charges['vat_amount'],
        total=charges['total'],
    )


def quote(draft: SelfOrder, method: str) -> dict:
    """What this draft costs by each method, without committing to one.

    PromptPay is the goods total. Card adds the processing fee + VAT on the fee.
    """
    cfg = gateways.resolve_payment_config(draft.branch)
    is_card = method == 'card'
    return compute_order_charges(
        draft.goods_total,
        is_card=is_card,
        settings=cfg,
        fee_percent=cfg.beam_card_fee_percent if is_card else None,
    )


def start_payment(draft: SelfOrder, method: str, redirect_url: str,
                  device_type: str = 'WEB') -> dict:
    """Create (or return) the gateway object for this draft.

    **Idempotent on the draft.**  The first call wins the method and creates the
    charge/link; later calls hand back what already exists.  If it weren't, a
    customer could open a PromptPay QR for the goods total and a card link for
    goods+fee, leave both live, pay one, and we would have two payable objects
    and no way to know which reflects reality.
    """
    if method not in ('qr', 'card') and method not in gateways.BEAM_APP_METHODS:
        raise SelfOrderError('Unknown payment method.')

    # Already started → replay the cached artefact, ignoring the requested
    # method.  Beam only returns the QR image / checkout URL when the charge is
    # created, so a replay has to come from our cache; re-calling the gateway
    # would mint a *second* payable object for the same cart.
    if draft.beam_charge_id:
        return {
            # An e-wallet / banking app is a charge too, told apart by its URL.
            'method': 'app' if draft.payment_uri_cache else 'qr',
            'qr_image': draft.qr_image_cache,
            'payment_uri': draft.payment_uri_cache,
            'total': float(draft.total),
        }
    if draft.beam_link_id:
        return {
            'method': 'card',
            'qr_image': '',
            'payment_uri': draft.payment_uri_cache,
            'total': float(draft.total),
        }

    reference_id = f'SELF-{draft.token[:12]}'
    description = f'{draft.branch.name} self-order'

    if method == 'qr':
        res = gateways.beam_create_promptpay_charge(
            amount=draft.goods_total,
            reference_id=reference_id,
            description=description,
            branch=draft.branch,
        )
        charges = quote(draft, 'qr')
        with transaction.atomic():
            draft.payment_method = BEAM_QR_METHOD
            draft.beam_charge_id = res['charge_id']
            draft.qr_image_cache = res['qr_image'] or ''
            draft.processing_fee = charges['processing_fee']
            draft.processing_fee_vat = charges['processing_fee_vat']
            draft.vat_amount = charges['vat_amount']
            draft.total = charges['total']
            draft.save(update_fields=[
                'payment_method', 'beam_charge_id', 'qr_image_cache',
                'processing_fee', 'processing_fee_vat', 'vat_amount', 'total',
            ])
        return {
            'method': 'qr',
            'qr_image': draft.qr_image_cache,
            'payment_uri': '',
            'total': float(draft.total),
        }

    if method in gateways.BEAM_APP_METHODS:
        # Opens the wallet or bank app, which sends the customer back to
        # redirect_url.  No fee, so the quote is PromptPay's.
        res = gateways.beam_create_app_charge(
            amount=draft.goods_total,
            reference_id=reference_id,
            method_type=method,
            return_url=redirect_url,
            device_type=device_type,
            description=description,
            branch=draft.branch,
        )
        charges = quote(draft, 'qr')
        with transaction.atomic():
            draft.payment_method = gateways.BEAM_APP_METHODS[method][1]
            draft.beam_charge_id = res['charge_id']
            draft.payment_uri_cache = res['payment_uri'] or ''
            draft.qr_image_cache = res['qr_image'] or ''
            draft.processing_fee = charges['processing_fee']
            draft.processing_fee_vat = charges['processing_fee_vat']
            draft.vat_amount = charges['vat_amount']
            draft.total = charges['total']
            draft.save(update_fields=[
                'payment_method', 'beam_charge_id', 'payment_uri_cache', 'qr_image_cache',
                'processing_fee', 'processing_fee_vat', 'vat_amount', 'total',
            ])
        return {
            'method': 'app',
            'qr_image': draft.qr_image_cache,
            'payment_uri': draft.payment_uri_cache,
            'total': float(draft.total),
        }

    # Card — hosted checkout.  redirect_url must be the customer's own status
    # page; the default in gateways points at the POS host, which is right for a
    # cashier and wrong here.
    res = gateways.beam_create_card_link(
        goods_total=draft.goods_total,
        reference_id=reference_id,
        description=description,
        redirect_url=redirect_url,
        branch=draft.branch,
    )
    with transaction.atomic():
        draft.payment_method = BEAM_CARD_METHOD
        draft.beam_link_id = res['link_id']
        draft.payment_uri_cache = res['payment_uri']
        draft.vat_amount = res['vat_amount']
        draft.processing_fee = res['processing_fee']
        draft.processing_fee_vat = res['processing_fee_vat']
        draft.total = res['amount_total']
        draft.save(update_fields=[
            'payment_method', 'beam_link_id', 'payment_uri_cache', 'vat_amount',
            'processing_fee', 'processing_fee_vat', 'total',
        ])
    return {
        'method': 'card',
        'qr_image': '',
        'payment_uri': res['payment_uri'],
        'total': float(draft.total),
    }


def gateway_status(draft: SelfOrder) -> str:
    """Ask the gateway what happened.  Returns pending | successful | failed.

    Never trusts the caller — this is the only thing that can move a draft to
    'paid', and it always costs an outbound HTTP call.
    """
    if draft.beam_charge_id:
        res = gateways.beam_get_charge(draft.beam_charge_id, branch=draft.branch)
        raw = (res['status'] or '').upper()
        if raw in gateways.BEAM_CHARGE_SUCCESS:
            return 'successful'
        if raw in gateways.BEAM_CHARGE_FAILED:
            return 'failed'
        return 'pending'
    if draft.beam_link_id:
        return gateways.beam_get_link(draft.beam_link_id, branch=draft.branch)['status']
    return 'pending'


def promote(draft: SelfOrder) -> Order | None:
    """Turn a paid draft into a real sale.  Idempotent.

    Called from three places that can race each other: the customer's status
    page, the POS print poller, and the sweeper.  Exactly one Order may result.

    The gateway call happens *outside* the lock on purpose — holding a row lock
    across a 10-15s httpx call would pin a gunicorn worker per concurrent poller.
    """
    if draft.status == 'paid':
        return draft.order
    if draft.status in ('failed', 'expired'):
        return None

    status = gateway_status(draft)          # network — no lock held

    if status == 'failed':
        SelfOrder.objects.filter(pk=draft.pk, status='pending').update(status='failed')
        return None
    if status != 'successful':
        return None

    with transaction.atomic():
        # Re-read under the lock. Another poller may have promoted this draft
        # while we were talking to the gateway.
        locked = SelfOrder.objects.select_for_update().get(pk=draft.pk)
        if locked.status == 'paid':
            return locked.order
        if locked.status != 'pending':
            return None

        subtotal, discount = bill_figures(locked.items)
        order = create_order_from_items(
            branch=locked.branch,
            items=[it for it in locked.items if not it.get('is_free')],
            free_items=[it for it in locked.items if it.get('is_free')],
            payment_method=locked.payment_method,
            # Attribute the sale to the round it was PLACED in, not the one open
            # when the payment confirmation lands — those differ when a customer
            # pays just before the cashier closes the round and the sweeper
            # confirms just after.  Falls back to the time window in
            # ``shift_orders`` if the draft predated any shift.
            shift=_placed_in_shift(locked),
            # Frozen — the customer was quoted this and the gateway already
            # captured it. Recomputing could disagree if an admin edited the fee
            # or VAT rate while the customer was paying.
            charges={
                'vat_amount': locked.vat_amount,
                'processing_fee': locked.processing_fee,
                'processing_fee_vat': locked.processing_fee_vat,
                'total': locked.total,
            },
            goods_total=locked.goods_total,
            subtotal=subtotal,
            discount_type='item' if discount > 0 else 'none',
            discount_amount=discount,
            paid_amount=locked.total,
            status='completed',     # paid is done; the kanban is switched off
            source='kiosk',         # already a valid Order.SOURCE_CHOICES value
            # No session here, so no cashier name. The void/reprint copy reads
            # this back from the DB, so it must not be empty.
            staff='Self Order',
            gateway_ids={
                'beam_charge_id': locked.beam_charge_id,
                'beam_link_id': locked.beam_link_id,
            },
            trust_item_prices=False,   # re-price from the DB even now
        )

        locked.status = 'paid'
        locked.paid_at = djtz.now()
        locked.order = order
        locked.save(update_fields=['status', 'paid_at', 'order'])

    logger.info(
        'self-order promoted token=%s order=%s total=%s',
        draft.token[:8], order.order_number, order.total,
    )
    return order


def expire_stale() -> int:
    """Mark drafts nobody ever paid as expired.  Returns how many."""
    cutoff = djtz.now() - timedelta(minutes=DRAFT_TTL_MINUTES)
    return (
        SelfOrder.objects
        .filter(status='pending', created_at__lt=cutoff)
        .update(status='expired')
    )
