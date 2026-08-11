"""A full tax invoice must record who it was issued to, and only for real.

The printed slip is the customer's legal document, and it is rendered on the
tablet from whatever this endpoint accepted.  So the endpoint — not the form —
is where the buyer particulars are guaranteed present and well-formed; a slip
that reaches a buyer with a truncated tax ID or no address is not a valid
ใบกำกับภาษีเต็มรูป and cannot be quietly reprinted into one later.
"""
from __future__ import annotations

from decimal import Decimal

from django.test import TestCase

from bravepos.models import BranchSession, Customer, Order, Staff

from .factories import make_branch, make_product, make_shop, open_shift


GOOD = {
    'name': 'Brave Brand Co., Ltd.',
    'tax_id': '0105563083534',
    'tax_branch': 'Head Office',
    'address': '55 Biohouse Building, 5th Floor, Sukhumvit 39, Bangkok 10110',
    'phone': '026625644',
    'email': 'ap@bravebrand.co.th',
}


class TaxInvoiceTests(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch()
        open_shift(self.branch)
        self.product = make_product(self.branch, price='350.00')

        self.staff = Staff.objects.create(
            name='Bonus', email='bonus@test.local', password_hash='x', role='cashier',
        )
        self.staff.branches.add(self.branch)
        self.session = BranchSession.objects.create(
            token='tok' * 12, branch=self.branch, staff=self.staff,
        )

        self.order = Order.objects.create(
            branch=self.branch, order_number='PS001019681',
            subtotal=Decimal('700.00'), total=Decimal('650.00'),
            payment_method='QR', paid_amount=Decimal('650.00'),
            staff='Bonus',
        )

    def _post(self, payload, order=None):
        return self.client.post(
            f'/api/orders/{(order or self.order).id}/tax-invoice',
            data=payload,
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self.session.token}',
        )

    # ── The happy path ──────────────────────────────────────────────────
    def test_buyer_particulars_are_stored_on_the_order(self):
        res = self._post(GOOD)
        self.assertEqual(res.status_code, 200)

        self.order.refresh_from_db()
        data = self.order.pos_tax_invoice
        for field, value in GOOD.items():
            self.assertEqual(data[field], value)

        # Reprints render from the response, so it has to carry the data back.
        self.assertEqual(res.json()['pos_tax_invoice']['tax_id'], GOOD['tax_id'])

    def test_issuer_is_stamped_so_a_reissue_is_attributable(self):
        self._post(GOOD)
        self.order.refresh_from_db()
        self.assertEqual(self.order.pos_tax_invoice['issued_by'], 'Bonus')
        self.assertTrue(self.order.pos_tax_invoice['issued_at'])

    # ── Validation: an invalid invoice must never be persisted ──────────
    def test_required_particulars_are_enforced(self):
        for missing in ('name', 'tax_id', 'address'):
            payload = {**GOOD}
            payload[missing] = ''
            res = self._post(payload)
            self.assertEqual(res.status_code, 400, f'{missing} was accepted blank')
            self.order.refresh_from_db()
            self.assertIsNone(self.order.pos_tax_invoice)

    def test_tax_id_must_be_thirteen_digits(self):
        res = self._post({**GOOD, 'tax_id': '12345'})
        self.assertEqual(res.status_code, 400)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.pos_tax_invoice)

    def test_tax_id_is_stored_as_bare_digits(self):
        # Buyers hand over their tax ID formatted; the printed slip and any
        # later accounting export want one canonical form.
        self._post({**GOOD, 'tax_id': '0-1055-63083-53-4'})
        self.order.refresh_from_db()
        self.assertEqual(self.order.pos_tax_invoice['tax_id'], '0105563083534')

    # ── Customer linkage ────────────────────────────────────────────────
    def test_anonymous_bill_is_attached_to_the_named_buyer(self):
        customer = Customer.objects.create(branch=self.branch, name='Brave Brand')
        self._post({**GOOD, 'customer_id': str(customer.id)})

        self.order.refresh_from_db()
        self.assertEqual(self.order.customer_id, customer.id)
        self.assertEqual(self.order.customer_name, 'Brave Brand')

    def test_a_customer_added_mid_flow_can_be_linked(self):
        # The cashier's real path: no such buyer exists, so they add one from
        # inside the flow and then issue the invoice to it.  The link only lands
        # if the POST stamped the caller's branch on the new customer — if it
        # didn't, the branch-scoped lookup here would silently skip it and the
        # sale would stay anonymous.
        created = self.client.post(
            '/api/customers',
            data={'name': 'Somchai', 'last_name': 'Jaidee'},
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self.session.token}',
        )
        self.assertEqual(created.status_code, 201, created.content)
        customer_id = created.json()['id']

        self._post({**GOOD, 'customer_id': customer_id})

        self.order.refresh_from_db()
        self.assertEqual(str(self.order.customer_id), customer_id)

    def test_an_existing_customer_link_is_never_rewritten(self):
        # Moving a paid sale into a different customer's history to issue an
        # invoice would corrupt both customers' stats.
        original = Customer.objects.create(branch=self.branch, name='Walk-in')
        self.order.customer = original
        self.order.customer_name = 'Walk-in'
        self.order.save(update_fields=['customer', 'customer_name'])

        other = Customer.objects.create(branch=self.branch, name='Someone Else')
        self._post({**GOOD, 'customer_id': str(other.id)})

        self.order.refresh_from_db()
        self.assertEqual(self.order.customer_id, original.id)
        self.assertEqual(self.order.customer_name, 'Walk-in')

    def test_customer_from_another_branch_is_ignored(self):
        outsider = Customer.objects.create(branch=make_branch(name='Bangkhae'), name='Elsewhere')
        res = self._post({**GOOD, 'customer_id': str(outsider.id)})

        # The invoice still issues — the buyer particulars are what matter — but
        # the sale is not linked across a branch boundary.
        self.assertEqual(res.status_code, 200)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.customer_id)

    # ── Isolation from the customer-facing Peak flow ─────────────────────
    def test_issuing_in_app_does_not_arm_the_peak_web_flow(self):
        # ``tax_invoice_process`` treats a non-empty ``tax_invoice_data`` as
        # "the customer submitted the web form" and will enqueue a real Peak
        # document from it.  A cashier printing a slip must never trip that.
        self._post(GOOD)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.tax_invoice_data)

        res = self.client.get(f'/receipt/{self.order.order_number}/tax-invoice/process/')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()['error'], 'Tax invoice form not submitted')

    def test_the_web_form_cannot_wipe_the_printed_invoice(self):
        # ``save_tax_invoice`` overwrites its field wholesale.  If both flows
        # shared one field, a customer using the receipt QR after the cashier
        # printed would erase the buyer address — and the next reprint would
        # emit a tax invoice with a blank address.
        self._post(GOOD)
        self.client.post(
            f'/receipt/{self.order.order_number}/tax-invoice/save/',
            data={'name': 'Someone Web', 'tax_id': '9999999999999'},
        )

        self.order.refresh_from_db()
        self.assertEqual(self.order.pos_tax_invoice['name'], GOOD['name'])
        self.assertEqual(self.order.pos_tax_invoice['address'], GOOD['address'])

    # ── Scoping ─────────────────────────────────────────────────────────
    def test_another_branchs_order_is_not_reachable(self):
        foreign = Order.objects.create(
            branch=make_branch(name='Bangkhae'), order_number='PS002000001',
            total=Decimal('100.00'),
        )
        res = self._post(GOOD, order=foreign)
        self.assertEqual(res.status_code, 404)

    def test_a_session_is_required(self):
        res = self.client.post(
            f'/api/orders/{self.order.id}/tax-invoice',
            data=GOOD, content_type='application/json',
        )
        self.assertIn(res.status_code, (401, 403))


