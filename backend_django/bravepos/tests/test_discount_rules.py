"""The Python discount rules must give the till's answers, to the satang.

``fixtures/discount_cases.json`` holds carts with what the till's TypeScript
(``frontend/lib/discountRules.ts``) makes of them; the same file is checked
against the TypeScript by ``frontend/__tests__/discount-rules.test.js``.  To add
a case, add it to the file and run that test with ``UPDATE=1`` to fill in its
expected answer from the TypeScript.
"""
from __future__ import annotations

import json
from pathlib import Path

from django.test import SimpleTestCase

from bravepos import discount_rules

CASES = json.loads(
    (Path(__file__).parent / 'fixtures' / 'discount_cases.json').read_text(encoding='utf-8'))


def _clean(line: dict) -> dict:
    # ``undefined`` keys vanish in the fixture's JSON; ``None`` is the same here.
    return {k: v for k, v in line.items() if v is not None}


class MatchesTheTillTests(SimpleTestCase):
    def test_reconcile(self):
        for case in CASES:
            with self.subTest(case['name']):
                got = [_clean(l) for l in discount_rules.reconcile(case['cart'], case['presets'])]
                self.assertEqual(got, case['expect'])

    def test_offered(self):
        for case in CASES:
            with self.subTest(case['name']):
                for pid, ids in case['offered'].items():
                    got = [p['id'] for p in
                           discount_rules.offered(case['expect'], case['presets'], pid)]
                    self.assertEqual(got, ids, pid)

    def test_rounding_is_math_round_not_bankers(self):
        self.assertEqual(discount_rules.js_round(2.5), 3)
        self.assertEqual(discount_rules.js_round(-2.5), -2)
        self.assertEqual(discount_rules.round2(0.125), 0.13)
