const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

// The till's discount rules against the cases the self-order website's Python
// copy (backend_django/bravepos/discount_rules.py) is also tested on, so the
// two can't drift apart unnoticed.  Imports the real TypeScript: Node 22
// strips the types.  Run with UPDATE=1 to rewrite the expected answers from the
// TypeScript after deliberately changing a rule — then the Python must follow.
const FIXTURE = path.join(
  __dirname, '..', '..', 'backend_django', 'bravepos', 'tests', 'fixtures', 'discount_cases.json',
);

const clean = (l) => Object.fromEntries(Object.entries(l).filter(([, v]) => v !== undefined));

// The till's per-line dropdown filter (CartItemModal in app/pos.tsx).
function offered(rules, lines, presets, pid) {
  const bill = rules.billBeforeDiscounts(lines);
  return presets.filter((p) => {
    if (!rules.meetsMinimum(p, bill)) return false;
    if (rules.isCombo(p)) {
      return p.conditions.some((r) => r.product_ids.includes(pid))
        && rules.comboSetsWith(lines, p, pid) > 0;
    }
    return p.all_products || p.product_ids.includes(pid);
  }).map((p) => p.id);
}

test('discount rules give the answers both the till and self-order are held to', async () => {
  const rules = await import(path.join(__dirname, '..', 'lib', 'discountRules.ts'));
  const cases = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
  for (const c of cases) {
    const got = rules.reconcileDiscounts(c.cart, c.presets).map(clean);
    const offers = Object.fromEntries(
      got.filter((l) => !l.free_of).map((l) => [l.product_id, offered(rules, got, c.presets, l.product_id)]),
    );
    if (process.env.UPDATE) {
      c.expect = got;
      c.offered = offers;
      continue;
    }
    assert.deepEqual(got, c.expect, c.name);
    assert.deepEqual(offers, c.offered, c.name);
  }
  if (process.env.UPDATE) fs.writeFileSync(FIXTURE, JSON.stringify(cases, null, 1) + '\n');
});
