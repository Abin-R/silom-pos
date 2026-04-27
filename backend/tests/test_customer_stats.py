"""Unit tests for the GET /customers/{customer_id}/stats endpoint.

These tests mock the MongoDB collections so no live database is needed.
The existing test_pos_api.py suite uses live HTTP; this file uses mocks
so the stats logic can be verified in isolation without a running server.
"""
import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure the backend package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Provide dummy env vars before importing server
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_db")

from httpx import AsyncClient, ASGITransport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cursor(docs):
    """Return a mock that behaves like motor's AsyncIOMotorCursor."""
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=docs)
    cursor.sort = MagicMock(return_value=cursor)
    return cursor


def _make_collection(find_one_return=None, find_return=None):
    col = MagicMock()
    col.find_one = AsyncMock(return_value=find_one_return)
    col.find = MagicMock(return_value=_make_cursor(find_return or []))
    return col


def _products_collection(products_by_id: dict):
    """Return a mock products collection that looks up by id."""
    col = MagicMock()

    async def _find_one(query, projection=None):
        return products_by_id.get(query.get("id"))

    col.find_one = AsyncMock(side_effect=_find_one)
    return col


# ---------------------------------------------------------------------------
# Fixtures / constants
# ---------------------------------------------------------------------------

CUSTOMER_ID = "cust-001"
PRODUCT_ID_A = "prod-aaa"
PRODUCT_ID_B = "prod-bbb"
CATEGORY_ID_X = "cat-xxx"
CATEGORY_ID_Y = "cat-yyy"

CUSTOMER_DOC = {"id": CUSTOMER_ID, "name": "Alice", "phone": "0812345678", "color": "#EF4444"}

PRODUCT_A = {"id": PRODUCT_ID_A, "category_id": CATEGORY_ID_X}
PRODUCT_B = {"id": PRODUCT_ID_B, "category_id": CATEGORY_ID_Y}

CATEGORY_X = {"id": CATEGORY_ID_X, "name": "Choco Gems"}
CATEGORY_Y = {"id": CATEGORY_ID_Y, "name": "Cookie Cake"}

# Orders with snapshotted category_name (new format)
COMPLETED_ORDERS_WITH_SNAPSHOT = [
    {
        "id": "ord-1",
        "customer_id": CUSTOMER_ID,
        "status": "completed",
        "total": 350.0,
        "items": [
            {"product_id": PRODUCT_ID_A, "name": "Choco Pop", "price": 350.0, "qty": 1,
             "category_id": CATEGORY_ID_X, "category_name": "Choco Gems"},
        ],
    },
    {
        "id": "ord-2",
        "customer_id": CUSTOMER_ID,
        "status": "completed",
        "total": 598.0,
        "items": [
            {"product_id": PRODUCT_ID_A, "name": "Choco Pop", "price": 299.0, "qty": 2,
             "category_id": CATEGORY_ID_X, "category_name": "Choco Gems"},
        ],
    },
    {
        "id": "ord-3",
        "customer_id": CUSTOMER_ID,
        "status": "completed",
        "total": 690.0,
        "items": [
            {"product_id": PRODUCT_ID_B, "name": "Cookie Cake", "price": 690.0, "qty": 1,
             "category_id": CATEGORY_ID_Y, "category_name": "Cookie Cake"},
        ],
    },
]

