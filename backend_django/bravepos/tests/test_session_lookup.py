"""Session lookup, and the branch list the login screen reads.

``get_session`` used to touch ``last_seen_at`` with
``sess.save(update_fields=...)``.  A logout, or a newer login on the same
branch, can delete the row between the lookup and that save, and then Django
raises ``DatabaseError: Save with update_fields did not affect any rows`` —
PYTHON-DJANGO-1A, 155 events, the tablets' pollers getting a 500 where a 401
was due.

``/api/branches`` ran one cashier query per branch for ``cashier_email``
(PYTHON-DJANGO-1G, an N+1 on every login screen).

Run:
    python manage.py test bravepos.tests.test_session_lookup \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.test import RequestFactory, TestCase
from django.utils import timezone as djtz

from bravepos import audit, views
from bravepos.models import BranchSession, Staff

from .factories import make_branch


class GetSessionTests(TestCase):
    def setUp(self):
        # get_session names the actor for the audit log; called outside a
        # request, nothing clears it, and later tests' writes would carry it.
        self.addCleanup(audit.clear_context)
        self.branch = make_branch()
        self.staff = Staff.objects.create(
            name='Cashier', email='c@test.local', password_hash='x', role='cashier')
        self.staff.branches.add(self.branch)
        self.sess = BranchSession.objects.create(
            token='tok' * 10, branch=self.branch, staff=self.staff)

    def _request(self):
        return RequestFactory().get('/', HTTP_AUTHORIZATION=f'Bearer {self.sess.token}')

    def test_a_live_session_is_returned_and_touched(self):
        stale = djtz.now() - timedelta(hours=1)
        BranchSession.objects.filter(pk=self.sess.pk).update(last_seen_at=stale)
        self.assertEqual(views.get_session(self._request()).pk, self.sess.pk)
        self.sess.refresh_from_db()
        self.assertGreater(self.sess.last_seen_at, stale)

    def test_a_session_deleted_mid_request_is_none_not_a_500(self):
        # The lookup hands back the row as it was; by the time of the write a
        # logout has removed it.
        found = BranchSession.objects.select_related('staff', 'branch').get(pk=self.sess.pk)
        BranchSession.objects.filter(pk=found.pk).delete()
        lookup = mock.Mock()
        lookup.get.return_value = found
        with mock.patch.object(BranchSession.objects, 'select_related', return_value=lookup):
            self.assertIsNone(views.get_session(self._request()))

    def test_the_poller_gets_a_401_for_a_replaced_session(self):
        token = self.sess.token
        self.sess.delete()
        res = self.client.get('/api/self-orders/pending', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(res.status_code, 401)


class BranchListCashierEmailTests(TestCase):
    def setUp(self):
        self.branches = [make_branch(name=f'Branch {i}') for i in range(4)]
        for i, b in enumerate(self.branches):
            for j, age in enumerate((2, 1)):  # the older cashier is the primary
                s = Staff.objects.create(
                    name=f'C{i}{j}', email=f'c{i}{j}@test.local',
                    password_hash='x', role='cashier')
                Staff.objects.filter(pk=s.pk).update(
                    created_at=djtz.now() - timedelta(days=age))
                s.branches.add(b)

    def test_cashier_email_is_the_oldest_cashier(self):
        rows = {r['name']: r['cashier_email'] for r in self.client.get('/api/branches').json()}
        self.assertEqual(rows['Branch 2'], 'c20@test.local')

    def test_query_count_does_not_grow_with_branches(self):
        with self.assertNumQueries(2):
            self.client.get('/api/branches')
        make_branch(name='Branch 99')
        with self.assertNumQueries(2):
            self.client.get('/api/branches')

    def test_a_cashier_rename_shows_in_the_update_response(self):
        admin = Staff.objects.create(
            name='Admin', email='a@test.local', password_hash='x', role='admin')
        b = self.branches[0]
        admin.branches.add(b)
        sess = BranchSession.objects.create(token='adm' * 10, branch=b, staff=admin)
        res = self.client.patch(
            f'/api/branches/{b.id}', {'cashier_email': 'renamed@test.local'},
            content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {sess.token}')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['cashier_email'], 'renamed@test.local')
