const test = require('node:test');
const assert = require('node:assert/strict');

// Two rules, mirrored from the code they describe, in the style of
// payment-methods.test.js — there is no React renderer in this project, so
// what is testable is the logic, lifted out of the component.
//
// Both exist because of REACT-NATIVE-4: `API DELETE /parked-orders/:uuid →
// 404`, 291 reports across 5 cashiers, still firing on 26 August.  A cashier
// taps Delete, nothing on screen changes while the request is out, so they
// tap again — and the second DELETE lands on a row the first one removed.

// ── Rule 1: the re-entry guard from pos.tsx's ParkedOrdersModal ─────────────
// Mirror of `deletingRef` + `del` + `retrieve`.
function makeParkedList({ onDelete, onRetrieve }) {
  const deleting = new Set();
  return {
    inFlight: () => [...deleting],
    async del(id) {
      if (deleting.has(id)) return false;   // the guard
      deleting.add(id);
      try {
        await onDelete(id);
      } finally {
        deleting.delete(id);
      }
      return true;
    },
    retrieve(item) {
      if (deleting.has(item.id)) return false;
      onRetrieve(item.items);
      this.del(item.id);
      return true;
    },
  };
}

test('a second tap while the first delete is in flight is ignored', async () => {
  const calls = [];
  let release;
  const gate = new Promise((r) => { release = r; });
  const list = makeParkedList({
    onDelete: (id) => { calls.push(id); return gate; },
    onRetrieve: () => {},
  });

  const first = list.del('abc');
  const second = list.del('abc');   // the double-tap
  release();
  await Promise.all([first, second]);

  assert.deepEqual(calls, ['abc'], 'the server should be asked exactly once');
  assert.equal(await second, false, 'the second tap reports itself as ignored');
});

test('the guard is released once the delete finishes', async () => {
  const calls = [];
  const list = makeParkedList({
    onDelete: (id) => { calls.push(id); return Promise.resolve(); },
    onRetrieve: () => {},
  });

  await list.del('abc');
  await list.del('abc');   // a deliberate second delete, later, is not a repeat

  assert.deepEqual(calls, ['abc', 'abc']);
  assert.deepEqual(list.inFlight(), [], 'nothing left marked in flight');
});

test('two different parked orders can be deleted at once', async () => {
  const calls = [];
  let release;
  const gate = new Promise((r) => { release = r; });
  const list = makeParkedList({
    onDelete: (id) => { calls.push(id); return gate; },
    onRetrieve: () => {},
  });

  const a = list.del('aaa');
  const b = list.del('bbb');
  release();
  await Promise.all([a, b]);

  assert.deepEqual(calls.sort(), ['aaa', 'bbb']);
});

test('double-tapping Retrieve loads the cart once, not twice', async () => {
  const carts = [];
  let release;
  const gate = new Promise((r) => { release = r; });
  const list = makeParkedList({
    onDelete: () => gate,
    onRetrieve: (items) => carts.push(items),
  });

  const item = { id: 'abc', items: [{ name: 'Croissant', qty: 1 }] };
  list.retrieve(item);
  list.retrieve(item);   // the double-tap
  release();
  await new Promise((r) => setImmediate(r));

  assert.equal(carts.length, 1, 'the cart must not be loaded twice');
});

// ── Rule 2: what apiFetch reports to Sentry ────────────────────────────────
// Mirror of the status handling in lib/api.ts.
function reportsToSentry(method, status) {
  if (status === 401) return false;              // token dropped, handled
  if (status === 404 && method === 'DELETE') return false;  // already gone
  return status < 200 || status >= 300;
}

test('a DELETE that 404s is not reported — the row is already gone', () => {
  assert.equal(reportsToSentry('DELETE', 404), false);
});

test('a GET that 404s is still reported', () => {
  assert.equal(reportsToSentry('GET', 404), true,
    'something missing that should be there is still worth hearing about');
});

test('a DELETE that fails for any other reason is still reported', () => {
  assert.equal(reportsToSentry('DELETE', 500), true);
  assert.equal(reportsToSentry('DELETE', 403), true);
});

test('a successful DELETE is not reported', () => {
  assert.equal(reportsToSentry('DELETE', 204), false);
  assert.equal(reportsToSentry('DELETE', 200), false);
});
