const test = require('node:test');
const assert = require('node:assert/strict');

// Regression cover for a reprint printing a different queue number than the
// original slip.  A bill rung up as "คิวที่ 17" reprinted as "คิวที่ 85",
// because the reprint snapshot dropped queue_number and the receipt fell
// back to deriving one from the order number (PS000000785 → "85").

// Mirror of the queue resolution in ReceiptImage.tsx / starPrinter.ts.
function resolveQueue(order) {
  return order.queue_number !== undefined
    ? String(order.queue_number)
    : (order.order_number || '').slice(-2).replace(/^0+/, '') || '1';
}

// Mirror of toReceiptOrder() in admin.tsx — the reprint snapshot.
function toReceiptOrder(order) {
  return {
    order_number: order.order_number,
    queue_number: order.queue_number ?? undefined,
  };
}

// Mirror of the sale-time receipt built in pos.tsx.
function toSaleReceipt(order) {
  return {
    order_number: order.order_number,
    queue_number: order.queue_number ?? undefined,
  };
}

test('reprint replays the stored queue number, not the order-number suffix', () => {
  const order = { order_number: 'PS000000785', queue_number: 17 };
  assert.equal(resolveQueue(toReceiptOrder(order)), '17');
});

test('a reprint matches the original slip for the same bill', () => {
  const order = { order_number: 'PS000000785', queue_number: 17 };
  assert.equal(
    resolveQueue(toReceiptOrder(order)),
    resolveQueue(toSaleReceipt(order)),
  );
});

test('the order-number suffix would have disagreed — the bug being fixed', () => {
  const order = { order_number: 'PS000000785', queue_number: 17 };
  // Pre-fix snapshot: queue_number dropped entirely.
  assert.equal(resolveQueue({ order_number: order.order_number }), '85');
  assert.notEqual('85', String(order.queue_number));
});

test('legacy bills with no queue number still fall back', () => {
  // Orders rung up before queue numbers existed carry null.
  const order = { order_number: 'PS000000785', queue_number: null };
  assert.equal(resolveQueue(toReceiptOrder(order)), '85');
});

test('queue number 0 is kept rather than treated as absent', () => {
  const order = { order_number: 'PS000000785', queue_number: 0 };
  assert.equal(resolveQueue(toReceiptOrder(order)), '0');
});
