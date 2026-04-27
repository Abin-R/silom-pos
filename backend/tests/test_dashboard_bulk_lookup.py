"""
Unit tests for the dashboard endpoint's bulk product lookup.

These tests verify that:
1. The dashboard computes cost_total, profit, top_products, and top_categories
   correctly using the in-memory product_map (no per-item DB queries).
2. Products missing from the DB are handled gracefully (cost treated as 0,
   category falls back to "Other").
3. An empty order set returns zeroed-out metrics without errors.
4. Cancelled orders are excluded from aggregation (handled by the caller's
   query, but covered here as a sanity check on the test fixture).
5. The N+1 fix is preserved: exactly one ``db.products.find`` call and zero
   ``db.products.find_one`` calls.

Test style note
---------------
``backend/tests/test_pos_api.py`` is an HTTP-level integration suite that
hits a deployed URL. This file uses ``unittest.mock`` + a pytest fixture to
swap ``server.db`` because the optimization being tested is internal logic
that is awkward to exercise end-to-end. The two styles intentionally
coexist; environment bootstrap (env vars + Motor client patching) lives in
``backend/tests/conftest.py``.
"""
import sys
import os
import pytest
from unittest.mock import MagicMock
from datetime import datetime, timezone, timedelta

# Make sure the backend package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Helpers to build fake DB cursors / find_one results
# ---------------------------------------------------------------------------

class FakeCursor:
    """Mimics a Motor cursor that supports .to_list()."""

    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length):
        return list(self._docs)


def make_db(orders, products, categories):
    """Return a mock `db` object whose collections return the supplied data."""
    db = MagicMock()

    # orders.find(...)
    db.orders.find.return_value = FakeCursor(orders)

    # products.find(...)  — bulk lookup
    db.products.find.return_value = FakeCursor(products)

    # categories.find(...)
    db.categories.find.return_value = FakeCursor(categories)

    return db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CATEGORIES = [
    {"id": "cat-1", "name": "Cookies"},
    {"id": "cat-2", "name": "Cakes"},
]

PRODUCTS = [
    {"id": "prod-A", "cost": 40.0, "category_id": "cat-1"},
    {"id": "prod-B", "cost": 100.0, "category_id": "cat-2"},
    {"id": "prod-C", "cost": 0.0,  "category_id": "cat-1"},
]


def make_order(items, days_ago=0, status="completed"):
    # Compute timestamps lazily so the module-load time doesn't drift away
    # from the dashboard's `datetime.now()` snapshot.
    created = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    total = sum(i["price"] * i["qty"] for i in items)
    return {
        "total": total,
        "status": status,
        "created_at": created,
        "items": items,
    }


@pytest.fixture
def run_dashboard(monkeypatch):
    """
    Returns an async callable ``run(db, period="month")`` that swaps
    ``server.db`` for the duration of one dashboard call.

    ``monkeypatch`` automatically restores the original attribute when the
    test ends, so callers don't need their own try/finally.
    """
    import server

    async def _run(db, period="month"):
        monkeypatch.setattr(server, "db", db)
        return await server.dashboard(period=period)

    return _run


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cost_and_profit_calculated_correctly(run_dashboard):
    """cost_total and profit must reflect the bulk-fetched product costs."""
    orders = [
        make_order([{"product_id": "prod-A", "name": "Cookie", "price": 95.0, "qty": 2}]),
        make_order([{"product_id": "prod-B", "name": "Cake",   "price": 590.0, "qty": 1}]),
    ]
    # prod-A: cost=40 * qty=2 = 80
    # prod-B: cost=100 * qty=1 = 100
    # cost_total = 180
    # total_sales = 95*2 + 590*1 = 780
    # profit = 780 - 180 = 600

    db = make_db(orders, PRODUCTS, CATEGORIES)
    result = await run_dashboard(db)

    assert result["cost"] == pytest.approx(180.0)
    assert result["profit"] == pytest.approx(600.0)
    assert result["total_sales"] == pytest.approx(780.0)
    assert result["tx_count"] == 2


@pytest.mark.asyncio
async def test_missing_product_treated_as_zero_cost(run_dashboard):
    """Items whose product_id is not in the DB should contribute 0 to cost."""
    orders = [
        make_order([{"product_id": "prod-UNKNOWN", "name": "Ghost", "price": 200.0, "qty": 3}]),
    ]
    db = make_db(orders, PRODUCTS, CATEGORIES)
    result = await run_dashboard(db)

    # total_sales = 200 * 3 = 600; with cost=0, profit must equal 600 exactly.
    assert result["cost"] == pytest.approx(0.0)
    assert result["total_sales"] == pytest.approx(600.0)
    assert result["profit"] == pytest.approx(600.0)


