const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

// On a shared queue the order type on the slip is the only thing telling a
// counter sale from a self-order.  Every place that builds a slip has to hand
// over what the server sent: a reprint that dropped queue_number once printed
// the wrong number, and dropping this would quietly print no order type.  The
// server sends it blank on other branches, so a slip must never fall back to
// Order.source or a default, or the line reaches branches without the flag.

const read = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');

for (const file of ['app/pos.tsx', 'app/admin.tsx', 'lib/useSelfOrderPrinting.tsx']) {
  test(`${file} passes the order type to the slip`, () => {
    assert.match(read(file), /source: (order|o)\.receipt_order_type,/);
  });

  test(`${file} has no queue prefix left`, () => {
    assert.doesNotMatch(read(file), /queue_label/);
  });
}

test('the slip labels every order type the till and self-order create', () => {
  const receipt = read('components/ReceiptImage.tsx');
  for (const source of ['counter', 'self_order', 'delivery']) {
    assert.match(receipt, new RegExp(`^  ${source}: "`, 'm'));
  }
});
