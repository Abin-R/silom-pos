"""Unit tests for the GET /customers/{customer_id}/stats endpoint.

These tests mock the MongoDB collections so no live database is needed.
"""
import os
import sys
import pytest
import pytest_asyncio
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


# ---------------------------------------------------------------------------
# Fixtures
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

COMPLETED_ORDERS = [
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
async def test_customer_stats_aggregates_correctly():
    """Stats for a customer with 3 completed orders should aggregate correctly."""
    import server

    # Mock db collections
    customers_col = _make_collection(find_one_return=CUSTOMER_DOC)
    orders_col = _make_collection(find_return=COMPLETED_ORDERS)
    categories_col = _make_collection(find_return=[CATEGORY_X, CATEGORY_Y])

    # products.find_one returns different docs based on product_id arg
    async def mock_product_find_one(query, projection=None):
        pid = query.get("id")
        if pid == PRODUCT_ID_A:
            return PRODUCT_A
        if pid == PRODUCT_ID_B:
            return PRODUCT_B
        return None

    products_col = MagicMock()
    products_col.find_one = AsyncMock(side_effect=mock_product_find_one)

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

    # 3 completed orders, none outstanding
    assert data["bill_count"] == 3
    assert data["outstanding_count"] == 0
    assert data["outstanding_total"] == 0.0

    expected_success = 350.0 + 598.0 + 690.0  # 1638.0
    assert abs(data["success_total"] - expected_success) < 0.01

    expected_avg = expected_success / 3
    assert abs(data["avg_bill"] - expected_avg) < 0.01

    # Top products: PRODUCT_ID_A has total 350+598=948, PRODUCT_ID_B has 690
    assert len(data["top_products"]) == 2
    assert data["top_products"][0]["product_id"] == PRODUCT_ID_A
    assert abs(data["top_products"][0]["total"] - 948.0) < 0.01
    assert data["top_products"][1]["product_id"] == PRODUCT_ID_B

    # Top categories: Choco Gems=948, Cookie Cake=690
    assert len(data["top_categories"]) == 2
    assert data["top_categories"][0]["name"] == "Choco Gems"
    assert abs(data["top_categories"][0]["total"] - 948.0) < 0.01
    assert data["top_categories"][1]["name"] == "Cookie Cake"


@pytest.mark.asyncio
async def test_customer_stats_no_orders():
    """A customer with no orders should return all zeros and empty lists."""
    import server

    customers_col = _make_collection(find_one_return=CUSTOMER_DOC)
    orders_col = _make_collection(find_return=[])
    categories_col = _make_collection(find_return=[])
    products_col = MagicMock()
    products_col.find_one = AsyncMock(return_value=None)

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
async def test_customer_stats_excludes_cancelled_orders():
    """Cancelled orders must not count toward success_total or bill_count."""
    import server

    orders_with_cancel = [
        {
            "id": "ord-ok",
            "customer_id": CUSTOMER_ID,
            "status": "completed",
            "total": 500.0,
            "items": [{"product_id": PRODUCT_ID_A, "name": "Choco Pop", "price": 500.0, "qty": 1}],
        },
        {
            "id": "ord-cancelled",
            "customer_id": CUSTOMER_ID,
            "status": "cancel",
            "total": 999.0,
            "items": [{"product_id": PRODUCT_ID_A, "name": "Choco Pop", "price": 999.0, "qty": 1}],
        },
    ]

    customers_col = _make_collection(find_one_return=CUSTOMER_DOC)
    orders_col = _make_collection(find_return=orders_with_cancel)
    categories_col = _make_collection(find_return=[CATEGORY_X])

    async def mock_product_find_one(query, projection=None):
        return PRODUCT_A if query.get("id") == PRODUCT_ID_A else None

    products_col = MagicMock()
    products_col.find_one = AsyncMock(side_effect=mock_product_find_one)

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
    assert abs(data["success_total"] - 500.0) < 0.01
    assert abs(data["avg_bill"] - 500.0) < 0.01


@pytest.mark.asyncio
async def test_customer_stats_outstanding_orders():
    """Orders with status other than 'completed' or 'cancel' count as outstanding."""
    import server

    orders_mixed = [
        {
            "id": "ord-done",
            "customer_id": CUSTOMER_ID,
            "status": "completed",
            "total": 300.0,
            "items": [{"product_id": PRODUCT_ID_A, "name": "Choco Pop", "price": 300.0, "qty": 1}],
        },
        {
            "id": "ord-pending",
            "customer_id": CUSTOMER_ID,
            "status": "pending",
            "total": 150.0,
            "items": [],
        },
    ]

    customers_col = _make_collection(find_one_return=CUSTOMER_DOC)
    orders_col = _make_collection(find_return=orders_mixed)
    categories_col = _make_collection(find_return=[CATEGORY_X])

    async def mock_product_find_one(query, projection=None):
        return PRODUCT_A if query.get("id") == PRODUCT_ID_A else None

    products_col = MagicMock()
    products_col.find_one = AsyncMock(side_effect=mock_product_find_one)

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
    assert abs(data["success_total"] - 300.0) < 0.01
    assert data["outstanding_count"] == 1
    assert abs(data["outstanding_total"] - 150.0) < 0.01


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