class ReceiptQrLandingTests(TestCase):
    """The QR on every printed slip lands here.  It has to keep offering both
    choices: a customer who only realises they need a tax invoice after leaving
    has no cashier to ask, and this page is their only route in."""

    def setUp(self):
        make_shop()
        self.branch = make_branch()
        self.order = Order.objects.create(
            branch=self.branch, order_number='PS001019681', total=Decimal('650.00'),
        )

    def test_the_scan_lands_on_the_menu_not_a_redirect(self):
        res = self.client.get(f'/receipt/{self.order.order_number}/')
        self.assertEqual(res.status_code, 200)

    def test_both_choices_are_offered(self):
        res = self.client.get(f'/receipt/{self.order.order_number}/')
        body = res.content.decode()
        self.assertIn(f'/receipt/{self.order.order_number}/tax-invoice/', body)
        self.assertIn('formaloo.me', body)

    def test_the_review_link_carries_the_order(self):
        # Without ``oid`` a submitted review can't be tied back to its sale.
        res = self.client.get(f'/receipt/{self.order.order_number}/')
        self.assertIn(f'oid={self.order.order_number}', res.content.decode())

    def test_it_needs_no_login(self):
        # Root-level and unauthenticated on purpose — the scanner is a customer
        # with no session.  A login redirect here would break every printed QR.
        res = self.client.get(f'/receipt/{self.order.order_number}/')
        self.assertNotIn('login', res.get('Location', '').lower())
        self.assertEqual(res.status_code, 200)


class CustomerTaxIdentityTests(TestCase):
    """The buyer's details are also remembered on the Customer, so the next
    invoice for the same company is one tap away."""

    def setUp(self):
        make_shop()
        self.branch = make_branch()
        staff = Staff.objects.create(
            name='Bonus', email='bonus@test.local', password_hash='x', role='cashier',
        )
        staff.branches.add(self.branch)
        self.session = BranchSession.objects.create(
            token='tok' * 12, branch=self.branch, staff=staff,
        )

    def _headers(self):
        return {'HTTP_AUTHORIZATION': f'Bearer {self.session.token}'}

    def test_tax_identity_round_trips_through_the_api(self):
        res = self.client.post(
            '/api/customers',
            data={
                'name': 'Brave Brand Co., Ltd.',
                'tax_id': '0105563083534',
                'tax_branch': 'Head Office',
                'address': '55 Biohouse Building, Bangkok 10110',
                'email': 'ap@bravebrand.co.th',
            },
            content_type='application/json',
            **self._headers(),
        )
        self.assertEqual(res.status_code, 201, res.content)
        body = res.json()
        self.assertEqual(body['tax_id'], '0105563083534')
        self.assertEqual(body['tax_branch'], 'Head Office')

    def test_blank_date_of_birth_does_not_fail_the_save(self):
        # The Add-Customer form sends "" for every untouched optional field;
        # DRF's DateField rejects "" outright, which used to fail the whole POST
        # over a field nobody filled in.
        res = self.client.post(
            '/api/customers',
            data={'name': 'Somchai', 'last_name': 'Jaidee', 'birth_date': ''},
            content_type='application/json',
            **self._headers(),
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertIsNone(Customer.objects.get(name='Somchai').birth_date)
