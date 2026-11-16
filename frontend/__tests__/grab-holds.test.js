const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

// The till's Grab-hold gate (lib/grabHolds.ts).  Imports the real TypeScript:
// Node strips the types.
const load = () => import(path.join(__dirname, '..', 'lib', 'grabHolds.ts'));

const res = (status, body) => ({ status, json: async () => body });

test('request body: paid lines by id, free items by the real product', async () => {
  const { buildTillCheckBody } = await load();
  const cart = [
    { product_id: 'a', qty: 2, price: 10, name: 'A' },
    { product_id: 'free-1', qty: 1, free_of: 'a', real_product_id: 'b' },
  ];
  assert.deepEqual(buildTillCheckBody(cart), {
    items: [{ product_id: 'a', qty: 2 }],
    free_items: [{ product_id: 'b', qty: 1 }],
  });
});

test('ok passes, blocked names the products', async () => {
  const { checkTillStock } = await load();
  const cart = [{ product_id: 'a', qty: 3 }];
  assert.deepEqual(await checkTillStock(async () => res(200, { ok: true, blocked: [] }), cart), {
    status: 'ok',
  });
  const blocked = [{ product_id: 'a', name: 'A', wanted: 3, can_sell: 1, held_for_grab: 2 }];
  assert.deepEqual(await checkTillStock(async () => res(200, { ok: false, blocked }), cart), {
    status: 'blocked',
    blocked,
  });
});

test('a failed check retries once, then blocks as unavailable', async () => {
  const { checkTillStock } = await load();
  const cart = [{ product_id: 'a', qty: 1 }];

  let calls = 0;
  const flaky = async () => {
    calls++;
    if (calls === 1) throw new Error('Network request failed');
    return res(200, { ok: true, blocked: [] });
  };
  assert.deepEqual(await checkTillStock(flaky, cart), { status: 'ok' });
  assert.equal(calls, 2);

  calls = 0;
  const dead = async () => {
    calls++;
    return res(500, null);
  };
  assert.deepEqual(await checkTillStock(dead, cart), { status: 'unavailable' });
  assert.equal(calls, 2);
});

test('nothing unclear lets a sale through', async () => {
  const { readTillCheck } = await load();
  for (const [status, body] of [
    [200, null],
    [200, {}],
    [200, { ok: 'yes' }],
    [200, { ok: false, blocked: [] }],
    [401, { ok: true }],
    [500, { ok: true }],
  ]) {
    assert.equal(readTillCheck(status, body).status, 'unavailable', JSON.stringify([status, body]));
  }
});
