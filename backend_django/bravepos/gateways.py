"""Payment gateway layer — Beam + Omise.

Everything here is deliberately free of ``request`` / DRF: plain functions that
take values, do HTTP, and return dicts or raise.  That is what lets the *same*
code serve two very different callers:

  * the authenticated POS views in ``views.py`` (a cashier at the till), and
  * the public self-ordering views in ``public_views.py`` (a customer's phone),
    which have no session and must never be given one.

It also makes the gateways stubbable in tests without a live Beam account —
patch these functions, not httpx.

The money math lives here too (``compute_order_charges``), because the fee and
VAT percentages are what turn a goods total into the satang we hand the
gateway; splitting them apart invites the two drifting.

Mirrors the existing ``peak.py`` as the precedent for a non-view integration
module.
"""
from __future__ import annotations

import base64
from decimal import ROUND_HALF_UP, Decimal

import httpx
from django.conf import settings as django_settings

from .models import Settings

# ─── Constants ───────────────────────────────────────────────────────────────
SATANG_PER_THB = 100

BEAM_POST_TIMEOUT_S = 15.0
BEAM_GET_TIMEOUT_S = 10.0

OMISE_API_URL = 'https://api.omise.co'
OMISE_POST_TIMEOUT_S = 15.0
OMISE_GET_TIMEOUT_S = 10.0

# Payment-method strings.  Card-ness is detected from these (they are what the
# POS sends and what lands in Order.payment_method), so they are load-bearing —
# renaming one silently stops the processing fee being charged.
CARD_METHOD_PREFIX = 'Credit Card'
BEAM_CARD_METHOD = 'Beam Card'
BEAM_QR_METHOD = 'Beam QR'

# Beam's *charge* endpoint reports raw lifecycle strings; its *payment-link*
# endpoint reports a different vocabulary which we normalise.  The POS frontend
# hard-codes both (pos.tsx checks SUCCEEDED/COMPLETED for charges and
# 'successful' for links), so the two shapes must stay as they are.
BEAM_CHARGE_SUCCESS = ('SUCCEEDED', 'COMPLETED')
BEAM_CHARGE_FAILED = ('FAILED', 'EXPIRED', 'CANCELLED', 'VOIDED')


class GatewayConfigError(Exception):
    """Credentials missing/unconfigured — the caller's fault, surface as 400."""


class GatewayError(Exception):
    """The gateway rejected us or is unreachable.  Carries an HTTP status."""

    def __init__(self, detail: str, status: int = 502):
        super().__init__(detail)
        self.detail = detail
        self.status = status


def get_shop_settings() -> Settings:
    """The Settings singleton (id='shop')."""
    obj, _ = Settings.objects.get_or_create(id='shop')
    return obj


# ─── Payment config resolution (shop-wide, with per-branch override) ──────────
class PaymentConfig:
    """The effective payment settings for a given branch.

    Duck-types the attributes both the credential helpers AND
    ``compute_order_charges`` read off a ``Settings`` object, so it can be passed
    wherever a ``Settings`` used to be.

    ``tax_percent`` is always the shop-wide value (VAT is a company-level rate).
    The gateway credentials and fee percentages come from the *branch* when that
    branch has opted in (``payment_own``), otherwise from the shop singleton.
    """

    __slots__ = (
        'tax_percent',
        'beam_merchant_id', 'beam_api_key', 'beam_sandbox', 'beam_card_fee_percent',
        'omise_public_key', 'omise_secret_key', 'omise_fee_percent',
        'source',
    )

    def __init__(self, shop: Settings, branch=None):
        # VAT is always the shop rate.
        self.tax_percent = shop.tax_percent

        use_branch = bool(branch is not None and getattr(branch, 'payment_own', False))
        src = branch if use_branch else shop
        self.source = 'branch' if use_branch else 'shop'

        self.beam_merchant_id = src.beam_merchant_id
        self.beam_api_key = src.beam_api_key
        self.beam_sandbox = src.beam_sandbox
        self.beam_card_fee_percent = src.beam_card_fee_percent
        self.omise_public_key = src.omise_public_key
        self.omise_secret_key = src.omise_secret_key
        self.omise_fee_percent = src.omise_fee_percent


def resolve_payment_config(branch=None) -> PaymentConfig:
    """Effective payment config for ``branch`` (or shop-wide if ``branch`` is None).

    A branch that hasn't opted in (the default) transparently uses the shop
    singleton, so existing single-account shops behave exactly as before.
    """
    return PaymentConfig(get_shop_settings(), branch)


