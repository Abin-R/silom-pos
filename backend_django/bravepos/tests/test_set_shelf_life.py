"""Loading shelf life from the FEFO sheet onto every branch's products."""
from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from .factories import make_branch, make_product


def run(*args):
    out = StringIO()
    call_command('set_shelf_life', *args, stdout=out, stderr=out)
    return out.getvalue()


class SetShelfLifeTests(TestCase):
    def setUp(self):
        self.silom = make_branch(name='Silom')
        self.bio = make_branch(name='BIO HOUSE')
        self.a = make_product(self.silom, name='Choco gems Pop Original')
        # Sheet says "Mini  Dot Cake" with two spaces; case differs too.
        self.b = make_product(self.bio, name='mini dot cake')
        self.other = make_product(self.bio, name='Latte')
        # Till name differs from the sheet's "Large Cookies Redvelved".
        self.c = make_product(self.silom, name='Red Velvet Cookie')

    def test_dry_run_writes_nothing(self):
        out = run()
        self.a.refresh_from_db()
        self.assertIsNone(self.a.shelf_life)
        self.assertIn('3 product(s) would change', out)

    def test_apply_sets_every_branch_and_leaves_others(self):
        run('--apply')
        for p in (self.a, self.b, self.c, self.other):
            p.refresh_from_db()
        self.assertEqual(self.a.shelf_life, 30)
        self.assertEqual(self.b.shelf_life, 6)
        self.assertEqual(self.c.shelf_life, 6)
        self.assertIsNone(self.other.shelf_life)
        self.assertIn('Updated 0', run('--apply'))
