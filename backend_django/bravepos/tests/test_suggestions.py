"""The cashier upsell strip: what gets mined, and what gets served.

The feature is only worth having if it is right about two things — that a
suggestion reflects what guests actually bought together, and that an admin's
"never offer this" is honoured absolutely.  Everything here pins one of those,
or pins a way the strip must fail quietly rather than loudly.

The mining half deliberately covers the *low-volume* case as thoroughly as the
happy path.  At coffee-shop volume the Apriori tier will often produce nothing
at all, and that has to be a normal Tuesday rather than an error.

Run:
    python manage.py test bravepos.tests.test_suggestions \\
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone as djtz

from bravepos.models import (
    Branch, BranchSession, Order, OrderItem, Staff, SuggestionOverride, SuggestionRule,
)

from .factories import make_branch, make_order, make_product, make_shop


def _mine(**kwargs):
    # Swallow the per-branch report; it is for cron logs, not test output.
    call_command('mine_suggestions', verbosity=0, stdout=StringIO(), **kwargs)


class MiningTests(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch(name='Silom')
        self.latte = make_product(self.branch, name='Latte')
        self.croissant = make_product(self.branch, name='Croissant')
        self.water = make_product(self.branch, name='Water')

    def test_mines_a_pair_rule_in_both_directions(self):
        """The tier that actually carries the feature.

        Both directions matter: the cashier may ring up the pastry first."""
        cookie = make_product(self.branch, name='Cookie')
        # The other baskets must NOT contain Latte.  A product present in every
        # basket has a lift of exactly 1.0 against everything — no better than
        # chance — and the miner is right to reject it.
        for _ in range(20):
            make_order(self.branch, [self.latte, self.croissant])
        for _ in range(10):
            make_order(self.branch, [self.water, cookie])

        _mine()

        # Learned tiers are pooled across the shop, so branch is NULL.
        forward = SuggestionRule.objects.get(
            branch__isnull=True, kind='pair',
            antecedent_key='latte', consequent_name='croissant',
        )
        reverse = SuggestionRule.objects.get(
            branch__isnull=True, kind='pair',
            antecedent_key='croissant', consequent_name='latte',
        )
        self.assertGreater(forward.lift, 1.0)
        self.assertGreater(reverse.lift, 1.0)
        self.assertEqual(forward.basket_count, 20)

    def test_low_volume_branch_still_gets_popularity(self):
        """A quiet branch must not end up with an empty strip.

        This is the expected steady state at a small shop, not a failure."""
        for _ in range(6):
            make_order(self.branch, [self.latte])

        _mine()

        self.assertEqual(
            SuggestionRule.objects.filter(branch__isnull=True, kind='rule').count(), 0,
        )
        self.assertTrue(
            SuggestionRule.objects.filter(branch=self.branch, kind='popular').exists(),
        )

    def test_apriori_tier_fires_above_the_gate(self):
        """The only test that actually exercises ``efficient_apriori``.

        Its unique contribution is the multi-item antecedent — {Latte,
        Croissant} → Cookie is something plain pair counting cannot express."""
        cookie = make_product(self.branch, name='Cookie')
        bun = make_product(self.branch, name='Bun')
        # 240 multi-item baskets clears the gate, and the second group dilutes
        # the marginals so the trio actually has lift rather than being
        # everything the shop sells.
        for _ in range(120):
            make_order(self.branch, [self.latte, self.croissant, cookie])
        for _ in range(120):
            make_order(self.branch, [self.water, bun])

        _mine()

        multi = SuggestionRule.objects.filter(
            branch__isnull=True, kind='rule', antecedent_size=2,
        )
        self.assertTrue(multi.exists(), 'expected at least one 2-item antecedent')

    def test_missing_library_degrades_to_counting(self):
        """A box that missed the pip install loses one tier, not the cron."""
        bun = make_product(self.branch, name='Bun')
        for _ in range(120):
            make_order(self.branch, [self.latte, self.croissant])
        for _ in range(120):
            make_order(self.branch, [self.water, bun])

        real_import = __import__

        def no_apriori(name, *args, **kwargs):
            if name == 'efficient_apriori':
                raise ImportError('not installed')
            return real_import(name, *args, **kwargs)

        with mock.patch('builtins.__import__', side_effect=no_apriori):
            _mine()

        self.assertEqual(
            SuggestionRule.objects.filter(branch__isnull=True, kind='rule').count(), 0,
        )
        self.assertTrue(
            SuggestionRule.objects.filter(branch__isnull=True, kind='pair').exists(),
        )

    def test_cancelled_orders_are_not_evidence(self):
        """A voided bill is not something a guest wanted."""
        for _ in range(30):
            make_order(self.branch, [self.latte, self.croissant], status='cancel')

        _mine()

        self.assertEqual(
            SuggestionRule.objects.filter(branch__isnull=True, kind='pair').count(), 0,
        )

    def test_window_excludes_stale_history(self):
        old = djtz.now() - timedelta(days=400)
        for _ in range(30):
            make_order(self.branch, [self.latte, self.croissant], created_at=old)

        _mine(days=180)

        self.assertEqual(
            SuggestionRule.objects.filter(branch__isnull=True, kind='pair').count(), 0,
        )

    def test_rewrite_is_scoped_to_one_branch(self):
        """Guards against a global truncate — mining B must not wipe A."""
        other = make_branch(name='Biohouse')
        o_latte = make_product(other, name='Latte')
        o_bun = make_product(other, name='Bun')
        for _ in range(20):
            make_order(self.branch, [self.latte, self.croissant])
        for _ in range(20):
            make_order(other, [o_latte, o_bun])

        _mine()
        before = SuggestionRule.objects.filter(branch=self.branch).count()
        _mine(branch=str(other.id))

        self.assertEqual(
            SuggestionRule.objects.filter(branch=self.branch).count(), before,
        )

    def test_branches_never_borrow_each_others_rules(self):
        other = make_branch(name='Biohouse')
        o_latte = make_product(other, name='Latte')
        o_bun = make_product(other, name='Bun')
        for _ in range(20):
            make_order(other, [o_latte, o_bun])

        _mine()

        self.assertEqual(SuggestionRule.objects.filter(branch=self.branch).count(), 0)

    def test_popular_only_leaves_mined_rules_alone(self):
        """The cheap daily pass must not undo Monday's work."""
        for _ in range(20):
            make_order(self.branch, [self.latte, self.croissant])
        _mine()
        pairs = SuggestionRule.objects.filter(branch__isnull=True, kind='pair').count()

        _mine(popular_only=True)

        self.assertEqual(
            SuggestionRule.objects.filter(branch__isnull=True, kind='pair').count(), pairs,
        )

    def test_dry_run_writes_nothing(self):
        for _ in range(20):
            make_order(self.branch, [self.latte, self.croissant])

        _mine(dry_run=True)

        self.assertEqual(SuggestionRule.objects.filter(branch=self.branch).count(), 0)


