"""The daily report's payment columns must match what the POS actually stores.

`_PAYMENT_BUCKETS` is keyed on the raw ``Order.payment_method`` string, and it
drifted away from the till: the keys said 'beam' / 'credit' while the POS wrote
'Beam QR' / 'Beam Card' / 'Credit Card'.  Nothing matched, so every QR and card
sale — every non-cash method the payment modal exposes — was silently reported
under Custom Pay.  These cases pin the mapping to the real strings.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from backoffice.views import _PAYMENT_COLUMNS, _payment_bucket
from bravepos.gateways import (
    BEAM_CARD_METHOD, BEAM_QR_METHOD, CARD_METHOD_PREFIX,
)


class PaymentBucketTests(SimpleTestCase):
    def test_qr_sales_do_not_fall_into_custom_pay(self):
        """The regression: a Beam QR sale used to land in Custom Pay."""
        self.assertEqual(_payment_bucket(BEAM_QR_METHOD), 'beam')

    def test_card_sales_report_as_credit(self):
        self.assertEqual(_payment_bucket(CARD_METHOD_PREFIX), 'credit')
        self.assertEqual(_payment_bucket(BEAM_CARD_METHOD), 'credit')

    def test_detail_suffix_is_ignored(self):
        self.assertEqual(
            _payment_bucket('Credit Card · VISA ····1234'), 'credit',
        )
        self.assertEqual(_payment_bucket('Custom · คนละครึ่ง'), 'custom')

    def test_remaining_methods_keep_their_own_column(self):
        for stored, expected in [
            ('Cash', 'cash'),
            ('PromptPay', 'promptpay'),
            ('QR Kbank', 'kbank'),
            ('Easy Pay', 'easypay'),
            ('EDC', 'edc'),
        ]:
            with self.subTest(stored=stored):
                self.assertEqual(_payment_bucket(stored), expected)

    def test_unknown_and_empty_fall_back_to_custom(self):
        self.assertEqual(_payment_bucket('Something Else'), 'custom')
        self.assertEqual(_payment_bucket(''), 'custom')
        self.assertEqual(_payment_bucket(None), 'custom')

    def test_every_bucket_has_a_column(self):
        """A bucket with no column would KeyError while writing the CSV."""
        columns = {key for key, _label in _PAYMENT_COLUMNS}
        from backoffice.views import _PAYMENT_BUCKETS
        self.assertEqual(set(_PAYMENT_BUCKETS.values()) - columns, set())
