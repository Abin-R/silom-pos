"""Self-ordering: money, promotion races, print claims, orphan recovery.

The race tests use TransactionTestCase + real threads on purpose. TestCase wraps
each test in a transaction that never commits, which hides exactly the
concurrency bugs these are here to catch.
"""
from __future__ import annotations

import threading
from decimal import Decimal
from unittest import skipUnless

from django.db import connection, connections
from django.test import Client, TestCase, TransactionTestCase
from datetime import timedelta as timezone_timedelta

from django.utils import timezone as djtz

# The race tests rely on real SELECT ... FOR UPDATE row locking, which SQLite
# does not implement (it silently ignores it).  Skip them on non-Postgres
# backends so a SQLite run reports them as skipped rather than passing without
# actually exercising the lock — run them against Postgres to verify Phase 0.
REQUIRES_ROW_LOCKS = skipUnless(
    connection.vendor == 'postgresql',
    'concurrency tests require PostgreSQL row locking (SELECT ... FOR UPDATE)',
)

from bravepos import gateways, selforder
from bravepos.gateways import compute_order_charges
from bravepos.models import (
    Branch, DiscountType, Order, SelfOrder, Settings, StockDocument, SuggestionRule,
)
from bravepos.orders import create_order_from_items

from .factories import (
    StubGateway, make_branch, make_product, make_shop, open_shift,
)


