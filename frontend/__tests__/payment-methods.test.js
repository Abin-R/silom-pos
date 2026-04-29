const test = require('node:test');
const assert = require('node:assert/strict');

// Mirror of PAYMENT_METHODS from pos.tsx
const PAYMENT_METHODS = Object.freeze({
  CASH: 'Cash',
  BEAM: 'Beam',
  EASY_PAY: 'Easy Pay',
  CREDIT: 'Credit',
  PROMPTPAY: 'PromptPay',
  QR_KBANK: 'QR Kbank',
  EDC: 'EDC',
  CUSTOM: 'Custom',
});

// Mirror of THB helper from pos.tsx
const THB = (n) =>
  `฿${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

function buildConfirmPayment(opts) {
  const { method, customPick, cardType, bankPick, cardLast4, total, paid } = opts;

  if (method === PAYMENT_METHODS.BEAM) {
    return { action: 'beam', ref: 'POS-test' };
  }

  const finalMethod =
    method === PAYMENT_METHODS.CUSTOM && customPick
      ? `${PAYMENT_METHODS.CUSTOM} · ${customPick}`
      : method === PAYMENT_METHODS.CREDIT && (cardType || bankPick)
        ? `${PAYMENT_METHODS.CREDIT} · ${cardType || bankPick}${cardLast4 ? ` ····${cardLast4}` : ''}`
        : method;

  const finalPaid =
    method === PAYMENT_METHODS.QR_KBANK ||
    method === PAYMENT_METHODS.PROMPTPAY ||
    method === PAYMENT_METHODS.CUSTOM ||
    method === PAYMENT_METHODS.CREDIT
      ? total
      : paid;

  return { action: 'pay', finalMethod, finalPaid };
}

const base = {
  customPick: '',
  cardType: '',
  bankPick: '',
  cardLast4: '',
  total: 500,
  paid: 300,
};

test('PAYMENT_METHODS has all 8 payment methods', () => {
  assert.equal(Object.keys(PAYMENT_METHODS).length, 8);
  assert.equal(PAYMENT_METHODS.CASH, 'Cash');
  assert.equal(PAYMENT_METHODS.BEAM, 'Beam');
  assert.equal(PAYMENT_METHODS.EASY_PAY, 'Easy Pay');
  assert.equal(PAYMENT_METHODS.CREDIT, 'Credit');
  assert.equal(PAYMENT_METHODS.PROMPTPAY, 'PromptPay');
  assert.equal(PAYMENT_METHODS.QR_KBANK, 'QR Kbank');
  assert.equal(PAYMENT_METHODS.EDC, 'EDC');
  assert.equal(PAYMENT_METHODS.CUSTOM, 'Custom');
  assert.equal(Object.isFrozen(PAYMENT_METHODS), true);
});

test('THB formats round number', () => {
  assert.equal(THB(100), '฿100.00');
});

test('THB formats with thousands separator', () => {
  assert.equal(THB(12480), '฿12,480.00');
});

test('THB formats zero', () => {
  assert.equal(THB(0), '฿0.00');
});

test('THB preserves fractional digits', () => {
  assert.equal(THB(9.5), '฿9.50');
  assert.equal(THB(1234.567), '฿1,234.57');
});

test('buildConfirmPayment returns beam action for Beam', () => {
  assert.deepEqual(buildConfirmPayment({ ...base, method: PAYMENT_METHODS.BEAM }), {
    action: 'beam',
    ref: 'POS-test',
  });
});

test('buildConfirmPayment returns Cash with paid amount', () => {
  assert.deepEqual(buildConfirmPayment({ ...base, method: PAYMENT_METHODS.CASH }), {
    action: 'pay',
    finalMethod: 'Cash',
    finalPaid: 300,
  });
});

test('buildConfirmPayment returns total for QR Kbank', () => {
  assert.deepEqual(buildConfirmPayment({ ...base, method: PAYMENT_METHODS.QR_KBANK }), {
    action: 'pay',
    finalMethod: 'QR Kbank',
    finalPaid: 500,
  });
});

test('buildConfirmPayment returns total for PromptPay', () => {
  assert.deepEqual(buildConfirmPayment({ ...base, method: PAYMENT_METHODS.PROMPTPAY }), {
    action: 'pay',
    finalMethod: 'PromptPay',
    finalPaid: 500,
  });
});

test('buildConfirmPayment formats Custom with pick', () => {
  assert.deepEqual(
    buildConfirmPayment({ ...base, method: PAYMENT_METHODS.CUSTOM, customPick: 'EDC Kbank' }),
    { action: 'pay', finalMethod: 'Custom · EDC Kbank', finalPaid: 500 }
  );
});

test('buildConfirmPayment returns bare Custom when no pick selected', () => {
  assert.deepEqual(
    buildConfirmPayment({ ...base, method: PAYMENT_METHODS.CUSTOM, customPick: '' }),
    { action: 'pay', finalMethod: 'Custom', finalPaid: 500 }
  );
});

test('buildConfirmPayment formats Credit with card type and last4', () => {
  assert.deepEqual(
    buildConfirmPayment({ ...base, method: PAYMENT_METHODS.CREDIT, cardType: 'VISA', cardLast4: '1234' }),
    { action: 'pay', finalMethod: 'Credit · VISA ····1234', finalPaid: 500 }
  );
});

test('buildConfirmPayment formats Credit with bank pick', () => {
  assert.deepEqual(
    buildConfirmPayment({ ...base, method: PAYMENT_METHODS.CREDIT, bankPick: 'Kbank' }),
    { action: 'pay', finalMethod: 'Credit · Kbank', finalPaid: 500 }
  );
});

test('buildConfirmPayment returns bare Credit when no card type or bank', () => {
  assert.deepEqual(buildConfirmPayment({ ...base, method: PAYMENT_METHODS.CREDIT }), {
    action: 'pay',
    finalMethod: 'Credit',
    finalPaid: 500,
  });
});

test('buildConfirmPayment returns EDC with paid amount', () => {
  assert.deepEqual(buildConfirmPayment({ ...base, method: PAYMENT_METHODS.EDC }), {
    action: 'pay',
    finalMethod: 'EDC',
    finalPaid: 300,
  });
});

test('buildConfirmPayment returns Easy Pay with paid amount', () => {
  assert.deepEqual(buildConfirmPayment({ ...base, method: PAYMENT_METHODS.EASY_PAY }), {
    action: 'pay',
    finalMethod: 'Easy Pay',
    finalPaid: 300,
  });
});
