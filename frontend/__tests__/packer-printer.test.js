const test = require('node:test');
const assert = require('node:assert/strict');

// Which printer a self-order slip goes to at a two-printer branch.
//
// Mirror of printerForSlip from lib/localPrinterConfig.ts, mirrored rather than
// imported because there is no TS transform in this test setup.  Source of
// truth is lib/localPrinterConfig.ts; keep the two in step.
function printerForSlip(docType, customer, packer) {
  return docType === 'packer' && packer.enabled && packer.identifier ? packer : customer;
}

const customer = { enabled: true, identifier: 'TCP:192.168.1.10' };
const packer = { enabled: true, identifier: 'TCP:192.168.1.11' };

test('a packer slip goes to the packer printer', () => {
  assert.equal(printerForSlip('packer', customer, packer), packer);
});

test('a customer receipt goes to the customer printer', () => {
  assert.equal(printerForSlip(undefined, customer, packer), customer);
  assert.equal(printerForSlip('abbreviated', customer, packer), customer);
  assert.equal(printerForSlip('full', customer, packer), customer);
});

test('a packer slip falls back when no packer printer is set up', () => {
  assert.equal(printerForSlip('packer', customer, { enabled: false, identifier: '' }), customer);
});

test('a packer slip falls back when the packer printer is disabled', () => {
  assert.equal(printerForSlip('packer', customer, { ...packer, enabled: false }), customer);
});