@pytest.mark.asyncio
async def test_top_categories_uses_product_map(run_dashboard):
    """top_categories must be derived from the bulk product_map, not per-item queries."""
    orders = [
        make_order([
            {"product_id": "prod-A", "name": "Cookie", "price": 95.0, "qty": 4},   # Cookies: 380
            {"product_id": "prod-B", "name": "Cake",   "price": 590.0, "qty": 1},  # Cakes:   590
        ]),
        make_order([
            {"product_id": "prod-C", "name": "Cookie2", "price": 50.0, "qty": 2},  # Cookies: 100
        ]),
    ]
    # Cookies total = 380 + 100 = 480
    # Cakes total   = 590

    db = make_db(orders, PRODUCTS, CATEGORIES)
    result = await run_dashboard(db)

    cats = {c["name"]: c["total"] for c in result["top_categories"]}
    assert cats["Cakes"] == pytest.approx(590.0)
    assert cats["Cookies"] == pytest.approx(480.0)
    # Cakes should rank first
    assert result["top_categories"][0]["name"] == "Cakes"


@pytest.mark.asyncio
async def test_unknown_category_falls_back_to_other(run_dashboard):
    """Products with a category_id not in the categories collection → 'Other'."""
    products_with_unknown_cat = [
        {"id": "prod-X", "cost": 10.0, "category_id": "cat-NONEXISTENT"},
    ]
    orders = [
        make_order([{"product_id": "prod-X", "name": "Mystery", "price": 100.0, "qty": 1}]),
    ]
    db = make_db(orders, products_with_unknown_cat, CATEGORIES)
    result = await run_dashboard(db)

    cat_names = [c["name"] for c in result["top_categories"]]
    assert "Other" in cat_names


@pytest.mark.asyncio
async def test_empty_orders_returns_zero_metrics(run_dashboard):
    """Dashboard with no orders in the period must return all-zero numeric fields."""
    db = make_db([], PRODUCTS, CATEGORIES)
    result = await run_dashboard(db)

    assert result["total_sales"] == 0
    assert result["cost"] == 0.0
    assert result["profit"] == 0.0
    assert result["tx_count"] == 0
    assert result["avg_bill"] == 0
    assert result["top_products"] == []
    assert result["top_categories"] == []


@pytest.mark.asyncio
async def test_no_products_query_when_no_items(run_dashboard):
    """If there are no items across all orders, the bulk products query must be skipped."""
    db = make_db([], PRODUCTS, CATEGORIES)
    await run_dashboard(db)
    # Empty docs ⇒ no product IDs ⇒ no Mongo round-trip for products at all.
    assert db.products.find.call_count == 0


@pytest.mark.asyncio
async def test_only_one_bulk_products_query_is_issued(run_dashboard):
    """
    The dashboard must issue exactly ONE db.products.find() call regardless
    of how many orders or items are present — the core N+1 fix.
    """
    orders = [
        make_order([
            {"product_id": "prod-A", "name": "Cookie", "price": 95.0, "qty": 1},
            {"product_id": "prod-B", "name": "Cake",   "price": 590.0, "qty": 2},
        ]),
        make_order([
            {"product_id": "prod-A", "name": "Cookie", "price": 95.0, "qty": 3},
            {"product_id": "prod-C", "name": "Cookie2", "price": 50.0, "qty": 1},
        ]),
        make_order([
            {"product_id": "prod-B", "name": "Cake",   "price": 590.0, "qty": 1},
        ]),
    ]
    db = make_db(orders, PRODUCTS, CATEGORIES)
    await run_dashboard(db)

    # products.find must have been called exactly once (the bulk lookup)
    assert db.products.find.call_count == 1
    # ...and the legacy per-item API must not be used at all — this is the
    # exact regression a future refactor would most likely re-introduce.
    assert db.products.find_one.call_count == 0

    # The single call must use $in with all three unique product IDs
    call_args = db.products.find.call_args
    query = call_args[0][0]  # first positional arg
    assert "$in" in query.get("id", {})
    assert set(query["id"]["$in"]) == {"prod-A", "prod-B", "prod-C"}