class RolloutTests(TestCase):
    """The strip ships dark and is turned on one branch at a time."""

    def test_a_new_branch_has_suggestions_switched_off(self):
        """The whole rollout plan rests on this default.

        Every other branch is a real shop serving real customers; merging this
        must not put suggestion chips in front of any of them.  A default of
        True would do exactly that on the first deploy, silently, with nobody
        having asked for it.  Deliberately asserted on a raw Branch rather than
        make_branch(), which opts the rest of the suite in."""
        branch = Branch.objects.create(name='Somewhere New')
        self.assertFalse(branch.suggestions_enabled)

    def test_a_branch_with_the_flag_off_serves_nothing(self):
        """Not even the popularity tier, which needs no mined rules at all and
        would otherwise leak the feature onto a branch that never enabled it."""
        branch = make_branch(name='Dark', suggestions_enabled=False)
        product = make_product(branch, name='Latte')
        product.is_favorite = True
        product.save()
        staff = Staff.objects.create(
            name='Bee', email='dark@test.local', password_hash='x', role='cashier',
        )
        staff.branches.add(branch)
        session = BranchSession.objects.create(
            token='d' * 36, branch=branch, staff=staff,
        )
        res = self.client.post(
            '/api/suggestions', {'product_ids': [str(product.id)]},
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {session.token}',
        )
        self.assertEqual(res.json(), {'suggestions': [], 'source': 'off'})


