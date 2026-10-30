"""PUBLIC, UNAUTHENTICATED views — customer self-ordering.

Everything in this file is reachable by anyone on the internet with no session,
and it moves real money. That is why it is its own file: ``backoffice/views.py``
is ``@login_required`` by convention on every view, and hiding an unauthenticated
gateway-touching flow in there means one forgotten decorator is a breach.

Mounted at the URL root (``/order/...``), mirroring the existing public
``/receipt/<order_number>/`` page, so it isn't gated by Django's auth login.

Trust model:
  * The **token** in the path is the only secret. It is
    ``secrets.token_urlsafe(32)`` — not the order number, which is a guessable
    running sequence (PS000000123) and already public on the receipt page.
  * The customer's browser is never trusted for money. Prices come from the DB
    (``selforder.price_cart``) and payment is confirmed by asking the gateway
    (``selforder.promote``), never by the client saying so.
  * CSRF is exempt on the POSTs for the same reason ``save_tax_invoice`` is —
    the customer isn't a logged-in user and has no CSRF token. The path token
    is what authorises the request.
"""
from __future__ import annotations

import io
import json
from decimal import Decimal, InvalidOperation

import segno
from django.conf import settings as django_settings
from django.http import Http404, HttpResponse, HttpResponseNotModified, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt

from . import images, selforder
from .gateways import GatewayConfigError, GatewayError
from .models import Branch, Product, SelfOrder
from .views import pick_suggestions


def _no_referrer(response):
    """Keep the cart token out of Referer headers on any outbound link/redirect
    (Beam's hosted checkout is off-site)."""
    response['Referrer-Policy'] = 'no-referrer'
    return response


def _get_draft(token: str) -> SelfOrder:
    return get_object_or_404(SelfOrder, token=token)


def _public_base() -> str:
    return django_settings.BRAVEPOS['PUBLIC_BASE_URL']


