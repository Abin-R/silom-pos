"""Google Analytics on the customer self-order pages.

The tag goes out only with a measurement ID *and* DEBUG off, the same gate as
the browser Sentry, so a dev machine or a test run never feeds the funnel.
The pages call soTrack() unconditionally, so it must always be defined.
"""
from __future__ import annotations

from django.test import Client, TestCase, override_settings

from .factories import make_branch, make_product, make_shop, open_shift

GA_ID = 'G-TEST12345'
GTAG = 'googletagmanager.com/gtag/js'


class SelfOrderAnalyticsTests(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch(name='Silom </script><b>')
        open_shift(self.branch)
        self.p = make_product(self.branch, price='100.00')
        self.c = Client()

    def _menu(self):
        res = self.c.get(f'/order/{self.branch.id}/')
        self.assertEqual(res.status_code, 200)
        return res

    def _pay_page(self):
        start = self.c.post(
            f'/order/{self.branch.id}/start/',
            data={'items': [{'product_id': str(self.p.id), 'qty': 1}]},
            content_type='application/json',
        )
        self.assertEqual(start.status_code, 200)
        res = self.c.get(start.json()['pay_url'])
        self.assertEqual(res.status_code, 200)
        return res

    def test_no_id_means_no_tag_but_soTrack_exists(self):
        res = self._menu()
        self.assertNotContains(res, GTAG)
        self.assertContains(res, 'window.soTrack = function () {}')

    @override_settings(GA_SELFORDER_ID=GA_ID, DEBUG=True)
    def test_debug_on_means_no_tag(self):
        self.assertNotContains(self._menu(), GTAG)

    @override_settings(GA_SELFORDER_ID=GA_ID, DEBUG=False)
    def test_tag_on_menu_and_pay_page(self):
        for res in (self._menu(), self._pay_page()):
            self.assertContains(res, f'{GTAG}?id={GA_ID}')
            self.assertContains(res, 'allow_google_signals: false')
            self.assertContains(res, f'branch_id: "{self.branch.id}"')

    @override_settings(GA_SELFORDER_ID=GA_ID, DEBUG=False)
    def test_branch_name_is_escaped(self):
        """The branch name goes in through json_script, never raw into JS."""
        res = self._menu()
        self.assertNotContains(res, 'Silom </script><b>"')
        self.assertContains(res, 'id="selforder-branch-name"')

    @override_settings(GA_SELFORDER_ID=GA_ID, DEBUG=False)
    def test_pay_page_masks_token(self):
        # No Sentry DSN under settings_test, so this is GA's mask alone.
        res = self._pay_page()
        self.assertContains(res, "'$1[token]'", count=1)
        self.assertContains(res, 'page_location: mask(')