class EndpointTests(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch(name='Silom')
        self.latte = make_product(self.branch, name='Latte')
        self.croissant = make_product(self.branch, name='Croissant')
        self.cookie = make_product(self.branch, name='Cookie')
        self.staff = Staff.objects.create(
            name='Bee', email='bee@test.local', password_hash='x', role='cashier',
        )
        self.staff.branches.add(self.branch)
        self.session = BranchSession.objects.create(
            token='s' * 36, branch=self.branch, staff=self.staff,
        )
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {self.session.token}'}

    def _rule(self, antecedent, consequent, kind='pair', score=5.0, branch=...):
        """A mined rule, keyed by product name.

        Learned tiers live on ``branch=None`` (shop-wide); ``popular`` is
        per-branch.  Pass ``branch=`` explicitly to place a row deliberately.
        """
        if branch is ...:
            branch = self.branch if kind == 'popular' else None
        return SuggestionRule.objects.create(
            branch=branch, kind=kind,
            antecedent_key=antecedent.name.strip().casefold() if antecedent else '',
            antecedent_size=1 if antecedent else 0,
            consequent_name=consequent.name.strip().casefold(),
            lift=score, score=score, basket_count=20,
        )

    def _post(self, product_ids, **body):
        return self.client.post(
            '/api/suggestions',
            {'product_ids': [str(p) for p in product_ids], **body},
            content_type='application/json', **self.auth,
        )

    def test_requires_a_session(self):
        res = self.client.post(
            '/api/suggestions', {'product_ids': []}, content_type='application/json',
        )
        self.assertEqual(res.status_code, 401)

    def test_suggests_from_rules_and_names_the_trigger(self):
        self._rule(self.latte, self.croissant)
        res = self._post([self.latte.id])
        body = res.json()

        self.assertEqual(res.status_code, 200)
        self.assertEqual([s['name'] for s in body['suggestions']], ['Croissant'])
        self.assertEqual(body['source'], 'pair')
        self.assertEqual(body['suggestions'][0]['because_id'], str(self.latte.id))
        self.assertEqual(body['suggestions'][0]['reason'], 'often_together')

    def test_never_suggests_what_is_already_in_the_cart(self):
        self._rule(self.latte, self.croissant)
        res = self._post([self.latte.id, self.croissant.id])
        self.assertEqual(res.json()['suggestions'], [])

    def test_a_real_rule_outranks_mere_popularity(self):
        """``score`` is not comparable across tiers, so the endpoint walks them
        in order.  A popular muffin must not displace a genuine pairing."""
        self._rule(None, self.cookie, kind='popular', score=99.0)
        self._rule(self.latte, self.croissant, kind='pair', score=1.5)

        names = [s['name'] for s in self._post([self.latte.id]).json()['suggestions']]
        self.assertEqual(names[0], 'Croissant')

    def test_pin_comes_first(self):
        self._rule(self.latte, self.croissant)
        SuggestionOverride.objects.create(
            branch=self.branch, mode='pin', trigger=self.latte, product=self.cookie,
        )
        body = self._post([self.latte.id]).json()

        self.assertEqual(body['suggestions'][0]['name'], 'Cookie')
        self.assertEqual(body['suggestions'][0]['reason'], 'pinned')
        self.assertEqual(body['source'], 'pinned')

    def test_block_beats_both_a_pin_and_a_mined_rule(self):
        """Two contradictory admin instructions; the safer one wins."""
        self._rule(self.latte, self.croissant, score=99.0)
        SuggestionOverride.objects.create(
            branch=self.branch, mode='pin', trigger=self.latte, product=self.croissant,
        )
        SuggestionOverride.objects.create(
            branch=self.branch, mode='block', product=self.croissant,
        )
        names = [s['name'] for s in self._post([self.latte.id]).json()['suggestions']]
        self.assertNotIn('Croissant', names)

    def test_an_inactive_product_is_never_suggested(self):
        self._rule(self.latte, self.croissant)
        self.croissant.active = False
        self.croissant.save()
        self.assertEqual(self._post([self.latte.id]).json()['suggestions'], [])

    def test_cold_start_falls_back_to_favourites(self):
        """A branch that opened this morning has no history at all."""
        self.cookie.is_favorite = True
        self.cookie.save()

        body = self._post([self.latte.id]).json()

        self.assertEqual([s['name'] for s in body['suggestions']], ['Cookie'])
        self.assertEqual(body['source'], 'none')

    def test_the_kill_switch_is_answered_honestly(self):
        """`off` rather than an empty list, so the admin screen can tell
        "switched off" from "nothing mined yet"."""
        self._rule(self.latte, self.croissant)
        self.branch.suggestions_enabled = False
        self.branch.save()

        body = self._post([self.latte.id]).json()
        self.assertEqual(body, {'suggestions': [], 'source': 'off'})

    def test_a_pairing_learned_anywhere_is_served_here(self):
        """The whole reason pooling exists.

        Only one branch rings up enough multi-item sales to learn anything; if
        rules stayed branch-scoped the other eleven could never show a real
        pairing, only bestsellers."""
        self._rule(self.latte, self.croissant)    # branch=None, shop-wide
        names = [s['name'] for s in self._post([self.latte.id]).json()['suggestions']]
        self.assertIn('Croissant', names)

    def test_a_pooled_rule_cannot_suggest_what_this_branch_does_not_stock(self):
        """The safeguard that makes pooling safe.

        A rule learned where "Mooncake" sells must not offer it at a branch
        that has never carried one — the cashier would be pushing something
        they cannot ring up."""
        elsewhere = make_branch(name='Biohouse')
        exotic = make_product(elsewhere, name='Mooncake')
        SuggestionRule.objects.create(
            branch=None, kind='pair',
            antecedent_key='latte', antecedent_size=1,
            consequent_name=exotic.name.strip().casefold(), lift=9, score=9,
        )
        names = [s['name'] for s in self._post([self.latte.id]).json()['suggestions']]
        self.assertNotIn('Mooncake', names)

    def test_another_branchs_popularity_does_not_leak(self):
        """Popularity stays local — "what sells best here" means nothing pooled."""
        other = make_branch(name='Elsewhere')
        o_bun = make_product(other, name='Bun')
        SuggestionRule.objects.create(
            branch=other, kind='popular', antecedent_key='', antecedent_size=0,
            consequent_name=o_bun.name.strip().casefold(), lift=9, score=9,
        )
        names = [s['name'] for s in self._post([self.latte.id]).json()['suggestions']]
        self.assertNotIn('Bun', names)

    def test_a_junk_payload_degrades_quietly(self):
        """This runs on every cart change; a malformed id must not 500 behind
        a sale."""
        for payload in ({'product_ids': 'nonsense'},
                        {'product_ids': ['not-a-uuid']},
                        {}):
            res = self.client.post(
                '/api/suggestions', payload,
                content_type='application/json', **self.auth,
            )
            self.assertEqual(res.status_code, 200, payload)

    def test_limit_is_clamped(self):
        for i in range(10):
            p = make_product(self.branch, name=f'Extra {i}')
            self._rule(self.latte, p, score=1.0 + i)
        got = self._post([self.latte.id], limit=99).json()['suggestions']
        self.assertLessEqual(len(got), 6)

    def test_query_budget(self):
        """Four queries plus the session lookup: name the cart, read the rules,
        read the overrides, resolve the names to this branch's products.

        Two more than the branch-scoped design cost — that is the price of
        pooling, and it is a fixed price, not per-suggestion.  This test is
        what catches a future N+1 creeping into the resolve step."""
        self._rule(self.latte, self.croissant)
        with self.assertNumQueries(6):
            self._post([self.latte.id])

    def test_status_is_admin_only(self):
        res = self.client.get('/api/suggestions/status', **self.auth)
        self.assertEqual(res.status_code, 403)

        self.staff.role = 'admin'
        self.staff.save()
        res = self.client.get('/api/suggestions/status', **self.auth)
        self.assertEqual(res.status_code, 200)
        self.assertIn('pairs', res.json())


class AttributionTests(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch(name='Silom')
        self.latte = make_product(self.branch, name='Latte')
        self.staff = Staff.objects.create(
            name='Bee', email='bee2@test.local', password_hash='x', role='cashier',
        )
        self.staff.branches.add(self.branch)
        self.session = BranchSession.objects.create(
            token='a' * 36, branch=self.branch, staff=self.staff,
        )
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {self.session.token}'}

    def _order(self, item):
        return self.client.post(
            '/api/orders',
            {'items': [item], 'subtotal': 100, 'total': 100,
             'payment_method': 'cash', 'paid_amount': 100},
            content_type='application/json', **self.auth,
        )

    def test_the_flag_round_trips(self):
        res = self._order({
            'product_id': str(self.latte.id), 'name': 'Latte',
            'price': 100, 'qty': 1, 'suggested': True,
        })
        self.assertEqual(res.status_code, 201)
        self.assertTrue(OrderItem.objects.get(order__id=res.json()['id']).suggested)
        self.assertTrue(res.json()['items'][0]['suggested'])

    def test_an_older_client_omitting_the_key_is_not_an_error(self):
        """A tablet on last week's JS bundle never sends it."""
        res = self._order({
            'product_id': str(self.latte.id), 'name': 'Latte', 'price': 100, 'qty': 1,
        })
        self.assertEqual(res.status_code, 201)
        self.assertFalse(OrderItem.objects.get(order__id=res.json()['id']).suggested)
