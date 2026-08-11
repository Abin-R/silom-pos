"""Transactions asks for one day, not the whole history.

The screen used to pull every bill the branch had ever rung up on each visit
and filter client-side, so a busy branch sat on a spinner.  The listing now
takes a ``from``/``to`` window computed from the till's local day, and these
tests pin the boundary semantics the client relies on: ``from`` inclusive,
``to`` exclusive, so "today" and "yesterday" can't both claim midnight.
"""
from __future__ import annotations

from datetime import timedelta, timezone as stdtz

from django.test import TestCase
from django.utils import timezone as djtz

from bravepos.models import BranchSession, Order, Staff

from .factories import make_branch, make_shop


class OrdersListWindowTests(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch()
        self.staff = Staff.objects.create(
            name='Bonus', email='bonus@test.local', password_hash='x', role='cashier',
        )
        self.staff.branches.add(self.branch)
        self.session = BranchSession.objects.create(
            token='tok' * 12, branch=self.branch, staff=self.staff,
        )
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {self.session.token}'}

        now = djtz.localtime()
        self.midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # created_at is auto_now_add, so backdate with an explicit update.
        self.today = self._order('T-1', self.midnight + timedelta(hours=9))
        self.at_midnight = self._order('T-2', self.midnight)
        self.yesterday = self._order('Y-1', self.midnight - timedelta(hours=3))
        self.last_week = self._order('W-1', self.midnight - timedelta(days=9))

    def _order(self, number, created_at):
        o = Order.objects.create(branch=self.branch, order_number=number, total='100.00')
        Order.objects.filter(id=o.id).update(created_at=created_at)
        return o

    def _numbers(self, **params):
        res = self.client.get('/api/orders', params, **self.auth)
        self.assertEqual(res.status_code, 200)
        return {row['order_number'] for row in res.json()}

    def test_no_window_returns_everything(self):
        self.assertEqual(self._numbers(), {'T-1', 'T-2', 'Y-1', 'W-1'})

    def test_from_is_inclusive_of_midnight(self):
        got = self._numbers(**{'from': self.midnight.isoformat()})
        self.assertEqual(got, {'T-1', 'T-2'})

    def test_to_is_exclusive_so_yesterday_stops_at_midnight(self):
        got = self._numbers(**{
            'from': (self.midnight - timedelta(days=1)).isoformat(),
            'to': self.midnight.isoformat(),
        })
        self.assertEqual(got, {'Y-1'})

    def test_last_seven_days_excludes_older(self):
        got = self._numbers(**{'from': (self.midnight - timedelta(days=6)).isoformat()})
        self.assertEqual(got, {'T-1', 'T-2', 'Y-1'})

    def test_utc_bound_matches_the_same_local_day(self):
        # The till actually sends UTC ("...Z"); it must select the same rows as
        # the local-offset form above, or "Today" shifts by 7 hours in Bangkok.
        got = self._numbers(**{
            'from': self.midnight.astimezone(stdtz.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
        })
        self.assertEqual(got, {'T-1', 'T-2'})

    def test_offset_that_lost_its_plus_sign_still_narrows(self):
        # An unencoded "+07:00" reaches Django as a space; treating that as
        # unparseable would silently return the whole history.
        got = self._numbers(**{'from': self.midnight.isoformat().replace('+', ' ')})
        self.assertEqual(got, {'T-1', 'T-2'})

    def test_unparseable_bound_falls_back_to_no_window(self):
        # A bad bound must not 500 the till — it just doesn't narrow anything.
        self.assertEqual(
            self._numbers(**{'from': 'not-a-date'}), {'T-1', 'T-2', 'Y-1', 'W-1'},
        )

    def test_limit_is_honoured_and_capped(self):
        self.assertEqual(len(self._numbers(limit='2')), 2)
        self.assertEqual(len(self._numbers(limit='99999')), 4)