def _err(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({'status': 'error', 'error': message}, status=status)


def _summary_lines(draft: SelfOrder) -> list[dict]:
    """Order-summary rows for the pay page, with the amount per LINE.

    ``draft.items`` stores the *unit* price, and the template can't multiply
    (``widthratio`` only does integers, which is wrong for money), so the line
    total is computed here.  It has to be the line total: rendering the unit
    price against a "name × qty" label makes the rows visibly fail to add up to
    the total the customer is about to pay.  Discounts are their own row below
    the lines (``_summary_money``); a free item is listed at ฿0.
    """
    lines = []
    for it in draft.items or []:
        try:
            qty = int(it.get('qty') or 0)
            price = Decimal(str(it.get('price') or 0))
        except (TypeError, ValueError, InvalidOperation):
            continue
        lines.append({
            'name': it.get('name') or '',
            'qty': qty,
            'line_total': Decimal('0') if it.get('is_free') else price * qty,
            'is_free': bool(it.get('is_free')),
            'discount_label': it.get('discount_label') or '',
        })
    return lines


def _summary_money(draft: SelfOrder) -> dict:
    """The discount and whole-baht rounding rows between the lines and the total."""
    subtotal, discount = selforder.bill_figures(draft.items or [])
    rounding = draft.goods_total - max(Decimal('0'), subtotal - discount)
    return {'discount': discount, 'rounding': rounding, 'rounding_abs': abs(rounding)}


# ─── Menu ────────────────────────────────────────────────────────────────────
def order_menu(request, branch_id):
    """The page a customer lands on after scanning the branch QR."""
    branch = get_object_or_404(Branch, id=branch_id, active=True)
    open_now = selforder.branch_is_open(branch)
    return _no_referrer(render(request, 'selforder/menu.html', {
        'branch': branch,
        'open_now': open_now,
        'menu_json': json.dumps(selforder.menu_for(branch) if open_now else []),
        'start_url': f'/order/{branch.id}/start/',
        'price_url': f'/order/{branch.id}/price/',
        'suggest_url': f'/order/{branch.id}/suggest/',
    }))


# ─── Branch QR download ──────────────────────────────────────────────────────
def order_qr_png(request, branch_id):
    """The branch's self-ordering QR as a print-ready PNG download.

    The till has no native module that can write a file, so its Download button
    opens this URL and the tablet's browser saves it. Encodes the same URL the
    till's Self-Order QR screen shows (``PUBLIC_BASE_URL`` + ``/order/<id>/``).
    """
    branch = get_object_or_404(Branch, id=branch_id, active=True)
    if not selforder.self_order_enabled(branch):
        raise Http404

    buf = io.BytesIO()
    # scale=20 → ~700 px for a typical URL: sharp on an A6 table card.
    segno.make(f'{_public_base()}/order/{branch.id}/', error='m').save(
        buf, kind='png', scale=20, border=4,
    )
    resp = HttpResponse(buf.getvalue(), content_type='image/png')
    name = slugify(branch.name) or 'branch'
    resp['Content-Disposition'] = f'attachment; filename="self-order-qr-{name}.png"'
    return resp


# ─── Product image ───────────────────────────────────────────────────────────
def product_image(request, branch_id, product_id):
    """Serve one product image as its own cacheable resource.

    Product images are stored as base64 ``data:`` URIs, and ``menu_for`` used to
    paste them straight into the menu HTML. Three oversized rows made that page
    648 KB — and because they were *markup*, nothing rendered until all of it
    arrived and every visit re-downloaded the lot.

    Served from here instead, the browser fetches them in parallel, lazily, and
    keeps them. The ``?v=`` the menu appends is a content hash, so an image that
    changes gets a new URL and this one can be cached immutably for a year
    without ever going stale.
    """
    branch = get_object_or_404(Branch, id=branch_id, active=True)
    # Same gate as the menu itself: the feature flag, not the unguessable-ness
    # of the URL, is what keeps an opted-out branch closed.
    if not selforder.self_order_enabled(branch):
        raise Http404

    product = get_object_or_404(Product, id=product_id, branch=branch, active=True)
    raw = product.image_url or product.image_base64 or ''
    decoded = images.decode(raw)
    if decoded is None:
        # Hosted URL or no image — the menu links straight to the origin in that
        # case and never points here.
        raise Http404

    data, mime = decoded
    etag = f'"{images.digest(raw)}"'
    if request.headers.get('If-None-Match') == etag:
        return HttpResponseNotModified()

    resp = HttpResponse(data, content_type=mime)
    resp['ETag'] = etag
    resp['Content-Length'] = str(len(data))
    # Only promise immutability when the caller asked for the version we
    # actually have; a bare/stale ``?v=`` must stay revalidatable or a renamed
    # image would be pinned in phone caches for a year.
    if request.GET.get('v') == images.digest(raw):
        resp['Cache-Control'] = 'public, max-age=31536000, immutable'
    else:
        resp['Cache-Control'] = 'public, max-age=300'
    return resp


# ─── Create a draft ──────────────────────────────────────────────────────────
@csrf_exempt
def order_start(request, branch_id):
    if request.method != 'POST':
        return _err('POST required', 405)

    branch = get_object_or_404(Branch, id=branch_id, active=True)

    # Re-check the shift here, not just at page render — a customer can sit on
    # the menu for 20 minutes and the shop can close underneath them.
    if not selforder.branch_is_open(branch):
        return _err('ร้านปิดรับออร์เดอร์แล้ว / This branch is closed.', 409)

    try:
        payload = json.loads(request.body or '{}')
    except ValueError:
        return _err('Malformed request.')

    try:
        draft = selforder.create_draft(branch, payload.get('items') or [])
    except selforder.SelfOrderError as e:
        return _err(str(e))

    return JsonResponse({
        'status': 'ok',
        'token': draft.token,
        'pay_url': f'/order/s/{draft.token}/',
        'goods_total': float(draft.goods_total),
    })


# ─── Price the cart ──────────────────────────────────────────────────────────
@csrf_exempt
def order_price(request, branch_id):
    """What the cart costs, with the discounts each line may pick.

    The menu calls this on every cart change so the page shows the server's
    numbers instead of computing money itself.  Nothing is saved.
    """
    if request.method != 'POST':
        return _err('POST required', 405)

    branch = get_object_or_404(Branch, id=branch_id, active=True)
    if not selforder.branch_is_open(branch):
        return _err('ร้านปิดรับออร์เดอร์แล้ว / This branch is closed.', 409)

    try:
        payload = json.loads(request.body or '{}')
    except ValueError:
        return _err('Malformed request.')

    try:
        res = selforder.preview(branch, payload.get('items') or [])
    except selforder.SelfOrderError as e:
        return _err(str(e))
    return JsonResponse({'status': 'ok', **res})


# ─── Frequently bought together ──────────────────────────────────────────────
@csrf_exempt
def order_suggest(request, branch_id):
    """What to offer alongside the cart — the till's upsell strip, for the
    customer.  Ids and reasons only: the page already holds the menu, and a
    product it doesn't hold (or holds as sold out) is simply not shown.

    On at every branch: ``Branch.suggestions_enabled`` switches the *till's*
    strip only.  The pairings are learned across all branches anyway."""
    if request.method != 'POST':
        return _err('POST required', 405)

    branch = get_object_or_404(Branch, id=branch_id, active=True)
    if not selforder.branch_is_open(branch):
        return JsonResponse({'status': 'ok', 'suggestions': []})

    try:
        payload = json.loads(request.body or '{}')
    except ValueError:
        return _err('Malformed request.')

    picked, _source = pick_suggestions(branch, payload.get('product_ids'), payload.get('limit'))
    return JsonResponse({'status': 'ok', 'suggestions': [
        {'id': s['id'], 'reason': s['reason'], 'because_id': s['because_id']}
        for s in picked
    ]})


# ─── Pay ─────────────────────────────────────────────────────────────────────
def order_pay_page(request, token):
    """Payment page: pick PromptPay or card, then poll for confirmation.

    This is also Beam's ``redirectUrl`` for the card flow — the customer lands
    back here after the hosted checkout and the page's poll picks up the result.
    """
    draft = _get_draft(token)
    return _no_referrer(render(request, 'selforder/pay.html', {
        'draft': draft,
        'branch': draft.branch,
        'lines': _summary_lines(draft),
        'money': _summary_money(draft),
        'quote_qr': selforder.quote(draft, 'qr'),
        'quote_card': selforder.quote(draft, 'card'),
        'pay_url': f'/order/s/{draft.token}/pay/',
        'status_url': f'/order/s/{draft.token}/status/',
    }))


def _device_type(request) -> str:
    """The phone the customer is on, as Beam wants it: the banking apps only
    open from a charge made for a phone."""
    ua = request.META.get('HTTP_USER_AGENT', '')
    if 'Android' in ua:
        return 'ANDROID'
    if any(k in ua for k in ('iPhone', 'iPad', 'iPod')):
        return 'IOS'
    return 'WEB'


@csrf_exempt
def order_pay(request, token):
    """Create (or replay) the gateway charge/link for this draft."""
    if request.method != 'POST':
        return _err('POST required', 405)

    draft = _get_draft(token)
    if draft.status != 'pending':
        # Already paid/expired — send them to the status endpoint's verdict.
        return _err('This order is no longer awaiting payment.', 409)

    try:
        payload = json.loads(request.body or '{}')
    except ValueError:
        return _err('Malformed request.')

    try:
        res = selforder.start_payment(
            draft,
            method=payload.get('method', ''),
            # Beam bounces the browser back here after the hosted checkout.
            # Must be the customer's own status page — the gateway default
            # points at the POS host, which is right for a cashier and wrong here.
            redirect_url=f'{_public_base()}/order/s/{draft.token}/',
            device_type=_device_type(request),
        )
    except selforder.SelfOrderError as e:
        return _err(str(e))
    except GatewayConfigError as e:
        return _err(str(e), 400)
    except GatewayError as e:
        return _err(e.detail, e.status)

    return JsonResponse({'status': 'ok', **res})


# ─── Poll ────────────────────────────────────────────────────────────────────
def order_status(request, token):
    """Ask the gateway, promote on success, tell the customer where they stand.

    Deliberately side-effecting on GET (it can create the Order). That is safe
    because promotion is idempotent, and it is what lets the *customer's own
    poll* be one of the things that can confirm a payment.
    """
    draft = _get_draft(token)

    try:
        order = selforder.promote(draft)
    except (GatewayError, GatewayConfigError) as e:
        # Don't strand the customer on a gateway hiccup — tell them to keep
        # polling. The sweeper will confirm it even if they give up.
        detail = getattr(e, 'detail', str(e))
        return JsonResponse({'status': 'pending', 'detail': detail}, status=202)

    draft.refresh_from_db()

    if draft.status == 'paid' and order is not None:
        return JsonResponse({
            'status': 'paid',
            'order_number': order.order_number,
            'queue_number': order.queue_number,
            'total': float(order.total),
            # /receipt/<n>/ redirects to the feedback form — see customer_receipt.
            'feedback_url': f'/receipt/{order.order_number}/',
        })
    if draft.status in ('failed', 'expired'):
        return JsonResponse({'status': draft.status}, status=200)

    return JsonResponse({'status': 'pending'}, status=202)
