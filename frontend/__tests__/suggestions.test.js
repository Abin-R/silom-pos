const test = require('node:test');
const assert = require('node:assert/strict');

// Mirrors of cartKey / hydrate from lib/useSuggestions.ts.  Mirrored rather
// than imported because there is no TS transform in this test setup — the same
// compromise __tests__/payment-methods.test.js already makes.  Source of truth
// is lib/useSuggestions.ts; keep the two in step.
function cartKey(productIds) {
  return [...new Set(productIds)].sort().join('|');
}

function pickReason(s) {
  return { reason: s.reason, because_id: s.because_id };
}

function hydrate(suggestions, products) {
  const byId = new Map(products.map((p) => [p.id, p]));
  return suggestions.map((s) => {
    const local = byId.get(s.id);
    const trigger = s.because_id ? byId.get(s.because_id) : undefined;
    const named = {
      ...pickReason(s),
      because_name: s.because_name || (trigger && trigger.name) || '',
    };
    return local ? { ...s, ...local, ...named } : { ...s, ...named };
  });
}

test('cartKey ignores order, so re-adding items in a different sequence does not refetch', () => {
  assert.equal(cartKey(['b', 'a']), cartKey(['a', 'b']));
});

test('cartKey collapses duplicates — a qty bump must not look like a new cart', () => {
  assert.equal(cartKey(['a', 'a', 'b']), 'a|b');
});

test('cartKey of an empty cart is empty, which is what suppresses the fetch', () => {
  assert.equal(cartKey([]), '');
});

test('hydrate prefers the locally-loaded product so the chip gets its image', () => {
  const [got] = hydrate(
    [{ id: 'p1', name: 'Croissant', price: 75, image_url: '', reason: 'often_together' }],
    [{ id: 'p1', name: 'Croissant', price: 75, image_base64: 'data:image/png;base64,AAA' }],
  );
  assert.equal(got.image_base64, 'data:image/png;base64,AAA');
});

test('hydrate never lets a local product row erase why the chip is there', () => {
  const [got] = hydrate(
    [{
      id: 'p1', name: 'Croissant', reason: 'often_together',
      because_id: 'p9', because_name: 'Latte',
    }],
    // A catalogue row carries no reason fields; spreading it must not blank them.
    [{ id: 'p1', name: 'Croissant', price: 75 }],
  );
  assert.equal(got.reason, 'often_together');
  assert.equal(got.because_name, 'Latte');
});

test('hydrate passes through a product this tablet has not loaded yet', () => {
  const payload = { id: 'new', name: 'Seasonal Bun', price: 60, reason: 'popular' };
  const [got] = hydrate([payload], []);
  assert.equal(got.name, 'Seasonal Bun');
  assert.equal(got.price, 60);
});

test('hydrate names the trigger from the local catalogue', () => {
  // The server sends because_id only — resolving the name there would cost an
  // extra query for something the client already has. This is what makes the
  // strip say "Goes well with Latte" instead of a bare "Suggested".
  const [got] = hydrate(
    [{ id: 'p1', name: 'Croissant', reason: 'often_together', because_id: 'p9' }],
    [{ id: 'p1', name: 'Croissant' }, { id: 'p9', name: 'Latte' }],
  );
  assert.equal(got.because_name, 'Latte');
});

test('hydrate leaves the label empty when the trigger is unknown', () => {
  const [got] = hydrate(
    [{ id: 'p1', name: 'Croissant', reason: 'often_together', because_id: 'gone' }],
    [{ id: 'p1', name: 'Croissant' }],
  );
  assert.equal(got.because_name, '');
});
