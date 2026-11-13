"""A till Beam QR that was paid but never became a bill gets reported.

The till saves a Beam QR sale only when its own poll sees "paid".  Every QR it
creates is recorded, and ``check_till_beam_charges`` reports any that Beam says
was paid but no bill carries (PS000011903: the QR was cancelled 0.8 s before
the money landed and the sale rung up again as Custom · QR).
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone as djtz

from bravepos import gateways, seatalk
from bravepos.models import BranchSession, Order, Staff, TillBeamCharge

from .factories import StubGateway, make_branch, make_shop


def _age(row, **delta):
    TillBeamCharge.objects.filter(id=row.id).update(created_at=djtz.now() - timedelta(**delta))
    row.refresh_from_db()
    return row


class RecordTillChargeTests(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch()
        staff = Staff.objects.create(
            name='Nok', email='nok@test.local', password_hash='x', role='cashier',
        )
        staff.branches.add(self.branch)
        session = BranchSession.objects.create(token='tok' * 12, branch=self.branch, staff=staff)
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {session.token}'}

    def _create(self):
        return self.client.post(
            '/api/beam/charge',
            {'amount': 680, 'reference_id': 'POS-1793365565400', 'description': 'Order POS-1'},
            content_type='application/json', **self.auth,
        )

    def test_a_till_qr_is_recorded(self):
        with StubGateway():
            res = self._create()
        self.assertEqual(res.status_code, 200, res.content)
        row = TillBeamCharge.objects.get()
        self.assertEqual(row.charge_id, res.json()['charge_id'])
        self.assertEqual(row.branch, self.branch)
        self.assertEqual(row.amount, Decimal('680'))
        self.assertEqual(row.reference_id, 'POS-1793365565400')
        self.assertEqual(row.staff, 'Nok')
        self.assertEqual(row.items, [])     # this till sent no basket
        self.assertFalse(row.done)

    def test_the_basket_is_recorded_with_the_qr(self):
        with StubGateway():
            res = self.client.post(
                '/api/beam/charge',
                {'amount': 1000, 'reference_id': 'POS-1', 'items': [
                    {'product': 'Original Edition - Choco Gems Pop', 'quantity': 2},
                    {'product': 'Cookie Popcorn', 'quantity': '1'},
                ]},
                content_type='application/json', **self.auth,
            )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(TillBeamCharge.objects.get().items, [
            {'product': 'Original Edition - Choco Gems Pop', 'quantity': 2},
            {'product': 'Cookie Popcorn', 'quantity': 1},
        ])

    def _beam_description(self, payload):
        with StubGateway():
            spy = mock.patch.object(gateways, 'beam_create_promptpay_charge',
                                    wraps=gateways.beam_create_promptpay_charge)
            with spy as create:
                res = self.client.post('/api/beam/charge', payload,
                                       content_type='application/json', **self.auth)
        self.assertEqual(res.status_code, 200, res.content)
        return create.call_args.kwargs['description']

    def test_beam_description_lists_the_products(self):
        self.assertEqual(self._beam_description({
            'amount': 1000, 'reference_id': 'POS-3', 'description': 'Order POS-3', 'items': [
                {'product': 'Original Edition - Choco Gems Pop', 'quantity': 2},
                {'product': 'Cookie Popcorn', 'quantity': 1},
            ]}), '2x Original Edition - Choco Gems Pop, 1x Cookie Popcorn')

    def test_a_long_basket_is_cut_short_for_beam(self):
        desc = self._beam_description({'amount': 1, 'reference_id': 'POS-4', 'items': [
            {'product': f'Product number {n}', 'quantity': 1} for n in range(40)]})
        self.assertLessEqual(len(desc), 255)
        self.assertTrue(desc.endswith('…'))
        self.assertTrue(desc.startswith('1x Product number 0, '))

    def test_no_basket_keeps_the_tills_description(self):
        self.assertEqual(self._beam_description(
            {'amount': 680, 'reference_id': 'POS-5', 'description': 'Order POS-5'}), 'Order POS-5')

    def test_a_malformed_basket_still_records_the_qr(self):
        with StubGateway():
            res = self.client.post(
                '/api/beam/charge',
                {'amount': 350, 'reference_id': 'POS-2', 'items': [
                    'junk', {'product': '', 'quantity': 1}, {'product': 'X', 'quantity': 'a'},
                    {'product': 'Y', 'quantity': 0}, {'product': 'Mama OG Cookie', 'quantity': 1},
                ]},
                content_type='application/json', **self.auth,
            )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(TillBeamCharge.objects.get().items,
                         [{'product': 'Mama OG Cookie', 'quantity': 1}])

    def test_a_failed_charge_records_nothing(self):
        with mock.patch.object(gateways, 'beam_create_promptpay_charge',
                               side_effect=gateways.GatewayError('down', 502)):
            res = self._create()
        self.assertEqual(res.status_code, 502)
        self.assertFalse(TillBeamCharge.objects.exists())

    def test_recording_failure_never_fails_the_charge(self):
        with StubGateway(), mock.patch.object(
                TillBeamCharge.objects, 'create', side_effect=RuntimeError('db')):
            res = self._create()
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.json()['qr_image'])

    def test_a_database_error_leaves_the_request_usable(self):
        # A real IntegrityError, not a mock: it must not poison the request's
        # transaction on its way out.
        TillBeamCharge.objects.create(branch=self.branch, charge_id='chg_1')
        with StubGateway():
            res = self._create()
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(TillBeamCharge.objects.count(), 1)


class CheckTillBeamChargesTests(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch(name='Central Pinklao')
        self.branch.beam_sandbox = False
        self.branch.save()
        self.sent = []
        self.send_ok = True

        def fake_send(group_id, text):
            self.sent.append(text)
            return self.send_ok

        p = mock.patch.object(seatalk, 'send_group_text', side_effect=fake_send)
        p.start()
        self.addCleanup(p.stop)

    def _row(self, minutes=5, amount='680.00', **kw):
        row = TillBeamCharge.objects.create(
            branch=self.branch, charge_id=kw.pop('charge_id', 'ch_680'),
            amount=Decimal(amount), staff='Cashier', **kw,
        )
        return _age(row, minutes=minutes)

    def _beam_says(self, status):
        return mock.patch.object(
            gateways, 'beam_get_charge',
            return_value={'charge_id': 'x', 'status': status, 'amount': Decimal('680'),
                          'currency': 'THB', 'raw': {}},
        )

    def _run(self, *args):
        out = StringIO()
        call_command('check_till_beam_charges', *args, stdout=out, stderr=StringIO())
        return out.getvalue()

    def _paid_twice(self, row):
        """First sweep sees it paid; the next one, after the grace, reports it."""
        with self._beam_says('SUCCEEDED'):
            self._run()
        TillBeamCharge.objects.filter(id=row.id).update(
            paid_seen_at=djtz.now() - timedelta(minutes=3))
        with self._beam_says('SUCCEEDED'):
            out = self._run()
        row.refresh_from_db()
        return out

    def test_still_pending_stays_open_and_quiet(self):
        row = self._row()
        with self._beam_says('PENDING'):
            self._run()
        row.refresh_from_db()
        self.assertFalse(row.done)
        self.assertEqual(self.sent, [])

    def test_failed_charge_is_closed_without_alert(self):
        row = self._row()
        with self._beam_says('EXPIRED'):
            self._run()
        row.refresh_from_db()
        self.assertTrue(row.done)
        self.assertEqual(row.status, 'EXPIRED')
        self.assertEqual(self.sent, [])

    def test_paid_charge_with_its_bill_is_closed_without_alert(self):
        row = self._row()
        Order.objects.create(branch=self.branch, order_number='PS1', total='680.00',
                             payment_method='Beam QR', beam_charge_id='ch_680')
        with self._beam_says('SUCCEEDED'):
            self._run()
        row.refresh_from_db()
        self.assertTrue(row.done)
        self.assertIsNone(row.alerted_at)
        self.assertEqual(self.sent, [])

    def test_paid_charge_gets_grace_before_alert(self):
        row = self._row()
        with self._beam_says('SUCCEEDED'):
            self._run()
        row.refresh_from_db()
        self.assertIsNotNone(row.paid_seen_at)
        self.assertFalse(row.done)
        self.assertEqual(self.sent, [])

    def test_bill_saved_during_grace_means_no_alert(self):
        row = self._row()
        with self._beam_says('SUCCEEDED'):
            self._run()
        Order.objects.create(branch=self.branch, order_number='PS1', total='680.00',
                             payment_method='Beam QR', beam_charge_id='ch_680')
        TillBeamCharge.objects.filter(id=row.id).update(
            paid_seen_at=djtz.now() - timedelta(minutes=3))
        with mock.patch.object(gateways, 'beam_get_charge') as beam:
            self._run()
        beam.assert_not_called()   # already known paid; no second Beam call
        row.refresh_from_db()
        self.assertTrue(row.done)
        self.assertEqual(self.sent, [])

    def test_unbilled_paid_charge_alerts_once_with_candidate_bill(self):
        row = self._row()
        custom = Order.objects.create(branch=self.branch, order_number='PS000011903',
                                      total='680.00', payment_method='Custom · QR')
        Order.objects.create(branch=self.branch, order_number='PS_OTHER',
                             total='120.00', payment_method='Cash')
        out = self._paid_twice(row)
        self.assertIn('UNBILLED ch_680', out)
        self.assertTrue(row.done)
        self.assertIsNotNone(row.alerted_at)
        self.assertEqual(len(self.sent), 1)
        text = self.sent[0]
        self.assertIn('Central Pinklao', text)
        self.assertIn('฿680.00', text)
        self.assertIn('ch_680', text)
        self.assertIn(custom.order_number, text)
        self.assertNotIn('PS_OTHER', text)
        with self._beam_says('SUCCEEDED'):
            self._run()
        self.assertEqual(len(self.sent), 1)   # never twice

    def test_alert_with_no_candidate_says_the_sale_may_be_missing(self):
        row = self._row()
        self._paid_twice(row)
        self.assertIn('may be missing', self.sent[0])

    def test_undelivered_alert_is_retried(self):
        row = self._row()
        self.send_ok = False
        self._paid_twice(row)
        self.assertFalse(row.done)
        self.send_ok = True
        with self._beam_says('SUCCEEDED'):
            self._run()
        row.refresh_from_db()
        self.assertTrue(row.done)
        self.assertEqual(len(self.sent), 2)

    def test_sandbox_branch_is_closed_without_alert(self):
        self.branch.beam_sandbox = True
        self.branch.save()
        row = self._row()
        self._paid_twice(row)
        self.assertTrue(row.done)
        self.assertEqual(self.sent, [])

    def test_fresh_and_old_charges_are_left_alone(self):
        self._row(minutes=1, charge_id='ch_fresh')
        self._row(minutes=60 * 25, charge_id='ch_old')
        with mock.patch.object(gateways, 'beam_get_charge') as beam:
            self._run()
        beam.assert_not_called()

    def test_gateway_error_keeps_the_row_for_next_run(self):
        row = self._row()
        with mock.patch.object(gateways, 'beam_get_charge',
                               side_effect=gateways.GatewayError('timeout', 502)):
            self._run()
        row.refresh_from_db()
        self.assertFalse(row.done)
        self.assertEqual(row.status, 'PENDING')

    def test_dry_run_changes_nothing(self):
        row = self._row()
        with self._beam_says('EXPIRED'):
            self._run('--dry-run')
        row.refresh_from_db()
        self.assertFalse(row.done)
        self.assertEqual(row.status, 'PENDING')