def run_concurrently(fns):
    """Run each callable in its own thread, closing its DB connection after.

    Django gives every thread its own connection; leaking them wedges the test
    database teardown.
    """
    errors = []

    def wrap(fn):
        def inner():
            try:
                fn()
            except Exception as e:          # noqa: BLE001 — surfaced below
                errors.append(e)
            finally:
                connections.close_all()
        return inner

    threads = [threading.Thread(target=wrap(f)) for f in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return errors


# ─── Money ───────────────────────────────────────────────────────────────────
class ChargeMathTests(TestCase):
    def setUp(self):
        self.s = make_shop()

    def test_promptpay_adds_nothing(self):
        """PromptPay carries no processing fee — the customer pays the menu price."""
        c = compute_order_charges(Decimal('107.00'), is_card=False, settings=self.s)
        self.assertEqual(c['total'], Decimal('107.00'))
        self.assertEqual(c['processing_fee'], Decimal('0.00'))
        # VAT is *inclusive*: 7/107 of the goods total, extracted not added.
        self.assertEqual(c['vat_amount'], Decimal('7.00'))

    def test_card_adds_fee_and_vat_on_the_fee(self):
        c = compute_order_charges(
            Decimal('100.00'), is_card=True, settings=self.s,
            fee_percent=self.s.beam_card_fee_percent,
        )
        self.assertEqual(c['processing_fee'], Decimal('3.65'))       # 3.65%
        self.assertEqual(c['processing_fee_vat'], Decimal('0.26'))   # 7% of the fee
        self.assertEqual(c['total'], Decimal('103.91'))

    def test_frozen_charges_survive_a_settings_edit_mid_payment(self):
        """The gateway has already captured a specific number of satang.

        If an admin edits the fee while a customer is on the checkout page,
        promoting must still record what was actually charged — not a fresh
        recomputation that disagrees with the money taken.
        """
        branch = make_branch()
        open_shift(branch)
        p = make_product(branch, price='100.00')

        with StubGateway() as gw:
            draft = selforder.create_draft(branch, [{'product_id': str(p.id), 'qty': 1}])
            selforder.start_payment(draft, 'card', redirect_url='https://x/')
            draft.refresh_from_db()
            quoted_total = draft.total
            self.assertEqual(quoted_total, Decimal('103.91'))

            # Admin changes the fee AFTER the customer was quoted and charged.
            Settings.objects.filter(id='shop').update(beam_card_fee_percent=Decimal('10.00'))

            gw.paid = True
            order = selforder.promote(draft)

        self.assertEqual(order.total, quoted_total, 'promotion must not re-price')
        self.assertEqual(order.processing_fee, Decimal('3.65'))


# ─── Public endpoint hardening ───────────────────────────────────────────────
class PublicEndpointTests(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch()
        open_shift(self.branch)
        self.p = make_product(self.branch, price='100.00')
        self.c = Client()

    def test_client_supplied_price_is_ignored(self):
        """A tampered price must not be read at all — the DB is the only source."""
        res = self.c.post(
            f'/order/{self.branch.id}/start/',
            data={'items': [{'product_id': str(self.p.id), 'qty': 2, 'price': 0}]},
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 200)
        draft = SelfOrder.objects.get(token=res.json()['token'])
        self.assertEqual(draft.goods_total, Decimal('200.00'))
        self.assertEqual(Decimal(str(draft.items[0]['price'])), Decimal('100.00'))

    def test_menu_never_leaks_cost(self):
        """Stock IS shown to customers (sold-out / low-stock nudges, like a food
        app).  Cost — the margin — must never reach a customer's phone."""
        menu = selforder.menu_for(self.branch)
        self.assertTrue(menu)
        for item in menu:
            self.assertNotIn('cost', item)
            # Inventory is intentionally surfaced.
            self.assertIn('stock', item)
            self.assertIn('sold_out', item)

    def test_closed_branch_refuses_orders(self):
        """No open shift means nobody is at the till to see the order or the slip."""
        self.branch.shifts.update(status='closed', closed_at=djtz.now())
        res = self.c.post(
            f'/order/{self.branch.id}/start/',
            data={'items': [{'product_id': str(self.p.id), 'qty': 1}]},
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(SelfOrder.objects.count(), 0)

    def test_qty_and_line_caps(self):
        res = self.c.post(
            f'/order/{self.branch.id}/start/',
            data={'items': [{'product_id': str(self.p.id), 'qty': 9999}]},
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 400)

    def test_disabled_branch_is_closed_even_with_open_shift(self):
        """The feature flag — not the URL — is what keeps a branch closed.
        A branch with self_order_enabled=False must refuse orders even though it
        has an open shift and a valid (public) UUID."""
        self.branch.self_order_enabled = False
        self.branch.save()
        # Menu page still resolves (200) but must not accept an order.
        res = self.c.post(
            f'/order/{self.branch.id}/start/',
            data={'items': [{'product_id': str(self.p.id), 'qty': 1}]},
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(SelfOrder.objects.count(), 0)

    def test_qr_png_downloads_the_branch_menu_link(self):
        res = self.c.get(f'/order/{self.branch.id}/qr.png')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'], 'image/png')
        self.assertTrue(res.content.startswith(b'\x89PNG'))
        self.assertEqual(
            res['Content-Disposition'], 'attachment; filename="self-order-qr-silom.png"',
        )

    def test_qr_png_is_404_on_a_disabled_branch(self):
        self.branch.self_order_enabled = False
        self.branch.save()
        res = self.c.get(f'/order/{self.branch.id}/qr.png')
        self.assertEqual(res.status_code, 404)


# ─── Frequently bought together ──────────────────────────────────────────────
class SelfOrderSuggestionTests(TestCase):
    """The till's upsell strip on the menu: same rules, at every branch."""

    def setUp(self):
        make_shop()
        self.branch = make_branch()
        open_shift(self.branch)
        self.latte = make_product(self.branch, name='Latte')
        self.croissant = make_product(self.branch, name='Croissant', price='60.00')
        SuggestionRule.objects.create(
            branch=None, kind='pair', antecedent_key='latte', antecedent_size=1,
            consequent_name='croissant', lift=3, score=3, basket_count=20,
        )
        self.c = Client()

    def _suggest(self, *products):
        return self.c.post(
            f'/order/{self.branch.id}/suggest/',
            data={'product_ids': [str(p.id) for p in products]},
            content_type='application/json',
        )

    def test_offers_what_is_bought_together(self):
        res = self._suggest(self.latte)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['suggestions'], [
            {'id': str(self.croissant.id), 'reason': 'often_together',
             'because_id': str(self.latte.id)},
        ])

    def test_offered_where_the_tills_strip_is_switched_off(self):
        """The branch switch is the till's; self-order suggests everywhere."""
        self.branch.suggestions_enabled = False
        self.branch.save()
        self.assertEqual(len(self._suggest(self.latte).json()['suggestions']), 1)

    def test_closed_branch_offers_nothing(self):
        self.branch.shifts.update(status='closed', closed_at=djtz.now())
        self.assertEqual(self._suggest(self.latte).json()['suggestions'], [])

    def test_suggested_line_is_attributed_on_the_order(self):
        """A line added from the strip lands as ``suggested`` on the Order, as
        a till's does, so the uplift report counts self-orders too."""
        with StubGateway():
            d = selforder.create_draft(self.branch, [
                {'product_id': str(self.latte.id), 'qty': 1},
                {'product_id': str(self.croissant.id), 'qty': 1, 'suggested': True},
            ])
            selforder.start_payment(d, 'qr', redirect_url='https://x/')
        with StubGateway(paid=True):
            order = selforder.promote(SelfOrder.objects.get(pk=d.pk))
        by_name = {i.name: i.suggested for i in order.items.all()}
        self.assertEqual(by_name, {'Latte': False, 'Croissant': True})


# ─── Discounts the customer picks ────────────────────────────────────────────
class SelfOrderDiscountTests(TestCase):
    """The till's preset dropdown, on the menu: picked by the customer, checked
    and computed by the server (bravepos/discount_rules.py)."""

    def setUp(self):
        make_shop()
        self.branch = make_branch()
        self.branch.discount_types_enabled = True
        self.branch.save()
        open_shift(self.branch)
        self.latte = make_product(self.branch, name='Latte', price='90.00', stock=10)
        self.cake = make_product(self.branch, name='Cake', price='125.00', stock=10)
        self.cookie = make_product(self.branch, name='Cookie', price='25.00', stock=10)
        self.c = Client()

    def _preset(self, products=(), **kw):
        kw.setdefault('self_order', True)
        d = DiscountType.objects.create(branch=self.branch, **kw)
        if products:
            d.products.set(products)
        return d

    def _items(self, *lines):
        return [{'product_id': str(p.id), 'qty': q, **({'discount_type_id': str(d.id)} if d else {})}
                for p, q, d in lines]

    def test_picked_percent_discount_is_computed_here_and_rounds_the_bill(self):
        d = self._preset([self.cake], name='Cake 15%', kind='percent', value=15,
                         applies_to=DiscountType.APPLIES_PRODUCTS)
        items, goods = selforder.price_cart(self.branch, self._items((self.cake, 1, d)))
        # 15% of 125 = 18.75 off → 106.25, and a discounted bill rounds to the baht.
        self.assertEqual(items[0]['discount'], 18.75)
        self.assertEqual(items[0]['discount_label'], 'Cake 15%')
        self.assertEqual(goods, Decimal('106'))

    def test_a_discount_the_line_is_not_offered_is_ignored(self):
        cake_only = self._preset([self.cake], name='Cake 50%', kind='percent', value=50,
                                 applies_to=DiscountType.APPLIES_PRODUCTS)
        items, goods = selforder.price_cart(self.branch, self._items((self.latte, 1, cake_only)))
        self.assertEqual(items[0]['discount'], 0)
        self.assertEqual(goods, Decimal('90.00'))

    def test_an_unknown_or_other_branch_preset_is_ignored(self):
        other = make_branch(name='Elsewhere')
        foreign = DiscountType.objects.create(branch=other, name='All 90%', kind='percent', value=90)
        items, goods = selforder.price_cart(self.branch, self._items((self.latte, 1, foreign)))
        self.assertEqual(items[0]['discount'], 0)
        items, _ = selforder.price_cart(self.branch, [
            {'product_id': str(self.latte.id), 'qty': 1, 'discount_type_id': 'other'},
        ])
        self.assertEqual(items[0]['discount'], 0)

    def test_a_promotion_not_ticked_for_self_order_is_till_only(self):
        """Staff and near-expiry discounts need a cashier's judgement: unless a
        promotion is ticked for self-order, the website neither lists it nor
        accepts it."""
        staff = self._preset(name='TRP STAFF', kind='percent', value=25, self_order=False)
        res = selforder.preview(self.branch, self._items((self.latte, 1, staff)))
        self.assertEqual(res['lines'][0]['options'], [])
        self.assertEqual(res['discount'], 0)
        # The till still offers it.
        self.assertIn(str(staff.id), [p['id'] for p in
                                      selforder.discounts.preset_feed(self.branch, '3')])

    def test_branch_without_discount_types_offers_none(self):
        self.branch.discount_types_enabled = False
        self.branch.save()
        d = self._preset(name='All 10%', kind='percent', value=10)
        items, goods = selforder.price_cart(self.branch, self._items((self.latte, 1, d)))
        self.assertEqual(items[0]['discount'], 0)
        self.assertEqual(selforder.preview(self.branch, self._items((self.latte, 1, None)))
                         ['lines'][0]['options'], [])

    def test_expired_promotion_is_not_offered(self):
        d = self._preset(name='Ended', kind='percent', value=10,
                         end_date=djtz.localdate() - timezone_timedelta(days=1))
        items, _ = selforder.price_cart(self.branch, self._items((self.latte, 1, d)))
        self.assertEqual(items[0]['discount'], 0)

    def test_free_item_picked_from_a_category_is_not_offered_online(self):
        d = self._preset(name='Free drink', kind='free', value=0,
                         free_category=self.latte.category)
        lines = selforder.preview(self.branch, self._items((self.cake, 1, None)))['lines']
        self.assertNotIn(str(d.id), [o['id'] for o in lines[0]['options']])

    def test_preview_lists_options_and_the_bill(self):
        d = self._preset([self.latte], name='Latte ฿10', kind='fixed', value=10,
                         applies_to=DiscountType.APPLIES_PRODUCTS)
        res = self.c.post(f'/order/{self.branch.id}/price/',
                          data={'items': self._items((self.latte, 2, d), (self.cake, 1, None))},
                          content_type='application/json')
        self.assertEqual(res.status_code, 200)
        body = res.json()
        latte, cake = body['lines']
        self.assertEqual([o['id'] for o in latte['options']], [str(d.id)])
        self.assertEqual(latte['options'][0]['value'], '฿10 off')
        self.assertEqual(latte['discount'], 10)
        self.assertEqual(cake['options'], [])
        self.assertEqual(body['subtotal'], 305.0)
        self.assertEqual(body['discount'], 10.0)
        self.assertEqual(body['total'], 295.0)

    def test_paid_order_records_the_discount_and_gives_the_free_item(self):
        pct = self._preset([self.cake], name='Cake 15%', kind='percent', value=15,
                           applies_to=DiscountType.APPLIES_PRODUCTS, code='PR-0001')
        free = self._preset([self.latte], name='Free cookie', kind='free', value=0,
                            applies_to=DiscountType.APPLIES_PRODUCTS, free_product=self.cookie)
        with StubGateway() as gw:
            d = selforder.create_draft(self.branch, self._items((self.cake, 1, pct), (self.latte, 1, free)))
            selforder.start_payment(d, 'qr', redirect_url='https://x/')
        # 125 + 90 = 215, less 18.75 = 196.25 → rounds to 196; that is what Beam charges.
        self.assertEqual(d.goods_total, Decimal('196'))
        self.assertEqual(gw.created_charges[0]['amount'], Decimal('196'))
        with StubGateway(paid=True):
            order = selforder.promote(SelfOrder.objects.get(pk=d.pk))

        self.assertEqual(order.subtotal, Decimal('215.00'))
        self.assertEqual(order.discount_amount, Decimal('18.75'))
        self.assertEqual(order.discount_type, 'item')
        self.assertEqual(order.total, Decimal('196.00'))
        cake_line = order.items.get(product_id=self.cake.id)
        self.assertEqual(cake_line.discount, Decimal('18.75'))
        self.assertEqual(cake_line.discount_code, 'PR-0001')
        # The cookie is not on the bill; it leaves stock on a "Free item" document.
        self.assertFalse(order.items.filter(product_id=self.cookie.id).exists())
        self.cookie.refresh_from_db()
        self.assertEqual(self.cookie.stock, 9)
        self.assertTrue(StockDocument.objects.filter(ref_no=order.order_number).exists())

    def test_pay_page_shows_the_discount(self):
        d = self._preset([self.cake], name='Cake 15%', kind='percent', value=15,
                         applies_to=DiscountType.APPLIES_PRODUCTS)
        draft = selforder.create_draft(self.branch, self._items((self.cake, 1, d)))
        res = self.c.get(f'/order/s/{draft.token}/')
        self.assertContains(res, 'Cake 15%')
        self.assertContains(res, '−฿18.75')
        self.assertContains(res, '฿106.00')
        self.assertContains(res, '−฿0.25')


# ─── Branch-level payment credentials ────────────────────────────────────────
class BranchPaymentConfigTests(TestCase):
    """Every branch owns its payment config; the shop row only seeds new ones."""

    def setUp(self):
        self.shop = make_shop()
        self.shop.beam_merchant_id = 'SHOP-MERCHANT'
        self.shop.beam_api_key = 'shop-key'
        self.shop.beam_card_fee_percent = Decimal('3.65')
        self.shop.save()
        self.branch = make_branch()

    def test_new_branch_is_seeded_from_the_shop_template(self):
        """Creating a branch copies the shop credentials onto it — so a branch
        can take payments the moment it exists, with no manual step."""
        from bravepos.gateways import resolve_payment_config
        self.assertEqual(self.branch.beam_merchant_id, 'SHOP-MERCHANT')
        self.assertEqual(self.branch.beam_api_key, 'shop-key')

        cfg = resolve_payment_config(self.branch)
        self.assertEqual(cfg.source, 'branch')
        self.assertEqual(cfg.beam_merchant_id, 'SHOP-MERCHANT')

    def test_editing_the_template_does_not_touch_existing_branches(self):
        """The shop row is a template, not a live parent.  Changing it must never
        redirect money at a branch that is already trading."""
        from bravepos.gateways import resolve_payment_config
        self.shop.beam_merchant_id = 'NEW-MERCHANT'
        self.shop.beam_api_key = 'new-key'
        self.shop.save()

        cfg = resolve_payment_config(self.branch)
        self.assertEqual(cfg.beam_merchant_id, 'SHOP-MERCHANT')
        self.assertEqual(cfg.beam_api_key, 'shop-key')

    def test_no_branch_resolves_to_shop(self):
        from bravepos.gateways import resolve_payment_config
        self.assertEqual(resolve_payment_config(None).beam_merchant_id, 'SHOP-MERCHANT')

    def test_branch_uses_its_own_keys_and_fee(self):
        from bravepos.gateways import resolve_payment_config
        self.branch.beam_merchant_id = 'BRANCH-MERCHANT'
        self.branch.beam_api_key = 'branch-key'
        self.branch.beam_card_fee_percent = Decimal('2.00')
        self.branch.save()

        cfg = resolve_payment_config(self.branch)
        self.assertEqual(cfg.source, 'branch')
        self.assertEqual(cfg.beam_merchant_id, 'BRANCH-MERCHANT')
        self.assertEqual(cfg.beam_card_fee_percent, Decimal('2.00'))
        # VAT is always shop-wide, even though credentials are per-branch.
        self.assertEqual(cfg.tax_percent, self.shop.tax_percent)

    def test_unconfigured_live_branch_falls_back_to_shop(self):
        """Belt and braces for a missed backfill: a live branch with no keys of
        its own keeps trading on the shop account rather than erroring at the
        till."""
        from bravepos.gateways import resolve_payment_config
        Branch.objects.filter(pk=self.branch.pk).update(
            beam_merchant_id='', beam_api_key='',
            omise_public_key='', omise_secret_key='',
            beam_sandbox=False,
        )
        cfg = resolve_payment_config(Branch.objects.get(pk=self.branch.pk))
        self.assertEqual(cfg.source, 'shop')
        self.assertEqual(cfg.beam_merchant_id, 'SHOP-MERCHANT')

    def test_unconfigured_test_branch_never_falls_back_to_live_keys(self):
        """The one case worth refusing: a test branch must not quietly reach for
        the shop's (live) account."""
        from bravepos.gateways import GatewayConfigError, resolve_payment_config
        Branch.objects.filter(pk=self.branch.pk).update(
            beam_merchant_id='', beam_api_key='',
            omise_public_key='', omise_secret_key='',
            beam_sandbox=True,
        )
        branch = Branch.objects.get(pk=self.branch.pk)

        cfg = resolve_payment_config(branch)
        self.assertEqual(cfg.source, 'branch')
        self.assertEqual(cfg.beam_merchant_id, '')

        with self.assertRaises(GatewayConfigError):
            gateways._beam_credentials(cfg)

    def test_self_order_charge_routes_to_branch_account(self):
        """A self-order charges the account belonging to that branch."""
        open_shift(self.branch)
        p = make_product(self.branch, price='100.00')
        self.branch.beam_merchant_id = 'BRANCH-MERCHANT'
        self.branch.beam_api_key = 'branch-key'
        self.branch.save()

        with StubGateway() as gw:
            d = selforder.create_draft(self.branch, [{'product_id': str(p.id), 'qty': 1}])
            selforder.start_payment(d, 'qr', redirect_url='https://x/')

        # The stub records the branch each charge was created for.
        self.assertEqual(gw.created_charges[0]['branch'], self.branch)


# ─── Payment idempotency ─────────────────────────────────────────────────────
class StartPaymentTests(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch()
        open_shift(self.branch)
        self.p = make_product(self.branch, price='100.00')

    def test_second_pay_call_does_not_mint_a_second_charge(self):
        """Otherwise a customer ends up holding a live PromptPay QR *and* a live
        card link, for different amounts, both payable."""
        with StubGateway() as gw:
            draft = selforder.create_draft(self.branch, [{'product_id': str(self.p.id), 'qty': 1}])
            first = selforder.start_payment(draft, 'qr', redirect_url='https://x/')
            draft.refresh_from_db()
            second = selforder.start_payment(draft, 'card', redirect_url='https://x/')

        self.assertEqual(len(gw.created_charges), 1)
        self.assertEqual(len(gw.created_links), 0, 'the card call must not create a link')
        self.assertEqual(second['method'], 'qr')          # first method wins
        self.assertEqual(first['qr_image'], second['qr_image'])

    def test_card_link_redirects_to_the_customers_own_status_page(self):
        """Not the POS host — the customer must land back on their order."""
        with StubGateway() as gw:
            draft = selforder.create_draft(self.branch, [{'product_id': str(self.p.id), 'qty': 1}])
            selforder.start_payment(
                draft, 'card', redirect_url=f'https://shop.test/order/s/{draft.token}/',
            )
        self.assertIn(draft.token, gw.created_links[0]['redirect_url'])


    def test_wallet_charge_is_the_goods_total_and_returns_to_the_order(self):
        """An e-wallet / banking app adds no fee, and Beam sends the customer
        back to their own order page."""
        with StubGateway() as gw:
            draft = selforder.create_draft(self.branch, [{'product_id': str(self.p.id), 'qty': 1}])
            res = selforder.start_payment(
                draft, 'KPLUS', redirect_url=f'https://shop.test/order/s/{draft.token}/',
                device_type='IOS',
            )
        draft.refresh_from_db()
        charge = gw.created_charges[0]
        self.assertEqual(charge['method_type'], 'KPLUS')
        self.assertEqual(charge['device_type'], 'IOS')
        self.assertIn(draft.token, charge['return_url'])
        self.assertEqual(charge['amount'], Decimal('100.00'))
        self.assertEqual(draft.total, Decimal('100.00'))
        self.assertEqual(draft.payment_method, 'Beam KPlus')
        self.assertEqual(res['payment_uri'], 'https://pay.beam.test/app')

    def test_wallet_replay_hands_back_the_same_link(self):
        with StubGateway() as gw:
            draft = selforder.create_draft(self.branch, [{'product_id': str(self.p.id), 'qty': 1}])
            first = selforder.start_payment(draft, 'LINE_PAY', redirect_url='https://x/')
            draft.refresh_from_db()
            second = selforder.start_payment(draft, 'qr', redirect_url='https://x/')
        self.assertEqual(len(gw.created_charges), 1)
        self.assertEqual(second['method'], 'app')
        self.assertEqual(first['payment_uri'], second['payment_uri'])

    def test_unknown_method_is_refused(self):
        with StubGateway():
            draft = selforder.create_draft(self.branch, [{'product_id': str(self.p.id), 'qty': 1}])
            with self.assertRaises(selforder.SelfOrderError):
                selforder.start_payment(draft, 'MAKE', redirect_url='https://x/')

    def test_paid_wallet_order_refunds_through_beam_on_void(self):
        with StubGateway() as gw:
            draft = selforder.create_draft(self.branch, [{'product_id': str(self.p.id), 'qty': 1}])
            selforder.start_payment(draft, 'TRUE_MONEY', redirect_url='https://x/')
            gw.paid = True
            order = selforder.promote(SelfOrder.objects.get(pk=draft.pk))
        self.assertEqual(order.payment_method, 'Beam TrueMoney')
        self.assertEqual(order.beam_charge_id, 'chg_1')
        self.assertIn(order.payment_method, gateways.BEAM_METHODS)


# ─── Promotion ───────────────────────────────────────────────────────────────
class PromotionTests(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch()
        open_shift(self.branch)
        self.p = make_product(self.branch, price='100.00', stock=10)

    def _draft(self, qty=1):
        with StubGateway():
            d = selforder.create_draft(self.branch, [{'product_id': str(self.p.id), 'qty': qty}])
            selforder.start_payment(d, 'qr', redirect_url='https://x/')
        return SelfOrder.objects.get(pk=d.pk)

    def test_unpaid_draft_creates_no_order(self):
        d = self._draft()
        with StubGateway(paid=False):
            self.assertIsNone(selforder.promote(d))
        self.assertEqual(Order.objects.count(), 0)

    def test_paid_draft_becomes_a_kiosk_order(self):
        d = self._draft(qty=2)
        with StubGateway(paid=True):
            order = selforder.promote(d)

        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(order.source, 'kiosk')
        self.assertEqual(order.status, 'completed')
        self.assertEqual(order.staff, 'Self Order')
        self.assertEqual(order.total, Decimal('200.00'))
        self.assertIsNotNone(order.queue_number)
        # Stock moved, and the sale is attributed to the open shift.
        self.p.refresh_from_db()
        self.assertEqual(self.p.stock, 8)
        self.assertIsNotNone(order.shift)

    def test_promotion_is_idempotent(self):
        d = self._draft()
        with StubGateway(paid=True):
            a = selforder.promote(d)
            b = selforder.promote(SelfOrder.objects.get(pk=d.pk))
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(Order.objects.count(), 1)

    def test_refunded_link_resolves_instead_of_polling_forever(self):
        """REFUNDED used to fall through to 'pending', so a refunded link was
        polled on a 3s timer indefinitely."""
        with StubGateway() as gw:
            d = selforder.create_draft(self.branch, [{'product_id': str(self.p.id), 'qty': 1}])
            selforder.start_payment(d, 'card', redirect_url='https://x/')
            gw.link_status = 'failed'      # what gateways maps REFUNDED to now
            self.assertIsNone(selforder.promote(SelfOrder.objects.get(pk=d.pk)))

        d.refresh_from_db()
        self.assertEqual(d.status, 'failed')
        self.assertEqual(Order.objects.count(), 0)


# ─── Orphan recovery (the sweeper) ───────────────────────────────────────────
class ReconcileTests(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch()
        open_shift(self.branch)
        self.p = make_product(self.branch, price='100.00')

    def test_sweeper_promotes_a_draft_nobody_polled(self):
        """The customer paid and closed the tab; the POS was asleep. The money
        is with Beam — without the sweeper there is no order and no receipt."""
        from django.core.management import call_command
        from io import StringIO

        with StubGateway() as gw:
            d = selforder.create_draft(self.branch, [{'product_id': str(self.p.id), 'qty': 1}])
            selforder.start_payment(d, 'qr', redirect_url='https://x/')
            self.assertEqual(Order.objects.count(), 0)   # nobody polled

            gw.paid = True
            out = StringIO()
            call_command('reconcile_selforders', stdout=out)

        self.assertEqual(Order.objects.count(), 1)
        d.refresh_from_db()
        self.assertEqual(d.status, 'paid')
        self.assertIsNotNone(d.order)

    def test_sweeper_ignores_drafts_that_never_reached_a_gateway(self):
        selforder.create_draft(self.branch, [{'product_id': str(self.p.id), 'qty': 1}])
        from django.core.management import call_command
        from io import StringIO
        with StubGateway(paid=True):
            call_command('reconcile_selforders', stdout=StringIO())
        self.assertEqual(Order.objects.count(), 0)


# ─── Shift attribution ───────────────────────────────────────────────────────
class ShiftAttributionTests(TestCase):
    def test_order_confirmed_after_the_round_closed_still_counts(self):
        """Pay 22:01, close the round 22:02, confirm 22:03.

        The old time-windowed summary dropped this sale from BOTH rounds.
        """
        from bravepos.views import shift_orders

        make_shop()
        branch = make_branch()
        shift = open_shift(branch)
        p = make_product(branch, price='100.00')

        with StubGateway() as gw:
            d = selforder.create_draft(branch, [{'product_id': str(p.id), 'qty': 1}])
            selforder.start_payment(d, 'qr', redirect_url='https://x/')

            # Cashier closes the round before the confirmation lands.
            shift.status = 'closed'
            shift.closed_at = djtz.now()
            shift.save()

            gw.paid = True
            order = selforder.promote(SelfOrder.objects.get(pk=d.pk))

        self.assertEqual(order.shift_id, shift.id)
        self.assertIn(order, list(shift_orders(shift)))


# ─── Concurrency: the reason Phase 0 exists ──────────────────────────────────
@REQUIRES_ROW_LOCKS
class ConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        make_shop()
        self.branch = make_branch()
        open_shift(self.branch)
        self.p = make_product(self.branch, price='100.00', stock=100)

    def test_two_pollers_on_one_draft_create_exactly_one_order(self):
        """The customer's phone and the POS poll the same token at once."""
        with StubGateway() as gw:
            d = selforder.create_draft(self.branch, [{'product_id': str(self.p.id), 'qty': 1}])
            selforder.start_payment(d, 'qr', redirect_url='https://x/')
            gw.paid = True

            def poll():
                selforder.promote(SelfOrder.objects.get(pk=d.pk))

            errors = run_concurrently([poll, poll, poll])

        self.assertEqual(errors, [], f'pollers raised: {errors}')
        self.assertEqual(Order.objects.count(), 1)

    def test_concurrent_checkouts_do_not_collide_on_order_number(self):
        """A self-order confirming while a cashier rings up used to 500 on the
        unique order_number — after the customer's money was already taken."""
        def ring_up():
            create_order_from_items(
                branch=self.branch,
                items=[{'product_id': str(self.p.id), 'name': 'Latte',
                        'price': '100.00', 'qty': 1}],
                payment_method='Cash',
                goods_total=Decimal('100.00'),
                paid_amount=Decimal('100.00'),
            )

        errors = run_concurrently([ring_up] * 6)

        self.assertEqual(errors, [], f'concurrent checkout raised: {errors}')
        self.assertEqual(Order.objects.count(), 6)
        self.assertEqual(
            Order.objects.values('order_number').distinct().count(), 6,
            'order numbers must be unique',
        )

    def test_concurrent_orders_both_decrement_stock(self):
        """The old code read stock into memory, dropped its lock, then wrote —
        so one of two concurrent decrements was silently lost."""
        def sell_two():
            create_order_from_items(
                branch=self.branch,
                items=[{'product_id': str(self.p.id), 'name': 'Latte',
                        'price': '100.00', 'qty': 2}],
                payment_method='Cash',
                goods_total=Decimal('200.00'),
                paid_amount=Decimal('200.00'),
            )

        errors = run_concurrently([sell_two] * 5)

        self.assertEqual(errors, [], f'raised: {errors}')
        self.p.refresh_from_db()
        self.assertEqual(self.p.stock, 100 - (5 * 2), 'no decrement may be lost')
