const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const TYPES = [{ id: 'shop-supply', name: 'Shop Supply' }];

async function stockInTypes() {
  return import(path.join(__dirname, '..', 'lib', 'stockInTypes.ts'));
}

test('a failed stock-in type load can retry successfully without treating the failure as empty', async () => {
  const { loadStockInTypes } = await stockInTypes();
  let calls = 0;
  const fetcher = async (url) => {
    calls += 1;
    assert.equal(url, 'https://api.example.test/api/stock-in-reasons?active=true');
    if (calls === 1) return { ok: false, json: async () => [] };
    return { ok: true, json: async () => TYPES };
  };

  await assert.rejects(
    loadStockInTypes(fetcher, 'https://api.example.test/api'),
    /Unable to load stock-in types/,
  );
  assert.deepEqual(
    await loadStockInTypes(fetcher, 'https://api.example.test/api'),
    TYPES,
  );
  assert.equal(calls, 2);
});

test('stock-in save stays gated until both a line and type are selected', async () => {
  const { canSaveStockIn } = await stockInTypes();

  assert.equal(canSaveStockIn(0, ''), false);
  assert.equal(canSaveStockIn(1, ''), false);
  assert.equal(canSaveStockIn(0, 'Shop Supply'), false);
  assert.equal(canSaveStockIn(1, 'Shop Supply'), true);
});