# ─── Tax + card-fee math ─────────────────────────────────────────────────────
def _q2(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def compute_order_charges(
    goods_total: Decimal,
    is_card: bool,
    settings: Settings,
    fee_percent=None,
) -> dict:
    """Split a goods total into VAT + card fee + grand total.

    VAT is *inclusive*: menu prices already contain it, so we extract
    ``goods × p/(100+p)`` (7/107) rather than adding anything on top.

    The card processing fee is the opposite — it is *added*, and passed on to
    the customer, with VAT charged on the fee itself.  So the grand total the
    gateway captures is ``goods + fee + fee_vat``, and it differs by payment
    method.  Cash/PromptPay pay exactly the goods total.
    """
    p = Decimal(str(settings.tax_percent or 0))
    goods_total = Decimal(str(goods_total or 0))
    vat_amount = _q2(goods_total * p / (100 + p)) if p else Decimal('0.00')

    fee = Decimal('0.00')
    fee_vat = Decimal('0.00')
    if is_card:
        rate_src = fee_percent if fee_percent is not None else settings.omise_fee_percent
        rate = Decimal(str(rate_src or 0))
        fee = _q2(goods_total * rate / 100)
        fee_vat = _q2(fee * p / 100) if p else Decimal('0.00')

    return {
        'vat_amount': vat_amount,
        'processing_fee': fee,
        'processing_fee_vat': fee_vat,
        'total': _q2(goods_total + fee + fee_vat),
    }


def to_satang(amount: Decimal) -> int:
    return int((Decimal(str(amount)) * SATANG_PER_THB).to_integral_value(ROUND_HALF_UP))


# ─── Beam ────────────────────────────────────────────────────────────────────
def _beam_credentials(cfg: PaymentConfig) -> tuple[str, dict]:
    if not cfg.beam_merchant_id or not cfg.beam_api_key:
        where = (
            'this branch (Backoffice → Branch → Payment)'
            if cfg.source == 'branch'
            else 'Settings → Payment'
        )
        raise GatewayConfigError(
            f'Beam credentials not configured. Add the Merchant ID and API Key '
            f'in {where}.'
        )
    base = (
        django_settings.BRAVEPOS['BEAM_PLAYGROUND_URL']
        if cfg.beam_sandbox
        else django_settings.BRAVEPOS['BEAM_PRODUCTION_URL']
    )
    token = base64.b64encode(f"{cfg.beam_merchant_id}:{cfg.beam_api_key}".encode()).decode()
    return base, {'Authorization': f'Basic {token}'}


def _extract_qr_data(data: dict) -> tuple[str | None, str | None]:
    qr_image, qr_string = None, None
    if data.get('actionRequired') == 'ENCODED_IMAGE':
        e = data.get('encodedImage', {})
        qr_image = e.get('imageBase64Encoded') or e.get('image')
        qr_string = e.get('rawData') or e.get('qrString')
    elif data.get('qrCode'):
        qr_image = data['qrCode']
    return qr_image, qr_string


def _beam_request(method: str, path: str, *, cfg: PaymentConfig, json=None, timeout: float) -> dict:
    base, headers = _beam_credentials(cfg)
    url = f"{base}{path}"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.request(method, url, json=json, headers=headers)
    except httpx.TimeoutException:
        raise GatewayError('Beam API timed out. Please try again.', 502)
    except httpx.RequestError as e:
        raise GatewayError(f'Cannot reach Beam API: {e}', 502)

    if resp.status_code == 401:
        raise GatewayError('Beam API key is invalid or expired.', 401)
    if resp.status_code not in (200, 201):
        raise GatewayError(f'Beam API error {resp.status_code}: {resp.text[:300]}', 502)
    return resp.json()


def beam_create_promptpay_charge(
    amount: Decimal, reference_id: str, description: str = '', branch=None,
) -> dict:
    """PromptPay QR charge.  ``amount`` is captured verbatim — PromptPay carries
    no processing fee, so this is the goods total with nothing added.

    ``branch`` selects whose Beam account to charge into: the branch's own if it
    has opted in, otherwise the shop's.  ``None`` = shop-wide (the POS default).
    """
    cfg = resolve_payment_config(branch)
    payload = {
        'amount': to_satang(amount),
        'currency': 'THB',
        'referenceId': reference_id,
        'description': description or f"Order {reference_id}",
        'paymentMethod': {'paymentMethodType': 'QR_PROMPT_PAY', 'qrPromptPay': {}},
    }
    data = _beam_request('POST', '/api/v1/charges', cfg=cfg, json=payload, timeout=BEAM_POST_TIMEOUT_S)

    charge_id = data.get('chargeId') or data.get('id') or data.get('charge_id') or ''
    if not charge_id:
        raise GatewayError(
            'Beam response did not include a charge id; cannot poll status.', 502,
        )
    qr_image, qr_string = _extract_qr_data(data)
    return {
        'charge_id': charge_id,
        'status': data.get('status', 'PENDING'),   # RAW Beam status — see BEAM_CHARGE_SUCCESS
        'qr_image': qr_image,
        'qr_string': qr_string,
        'amount': Decimal(str(amount)),
        'currency': 'THB',
        'raw': data,
    }


def beam_get_charge(charge_id: str, branch=None) -> dict:
    cfg = resolve_payment_config(branch)
    data = _beam_request(
        'GET', f'/api/v1/charges/{charge_id}', cfg=cfg, timeout=BEAM_GET_TIMEOUT_S,
    )
    return {
        'charge_id': data.get('chargeId') or data.get('id') or charge_id,
        'status': data.get('status', 'PENDING'),   # RAW — the POS matches on this
        'amount': Decimal(str((data.get('amount') or 0))) / SATANG_PER_THB,
        'currency': data.get('currency', 'THB'),
        'raw': data,
    }


def beam_create_card_link(
    goods_total: Decimal,
    reference_id: str,
    description: str = '',
    redirect_url: str | None = None,
    branch=None,
) -> dict:
    """Card hosted-checkout link.  ``goods_total`` is the pre-fee total; the
    customer is charged goods + card fee + VAT on the fee.

    ``redirect_url`` is where Beam bounces the customer's browser once they're
    done.  It used to be hardcoded to the POS host, which is right for a cashier
    (the customer never sees it) and wrong for self-ordering — a customer must
    land back on their own order-status page.

    ``branch`` selects whose Beam account + card fee rate apply.
    """
    cfg = resolve_payment_config(branch)
    goods_total = Decimal(str(goods_total or 0))
    charges = compute_order_charges(
        goods_total, is_card=True, settings=cfg, fee_percent=cfg.beam_card_fee_percent,
    )

    payload = {
        'order': {
            'netAmount': to_satang(charges['total']),
            'currency': 'THB',
            'referenceId': reference_id,
            'description': description or f"Order {reference_id}",
        },
        # Card only — this method is the card analogue of the QR PromptPay flow.
        # Each method is an object with an ``isEnabled`` flag (Beam's schema).
        'linkSettings': {
            'card': {'isEnabled': True},
            'cardInstallments': {'isEnabled': False},
            'buyNowPayLater': {'isEnabled': False},
            'eWallets': {'isEnabled': False},
            'mobileBanking': {'isEnabled': False},
            'qrPromptPay': {'isEnabled': False},
        },
        'redirectUrl': redirect_url or django_settings.BRAVEPOS.get(
            'PUBLIC_BASE_URL', 'https://pos.rollingpinn.com',
        ),
    }
    data = _beam_request(
        'POST', '/api/v1/payment-links', cfg=cfg, json=payload, timeout=BEAM_POST_TIMEOUT_S,
    )

    link_id = data.get('paymentLinkId') or data.get('id') or ''
    payment_uri = data.get('url') or data.get('paymentLinkUrl') or ''
    if not link_id or not payment_uri:
        raise GatewayError(
            'Beam response did not include a payment link id / URL.', 502,
        )
    return {
        'link_id': link_id,
        'payment_uri': payment_uri,
        'goods_total': goods_total,
        'vat_amount': charges['vat_amount'],
        'processing_fee': charges['processing_fee'],
        'processing_fee_vat': charges['processing_fee_vat'],
        'amount_total': charges['total'],
        'currency': 'THB',
        'raw': data,
    }


def beam_get_link(link_id: str, branch=None) -> dict:
    """Poll a Beam payment link.

    Returns a *normalised* status (successful | failed | pending) — unlike
    ``beam_get_charge``, which returns Beam's raw string.  Both shapes are
    hard-coded in pos.tsx, so neither can change.

    Also returns ``amount`` and ``raw`` so a caller promoting a draft into a
    real sale can check what the gateway actually captured.
    """
    cfg = resolve_payment_config(branch)
    data = _beam_request(
        'GET', f'/api/v1/payment-links/{link_id}', cfg=cfg, timeout=BEAM_GET_TIMEOUT_S,
    )

    # Link lifecycle: ACTIVE | PAID | EXPIRED | DISABLED | VOIDED | REFUNDED.
    beam_status = (data.get('status') or '').upper()
    if beam_status == 'PAID':
        status = 'successful'
    elif beam_status in ('EXPIRED', 'DISABLED', 'VOIDED', 'REFUNDED'):
        # REFUNDED used to fall through to 'pending', so a refunded link was
        # polled forever — by the POS on a 3s timer, and now by the sweeper.
        status = 'failed'
    else:
        status = 'pending'

    charge = data.get('charge') or {}
    charge_id = (
        data.get('chargeId')
        or charge.get('chargeId')
        or charge.get('id')
        or ''
    )
    order = data.get('order') or {}
    net = order.get('netAmount') or data.get('netAmount') or 0
    return {
        'link_id': link_id,
        'status': status,
        'charge_id': charge_id,
        'amount': Decimal(str(net)) / SATANG_PER_THB,
        'currency': 'THB',
        'raw': data,
    }


# ─── Omise ───────────────────────────────────────────────────────────────────
def _omise_headers(cfg: PaymentConfig) -> dict:
    """Omise uses the secret key as the basic-auth username with an empty
    password; the key prefix (skey_test_ vs skey_) selects test/live."""
    if not cfg.omise_secret_key:
        where = (
            'this branch (Backoffice → Branch → Payment)'
            if cfg.source == 'branch'
            else 'Settings → Payment'
        )
        raise GatewayConfigError(
            f'Omise credentials not configured. Add your Omise public + secret '
            f'keys in {where}.'
        )
    token = base64.b64encode(f"{cfg.omise_secret_key}:".encode()).decode()
    return {'Authorization': f'Basic {token}'}


def omise_create_link(
    goods_total: Decimal, title: str = '', description: str = '', branch=None,
) -> dict:
    cfg = resolve_payment_config(branch)
    headers = _omise_headers(cfg)
    goods_total = Decimal(str(goods_total or 0))
    charges = compute_order_charges(goods_total, is_card=True, settings=cfg)

    payload = {
        'amount': to_satang(charges['total']),
        'currency': 'thb',
        'title': title or 'POS Order',
        'description': description or '',
        # Single-use link — it cannot be paid twice.
        'multiple': 'false',
    }
    try:
        with httpx.Client(timeout=OMISE_POST_TIMEOUT_S) as client:
            resp = client.post(f"{OMISE_API_URL}/links", data=payload, headers=headers)
    except httpx.TimeoutException:
        raise GatewayError('Omise API timed out. Please try again.', 502)
    except httpx.RequestError as e:
        raise GatewayError(f'Cannot reach Omise API: {e}', 502)

    if resp.status_code in (401, 403):
        raise GatewayError('Omise secret key is invalid or expired.', 401)
    if resp.status_code not in (200, 201):
        raise GatewayError(f'Omise API error {resp.status_code}: {resp.text[:300]}', 502)

    data = resp.json()
    link_id = data.get('id') or ''
    payment_uri = data.get('payment_uri') or ''
    if not link_id or not payment_uri:
        raise GatewayError(
            'Omise response did not include a link id / payment URI.', 502,
        )
    return {
        'link_id': link_id,
        'payment_uri': payment_uri,
        'goods_total': goods_total,
        'vat_amount': charges['vat_amount'],
        'processing_fee': charges['processing_fee'],
        'processing_fee_vat': charges['processing_fee_vat'],
        'amount_total': charges['total'],
        'currency': 'THB',
        'raw': data,
    }


def omise_get_link(link_id: str, branch=None) -> dict:
    headers = _omise_headers(resolve_payment_config(branch))
    try:
        with httpx.Client(timeout=OMISE_GET_TIMEOUT_S) as client:
            resp = client.get(f"{OMISE_API_URL}/links/{link_id}", headers=headers)
    except httpx.TimeoutException:
        raise GatewayError('Omise API timed out.', 502)
    except httpx.RequestError as e:
        raise GatewayError(f'Cannot reach Omise API: {e}', 502)
    if resp.status_code != 200:
        raise GatewayError(f'Omise API error {resp.status_code}', 502)

    data = resp.json()
    charge_list = (data.get('charges') or {}).get('data') or []
    paid = next(
        (c for c in charge_list if c.get('status') == 'successful' and c.get('paid')),
        None,
    )
    failed = any(c.get('status') == 'failed' for c in charge_list)
    return {
        'link_id': link_id,
        'status': 'successful' if paid else ('failed' if (failed and not paid) else 'pending'),
        'charge_id': paid['id'] if paid else '',
        'amount': Decimal(str(paid.get('amount', 0))) / SATANG_PER_THB if paid else Decimal('0'),
        'currency': 'THB',
        'raw': data,
    }
