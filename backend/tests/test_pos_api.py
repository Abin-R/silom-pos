"""Backend API tests for Bakery POS."""
import os
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "https://point-of-sale-69.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def s():
    sess = requests.Session()
    sess.headers.update({"Content-Type": "application/json"})
    return sess


# ---------- Health ----------
class TestHealth:
    def test_root(self, s):
        r = s.get(f"{API}/")
        assert r.status_code == 200
        assert "message" in r.json()


# ---------- Categories ----------
class TestCategories:
    def test_list_categories_count(self, s):
        r = s.get(f"{API}/categories")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) == 6, f"expected 6 categories, got {len(data)}"
        names = {c["name"] for c in data}
        assert {"Favorite", "Choco Gems", "Mousse Cake", "Soft Cookies", "Dubai Chocolate", "Cookie Cake"} == names
        for c in data:
            assert "id" in c and "color" in c


# ---------- Products ----------
class TestProducts:
    def test_list_products_count(self, s):
        r = s.get(f"{API}/products")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 25, f"expected 25 products, got {len(data)}"
        for p in data:
            assert "price" in p and p["price"] > 0
            assert "image_url" in p and p["image_url"].startswith("http")
            assert "category_id" in p

    def test_products_favorite_filter(self, s):
        r = s.get(f"{API}/products?favorite=true")
        assert r.status_code == 200
        data = r.json()
        assert len(data) >= 1
        assert all(p["is_favorite"] is True for p in data)

    def test_products_category_filter(self, s):
        cats = s.get(f"{API}/categories").json()
        cg = next(c for c in cats if c["name"] == "Choco Gems")
        r = s.get(f"{API}/products?category_id={cg['id']}")
        assert r.status_code == 200
        data = r.json()
        assert len(data) >= 1
        assert all(p["category_id"] == cg["id"] for p in data)


# ---------- Auth ----------
class TestAuth:
    def test_verify_pin_admin(self, s):
        r = s.post(f"{API}/auth/verify-pin", json={"pin": "1234"})
        assert r.status_code == 200
        j = r.json()
        assert j["success"] is True
        assert j["staff_name"] == "Admin"

    def test_verify_pin_cashier(self, s):
        r = s.post(f"{API}/auth/verify-pin", json={"pin": "0000"})
        assert r.status_code == 200
        assert r.json()["staff_name"] == "Cashier"

    def test_verify_pin_invalid(self, s):
        r = s.post(f"{API}/auth/verify-pin", json={"pin": "9999"})
        assert r.status_code == 401


# ---------- Customers ----------
class TestCustomers:
    def test_list_customers(self, s):
        r = s.get(f"{API}/customers")
        assert r.status_code == 200
        data = r.json()
        assert len(data) >= 7

    def test_create_customer(self, s):
        payload = {"name": "TEST_Customer", "phone": "0999999999"}
        r = s.post(f"{API}/customers", json=payload)
        assert r.status_code == 200
        c = r.json()
        assert c["name"] == payload["name"]
        assert c["phone"] == payload["phone"]
        assert "id" in c
        # verify in list
        lst = s.get(f"{API}/customers?q=TEST_Customer").json()
        assert any(x["id"] == c["id"] for x in lst)


# ---------- Orders ----------
class TestOrders:
    @pytest.fixture
    def sample_order(self, s):
        products = s.get(f"{API}/products").json()
        p = products[0]
        return {
            "items": [{"product_id": p["id"], "name": p["name"], "price": p["price"], "qty": 2}],
            "subtotal": p["price"] * 2,
            "discount_type": "none",
            "discount_value": 0,
            "discount_amount": 0,
            "total": p["price"] * 2,
            "payment_method": "PromptPay",
            "paid_amount": p["price"] * 2,
            "change": 0,
            "source": "table",
        }

    def test_create_order_and_persist(self, s, sample_order):
        r = s.post(f"{API}/orders", json=sample_order)
        assert r.status_code == 200, r.text
        o = r.json()
        assert o["order_number"].startswith("GF-")
        assert o["total"] == sample_order["total"]
        assert o["status"] == "completed"
        assert o["source"] == "table"
        # verify list contains
        lst = s.get(f"{API}/orders").json()
        assert any(x["id"] == o["id"] for x in lst)

    def test_list_orders_by_source_delivery(self, s):
        r = s.get(f"{API}/orders?source=delivery")
        assert r.status_code == 200
        data = r.json()
        assert len(data) >= 1
        assert all(x["source"] == "delivery" for x in data)
        assert any(x.get("delivery_provider") == "Grab" for x in data)

    def test_update_order_status(self, s, sample_order):
        create = s.post(f"{API}/orders", json=sample_order).json()
        oid = create["id"]
        r = s.put(f"{API}/orders/{oid}/status", json={"status": "cancelled"})
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"


# ---------- Parked Orders ----------
class TestParkedOrders:
    def test_parked_crud(self, s):
        products = s.get(f"{API}/products").json()
        p = products[0]
        payload = {
            "label": "TEST_Park",
            "items": [{"product_id": p["id"], "name": p["name"], "price": p["price"], "qty": 1}],
            "subtotal": p["price"],
        }
        # create
        r = s.post(f"{API}/parked-orders", json=payload)
        assert r.status_code == 200
        pk = r.json()
        pid = pk["id"]
        # list
        lst = s.get(f"{API}/parked-orders").json()
        assert any(x["id"] == pid for x in lst)
        # delete
        r2 = s.delete(f"{API}/parked-orders/{pid}")
        assert r2.status_code == 200
        lst2 = s.get(f"{API}/parked-orders").json()
        assert not any(x["id"] == pid for x in lst2)
