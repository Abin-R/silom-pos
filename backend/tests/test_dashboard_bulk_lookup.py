"""
Unit tests for the dashboard endpoint's bulk product lookup.

These tests verify that:
1. The dashboard computes cost_total, profit, top_products, and top_categories
   correctly using the in-memory product_map (no per-item DB queries).
2. Products missing from the DB are handled gracefully (cost treated as 0,
   category falls back to "Other").
3. An empty order set returns zeroed-out metrics without errors.
"""
import sys
import os
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
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

    def sort(self, *args, **kwargs):
        return self

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

NOW = datetime.now(timezone.utc)


def make_order(items, days_ago=0):
    created = (NOW - timedelta(days=days_ago)).isoformat()
    total = sum(i["price"] * i["qty"] for i in items)
    return {
        "total": total,
        "status": "completed",
        "created_at": created,
        "items": items,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cost_and_profit_calculated_correctly():
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

    import server
    original_db = server.db
    server.db = db
    try:
        result = await server.dashboard(period="month")
    finally:
        server.db = original_db

    assert result["cost"] == pytest.approx(180.0)
    assert result["profit"] == pytest.approx(600.0)
    assert result["total_sales"] == pytest.approx(780.0)
    assert result["tx_count"] == 2


@pytest.mark.asyncio
async def test_missing_product_treated_as_zero_cost():
    """Items whose product_id is not in the DB should contribute 0 to cost."""
    orders = [
        make_order([{"product_id": "prod-UNKNOWN", "name": "Ghost", "price": 200.0, "qty": 3}]),
    ]
    db = make_db(orders, PRODUCTS, CATEGORIES)

    import server
    original_db = server.db
    server.db = db
    try:
        result = await server.dashboard(period="month")
    finally:
        server.db = original_db

    assert result["cost"] == pytest.approx(0.0)
    assert result["profit"] == pytest.approx(result["total_sales"])


@pytest.mark.asyncio
async def test_top_categories_uses_product_map():
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

    import server
    original_db = server.db
    server.db = db
    try:
        result = await server.dashboard(period="month")
    finally:
        server.db = original_db

    cats = {c["name"]: c["total"] for c in result["top_categories"]}
    assert cats["Cakes"] == pytest.approx(590.0)
    assert cats["Cookies"] == pytest.approx(480.0)
    # Cakes should rank first
    assert result["top_categories"][0]["name"] == "Cakes"


@pytest.mark.asyncio
async def test_unknown_category_falls_back_to_other():
    """Products with a category_id not in the categories collection → 'Other'."""
    products_with_unknown_cat = [
        {"id": "prod-X", "cost": 10.0, "category_id": "cat-NONEXISTENT"},
    ]
    orders = [
        make_order([{"product_id": "prod-X", "name": "Mystery", "price": 100.0, "qty": 1}]),
    ]
    db = make_db(orders, products_with_unknown_cat, CATEGORIES)

    import server
    original_db = server.db
    server.db = db
    try:
        result = await server.dashboard(period="month")
    finally:
        server.db = original_db

    cat_names = [c["name"] for c in result["top_categories"]]
    assert "Other" in cat_names


@pytest.mark.asyncio
async def test_empty_orders_returns_zero_metrics():
    """Dashboard with no orders in the period must return all-zero numeric fields."""
    db = make_db([], PRODUCTS, CATEGORIES)

    import server
    original_db = server.db
    server.db = db
    try:
        result = await server.dashboard(period="month")
    finally:
        server.db = original_db

    assert result["total_sales"] == 0
    assert result["cost"] == 0.0
    assert result["profit"] == 0.0
    assert result["tx_count"] == 0
    assert result["avg_bill"] == 0
    assert result["top_products"] == []
    assert result["top_categories"] == []


@pytest.mark.asyncio
async def test_only_one_bulk_products_query_is_issued():
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

    import server
    original_db = server.db
    server.db = db
    try:
        await server.dashboard(period="month")
    finally:
        server.db = original_db

    # products.find must have been called exactly once (the bulk lookup)
    assert db.products.find.call_count == 1

    # The single call must use $in with all three unique product IDs
    call_args = db.products.find.call_args
    query = call_args[0][0]  # first positional arg
    assert "$in" in query.get("id", {})
    assert set(query["id"]["$in"]) == {"prod-A", "prod-B", "prod-C"}