# Legacy orders without category_name (pre-snapshot format)
COMPLETED_ORDERS_LEGACY = [
    {
        "id": "ord-1",
        "customer_id": CUSTOMER_ID,
        "status": "completed",
        "total": 350.0,
        "items": [
            {"product_id": PRODUCT_ID_A, "name": "Choco Pop", "price": 350.0, "qty": 1},
        ],
    },
    {
        "id": "ord-2",
        "customer_id": CUSTOMER_ID,
        "status": "completed",
        "total": 598.0,
        "items": [
            {"product_id": PRODUCT_ID_A, "name": "Choco Pop", "price": 299.0, "qty": 2},
        ],
    },
    {
        "id": "ord-3",
        "customer_id": CUSTOMER_ID,
        "status": "completed",
        "total": 690.0,
        "items": [
            {"product_id": PRODUCT_ID_B, "name": "Cookie Cake", "price": 690.0, "qty": 1},
        ],
    },
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_customer_stats_aggregates_correctly_with_snapshot():
    """Stats for a customer with 3 completed orders (snapshotted category) aggregate correctly."""
    import server

    customers_col = _make_collection(find_one_return=CUSTOMER_DOC)
    orders_col = _make_collection(find_return=COMPLETED_ORDERS_WITH_SNAPSHOT)
    # categories and products should NOT be queried when snapshot is present
    categories_col = _make_collection(find_return=[])
    products_col = _products_collection({})

    with patch.object(server, "db") as mock_db:
        mock_db.customers = customers_col
        mock_db.orders = orders_col
        mock_db.categories = categories_col
        mock_db.products = products_col

        transport = ASGITransport(app=server.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/customers/{CUSTOMER_ID}/stats")

    assert resp.status_code == 200
    data = resp.json()

    assert data["bill_count"] == 3
    assert data["outstanding_count"] == 0
    assert data["outstanding_total"] == 0.0

    expected_success = 350.0 + 598.0 + 690.0  # 1638.0
    assert data["success_total"] == pytest.approx(expected_success)
    assert data["avg_bill"] == pytest.approx(expected_success / 3)

    # Top products: PRODUCT_ID_A total=948, PRODUCT_ID_B total=690
    assert len(data["top_products"]) == 2
    assert data["top_products"][0]["product_id"] == PRODUCT_ID_A
    assert data["top_products"][0]["total"] == pytest.approx(948.0)
    assert data["top_products"][1]["product_id"] == PRODUCT_ID_B

    # Top categories: Choco Gems=948, Cookie Cake=690
    assert len(data["top_categories"]) == 2
    assert data["top_categories"][0]["name"] == "Choco Gems"
    assert data["top_categories"][0]["total"] == pytest.approx(948.0)
    assert data["top_categories"][1]["name"] == "Cookie Cake"

    # Verify no live product/category lookups were made (snapshot was used)
    products_col.find_one.assert_not_called()
    categories_col.find.assert_not_called()


@pytest.mark.asyncio
async def test_customer_stats_legacy_orders_fall_back_to_live_lookup():
    """Legacy orders without category_name fall back to live product/category lookup."""
    import server

    customers_col = _make_collection(find_one_return=CUSTOMER_DOC)
    orders_col = _make_collection(find_return=COMPLETED_ORDERS_LEGACY)
    categories_col = _make_collection(find_return=[CATEGORY_X, CATEGORY_Y])
    products_col = _products_collection({PRODUCT_ID_A: PRODUCT_A, PRODUCT_ID_B: PRODUCT_B})

    with patch.object(server, "db") as mock_db:
        mock_db.customers = customers_col
        mock_db.orders = orders_col
        mock_db.categories = categories_col
        mock_db.products = products_col

        transport = ASGITransport(app=server.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/customers/{CUSTOMER_ID}/stats")

    assert resp.status_code == 200
    data = resp.json()

    assert data["bill_count"] == 3
    assert data["success_total"] == pytest.approx(1638.0)

    # Categories resolved via live lookup
    assert len(data["top_categories"]) == 2
    assert data["top_categories"][0]["name"] == "Choco Gems"
    assert data["top_categories"][0]["total"] == pytest.approx(948.0)


@pytest.mark.asyncio
async def test_customer_stats_no_orders():
    """A customer with no orders should return all zeros and empty lists."""
    import server

    customers_col = _make_collection(find_one_return=CUSTOMER_DOC)
    orders_col = _make_collection(find_return=[])
    categories_col = _make_collection(find_return=[])
    products_col = _products_collection({})

    with patch.object(server, "db") as mock_db:
        mock_db.customers = customers_col
        mock_db.orders = orders_col
        mock_db.categories = categories_col
        mock_db.products = products_col

        transport = ASGITransport(app=server.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/customers/{CUSTOMER_ID}/stats")

    assert resp.status_code == 200
    data = resp.json()
    assert data["bill_count"] == 0
    assert data["success_total"] == 0.0
    assert data["avg_bill"] == 0.0
    assert data["outstanding_total"] == 0.0
    assert data["outstanding_count"] == 0
    assert data["top_products"] == []
    assert data["top_categories"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_status,expected_outstanding_count,expected_outstanding_total", [
    ("cancel", 0, 0.0),
    ("pending", 1, 150.0),
])
async def test_customer_stats_status_filtering(extra_status, expected_outstanding_count, expected_outstanding_total):
    """Cancelled orders are excluded entirely; other non-completed statuses count as outstanding."""
    import server

    orders = [
        {
            "id": "ord-ok",
            "customer_id": CUSTOMER_ID,
            "status": "completed",
            "total": 500.0,
            "items": [{"product_id": PRODUCT_ID_A, "name": "Choco Pop", "price": 500.0, "qty": 1,
                       "category_id": CATEGORY_ID_X, "category_name": "Choco Gems"}],
        },
        {
            "id": "ord-extra",
            "customer_id": CUSTOMER_ID,
            "status": extra_status,
            "total": 150.0,
            "items": [],
        },
    ]

    customers_col = _make_collection(find_one_return=CUSTOMER_DOC)
    orders_col = _make_collection(find_return=orders)
    categories_col = _make_collection(find_return=[CATEGORY_X])
    products_col = _products_collection({PRODUCT_ID_A: PRODUCT_A})

    with patch.object(server, "db") as mock_db:
        mock_db.customers = customers_col
        mock_db.orders = orders_col
        mock_db.categories = categories_col
        mock_db.products = products_col

        transport = ASGITransport(app=server.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/customers/{CUSTOMER_ID}/stats")

    assert resp.status_code == 200
    data = resp.json()
    assert data["bill_count"] == 1
    assert data["success_total"] == pytest.approx(500.0)
    assert data["outstanding_count"] == expected_outstanding_count
    assert data["outstanding_total"] == pytest.approx(expected_outstanding_total)


@pytest.mark.asyncio
async def test_customer_stats_not_found():
    """Requesting stats for a non-existent customer should return 404."""
    import server

    customers_col = _make_collection(find_one_return=None)

    with patch.object(server, "db") as mock_db:
        mock_db.customers = customers_col

        transport = ASGITransport(app=server.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/customers/nonexistent-id/stats")

    assert resp.status_code == 404
