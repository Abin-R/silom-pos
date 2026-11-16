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

test('each try is cut off at the timeout and handed an abort signal', async () => {
  const { checkTillStock } = await load();
  const cart = [{ product_id: 'a', qty: 1 }];
  const signals = [];
  // A request that never answers, and ignores the signal as a stuck socket would.
  const hung = (body, signal) => {
    signals.push(signal);
    return new Promise(() => {});
  };
  const started = Date.now();
  assert.deepEqual(await checkTillStock(hung, cart, 30), { status: 'unavailable' });
  assert.ok(Date.now() - started < 1000);
  assert.equal(signals.length, 2); // retried once
  assert.ok(signals.every((s) => s && s.aborted === true));
});

test('a timed-out first try is retried and a good second answer passes', async () => {
  const { checkTillStock } = await load();
  const cart = [{ product_id: 'a', qty: 1 }];
  let calls = 0;
  const send = (body, signal) => {
    calls++;
    if (calls === 1) return new Promise(() => {});
    return Promise.resolve(res(200, { ok: true, blocked: [] }));
  };
  assert.deepEqual(await checkTillStock(send, cart, 30), { status: 'ok' });
  assert.equal(calls, 2);
});

test('a fast answer is not aborted and the default timeout is about five seconds', async () => {
  const { checkTillStock, TILL_CHECK_TIMEOUT_MS } = await load();
  assert.equal(TILL_CHECK_TIMEOUT_MS, 5000);
  let seen;
  const send = async (body, signal) => {
    seen = signal;
    return res(200, { ok: true, blocked: [] });
  };
  assert.deepEqual(await checkTillStock(send, [{ product_id: 'a', qty: 1 }]), { status: 'ok' });
  assert.equal(seen.aborted, false);
});

test('a request that rejects after the clock won is not an unhandled rejection', async () => {
  const { checkTillStock } = await load();
  const send = (body, signal) =>
    new Promise((_, reject) => signal.addEventListener('abort', () => reject(new Error('aborted'))));
  assert.deepEqual(await checkTillStock(send, [{ product_id: 'a', qty: 1 }], 20), {
    status: 'unavailable',
  });
});
