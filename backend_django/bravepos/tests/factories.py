"""Shared fixtures + a stub gateway.

There is no Beam sandbox account, so every test drives a fake gateway. The
seam is deliberate: ``bravepos.gateways`` is plain functions with no request
object, precisely so they can be swapped for these.
"""
from __future__ import annotations

from decimal import Decimal
from unittest import mock

from bravepos.models import Branch, Category, Product, Settings, Shift


def make_shop(tax_percent=7, beam_fee=Decimal('3.65')) -> Settings:
    s, _ = Settings.objects.get_or_create(id='shop')
    s.tax_percent = tax_percent
    s.beam_card_fee_percent = beam_fee
    s.omise_fee_percent = beam_fee
    s.beam_merchant_id = 'test-merchant'
    s.beam_api_key = 'test-key'
    s.beam_sandbox = True
    s.save()
    return s


def make_branch(name='Silom', self_order_enabled=True) -> Branch:
    # Tests default to self-ordering ON so they exercise the live path; the
    # gating test flips it off explicitly.
    return Branch.objects.create(
        name=name, active=True, self_order_enabled=self_order_enabled,
    )


def open_shift(branch) -> Shift:
    return Shift.objects.create(branch=branch, status='open', round_number=1)


def make_product(branch, name='Latte', price='100.00', stock=50) -> Product:
    cat, _ = Category.objects.get_or_create(
        branch=branch, name='Drinks', defaults={'color': '#fff', 'order': 1},
    )
    return Product.objects.create(
        branch=branch, category=cat, name=name,
        price=Decimal(price), stock=stock, active=True,
    )


class StubGateway:
    """Context manager patching every outbound gateway call.

    ``paid`` flips what the status endpoints report, so a test can create a
    charge, assert nothing was promoted, then mark it paid and assert it was.
    """

    def __init__(self, paid=False, link_status=None, charge_status=None):
        self.paid = paid
        self.link_status = link_status
        self.charge_status = charge_status
        self.created_charges = []
        self.created_links = []
        self._patches = []

    # — Beam PromptPay —  (accept branch: gateway calls are now branch-aware)
    def _create_charge(self, amount, reference_id, description='', branch=None):
        self.created_charges.append({'amount': amount, 'reference_id': reference_id, 'branch': branch})
        return {
            'charge_id': f'chg_{len(self.created_charges)}',
            'status': 'PENDING',
            'qr_image': 'iVBORw0KGgo=',      # 1px png, enough to assert on
            'qr_string': '00020101',
            'amount': Decimal(str(amount)),
            'currency': 'THB',
            'raw': {},
        }

    def _get_charge(self, charge_id, branch=None):
        status = self.charge_status or ('SUCCEEDED' if self.paid else 'PENDING')
        return {
            'charge_id': charge_id, 'status': status,
            'amount': Decimal('0'), 'currency': 'THB', 'raw': {},
        }

    # — Beam card link —
    def _create_link(self, goods_total, reference_id, description='', redirect_url=None, branch=None):
        from bravepos.gateways import compute_order_charges, get_shop_settings
        s = get_shop_settings()
        charges = compute_order_charges(
            goods_total, is_card=True, settings=s, fee_percent=s.beam_card_fee_percent,
        )
        self.created_links.append({
            'goods_total': goods_total, 'redirect_url': redirect_url,
        })
        return {
            'link_id': f'lnk_{len(self.created_links)}',
            'payment_uri': 'https://pay.beam.test/lnk',
            'goods_total': goods_total,
            'vat_amount': charges['vat_amount'],
            'processing_fee': charges['processing_fee'],
            'processing_fee_vat': charges['processing_fee_vat'],
            'amount_total': charges['total'],
            'currency': 'THB',
            'raw': {},
        }

    def _get_link(self, link_id, branch=None):
        status = self.link_status or ('successful' if self.paid else 'pending')
        return {
            'link_id': link_id, 'status': status, 'charge_id': 'chg_x',
            'amount': Decimal('0'), 'currency': 'THB', 'raw': {},
        }

    def __enter__(self):
        targets = {
            'beam_create_promptpay_charge': self._create_charge,
            'beam_get_charge': self._get_charge,
            'beam_create_card_link': self._create_link,
            'beam_get_link': self._get_link,
        }
        for name, fn in targets.items():
            # Patch in every module that imported the symbol, not just its home.
            for mod in ('bravepos.gateways', 'bravepos.selforder'):
                try:
                    p = mock.patch(f'{mod}.{name}', side_effect=fn)
                    p.start()
                    self._patches.append(p)
                except AttributeError:
                    pass
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        self._patches.clear()
        return False
